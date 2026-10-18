"""工具系统基类 — AbstractTool, ToolRegistry, 工具模式生成。"""

from __future__ import annotations

import asyncio
import difflib
import inspect
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from automind.core.logging import get_logger
from automind.core.types import PermissionTier, ToolResult, ToolSource

_logger = get_logger("automind.tools")

#: Python 抛出的调用契约错误，从原始消息里回捞参数名
_UNEXPECTED_KW = re.compile(r"unexpected keyword argument '([^']+)'")
_MISSING_ARG = re.compile(r"missing \d+ required (?:positional|keyword-only) argument")

#: 建议条数上限 —— 给多了模型反而挑花眼，也让观察变长（见 unknown_tool_error）
_SUGGEST_LIMIT = 3


def _norm(name: Any) -> str:
    """标识符归一化：只留小写字母数字。

    ``FileRead`` / ``file-read`` / ``FILE_READ`` → ``fileread``。
    模型写错工具名/参数名时，绝大多数差异就是这个层级的（大小写、分隔符），
    归一化之后可以直接判定"其实是同一个名字"，而不用去猜。
    """
    return re.sub(r"[^a-z0-9]+", "", str(name).casefold())


def _tokens(name: Any) -> frozenset[str]:
    """标识符的"词集合"，用于识别 ``read_file`` / ``file_read`` 这类**词序**差异。

    驼峰先拆词再小写：``FileRead`` → {file, read}，与 ``read_file`` 同集合。
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(name))
    return frozenset(t for t in re.split(r"[^A-Za-z0-9]+", spaced.casefold()) if t)


def suggest_names(name: Any, candidates: Sequence[str],
                  limit: int = _SUGGEST_LIMIT) -> list[str]:
    """从候选名里挑出与 ``name`` 最像的几个 —— "你是不是想用 X？"。

    三级匹配，从"确定"到"猜"：
    1. **归一化后相同**：``FileRead`` → ``file_read``（大小写/分隔符差异，几乎可断定）；
    2. **词集合相同**：``read_file`` → ``file_read``（词序差异，同样可断定）；
    3. **拼写相近**：漏字母、多字母（``pathh`` → ``path``）。

    只在前两级都不中时才用 difflib —— 它会把 ``file_write`` 也列给 ``file_read``，
    对"词序写反"这种确定性错误反而是噪声。
    """
    cands = [str(c) for c in candidates]
    if not cands:
        return []
    want = _norm(name)
    if not want:
        return []

    same = [c for c in cands if _norm(c) == want]
    if same:
        return same[:limit]

    want_tokens = _tokens(name)
    if want_tokens:
        reordered = [c for c in cands if _tokens(c) == want_tokens]
        if reordered:
            return reordered[:limit]

    close = difflib.get_close_matches(str(name), cands, n=limit, cutoff=0.6)
    if len(close) < limit:
        # 再按归一化名比一轮：模型写 "fileRead" 时，原始字符串的相似度会被大小写
        # 与下划线拖低到阈值以下，而归一化名之间的相似度是实打实的。
        by_norm = {_norm(c): c for c in cands}
        for hit in difflib.get_close_matches(want, list(by_norm), n=limit, cutoff=0.7):
            if by_norm[hit] not in close:
                close.append(by_norm[hit])
    return close[:limit]


def resolve_name(name: str, candidates: Sequence[str]) -> tuple[str | None, list[str]]:
    """把模型给的名字解析成候选表里的**真名**，解析不了就给建议。

    返回 ``(真名 | 候选名 | None, 建议列表)``：

    * 名字写法不同但可确定是同一个（大小写/分隔符/词序）→ 直接解析成功，
      调用照常进行。这类差异让一次工具调用白跑没有任何意义 —— 真名是确定的；
    * 只在**唯一命中**时才这样解析，两个工具都可能时宁可报错让模型自己选；
    * 完全不像 → ``None``，附上最接近的几个。
    """
    cands = [str(c) for c in candidates]
    if name in cands:
        return name, []
    want, toks = _norm(name), _tokens(name)
    for matcher in ([c for c in cands if _norm(c) == want],
                    [c for c in cands if toks and _tokens(c) == toks],
                    # v1.11.2：带命名空间的工具（MCP 的 mcp__服务器__工具、插件前缀）
                    # 模型几乎从不照抄全名，而是写 github.create_issue /
                    # github_create_issue / 只写 create_issue —— 去掉命名空间后
                    # 唯一命中就是确定的，不该让这次调用白跑
                    [c for c in cands if want and want in _namespace_forms(c)],
                    [c for c in cands if want and _norm(_short_name(c)) == want]):
        if len(matcher) == 1:
            return matcher[0], []
        if len(matcher) > 1:                      # 有歧义：不替模型做主
            return None, matcher[:_SUGGEST_LIMIT]
    return None, suggest_names(name, cands)


#: 视为"命名空间"的前缀：去掉它剩下的才是工具本名
_NS_PREFIXES = ("mcp", "plugin", "plg", "ext", "skill", "connector")


def _short_name(name: str) -> str:
    """命名空间工具的本名：``mcp__github__create_issue`` → ``create_issue``，
    ``plugin_weather`` → ``weather``；没有命名空间返回空串（不参与这一级匹配，
    否则 ``read`` 也能"唯一命中" ``file_read``，那是在猜）。"""
    if "__" in name:
        return name.rsplit("__", 1)[-1]
    head, _, rest = name.partition("_")
    return rest if head.casefold() in _NS_PREFIXES and rest else ""


def _namespace_forms(name: str) -> set[str]:
    """命名空间工具的等价写法（归一化后）：去掉 ``mcp`` 前缀的 服务器+工具 组合。"""
    if "__" not in name:
        return set()
    parts = [p for p in name.split("__") if p]
    if parts and parts[0].casefold() in _NS_PREFIXES:
        parts = parts[1:]
    return {_norm("".join(parts))} if len(parts) >= 2 else set()


def unknown_tool_error(name: str, candidates: Sequence[str]) -> str:
    """未知工具名 → 模型能据此改正的**短**错误。

    v1.7.2：这里刻意**不再列出全部工具名**。旧消息是
    ``Tool 'x' not found. Available: [...]``，注册表涨到 30+ 个工具后这条
    观察本身就变得很长，而它会经过 ``tools/output_budget.py`` 的「头+尾」夹取
    —— 清单从中段被剪掉，模型拿到的是一份**残缺**的工具表，比不给还糟
    （它会把被剪断的半个名字当成真工具名再试一次）。

    现在只给最像的三个 + 一句"去哪看完整清单"，长度稳定在 200 字符内，
    任何体积配置下都不可能被夹断。
    """
    tips = suggest_names(name, candidates)
    hint = f"你是不是想用：{'、'.join(tips)}？" if tips else ""
    return (f"没有名为 '{name}' 的工具。{hint}"
            f"本实例共 {len(list(candidates))} 个工具，完整清单见系统提示中的工具目录，"
            f"请照抄其中的名字，不要凭记忆拼写。")


def _valid_params(tool: AbstractTool) -> list[str]:
    """工具 schema 里声明的合法参数名。"""
    try:
        props = (tool.parameters or {}).get("properties", {})
    except Exception:                                     # pragma: no cover - 防御性
        return []
    return list(props) if isinstance(props, dict) else []


def _accepts_extra_kwargs(tool: AbstractTool) -> bool:
    """``execute`` 是否吃 ``**kwargs``（吃的话，Python 不会替我们拦住错参数）。"""
    try:
        return any(p.kind is inspect.Parameter.VAR_KEYWORD
                   for p in inspect.signature(tool.execute).parameters.values())
    except (TypeError, ValueError):                        # pragma: no cover - 内建/装饰器
        return True


def typo_params(tool: AbstractTool, kwargs: dict[str, Any]) -> dict[str, list[str]]:
    """挑出"看起来是写错了"的参数：``{模型写的名: [建议]}``。

    判据是**建议非空**（即它很像某个声明的参数名），而不是"不在声明里"。
    ``**kwargs`` 类工具本来就允许模型传 schema 之外的参数（``timeout`` 就是
    这类约定），一律拒绝会误伤；但"很像某个真参数名"基本只有一种解释：
    写错了 —— 而这类工具会把写错的参数**静默忽略**，然后拿着默认值跑出
    一个看起来成功、实则答非所问的结果，比直接报错难查得多。
    """
    if not _accepts_extra_kwargs(tool):
        return {}                      # 显式签名：Python 自己会抛 TypeError，我们只负责翻译
    valid = _valid_params(tool)
    if not valid:
        return {}
    out: dict[str, list[str]] = {}
    for key in kwargs:
        if key in valid:
            continue
        tips = suggest_names(key, valid)
        if tips:
            out[key] = tips
    return out


def _schema_props(tool: AbstractTool) -> dict[str, Any]:
    try:
        props = (tool.parameters or {}).get("properties", {})
    except Exception:                                     # pragma: no cover - 防御性
        return {}
    return props if isinstance(props, dict) else {}


def normalize_param_names(tool: AbstractTool, kwargs: dict[str, Any]
                          ) -> tuple[dict[str, Any], dict[str, str]]:
    """把**可确定**写错的参数名改成真名：``filePath`` / ``file-path`` → ``file_path``。

    与工具名的 :func:`resolve_name` 同一策略：只在"归一化后相同"或"词集合
    相同"且**唯一命中**、目标参数又没被同时传入时才改；拼写相近这类"猜"的情况
    仍交给 :func:`typo_params` 报错，让模型自己确认。返回 ``(新参数, 改名表)``。
    """
    valid = list(_schema_props(tool))
    if not valid or not kwargs:
        return kwargs, {}
    out = dict(kwargs)
    renamed: dict[str, str] = {}
    for key in list(kwargs):
        if key in valid:
            continue
        want, toks = _norm(key), _tokens(key)
        hits = [v for v in valid if _norm(v) == want] or \
               [v for v in valid if toks and _tokens(v) == toks]
        if len(hits) == 1 and hits[0] not in out:
            out[hits[0]] = out.pop(key)
            renamed[key] = hits[0]
    return out, renamed


_TRUE = {"true", "yes", "y", "1", "on", "是"}
_FALSE = {"false", "no", "n", "0", "off", "否", "none", "null", ""}


#: 参数名里"这是文件系统路径"的精确名单（v1.11.13）。
#: 之前只有 file_* 工具自己走 `_RootGuard` 解析相对路径，office/media/archive
#: 等工具直接 `Path(str(kwargs.get("path")))` —— 相对路径按**服务进程 cwd** 落，
#: 于是 Web 切到 A 项目、IM 机器人跑 B 项目时，产物会写进 AutoMind 自己的目录，
#: 用户找不到、隔离副本里也没有，表现为"工具失败/工作区混淆"。这里在分派入口
#: 统一把相对路径锚到**当前任务的工作副本/项目根**，所有工具一致。
#:
#: 只列**语义明确是路径**的名字：``input``/``source``/``file`` 这类在别的工具里
#: 可能是文本或标识符（例如 skill_invoke.input 是技能输入文本），不能一律当路径。
_PATH_KEYS = frozenset({
    "path", "filename", "filepath", "file_path",
    "output", "output_file", "saved_to", "dest", "destination", "dst",
    "sources", "inputs", "input_file",
    "database", "db", "db_path", "workdir", "cwd",
    "csv_path", "text_path", "attachments",
})
#: 这些路径始终以**项目根**为基准（不是会话副本）：git 仓库必须指向真实仓库，
#: 隔离副本里 .git 是跳过复制的。
_PROJECT_ROOT_KEYS = frozenset({"repo", "repository"})


def _path_base() -> Any:
    """相对路径的基准：会话副本优先，其次项目根；都没有则不处理。"""
    try:
        from automind.core import session_ctx
    except Exception:                                    # pragma: no cover - 防御
        return None
    try:
        ws = session_ctx.workspace()
        if ws:
            return Path(ws)
        root = session_ctx.project_root()
        if root:
            return Path(root)
    except Exception:                                    # pragma: no cover - 防御
        return None
    return None


def _project_base() -> Any:
    try:
        from automind.core import session_ctx
        root = session_ctx.project_root()
    except Exception:                                    # pragma: no cover - 防御
        return None
    return Path(root) if root else None


def _looks_like_path(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    low = text.lower()
    return not low.startswith(("http://", "https://", "data:", "file://", "mailto:"))


def _as_absolute(value: Any, base: Any) -> Any:
    """把一个路径参数变成绝对路径；非字符串/URL/空值原样返回。"""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()          # 模型常把整条路径再包一层引号
    if not _looks_like_path(text) or base is None:
        return value
    try:
        p = Path(text).expanduser()
        if p.is_absolute():
            return str(p)
        return str((Path(base) / p).resolve())
    except (OSError, ValueError):
        return value


def absolutize_paths(tool: AbstractTool, kwargs: dict[str, Any]
                     ) -> tuple[dict[str, Any], list[str]]:
    """把路径类参数锚到当前任务的会话副本/项目根（v1.11.13）。

    只动**名字明确是路径**的参数（``path`` / ``output`` / ``sources`` …），
    不动 ``query`` / ``command`` / ``content``；URL 与绝对路径原样保留。
    没有绑定会话上下文（库调用、单测）时不做任何事，保持既有行为。
    """
    if not kwargs:
        return kwargs, []
    base = _path_base()
    project_base = _project_base()
    if base is None and project_base is None:
        return kwargs, []
    props = _schema_props(tool)
    out = dict(kwargs)
    changed: list[str] = []
    for key, value in kwargs.items():
        if props and key not in props:
            # 只处理本工具 schema 里声明过的参数：避免把 skill_invoke.input
            # 这类同名但语义完全不同的字段误当路径改写。
            continue
        if key in _PROJECT_ROOT_KEYS:
            if project_base is not None and isinstance(value, str):
                new = _as_absolute(value, project_base)
                if new != value:
                    out[key] = new
                    changed.append(key)
            continue
        if key not in _PATH_KEYS:
            continue
        target_base = base or project_base
        if isinstance(value, (list, tuple)):
            mapped = [_as_absolute(v, target_base) for v in value]
            if list(mapped) != list(value):
                out[key] = mapped
                changed.append(key)
        elif isinstance(value, dict):
            continue
        else:
            new = _as_absolute(value, target_base)
            if new != value:
                out[key] = new
                changed.append(key)
    files = kwargs.get("files")
    if isinstance(files, list) and (not props or "files" in props):
        mapped_files = []
        touched = False
        for item in files:
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                value = item["path"]
                new = _as_absolute(value, base or project_base)
                if new != value:
                    mapped_files.append({**item, "path": new})
                    touched = True
                    continue
            mapped_files.append(item)
        if touched:
            out["files"] = mapped_files
            changed.append("files")
    return out, changed



#: 常见"同义参数名"（v1.11.13）。模型经常把 path 写成 file_path、command
#: 写成 cmd、max_results 写成 limit —— 这些不是拼写错误而是另一套命名习惯。
#: 在确认目标参数存在、且未被同时传入时直接改名，省掉一次白跑的重试。
_PARAM_ALIASES: dict[str, tuple[str, ...]] = {
    "path": ("file_path", "filepath", "filename", "file", "target", "local_path"),
    "command": ("cmd", "shell_command", "command_line"),
    "content": ("text", "contents", "data", "body"),
    "workdir": ("cwd", "working_directory", "working_dir"),
    "query": ("q", "keyword", "search_query"),
    "url": ("uri", "link", "target_url"),
    "output": ("output_path", "out", "save_path", "dst"),
    "files": ("file_list",),
    "old_string": ("old", "old_text", "find", "search_text"),
    "new_string": ("new", "new_text", "replace_with", "replacement"),
    "timeout": ("timeout_s", "timeout_sec", "timeout_seconds"),
    "max_results": ("limit", "num_results"),
    "database": ("db", "db_path"),
    "sql": ("statement", "query_sql"),
}


def apply_param_aliases(tool: AbstractTool, kwargs: dict[str, Any]
                        ) -> tuple[dict[str, Any], dict[str, str]]:
    """把同义参数名改成 schema 里的真名（见 ``_PARAM_ALIASES``）。"""
    props = _schema_props(tool)
    if not props or not kwargs:
        return kwargs, {}
    valid = set(props)
    out = dict(kwargs)
    renamed: dict[str, str] = {}
    for target, aliases in _PARAM_ALIASES.items():
        if target not in valid or target in out:
            continue
        for alias in aliases:
            if alias in valid or alias not in out:
                continue              # alias 本身是合法参数（如 file_read.limit）就别动
            out[target] = out.pop(alias)
            renamed[alias] = target
            break
    return out, renamed


def _coerce_container_string(text: str) -> Any:
    """把被"再序列化一层"的数组/对象字符串解析出来。

    兼容：JSON、Python 字面量（模型有时用单引号）、外层 Markdown 代码围栏。
    解析失败返回原字符串，由调用方保留原值。
    """
    import ast
    import json as _json

    body = text.strip()
    if body.startswith("```"):
        body = re.sub(r"^```[A-Za-z0-9_+-]*\s*", "", body)
        body = re.sub(r"\s*```$", "", body).strip()
    if body[:1] not in "[{":
        return text
    for parser in (_json.loads, ast.literal_eval):
        try:
            parsed = parser(body)
        except Exception:
            continue
        if isinstance(parsed, (list, dict)):
            return parsed
    return text


def coerce_param_types(tool: AbstractTool, kwargs: dict[str, Any]
                       ) -> tuple[dict[str, Any], list[str]]:
    """按 JSON Schema 声明把**明显**类型不符的参数转过来（转不了就原样保留）。

    模型最常见的类型失误：数字写成字符串（``"timeout": "600"``）、布尔写成
    字符串（``"recursive": "true"``）、数组/对象被再序列化了一层
    （``"files": "[\"a.py\"]"`` / Python 单引号列表）、只有一个元素时忘了
    包成数组。这些都会让工具在内部 ``int()`` / 比较 / 迭代时出错或静默走错
    分支 —— 而意图是明确的。
    """
    props = _schema_props(tool)
    if not props:
        return kwargs, []
    out = dict(kwargs)
    fixed: list[str] = []
    for key, val in kwargs.items():
        spec = props.get(key)
        if not isinstance(spec, dict):
            continue
        typ = spec.get("type")
        if isinstance(typ, list):           # ["string", "null"] 之类：不替它做选择
            continue
        new = val
        try:
            if typ == "integer" and isinstance(val, str) and re.fullmatch(r"\s*-?\d+\s*", val) or typ == "integer" and isinstance(val, float) and val.is_integer():
                new = int(val)
            elif typ == "number" and isinstance(val, str) and re.fullmatch(
                    r"\s*-?\d+(\.\d+)?\s*", val):
                new = float(val) if "." in val else int(val)
            elif typ == "boolean" and isinstance(val, str) and val.strip().lower() in _TRUE | _FALSE:
                new = val.strip().lower() in _TRUE
            elif typ == "boolean" and isinstance(val, int) and not isinstance(val, bool) and val in (0, 1):
                new = bool(val)
            elif typ in ("array", "object") and isinstance(val, str) and (
                    val.strip()[:1] in "[{" or "```" in val):
                parsed = _coerce_container_string(val)
                if (typ == "array" and isinstance(parsed, list)) or \
                        (typ == "object" and isinstance(parsed, dict)):
                    new = parsed
            elif typ == "array" and isinstance(val, (str, int, float)) and not isinstance(val, bool):
                new = [val]
        except Exception:
            new = val
        if new is not val:
            out[key] = new
            fixed.append(key)
    return out, fixed


def missing_required(tool: AbstractTool, kwargs: dict[str, Any]) -> list[str]:
    """schema 声明为必需、但这次没传（或传了 None）的参数。"""
    try:
        req = (tool.parameters or {}).get("required", []) or []
    except Exception:                                     # pragma: no cover
        return []
    return [r for r in req if isinstance(r, str) and kwargs.get(r) is None]


def describe_tool_error(tool: AbstractTool, exc: BaseException) -> str:
    """把工具异常转成**模型能据此改正**的错误文本。

    v1.7.0：``dispatch`` 此前只做 ``str(exc)``，于是模型把参数名写成
    ``pathh`` 时收到的观察是::

        Error: 'path'

    —— 既看不出是参数写错了，也不知道正确参数名是什么，只能盲猜重试。
    而日志里明明记着 ``KeyError: 'path'``：信息存在过，只是没送到模型手里。

    参数名/类型写错是 LLM 工具调用最高频的失败模式，所以这里把
    **异常类型**、**出错的参数名**与**本工具的合法参数名**一并给出，
    让模型一次调用就能改对，而不是靠重试撞对。
    """
    valid = _valid_params(tool)
    hint = f"本工具支持参数：{', '.join(valid)}" if valid else ""

    if isinstance(exc, KeyError) and exc.args and isinstance(exc.args[0], str):
        # 工具内部用 ``kwargs["x"]`` 取值 → 参数缺失。仅当该名字确实属于本
        # 工具时才这样定性，否则可能是工具自身的 bug，不能误导模型去"补参数"。
        key = exc.args[0]
        if key in valid:
            return (f"调用 {tool.name} 缺少必需参数 '{key}'（KeyError）。"
                    f"{hint}。请补上该参数后重试。")

    if isinstance(exc, TypeError):
        m = _UNEXPECTED_KW.search(str(exc))
        if m:
            bad = m.group(1)
            # v1.7.2：光给合法参数名清单还不够 —— 模型得自己比对 "pathh" 和
            # "path" 差在哪。直接把最像的那个点出来，一次就能改对。
            tips = suggest_names(bad, valid)
            guess = f"你是不是想传：{'、'.join(tips)}？" if tips else ""
            return (f"调用 {tool.name} 时传了它不认识的参数 '{bad}'（TypeError）。"
                    f"{guess}{hint}。请改用上面的参数名重试。")
        if _MISSING_ARG.search(str(exc)):
            return f"调用 {tool.name} 缺少必需参数（TypeError）。{hint}。"

    detail = f"{type(exc).__name__}: {exc}"
    return f"{detail}。{hint}" if hint else detail


class AbstractTool(ABC):
    """所有工具的抽象基类。

    子类需要实现:
        - execute(**kwargs) → ToolResult

    可选覆盖:
        - dry_run_possible() → bool
        - get_execution_plan(**kwargs) → str
    """

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}
    permission_tier: PermissionTier = PermissionTier.SAFE
    risk_score: int = 0  # 0-100
    source: ToolSource = ToolSource.BUILTIN

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult: ...

    def dry_run_possible(self) -> bool:
        """是否支持干运行 (预览而不执行)。"""
        return False

    def get_execution_plan(self, **kwargs: Any) -> str:
        """生成人类可读的执行计划预览。"""
        params_str = ", ".join(f"{k}={v}" for k, v in kwargs.items())
        return f"[{self.name}] {self.description}\n  Parameters: {params_str}"

    def to_openai_schema(self) -> dict[str, Any]:
        """生成 OpenAI Function Calling 格式的 schema。"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": self.parameters.get("properties", {}),
                "required": self.parameters.get("required", []),
            },
        }

    def to_anthropic_schema(self) -> dict[str, Any]:
        """生成 Anthropic tool use 格式的 schema。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": self.parameters.get("properties", {}),
                "required": self.parameters.get("required", []),
            },
        }


class ToolRegistry:
    """工具注册中心 — 管理所有可用的工具。

    使用示例::

        registry = ToolRegistry()
        registry.register(MyTool())
        result = await registry.dispatch("my_tool", arg1="val1")
    """

    def __init__(self) -> None:
        self._tools: dict[str, AbstractTool] = {}
        #: 名字不认识时的改道钩子（v1.11.2）：``fn(name, kwargs) -> (新名字, 新参数) | None``。
        #: 典型用法：模型把技能名当工具名直接调用 → 改道到 skill_invoke。
        self._fallbacks: list[Any] = []
        #: 插件提供的分派中间件（v1.11.6）：``async mw(tool_name, kwargs, call_next) -> ToolResult``，
        #: 按登记顺序由外向内包住真正的 ``tool.execute``（参数校验之后、计时之内）。
        self._middlewares: list[Any] = []
        #: 由插件注册的工具名（插件卸载时据此注销，不误删内置工具）
        self._plugin_tools: set[str] = set()

    def set_middlewares(self, middlewares: list[Any]) -> None:
        """整体替换插件中间件（插件启停后由 Agent 重新同步）。"""
        self._middlewares = [m for m in middlewares if callable(m)]

    def sync_plugin_tools(self, tools: list[AbstractTool]) -> None:
        """把插件提供的工具同步进注册表：新增的注册、已卸载插件的注销。

        与内置工具同名的插件工具**不覆盖**内置实现 —— 插件只能加能力，不能顶替。
        """
        want = {t.name: t for t in tools if getattr(t, "name", "")}
        for name in list(self._plugin_tools):
            if name not in want:
                self._tools.pop(name, None)
                self._plugin_tools.discard(name)
        for name, tool in want.items():
            if name in self._tools and name not in self._plugin_tools:
                continue
            self._tools[name] = tool
            self._plugin_tools.add(name)

    def add_fallback(self, fn: Any) -> None:
        """登记一个未知工具名的改道钩子（重复登记同一个函数不会叠加）。"""
        if fn not in self._fallbacks:
            self._fallbacks.append(fn)

    def register(self, tool: AbstractTool) -> None:
        """注册工具。"""
        if not tool.name:
            raise ValueError("Tool must have a name")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """注销工具。"""
        self._tools.pop(name, None)

    def get(self, name: str) -> AbstractTool:
        """获取工具。"""
        if name not in self._tools:
            from automind.core.exceptions import ToolNotFoundError
            raise ToolNotFoundError(f"Tool '{name}' not found. Available: {self.list_names()}")
        return self._tools[name]

    def list_names(self) -> list[str]:
        """返回所有工具名称。"""
        return sorted(self._tools.keys())

    def list_all(self) -> list[AbstractTool]:
        """返回所有工具实例。"""
        return list(self._tools.values())

    def list_by_tier(self, tier: PermissionTier) -> list[AbstractTool]:
        """按权限等级筛选工具。"""
        return [t for t in self._tools.values() if t.permission_tier == tier]

    def get_openai_schemas(self) -> list[dict[str, Any]]:
        """生成 OpenAI 格式的所有工具 schema。"""
        return [t.to_openai_schema() for t in self._tools.values()]

    def get_anthropic_schemas(self) -> list[dict[str, Any]]:
        """生成 Anthropic 格式的所有工具 schema。"""
        return [t.to_anthropic_schema() for t in self._tools.values()]

    async def dispatch(self, tool_name: str, **kwargs: Any) -> ToolResult:
        """分派工具调用 —— **无论发生什么，返回的都是 ToolResult**。

        v1.7.2：契约统一。此前 ``self.get(tool_name)`` 写在 ``try`` **之外**，
        未知工具名抛 ``ToolNotFoundError`` 逸出，于是同一个入口有两种形态：
        "返回失败结果"（工具自己炸了）与"抛异常"（名字不存在），
        调用方必须两边都处理。实际结果是只有 ``react_executor`` 补了那层
        ``except``，其余直接调 ``dispatch`` 的地方（技能、计划执行器、插件、
        CLI）全都没有 —— 一次工具名拼错就能让整条链路崩掉，而不是让模型
        看到错误、换个名字重试。

        现在：名字不认识 → 失败的 ToolResult（附建议）；名字只是写法不同
        （``FileRead`` / ``read_file``）→ 直接解析到真名照常执行；参数写错 →
        失败的 ToolResult（附建议）。**唯一还会抛的**是 ``BaseException``
        （``CancelledError`` 等）—— 取消必须照常传播，不能被当成工具失败吞掉。

        Args:
            tool_name: 工具名称（大小写/分隔符/词序差异会被容错解析）。
            **kwargs: 工具参数。

        Returns:
            ToolResult 实例。
        """
        real, tips = resolve_name(tool_name, self.list_names())
        if real is None:
            for fb in self._fallbacks:
                try:
                    hit = fb(tool_name, dict(kwargs))
                except Exception as e:                    # 钩子出错不能拖垮分派
                    _logger.warning("tool_fallback_failed", tool=tool_name, error=str(e))
                    hit = None
                if hit and hit[0] in self._tools and hit[0] != tool_name:
                    _logger.info("tool_name_rerouted", requested=tool_name, to=hit[0])
                    return await self.dispatch(hit[0], **hit[1])
        if real is None:
            _logger.warning("tool_not_found", tool=tool_name, suggestions=tips)
            return ToolResult(
                tool_name=tool_name, success=False,
                error=unknown_tool_error(tool_name, self.list_names()),
                metadata={"tool_not_found": True, "suggestions": tips},
            )
        if real != tool_name:
            # 记一笔：这是模型的高频小毛病，观测中心里能看出它总在写错哪个名字
            _logger.info("tool_name_resolved", requested=tool_name, resolved=real)

        tool = self._tools[real]
        # v1.7.6：执行前的三道"可确定"修正 —— 能确定意图的就直接修好照常执行，
        # 确定不了的给出能一次改对的报错。目的只有一个：少让一次工具调用白跑。
        kwargs, renamed = normalize_param_names(tool, kwargs)
        if renamed:
            _logger.info("tool_params_renamed", tool=real, renamed=renamed)
        kwargs, aliased = apply_param_aliases(tool, kwargs)
        if aliased:
            _logger.info("tool_params_aliased", tool=real, renamed=aliased)
        kwargs, coerced = coerce_param_types(tool, kwargs)
        if coerced:
            _logger.info("tool_params_coerced", tool=real, params=coerced)
        # v1.11.13：路径参数统一锚到当前会话副本/项目根 —— 修"office/media 等
        # 工具按服务进程 cwd 解析相对路径，产物落到 AutoMind 自己目录"的混淆。
        kwargs, absolutized = absolutize_paths(tool, kwargs)
        if absolutized:
            _logger.info("tool_paths_absolutized", tool=real, params=absolutized)
        typos = typo_params(tool, kwargs)
        if typos:
            # 在**执行之前**拦下来：写错的参数会被 **kwargs 类工具静默忽略，
            # 然后拿着默认值跑出一个"成功但答非所问"的结果，事后极难归因。
            detail = "；".join(f"'{k}' 你是不是想传：{'、'.join(v)}？"
                              for k, v in typos.items())
            _logger.warning("tool_params_typo", tool=real, typos=list(typos))
            return ToolResult(
                tool_name=real, success=False,
                error=(f"调用 {real} 的参数名写错了：{detail}"
                       f"本工具支持参数：{', '.join(_valid_params(tool))}。"
                       f"请改用上面的参数名重试。"),
                metadata={"param_typos": typos},
            )

        missing = missing_required(tool, kwargs)
        if missing:
            # 缺的参数多半就是模型写错名字的那个（``pathh`` 之于 ``path``）——
            # 把"写错的名字 → 该用的名字"一并点出来，比单说"缺 path"更容易一次改对
            valid = _valid_params(tool)
            wrong = {k: suggest_names(k, valid) for k in kwargs if k not in valid}
            wrong = {k: v for k, v in wrong.items() if v}
            typo_hint = "".join(f"你传的 '{k}' 是不是想传：{'、'.join(v)}？"
                                for k, v in wrong.items())
            _logger.warning("tool_params_missing", tool=real, missing=missing,
                            unknown=list(wrong))
            return ToolResult(
                tool_name=real, success=False,
                error=(f"调用 {real} 缺少必需参数：{', '.join(missing)}。{typo_hint}"
                       f"本工具支持参数：{', '.join(valid)}。"
                       f"请补上这些参数后重试（本次未执行）。"),
                metadata={"missing_params": missing, "param_typos": wrong},
            )

        start = time.perf_counter()
        try:
            result = await self._execute_with_middlewares(real, tool, kwargs)
        except Exception as e:
            duration = (time.perf_counter() - start) * 1000
            _logger.error("tool_dispatch_error", tool=real,
                          error=f"{type(e).__name__}: {e}", duration_ms=round(duration, 1))
            return ToolResult(
                tool_name=real,
                success=False,
                error=describe_tool_error(tool, e),
                duration_ms=duration,
            )
        result.duration_ms = (time.perf_counter() - start) * 1000
        result.tool_name = real
        _logger.info("tool_dispatch", tool=real, success=result.success,
                     duration_ms=round(result.duration_ms, 1))
        return result

    async def _execute_with_middlewares(self, name: str, tool: AbstractTool,
                                        kwargs: dict[str, Any]) -> ToolResult:
        # v1.11.9：修复"重放风暴"。旧实现的 except 分支无条件 `call(i + 1, kw)`：
        #   · 工具自己抛异常时，异常会从最内层穿出，被**每一个**外层中间件帧的
        #     except 接住再往下重放 —— 4 个中间件插件时一个抛异常的工具会执行 3 次；
        #   · 中间件在工具**已经执行完之后**做后处理时抛异常（重试换结果、记账、
        #     回读校验都可能），旧代码同样把整条链重跑一遍 —— 写盘类操作被二次执行。
        # 现在用 tool_ran 标记：工具真正跑过之后，任何一层中间件再抛异常都**不再
        # 重放**，直接合成一条可诊断的失败结果（工具可能有副作用，重放不是无害的）。
        state = {"tool_ran": False}

        async def call(i: int, kw: dict[str, Any]) -> ToolResult:
            if i >= len(self._middlewares):
                state["tool_ran"] = True
                return await tool.execute(**kw)
            mw = self._middlewares[i]
            try:
                return await mw(name, kw, lambda nkw: call(i + 1, nkw))
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                raise
            except Exception as e:
                if state["tool_ran"]:
                    # 工具已执行（或已开始执行）：不能重放，合成失败结果交回模型。
                    # 异常也可能来自工具本身（穿过了不接异常的中间件），
                    # 所以仍走 describe_tool_error —— 参数名提示等信息不能丢。
                    _logger.error("tool_middleware_failed_after_execution", tool=name,
                                  middleware=i, error=f"{type(e).__name__}: {e}")
                    return ToolResult(
                        tool_name=name, success=False,
                        error=(f"{describe_tool_error(tool, e)}\n"
                               f"（该异常发生在工具已执行之后，本次产出状态未知："
                               f"请先用只读工具核对实际结果，确认无误前不要重做有副作用的操作。）"),
                        metadata={"middleware_failure": True, "tool_executed": True},
                    )
                # 工具还没跑过：跳过出错的中间件，照常执行
                _logger.warning("tool_middleware_failed", tool=name, error=f"{type(e).__name__}: {e}")
                return await call(i + 1, kw)
        return await call(0, dict(kwargs))

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __repr__(self) -> str:
        return f"ToolRegistry({len(self._tools)} tools: {', '.join(self.list_names())})"
