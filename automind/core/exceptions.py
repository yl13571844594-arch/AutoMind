"""AutoMind 异常层次结构。"""

from __future__ import annotations


class AutoMindError(Exception):
    """所有 AutoMind 异常的基类。"""

    def __init__(self, message: str = "", *, code: str = "", details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


# ── LLM 异常 ──────────────────────────────────────────────


class LLMError(AutoMindError):
    """LLM 调用相关异常基类。"""


class LLMAuthenticationError(LLMError):
    """API Key 无效或未配置。"""


class LLMRateLimitError(LLMError):
    """速率限制。"""


class LLMTimeoutError(LLMError):
    """请求超时。"""


class LLMContextTooLargeError(LLMError):
    """上下文超出模型限制。"""


class LLMProviderNotFoundError(LLMError):
    """未知的 LLM 提供商。"""


# ── 工具异常 ──────────────────────────────────────────────


class ToolError(AutoMindError):
    """工具执行相关异常基类。"""


class ToolNotFoundError(ToolError):
    """工具未注册。"""


class ToolExecutionError(ToolError):
    """工具执行失败。"""


class ToolTimeoutError(ToolError):
    """工具执行超时。"""


class PermissionDeniedError(ToolError):
    """权限校验未通过。"""


class SandboxError(ToolError):
    """沙箱执行异常。"""


# ── 规划异常 ──────────────────────────────────────────────


class PlanError(AutoMindError):
    """规划相关异常基类。"""


class PlanGenerationError(PlanError):
    """计划生成失败。"""


class PlanExecutionError(PlanError):
    """计划执行失败。"""


class PreconditionViolationError(PlanError):
    """前置条件不满足。"""


class PostconditionViolationError(PlanError):
    """后置条件未达成。"""


class BacktrackLimitError(PlanError):
    """回溯次数超出上限。"""


# ── 记忆异常 ──────────────────────────────────────────────


class MemoryError(AutoMindError):
    """记忆系统异常基类。"""


class EmbeddingError(MemoryError):
    """向量嵌入失败。"""


# ── 输入/上下文异常 ───────────────────────────────────────


class ContextError(AutoMindError):
    """上下文相关异常基类。"""


class InputParseError(ContextError):
    """输入解析失败。"""


# ── 状态异常 ──────────────────────────────────────────────


class StateError(AutoMindError):
    """状态管理异常基类。"""


class CheckpointNotFoundError(StateError):
    """检查点不存在。"""


class CheckpointCorruptedError(StateError):
    """检查点数据损坏。"""
