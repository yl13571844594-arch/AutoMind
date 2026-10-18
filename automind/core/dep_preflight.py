"""依赖预检（v1.11.9）—— "开箱即用"不能靠猜，要能一键看清、一键补齐。

**为什么需要它**：内置工具与插件的依赖分三层 —— 核心依赖（pip 装包即有）、
可选依赖（office / 多媒体，缺库时工具返回安装提示但**任务会失败一步**）、
外部程序（ffmpeg / tesseract，pip 装不到）。此前没有任何地方能一次看全
"我的环境到底缺什么、缺了影响什么、怎么装"，用户只能等任务失败时逐个撞上。

**三个入口**：

* ``python -m automind.core.dep_preflight`` —— 全量自检，人读报告；
* ``automind doctor [--fix]`` —— 同上；``--fix`` 把缺的 Python 库直接装上；
* ``automind.server.main()`` 启动时静默快检 —— 有缺口就在横幅下打一行
  "缺 N 项，运行 automind doctor 查看"，不阻断启动。

**设计约束**：

* 检测用 :func:`importlib.util.find_spec`（只定位、不执行包代码）——
  ``import matplotlib`` 要 1~2 秒，启动快检不能背这个开销；
* 可选依赖的"模块名 ↔ pip 包名"映射**复用** ``tools/_toolkit.OPTIONAL_DEPS``，
  不另抄一份（抄了必然漂移，提示就成了空头支票）；
* 插件检查只做"清单 + 入口文件 + 语法编译"三件事，**不执行**插件代码
  （执行属于 Agent 构建时的正常加载，预检要快、要无副作用）。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from typing import Any

#: 核心依赖（pyproject 的 [project].dependencies）—— 缺了进程都起不来
CORE_DEPS: dict[str, tuple[str, str]] = {
    "pydantic": ("pydantic>=2.0", "配置与数据模型"),
    "yaml": ("pyyaml>=6.0", "YAML 配置解析"),
    "pydantic_settings": ("pydantic-settings>=2.0", "环境变量配置"),
    "rich": ("rich>=13.0", "CLI 增强输出"),
}

#: Web 工作台 / IM 机器人（pyproject 的 [project.optional-dependencies].web）
WEB_DEPS: dict[str, tuple[str, str]] = {
    "fastapi": ("fastapi>=0.110.0", "Web API 服务"),
    "uvicorn": ("uvicorn>=0.29.0", "Web 服务器"),
    "openai": ("openai>=1.30.0", "OpenAI 兼容模型接入"),
    "httpx": ("httpx>=0.27.0", "HTTP 请求 / 网页搜索"),
    "websockets": ("websockets>=12.0", "IM 机器人长连接"),
}

#: 浏览器 / MCP 等可选扩展（不在 [office] extra 里；缺了只影响对应工具）
EXTRA_DEPS: dict[str, tuple[str, str]] = {
    "playwright": ("playwright>=1.40", "浏览器自动化（browser 工具；还需 playwright install chromium 或系统 Edge/Chrome）"),
    "mcp": ("mcp>=1.0", "MCP 服务器接入"),
}

#: pip 装不到的外部程序（按平台过滤；检测与安装提示都来自 _toolkit）
_EXTERNAL_BINARIES: tuple[str, ...] = ("ffmpeg", "ffprobe", "tesseract", "git")


def _module_present(module: str) -> bool:
    """模块能否被定位（不执行其代码）。"""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        # find_spec 对带点的名字会触发父包导入；这里全是顶层名，防御性兜住
        return False


def check_python_deps() -> list[dict[str, Any]]:
    """核心 + Web + 可选（office / 多媒体）依赖逐项检查。"""
    from automind.tools._toolkit import OPTIONAL_DEPS

    groups: list[tuple[str, dict[str, tuple[str, str]], bool]] = [
        ("core", CORE_DEPS, True),
        ("web", WEB_DEPS, True),
        ("office", OPTIONAL_DEPS, False),   # 可选：缺了不致命，但要让人看见
        ("extra", EXTRA_DEPS, False),       # 浏览器 / MCP：缺了不影响核心工具
    ]
    out: list[dict[str, Any]] = []
    for kind, table, required in groups:
        for module, (package, purpose) in table.items():
            if module == "win32com" and sys.platform != "win32":
                continue                     # Windows 专属（Outlook / COM）
            out.append({
                "kind": kind, "name": module, "package": package,
                "purpose": purpose, "required": required,
                "ok": _module_present(module),
            })
    return out


def check_binaries() -> list[dict[str, Any]]:
    """外部程序（pip 装不到的那一半）检查。"""
    from automind.tools._toolkit import binary_install_hint, find_binary

    out: list[dict[str, Any]] = []
    for binary in _EXTERNAL_BINARIES:
        if binary == "tesseract" and not _module_present("pytesseract"):
            continue                         # 连壳都没装，先装壳（上面已报）
        found = find_binary(binary)
        out.append({
            "kind": "binary", "name": binary, "package": "",
            "purpose": "外部程序", "required": False,
            "ok": found is not None, "detail": found or "",
            "fix": binary_install_hint(binary),
        })
    return out


def check_builtin_plugins() -> list[dict[str, Any]]:
    """内置插件三件套检查：清单可解析、入口文件在、代码语法能编译。"""
    from automind.core.plugin import builtin_plugin_dir

    out: list[dict[str, Any]] = []
    base = builtin_plugin_dir()
    if not base.is_dir():
        return [{"kind": "plugin", "name": "(builtin_plugins)",
                 "ok": False, "detail": f"内置插件目录不存在：{base}", "fix": None}]
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        manifest = entry / "plugin.json"
        if not manifest.is_file():
            continue
        problems: list[str] = []
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            entry_point = str(data.get("entry_point", "hooks:get_hooks"))
            mod_name = entry_point.partition(":")[0] or "hooks"
        except (ValueError, OSError) as e:
            out.append({"kind": "plugin", "name": entry.name, "ok": False,
                        "detail": f"plugin.json 无法解析：{e}", "fix": None})
            continue
        code = entry / f"{mod_name}.py"
        if not code.is_file():
            problems.append(f"入口文件缺失：{code.name}")
        else:
            try:
                compile(code.read_text(encoding="utf-8"), str(code), "exec")
            except SyntaxError as e:
                problems.append(f"语法错误：第 {e.lineno} 行 {e.msg}")
        out.append({
            "kind": "plugin", "name": entry.name,
            "ok": not problems, "detail": "；".join(problems), "fix": None,
            "version": str(data.get("version", "")),
        })
    return out


def check_all() -> list[dict[str, Any]]:
    """全部检查项（Python 依赖 + 外部程序 + 内置插件）。"""
    return check_python_deps() + check_binaries() + check_builtin_plugins()


def missing_items(results: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """结果里未通过的项（插件失败也算 —— 装了却用不了同样是缺口）。"""
    results = results if results is not None else check_all()
    return [r for r in results if not r["ok"]]


def _pip_mirror_args() -> list[str]:
    """按配置给 pip 加国内镜像（AUTOMIND_NET_MIRROR=0 时直连官方源）。"""
    from automind.core.netresilience import PIP_MIRROR, mirror_enabled

    return ["-i", PIP_MIRROR] if mirror_enabled() else []


def ensure_missing(results: list[dict[str, Any]] | None = None,
                   *, quiet: bool = False) -> tuple[int, int]:
    """把缺的 **Python 库**装上（外部程序与插件不在此列）。

    返回 (成功数, 失败数)。逐个安装而不是拼一条命令：一个包失败
    不该拖累其余的，且报告能指名道姓说清哪个没装上。
    """
    from automind.core.netresilience import PIP_MIRROR

    todo = [r for r in missing_items(results)
            if r["kind"] in ("core", "web", "office", "extra")]
    ok = fail = 0
    for item in todo:
        pkg = item["package"]
        if not pkg:
            fail += 1
            continue
        cmd = [sys.executable, "-m", "pip", "install", pkg, *_pip_mirror_args()]
        if not quiet:
            print(f"  · 安装 {pkg}（{item['purpose']}）…")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as e:
            if not quiet:
                print(f"    ✗ {type(e).__name__}: {e}")
            fail += 1
            continue
        if proc.returncode == 0:
            ok += 1
            if not quiet:
                print(f"    ✓ {pkg} 已安装")
        else:
            fail += 1
            if not quiet:
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()
                print(f"    ✗ {pkg} 安装失败：{tail[-1] if tail else '未知原因'}"
                      f"（可重试或换源：pip install {pkg} -i {PIP_MIRROR}）")
    return ok, fail


# ── 报告渲染 ────────────────────────────────────────────────

_KIND_LABEL = {"core": "核心依赖", "web": "Web / 机器人", "office": "办公 / 多媒体（可选）",
               "extra": "浏览器 / MCP 扩展（可选）",
               "binary": "外部程序", "plugin": "内置插件"}


def render_report(results: list[dict[str, Any]] | None = None) -> str:
    """渲染人读报告（中文控制台）。"""
    from automind import __version__

    results = results if results is not None else check_all()
    lines = [f"AutoMind v{__version__} · Python {sys.version.split()[0]} · 依赖自检",
             ""]
    for kind in ("core", "web", "office", "extra", "binary", "plugin"):
        rows = [r for r in results if r["kind"] == kind]
        if not rows:
            continue
        lines.append(f"── {_KIND_LABEL[kind]} ──")
        for r in rows:
            mark = "✓" if r["ok"] else "✗"
            extra = ""
            if not r["ok"]:
                # 插件行没有 purpose / package（它们的"修法"是看 detail 排查）
                purpose = r.get("purpose") or "该能力"
                extra = f" —— {purpose}不可用"
                if r.get("fix"):
                    extra += f"。安装：{r['fix']}"
                elif r.get("package"):
                    extra += f"。安装：pip install {r['package']}"
                if r.get("detail"):
                    extra += f"（{r['detail']}）"
            elif r.get("detail") and r["kind"] == "binary":
                extra = f"（{r['detail']}）"
            lines.append(f"  {mark} {r['name']}{extra}")
        lines.append("")

    bad = missing_items(results)
    if not bad:
        lines.append("全部通过：工具与内置插件开箱即用。")
    else:
        py_missing = [r for r in bad if r["kind"] in ("core", "web", "office", "extra")]
        lines.append(f"共 {len(bad)} 项未就绪。")
        if py_missing:
            names = " ".join(r["package"] for r in py_missing)
            lines.append("一键补齐缺失的 Python 库：automind doctor --fix")
            lines.append(f"或手动：pip install {names}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """命令行入口：``python -m automind.core.dep_preflight [--fix] [--json]``。

    返回码：全部就绪（或 --fix 后全部就绪）为 0；仍有缺口为 1 ——
    外部程序（ffmpeg / tesseract）pip 装不到，--fix 补不齐它们，
    如实返回 1 让脚本 / 用户知道"还差这一步"。
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    fix = "--fix" in argv
    as_json = "--json" in argv

    results = check_all()
    if fix:
        # --json 时静默安装：进度行会污染 stdout 的机器可读输出
        ok, fail = ensure_missing(results, quiet=as_json)
        results = check_all()                 # 复检：刚装上的库现在应转为 ✓
        if not as_json:
            print(f"\n安装完成：成功 {ok} 个，失败 {fail} 个。"
                  "（外部程序需按报告里的提示单独安装，pip 装不到。）")

    if as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print(render_report(results))
    return 1 if missing_items(results) else 0


if __name__ == "__main__":                     # pragma: no cover - 命令行入口
    sys.exit(main())
