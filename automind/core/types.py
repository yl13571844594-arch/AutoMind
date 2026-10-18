"""AutoMind 共享数据类型 — 所有模块使用的 Pydantic 模型定义。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

# ═══════════════════════════════════════════════════════════════
# Enum 定义
# ═══════════════════════════════════════════════════════════════


class Role(str, Enum):
    """消息角色。"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ExecutionMode(str, Enum):
    """Agent 执行模式（底层引擎）。"""

    REACT = "react"
    PLAN_AND_EXECUTE = "plan_and_execute"
    MULTI_AGENT = "multi_agent"


class InteractionMode(str, Enum):
    """用户交互模式（上层体验）。

    - CHAT    对话模式：纯粹的多轮对话，不调用工具、不规划，响应最快。
    - WORK    工作模式：分层规划 + 工具执行 + 符号验证，适合自动化任务。
    - CODING  编程模式：ReAct 思考-行动循环，聚焦读写代码、运行命令与测试。
    """

    CHAT = "chat"
    WORK = "work"
    CODING = "coding"
    MULTI = "multi"  # 多智能体协同
    LOOP = "loop"    # 循环工程：自主"行动-观察-修正"闭环


class PlanStatus(str, Enum):
    """计划整体状态。"""

    DRAFTING = "drafting"
    REVIEWING = "reviewing"
    APPROVED = "approved"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


class GoalStatus(str, Enum):
    """单个目标状态。"""

    PENDING = "pending"
    BLOCKED = "blocked"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    REVERTED = "reverted"


class PermissionTier(str, Enum):
    """工具权限等级。"""

    SAFE = "safe"
    SENSITIVE = "sensitive"
    DANGEROUS = "dangerous"


class PermissionDecision(str, Enum):
    """权限决策结果。"""

    ALLOW = "allow"
    DENY = "deny"
    ASK_USER = "ask_user"


class ToolSource(str, Enum):
    """工具来源。"""

    BUILTIN = "builtin"
    MCP = "mcp"
    SKILL = "skill"
    CUSTOM = "custom"


class EventType(str, Enum):
    """事件类型。"""

    TOOL_START = "tool.start"
    TOOL_END = "tool.end"
    TOOL_ERROR = "tool.error"
    PLAN_CREATED = "plan.created"
    PLAN_UPDATED = "plan.updated"
    GOAL_START = "goal.start"
    GOAL_END = "goal.end"
    GOAL_FAILED = "goal.failed"
    BACKTRACK = "backtrack"
    PERMISSION_REQUEST = "permission.request"
    LLM_REQUEST = "llm.request"
    LLM_RESPONSE = "llm.response"
    CHECKPOINT_SAVED = "checkpoint.saved"
    HUMAN_INPUT_NEEDED = "human.input_needed"


# ═══════════════════════════════════════════════════════════════
# 消息与输入
# ═══════════════════════════════════════════════════════════════


class Message(BaseModel):
    """对话消息。"""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    role: Role
    content: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, str]:
        """转为 LLM API 格式 (role, content)。"""
        return {"role": self.role.value, "content": self.content}


class ToolCall(BaseModel):
    """LLM 返回的工具调用。"""

    id: str
    name: str
    arguments: dict[str, Any]
    #: 参数解析失败/被截断时的说明（v1.7.6）。非空 = 这次调用**不能执行**：
    #: 参数是残缺的，执行只会得到"缺参数"的误导性报错，或把半截内容写进文件。
    arguments_error: str = ""


class ToolResult(BaseModel):
    """工具执行结果。"""

    tool_name: str
    success: bool
    output: Any = None
    error: str | None = None
    exit_code: int | None = None
    duration_ms: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)
    #: 超时被杀（与"命令自己返回非零"是两回事：前者可加大 timeout 重试，
    #: 后者要改命令）。上层据此给出可自愈的指引而不是笼统的失败。
    timed_out: bool = False
    #: 后台/异步执行的任务标识（长耗时命令改走后台通道时非空）
    background_id: str = ""


class LLMResponse(BaseModel):
    """统一 LLM 响应。"""

    text: str
    tool_calls: list[ToolCall] | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = "stop"
    provider: str = ""
    model: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)
    # ── 提示缓存度量（v1.11.10）──
    # 各家 usage 的口径不同，这里先归一化，别让上层去猜：
    #   · DeepSeek：prompt_cache_hit_tokens / prompt_cache_miss_tokens
    #   · OpenAI / GLM(智谱) / Kimi / 通义 / 豆包：prompt_tokens_details.cached_tokens
    #   · Anthropic：cache_read_input_tokens / cache_creation_input_tokens
    #: 本次输入中命中缓存（无需重新计算）的 token 数
    cached_tokens: int = 0
    #: 写入缓存的新 token 数（目前只有 Anthropic 显式返回）
    cache_creation_tokens: int = 0
    #: 本次调用的**总输入** token 数（含命中部分）。OpenAI 兼容口径就是
    #: prompt_tokens；Anthropic 的 prompt_tokens 不含缓存读取，必须补上
    #: read+creation 才是真实输入量 —— 命中率的分母要的是后者。
    input_tokens: int = 0
    #: 是否命中缓存（旧字段，保留兼容：有命中即为 True）
    cache_hit: bool = False
    #: 提供商是否**上报了**缓存口径。False = 压根不报（如 Ollama），
    #: 界面应显示"—"；True 且 cached=0 才是"报了，这次确实没命中"。
    cache_reported: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def total_input_tokens(self) -> int:
        """总输入 token：优先用提供商明确给出的口径，否则退回 prompt。"""
        return self.input_tokens or self.prompt_tokens

    @property
    def cache_savings_pct(self) -> float:
        """缓存命中占比：命中 token / 总输入 token（0~100）。"""
        total_input = self.total_input_tokens
        if total_input <= 0:
            return 0.0
        return round(self.cached_tokens / total_input * 100, 1)


class InputMessage(BaseModel):
    """解析后的用户输入。"""

    raw_text: str
    intent: str = ""
    entities: dict[str, Any] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)
    attached_files: list[str] = Field(default_factory=list)
    images: list[bytes] = Field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
# 规划与目标
# ═══════════════════════════════════════════════════════════════


class Predicate(BaseModel):
    """STRIPS 风格谓词 — 前置/后置条件。"""

    name: str
    arguments: list[str] = Field(default_factory=list)
    negated: bool = False

    def __str__(self) -> str:
        prefix = "¬" if self.negated else ""
        args = ", ".join(self.arguments)
        return f"{prefix}{self.name}({args})"

    def to_datalog(self) -> str:
        """转为 Datalog 事实字符串。"""
        args = ", ".join(self.arguments)
        if self.negated:
            return f"not {self.name}({args})"
        return f"{self.name}({args})"


class Action(BaseModel):
    """一个可执行的动作。"""

    tool_name: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    description: str = ""
    expected_output: str = ""


class Goal(BaseModel):
    """分层规划中的目标节点。"""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    description: str
    preconditions: list[Predicate] = Field(default_factory=list)
    expected_effects: list[Predicate] = Field(default_factory=list)
    resource_deps: list[str] = Field(default_factory=list)
    parent_id: str | None = None
    children: list[Goal] = Field(default_factory=list)
    assigned_action: Action | None = None
    status: GoalStatus = GoalStatus.PENDING
    max_retries: int = 3
    retry_count: int = 0
    verification_result: str | None = None
    error_context: str | None = None

    def all_children(self) -> list[Goal]:
        """递归获取所有子孙节点。"""
        result = list(self.children)
        for child in self.children:
            result.extend(child.all_children())
        return result

    def leaf_goals(self) -> list[Goal]:
        """获取所有叶子目标 (无子节点)。"""
        if not self.children:
            return [self]
        result = []
        for child in self.children:
            result.extend(child.leaf_goals())
        return result


class HierarchicalPlan(BaseModel):
    """分层执行计划。"""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    task_description: str
    root_goal: Goal
    status: PlanStatus = PlanStatus.DRAFTING
    execution_order: list[str] = Field(default_factory=list)
    parallel_groups: list[list[str]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    revision_history: list[str] = Field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
# 记忆
# ═══════════════════════════════════════════════════════════════


class MemoryChunk(BaseModel):
    """记忆块 — 从记忆系统检索到的信息。"""

    source: Literal["short_term", "long_term", "project", "knowledge_graph", "entity"]
    content: str
    relevance_score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class Entity(BaseModel):
    """知识图谱实体。"""

    id: str
    type: str
    name: str
    properties: dict[str, Any] = Field(default_factory=dict)
    source: str = ""


class Relation(BaseModel):
    """知识图谱关系。"""

    source_id: str
    target_id: str
    relation_type: str
    properties: dict[str, Any] = Field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════
# Agent 状态
# ═══════════════════════════════════════════════════════════════


class TokenUsage(BaseModel):
    """Token 消耗统计（v1.11.10 起同时记录提示缓存命中）。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: 命中提示缓存的输入 token 数
    cached_tokens: int = 0
    #: 写入提示缓存的 token 数（Anthropic 专用，其余提供商恒为 0）
    cache_creation_tokens: int = 0
    #: 总输入 token（含命中）。OpenAI 兼容口径 = prompt_tokens；Anthropic 的
    #: prompt_tokens 只含未命中部分，这里才是真实输入量，命中率按它算分母。
    input_tokens: int = 0
    #: LLM 调用次数 —— 命中率之外，还要能说"几次调用里命中几次"
    calls: int = 0
    cache_hit_calls: int = 0
    #: 其中**上报了**缓存口径的调用次数（0 = 该模型不提供缓存统计）
    cache_reported_calls: int = 0
    #: 单次请求输入 token 的**峰值**。上下文占用看的是它；累加的
    #: ``input_tokens`` 是"总花销"，两者语义不同，不能混用。
    peak_prompt_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cache_hit_rate(self) -> float | None:
        """提示缓存命中率（%）；提供商没给缓存用量时返回 None（不是 0）。"""
        if self.input_tokens <= 0 or self.cache_reported_calls <= 0:
            return None
        return round(self.cached_tokens / self.input_tokens * 100, 1)

    def add(self, response: LLMResponse) -> None:
        call_input = response.total_input_tokens
        self.prompt_tokens += response.prompt_tokens
        self.completion_tokens += response.completion_tokens
        self.cached_tokens += response.cached_tokens
        self.cache_creation_tokens += response.cache_creation_tokens
        self.input_tokens += call_input
        self.calls += 1
        if response.cached_tokens > 0:
            self.cache_hit_calls += 1
        if response.cache_reported:
            self.cache_reported_calls += 1
        self.peak_prompt_tokens = max(self.peak_prompt_tokens, call_input)


class AgentState(BaseModel):
    """Agent 完整状态 — 用于检查点。"""

    session_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    messages: list[Message] = Field(default_factory=list)
    plan: HierarchicalPlan | None = None
    task_stack: list[dict[str, Any]] = Field(default_factory=list)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    tool_state: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_updated: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentResult(BaseModel):
    """Agent 任务执行结果。"""

    success: bool
    output: str = ""
    plan: HierarchicalPlan | None = None
    steps_executed: int = 0
    errors_corrected: int = 0
    backtracks: int = 0
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    duration_ms: float = 0.0
    checkpoints: list[str] = Field(default_factory=list)
