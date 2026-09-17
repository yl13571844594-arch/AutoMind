"""连接器 SDK — 用户不改源码就能给平台加自己的工具（v1.7.3）。

## 它解决的是什么

内置 32 个工具，但客户要接的多半是**自家内部系统**：工单系统、ERP、内部
HTTP API。此前只有两条路——fork ``automind/agent.py`` 去改
``_register_default_tools``，或者自己写一个 MCP server 再配进 mcp 配置。
前者意味着从此跟不上上游，后者意味着为了一个函数要搭一个进程。
两条路的交付成本都远超"把这段 HTTP 调用包成工具"本身。

现在：往 ``~/.automind/connectors/`` 扔一个 ``.py``，里面写一个
:class:`~automind.tools.base.AbstractTool` 子类，重启（或调一次
``/api/tools/reload``）即可用。**不改源码、不重启进程、不需要 MCP**。

## 安全模型 —— 先说清楚，再写代码

**连接器就是用户自己的代码，加载它等同于执行任意代码**（与
``agent.py`` 里连接 MCP server 是同一性质：MCP server 也是用户指定、
平台直接启动的进程）。这一点无法通过"沙箱"绕开 —— 一个能访问内网 API
的工具，本质上就必须拥有网络与凭据。所以本模块**不假装能把不可信代码
变安全**，而是把边界划清、把风险讲明：

1. **只扫指定目录，不递归**。默认只有 ``~/.automind/connectors/*.py``
   （可用 ``AUTOMIND_CONNECTORS_DIR`` 覆盖）。不递归子目录、不去别处
   找模块、不认 ``sys.path`` 上恰好同名的东西 —— "我把文件放这儿了"
   与"它被执行了"之间的对应关系必须是一一可见的。
2. **文件名过滤**：只收 ``*.py``，``_`` 开头的（``_helper.py``、
   ``__init__.py``）一律跳过。约定即安全：临时文件、编辑器备份、
   想手动跑一次的脚本都能靠在名字前加下划线来"停放"。
3. **失败的模块不留半个注册对象**。一个文件里的类逐个"构造成功才
   注册"，中途炸掉不会留下一个名字在、``execute`` 却是坏的工具。
4. **必须放在只有你能写的目录**。放在共享盘、可被他人 push 的仓库、
   Web 可写的目录里，等于让别人在你的 Agent 里执行任意代码。这条写在
   ``docs/CONNECTORS.md`` 的第一屏。

## 失败绝不静默（v1.6.4 / v1.7.2 的铁律）

连接器加载发生在**任务开始之前**，没有任何一步会因此报错——正是
"失败被伪装成成功"最容易存活的地方：用户以为工具装上了，模型却说
"没有这个工具"，两边都看不到原因。因此每个失败都同时做三件事：

* ``logger.warning`` 一条结构化日志；
* 记进 :func:`load_failures` 的账目（``file`` / ``path`` / ``error`` /
  ``hint``），供 ``/api/tools/registration`` 之类的端点展示；
* **一个文件坏掉不连累其余文件**（与 ``_register_default_tools`` 的分组
  注册同一原则：不连累 ≠ 不吭声）。

账目按文件去重、每次扫描时同步到"当前目录里真实存在的文件"——
删掉坏文件之后，那条失败也跟着消失，否则界面上会永远挂着一条已修好的
告警（这比不报还糟：用户会学会忽略它）。
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from automind.core.logging import get_logger
from automind.tools.base import AbstractTool

logger = get_logger("automind.connectors")

#: 用户连接器目录。与 ``~/.automind/plugins``（插件）、``~/.automind_license``
#: （授权）同级 —— 用户资产集中在一个地方，备份/迁移时不会漏。
DEFAULT_CONNECTOR_DIR = Path("~/.automind/connectors")

#: 目录覆盖变量；多个目录用 ``os.pathsep`` 分隔（Windows ``;`` / POSIX ``:``），
#: 靠后的目录覆盖靠前的同名工具 —— 与插件目录同向，"放后面就能盖住"。
ENV_DIR_VAR = "AUTOMIND_CONNECTORS_DIR"


def connector_dirs() -> list[Path]:
    """本次扫描要看的目录（按优先级从低到高）。

    环境变量**每次调用都重新读**：测试要能 monkeypatch 它，运维也可能在
    服务跑着的时候改配置再 reload —— 把目录缓存进单例会让 reload 用的还是
    老路径，那是最难查的一类"改了没反应"。
    """
    raw = os.environ.get(ENV_DIR_VAR, "").strip()
    if not raw:
        return [DEFAULT_CONNECTOR_DIR.expanduser()]
    dirs: list[Path] = []
    for part in raw.split(os.pathsep):
        # 从资源管理器复制路径常带引号，直接 Path() 会得到一个含引号的目录名
        cleaned = part.strip().strip('"').strip("'").strip()
        if cleaned:
            dirs.append(Path(cleaned).expanduser())
    return dirs or [DEFAULT_CONNECTOR_DIR.expanduser()]


# ═══════════════════════════════════════════════════════════════
# 账目与状态
# ═══════════════════════════════════════════════════════════════


@dataclass
class _Hit:
    """一个连接器文件注册出来的一个工具 —— **"谁给的"必须可追溯**。

    只记工具名是不够的：reload 时要能回答"这个工具是不是连接器给的、
    该不该由我摘掉"。少了这份归属关系，reload 就会把工具名当成凭空出现的
    东西，删掉文件后旧工具会一直挂在注册表里（模型仍能调用一个已经不存在的
    连接器，调用时才炸，而错误现场离真正的原因已经很远）。
    """

    name: str
    file: str
    path: str
    cls: str
    description: str = ""
    tier: str = ""
    #: 我们注册进去的那个实例。"摘旧工具"和"这个工具被人覆盖过没有"都靠它判定 ——
    #: 只看名字的话，无法区分"注册表里这个是连接器给的"与"已经换成别人的了"。
    tool: Any = None


@dataclass
class _State:
    """进程内的连接器账目（单例，见 :data:`_STATE`）。"""

    #: 文件绝对路径 -> 该文件注册出来的工具（一个文件可以有多个工具）
    hits: dict[str, list[_Hit]] = field(default_factory=dict)
    #: 文件绝对路径 -> 打开失败 / 构造失败的原因
    failures: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 计数器：给每次加载的模块起唯一名字（见 _load_file）
    seq: int = 0
    #: 上一次加载的 (注册表身份, 目录列表, 文件名列表) —— 用于识别"同一个注册表
    #: 又调了一次 load_connectors"。进程里出现第二个 AutoMindAgent 时（各入口/
    #: 各实例各建一个 Agent），它会拿着**同一个** ToolRegistry 再走一遍启动注册：
    #: 不识别的话，每个工具都被重新注册一遍，日志里刷满一屏"同名工具，只保留
    #: 最后扫到的那个"的告警 —— 一条真实告警被噪声淹掉，比不报还糟。
    #: 记注册表**本身**而不只是它的名字：换个注册表就是另一次加载，必须真注册。
    last: tuple[Any, tuple[str, ...], tuple[str, ...]] | None = None

    def clear(self) -> None:
        self.hits.clear()
        self.failures.clear()
        self.last = None


_STATE = _State()


def reset_state() -> None:
    """清空账目。**仅供测试**（以及将来"彻底重来"的运维入口）。"""
    _STATE.clear()


def load_failures() -> list[dict[str, Any]]:
    """本次加载失败的连接器 —— 供端点展示"你装的哪个连接器坏了、为什么"。

    返回的是**防御性拷贝**：调用方（Web 端点、CLI）拿到手就能改，改不动
    内部账目。返回同一份对象的话，一个端点顺手做的格式化会污染下一次调用，
    这类 bug 只在特定端点访问顺序下才出现，极难复现。
    """
    return [dict(f) for f in _STATE.failures.values()]


def loaded_connectors() -> list[dict[str, Any]]:
    """已加载的连接器工具清单（含它来自哪个文件）—— 供端点展示与排查。"""
    out: list[dict[str, Any]] = []
    for hits in _STATE.hits.values():
        for h in hits:
            out.append({"name": h.name, "file": h.file, "path": h.path,
                        "class": h.cls, "description": h.description, "tier": h.tier})
    out.sort(key=lambda d: d["name"])
    return out


def describe_dirs() -> dict[str, Any]:
    """当前扫描配置 —— 界面上最有用的那句话是"我到底在哪儿找"。"""
    dirs = connector_dirs()
    return {
        "env_var": ENV_DIR_VAR,
        "env_set": bool(os.environ.get(ENV_DIR_VAR, "").strip()),
        "separator": os.pathsep,
        "dirs": [
            {"path": str(d), "exists": d.is_dir()} for d in dirs
        ],
    }


# ═══════════════════════════════════════════════════════════════
# 发现
# ═══════════════════════════════════════════════════════════════


def _candidate_files() -> list[Path]:
    """所有候选连接器文件（已排序、已去重）。

    两条边界，都是为了"看得见"而不是为了防攻击者（能往这个目录写文件的人
    本来就能执行代码）：

    * ``glob("*.py")`` 而**不是** ``rglob`` —— 只扫这一层。递归会让
      "我把文件放这儿了"变成"它可能在任何子目录里"，排查时先要自己找一遍；
    * 跳过 ``_`` 开头的文件 —— 给临时脚本/草稿一个明确的停放位，
      而不是逼用户把它挪到目录外（挪出去下次就忘了拿回来）。
    """
    seen: set[str] = set()
    files: list[Path] = []
    for base in connector_dirs():
        if not base.is_dir():
            continue
        try:
            entries = sorted(base.glob("*.py"))
        except OSError as e:                              # pragma: no cover - 极端环境
            logger.warning("connector_dir_unreadable", dir=str(base),
                           error=f"{type(e).__name__}: {e}")
            continue
        for entry in entries:
            if entry.name.startswith("_") or not entry.is_file():
                continue
            key = str(entry.resolve()).casefold()
            if key in seen:                 # 同一文件出现在两个配置目录里
                continue
            seen.add(key)
            files.append(entry)
    return files


def _is_inside(path: Path, dirs: list[Path]) -> bool:
    """文件真实位置是否落在配置目录之内（拦截指向别处的符号链接）。

    "只扫指定目录"如果不管符号链接，就是一句空话：连接器目录里放一个指向
    任意位置的链接，照样会被执行。这里按 ``resolve()`` 之后的真实路径判定，
    越界的**记账并拒绝**（而不是静默跳过）—— 静默跳过正是 v1.6.4 要根治的
    那种"东西不见了但没人知道为什么"。
    """
    try:
        real = path.resolve()
    except OSError:                                       # pragma: no cover - 极端环境
        return False
    for base in dirs:
        try:
            base_real = base.resolve()
        except OSError:                                   # pragma: no cover - 极端环境
            continue
        if real == base_real or base_real in real.parents:
            return True
    return False


# ═══════════════════════════════════════════════════════════════
# 为什么失败 + 怎么修
# ═══════════════════════════════════════════════════════════════


def _hint_for(exc: BaseException) -> str:
    """把异常翻成**下一步动作**。

    连接器的失败几乎全落在四种里，每一种的修法完全不同：少装一个包（pip）、
    依赖没进来（解释器不对）、语法写错（看行号）、忘了 ``await``（返回值类型
    不对）。只报 ``TypeError: ...`` 的话，用户得自己先猜到是哪一类。
    """
    if isinstance(exc, ImportError):
        missing = getattr(exc, "name", "") or ""
        if missing and not missing.startswith("automind"):
            return f"缺依赖 {missing}：pip install {missing}（或改用标准库实现）"
        return ("导入失败。若是缺 automind 本身，说明加载它的 Python 解释器不是 "
                "安装 AutoMind 的那个（连接器由服务进程加载，用它来跑代码最准）")
    if isinstance(exc, SyntaxError):
        loc = f"{exc.filename or ''}:{exc.lineno or '?'}"
        return f"语法错误，位置 {loc} —— 修好后重跑一次 reload 即可"
    if isinstance(exc, NameError):
        return f"名字未定义（{exc}）：检查是否漏了 import，或写错了变量名"
    if isinstance(exc, AttributeError):
        return (f"属性不存在（{exc}）：若与 execute/name/parameters 有关，"
                f"多半是名称拼错或返回值写错了类型")
    if isinstance(exc, OSError):
        return "文件访问失败：确认路径存在、且当前用户有读权限"
    return "查看上面那条 warning 里的原始异常类型与消息定位问题"


def _record_failure(path: Path, exc: BaseException, *, stage: str = "") -> None:
    """记一条失败：**日志 + 账目**，两者缺一不可。

    只有日志 → 桌面版/Web 版用户看不到（默认只进 stderr）；
    只有账目 → 排查时拿不到堆栈上下文。v1.7.2 已经为工具分组定过这个规矩，
    连接器沿用同一套。

    账目按文件存（一个文件一条），而不是按类存：用户的心智模型是
    "我这个文件坏了"，一屏十几条同类报错只会盖住真正的第一现场。
    """
    key = _key_of(path)
    entry: dict[str, Any] = {
        "file": path.name,
        "path": str(path),
        "error": f"{type(exc).__name__}: {exc}",
        "hint": _hint_for(exc),
    }
    if stage:
        entry["stage"] = stage
    _STATE.failures[key] = entry
    logger.warning("connector_load_failed", file=path.name, path=str(path),
                   stage=stage or "load", error=entry["error"], hint=entry["hint"])


def _key_of(path: Path) -> str:
    """账目的键：绝对路径的规范化形式（Windows 大小写不敏感）。"""
    try:
        return str(path.resolve()).casefold()
    except OSError:                                       # pragma: no cover - 极端环境
        return str(path).casefold()


# ═══════════════════════════════════════════════════════════════
# 加载
# ═══════════════════════════════════════════════════════════════


def _load_file(path: Path, registry: Any) -> list[_Hit]:
    """加载单个连接器文件，返回它注册出来的工具。

    失败**不抛**，只记账 —— 一个文件写坏了不该让另外十个也装不上，
    更不该让服务起不来。空列表 = 这个文件没提供可用工具（原因已在账目里）。
    """
    if not _is_inside(path, connector_dirs()):
        _record_failure(path, ValueError(
            f"文件真实位置不在连接器目录内（符号链接指向了别处）：{path}"), stage="scan")
        return []

    _STATE.seq += 1
    # 模块名唯一：改完代码 reload 时，若复用同一个名字，``spec.loader.exec_module``
    # 虽然会重新执行，但残留的旧引用/父包缓存会让"改了不生效"变得难以解释。
    # 顺带避免两个目录里的同名文件互相覆盖 ``sys.modules``。
    mod_name = f"automind_connector_{_STATE.seq}_{path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            _record_failure(path, ImportError(f"无法为该文件建立模块规格：{path}"),
                            stage="spec")
            return []
        module = importlib.util.module_from_spec(spec)
        # 放进 sys.modules 再执行：连接器内部若做了 ``from __future__`` 之外的
        # 自引用（dataclass 装饰器、typing.get_type_hints）需要能查到自己。
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
    except BaseException as e:                            # noqa: BLE001 - 用户代码什么都可能抛
        sys.modules.pop(mod_name, None)
        _record_failure(path, e, stage="import")
        return []

    try:
        classes = _tool_classes(module)
    except Exception as e:                                # pragma: no cover - 防御性
        _record_failure(path, e, stage="inspect")
        return []

    if not classes:
        # 文件跑通了，但一个工具都没提供 —— 与"文件炸了"是两种问题，
        # 报错也要分开说：用户改的方向完全不同（一个去修异常，一个去补类）。
        _record_failure(path, LookupError(
            "文件里没有可用的工具类。连接器必须定义 AbstractTool 的子类，"
            "并且它不是抽象类、name 非空"), stage="scan")
        return []

    hits: list[_Hit] = []
    for cls in classes:
        name = str(getattr(cls, "name", "") or "").strip()
        if not name:
            _record_failure(path, ValueError(
                f"类 {cls.__name__} 的 name 是空的：模型只能通过 name 调用工具，"
                f"空名字等于没注册"), stage="scan")
            continue
        try:
            tool = cls()
        except BaseException as e:                        # noqa: BLE001 - 用户构造函数
            _record_failure(path, RuntimeError(
                f"类 {cls.__name__} 构造失败，未注册：{type(e).__name__}: {e}"), stage="init")
            continue
        try:
            registry.register(tool)
        except Exception as e:
            _record_failure(path, RuntimeError(f"类 {cls.__name__} 注册失败：{e}"), stage="register")
            continue
        hits.append(_Hit(
            name=name, file=path.name, path=str(path), cls=cls.__name__,
            description=str(getattr(cls, "description", "") or "").splitlines()[0][:120],
            tier=str(getattr(getattr(cls, "permission_tier", ""), "value", "") or ""),
            tool=tool,
        ))
        logger.info("connector_loaded", tool=name, file=path.name, cls=cls.__name__)
    return hits


def _tool_classes(module: Any) -> list[type]:
    """挑出模块里可用的工具类 —— **导入进来的基类不算**。

    过滤规则刻意保守：宁可少收，也不要收进来一个"看起来是工具、其实是
    从别的模块 import 进来的 AbstractTool 子类"。后者的典型后果是同一个
    工具被注册两次（连接器 A import 了连接器 B 的类），或者用户只是
    ``from automind.tools.net_tools import HttpRequestTool`` 想复用一下，
    结果把内置工具又注册了一遍并覆盖掉原对象。
    """
    out: list[type] = []
    for _, obj in vars(module).items():
        if not inspect.isclass(obj) or not issubclass(obj, AbstractTool):
            continue
        if getattr(obj, "__module__", "") != module.__name__:
            continue                                      # 别处定义、这里只是 import 进来
        if inspect.isabstract(obj):
            continue                                      # 抽象类只是半成品，实例化必炸
        if obj.__name__ == AbstractTool.__name__ and obj is AbstractTool:
            continue
        out.append(obj)
    return out


# ═══════════════════════════════════════════════════════════════
# 对外 API（父 agent 按这三个接线）
# ═══════════════════════════════════════════════════════════════


def load_connectors(registry: Any) -> list[str]:
    """扫描并加载连接器；返回**本次注册成功的工具名**（排序去重）。

    启动时调用一次即可。返回空列表是正常结果（用户没装连接器），
    不等于出错 —— "一个都没有"与"全都炸了"的区别看 :func:`load_failures`。

    **幂等**：同一个注册表、同一批目录、同一批工具连着调两次，第二次直接返回
    上次的结果、不再重新注册（见 ``_State.last``）。这不是优化，是正确性 ——
    进程里出现第二个 Agent 实例时，重扫一遍会把每个工具再注册一次，并把
    一屏"同名工具"告警刷进日志，把真正的告警淹掉。要强制重扫用
    :func:`reload_connectors`（它先摘后扫，语义明确）。
    """
    dirs = connector_dirs()
    files = _candidate_files()

    names = tuple(sorted({p.name for p in files}))
    fingerprint = (registry, tuple(str(d) for d in dirs), names)
    if _STATE.last is not None and fingerprint == _STATE.last:
        return sorted({h.name for hits in _STATE.hits.values() for h in hits})

    # 目录里已经没有的失败记录要清掉：删掉坏文件之后那条告警还挂着，
    # 用户会学会忽略这个面板，等于把整条"失败可见"的通道作废。
    live = {_key_of(p) for p in files}
    for stale in [k for k in _STATE.failures if k not in live]:
        _STATE.failures.pop(stale, None)

    loaded: list[str] = []
    for path in files:
        hits = _load_file(path, registry)
        _STATE.hits[_key_of(path)] = hits
        loaded.extend(h.name for h in hits)

    _warn_duplicates()
    _STATE.last = fingerprint
    if loaded:
        logger.info("connectors_loaded", count=len(loaded), tools=sorted(set(loaded)),
                    dirs=[str(d) for d in dirs])
    return sorted(set(loaded))


def reload_connectors(registry: Any) -> dict[str, list[str]]:
    """清掉上次由连接器注册的工具，重新扫描。

    返回 ``{"loaded": [...], "failed": [...], "removed": [...]}``。

    **先摘旧的、再扫新的**，而不是"直接覆盖同名"：覆盖只能处理"名字没变"，
    而开发连接器时的高频动作恰恰是改名 —— 改完名字直接 reload，旧名字会
    永远留在注册表里（模型能调用它，调用时才发现背后的文件早不叫这个了）。
    先摘后扫顺带把"删掉的文件"也一起收拾了。

    ``removed`` 是**上次有、这次没有**的工具名（文件被删、改名、或者这次
    加载失败）。列出来是为了让界面上能看到"刚才那次 reload 少了个东西"。
    """
    previous = sorted({h.name for hits in _STATE.hits.values() for h in hits})
    for hits in _STATE.hits.values():
        for h in hits:
            try:
                registry.unregister(h.name)
            except AttributeError:                        # pragma: no cover - 极简替身
                logger.debug("connector_registry_has_no_unregister", tool=h.name)
                break
            current = getattr(registry, "_tools", {}).get(h.name)
            if current is not None and current is not h.tool:
                # 名字还在，但已经是**别人的**工具了（重启内置注册、或另一份
                # 连接器抢先注册）。这不是"没摘干净"，摘下去反而会把别人的
                # 工具误删。记一笔是为了让"我的连接器怎么不见了"有据可查。
                logger.info("connector_tool_superseded", tool=h.name, file=h.file)
    _STATE.hits.clear()
    # 指纹也必须一起作废：留着它，下面那次 load 会被"同一个注册表、同一批文件"
    # 判成重复调用而**直接跳过**，结果就是 reload 把工具摘掉之后再也装不回来
    # （注册表变空、removed 却列着刚被摘掉的名字，正是最难查的那种"操作成功但
    # 什么都没发生"）。指纹是给 load_connectors 去重的，reload 的语义是强制重扫。
    _STATE.last = None

    loaded = load_connectors(registry)
    removed = sorted(set(previous) - set(loaded))
    failed = sorted({f["file"] for f in _STATE.failures.values()})
    result = {"loaded": loaded, "failed": failed, "removed": removed}
    logger.info("connectors_reloaded", **result)
    return result


def _warn_duplicates() -> None:
    """同名工具只留最后一个 —— 必须吭声。

    重名有两种：两个连接器撞名、连接器盖掉内置工具。两者都会让**先注册的
    那个静默消失**（模型照旧看得到这个名字，跑出来却是另一个工具的活），
    是那种"功能在、但行为不是你以为的那个"的故障。不阻断加载（用户可能
    就是故意想覆盖），但一定要留下痕迹。
    """
    seen: dict[str, list[str]] = {}
    for hits in _STATE.hits.values():
        for h in hits:
            seen.setdefault(h.name, []).append(h.file)
    for name, files in sorted(seen.items()):
        if len(files) > 1:
            winner = files[-1]
            logger.warning("connector_duplicate_tool", tool=name, files=files,
                           kept=winner,
                           hint="同名工具只保留最后扫到的那个，请改名或删掉多余的")
