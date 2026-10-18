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


class LLMConnectionError(LLMError):
    """网络/中转层不可达，或服务端暂时不可用（5xx）—— 典型瞬时错误。

    v1.7.6 新增。此前 SDK 抛出的 ``APIConnectionError``（``str()`` 恰好是
    ``"Connection error."``）不命中任何重试关键字，被原样上抛，长任务跑到
    一半遇到一次中转抖动就整轮失败。单独成类，上层才能据此"等一等再接着做"。
    """


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


# ── 资源异常 ──────────────────────────────────────────────


class ResourceError(AutoMindError):
    """资源管理异常基类（Token 预算 / 速率 / 超时）。"""


class BudgetExhaustedError(ResourceError):
    """Token 预算耗尽。

    **这是可恢复的**：调用方应当就地收尾并把已完成的工作交出去，
    而不是让它一路上浮成 HTTP 500。此前预算耗尽抛的是裸 ``RuntimeError``，
    ReAct 循环没有任何地方接得住 —— 长任务因此在跑到一半时丢掉全部成果
    （实测：默认 81,920 预算下，每步 2.7k token 的任务在第 22~31 步被掐死）。

    之所以专门做一个类型而不是继续用 ``RuntimeError("Token budget exhausted")``：
    判据靠子串匹配时，任何措辞改动都会让捕获静默失效，而失效的表现恰恰是
    "任务又被掐死了" —— 这类错误最难在测试里发现。
    """


# ── 状态异常 ──────────────────────────────────────────────


class StateError(AutoMindError):
    """状态管理异常基类。"""


class CheckpointNotFoundError(StateError):
    """检查点不存在。"""


class CheckpointCorruptedError(StateError):
    """检查点数据损坏。"""
