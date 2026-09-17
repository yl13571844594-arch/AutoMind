"""工作流异常与"错误定位"。

为什么单独一个模块放异常：

工作流是**给客户评审的交付物**，因此它的报错必须回答"改哪一行"，
而不是只回答"哪不对"。执行器（plan_executor.py / tools/base.py）里的报错
面向的是**模型**——要短、要能让模型自己改对；工作流的报错面向的是
**写 YAML 的 FDE 和审 YAML 的客户**——要能直接定位到文件里的位置。

两者的读者不同，信息密度也就不同，所以这里不试图复用 `ToolError` 那套，
而是定义 `WorkflowError` 系列，统一携带 `file:line:column` 与 YAML 字段路径。

设计上的一个硬边界：**加载期的错误是"拒绝加载"，不是"警告后继续"**。
一份校验不过的工作流如果还能跑，那"跑的就是批的那版"就无从谈起。
唯一在加载期降级为 warning 的是"引用了排在自己后面的步骤输出"——
它结构合法、只是当前执行路径上多半取不到值，详见 loader.py 的说明。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any

# ═══════════════════════════════════════════════════════════════
# 位置与错误条目
# ═══════════════════════════════════════════════════════════════

#: difflib 建议条数上限。和 tools/base.py 的 `_SUGGEST_LIMIT` 同一考虑：
#: 给三个是"帮你改对"，给十个是"挑花眼"。
SUGGEST_LIMIT = 3


@dataclass(frozen=True)
class Position:
    """工作流源文件里的一个位置（1 基行号 / 1 基列号）。

    为什么不用 PyYAML 自己的 `Mark`：`Mark` 持有 buffer/pointer 等一堆
    内部状态，既不可 JSON 序列化，也不好跨模块传。校验结果要能直接回给
    前端（`POST /api/workflow/validate`），所以这里只留行、列两个整数。
    """

    line: int = 0
    column: int = 0

    def __str__(self) -> str:
        return f"第 {self.line} 行第 {self.column} 列" if self.line else "（位置未知）"

    def as_dict(self) -> dict[str, int]:
        return {"line": self.line, "column": self.column}


@dataclass
class Issue:
    """一条校验问题 —— 可直接回给前端渲染成"哪一行、哪个字段、怎么改"。"""

    message: str
    #: YAML 里的字段路径，如 `steps[2].tool`（人读比行号更快定位）
    path: str = ""
    position: Position = field(default_factory=Position)
    #: "error" 一律拒绝加载；"warning" 只是提醒，进来即代表结构合法
    level: str = "error"
    #: 可选的"你是不是想写 X"候选
    suggestions: list[str] = field(default_factory=list)
    #: 源文件路径（由 loader 填；`parse_text` 场景为空）
    file: str = ""

    def format(self) -> str:
        """人类可读的一行（中文），CLI 直接打印这个。"""
        head = f"{self.file}:" if self.file else ""
        loc = f"{head}{self.position.line}:{self.position.column}" \
            if self.position.line else (self.file or "")
        where = f"{loc} " if loc else ""
        field_part = f"[{self.path}] " if self.path else ""
        tag = "错误" if self.level == "error" else "警告"
        tip = f"（你是不是想写：{'、'.join(self.suggestions)}？）" if self.suggestions else ""
        return f"{where}{tag}：{field_part}{self.message}{tip}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "message": self.message,
            "path": self.path,
            "file": self.file,
            "line": self.position.line,
            "column": self.position.column,
            "suggestions": list(self.suggestions),
        }


def suggest(value: str, candidates: list[str], limit: int = SUGGEST_LIMIT,
            *, cutoff: float = 0.33) -> list[str]:
    """从候选名里挑出与 `value` 最像的几个 —— "你是不是想写 X？"。

    名字写错（`htp_request`、`writback`、把 `{{ ticket }}` 当变量写）是 YAML 里
    最高频的问题，而这类错误的后果是**整份文件加载失败**。只报"未知字段 result"
    会让人去翻字段表；顺手点出最像的那个，一次就能改对。

    三级匹配，从确定到猜：
      1. **忽略大小写/分隔符后相同** —— 几乎可断定（`ticketId` → `ticket_id`）；
      2. **完全包含或被包含** —— `ticket` vs `ticket_id`、`output` vs `outputs`，
         这类"少写/多写了半截"的意图非常明确；
      3. **纯相似度兜底**（difflib）。

    为什么要有第 2 级：字段名都很短，纯相似度在这类名字上不可靠 ——
    `result` 与 `output` 的相似度只有 0.33，`ticket` 与 `inputs` 只有 0.17，
    阈值调到能命中前者就会让后者也挤进来。用"包含关系"这一确定性信号替代，
    既命中"少写半截"，又不引入噪声。
    """
    cands = [str(c) for c in candidates]
    if not value or not cands:
        return []

    def _norm(text: str) -> str:
        return "".join(ch for ch in str(text).casefold() if ch.isalnum())

    want = _norm(value)
    if not want:
        return []

    exact = [c for c in cands if _norm(c) == want]
    if exact:
        return exact[:limit]

    # 最长匹配优先：`ticket` 应当先命中 `ticket_id` 而不是 `tickets`
    partial = sorted((c for c in cands if want in _norm(c) or _norm(c) in want),
                     key=lambda c: (-len(_norm(c)), c))
    if partial:
        return partial[:limit]

    return difflib.get_close_matches(str(value), cands, n=limit, cutoff=cutoff)


# ═══════════════════════════════════════════════════════════════
# 异常
# ═══════════════════════════════════════════════════════════════


class WorkflowError(Exception):
    """工作流异常基类。"""

    def __init__(self, message: str, *, position: Position | None = None,
                 path: str = "", file: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.position = position or Position()
        self.path = path
        self.file = file

    def format(self) -> str:
        """人类可读的一行（中文）。"""
        head = f"{self.file}:" if self.file else ""
        where = f"{head}{self.position.line}:{self.position.column} " \
            if self.position.line else (f"{self.file} " if self.file else "")
        field_part = f"[{self.path}] " if self.path else ""
        return f"{where}{field_part}{self.message}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": type(self).__name__,
            "message": self.message,
            "path": self.path,
            "file": self.file,
            "line": self.position.line,
            "column": self.position.column,
        }


class WorkflowLoadError(WorkflowError):
    """加载/校验失败。携带**全部**问题（不是第一个），见 loader.collect_issues。"""

    def __init__(self, message: str, *, issues: list[Issue] | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.issues: list[Issue] = list(issues or [])

    def format(self) -> str:
        if not self.issues:
            return super().format()
        # 一次把话说完：改一条跑一次，最耗 FDE 的时间
        lines = [f"工作流校验失败，共 {len(self.issues)} 处问题："]
        lines.extend(f"  {i}. {issue.format()}" for i, issue in enumerate(self.issues, 1))
        return "\n".join(lines)


class UnknownSchemaVersionError(WorkflowLoadError):
    """`version` 字段不认识。

    单独一个类型是因为这条必须**绝不含糊**：不能"猜一个相近版本继续跑"。
    猜错版本的后果不是报错，而是**静默地按错误的语义执行**——
    一份给客户批过的 v1 流程被 v2 的执行器按新语义跑掉，是最坏的失败形态。
    """


class TemplateError(WorkflowError):
    """模板渲染/校验失败。

    `available` 是渲染那一刻真实可用的变量清单。给人看是"你还能写哪些"，
    给模型看是"照抄这个清单"，两边都比一句"undefined"有用得多。
    """

    def __init__(self, message: str, *, available: list[str] | None = None,
                 expression: str = "", suggestions: list[str] | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.available = sorted(available or [])
        self.expression = expression
        self.suggestions = list(suggestions or [])

    def format(self) -> str:
        parts = [super().format()]
        if self.suggestions:
            parts.append(f"（你是不是想写：{'、'.join(self.suggestions)}？）")
        if self.available:
            parts.append(f"当前可用变量：{'、'.join(self.available)}")
        return "".join(parts)

    def as_dict(self) -> dict[str, Any]:
        data = super().as_dict()
        data.update({"available": list(self.available), "expression": self.expression,
                     "suggestions": list(self.suggestions)})
        return data


class WorkflowExecutionError(WorkflowError):
    """执行期的框架级错误（跑之前就发现跑不了：注册表没注入等）。"""


class WorkflowCancelledError(WorkflowExecutionError):
    """保留类型：执行器实际**不抛**它。

    v1 的边界：取消一律透传 `asyncio.CancelledError`（详见 executor.py 的
    `_mark_cancelled`）。父 agent 的"停止"按钮依赖 asyncio 的取消语义，
    若在这里把 `CancelledError` 换成自定义异常，取消就不再"一路向上"，
    上层的 `except CancelledError` 会失效 —— 那才是真正停不住的原因。
    定义这个类型只是为了让调用方能 `except WorkflowCancelledError` 写兼容分支。
    """


__all__ = [
    "SUGGEST_LIMIT",
    "Issue",
    "Position",
    "TemplateError",
    "UnknownSchemaVersionError",
    "WorkflowCancelledError",
    "WorkflowError",
    "WorkflowExecutionError",
    "WorkflowLoadError",
    "suggest",
]
