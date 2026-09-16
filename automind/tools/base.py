"""工具系统基类 — AbstractTool, ToolRegistry, 工具模式生成。"""

from __future__ import annotations

import difflib
import inspect
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
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
                    [c for c in cands if toks and _tokens(c) == toks]):
        if len(matcher) == 1:
            return matcher[0], []
        if len(matcher) > 1:                      # 有歧义：不替模型做主
            return None, matcher[:_SUGGEST_LIMIT]
    return None, suggest_names(name, cands)


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

        start = time.perf_counter()
        try:
            result = await tool.execute(**kwargs)
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

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __repr__(self) -> str:
        return f"ToolRegistry({len(self._tools)} tools: {', '.join(self.list_names())})"
