"""工作流的数据结构 + 逐步校验。

这一层回答的问题是：**"这份 YAML 说清了自己要干什么吗？"**

它刻意不做执行相关的事（不碰工具、不碰 LLM、不碰网络），因此可以在
CI 里对仓库里所有 `*.yaml` 工作流跑一遍校验 —— 这就是"可评审"的技术含义：
评审的不是一段散文式提示词，而是一份**能被机器拒绝**的结构化文件。

校验的两条原则
--------------
1. **一次报全部问题**（见 `collect_issues`）。写 YAML 的人改一条跑一次，
   最耗时间；能一次列全就一次列全。
2. **未知字段不静默忽略**。默认按错误处理：`on_failur: continue` 这种
   一个字母的笔误，如果被静默忽略，就会退回默认的 `abort` —— 客户拿到的
   行为和他读到的文件不一致，而这是"工作流即代码"最不能出的错。
   需要放行时用 `WorkflowLoader(unknown_fields="warning")` 显式打开，
   详见 docs/WORKFLOWS.md。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from automind.workflow.exceptions import Issue, Position, suggest

# ═══════════════════════════════════════════════════════════════
# 常量表
# ═══════════════════════════════════════════════════════════════

#: 本实现唯一认识的 schema 版本。**不认识就报错，绝不猜**。
#: 见 exceptions.UnknownSchemaVersionError 里写的理由。
SUPPORTED_VERSIONS: tuple[int, ...] = (1,)

#: 步骤类型 → 该类型必需字段 + 该类型专属的合法字段。
STEP_TYPES: dict[str, tuple[tuple[str, ...], frozenset[str]]] = {
    "tool": (("tool",), frozenset({"tool", "args"})),
    "llm": (("prompt",), frozenset({"prompt", "system"})),
    "human": (("prompt",), frozenset({"prompt"})),
    "branch": (("condition", "then", "else"), frozenset({"condition", "then", "else"})),
}

#: 所有步骤共有的字段（类型无关）。
COMMON_STEP_FIELDS: frozenset[str] = frozenset({"id", "type", "timeout", "on_failure"})

#: 顶层合法字段。
TOP_LEVEL_FIELDS: frozenset[str] = frozenset(
    {"version", "name", "description", "inputs", "steps", "metadata"})

#: 入参声明的合法字段。
INPUT_FIELDS: frozenset[str] = frozenset({"type", "required", "default", "description"})

#: 入参支持的类型名（只做"声明式"校验：类型不符时由 CLI/服务端做转换，
#: 转换不了就明确报错，不悄悄按字符串塞进去）。
INPUT_TYPES: tuple[str, ...] = ("string", "integer", "number", "boolean", "object", "array")

#: branch 步骤支持的受限比较算子。**这是刻意的白名单**：
#: 模板/条件一旦支持表达式求值，YAML 就变成了代码执行通道，
#: "客户能评审的流程"立刻退化成"客户看不懂的脚本"。详见 template.py。
CONDITION_OPS: tuple[str, ...] = ("==", "!=", "contains")

#: on_failure 的取值说明（写进错误提示，让人不用翻文档）。
ON_FAILURE_HINT = ("可选值：abort（中止整个工作流，默认）、continue（记录失败后继续）、"
                   "retry(n)（重试 n 次后仍失败则视为失败）")


# ═══════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class FailurePolicy:
    """步骤失败后的处置。

    `abort` / `continue` / `retry` 三态。注意 `retry` 的语义是
    **"重试 n 次；仍失败则步骤判失败"**，而不是"重试到成功为止"——
    无限重试会让一次 CI 跑到天亮，也让报告失去意义。
    """

    kind: str = "abort"          # abort | continue | retry
    retries: int = 0             # 仅 retry 有意义：额外重试次数
    raw: str = "abort"           # 原始写法，报告里如实回显

    @property
    def max_attempts(self) -> int:
        return self.retries + 1 if self.kind == "retry" else 1

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "retries": self.retries, "raw": self.raw}


@dataclass
class StepSpec:
    """一个步骤的声明。"""

    id: str
    type: str
    position: Position = field(default_factory=Position)
    #: tool 步骤
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    #: llm / human 步骤
    prompt: str = ""
    system: str = ""
    #: branch 步骤：受限比较 + 跳转目标
    condition: str = ""
    then: str = ""
    else_: str = ""
    #: 通用
    timeout: float = 0.0                 # 0 = 不限
    on_failure: FailurePolicy = field(default_factory=FailurePolicy)

    def as_dict(self) -> dict[str, Any]:
        """序列化 —— 前端要拿它渲染"这份流程长什么样"。"""
        data: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "line": self.position.line,
            "on_failure": self.on_failure.as_dict(),
        }
        if self.timeout:
            data["timeout"] = self.timeout
        if self.type == "tool":
            data.update({"tool": self.tool, "args": self.args})
        elif self.type == "llm":
            data["prompt"] = self.prompt
            if self.system:
                data["system"] = self.system
        elif self.type == "human":
            data["prompt"] = self.prompt
        elif self.type == "branch":
            data.update({"condition": self.condition, "then": self.then, "else": self.else_})
        return data


@dataclass
class InputSpec:
    """一个声明式入参。"""

    name: str
    type: str = "string"
    required: bool = False
    default: Any = None
    description: str = ""
    position: Position = field(default_factory=Position)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "required": self.required,
                "default": self.default, "description": self.description,
                "line": self.position.line}


@dataclass
class WorkflowSchema:
    """一份校验通过的工作流。

    `warnings` 与 `issues(level="error")` 的区别：warnings 里的条目**不阻止加载**。
    目前只有一类：引用了排在自己后面的步骤输出（结构合法，但当前执行路径上
    多半还没跑）。它必须在加载期被看见 —— 静默忽略正是要避免的事。
    """

    version: int
    name: str
    description: str = ""
    inputs: dict[str, InputSpec] = field(default_factory=dict)
    steps: list[StepSpec] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    #: 源文件路径（`parse_text` 场景为空字符串）
    source: str = ""
    #: 加载期的非致命提醒；CLI/前端都要显示出来
    warnings: list[Issue] = field(default_factory=list)
    #: 源文件字节级摘要（sha256）。前端可以用它证明"跑的就是批的那版"
    source_digest: str = ""

    # ── 查询辅助 ──────────────────────────────────────

    @property
    def step_ids(self) -> list[str]:
        return [s.id for s in self.steps]

    def get_step(self, step_id: str) -> StepSpec | None:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None

    def index_of(self, step_id: str) -> int:
        for i, s in enumerate(self.steps):
            if s.id == step_id:
                return i
        return -1

    def required_inputs(self) -> list[str]:
        """必填且无默认值的入参名（CLI 据此校验调用方给全了没）。"""
        return [n for n, spec in self.inputs.items()
                if spec.required and spec.default is None]

    def as_dict(self) -> dict[str, Any]:
        """序列化 —— 回给前端做"流程预览/评审"。"""
        return {
            "version": self.version,
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "source_digest": self.source_digest,
            "inputs": {n: s.as_dict() for n, s in self.inputs.items()},
            "steps": [s.as_dict() for s in self.steps],
            "metadata": self.metadata,
            "warnings": [w.as_dict() for w in self.warnings],
        }

    def summarize(self) -> str:
        """人类可读的流程摘要（dry-run 的第一段输出）。"""
        lines = [f"工作流：{self.name}（schema v{self.version}）"]
        if self.description:
            lines.append(f"说明：{self.description}")
        if self.source:
            digest = self.source_digest[:12] if self.source_digest else "未知"
            lines.append(f"来源：{self.source}  摘要：{digest}")
        if self.inputs:
            lines.append("入参：")
            for spec in self.inputs.values():
                flag = "必填" if spec.required else "可选"
                default = "" if spec.default is None else f"，默认 {spec.default!r}"
                lines.append(f"  - {spec.name}（{spec.type}，{flag}{default}）")
        lines.append(f"步骤：共 {len(self.steps)} 步")
        for i, step in enumerate(self.steps, 1):
            lines.append(f"  {i}. {_describe_step(step)}")
        return "\n".join(lines)


def _describe_step(step: StepSpec) -> str:
    """一行话说清一个步骤"将执行什么" —— dry-run 与 `--list` 都用它。"""
    on_failure = step.on_failure
    policy = on_failure.raw if on_failure.kind != "retry" else f"retry({on_failure.retries})"
    tail = f"（失败策略 {policy}）" if policy != "abort" else ""
    if step.timeout:
        tail += f"（超时 {step.timeout:g}s）"
    if step.type == "tool":
        arg_keys = "、".join(step.args) if step.args else "无参数"
        return f"[{step.id}] type=tool  tool={step.tool}  参数={arg_keys}{tail}"
    if step.type == "llm":
        return f"[{step.id}] type=llm  提示词={_clip(step.prompt)}{tail}"
    if step.type == "human":
        return f"[{step.id}] type=human  待批内容={_clip(step.prompt)}{tail}"
    if step.type == "branch":
        return (f"[{step.id}] type=branch  当 {_clip(step.condition)} 成立 → {step.then}，"
                f"否则 → {step.else_}{tail}")
    return f"[{step.id}] type={step.type}{tail}"


def _clip(text: str, limit: int = 60) -> str:
    one_line = " ".join(str(text).split())
    return one_line if len(one_line) <= limit else one_line[:limit] + "…"


# ═══════════════════════════════════════════════════════════════
# 校验
# ═══════════════════════════════════════════════════════════════


class _Ctx:
    """校验上下文：收集问题 + 查位置。

    `positions` 由 loader 在解析 YAML 时填好（对象 id → 行/列）。
    为什么按 id 查而不是让每个值自带行号：YAML 的标量在 Python 里就是
    `str`/`int`，没法挂属性；而整份文档在 schema 构建完成前一直存活，
    id 不会失效（loader 用 `id()` 而不是 `==`，避免相同内容的值互相串位）。
    """

    def __init__(self, positions: dict[int, Position] | None = None) -> None:
        self.positions = positions if positions is not None else {}
        self.issues: list[Issue] = []
        self.warnings: list[Issue] = []

    def pos(self, obj: Any, fallback: Position | None = None) -> Position:
        if obj is not None:
            hit = self.positions.get(id(obj))
            if hit is not None:
                return hit
        return fallback or Position()

    def error(self, message: str, *, path: str = "", at: Any = None,
              position: Position | None = None,
              suggestions: list[str] | None = None) -> None:
        self.issues.append(Issue(message=message, path=path, position=position or self.pos(at),
                                 level="error", suggestions=suggestions or []))

    def warn(self, message: str, *, path: str = "", at: Any = None,
             position: Position | None = None,
             suggestions: list[str] | None = None) -> None:
        self.warnings.append(Issue(message=message, path=path, position=position or self.pos(at),
                                   level="warning", suggestions=suggestions or []))

    @property
    def failed(self) -> bool:
        return bool(self.issues)


#: 未知字段的处理策略（模块级常量，供 loader 与文档引用）
UNKNOWN_FIELD_ERROR = "error"
UNKNOWN_FIELD_WARNING = "warning"



def _check_unknown_fields(ctx: _Ctx, mapping: dict[str, Any], allowed: frozenset[str],
                          *, where: str, unknown_fields: str) -> None:
    """未知字段：默认报错，可选择降级为 warning。

    为什么默认报错而不是"忽略"：这一条直接关系到"跑的就是批的那版"。
    `timout: 30`（少个 e）被静默忽略后，步骤照跑、但没有超时保护；
    报告里也一样是"成功"—— 客户对照文件逐条核对时看不出任何差别。
    """
    for key in mapping:
        if key in allowed:
            continue
        tips = suggest(str(key), sorted(allowed))
        message = (f"未知字段 '{key}'。本处允许的字段：{'、'.join(sorted(allowed))}。"
                   "（未知字段不会被静默忽略：写错一个字段名会让行为与文件不一致，"
                   "因此默认按错误处理；确需放行请用 unknown_fields=\"warning\"）")
        if unknown_fields == UNKNOWN_FIELD_WARNING:
            ctx.warn(message, path=f"{where}.{key}", at=mapping[key], suggestions=tips)
        else:
            ctx.error(message, path=f"{where}.{key}", at=mapping[key], suggestions=tips)


def _as_bool(ctx: _Ctx, value: Any, *, path: str) -> bool:
    """把 required 之类的开关解析成布尔；写法不合法就报错（不猜）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {
            "true", "false", "yes", "no", "on", "off", "1", "0"}:
        # YAML 1.1 里 `on`/`yes` 会被 PyYAML 解析成布尔，但 `"on"` 是字符串；
        # 两种来源都接受，避免"同样的意图两种写法一个过一个不过"。
        return value.strip().lower() in {"true", "yes", "on", "1"}
    ctx.error(f"应当是布尔值（true/false），实际是 {type(value).__name__}：{value!r}",
              path=path, at=value)
    return False


def _check_timeout(ctx: _Ctx, raw: Any, *, path: str) -> float:
    """timeout：正数秒；0/缺省 = 不限。"""
    if isinstance(raw, bool):                      # bool 是 int 的子类，先拦掉
        ctx.error("timeout 应当是秒数（数字），不是布尔值", path=path, at=raw)
        return 0.0
    if isinstance(raw, (int, float)):
        if raw < 0:
            ctx.error(f"timeout 不能为负数：{raw}", path=path, at=raw)
            return 0.0
        return float(raw)
    if isinstance(raw, str):
        try:
            value = float(raw.strip())
        except ValueError:
            ctx.error(f"timeout 应当是秒数（数字），实际是 {raw!r}", path=path, at=raw)
            return 0.0
        if value < 0:
            ctx.error(f"timeout 不能为负数：{raw}", path=path, at=raw)
            return 0.0
        return value
    ctx.error(f"timeout 应当是秒数（数字），实际是 {type(raw).__name__}", path=path, at=raw)
    return 0.0


def _parse_on_failure(ctx: _Ctx, raw: Any, *, path: str, at: Any) -> FailurePolicy:
    """解析 `on_failure`。

    支持的写法（大小写不敏感，括号与空格容错）：
      · ``abort`` / ``continue``
      · ``retry``（默认重试 1 次）
      · ``retry(3)`` / ``retry:3`` / ``retry 3``

    为什么连 `retry:3` 也认：YAML 里写 ``on_failure: retry:3`` 会因为冒号
    被当成映射而报错，人会顺手改成引号形式 ``"retry:3"``，认了它少一次
    来回；但**不认识的一律报错**，不做模糊匹配。
    """
    if raw is None:
        return FailurePolicy()
    if isinstance(raw, FailurePolicy):             # 程序化构造 schema 时直接用
        return raw
    if isinstance(raw, int) and not isinstance(raw, bool):
        # 允许 `on_failure: 3` 吗？不允许 —— 语义不明（重试 3 次？第 3 种策略？）。
        ctx.error(f"on_failure 不能直接写数字：{raw}。{ON_FAILURE_HINT}", path=path, at=at)
        return FailurePolicy()
    if not isinstance(raw, str):
        ctx.error(f"on_failure 应当是字符串，实际是 {type(raw).__name__}。{ON_FAILURE_HINT}",
                  path=path, at=at)
        return FailurePolicy()

    text = raw.strip().lower().replace(" ", "")
    if text in ("abort", "continue"):
        return FailurePolicy(kind=text, raw=raw.strip())
    if text == "retry":
        # 裸 retry 默认重试 1 次。刻意不给"无限重试"的写法：
        # 无限重试会让一次 CI 跑到天亮，也让报告里的"成功/失败"失去意义。
        return FailurePolicy(kind="retry", retries=1, raw=raw.strip())

    count: int | None = None
    if text.startswith("retry(") and text.endswith(")"):
        count = _as_positive_int(text[len("retry("):-1])
    elif text.startswith("retry:") and text.endswith(")"):
        count = _as_positive_int(text[len("retry:"):-1])
    elif text.startswith("retry:"):
        count = _as_positive_int(text[len("retry:"):])
    elif text.startswith("retry("):
        # 括号没闭合 —— 单独指出，比"无法识别"有用
        ctx.error(f"on_failure 的括号没有闭合：{raw!r}。正确写法如 retry(3)", path=path, at=at)
        return FailurePolicy()

    if count is not None:
        if count <= 0:
            ctx.error(f"retry 次数必须大于 0（retry(1) 表示失败后重试 1 次、共最多执行 2 次），"
                      f"实际是 {count}", path=path, at=at)
            return FailurePolicy()
        if count > 100:
            # 上限是"防止把一次 CI 跑到天亮"，不是能力限制
            ctx.error(f"retry 次数过大：{count}（上限 100）。"
                      "需要更多次请改用外层调度重跑，而不是让一步无限重试", path=path, at=at)
            return FailurePolicy()
        return FailurePolicy(kind="retry", retries=count, raw=raw.strip())

    ctx.error(f"无法识别的 on_failure 写法：{raw!r}。{ON_FAILURE_HINT}", path=path, at=at)
    return FailurePolicy()


def _as_positive_int(text: str) -> int | None:
    """把 `retry(3)` 里的 `3` 取出来；不是纯数字就返回 None（交由调用方报错）。"""
    inner = text.strip()
    return int(inner) if inner.isdigit() else None


def _check_inputs(ctx: _Ctx, raw: Any, *, unknown_fields: str) -> dict[str, InputSpec]:
    """校验 `inputs` 段。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        ctx.error(f"inputs 应当是映射（每项形如 name: {{type: string}}），"
                  f"实际是 {type(raw).__name__}", path="inputs", at=raw)
        return {}
    out: dict[str, InputSpec] = {}
    for name, spec in raw.items():
        key = str(name)
        path = f"inputs.{key}"
        if not isinstance(spec, dict):
            # 简写形式 `ticket_id: string` 也认 —— 它无歧义，且比嵌套少一半行数
            if isinstance(spec, str) and spec.strip().lower() in INPUT_TYPES:
                out[key] = InputSpec(name=key, type=spec.strip().lower(),
                                     position=ctx.pos(spec, ctx.pos(raw)))
                continue
            ctx.error(f"入参 {key} 的声明应当是映射（如 {{type: string, required: true}}），"
                      f"实际是 {type(spec).__name__}", path=path, at=spec)
            continue
        _check_unknown_fields(ctx, spec, INPUT_FIELDS, where=path, unknown_fields=unknown_fields)
        itype = spec.get("type", "string")
        if not isinstance(itype, str) or itype.strip().lower() not in INPUT_TYPES:
            ctx.error(f"入参 {key} 的 type 不支持：{itype!r}。支持：{'、'.join(INPUT_TYPES)}",
                      path=f"{path}.type", at=spec.get("type", spec),
                      suggestions=suggest(str(itype), list(INPUT_TYPES)))
            itype = "string"
        required = _as_bool(ctx, spec["required"], path=f"{path}.required") \
            if "required" in spec else False
        default = spec.get("default")
        if required and default is not None:
            # 两者同时给并非语法错误，但语义矛盾：给了默认值就永远不会"缺"。
            # 归为错误而不是警告 —— 它几乎总是"我本想写 required: false"的笔误。
            ctx.error(f"入参 {key} 同时声明了 required: true 与 default，二者矛盾："
                      "有默认值时该入参永远不会缺失，required 形同虚设。"
                      "请二选一（要默认值就 required: false）",
                      path=path, at=spec)
            required = False
        out[key] = InputSpec(
            name=key, type=str(itype).strip().lower(), required=required, default=default,
            description=str(spec.get("description") or ""),
            position=ctx.pos(spec, ctx.pos(raw)),
        )
    return out


def _check_step(ctx: _Ctx, raw: Any, index: int, *, tool_names: list[str] | None,
                unknown_fields: str) -> StepSpec | None:
    """校验单个步骤；结构不可用时返回 None（问题已记入 ctx）。"""
    path = f"steps[{index}]"
    if not isinstance(raw, dict):
        ctx.error(f"步骤应当是映射（含 id / type），实际是 {type(raw).__name__}",
                  path=path, at=raw)
        return None

    step_id = raw.get("id")
    if step_id is None:
        ctx.error("缺少必需的 id 字段。每个步骤都要有唯一 id，"
                  "后续步骤靠 `{{ steps.<id>.output }}` 引用它的结果", path=f"{path}.id", at=raw)
        step_id = f"<第{index + 1}步无id>"
    elif not isinstance(step_id, str) or not step_id.strip():
        ctx.error(f"id 应当是非空字符串，实际是 {step_id!r}", path=f"{path}.id", at=raw.get("id"))
        step_id = f"<第{index + 1}步id非法>"
    else:
        step_id = step_id.strip()
        # 点号会让模板路径产生歧义（steps.a.b.output 到底是哪个步骤），提前拦掉
        if "." in step_id or "{{" in step_id:
            ctx.error(f"id 里不能包含 '.' 或 '{{'：{step_id!r}（模板用 `steps.<id>.output` "
                      "定位步骤，点号会让路径无法解析）", path=f"{path}.id", at=raw.get("id"))

    step_path = f"steps[{index}]({step_id})"

    stype = raw.get("type")
    if stype is None:
        ctx.error(f"缺少必需的 type 字段。可选：{'、'.join(STEP_TYPES)}",
                  path=f"{step_path}.type", at=raw)
        allowed = COMMON_STEP_FIELDS
        stype = ""
    elif not isinstance(stype, str) or stype.strip() not in STEP_TYPES:
        ctx.error(f"未知的步骤类型：{stype!r}。可选：{'、'.join(STEP_TYPES)}",
                  path=f"{step_path}.type", at=raw.get("type"),
                  suggestions=suggest(str(stype), list(STEP_TYPES)))
        allowed = COMMON_STEP_FIELDS
        stype = ""
    else:
        stype = stype.strip()
        allowed = COMMON_STEP_FIELDS | STEP_TYPES[stype][1]

    _check_unknown_fields(ctx, raw, allowed, where=step_path, unknown_fields=unknown_fields)

    # ── 类型专属必需字段 ──────────────────────────────
    if stype:
        for required_field in STEP_TYPES[stype][0]:
            if raw.get(required_field) in (None, ""):
                ctx.error(f"{stype} 类型的步骤缺少必需字段 '{required_field}'",
                          path=f"{step_path}.{required_field}", at=raw)

    tool = ""
    args: dict[str, Any] = {}
    prompt = ""
    system = ""
    condition = then = else_ = ""

    if stype == "tool":
        tool = str(raw.get("tool") or "").strip()
        if tool:
            if tool_names is not None and tool not in tool_names:
                # 只做"看起来像不像"的建议，不拦下加载：注册表是**运行时**注入的，
                # 加载器不知道某个 MCP 工具此刻挂没挂上。真正的判定在执行时
                # （registry.dispatch 会返回带建议的失败结果）。
                ctx.warn(f"工具 {tool!r} 不在当前内置工具清单里。若它来自 MCP/插件，"
                         "请确认运行时已挂载；否则这一步会在执行时失败",
                         path=f"{step_path}.tool", at=raw.get("tool"),
                         suggestions=suggest(tool, tool_names))
        args_raw = raw.get("args")
        if args_raw is None:
            args = {}
        elif isinstance(args_raw, dict):
            args = args_raw
        else:
            ctx.error(f"tool 步骤的 args 应当是映射（工具参数名 → 值），"
                      f"实际是 {type(args_raw).__name__}", path=f"{step_path}.args", at=args_raw)
    elif stype == "llm":
        prompt = str(raw.get("prompt") or "")
        system = str(raw.get("system") or "")
    elif stype == "human":
        prompt = str(raw.get("prompt") or "")
    elif stype == "branch":
        condition = str(raw.get("condition") or "")
        then = str(raw.get("then") or "")
        else_ = str(raw.get("else") or "")
        # 条件语法在这里就查掉：`condition: status = ok`（单等号）是典型笔误，
        # 若留到执行时才发现，前序步骤（可能已经改了生产系统）就白跑了。
        if condition:
            _check_condition(ctx, condition, path=f"{step_path}.condition", at=raw.get("condition"))

    timeout = _check_timeout(ctx, raw["timeout"], path=f"{step_path}.timeout") \
        if "timeout" in raw else 0.0
    policy = _parse_on_failure(ctx, raw.get("on_failure"), path=f"{step_path}.on_failure",
                               at=raw.get("on_failure"))

    return StepSpec(
        id=step_id, type=stype, position=ctx.pos(raw), tool=tool, args=args,
        prompt=prompt, system=system, condition=condition, then=then, else_=else_,
        timeout=timeout, on_failure=policy,
    )


def _check_condition(ctx: _Ctx, condition: str, *, path: str, at: Any) -> None:
    """校验 branch 条件：必须是 `左值 <算子> 右值` 的受限比较。

    为什么要**在加载期**就查语法：branch 的条件写错 = 跳转走错分支，
    而错的那条分支可能正在改生产配置。这是本实现里最不能"跑了才知道"的一类错误。

    为什么只允许三种算子、且左右都只是模板或字面量：
    见 template.py —— 一旦支持表达式求值，YAML 就成了代码，评审也就失效了。
    """
    import re

    text = condition.strip()
    for op in CONDITION_OPS:
        idx = text.find(op)
        if idx > 0:
            left, right = text[:idx].strip(), text[idx + len(op):].strip()
            if not left or not right:
                ctx.error(f"条件 {condition!r} 的比较算子 {op!r} 两侧不能为空。"
                          "写法：`{{ steps.x.output.status }} == ok`", path=path, at=at)
            return
    # 没找到合法算子：给出"最常见的三种笔误"的定向提示，而不是一句"语法错误"
    hint = ""
    if re.search(r"(?<![=!<>])=(?!=)", text):
        hint = "（检测到单个 '='，比较相等请写 '=='）"
    elif " equals " in f" {text} ":
        hint = "（比较相等请写 '=='，本实现不做表达式求值）"
    ctx.error(f"无法解析的 branch 条件：{condition!r}{hint}。"
              f"仅支持 {'、'.join(CONDITION_OPS)} 三种受限比较，"
              "两侧可以是模板 `{{ ... }}` 或字面量（如 `\"approved\" == \"true\"`）。"
              "不支持 and/or/函数调用等表达式", path=path, at=at)


def collect_issues(
    document: Any,
    *,
    positions: dict[int, Position] | None = None,
    source: str = "",
    source_digest: str = "",
    tool_names: list[str] | None = None,
    unknown_fields: str = UNKNOWN_FIELD_ERROR,
) -> tuple[WorkflowSchema | None, list[Issue], list[Issue]]:
    """把已解析成 Python 对象的 YAML 文档校验成 `WorkflowSchema`。

    返回 `(schema | None, errors, warnings)`。schema 为 None 表示存在**错误**级问题
    （warnings 不阻止加载）。

    `tool_names` 只用于"给建议"，不用来拒绝加载 —— 工具注册表是运行时注入的，
    加载器看不到某个 MCP 工具此刻挂没挂上。真正的判定在执行时。
    """
    ctx = _Ctx(positions)
    if not isinstance(document, dict):
        ctx.error(f"工作流文件的顶层应当是映射（含 version/name/steps），"
                  f"实际是 {type(document).__name__}", at=document)
        return None, ctx.issues, ctx.warnings

    _check_unknown_fields(ctx, document, TOP_LEVEL_FIELDS, where="", unknown_fields=unknown_fields)

    # ── version：强制，且不认识就报错 ──────────────────
    version_raw = document.get("version")
    version = 0
    if version_raw is None:
        ctx.error("缺少必需的 version 字段（当前支持的 schema 版本："
                  f"{'、'.join(str(v) for v in SUPPORTED_VERSIONS)}）。"
                  "版本必须显式声明：执行器据它决定语义，绝不能靠猜",
                  path="version", at=document)
    elif isinstance(version_raw, bool) or not isinstance(version_raw, int):
        ctx.error(f"version 应当是整数（如 version: 1），实际是 "
                  f"{type(version_raw).__name__}：{version_raw!r}", path="version", at=version_raw)
    elif version_raw not in SUPPORTED_VERSIONS:
        ctx.error(f"不支持的 schema 版本：{version_raw}。本实现仅支持 "
                  f"{'、'.join(str(v) for v in SUPPORTED_VERSIONS)}；"
                  "遇到不认识的版本必须明确报错而不是按相近版本执行 —— "
                  "按错误语义静默跑完一份已批准的流程，比直接失败严重得多",
                  path="version", at=version_raw)
    else:
        version = version_raw

    name = document.get("name")
    if name is None or not str(name).strip():
        ctx.error("缺少必需的 name 字段（工作流名称，用于报告与审计）", path="name", at=document)
        name = ""
    else:
        name = str(name).strip()

    inputs = _check_inputs(ctx, document.get("inputs"), unknown_fields=unknown_fields)

    steps_raw = document.get("steps")
    steps: list[StepSpec] = []
    if steps_raw is None:
        ctx.error("缺少必需的 steps 字段（至少要有一步）", path="steps", at=document)
    elif not isinstance(steps_raw, list):
        ctx.error(f"steps 应当是列表，实际是 {type(steps_raw).__name__}", path="steps", at=steps_raw)
    elif not steps_raw:
        ctx.error("steps 不能为空列表：一个什么都不做的工作流无法评审，"
                  "也没有执行的意义", path="steps", at=steps_raw)
    else:
        for i, raw_step in enumerate(steps_raw):
            spec = _check_step(ctx, raw_step, i, tool_names=tool_names,
                               unknown_fields=unknown_fields)
            if spec is not None:
                steps.append(spec)

    # ── 步骤间引用：重复 id / 跳转目标 / 模板变量 ─────────
    _check_step_references(ctx, steps, inputs)

    metadata_raw = document.get("metadata")
    metadata: dict[str, Any] = {}
    if metadata_raw is None:
        metadata = {}
    elif isinstance(metadata_raw, dict):
        metadata = metadata_raw
    else:
        ctx.error(f"metadata 应当是映射（自由扩展信息），实际是 "
                  f"{type(metadata_raw).__name__}", path="metadata", at=metadata_raw)

    description = str(document.get("description") or "")

    if ctx.failed:
        return None, ctx.issues, ctx.warnings

    schema = WorkflowSchema(
        version=version, name=name, description=description, inputs=inputs, steps=steps,
        metadata=metadata, source=source, warnings=list(ctx.warnings),
        source_digest=source_digest,
    )
    return schema, ctx.issues, ctx.warnings


def _check_step_references(ctx: _Ctx, steps: list[StepSpec],
                           inputs: dict[str, InputSpec]) -> None:
    """检查步骤之间的一致性：id 唯一、跳转目标存在、模板变量有定义。

    这一组是"工作流即代码"最核心的静态检查 —— 它能在**没连任何系统**的情况下
    回答"这份流程跑得起来吗"。ReAct 模式做不到这件事，因为步骤是运行时才生成的。
    """
    # 1) 重复 id
    seen: dict[str, int] = {}
    for i, step in enumerate(steps):
        if step.id in seen:
            ctx.error(f"步骤 id 重复：{step.id!r}（首次出现在 steps[{seen[step.id]}]）。"
                      "id 是模板引用与报告的唯一键，重复会让 `{{ steps.x.output }}` "
                      "指向不确定的一步", path=f"steps[{i}]({step.id}).id", at=None,
                      position=step.position)
        else:
            seen[step.id] = i

    # 2) 跳转目标必须存在（且不能是循环 —— v1 不支持循环，见 executor 说明）
    for i, step in enumerate(steps):
        if step.type != "branch":
            continue
        for field_name, target in (("then", step.then), ("else", step.else_)):
            if not target:
                continue      # 缺字段的问题在 _check_step 里已经报过
            if target not in seen:
                ctx.error(f"branch 的 {field_name} 指向不存在的步骤：{target!r}",
                          path=f"steps[{i}]({step.id}).{field_name}", position=step.position,
                          suggestions=suggest(target, list(seen)))
                continue
            if seen[target] <= i:
                # 往回跳 = 循环。v1 明确不支持：循环会让 `on_failure: retry`、
                # 超时、报告字段的语义全部复杂化，而"重跑一遍整条流程"用外层
                # 调度（CI / 服务端重试）表达得更清楚、也更好审。
                ctx.error(f"branch 的 {field_name} 指向已执行过的步骤 {target!r}"
                          "（第 {} 步 → 第 {} 步），v1 不支持循环。"
                          "需要重复执行请用外层调度重跑整个工作流".format(
                              i + 1, seen[target] + 1),
                          path=f"steps[{i}]({step.id}).{field_name}", position=step.position)

    # 3) 模板变量：inputs 必须声明、env 必须大写、steps 必须存在
    from automind.workflow.template import validate_template

    declared_inputs = set(inputs)
    # 所有步骤 id 一次性收齐（而不是边遍历边攒）：只有拿到**完整**的 id 集合，
    # `validate_template` 才能区分"引用了不存在的步骤"（错误）与
    # "引用了排在自己后面的步骤"（warning）。边攒边查会把后一种误判成前一种 ——
    # 实测踩过：一份完全合法的 `{{ steps.b.output }}`（b 在后面）被报成"不存在"。
    all_step_ids = set(seen)
    for i, step in enumerate(steps):
        for field_name, text in _template_fields(step):
            if not text:
                continue
            path = f"steps[{i}]({step.id}).{field_name}"
            for problem in validate_template(text, inputs=declared_inputs,
                                             step_ids=all_step_ids,
                                             order=seen, current_index=i):
                position = _arg_position(ctx, step, field_name, problem.path)
                if problem.kind == "forward_reference":
                    # 前向引用**降级为 warning**：结构合法，只是当前执行路径上
                    # 多半还没跑。为什么不报错：branch 会让"先跳到后面再回来"的
                    # 写法变得难以静态判定（判断可达性等于实现半个 CFG 分析），
                    # 而 v1 的目标是"能拦住确定的错"，不是"能证明所有错"。
                    ctx.warn(f"{problem.message}（该步骤排在第 {i + 1} 步，"
                             "引用的是它后面的步骤）",
                             path=path, position=position)
                else:
                    ctx.error(problem.message, path=path, position=position,
                              suggestions=problem.suggestions)


def _template_fields(step: StepSpec) -> list[tuple[str, str]]:
    """一个步骤里所有"会被渲染"的文本字段及其字段名。

    必须与 executor 实际渲染的字段逐一对应 —— 漏一个就会让"加载期校验通过、
    执行期才报未定义变量"，那正是这套静态检查存在的意义（提前发现）。
    """
    fields: list[tuple[str, str]] = []
    if step.type == "tool":
        fields.append(("tool", step.tool))
        for key, value in step.args.items():
            if isinstance(value, str):
                fields.append((f"args.{key}", value))
            elif isinstance(value, (list, dict)):
                # 嵌套结构里的模板也要查：否则 `json_body: {id: "{{ inputs.nope }}"}}`
                # 这种最常见的写法会漏过校验
                fields.extend(_walk_nested(f"args.{key}", value))
    elif step.type in ("llm", "human"):
        fields.append(("prompt", step.prompt))
        if step.system:
            fields.append(("system", step.system))
    elif step.type == "branch":
        fields.append(("condition", step.condition))
    return fields


def _walk_nested(prefix: str, value: Any) -> list[tuple[str, str]]:
    """递归展开嵌套 list/dict，取出其中的字符串模板。"""
    out: list[tuple[str, str]] = []
    if isinstance(value, str):
        out.append((prefix, value))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            out.extend(_walk_nested(f"{prefix}[{i}]", item))
    elif isinstance(value, dict):
        for k, item in value.items():
            out.extend(_walk_nested(f"{prefix}.{k}", item))
    return out


def _arg_position(ctx: _Ctx, step: StepSpec, field_name: str, sub_path: str) -> Position:
    """定位到具体参数值的行号。

    行号精确到"哪个参数"是这套报错可用性的关键：一个 tool 步骤可能有十几个
    参数，只说"第 12 行有问题"还得自己找。
    """
    if field_name == "tool":
        return ctx.pos(step.tool, step.position)
    if field_name in ("prompt", "system", "condition"):
        return ctx.pos(getattr(step, field_name), step.position)
    if field_name.startswith("args."):
        obj: Any = step.args
        for part in field_name[len("args."):].split("."):
            key = part.split("[", 1)[0]
            if isinstance(obj, dict) and key in obj:
                obj = obj[key]
            else:
                return step.position
            if "[" in part:
                idx = part[part.find("[") + 1:part.find("]")]
                if isinstance(obj, list) and idx.isdigit() and int(idx) < len(obj):
                    obj = obj[int(idx)]
                else:
                    return step.position
        hit = ctx.pos(obj, step.position)
        return hit
    return step.position


__all__ = [
    "COMMON_STEP_FIELDS",
    "CONDITION_OPS",
    "INPUT_TYPES",
    "ON_FAILURE_HINT",
    "STEP_TYPES",
    "SUPPORTED_VERSIONS",
    "TOP_LEVEL_FIELDS",
    "UNKNOWN_FIELD_ERROR",
    "UNKNOWN_FIELD_WARNING",
    "FailurePolicy",
    "InputSpec",
    "StepSpec",
    "WorkflowSchema",
    "collect_issues",
]
