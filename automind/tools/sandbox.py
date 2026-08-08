"""代码沙箱 — 在**独立子进程**中执行受限的 Python 代码。

安全模型（v1.4.4 重写；此前的实现可被轻易逃逸，实测记录见下）
──────────────────────────────────────────────────────────────
旧实现在**同进程**里 `exec` 一段带白名单 builtins 的代码，实测四条逃逸路径全部成立：

  · ``import pathlib; pathlib.Path('x').write_text('pwned')``  → 直接写盘（已实测落地文件）
  · ``pathlib.os`` / ``uuid.os`` / ``random._os`` / ``doctest.os`` → 直接拿到完整 ``os``
  · ``enum.bltns`` → 直接拿到 ``builtins``（``open``/``__import__`` 全都回来了）
  · ``().__class__.__base__.__subclasses__()`` → 554 个类可达，经
    ``_wrap_close.__init__.__globals__`` 拿到 ``os.system``，即**任意命令执行**

根因有两层：
  1. **按模块名做白名单是无效的** —— 标准库模块普遍把 ``os``/``sys``/``builtins``
     作为普通属性挂在自己身上，放行任何一个模块几乎都等于放行整个解释器；
  2. **同进程 exec 无法真正隔离** —— 对象图是连通的，只要能沿属性走，
     总能从任意一个对象回到 ``builtins``。

因此改为三层防御，任何一层都不单独承担安全责任：

  第一层 · 静态 AST 校验：拒绝下划线开头的属性访问（一举封死 ``__class__``、
           ``__globals__``、``__subclasses__``、``_os`` 这一整类穿越），拒绝
           ``eval``/``exec``/``open``/``getattr`` 等危险名字，拒绝非白名单 import。
  第二层 · 受限运行时：白名单 builtins（**不含** ``getattr``/``open``/``object``），
           允许的模块一律用 ``_SafeModule`` 代理包一层 —— 只透出公开的非模块属性，
           ``pathlib.os``、``enum.bltns`` 这类穿越在运行时同样拿不到。
  第三层 · 进程隔离：代码跑在 ``python -I -S`` 子进程里。这一层解决的是
           "前两层被绕过时不至于失守"，同时修掉一个真实的可用性缺陷 ——
           旧实现用 ``asyncio.to_thread`` + ``wait_for``，超时只是**放弃等待**，
           线程仍在空转，``while True: pass`` 实测把整个进程挂死 2 分钟无法回收。
           子进程可以被 kill，超时是真的能停下来。

需要说明的边界：子进程仍以当前用户身份运行，本工具不提供操作系统级隔离
（容器 / seccomp / AppContainer）。它拦的是"模型写出的代码意外或被诱导越权"，
不是"有确定攻击意图的本地攻击者"。真要跑不可信代码，请在容器里部署。
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from automind.core.types import PermissionTier, ToolResult
from automind.tools.base import AbstractTool

# ── 白名单 ────────────────────────────────────────────────

SAFE_BUILTINS: frozenset[str] = frozenset({
    "abs", "all", "any", "ascii", "bin", "bool", "bytes", "callable",
    "chr", "complex", "dict", "divmod", "enumerate", "filter",
    "float", "format", "frozenset", "hash", "hex", "int", "isinstance",
    "issubclass", "iter", "len", "list", "map", "max", "min", "next",
    "oct", "ord", "pow", "print", "range", "repr", "reversed", "round",
    "set", "slice", "sorted", "str", "sum", "tuple", "type", "zip",
    "True", "False", "None", "Exception", "ValueError", "TypeError",
    "KeyError", "IndexError", "StopIteration", "ZeroDivisionError",
    "ArithmeticError", "AttributeError", "RuntimeError", "NotImplementedError",
})

# 相比旧版剔除：
#   pathlib / uuid / doctest / unittest —— 都把 os 作为普通属性挂出来
#   random —— random._os 同理（需要随机数可用 secrets？同样不给，沙箱不该碰系统熵之外的东西）
# 保留的模块仍会被 _SafeModule 包一层，属性穿越在运行时再拦一道。
ALLOWED_IMPORTS: frozenset[str] = frozenset({
    "math", "json", "re", "datetime", "collections", "itertools",
    "functools", "typing", "dataclasses", "enum", "copy",
    "textwrap", "string", "decimal", "fractions", "statistics",
})

# 这些名字即便不在 builtins 白名单里，也要在 AST 层显式拒绝：
# 用户代码可能通过参数默认值、闭包等方式拿到同名对象，先在语法层堵死更省心。
DENIED_NAMES: frozenset[str] = frozenset({
    "eval", "exec", "compile", "open", "input", "breakpoint", "help",
    "exit", "quit", "globals", "locals", "vars", "dir",
    "getattr", "setattr", "delattr", "hasattr",
    "__import__", "__builtins__", "memoryview", "object", "super",
    "classmethod", "staticmethod", "property",
})


class SandboxViolation(Exception):
    """代码在静态校验阶段就被判定越界。"""


def validate_code(code: str, allowed_imports: frozenset[str] = ALLOWED_IMPORTS) -> None:
    """执行前的静态校验；有任何越界构造直接抛 SandboxViolation。

    这里最关键的一条是**拒绝下划线开头的属性访问**。CPython 的沙箱逃逸几乎
    全部依赖 ``__class__`` / ``__bases__`` / ``__subclasses__`` / ``__globals__``
    这条对象图路径，或 ``random._os`` 这类私有别名；把"下划线属性"整类封掉，
    比逐个枚举黑名单可靠得多（黑名单永远漏）。
    """
    try:
        tree = ast.parse(code, "<sandbox>", "exec")
    except SyntaxError as e:
        raise SandboxViolation(f"语法错误：{e}") from e

    for node in ast.walk(tree):
        # 属性穿越：obj.__class__ / mod._os / f.__globals__ …
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise SandboxViolation(
                f"禁止访问下划线属性 '{node.attr}' —— 这是沙箱逃逸的主要路径")

        # 危险名字（含被剔除出 builtins 的那些）
        if isinstance(node, ast.Name) and node.id in DENIED_NAMES:
            raise SandboxViolation(f"禁止使用 '{node.id}'")

        # 赋值/形参等位置出现的下划线标识符不拦（局部变量叫 _ 很常见），
        # 但不允许自定义以下划线开头的**属性**（见上）。

        # import 白名单（与运行时的 __import__ 双保险）
        if isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                if top not in allowed_imports:
                    raise SandboxViolation(f"禁止导入 '{a.name}'")
        if isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            if node.level or top not in allowed_imports:
                raise SandboxViolation(f"禁止从 '{node.module or '.'}' 导入")

        # with / async 之类不拦；真正的能力边界由 builtins 与模块代理决定


# ── 子进程引导脚本 ────────────────────────────────────────
# 独立成串，用 -I（隔离模式，忽略环境变量与用户 site）+ -S（不加载 site）启动。
_CHILD = r'''
import ast, builtins, json, sys, types
from io import StringIO

def _apply_limits():
    """POSIX 资源限额 —— 在子进程自己身上设，单线程、无 fork 隐患。

    每条独立 try：某条不被内核支持（容器/BSD 差异）不能连累其余。
    刻意**不设** RLIMIT_NPROC —— 它按"用户"而非"进程"计数，置 0 会波及
    该用户的其它进程，且在容器里常导致子进程直接起不来；限制 fork 的目的
    已由 AST 层禁止 os/subprocess 达成。
    RLIMIT_FSIZE 也不设死为 0：stdout 是管道不受影响，但 Python 写
    __pycache__ 时会触发 SIGXFSZ，收益不抵风险。
    """
    try:
        import resource
    except ImportError:
        return
    for name, limit in (("RLIMIT_CPU", (30, 30)),
                        ("RLIMIT_AS", (1024 * 1024 * 1024,) * 2),
                        ("RLIMIT_CORE", (0, 0))):
        try:
            resource.setrlimit(getattr(resource, name), limit)
        except (ValueError, OSError, AttributeError):
            pass


_apply_limits()

payload = json.loads(sys.stdin.read())
code = payload["code"]
ALLOWED = frozenset(payload["allowed"])
SAFE = frozenset(payload["builtins"])
DENIED = frozenset(payload["denied"])

# ── 与父进程同一份静态校验（父进程已校验过，这里再来一遍，防止有人绕过父进程直调）
def validate(src):
    tree = ast.parse(src, "<sandbox>", "exec")
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError("禁止访问下划线属性 '%s'" % node.attr)
        if isinstance(node, ast.Name) and node.id in DENIED:
            raise ValueError("禁止使用 '%s'" % node.id)
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in ALLOWED:
                    raise ValueError("禁止导入 '%s'" % a.name)
        if isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "").split(".")[0] not in ALLOWED:
                raise ValueError("禁止从 '%s' 导入" % (node.module or "."))

class SafeModule:
    """模块代理 —— 只透出公开的、非模块的属性。

    这一层专治 pathlib.os / enum.bltns / random._os 这类"放行一个模块
    等于放行整个解释器"的穿越：模块型属性一律不给，下划线属性一律不给。
    """
    def __init__(self, mod):
        object.__setattr__(self, "_mod", mod)
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError("sandbox: 禁止访问 '%s'" % name)
        val = getattr(object.__getattribute__(self, "_mod"), name)
        if isinstance(val, types.ModuleType):
            raise AttributeError("sandbox: 禁止经模块属性 '%s' 穿越" % name)
        return val
    def __setattr__(self, name, value):
        raise AttributeError("sandbox: 模块只读")
    def __repr__(self):
        return "<sandbox module %r>" % object.__getattribute__(self, "_mod").__name__

def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    top = name.split(".")[0]
    if level or top not in ALLOWED:
        raise ImportError("sandbox: 禁止导入 '%s'" % name)
    return SafeModule(__import__(name, globals, locals, fromlist, level))

out, err = StringIO(), StringIO()
result = {"stdout": "", "stderr": "", "result": None, "error": None}
try:
    validate(code)
    safe_builtins = {n: getattr(builtins, n) for n in SAFE if hasattr(builtins, n)}
    safe_builtins["__import__"] = safe_import
    g = {"__builtins__": safe_builtins, "__name__": "__sandbox__"}
    for m in ALLOWED:
        try:
            g[m] = SafeModule(__import__(m))
        except ImportError:
            pass
    so, se = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        exec(compile(code, "<sandbox>", "exec"), g)
    finally:
        sys.stdout, sys.stderr = so, se
    val = g.get("result")
    try:
        json.dumps(val)
        result["result"] = val
    except (TypeError, ValueError):
        result["result"] = repr(val)[:2000]
except BaseException as e:
    result["error"] = "%s: %s" % (type(e).__name__, e)
result["stdout"] = out.getvalue()[:100000]
result["stderr"] = err.getvalue()[:100000]
sys.__stdout__.write(json.dumps(result))
'''


class PythonSandboxTool(AbstractTool):
    """Python 代码沙箱 — 独立子进程 + 静态校验 + 受限运行时。"""

    name = "python_sandbox"
    description = (
        "Execute Python code in an isolated subprocess sandbox. "
        "Only pure-computation stdlib modules are importable; filesystem, "
        "network and process APIs are unavailable. Returns stdout and errors."
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python code to execute."},
            "timeout": {"type": "number", "description": "Timeout in seconds (default: 30)."},
        },
        "required": ["code"],
    }
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 60

    # 保留类属性名以兼容既有引用
    SAFE_BUILTINS = SAFE_BUILTINS
    ALLOWED_IMPORTS = ALLOWED_IMPORTS

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    async def execute(self, **kwargs: Any) -> ToolResult:
        code = kwargs["code"]
        timeout = float(kwargs.get("timeout") or self.timeout)

        # 第一层：静态校验。越界代码根本不必启动子进程，
        # 并且能给出比运行时报错清楚得多的原因。
        try:
            validate_code(code)
        except SandboxViolation as e:
            return ToolResult(
                tool_name=self.name, success=False,
                error=f"沙箱拒绝执行：{e}",
                output={"stdout": "", "stderr": "", "violation": str(e)},
            )

        try:
            return await asyncio.to_thread(self._run_child, code, timeout)
        except Exception as e:   # 子进程启动失败等
            return ToolResult(tool_name=self.name, success=False,
                              error=f"{type(e).__name__}: {e}")

    def _run_child(self, code: str, timeout: float) -> ToolResult:
        payload = json.dumps({
            "code": code,
            "allowed": sorted(ALLOWED_IMPORTS),
            "builtins": sorted(SAFE_BUILTINS),
            "denied": sorted(DENIED_NAMES),
        })
        # -I：隔离模式（忽略 PYTHON* 环境变量与用户 site-packages）
        # -S：不自动 import site，减少可达对象
        argv = [sys.executable, "-I", "-S", "-c", _CHILD]
        kw: dict[str, Any] = {}
        if os.name == "nt":
            # Windows：新建进程组，超时时能连同其子孙一起结束
            kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            # POSIX：另起会话，kill 时不会波及父进程所在的进程组。
            # 资源限额由子进程自己设（见 _CHILD），不用 preexec_fn —— 那在
            # 多线程进程里不安全，而这里正是从线程池线程发起的。
            kw["start_new_session"] = True

        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", cwd=str(Path(sys.executable).parent), **kw)
        try:
            out, err = proc.communicate(payload, timeout=timeout)
        except subprocess.TimeoutExpired:
            # 关键：真的把它杀掉。旧实现只是不再等待，线程会一直空转下去。
            proc.kill()
            proc.communicate()
            return ToolResult(
                tool_name=self.name, success=False,
                error=f"代码执行超时（{timeout}s），已强制终止",
                output={"stdout": "", "stderr": "", "timeout": True},
            )

        if proc.returncode != 0 and not out.strip():
            return ToolResult(
                tool_name=self.name, success=False,
                error=f"沙箱子进程异常退出（code={proc.returncode}）：{err[:500]}",
                output={"stdout": "", "stderr": err[:2000]},
            )
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return ToolResult(
                tool_name=self.name, success=False,
                error="沙箱返回内容无法解析",
                output={"stdout": out[:2000], "stderr": err[:2000]},
            )

        return ToolResult(
            tool_name=self.name,
            success=data.get("error") is None,
            output={
                "stdout": data.get("stdout", ""),
                "stderr": data.get("stderr", ""),
                "result": data.get("result"),
            },
            error=data.get("error") or (data.get("stderr") or None),
        )


"""POSIX 资源限额已移入子进程引导脚本（_CHILD 顶部的 _apply_limits）。

原先走 `subprocess(preexec_fn=...)`，有两个问题：
  · **preexec_fn 在有线程的进程里不安全**（Python 文档明确警告：fork 与 exec
    之间只能调用 async-signal-safe 的函数，否则可能死锁）。而这里的
    `_run_child` 正是经 `asyncio.to_thread` 在线程池线程中调用的 —— 属于
    文档点名的危险用法。
  · 四条 setrlimit 共用一个 try，第一条失败会静默跳过其余三条。
移到子进程自己 exec 之后再设：那时是单线程、无 fork 顾虑，且每条独立容错。
"""
