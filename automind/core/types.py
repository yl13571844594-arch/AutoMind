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


class ToolResult(BaseModel):
    """工具执行结果。"""

    tool_name: str
    success: bool
    output: Any = None
    error: str | None = None
    exit_code: int | None = None
    duration_ms: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


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
    # 缓存命中度量（DeepSeek / OpenAI 兼容提供商返回 usage 时填充）
    cache_hit: bool = False
    cached_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cache_savings_pct(self) -> float:
        """缓存节省占比：缓存命中 token / 总输入 token。"""
        total_input = self.prompt_tokens + self.cached_tokens
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
    """Token 消耗统计。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, response: LLMResponse) -> None:
        self.prompt_tokens += response.prompt_tokens
        self.completion_tokens += response.completion_tokens


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
