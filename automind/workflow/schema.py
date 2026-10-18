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

from automind.workflow.conditions import CONDITION_OPS as _CONDITION_OPS
from automind.workflow.conditions import check_condition as _check_condition_text
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
    # v1.10.0（企业版）：approvers + mode = 多级 / 按角色审批（R-303）
    "human": (("prompt",), frozenset({"prompt", "approvers", "mode"})),
    "branch": (("condition", "then", "else"), frozenset({"condition", "then", "else"})),
    # v1.8.0：把一个子目标交给 Agent 自主完成（ReAct），工具集受 allowed_tools 限制
    "agent": (("goal",), frozenset({"goal", "allowed_tools", "max_steps",
                                    "token_budget", "mode"})),
    # v1.8.0：对一个列表逐项执行一组子步骤（上限见 workflow_foreach_max）；
    # v1.9.0：max_concurrency > 1 时逐项并发（专业版，特性 workflow_parallel）
    "foreach": (("items", "steps"), frozenset({"items", "steps", "max_concurrency"})),
    # v1.9.0：一组互不依赖的子步骤并发执行（专业版，特性 workflow_parallel）
    "parallel": (("steps",), frozenset({"steps", "max_concurrency"})),
}

#: foreach 的子步骤允许的类型。branch 与嵌套 foreach 刻意不放：
#: 子步骤里的跳转会让"第几项走了哪条路"在报告里无法逐行对照，
#: 嵌套循环会让迭代上限的语义变成乘法 —— 两者都等真实需求出现再加。
FOREACH_CHILD_TYPES: tuple[str, ...] = ("tool", "llm", "human", "agent")

#: parallel 组的子步骤允许的类型（v1.9.0）。branch 不放：并发的几步里有一步要
#: "跳到别处"没有确定的含义；嵌套 parallel / foreach 让并发度变成乘法、续跑粒度
#: 也说不清 —— 与 foreach 同一条理由。
PARALLEL_CHILD_TYPES: tuple[str, ...] = ("tool", "llm", "human", "agent")

#: 并发度：parallel 默认 4（路线图 R-204）；foreach 默认 1（= 逐项顺序执行）。
#: 上限 32 不是能力限制，是"别把下游系统打挂"的护栏 —— 需要更高请分批。
PARALLEL_DEFAULT_CONCURRENCY = 4
MAX_CONCURRENCY_CAP = 32

#: agent 步骤支持的执行模式（目前只有 ReAct）。
AGENT_MODES: tuple[str, ...] = ("react",)

#: agent 步骤 max_steps 的默认值与上限。
AGENT_DEFAULT_MAX_STEPS = 20
AGENT_MAX_STEPS_CAP = 200

#: **已规划、属于更高版本**的写法 → (特性键, 计划提供的版本)。
#: 为什么要单独登记而不是当"未知字段/未知类型"报：写的人多半是照着
#: 文档写的，"未知"会让他以为自己拼错了。这里直接说清"需要哪个版本"，
#: 并且在**加载期**拦下 —— 不能让一条流程跑到一半才因为版本不够失败。
#: v1.9.0 起 parallel / triggers 已实现（移入下面的 FEATURE_GATED_*），这两张表
#: 暂时为空，机制保留给后续版本的规划项。
RESERVED_STEP_TYPES: dict[str, tuple[str, str]] = {}
RESERVED_TOP_LEVEL: dict[str, tuple[str, str]] = {}

#: **需要商业特性**的写法 → 特性键（v1.9.0）。与 RESERVED_* 的区别：这些已经实现，
#: 只是不在当前版本的授权范围内；加载期按版本拦截，并说清需要哪个版本 ——
#: 不能让一条流程跑到一半才因为版本不够失败（路线图 §2.4 拦截原则）。
FEATURE_GATED_STEP_TYPES: dict[str, str] = {
    "parallel": "workflow_parallel",
}
FEATURE_GATED_TOP_LEVEL: dict[str, str] = {
    "triggers": "workflow_triggers",
    "on_restart": "workflow_triggers",
    "notify": "workflow_triggers",
}

#: 所有步骤共有的字段（类型无关）。
COMMON_STEP_FIELDS: frozenset[str] = frozenset({"id", "type", "timeout", "on_failure"})

#: 顶层合法字段。
TOP_LEVEL_FIELDS: frozenset[str] = frozenset(
    {"version", "name", "description", "inputs", "steps", "metadata",
     # v1.9.0（专业版）：触发器、重启续跑策略、失败通知
     "triggers", "on_restart", "notify"})

#: 触发器（v1.9.0，R-201 / R-202 / R-209）的合法字段。
#: cron 与 webhook 二选一；retry 是"触发运行没有全绿时，从断点自动续跑几次"。
TRIGGER_FIELDS: frozenset[str] = frozenset(
    {"id", "cron", "webhook", "inputs", "description", "enabled", "retry", "retry_delay"})
TRIGGER_MAX_RETRY = 5
TRIGGER_DEFAULT_RETRY_DELAY = 60
TRIGGER_MIN_RETRY_DELAY = 10

#: 多级 / 按角色审批（v1.10.0，企业版，R-303）：审批人写成 ``role:<角色>`` 或
#: ``user:<用户名>``；mode=all 按顺序逐级审批（每一级都要通过），any 任一人通过即可。
APPROVER_PREFIXES: tuple[str, ...] = ("role", "user")
APPROVAL_MODES: tuple[str, ...] = ("all", "any")
MAX_APPROVERS = 5

#: 服务重启时，被打断的**触发运行**怎么办（D5，R-208）。手动运行一律只提示。
ON_RESTART_CHOICES: tuple[str, ...] = ("notify", "resume")

#: 失败通知（R-209）：什么时候发、发到哪。
NOTIFY_FIELDS: frozenset[str] = frozenset({"on", "channels"})
NOTIFY_EVENTS: tuple[str, ...] = ("failed", "partial", "interrupted", "ok")
NOTIFY_CHANNELS: tuple[str, ...] = ("webhook", "im", "desktop")
NOTIFY_DEFAULT_EVENTS: tuple[str, ...] = ("failed", "partial", "interrupted")
NOTIFY_DEFAULT_CHANNELS: tuple[str, ...] = ("webhook",)

#: 入参声明的合法字段。
INPUT_FIELDS: frozenset[str] = frozenset({"type", "required", "default", "description"})

#: 入参支持的类型名（只做"声明式"校验：类型不符时由 CLI/服务端做转换，
#: 转换不了就明确报错，不悄悄按字符串塞进去）。
INPUT_TYPES: tuple[str, ...] = ("string", "integer", "number", "boolean", "object", "array")

#: branch 步骤支持的受限比较算子。**这是刻意的白名单**：
#: 模板/条件一旦支持表达式求值，YAML 就变成了代码执行通道，
#: "客户能评审的流程"立刻退化成"客户看不懂的脚本"。详见 template.py。
#: v1.9.0（R-206）扩到 9 个，解析与求值统一在 conditions.py（校验与执行共用一份）。
CONDITION_OPS: tuple[str, ...] = _CONDITION_OPS

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
    #: agent 步骤（v1.8.0）
    goal: str = ""
    #: None = 未声明（运行时只给只读工具）；[] = 显式声明"一个工具都不给"
    allowed_tools: list[str] | None = None
    max_steps: int = AGENT_DEFAULT_MAX_STEPS
    token_budget: int = 0                # 0 = 不单独设限（仍受任务总预算约束）
    mode: str = "react"
    #: foreach 步骤（v1.8.0）：items 是模板串或字面量列表
    items: Any = None
    substeps: list[StepSpec] = field(default_factory=list)
    #: 子步骤所属 foreach / parallel 的 id（顶层步骤为空串）
    parent: str = ""
    #: 并发度（v1.9.0）：parallel 默认 4；foreach 默认 1（逐项顺序）
    max_concurrency: int = 1
    #: human 步骤的审批人（v1.10.0，企业版）：空 = 任何能看到弹窗的人都可以批
    approvers: list[str] = field(default_factory=list)
    approval_mode: str = "all"
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
            if self.approvers:
                data.update({"approvers": list(self.approvers), "mode": self.approval_mode})
        elif self.type == "branch":
            data.update({"condition": self.condition, "then": self.then, "else": self.else_})
        elif self.type == "agent":
            data.update({"goal": self.goal, "allowed_tools": self.allowed_tools,
                         "max_steps": self.max_steps, "token_budget": self.token_budget,
                         "mode": self.mode})
        elif self.type == "foreach":
            data.update({"items": self.items,
                         "steps": [s.as_dict() for s in self.substeps]})
            if self.max_concurrency > 1:
                data["max_concurrency"] = self.max_concurrency
        elif self.type == "parallel":
            data.update({"steps": [s.as_dict() for s in self.substeps],
                         "max_concurrency": self.max_concurrency})
        if self.parent:
            data["parent"] = self.parent
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
class TriggerSpec:
    """一个触发器的声明（v1.9.0，专业版）。

    ``id`` 在一份工作流内唯一（缺省 ``t1`` / ``t2``…按声明顺序）；服务端用
    "文件名 + id" 定位它，启停状态与 webhook 令牌都挂在这个键上。
    """

    id: str
    kind: str                            # cron | webhook
    cron: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    enabled: bool = True
    #: 触发运行没有全绿时，从断点自动续跑的次数（0 = 不重试）
    retry: int = 0
    retry_delay: float = TRIGGER_DEFAULT_RETRY_DELAY
    #: cron 相邻两次触发的最短间隔（秒，加载期算出；webhook 为 0）
    min_interval: int = 0
    position: Position = field(default_factory=Position)

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "kind": self.kind, "inputs": self.inputs,
                                "description": self.description, "enabled": self.enabled,
                                "retry": self.retry, "retry_delay": self.retry_delay,
                                "line": self.position.line}
        if self.kind == "cron":
            data.update({"cron": self.cron, "min_interval": self.min_interval})
        return data


@dataclass
class NotifySpec:
    """失败通知（v1.9.0，R-209）：哪些结果要通知、走哪些通道。"""

    on: list[str] = field(default_factory=lambda: list(NOTIFY_DEFAULT_EVENTS))
    channels: list[str] = field(default_factory=lambda: list(NOTIFY_DEFAULT_CHANNELS))
    #: 文件里是否显式写了 notify（没写时用默认值，但界面要能区分）
    declared: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"on": list(self.on), "channels": list(self.channels), "declared": self.declared}


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
    #: v1.9.0（专业版）：触发器 / 重启续跑策略 / 失败通知
    triggers: list[TriggerSpec] = field(default_factory=list)
    on_restart: str = "notify"
    notify: NotifySpec = field(default_factory=NotifySpec)

    # ── 查询辅助 ──────────────────────────────────────

    @property
    def step_ids(self) -> list[str]:
        return [s.id for s in self.steps]

    def get_step(self, step_id: str) -> StepSpec | None:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None

    def secret_names(self) -> list[str]:
        """引用到的凭据名（v1.10.0）：界面据此提示"运行前要先配好哪些凭据"。"""
        from automind.workflow.template import secret_refs

        names: list[str] = []
        for step in [*self.steps, *(c for s in self.steps for c in s.substeps)]:
            for _field, text in _template_fields(step):
                for name in secret_refs(text):
                    if name not in names:
                        names.append(name)
        return names

    def get_trigger(self, trigger_id: str) -> TriggerSpec | None:
        return next((t for t in self.triggers if t.id == trigger_id), None)

    def uses_agent(self) -> bool:
        """是否含 agent 步骤（含 foreach 内的）—— 服务端据此决定要不要准备 Agent。"""
        return any(s.type == "agent" or any(c.type == "agent" for c in s.substeps)
                   for s in self.steps)

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
            "triggers": [t.as_dict() for t in self.triggers],
            "on_restart": self.on_restart,
            "notify": self.notify.as_dict(),
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
        if self.triggers:
            lines.append("触发器：")
            for t in self.triggers:
                what = f"定时 {t.cron}" if t.kind == "cron" else "入站 webhook"
                extra = f"，失败后自动续跑 {t.retry} 次" if t.retry else ""
                off = "（已停用）" if not t.enabled else ""
                lines.append(f"  - [{t.id}] {what}{extra}{off}")
            lines.append(f"重启策略：{'自动续跑' if self.on_restart == 'resume' else '只通知'}")
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
        who = ""
        if step.approvers:
            joiner = " → " if step.approval_mode == "all" else " 或 "
            who = f"  审批人={joiner.join(step.approvers)}"
        return f"[{step.id}] type=human  待批内容={_clip(step.prompt)}{who}{tail}"
    if step.type == "branch":
        return (f"[{step.id}] type=branch  当 {_clip(step.condition)} 成立 → {step.then}，"
                f"否则 → {step.else_}{tail}")
    if step.type == "agent":
        tools = ("仅只读工具（未声明 allowed_tools）" if step.allowed_tools is None
                 else "、".join(step.allowed_tools) or "无")
        return (f"[{step.id}] type=agent  目标={_clip(step.goal)}  工具={tools}  "
                f"最多 {step.max_steps} 步{tail}")
    if step.type == "foreach":
        inner = "、".join(f"{s.id}({s.type})" for s in step.substeps)
        conc = f"  并发={step.max_concurrency}" if step.max_concurrency > 1 else ""
        return (f"[{step.id}] type=foreach  逐项={_clip(str(step.items))}  "
                f"子步骤={inner}{conc}{tail}")
    if step.type == "parallel":
        inner = "、".join(f"{s.id}({s.type})" for s in step.substeps)
        return (f"[{step.id}] type=parallel  并发执行={inner}  "
                f"最多同时 {step.max_concurrency} 个{tail}")
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

    def __init__(self, positions: dict[int, Position] | None = None, *,
                 foreach_max: int | None = None,
                 features: set[str] | frozenset[str] | None = None,
                 edition_label: str = "", edition: str = "",
                 min_interval: int | None = None) -> None:
        self.positions = positions if positions is not None else {}
        self.issues: list[Issue] = []
        self.warnings: list[Issue] = []
        #: foreach 单次最大迭代数（None = 不限）；由 loader 按版本限额注入
        self.foreach_max = foreach_max
        #: 定时触发的最小间隔（秒；None = 不限）；由 loader 按版本限额注入（v1.9.0）
        self.min_interval = min_interval
        #: 当前版本已授权的商业特性键（None = 不做版本拦截，如纯离线 CLI 校验）
        self.features = features
        self.edition_label = edition_label
        #: 当前版本名（community / pro / enterprise；空 = 不按版本判断）
        self.edition = edition

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
        # 标量值没有行号（只有容器有）—— 退到所在映射的行号，至少能定位到哪一步
        position = ctx.pos(mapping[key], ctx.pos(mapping))
        if unknown_fields == UNKNOWN_FIELD_WARNING:
            ctx.warn(message, path=f"{where}.{key}", position=position, suggestions=tips)
        else:
            ctx.error(message, path=f"{where}.{key}", position=position, suggestions=tips)


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


def _reserved_message(ctx: _Ctx, what: str, feature: str, since: str) -> str:
    """"这是更高版本的写法"的提示语（加载期拦截用）。

    两种情形分开说：版本不够 → 告诉他需要哪个版本；版本够了（或该特性已授权）→
    如实说"本版本还没实现、计划何时提供"，绝不假装支持然后在运行时悄悄忽略。
    """
    from automind.core.edition import (
        _EDITION_LABELS,
        COMMERCIAL_FEATURES,
        EDITION_RANK,
        PLANNED_FEATURES,
    )

    if feature in PLANNED_FEATURES:
        tier, label, since = PLANNED_FEATURES[feature]
    else:
        tier, label = COMMERCIAL_FEATURES.get(feature, ("pro", feature))
    tier_label = _EDITION_LABELS.get(tier, tier)
    licensed = bool(ctx.features is not None and feature in ctx.features) or (
        bool(ctx.edition) and EDITION_RANK.get(ctx.edition, 0) >= EDITION_RANK.get(tier, 1))
    if licensed:
        return (f"{what}（{label}）属于{tier_label}能力，当前版本已满足，但 AutoMind 尚未实现，"
                f"计划于 {since} 提供；请升级 AutoMind 后再使用")
    current = f"，当前为{ctx.edition_label}" if ctx.edition_label else ""
    return (f"{what} 需要{tier_label}（{label}）{current}。"
            "加载期即拦截，避免流程跑到一半才因版本不够而失败；dry-run 同样不可用该写法")


def _feature_missing(ctx: _Ctx, feature: str) -> bool:
    """当前版本没有授权该特性（features 为 None = 不做版本拦截，如离线工具）。"""
    return ctx.features is not None and feature not in ctx.features


def _gated_message(ctx: _Ctx, what: str, feature: str) -> str:
    """"这个写法需要更高版本"的提示语（v1.9.0 起这些写法已实现，只是未授权）。"""
    from automind.core.edition import _EDITION_LABELS, COMMERCIAL_FEATURES

    tier, label = COMMERCIAL_FEATURES.get(feature, ("pro", feature))
    tier_label = _EDITION_LABELS.get(tier, tier)
    current = f"，当前为{ctx.edition_label}" if ctx.edition_label else ""
    return (f"{what} 需要{tier_label}（{label}）{current}。加载期即拦截，"
            "避免流程跑到一半才因版本不够而失败；安装 automind-pro 并配置许可证后可用")


def _check_step(ctx: _Ctx, raw: Any, index: int, *, tool_names: list[str] | None,
                unknown_fields: str, parent: str = "",
                allowed_types: tuple[str, ...] | None = None,
                container: str = "foreach") -> StepSpec | None:
    """校验单个步骤；结构不可用时返回 None（问题已记入 ctx）。

    ``parent`` 非空表示这是 foreach / parallel（``container``）的子步骤：
    路径写成 ``steps[i].steps[j]``，类型只允许 ``allowed_types``。
    """
    path = f"{parent}.steps[{index}]" if parent else f"steps[{index}]"
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

    step_path = f"{path}({step_id})"
    type_choices = list(allowed_types or STEP_TYPES)

    stype = raw.get("type")
    if stype is None:
        ctx.error(f"缺少必需的 type 字段。可选：{'、'.join(type_choices)}",
                  path=f"{step_path}.type", at=raw)
        allowed = COMMON_STEP_FIELDS
        stype = ""
    elif isinstance(stype, str) and stype.strip() in RESERVED_STEP_TYPES:
        feature, since = RESERVED_STEP_TYPES[stype.strip()]
        ctx.error(_reserved_message(ctx, f"步骤类型 {stype.strip()!r}", feature, since),
                  path=f"{step_path}.type", at=raw)
        # 整个步骤的字段都不再逐个报"未知"：根因只有一个，就是版本
        return None
    elif not isinstance(stype, str) or stype.strip() not in STEP_TYPES:
        ctx.error(f"未知的步骤类型：{stype!r}。可选：{'、'.join(type_choices)}",
                  path=f"{step_path}.type", at=raw.get("type"),
                  suggestions=suggest(str(stype), type_choices))
        allowed = COMMON_STEP_FIELDS
        stype = ""
    elif allowed_types is not None and stype.strip() not in allowed_types:
        why = ("子步骤里的跳转会让“第几项走了哪条路”在报告里无法逐行对照，"
               "嵌套循环会让迭代上限变成乘法" if container == "foreach" else
               "并发的几步里有一步要“跳到别处”没有确定的含义，"
               "嵌套 parallel / foreach 会让并发度变成乘法")
        ctx.error(f"{container} 的子步骤不支持 {stype.strip()!r} 类型（只支持 "
                  f"{'、'.join(allowed_types)}）。{why}",
                  path=f"{step_path}.type", at=raw)
        allowed = COMMON_STEP_FIELDS | STEP_TYPES[stype.strip()][1]
        stype = ""
    else:
        stype = stype.strip()
        allowed = COMMON_STEP_FIELDS | STEP_TYPES[stype][1]
        gate = FEATURE_GATED_STEP_TYPES.get(stype)
        if gate and _feature_missing(ctx, gate):
            # 只报一条"版本不够"，但照常往下校验字段：升级之后不必再改一轮
            ctx.error(_gated_message(ctx, f"步骤类型 {stype!r}", gate),
                      path=f"{step_path}.type", at=raw)

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

    extra: dict[str, Any] = {}
    if stype == "human" and ("approvers" in raw or "mode" in raw):
        extra = _check_approvers(ctx, raw, step_path=step_path)
    if stype == "agent":
        extra = _check_agent(ctx, raw, step_path=step_path, tool_names=tool_names)
    elif stype == "foreach":
        extra = _check_foreach(ctx, raw, step_path=step_path, step_id=step_id,
                               tool_names=tool_names, unknown_fields=unknown_fields)
    elif stype == "parallel":
        extra = _check_parallel(ctx, raw, step_path=step_path, step_id=step_id,
                                tool_names=tool_names, unknown_fields=unknown_fields)

    timeout = _check_timeout(ctx, raw["timeout"], path=f"{step_path}.timeout") \
        if "timeout" in raw else 0.0
    policy = _parse_on_failure(ctx, raw.get("on_failure"), path=f"{step_path}.on_failure",
                               at=raw.get("on_failure"))

    return StepSpec(
        id=step_id, type=stype, position=ctx.pos(raw), tool=tool, args=args,
        prompt=prompt, system=system, condition=condition, then=then, else_=else_,
        timeout=timeout, on_failure=policy, parent=parent, **extra,
    )


def _as_int(ctx: _Ctx, raw: Any, *, path: str, at: Any, minimum: int,
            maximum: int | None, what: str) -> int | None:
    """把整数型字段解析出来；不合法就报错并返回 None（调用方用默认值）。"""
    if isinstance(raw, bool):
        ctx.error(f"{what} 应当是整数，不是布尔值", path=path, at=at)
        return None
    if isinstance(raw, str) and raw.strip().isdigit():
        raw = int(raw.strip())
    if not isinstance(raw, int):
        ctx.error(f"{what} 应当是整数，实际是 {type(raw).__name__}：{raw!r}", path=path, at=at)
        return None
    if raw < minimum:
        ctx.error(f"{what} 不能小于 {minimum}：{raw}", path=path, at=at)
        return None
    if maximum is not None and raw > maximum:
        ctx.error(f"{what} 过大：{raw}（上限 {maximum}）", path=path, at=at)
        return None
    return raw


def _check_agent(ctx: _Ctx, raw: dict[str, Any], *, step_path: str,
                 tool_names: list[str] | None) -> dict[str, Any]:
    """校验 agent 步骤专属字段（v1.8.0，D1/D6）。

    工具集的规则写在这里而不是运行时才定：**未声明 allowed_tools 时只给只读工具**。
    一个"交给 Agent 自主完成"的步骤默认能调任何工具，等于把工作流"可评审"的
    承诺整个让出去 —— 评审方看不到它会动什么。所以不写就给最小权限，并在加载期
    以 warning 说出来。
    """
    out: dict[str, Any] = {"goal": str(raw.get("goal") or "")}

    tools_raw = raw.get("allowed_tools")
    if tools_raw is None:
        out["allowed_tools"] = None
        ctx.warn("agent 步骤未声明 allowed_tools：运行时只会提供只读（SAFE 级）工具。"
                 "需要写文件、跑命令等能力请显式列出，例如 "
                 "allowed_tools: [file_read, file_write, python_sandbox]",
                 path=f"{step_path}.allowed_tools", at=raw)
    elif not isinstance(tools_raw, list) or not all(
            isinstance(t, str) and t.strip() for t in tools_raw):
        ctx.error("allowed_tools 应当是工具名列表，如 [file_read, file_write]",
                  path=f"{step_path}.allowed_tools", at=tools_raw)
        out["allowed_tools"] = []
    else:
        names = [t.strip() for t in tools_raw]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            ctx.warn(f"allowed_tools 里有重复的工具名：{'、'.join(dup)}",
                     path=f"{step_path}.allowed_tools", at=tools_raw)
        if tool_names is not None:
            for name in names:
                if name not in tool_names:
                    # 与 tool 步骤一样只提醒不拦：MCP/插件工具运行时才挂载
                    ctx.warn(f"allowed_tools 里的 {name!r} 不在当前内置工具清单里；"
                             "若运行时仍不存在，该工具不会提供给 Agent",
                             path=f"{step_path}.allowed_tools", at=tools_raw,
                             suggestions=suggest(name, tool_names))
        out["allowed_tools"] = list(dict.fromkeys(names))

    if "max_steps" in raw:
        value = _as_int(ctx, raw["max_steps"], path=f"{step_path}.max_steps",
                        at=raw.get("max_steps"), minimum=1, maximum=AGENT_MAX_STEPS_CAP,
                        what="max_steps")
        out["max_steps"] = value if value is not None else AGENT_DEFAULT_MAX_STEPS
    if "token_budget" in raw:
        value = _as_int(ctx, raw["token_budget"], path=f"{step_path}.token_budget",
                        at=raw.get("token_budget"), minimum=0, maximum=None,
                        what="token_budget")
        out["token_budget"] = value or 0
    mode = raw.get("mode", "react")
    if not isinstance(mode, str) or mode.strip().lower() not in AGENT_MODES:
        ctx.error(f"agent 步骤的 mode 不支持：{mode!r}。可选：{'、'.join(AGENT_MODES)}",
                  path=f"{step_path}.mode", at=raw.get("mode"),
                  suggestions=suggest(str(mode), list(AGENT_MODES)))
        mode = "react"
    out["mode"] = str(mode).strip().lower()
    return out


def _check_foreach(ctx: _Ctx, raw: dict[str, Any], *, step_path: str, step_id: str,
                   tool_names: list[str] | None, unknown_fields: str) -> dict[str, Any]:
    """校验 foreach 步骤（v1.8.0，R-107）。

    迭代上限在**加载期**能判就判：items 写成字面量列表时直接数；写成模板时
    数不了，由执行器在展开后、执行任何一项之前再判一次（同样按"整步失败"处理，
    不会出现"跑了 20 项才发现超限"）。
    """
    out: dict[str, Any] = {}
    items = raw.get("items")
    if isinstance(items, list):
        if ctx.foreach_max is not None and len(items) > ctx.foreach_max:
            ctx.error(f"foreach 的 items 有 {len(items)} 项，超过{ctx.edition_label or '当前版本'}"
                      f"单次最大迭代数 {ctx.foreach_max}。请拆分成多次运行，"
                      "或升级到更高版本（专业版上限 1000）",
                      path=f"{step_path}.items", at=items)
    elif isinstance(items, str):
        if "{{" not in items:
            ctx.error("foreach 的 items 应当是模板（如 \"{{ steps.fetch.output.rows }}\"）"
                      "或字面量列表；写成普通字符串无法逐项展开",
                      path=f"{step_path}.items", at=raw)
    elif items is not None:
        ctx.error(f"foreach 的 items 应当是模板或列表，实际是 {type(items).__name__}",
                  path=f"{step_path}.items", at=items)
    out["items"] = items

    children_raw = raw.get("steps")
    children: list[StepSpec] = []
    if children_raw is None:
        pass                                   # 缺字段已在必需字段检查里报过
    elif not isinstance(children_raw, list) or not children_raw:
        ctx.error("foreach 的 steps 应当是非空列表（每一项要执行的子步骤）",
                  path=f"{step_path}.steps", at=children_raw)
    else:
        for j, child_raw in enumerate(children_raw):
            child = _check_step(ctx, child_raw, j, tool_names=tool_names,
                                unknown_fields=unknown_fields, parent=step_path,
                                allowed_types=FOREACH_CHILD_TYPES)
            if child is not None:
                child.parent = step_id
                children.append(child)
    out["substeps"] = children

    if "max_concurrency" in raw:
        value = _as_int(ctx, raw["max_concurrency"], path=f"{step_path}.max_concurrency",
                        at=raw.get("max_concurrency"), minimum=1,
                        maximum=MAX_CONCURRENCY_CAP, what="max_concurrency")
        if value is not None and value > 1 and _feature_missing(ctx, "workflow_parallel"):
            ctx.error(_gated_message(ctx, "foreach 逐项并发（max_concurrency > 1）",
                                     "workflow_parallel"),
                      path=f"{step_path}.max_concurrency", at=raw)
        out["max_concurrency"] = value or 1
    return out


def _check_approvers(ctx: _Ctx, raw: dict[str, Any], *, step_path: str) -> dict[str, Any]:
    """human 步骤的 ``approvers`` / ``mode``（v1.10.0，企业版，R-303）。

    审批人必须写成 ``role:<角色>`` 或 ``user:<用户名>``：裸写一个名字到底是角色还是
    用户，在审批这件事上不能靠猜（批错了人，比不让批更糟）。
    """
    out: dict[str, Any] = {}
    if _feature_missing(ctx, "workflow_governance"):
        ctx.error(_gated_message(ctx, "human 步骤的 approvers / mode（多级 / 按角色审批）",
                                 "workflow_governance"), path=f"{step_path}.approvers", at=raw)
    value = raw.get("approvers")
    approvers: list[str] = []
    if value is None:
        ctx.warn("写了 mode 却没写 approvers：mode 只在指定了审批人时有意义",
                 path=f"{step_path}.mode", at=raw)
    elif isinstance(value, str):
        value = [value]
    if value is not None and (not isinstance(value, list) or not value):
        ctx.error("approvers 应当是非空列表，如 [role:finance, role:manager]",
                  path=f"{step_path}.approvers", at=raw)
        value = []
    for item in value or []:
        text = str(item).strip()
        kind, _, name = text.partition(":")
        if kind not in APPROVER_PREFIXES or not name.strip():
            ctx.error(f"审批人 {text!r} 写法不对：应为 role:<角色> 或 user:<用户名>"
                      "（裸写名字分不清是角色还是用户）", path=f"{step_path}.approvers", at=raw,
                      suggestions=[f"role:{text}", f"user:{text}"] if ":" not in text else [])
            continue
        norm = f"{kind}:{name.strip()}"
        if norm in approvers:
            ctx.error(f"审批人重复：{norm}", path=f"{step_path}.approvers", at=raw)
            continue
        approvers.append(norm)
    if len(approvers) > MAX_APPROVERS:
        ctx.error(f"审批人最多 {MAX_APPROVERS} 个（实际 {len(approvers)}）",
                  path=f"{step_path}.approvers", at=raw)
    mode = raw.get("mode", "all")
    if not isinstance(mode, str) or mode.strip().lower() not in APPROVAL_MODES:
        ctx.error(f"human 步骤的 mode 只能是 all（逐级都要通过）或 any（任一人通过）："
                  f"{mode!r}", path=f"{step_path}.mode", at=raw,
                  suggestions=suggest(str(mode), list(APPROVAL_MODES)))
        mode = "all"
    out["approvers"] = approvers
    out["approval_mode"] = str(mode).strip().lower()
    return out


def _check_parallel(ctx: _Ctx, raw: dict[str, Any], *, step_path: str, step_id: str,
                    tool_names: list[str] | None, unknown_fields: str) -> dict[str, Any]:
    """校验 parallel 并行组（v1.9.0，R-204）。

    组内子步骤**彼此并发**：互相引用对方的输出没有确定的先后，因此在
    `_check_step_references` 里直接报错；组结束后，后续步骤可以照常用
    ``{{ steps.<子步骤>.output }}`` 引用任何一个子步骤（与 foreach 不同 ——
    foreach 的子步骤每项都跑一次，出了循环没有"那一个"值）。
    """
    out: dict[str, Any] = {"max_concurrency": PARALLEL_DEFAULT_CONCURRENCY}
    children_raw = raw.get("steps")
    children: list[StepSpec] = []
    if children_raw is None:
        pass                                   # 缺字段已在必需字段检查里报过
    elif not isinstance(children_raw, list) or not children_raw:
        ctx.error("parallel 的 steps 应当是非空列表（要并发执行的子步骤）",
                  path=f"{step_path}.steps", at=children_raw)
    else:
        if len(children_raw) == 1:
            ctx.warn("parallel 组里只有一个子步骤，并发没有意义；可以直接写成普通步骤",
                     path=f"{step_path}.steps", at=children_raw)
        for j, child_raw in enumerate(children_raw):
            child = _check_step(ctx, child_raw, j, tool_names=tool_names,
                                unknown_fields=unknown_fields, parent=step_path,
                                allowed_types=PARALLEL_CHILD_TYPES, container="parallel")
            if child is not None:
                child.parent = step_id
                if child.on_failure.kind == "retry" and child.type == "human":
                    ctx.warn(f"并行组里的 human 步骤 {child.id!r} 写了 retry：审批不会重试",
                             path=f"{step_path}.steps[{j}]", position=child.position)
                children.append(child)
    out["substeps"] = children
    if "max_concurrency" in raw:
        value = _as_int(ctx, raw["max_concurrency"], path=f"{step_path}.max_concurrency",
                        at=raw.get("max_concurrency"), minimum=1,
                        maximum=MAX_CONCURRENCY_CAP, what="max_concurrency")
        out["max_concurrency"] = value or PARALLEL_DEFAULT_CONCURRENCY
    return out


def _check_condition(ctx: _Ctx, condition: str, *, path: str, at: Any) -> None:
    """校验 branch 条件：必须是 `左值 <算子> 右值` 的受限比较。

    为什么要**在加载期**就查语法：branch 的条件写错 = 跳转走错分支，
    而错的那条分支可能正在改生产配置。这是本实现里最不能"跑了才知道"的一类错误。

    为什么只允许白名单算子、且左右都只是模板或字面量：
    见 template.py —— 一旦支持表达式求值，YAML 就成了代码，评审也就失效了。
    解析规则统一在 conditions.py（执行器用同一份），这里只负责把问题记进 ctx。
    """
    for message, suggestions in _check_condition_text(condition):
        if "条件" not in message:
            message = f"branch 条件 {condition!r} 有误：{message}"
        ctx.error(message, path=path, at=at, suggestions=suggestions)


def collect_issues(
    document: Any,
    *,
    positions: dict[int, Position] | None = None,
    source: str = "",
    source_digest: str = "",
    tool_names: list[str] | None = None,
    unknown_fields: str = UNKNOWN_FIELD_ERROR,
    foreach_max: int | None = None,
    features: set[str] | frozenset[str] | None = None,
    edition_label: str = "",
    edition: str = "",
    min_interval: int | None = None,
) -> tuple[WorkflowSchema | None, list[Issue], list[Issue]]:
    """把已解析成 Python 对象的 YAML 文档校验成 `WorkflowSchema`。

    返回 `(schema | None, errors, warnings)`。schema 为 None 表示存在**错误**级问题
    （warnings 不阻止加载）。

    `tool_names` 只用于"给建议"，不用来拒绝加载 —— 工具注册表是运行时注入的，
    加载器看不到某个 MCP 工具此刻挂没挂上。真正的判定在执行时。

    `foreach_max` / `features` 是版本限额与特性（v1.8.0，R-110）：由 loader 按当前
    版本注入，在**加载期**拦截超出版本能力的写法。
    """
    ctx = _Ctx(positions, foreach_max=foreach_max, features=features,
               edition_label=edition_label, edition=edition, min_interval=min_interval)
    if not isinstance(document, dict):
        ctx.error(f"工作流文件的顶层应当是映射（含 version/name/steps），"
                  f"实际是 {type(document).__name__}", at=document)
        return None, ctx.issues, ctx.warnings

    for key, (feature, since) in RESERVED_TOP_LEVEL.items():
        if key in document:
            ctx.error(_reserved_message(ctx, f"顶层字段 {key!r}", feature, since),
                      path=key, at=document[key] if isinstance(document[key], (dict, list))
                      else document)
    _check_unknown_fields(
        ctx, {k: v for k, v in document.items() if k not in RESERVED_TOP_LEVEL},
        TOP_LEVEL_FIELDS, where="", unknown_fields=unknown_fields)
    for key, feature in FEATURE_GATED_TOP_LEVEL.items():
        if key in document and _feature_missing(ctx, feature):
            ctx.error(_gated_message(ctx, f"顶层字段 {key!r}", feature), path=key,
                      at=document[key] if isinstance(document[key], (dict, list)) else document)

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

    triggers = _check_triggers(ctx, document.get("triggers"), inputs,
                               unknown_fields=unknown_fields)
    on_restart = _check_on_restart(ctx, document, has_triggers=bool(triggers))
    notify = _check_notify(ctx, document.get("notify"), unknown_fields=unknown_fields)

    if ctx.failed:
        return None, ctx.issues, ctx.warnings

    schema = WorkflowSchema(
        version=version, name=name, description=description, inputs=inputs, steps=steps,
        metadata=metadata, source=source, warnings=list(ctx.warnings),
        source_digest=source_digest, triggers=triggers, on_restart=on_restart,
        notify=notify,
    )
    return schema, ctx.issues, ctx.warnings


def _check_triggers(ctx: _Ctx, raw: Any, inputs: dict[str, InputSpec], *,
                    unknown_fields: str) -> list[TriggerSpec]:
    """校验 ``triggers``（v1.9.0，R-201 / R-202）。

    能在加载期判定的都在这里判：cron 写法、定时最小间隔（专业版 300 秒）、
    触发入参是否声明过、定时触发是否给全了必填入参 —— 定时触发**没有人**在
    旁边补参数，缺了就是每天早上 9 点准时失败一次。
    """
    from automind.workflow.cron import CronError, CronExpr, format_interval

    if raw is None:
        return []
    if not isinstance(raw, list):
        ctx.error("triggers 应当是列表，每项形如 {cron: \"0 9 * * 1-5\"} 或 {webhook: true}",
                  path="triggers", at=raw)
        return []
    out: list[TriggerSpec] = []
    seen: dict[str, int] = {}
    for i, item in enumerate(raw):
        path = f"triggers[{i}]"
        if not isinstance(item, dict):
            ctx.error(f"触发器应当是映射（如 {{cron: \"0 9 * * *\"}}），实际是 "
                      f"{type(item).__name__}", path=path, at=item if isinstance(
                          item, (dict, list)) else raw)
            continue
        _check_unknown_fields(ctx, item, TRIGGER_FIELDS, where=path,
                              unknown_fields=unknown_fields)
        position = ctx.pos(item, ctx.pos(raw))
        tid_raw = item.get("id", f"t{i + 1}")
        tid = str(tid_raw).strip() if tid_raw is not None else ""
        if not tid or not all(ch.isalnum() or ch in "-_" for ch in tid) or len(tid) > 40:
            ctx.error(f"触发器 id 只能由字母、数字、- 与 _ 组成（≤40 字符）：{tid_raw!r}",
                      path=f"{path}.id", position=position)
            tid = f"t{i + 1}"
        if tid in seen:
            ctx.error(f"触发器 id 重复：{tid!r}（首次出现在 triggers[{seen[tid]}]）。"
                      "启停状态与 webhook 令牌按 id 记，重复会让两个触发器共用一份状态",
                      path=f"{path}.id", position=position)
        seen[tid] = i

        has_cron = item.get("cron") not in (None, "")
        has_hook = "webhook" in item and item.get("webhook") not in (None, False)
        if has_cron and has_hook:
            ctx.error("一个触发器只能是 cron 或 webhook 之一；两种都要请写成两个触发器",
                      path=path, position=position)
            continue
        if not has_cron and not has_hook:
            ctx.error("触发器缺少 cron（定时）或 webhook: true（入站 webhook）",
                      path=path, position=position)
            continue
        spec = TriggerSpec(id=tid, kind="cron" if has_cron else "webhook", position=position)
        if has_cron:
            text = str(item.get("cron")).strip()
            try:
                expr = CronExpr.parse(text)
            except CronError as exc:
                ctx.error(f"cron 表达式不合法：{exc}", path=f"{path}.cron", position=position)
                continue
            spec.cron = expr.text
            spec.min_interval = expr.min_interval_seconds()
            if ctx.min_interval is not None and spec.min_interval < ctx.min_interval:
                ctx.error(f"定时触发 {text!r} 最短每 {format_interval(spec.min_interval)} 触发一次，"
                          f"低于{ctx.edition_label or '当前版本'}的最小间隔 "
                          f"{format_interval(ctx.min_interval)}（企业版下限 1 分钟）",
                          path=f"{path}.cron", position=position)
        elif item.get("webhook") is not True:
            ctx.error(f"webhook 触发器请写 webhook: true，实际是 {item.get('webhook')!r}"
                      "（令牌由服务端生成，不写进文件 —— 文件会进版本库）",
                      path=f"{path}.webhook", position=position)

        trig_inputs = item.get("inputs")
        if trig_inputs is None:
            trig_inputs = {}
        if not isinstance(trig_inputs, dict):
            ctx.error("触发器的 inputs 应当是映射（入参名 → 值）", path=f"{path}.inputs",
                      position=position)
            trig_inputs = {}
        for key in trig_inputs:
            if str(key) not in inputs:
                ctx.error(f"触发器传入了未声明的入参 {key!r}", path=f"{path}.inputs.{key}",
                          position=position, suggestions=suggest(str(key), list(inputs)))
        missing = [n for n, s in inputs.items()
                   if s.required and s.default is None and n not in trig_inputs]
        # webhook 的入参本来就由调用方在请求体里给（缺了那一次触发返回 422），不提醒
        if missing and spec.kind == "cron":
            ctx.error(f"定时触发没有给出必填入参：{'、'.join(missing)}。定时运行时没有人补参数，"
                      "请在该触发器的 inputs 里写上，或给入参声明 default",
                      path=f"{path}.inputs", position=position)
        spec.inputs = dict(trig_inputs)
        spec.description = str(item.get("description") or "")
        if "enabled" in item:
            spec.enabled = _as_bool(ctx, item["enabled"], path=f"{path}.enabled")
        if "retry" in item:
            value = _as_int(ctx, item["retry"], path=f"{path}.retry", at=item,
                            minimum=0, maximum=TRIGGER_MAX_RETRY, what="retry")
            spec.retry = value or 0
        if "retry_delay" in item:
            value = _as_int(ctx, item["retry_delay"], path=f"{path}.retry_delay", at=item,
                            minimum=TRIGGER_MIN_RETRY_DELAY, maximum=86400,
                            what="retry_delay（秒）")
            spec.retry_delay = float(value or TRIGGER_DEFAULT_RETRY_DELAY)
        out.append(spec)
    return out


def _check_on_restart(ctx: _Ctx, document: dict[str, Any], *, has_triggers: bool) -> str:
    """``on_restart``（D5，R-208）：只作用于触发运行；手动运行一律只提示。"""
    raw = document.get("on_restart")
    if raw is None:
        return "notify"
    value = str(raw).strip().lower() if isinstance(raw, str) else ""
    if value not in ON_RESTART_CHOICES:
        ctx.error(f"on_restart 只能是 {' / '.join(ON_RESTART_CHOICES)}，实际是 {raw!r}",
                  path="on_restart", at=document,
                  suggestions=suggest(str(raw), list(ON_RESTART_CHOICES)))
        return "notify"
    if not has_triggers:
        ctx.warn("on_restart 只对触发器发起的运行生效；这份工作流没有触发器，"
                 "手动运行被重启打断时一律只提示、不自动续跑", path="on_restart", at=document)
    return value


def _check_notify(ctx: _Ctx, raw: Any, *, unknown_fields: str) -> NotifySpec:
    """``notify``（R-209）：``{on: [failed, partial], channels: [webhook, im]}``。"""
    if raw is None:
        return NotifySpec()
    if not isinstance(raw, dict):
        ctx.error("notify 应当是映射，如 {on: [failed, partial], channels: [webhook, im]}",
                  path="notify", at=raw)
        return NotifySpec()
    if True in raw and "on" not in raw:
        # YAML 1.1 把裸写的 `on` 当成布尔 true —— `notify: {on: [failed]}` 解析出来的
        # 键是 True 而不是 "on"。这是 YAML 的历史包袱，不是写错了：按 on 认下
        raw = {("on" if k is True else k): v for k, v in raw.items()}
    _check_unknown_fields(ctx, raw, NOTIFY_FIELDS, where="notify", unknown_fields=unknown_fields)
    spec = NotifySpec(declared=True)
    for key, choices, default in (("on", NOTIFY_EVENTS, NOTIFY_DEFAULT_EVENTS),
                                  ("channels", NOTIFY_CHANNELS, NOTIFY_DEFAULT_CHANNELS)):
        value = raw.get(key)
        if value is None:
            setattr(spec, key, list(default))
            continue
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            ctx.error(f"notify.{key} 应当是列表，可选：{'、'.join(choices)}",
                      path=f"notify.{key}", at=raw)
            continue
        picked: list[str] = []
        for v in value:
            name = v.strip().lower()
            if name not in choices:
                ctx.error(f"notify.{key} 不支持 {v!r}，可选：{'、'.join(choices)}",
                          path=f"notify.{key}", at=raw, suggestions=suggest(v, list(choices)))
            elif name not in picked:
                picked.append(name)
        setattr(spec, key, picked)
    return spec


def _check_step_references(ctx: _Ctx, steps: list[StepSpec],
                           inputs: dict[str, InputSpec]) -> None:
    """检查步骤之间的一致性：id 唯一、跳转目标存在、模板变量有定义。

    这一组是"工作流即代码"最核心的静态检查 —— 它能在**没连任何系统**的情况下
    回答"这份流程跑得起来吗"。ReAct 模式做不到这件事，因为步骤是运行时才生成的。
    """
    # 1) 重复 id —— foreach 的子步骤与顶层步骤共用一个命名空间：
    #    同名会让 `{{ steps.x.output }}` 在循环内外指向不同的东西
    seen: dict[str, int] = {}
    all_ids: dict[str, str] = {}
    for i, step in enumerate(steps):
        if step.id in seen or step.id in all_ids:
            ctx.error(f"步骤 id 重复：{step.id!r}（首次出现在 "
                      f"{all_ids.get(step.id) or f'steps[{seen[step.id]}]'}）。"
                      "id 是模板引用与报告的唯一键，重复会让 `{{ steps.x.output }}` "
                      "指向不确定的一步", path=f"steps[{i}]({step.id}).id", at=None,
                      position=step.position)
        else:
            seen[step.id] = i
            all_ids[step.id] = f"steps[{i}]"
        for j, child in enumerate(step.substeps):
            where = f"steps[{i}]({step.id}).steps[{j}]"
            if child.id in all_ids:
                ctx.error(f"步骤 id 重复：{child.id!r}（首次出现在 {all_ids[child.id]}）。"
                          f"{step.type} 的子步骤与顶层步骤共用同一个 id 命名空间",
                          path=f"{where}({child.id}).id", position=child.position)
            else:
                all_ids[child.id] = where
    # foreach 的子步骤只在循环内可见；parallel 的子步骤组结束后对外可见（v1.9.0）
    child_owner = {c.id: s.id for s in steps if s.type == "foreach" for c in s.substeps}
    group_of = {c.id: s.id for s in steps if s.type == "parallel" for c in s.substeps}

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
        # then 必须排在 else 前面（v1.9.0 起加载期拦截）。区间语义是"then 区间从 then 起、
        # 到 else 之前；else 区间从 else 起、到文件末尾"—— then 排在 else 后面时，判定为真
        # 会把 then 目标连同 else 区间一起标成 skipped，**两条路都不走**而报告一片灰。
        if (step.then in seen and step.else_ in seen and seen[step.then] > i
                and seen[step.else_] > i and seen[step.then] > seen[step.else_]):
            ctx.error(f"branch 的 then（{step.then!r}，第 {seen[step.then] + 1} 步）必须排在 "
                      f"else（{step.else_!r}，第 {seen[step.else_] + 1} 步）前面：then 区间从 then "
                      "到 else 之前、else 区间从 else 到文件末尾。条件写反了的话把两个去向对调，"
                      "或改用相反的算子（== ↔ !=、> ↔ <=）",
                      path=f"steps[{i}]({step.id}).then", position=step.position)

    # 3) 模板变量：inputs 必须声明、env 必须大写、steps 必须存在
    from automind.workflow.template import validate_template

    declared_inputs = set(inputs)
    # 所有步骤 id 一次性收齐（而不是边遍历边攒）：只有拿到**完整**的 id 集合，
    # `validate_template` 才能区分"引用了不存在的步骤"（错误）与
    # "引用了排在自己后面的步骤"（warning）。边攒边查会把后一种误判成前一种 ——
    # 实测踩过：一份完全合法的 `{{ steps.b.output }}`（b 在后面）被报成"不存在"。
    all_step_ids = set(seen) | set(group_of)
    # parallel 子步骤的"先后"按所属组的位置算：组之后的步骤引用它是正常的
    top_order: dict[str, float] = {**{cid: float(seen[gid]) for cid, gid in group_of.items()
                                      if gid in seen}, **{k: float(v) for k, v in seen.items()}}

    from automind.workflow.template import secret_refs

    secrets_gated = [False]

    def _check_secrets(step: StepSpec, field_name: str, text: str, path: str) -> None:
        """凭据（v1.10.0，R-305）只能进 tool 步骤的 args：进了 llm / agent 的提示词就是
        把密钥发给模型服务商，进了 human 的待批内容就是摊在弹窗里给人看，进了 branch
        条件就会被写进报告 —— 三种都是泄露，加载期直接拒绝。"""
        refs = secret_refs(text)
        if not refs:
            return
        position = _arg_position(ctx, step, field_name, "")
        if _feature_missing(ctx, "workflow_governance") and not secrets_gated[0]:
            secrets_gated[0] = True
            ctx.error(_gated_message(ctx, "凭据引用 {{ secrets.* }}", "workflow_governance"),
                      path=path, position=position)
        if not (step.type == "tool" and field_name.startswith("args")):
            ctx.error(f"{{{{ secrets.{refs[0]} }}}} 只能用在 tool 步骤的 args 里："
                      f"放进 {step.type} 步骤的 {field_name} 会让凭据明文进入提示词、"
                      "审批弹窗或报告", path=path, position=position)

    def _check_fields(step: StepSpec, path_prefix: str, *, step_ids: set[str],
                      order: dict[str, float], current: float, allow_loop: bool,
                      own_foreach: str = "", own_group: str = "") -> None:
        for field_name, text in _template_fields(step):
            if not text:
                continue
            path = f"{path_prefix}.{field_name}"
            _check_secrets(step, field_name, text, path)
            for problem in validate_template(text, inputs=declared_inputs,
                                             step_ids=step_ids, order=order,
                                             current_index=current, allow_loop=allow_loop):
                position = _arg_position(ctx, step, field_name, problem.path)
                if problem.kind == "forward_reference":
                    # 前向引用**降级为 warning**：结构合法，只是当前执行路径上
                    # 多半还没跑。为什么不报错：branch 会让"先跳到后面再回来"的
                    # 写法变得难以静态判定（判断可达性等于实现半个 CFG 分析），
                    # 而 v1 的目标是"能拦住确定的错"，不是"能证明所有错"。
                    ctx.warn(f"{problem.message}（该步骤排在第 {int(current) + 1} 步，"
                             "引用的是它后面的步骤）",
                             path=path, position=position)
                    continue
                message = problem.message
                ref = problem.path.split(".")[1] if problem.path.startswith("steps.") else ""
                if problem.kind == "unknown_step" and ref and ref == own_foreach:
                    message = (f"不能在 foreach {ref!r} 的子步骤里引用它自己的输出："
                               "那份输出要等所有项跑完才产生。本项的值请用 "
                               "{{ loop.item }} / {{ loop.index }}")
                elif problem.kind == "unknown_step" and own_group and ref == own_group:
                    message = (f"不能在并行组 {ref!r} 的子步骤里引用它自己的输出："
                               "那份输出要等整组跑完才产生")
                elif problem.kind == "unknown_step" and own_group and group_of.get(ref) == own_group:
                    message = (f"{ref!r} 与本步骤同在并行组 {own_group!r} 里，两者**同时**执行，"
                               "互相引用没有确定的先后。需要用它的结果请把本步骤移到组外"
                               "（排在组后面）")
                elif problem.kind == "unknown_step" and ref in child_owner:
                    message = (f"{ref!r} 是 foreach {child_owner[ref]!r} 的子步骤，"
                               "只在那个 foreach 内部可见；在外面请引用 "
                               f"{{{{ steps.{child_owner[ref]}.output }}}}"
                               "（每一项一条记录的列表）")
                ctx.error(message, path=path, position=position,
                          suggestions=problem.suggestions)

    for i, step in enumerate(steps):
        _check_fields(step, f"steps[{i}]({step.id})", step_ids=all_step_ids,
                      order=dict(top_order), current=float(i), allow_loop=False)
        if not step.substeps:
            continue
        if step.type == "parallel":
            # 组内子步骤能看到：组外的一切（排在组前面的才有值），但看不到兄弟与组自身
            siblings = {c.id for c in step.substeps}
            group_ids = (all_step_ids - siblings) - {step.id}
            for j, child in enumerate(step.substeps):
                _check_fields(child, f"steps[{i}]({step.id}).steps[{j}]({child.id})",
                              step_ids=group_ids, order=dict(top_order), current=float(i),
                              allow_loop=False, own_group=step.id)
            continue
        # 子步骤能看到：顶层步骤（排在 foreach 前面的才有值）+ 同一项里的兄弟子步骤。
        # 兄弟的"先后"用 i + (k+1)/1000 表示：比 foreach 自己靠后、比下一个顶层步骤靠前
        sub_order: dict[str, float] = dict(top_order)
        for k, sibling in enumerate(step.substeps):
            sub_order[sibling.id] = i + (k + 1) / 1000
        sub_ids = (all_step_ids | {c.id for c in step.substeps}) - {step.id}
        for j, child in enumerate(step.substeps):
            _check_fields(child, f"steps[{i}]({step.id}).steps[{j}]({child.id})",
                          step_ids=sub_ids, order=sub_order,
                          current=i + (j + 1) / 1000, allow_loop=True, own_foreach=step.id)


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
    elif step.type == "agent":
        fields.append(("goal", step.goal))
    elif step.type == "foreach":
        if isinstance(step.items, str):
            fields.append(("items", step.items))
        elif isinstance(step.items, list):
            fields.extend(_walk_nested("items", step.items))
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
    if field_name in ("prompt", "system", "condition", "goal"):
        return ctx.pos(getattr(step, field_name), step.position)
    if field_name.startswith("items"):
        return ctx.pos(step.items, step.position)
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
    "AGENT_DEFAULT_MAX_STEPS",
    "AGENT_MAX_STEPS_CAP",
    "AGENT_MODES",
    "APPROVAL_MODES",
    "APPROVER_PREFIXES",
    "COMMON_STEP_FIELDS",
    "CONDITION_OPS",
    "FEATURE_GATED_STEP_TYPES",
    "FEATURE_GATED_TOP_LEVEL",
    "FOREACH_CHILD_TYPES",
    "INPUT_TYPES",
    "MAX_CONCURRENCY_CAP",
    "NOTIFY_CHANNELS",
    "NOTIFY_EVENTS",
    "ON_FAILURE_HINT",
    "ON_RESTART_CHOICES",
    "PARALLEL_CHILD_TYPES",
    "PARALLEL_DEFAULT_CONCURRENCY",
    "RESERVED_STEP_TYPES",
    "RESERVED_TOP_LEVEL",
    "STEP_TYPES",
    "SUPPORTED_VERSIONS",
    "TOP_LEVEL_FIELDS",
    "TRIGGER_FIELDS",
    "UNKNOWN_FIELD_ERROR",
    "UNKNOWN_FIELD_WARNING",
    "FailurePolicy",
    "InputSpec",
    "NotifySpec",
    "StepSpec",
    "TriggerSpec",
    "WorkflowSchema",
    "collect_issues",
]
