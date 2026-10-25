"""确定性工作流执行器。

这是"工作流即代码"的落地处：**步骤怎么走，由文件决定，不由模型决定。**

与 ReAct / Plan-and-Execute 的分工
---------------------------------
既有两条路径都是"运行时决定步骤"：ReAct 每轮让模型挑一个动作，Plan 让模型
先分解再执行。它们的价值是**面对没写过的任务**（"帮我看看这个仓库为什么慢"），
代价是不可评审、不可复现 —— 同一句输入两次跑出的步骤可能不同。

本执行器反过来：**没有任何规划调用**。`llm` 类型的步骤只做一次文本生成，
不参与"下一步做什么"的决策；`branch` 的条件是文件里写死的受限比较。
因此同一份文件 + 同一份入参 = 同一条执行路径，报告可以逐条对照客户批过的那版。

三条硬边界
---------
1. **失败不许记成成功。** `on_failure: continue` 只是"不中断后续步骤"，
   该步骤在报告里依然是 `failed`；整轮状态是 `partial`（部分成功），
   不是 `ok`。把失败美化成成功，等于让 CI 和客户都失去判据。
2. **人不在场就是拒绝。** `human` 步骤没有审批回调时 **fail-closed**：
   按拒绝处理并如实记录，绝不默认放行。理由见 `_run_human`。
3. **取消必须一路向上。** `asyncio.CancelledError` 只做记录、**不吞**，
   照常往外抛（`CancelledError` 继承自 `BaseException`，`except Exception`
   本来就拦不住它 —— 这里刻意保持这个性质，父 agent 的"停止"才停得住）。
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from automind.core.logging import get_logger
from automind.core.types import ToolResult
from automind.state.human_loop import ApprovalOutcome
from automind.workflow.conditions import LIST_OPS, NUMERIC_OPS, as_number, parse_literal_list
from automind.workflow.conditions import split_condition as _parse_condition
from automind.workflow.exceptions import Position, TemplateError, WorkflowExecutionError
from automind.workflow.schema import StepSpec, WorkflowSchema
from automind.workflow.template import find_placeholders, render, render_structure, secret_refs

logger = get_logger("automind.workflow.executor")

# ═══════════════════════════════════════════════════════════════
# 报告结构
# ═══════════════════════════════════════════════════════════════

#: 单步状态。`started` 只会短暂出现（在 `on_step_start` 回调里），
#: 落进最终报告的只会是后五种。
STATUS_STARTED = "started"
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_ABORTED = "aborted"
STATUS_CANCELLED = "cancelled"

#: 整轮状态。刻意分出 `partial`：有步骤失败但整轮跑完了，
#: 这和"全绿"是两件事，合并成一个 `ok` 会让 CI 门禁形同虚设。
RUN_OK = "ok"
RUN_PARTIAL = "partial"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
RUN_DRY_RUN = "dry_run"
RUN_ABORTED = "aborted"

#: 退出码约定（CLI 与服务端共用同一套判据，避免"CLI 红了但接口绿了"）
EXIT_OK = 0
EXIT_STEP_FAILED = 1
EXIT_BAD_WORKFLOW = 2
EXIT_CANCELLED = 130

#: 在 `_attempts` 里按 retry(n) 重试的类型（tool 在 `_run_tool` 内部自己重试）
_RETRY_IN_STEP = frozenset({"llm", "agent"})
#: 支持 retry 的全部类型；其余类型写 retry 时"失败即用尽"
_RETRYABLE_TYPES = frozenset({"tool", "llm", "agent"})

#: agent 步骤视为"完成"的结束原因：模型自己判断做完了、不再调工具
_AGENT_DONE = frozenset({"no_more_tools"})
_AGENT_STOP_ZH = {
    "max_iterations": "达到 max_steps 上限",
    "task_budget": "token 预算或时长预算用尽",
    "no_progress": "反复做同一件事、没有进展",
    "empty_response": "模型连续返回空内容",
    "llm_unavailable": "模型连接不可用",
    "error": "执行出错",
    "cancelled": "被取消",
}

#: foreach 上限"按当前版本自动取"的哨兵
_AUTO_LIMIT: Any = object()

#: foreach 逐项执行时的"本项上下文"（v1.9.0）。
#:
#: 为什么用 ContextVar：逐项**并发**时，每一项都要有自己的 ``loop`` 与自己的子步骤
#: 输出 —— 共用一份上下文，第 3 项的子步骤会读到第 7 项刚写进去的输出（串项），
#: 而报告上看不出任何异常。ContextVar 的值随 asyncio 任务隔离：每一项在自己的
#: 任务里 ``set`` 一份浅拷贝，执行器里所有读写 ``self._context`` 的地方（渲染、
#: 记录输出）就自动落到本项的那份上，不必把上下文一路当参数传下去。
#: 值是 ``(执行器, 上下文)``：同一进程里可能有多个执行器（嵌套工作流），只认自己的。
_ITEM_CONTEXT: contextvars.ContextVar[tuple[Any, dict[str, Any]] | None] = \
    contextvars.ContextVar("automind_workflow_item_context", default=None)

#: 一组任务的执行器：``await runner(jobs, max_concurrency=n, stop=event) -> list``。
#: ``jobs`` 是零参协程工厂；``stop`` 被置位后**未启动**的任务不再启动（结果为 None），
#: 已启动的跑完为止（中途掐断一个工具调用，比让它跑完更难对账）。
GroupRunner = Callable[..., Awaitable[list[Any]]]


async def run_group_sequential(jobs: list[Callable[[], Awaitable[Any]]], *,
                               max_concurrency: int = 1,  # noqa: ARG001
                               stop: asyncio.Event | None = None) -> list[Any]:
    """逐个执行（社区核心的兜底实现）。

    并发执行器是专业版特性 ``workflow_parallel`` 的实现（automind_pro）；没有它时
    parallel / 逐项并发在**加载期**就被拦下了。这里的兜底只服务于"绕过加载器、
    程序化构造 schema"的调用方：语义完全一致，只是不并发。
    """
    out: list[Any] = []
    for job in jobs:
        if stop is not None and stop.is_set():
            out.append(None)
            continue
        out.append(await job())
    return out


def _default_group_runner() -> GroupRunner:
    try:
        from automind.core import edition

        feature = edition.get_feature("workflow_parallel")
        runner = getattr(feature, "run_group", None)
        if callable(runner):
            return runner
    except Exception:                                  # pragma: no cover - 防御性
        pass
    return run_group_sequential


def _resolve_foreach_max(value: Any) -> int | None:
    """foreach 单次迭代上限：显式给值就用它，否则按当前版本限额（None = 不限）。"""
    if value is not _AUTO_LIMIT:
        return value
    try:
        from automind.core import quota

        return quota.workflow_limit("workflow_foreach_max")
    except Exception:                                  # pragma: no cover - 防御性
        return None


@dataclass
class StepReport:
    """单步执行结果（报告的一行）。"""

    id: str
    type: str
    status: str = STATUS_SKIPPED
    started_at: str = ""
    finished_at: str = ""
    duration_ms: float = 0.0
    #: 步骤输出的摘要（sha256 前 16 位）。为什么不直接放全文：
    #: 工具输出可能有几十万字（网页、日志），报告要能进日志、进前端、进 CI 产物。
    #: 全文通过 `WorkflowRun.outputs` 单独取，两者按 digest 对得上。
    output_digest: str = ""
    error: str = ""
    #: 重试次数（0 = 一次成功）；`attempts = retries + 1`
    retries: int = 0
    #: 执行这一行时的行号 —— 报告能直接指回文件
    line: int = 0
    #: 本步骤消耗的 token（v1.11.16）：{"prompt": n, "completion": n, "total": n}。
    #: 此前只把 agent 的 token 塞在 detail 里、整轮没有汇总，看板因此永远显示 0。
    tokens: dict[str, int] = field(default_factory=dict)
    #: 补充信息（工具名、跳转去向、审批结果等），便于审计与排障
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data = {
            "id": self.id, "type": self.type, "status": self.status,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "duration_ms": round(self.duration_ms, 1), "output_digest": self.output_digest,
            "error": self.error, "retries": self.retries, "line": self.line,
            "detail": jsonable(self.detail),
        }
        if self.tokens:
            data["tokens"] = dict(self.tokens)
        return data


@dataclass
class WorkflowRun:
    """整轮执行报告。"""

    workflow: str = ""
    version: int = 0
    run_id: str = ""
    status: str = RUN_OK
    started_at: str = ""
    finished_at: str = ""
    duration_ms: float = 0.0
    steps: list[StepReport] = field(default_factory=list)
    inputs: dict[str, Any] = field(default_factory=dict)
    #: 整轮层面的失败原因（如"某步 abort 导致后续未执行"）
    error: str = ""
    #: 源文件摘要 —— 报告与文件版本对得上的凭据
    source_digest: str = ""
    source: str = ""
    dry_run: bool = False
    #: 加载期的 warning 原文，随报告一起交出去（不许只在加载时闪一下）
    warnings: list[str] = field(default_factory=list)
    #: 步骤成功的真实输出（不进 JSON 报告的默认输出，按需取）
    outputs: dict[str, Any] = field(default_factory=dict)
    #: 断点续跑（v1.8.0，R-104）：本轮接的是哪一次运行、哪些步骤直接沿用了上次的结果
    resumed_from: str = ""
    resumed_steps: list[str] = field(default_factory=list)
    #: 整轮 token 汇总（v1.11.16）：{"prompt": n, "completion": n, "total": n}。
    #: 看板（pro 的 workflow_history）读的就是它；此前 executor 从不写这个字段，
    #: 导致"平均 token"永远是 0 —— 这不是没数据，是数据没汇总上来。
    tokens: dict[str, int] = field(
        default_factory=lambda: {"prompt": 0, "completion": 0, "total": 0})

    # ── 统计 ──────────────────────────────────────────

    @property
    def succeeded(self) -> int:
        return sum(1 for s in self.steps if s.status == STATUS_OK)

    @property
    def failed(self) -> int:
        return sum(1 for s in self.steps if s.status in (STATUS_FAILED, STATUS_ABORTED))

    @property
    def skipped(self) -> int:
        return sum(1 for s in self.steps if s.status == STATUS_SKIPPED)

    @property
    def cancelled(self) -> int:
        return sum(1 for s in self.steps if s.status == STATUS_CANCELLED)

    @property
    def total(self) -> int:
        return len(self.steps)

    @property
    def ok(self) -> bool:
        """整轮是否"全绿"。

        判据里带上 `failed == 0` 而不只看 status：状态字段将来可能被
        扩展（比如加入重试计数），而"有没有失败的步骤"永远是硬判据。
        宁可多判一次，也不要出现"状态是 ok 但报告里有红行"。
        """
        return self.failed == 0 and self.status in (RUN_OK, RUN_DRY_RUN)

    @property
    def exit_code(self) -> int:
        """CLI / CI 用的退出码。与服务端共用同一套判据。"""
        if self.status == RUN_CANCELLED:
            return EXIT_CANCELLED
        if self.status == RUN_FAILED or self.status == RUN_ABORTED:
            return EXIT_STEP_FAILED
        if self.failed:
            # 部分成功也要拦住 CI：客户批的流程里有一红行，就不该发布
            return EXIT_STEP_FAILED
        return EXIT_OK

    def as_dict(self, *, include_outputs: bool = False) -> dict[str, Any]:
        """转成可直接 `json.dumps` 的字典 —— 前端与 CI 都吃这个。"""
        data: dict[str, Any] = {
            "workflow": self.workflow,
            "version": self.version,
            "run_id": self.run_id,
            "status": self.status,
            "dry_run": self.dry_run,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": round(self.duration_ms, 1),
            "source": self.source,
            "source_digest": self.source_digest,
            "inputs": jsonable(self.inputs),
            "error": self.error,
            "warnings": list(self.warnings),
            "summary": {
                "total": self.total, "succeeded": self.succeeded,
                "failed": self.failed, "skipped": self.skipped,
                "cancelled": self.cancelled, "ok": self.ok,
                "exit_code": self.exit_code,
            },
            "tokens": dict(self.tokens),
            "steps": [s.as_dict() for s in self.steps],
        }
        if self.resumed_from:
            data["resumed_from"] = self.resumed_from
            data["resumed_steps"] = list(self.resumed_steps)
        if include_outputs:
            data["outputs"] = jsonable(self.outputs)
        return data

    def to_json(self, *, include_outputs: bool = False, indent: int | None = 2) -> str:
        return json.dumps(self.as_dict(include_outputs=include_outputs),
                          ensure_ascii=False, indent=indent, default=str)

    def summary_line(self) -> str:
        tokens = int(self.tokens.get("total") or 0)
        return (f"工作流「{self.workflow}」{_status_zh(self.status)}："
                f"共 {self.total} 步，成功 {self.succeeded}、失败 {self.failed}、"
                f"跳过 {self.skipped}"
                + (f"、取消 {self.cancelled}" if self.cancelled else "")
                + (f"（其中 {len(self.resumed_steps)} 步沿用上次结果）"
                   if self.resumed_steps else "")
                + f"，耗时 {self.duration_ms / 1000:.2f}s"
                + (f"，消耗 {tokens} tokens" if tokens else ""))


def _status_zh(status: str) -> str:
    return {
        RUN_OK: "执行成功", RUN_PARTIAL: "部分成功", RUN_FAILED: "执行失败",
        RUN_CANCELLED: "已取消", RUN_DRY_RUN: "试运行（未真正执行）",
        RUN_ABORTED: "已中止",
    }.get(status, status)


#: 报告里替换凭据明文用的记号（v1.10.0，R-305）
REDACTED = "***"
#: 短于这个长度的凭据值不做替换：把报告里每个 "ab" 都换成 *** 只会让报告不可读，
#: 而这么短的值本来也不该被当成凭据（存储端同样拒绝）
MIN_REDACT_LEN = 4


class _DryRunSecrets(dict):
    """试运行时的凭据占位：任何名字都取得到，渲染成一句"这里是哪个凭据"。"""

    __slots__ = ()

    def __missing__(self, key: str) -> str:
        return f"⟨凭据：{key}⟩"


class _DryRunPlaceholder(dict):
    """dry-run 时"尚未产生的步骤输出"的占位。

    为什么不能简单用空字符串：后面的步骤常写 `{{ steps.a.output.field }}`，
    而空字符串上取不到 `.field` —— 试运行会在一个**本来完全合法**的流程上报错，
    把"渲染没问题"这个真正要验的事淹掉（实测踩过）。

    这个占位同时满足两件事：
      · 当映射用 —— 任意键都取得到（返回另一个占位），嵌套取多少层都不炸；
      · 拼进字符串时渲染成一句人话，评审一眼能看出"这里是真跑时才有的值"。
    它只在 dry-run 的上下文里存在，永远不会出现在真实执行的报告里。
    """

    __slots__ = ()

    def __missing__(self, key: str) -> _DryRunPlaceholder:
        return self

    def __str__(self) -> str:
        return "⟨试运行占位：真跑时才有值⟩"

    def __repr__(self) -> str:                         # 报告里也是同样一句
        return "⟨试运行占位⟩"


def _with_defaults(schema: WorkflowSchema, provided: dict[str, Any]) -> dict[str, Any]:
    """给未提供的入参补上声明里的默认值。

    只补默认值、不做完整性校验：缺必填项是 `check_inputs` 的职责，
    由调用方据此决定退出码（CLI 是 2）。这里只保证一件事 ——
    **声明了 default 的入参一定有值**，否则 `{{ inputs.x }}` 会在
    明明写了默认值的情况下报"取不到键"。
    """
    out: dict[str, Any] = {}
    for name, spec in schema.inputs.items():
        if name in provided and provided[name] is not None:
            out[name] = provided[name]
        elif spec.default is not None:
            out[name] = spec.default
        elif name in provided:
            out[name] = provided[name]
    return out


def jsonable(value: Any) -> Any:
    """把任意值转成 JSON 可序列化的形态（不可序列化的退回 str）。

    覆写了 `__str__` 的 dict 子类按**不透明值**处理（走 str）而不是展开成对象：
    这类对象（dry-run 的 `_DryRunPlaceholder`）的意义全在它的字符串表示上，
    展开成 `{}` 会让"这里还没有值"变成"这里的值是空对象"，是两种完全不同的含义。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict) and type(value).__str__ is dict.__str__:
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def digest_of(value: Any) -> str:
    """算输出的稳定摘要。

    为什么用"规范化 JSON 再 sha256"而不是直接 hash(str(value))：
    dict 的键序在 Python 3.7+ 是插入序，同一份内容由不同代码路径产生时
    顺序可能不同，直接 hash 会得出不同的摘要 —— 而摘要的用途正是
    "两次运行是不是同一个结果"，不稳定就等于没用。
    """
    payload = json.dumps(jsonable(value), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def to_text(value: Any) -> str:
    """把步骤输出转成"拼进提示词"的文本。

    dict/list 走 JSON，其余走 str：提示词里要的是**能被模型读懂的结构**，
    而不是 Python 的 repr（单引号、True/None 这些写法会让模型误解成别的语义）。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(jsonable(value), ensure_ascii=False, indent=2, default=str)
    return str(value)


# ═══════════════════════════════════════════════════════════════
# 执行器
# ═══════════════════════════════════════════════════════════════


class WorkflowExecutor:
    """按文件顺序执行一份校验通过的工作流。

    依赖全部**注入**，执行器自己不 new 任何东西：

    * ``registry``      —— `automind.tools.base.ToolRegistry`，`tool` 步骤走它 `dispatch`
    * ``llm``           —— 需要 `async generate(messages) -> LLMResponse|str`，`llm` 步骤用
    * ``approval``      —— `async (step, rendered_prompt) -> bool|dict`，`human` 步骤用
    * ``agent_runner``  —— `async (step, rendered_goal) -> dict`，`agent` 步骤用
      （返回 D6 结构，见 `automind/workflow/agent_step.py`）
    * ``env``           —— 模板 `{{ env.NAME }}` 的取值来源
    * ``resume``        —— 断点续跑：上一次运行里**已成功**的步骤及其输出，
      这些步骤不再执行、直接沿用（branch 除外：它没有副作用，重新判定更可靠）
    * ``on_checkpoint`` —— 每个顶层步骤结束后收到一份可落盘的快照

    为什么要注入而不是内部构造：执行器要能在**没连任何真实系统**的情况下被
    测试与 dry-run（见 tests/workflow），也要能复用 agent 已经建好的注册表
    （工具是同一批、权限也走同一条链路）。
    """

    def __init__(
        self,
        schema: WorkflowSchema,
        *,
        inputs: dict[str, Any] | None = None,
        registry: Any = None,
        llm: Any = None,
        approval: Callable[..., Awaitable[Any]] | None = None,
        env: dict[str, str] | None = None,
        dry_run: bool = False,
        strict_templates: bool = True,
        on_event: Callable[[str, dict[str, Any]], Any] | None = None,
        agent_runner: Callable[..., Awaitable[Any]] | None = None,
        run_id: str = "",
        resume: dict[str, Any] | None = None,
        on_checkpoint: Callable[[dict[str, Any]], Any] | None = None,
        foreach_max: Any = _AUTO_LIMIT,
        group_runner: GroupRunner | None = None,
        secrets: dict[str, str] | None = None,
    ) -> None:
        self.schema = schema
        # 入参在这里就补全默认值，而不是指望调用方先调 check_inputs：
        # 执行器是这个包唯一的执行入口，CLI / 服务端 / 测试都会经过它。
        # 把"补默认值"留在外面，任何一个漏调的调用方都会让 `{{ inputs.x }}`
        # 在明明写了 default 的情况下报"取不到键"—— 实测就是这么翻车的。
        # 缺必填项不在这里拦（那是 check_inputs 的职责，调用方据此给退出码），
        # 这里只保证"声明了默认值的入参一定有值"。
        self.inputs = _with_defaults(schema, inputs or {})
        self.registry = registry
        self.llm = llm
        self.approval = approval
        self.env = dict(env) if env is not None else _os_env()
        self.dry_run = bool(dry_run)
        #: 严格模板是本实现的默认且推荐行为；留这个开关是为了让上层在
        #: "探测式"场景（如只想知道某段模板能不能渲染）能显式放行，
        #: 但**默认必须严格**：空串渲染会把错误推到远端系统上。
        self.strict_templates = bool(strict_templates)
        self.on_event = on_event
        self.agent_runner = agent_runner
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.on_checkpoint = on_checkpoint
        self.foreach_max = _resolve_foreach_max(foreach_max)
        #: parallel 组 / foreach 逐项并发的执行器（v1.9.0）；缺省取专业版特性，
        #: 没有则逐个执行（见 run_group_sequential）
        self.group_runner: GroupRunner = group_runner or _default_group_runner()
        #: 凭据（v1.10.0，企业版）：名 → 明文。只在渲染 tool 参数时取用；
        #: 报告、事件、落盘快照里出现的明文一律替换成 ***（见 ``_redact``）
        self._secrets: dict[str, str] = {str(k): str(v) for k, v in (secrets or {}).items()}
        self._redact_values: list[str] = sorted(
            {v for v in self._secrets.values() if len(v) >= MIN_REDACT_LEN},
            key=len, reverse=True)
        #: 可沿用的步骤：id → {"output": ..., "output_digest": ...}
        resume = resume or {}
        self._resume_from = str(resume.get("from_run") or "")
        self._resume_steps: dict[str, dict[str, Any]] = {
            str(k): v for k, v in (resume.get("steps") or {}).items()
            if isinstance(v, dict)}
        self._resumed: list[str] = []

        self._visits: dict[str, int] = {}
        self._records: dict[str, StepReport] = {}
        self._base_context: dict[str, Any] = {}
        self._current_id: str = ""
        self._cancelled = False
        #: 整轮 token 汇总（v1.11.16）：每次 llm / agent 调用后累加，收尾时写进报告
        self._tokens: dict[str, int] = {"prompt": 0, "completion": 0, "total": 0}
        #: 快照序号：并行组里多个子步骤几乎同时落盘时，旧快照不能覆盖新快照
        self._snapshot_seq = 0

    @property
    def _context(self) -> dict[str, Any]:
        """模板上下文：foreach 逐项执行时是本项的那份，否则是整轮的那份。"""
        hit = _ITEM_CONTEXT.get()
        if hit is not None and hit[0] is self:
            return hit[1]
        return self._base_context

    @_context.setter
    def _context(self, value: dict[str, Any]) -> None:
        self._base_context = value

    # ── 对外入口 ──────────────────────────────────────

    async def run(self) -> WorkflowRun:
        """执行整个工作流，返回报告。

        取消语义：遇到 `asyncio.CancelledError` 时记录当前步骤为 `cancelled`、
        整轮标 `cancelled`，然后**原样抛出** —— 取消状态会留在报告里
        （`run.status` 与那一步的 `status`），但异常继续向上走，
        父 agent 的"停止"依赖这个传播。
        """
        run = WorkflowRun(
            workflow=self.schema.name, version=self.schema.version,
            run_id=self.run_id, started_at=_now(), dry_run=self.dry_run,
            inputs=self.inputs, source=self.schema.source,
            source_digest=self.schema.source_digest,
            warnings=[w.message for w in self.schema.warnings],
            resumed_from=self._resume_from,
        )
        self._run = run
        started = time.perf_counter()
        self._context = self._build_context()
        finished = False
        #: 整轮硬超时（v1.11.16）：0 = 不限。步骤级 timeout 管"单步最多多久"，
        #: 这里兜的是"整份流程无论卡在第几步都必须停下"—— 重试、循环展开、并行组
        #: 都可能把总时长拖得比任何单步都长，只有整轮上限能保证无人值守任务不挂死。
        run_timeout = float(getattr(self.schema, "timeout_seconds", 0.0) or 0.0)

        try:
            if run_timeout > 0:
                async with asyncio.timeout(run_timeout):
                    await self._execute_steps(run)
            else:
                await self._execute_steps(run)
        except asyncio.CancelledError:
            self._cancelled = True
            run.status = RUN_CANCELLED
            run.error = f"工作流在第 {self._step_no()} 步（{self._current_id}）被取消"
            run.finished_at = _now()
            run.duration_ms = (time.perf_counter() - started) * 1000
            self._finish_records(run)
            finished = True
            # 取消照常传播：吞掉它会让上层的 asyncio 语义失效（"停止"变"卡住"）。
            # 注意：正因为要传播，这里**不能**再 await 任何东西（包括发
            # run_end 事件）—— 在已取消的任务里 await 会立刻再抛一次，
            # 把"记录取消"这件事本身也一起丢掉。
            raise
        except TimeoutError:
            # 整轮超时（v1.11.16）：与"被父任务取消"区分开 —— 这是我们自己
            # 限时到了。状态记 aborted（不是 cancelled）：流程是被中止的，
            # 不是有人叫停的，报告与通知据此给出不同的处置建议。
            run.status = RUN_ABORTED
            run.error = (f"工作流超时：超过整轮上限 {run_timeout:g}s 未完成，已中止。"
                         "可调大 timeout_seconds，或给慢步骤单独设 timeout / "
                         "把长任务拆成多次运行")
            current = self._records.get(self._current_id)
            if current is not None and current.status in (STATUS_STARTED, STATUS_CANCELLED):
                current.status = STATUS_ABORTED
                current.error = run.error
            self._mark_remaining(run, aborted=True)
            logger.warning("workflow_run_timeout", workflow=self.schema.name,
                           timeout=run_timeout)
        except Exception as exc:                     # 执行器自身的 bug / 未预期异常
            run.status = RUN_FAILED
            run.error = f"执行器内部错误（{type(exc).__name__}）：{exc}"
            logger.error("workflow_executor_error", workflow=self.schema.name,
                         error=f"{type(exc).__name__}: {exc}")
        finally:
            if not finished and not run.finished_at:
                run.finished_at = _now()
                run.duration_ms = (time.perf_counter() - started) * 1000
                self._finish_records(run)

        await self._emit("run_end", {"status": run.status, "run_id": run.run_id,
                                     "duration_ms": round(run.duration_ms, 1)})
        return run

    # ── 主循环 ────────────────────────────────────────

    async def _execute_steps(self, run: WorkflowRun) -> None:
        """顺序推进指针；branch 只会**向前**跳（循环在加载期已被拒绝）。"""
        steps = self.schema.steps
        index = 0
        aborted = False

        while index < len(steps):
            step = steps[index]
            self._current_id = step.id

            # 已被判定为"不在这条执行路径上"的步骤（例如 branch 没选中的那一侧），
            # 主循环走到它时必须**跳过**而不是执行。
            #
            # 这一条是 branch 语义的另一半：`_run_branch` 负责标出没走的那条路，
            # 这里负责不跑它。少了这一半，`branch(then=good, else=bad)` 会先跑
            # good、紧接着又跑 bad —— 两条互斥分支全被执行，而报告全是绿的
            # （实测踩过：这是本模块最危险的一类错，因为表面上"跑通了"）。
            if step.id in self._records:
                index += 1
                continue

            # 断点续跑（R-104）：上次已成功的步骤直接沿用结果，**不再执行**。
            # branch 例外 —— 它没有副作用，而且它的判定依赖前面步骤的输出；
            # 重新判定一次与"沿用上次的去向"结果相同，但少一处状态要对账。
            if (step.id in self._resume_steps and step.type != "branch"
                    and not self.dry_run):
                await self._restore_step(step)
                index += 1
                continue

            # 同一份文件 + 同一条路径 = 同一条执行序列。若某步被走到第二次，
            # 说明出现了加载期没拦住的循环（例如将来放宽了跳转规则），
            # 此时按"中止"处理：继续跑会变成死循环，而报告里只会看到一堆重复行。
            self._visits[step.id] = self._visits.get(step.id, 0) + 1
            if self._visits[step.id] > 1:
                record = StepReport(id=step.id, type=step.type, status=STATUS_ABORTED,
                                    error="该步骤被重复执行（工作流出现循环），已中止",
                                    line=step.position.line)
                self._records[step.id] = record
                run.error = f"检测到循环：步骤 {step.id} 被要求执行第二次，已中止整轮"
                run.status = RUN_ABORTED
                await self._emit_step_end(record)
                return

            await self._emit_step_start(step)
            record, jump = await self._run_step(step)

            self._records[step.id] = record
            await self._emit_step_end(record)
            await self._checkpoint()

            # 失败降级分支（v1.11.16）：on_failure: fallback(<步骤id>)。
            # 与 branch 的区别：branch 是"条件选路"，这里是"失败才走的路"；
            # 跳转区间内的步骤**不执行**（它们多依赖失败步骤的输出），
            # 在报告里标 skipped 并写明原因。当前步骤仍记 failed ——
            # 降级路径跑通 ≠ 原来的失败没发生，整轮至少是 partial。
            if record.status == STATUS_FAILED and step.on_failure.kind == "fallback":
                target = step.on_failure.fallback
                resolved = self.schema.index_of(target)
                if resolved < 0 or resolved <= index:
                    record.status = STATUS_ABORTED
                    record.error = (f"on_failure: fallback 目标 {target!r} 无法到达"
                                    "（不存在或不在当前步骤之后），控制流无法继续，已中止整轮")
                    aborted = True
                    break
                record.detail["fallback"] = target
                record.detail.setdefault("on_failure", step.on_failure.raw)
                self._skip_range(
                    index + 1, resolved,
                    f"前序步骤 {step.id} 失败，已按 on_failure: fallback 跳转到 {target}，"
                    "本步骤未执行")
                await self._emit("fallback_taken", {
                    "step": step.id, "target": target, "run_id": run.run_id})
                logger.warning("workflow_fallback_taken", workflow=self.schema.name,
                               step=step.id, target=target, error=record.error)
                index = resolved
                continue

            if record.status == STATUS_ABORTED and not jump:
                aborted = True
                break
            index = jump if jump is not None else index + 1

        # 收尾：没跑到的步骤一律如实标 skipped，说明原因（报告必须完整 ——
        # 客户对照文件逐条看时，"少了一行"和"这行是跳过"是两件不同的事）
        self._mark_remaining(run, aborted)
        run.status = self._final_status(run, aborted)

    async def _restore_step(self, step: StepSpec) -> None:
        """把上次运行里已成功的步骤"原样搬过来"：输出进上下文、报告里如实标注。"""
        saved = self._resume_steps[step.id]
        output = saved.get("output")
        await self._emit_step_start(step)
        now = _now()
        record = StepReport(
            id=step.id, type=step.type, status=STATUS_OK, started_at=now, finished_at=now,
            line=step.position.line,
            output_digest=str(saved.get("output_digest") or digest_of(output)),
            detail={"resumed": True, "from_run": self._resume_from})
        self._record_output(step, output)
        if step.type == "parallel":
            # 整组上次就成功了：子步骤的输出也一起搬回来，组后面的步骤照常能引用
            for child in step.substeps:
                saved_child = self._resume_steps.get(child.id)
                if saved_child is None and isinstance(output, dict):
                    saved_child = {"output": output.get(child.id)}
                if saved_child is not None:
                    self._record_output(child, saved_child.get("output"))
        self._records[step.id] = record
        self._resumed.append(step.id)
        logger.info("workflow_step_resumed", step=step.id, from_run=self._resume_from)
        await self._emit_step_end(record)
        await self._checkpoint()

    def snapshot(self) -> dict[str, Any]:
        """当前进度的可落盘快照（R-104）：已结束步骤的记录 + 成功步骤的输出。

        只放**顶层**步骤：foreach 的子步骤以整个 foreach 为续跑粒度
        （一项跑一半的状态很难说清"重跑安不安全"，整步重跑的语义最简单）。
        parallel 组例外（D2，v1.9.0）：组内子步骤以**子步骤**为续跑粒度 ——
        组里已成功的子步骤续跑时跳过，只重跑没成功的那几个。
        """
        top = {s.id for s in self.schema.steps}
        child_of = {c.id: s.id for s in self.schema.steps if s.type == "parallel"
                    for c in s.substeps}
        top |= set(child_of)
        steps_ctx = self._context.get("steps", {}) if self._context else {}
        records = {sid: r.as_dict() for sid, r in self._records.items()
                   if sid in top and r.status != STATUS_STARTED}
        for sid, rec in records.items():
            if sid in child_of:                        # 组内子步骤：标上所属的组
                rec["parent"] = child_of[sid]
        outputs = {sid: (steps_ctx.get(sid) or {}).get("output")
                   for sid, r in self._records.items()
                   if sid in top and r.status == STATUS_OK}
        self._snapshot_seq += 1
        # 快照里的输出是续跑时要喂回去的：凭据明文换成 *** 之后，续跑的步骤若引用到它，
        # 拿到的会是 ***。这是刻意的 —— 宁可续跑时那一步失败，也不让明文落盘。
        return {"run_id": self.run_id, "workflow": self.schema.name, "seq": self._snapshot_seq,
                "source": self.schema.source, "source_digest": self.schema.source_digest,
                "inputs": self._redact(jsonable(self.inputs)), "records": self._redact(records),
                "outputs": self._redact(jsonable(outputs)), "resumed_from": self._resume_from}

    async def _checkpoint(self) -> None:
        """把快照交给注入的落盘回调。失败只记日志 —— 落盘是保险，不是流程的一部分。"""
        if self.on_checkpoint is None or self.dry_run:
            return
        try:
            result = self.on_checkpoint(self.snapshot())
            if asyncio.iscoroutine(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:                       # pragma: no cover - 观测兜底
            logger.warning("workflow_checkpoint_failed", run_id=self.run_id,
                           error=f"{type(exc).__name__}: {exc}")

    def _final_status(self, run: WorkflowRun, aborted: bool) -> str:
        if self.dry_run:
            return RUN_DRY_RUN
        if aborted:
            return RUN_ABORTED
        failed = sum(1 for r in self._records.values() if r.status == STATUS_FAILED)
        if failed:
            # 有失败步骤但整轮跑完了 → partial，而不是 ok。
            # 见模块开头第 1 条硬边界。
            return RUN_PARTIAL
        return RUN_OK

    async def _run_step(self, step: StepSpec) -> tuple[StepReport, int | None]:
        """执行一个步骤。返回 `(报告行, 跳转目标下标 | None)`。

        为什么返回元组而不是"把跳转写进 record 让调用方自己读"：调用方
        （`_execute_steps`）必须**明确处理**跳转，否则一个 branch 步骤会被
        当成普通步骤走下一行 —— 流程静默走错分支，而报告里全是绿的。
        """
        record = StepReport(id=step.id, type=step.type, status=STATUS_STARTED,
                            started_at=_now(), line=step.position.line)
        started = time.perf_counter()

        # 未知类型在加载期就被拒绝；这里是防御性兜底，且**必须在 dry-run 分支之前**：
        # 放在后面会让 dry-run 走到一个没有返回值的分支上（实测踩过这个坑）。
        handler = self._handler_for(step.type)

        if self.dry_run:
            # dry-run 只渲染参数、不做任何真实动作。见 _dry_run_step。
            if handler is None:
                record.status = STATUS_FAILED
                record.error = f"未知的步骤类型：{step.type}"
            else:
                try:
                    # 步骤级 if / unless（v1.11.16）：试运行时只渲染预览、不据此跳过 ——
                    # 真跑时才有的步骤输出在 dry-run 里是占位对象，拿它判定条件等于
                    # 让试运行报告一个假的执行路径；但"条件里引用错了变量"必须现在暴露。
                    self._dry_run_conditions(step, record)
                except TemplateError as exc:
                    record.status = STATUS_FAILED
                    record.error = f"试运行渲染失败：{exc.format()}"
                else:
                    await self._dry_run_step(step, record)
            record.duration_ms = (time.perf_counter() - started) * 1000
            record.finished_at = _now()
            return record, None

        jump: int | None = None
        try:
            if handler is None:                       # 加载期已拦住，防御性兜底
                record.status = STATUS_FAILED
                record.error = f"未知的步骤类型：{step.type}"
                record.duration_ms = (time.perf_counter() - started) * 1000
                record.finished_at = _now()
                return record, None

            if step.if_condition or step.unless_condition:
                self._apply_condition_gate(step, record)
                if record.status == STATUS_SKIPPED:
                    record.duration_ms = (time.perf_counter() - started) * 1000
                    record.finished_at = _now()
                    return record, None

            if record.status != STATUS_FAILED:
                if step.timeout:
                    # 超时是**步骤级**的硬上限，与工具自身的 timeout 参数是两回事：
                    # 工具超时管"这一次调用"，步骤超时还兜住"多次重试的总时长"。
                    async with asyncio.timeout(step.timeout):
                        await self._attempts(step, record, handler)
                else:
                    await self._attempts(step, record, handler)
                # 跳转目标由 branch 步骤自己写进 detail["goto"]：用一个显式的
                # "执行期协议字段"而不是让每个 handler 返回不同形状的值，
                # 避免"忘了解包元组"这类只有跑到那条分支才会暴露的错误。
                if step.type == "branch" and record.status == STATUS_OK:
                    target_id = record.detail.get("goto")
                    resolved = self.schema.index_of(str(target_id)) if target_id else -1
                    if resolved < 0:
                        # 跳转目标不存在 = **控制流坏了**，不是"这一步的业务失败"。
                        # 此时无论 on_failure 写什么都必须中止：按顺序往下走会让
                        # 两条互斥分支都执行（实测过这个后果），而那是"报告全绿、
                        # 实际多做了事"的静默错误。加载期已经拦过，这里是第二道闸。
                        record.status = STATUS_ABORTED
                        record.error = (f"branch 的跳转目标不存在：{target_id!r}；"
                                        "控制流无法继续，已中止整轮（不接受 on_failure 放行）")
                    else:
                        jump = resolved
        except TimeoutError:
            record.status = STATUS_FAILED
            record.error = (f"步骤超时：超过 {step.timeout:g}s 未完成。"
                            "若是等待人工审批，请调大该步骤的 timeout 或改用 "
                            "abort 之外的处理策略")
            logger.warning("workflow_step_timeout", step=step.id, timeout=step.timeout)
        except asyncio.CancelledError:
            # 只记录，不吞：取消必须一路向上（见模块开头第 3 条硬边界）
            record.status = STATUS_CANCELLED
            record.error = "执行过程中被取消（父任务停止）"
            record.duration_ms = (time.perf_counter() - started) * 1000
            record.finished_at = _now()
            self._records[step.id] = record
            raise

        # 失败策略的**唯一**收口处。刻意放在这里而不是各 handler 里：
        # 无论失败来自工具返回、模板渲染、超时还是"没注入依赖"，
        # `on_failure` 的语义都一样 —— 分散在 handler 里写，迟早出现
        # "某类失败忘了看策略"，而那种 bug 的表现是"该停的没停，继续往下改了生产系统"。
        #
        # 三态收口（与 docs/WORKFLOWS.md 的表述逐字对应）：
        #   · abort            → 整轮中止
        #   · continue         → 本步记失败，后续照常跑
        #   · retry(n) 用尽    → 仍失败，**按 abort 处理**（重试只是给一次机会，
        #                        不是"允许失败"）；若第一次就成功则根本走不到这里
        #   · retry(n) 但未用尽 → 不会走到这里（handler 内部会继续重试）
        if record.status == STATUS_FAILED:
            policy = step.on_failure
            # 不支持重试的类型（human / branch / foreach）写了 retry：失败即视为用尽。
            # 此前 llm 步骤写 retry(n) 根本不重试、失败后还按 continue 放行 ——
            # 与文档"retry 用尽按 abort 处理"相反（v1.8.0 修正）。
            exhausted_retry = policy.kind == "retry" and (
                record.retries >= policy.retries or step.type not in _RETRYABLE_TYPES)
            if policy.kind == "abort" or exhausted_retry:
                record.status = STATUS_ABORTED
                record.detail.setdefault("on_failure", policy.raw)

        record.duration_ms = (time.perf_counter() - started) * 1000
        record.finished_at = _now()
        if self._redact_values:
            record.error = self._redact(record.error)
            record.detail = self._redact(record.detail)
        return record, jump

    # ── 步骤级条件（v1.11.16）────────────────────────────

    @staticmethod
    def _conditions_of(step: StepSpec) -> list[tuple[str, str]]:
        """该步骤声明的条件列表（最多一个；if / unless 二者互斥）。"""
        out: list[tuple[str, str]] = []
        if step.if_condition:
            out.append(("if", step.if_condition))
        if step.unless_condition:
            out.append(("unless", step.unless_condition))
        return out

    def _apply_condition_gate(self, step: StepSpec, record: StepReport) -> None:
        """按 ``if`` / ``unless`` 决定这一步执不执行（真实运行）。

        语义只有两条，且**没有"条件求值失败就放行"的路径**：

          * ``if``     条件成立 → 执行；不成立 → ``skipped``；
          * ``unless`` 条件成立 → ``skipped``；不成立 → 执行；
          * 条件**无法判定**（数值比较遇到非数字、in 的右侧不是列表等）→ ``failed``
            —— 与 branch 同一条理由：判不了就继续走，等于让执行路径变成掷硬币。

        被跳过的步骤也会把 ``output=None`` 写进上下文：后面的步骤若引用它，
        会**明确报错**（而不是拿到空串后一路往下走）。
        """
        for field_name, text in self._conditions_of(step):
            operator, left, right = _split_condition(text)
            if operator is None:
                record.status = STATUS_FAILED
                record.error = f"步骤条件无法解析：{field_name}: {text!r}"
                record.detail["condition"] = {"field": field_name, "text": text,
                                              "error": "条件语法无法解析"}
                return
            try:
                matched, lhs, rhs = self._evaluate_condition(
                    step, operator, left, right, field_name=field_name)
            except _ConditionError as exc:
                record.status = STATUS_FAILED
                record.error = f"步骤条件无法判定（{field_name}）：{exc}"
                record.detail["condition"] = {"field": field_name, "text": text,
                                              "operator": operator, "error": str(exc)}
                return
            detail = {"field": field_name, "text": text, "operator": operator,
                      "matched": matched, "left": lhs, "right": rhs}
            should_run = matched if field_name == "if" else not matched
            detail["should_run"] = should_run
            record.detail["condition"] = detail
            if not should_run:
                record.status = STATUS_SKIPPED
                record.error = (f"{field_name} 条件不满足，该步骤已跳过"
                                f"（{text} → 左侧 {lhs!r}"
                                + (f"，右侧 {rhs!r}" if rhs or operator != "exists" else "")
                                + "）")
                self._record_output(step, None, error=record.error)
                logger.info("workflow_step_condition_skip", step=step.id,
                            field=field_name, matched=matched)
                return
            return                                  # 只有一个条件，命中了就执行

    def _dry_run_conditions(self, step: StepSpec, record: StepReport) -> None:
        """试运行时把条件两侧渲染出来做预览；渲染错误照常抛 TemplateError。"""
        for field_name, text in self._conditions_of(step):
            operator, left, right = _split_condition(text)
            preview: dict[str, Any] = {"field": field_name, "text": text,
                                       "operator": operator or ""}
            path_name = f"steps.{step.id}.{field_name}"
            if operator is None:
                record.detail[f"condition_{field_name}"] = preview
                continue
            if operator == "exists":
                preview["present"] = self._exists(left)
            else:
                preview["left_preview"] = to_text(render(
                    left, self._context, position=step.position,
                    path_name=path_name, strict=self.strict_templates))[:120]
                if operator in LIST_OPS and parse_literal_list(right) is not None:
                    preview["right"] = right
                else:
                    preview["right"] = to_text(render(
                        right, self._context, position=step.position,
                        path_name=path_name, strict=self.strict_templates))[:120]
            record.detail[f"condition_{field_name}"] = preview

    def _handler_for(self, step_type: str) -> Callable[..., Awaitable[None]] | None:
        return {
            "tool": self._run_tool,
            "llm": self._run_llm,
            "human": self._run_human,
            "branch": self._run_branch,
            "agent": self._run_agent,
            "foreach": self._run_foreach,
            "parallel": self._run_parallel,
        }.get(step_type)

    async def _attempts(self, step: StepSpec, record: StepReport,
                        handler: Callable[..., Awaitable[None]]) -> None:
        """执行 handler；llm / agent 按 `retry(n)` 在这里重试（tool 在 handler 内部重试）。

        异常在这里统一转成"本次尝试失败"：
          · 模板错误、依赖没注入（WorkflowExecutionError）—— 重试也不会好，直接收尾；
          · 其它异常（网络抖动、模型报错）—— 计一次失败，还有次数就再试；
          · 取消与步骤超时照常往外抛（由 `_run_step` 收口）。
        """
        tries = step.on_failure.max_attempts if step.type in _RETRY_IN_STEP else 1
        for attempt in range(tries):
            if attempt:
                record.retries = attempt
                record.status = STATUS_STARTED
                record.error = ""
                logger.info("workflow_step_retry", step=step.id, step_type=step.type,
                            attempt=attempt + 1)
            try:
                await handler(step, record)
            except (asyncio.CancelledError, TimeoutError):
                raise
            except TemplateError as exc:
                # 模板错误在加载期已尽量拦掉；能走到这里的是"执行路径导致取不到值"
                # （例如 branch 跳过了产出某变量的步骤）。报错要能指向具体字段。
                record.status = STATUS_FAILED
                record.error = f"模板渲染失败：{exc.format()}"
                return
            except WorkflowExecutionError as exc:
                record.status = STATUS_FAILED
                record.error = exc.message
                return
            except Exception as exc:                   # 兜底：任何异常都不许吞掉
                record.status = STATUS_FAILED
                record.error = f"步骤抛出未预期的异常（{type(exc).__name__}）：{exc}"
                logger.error("workflow_step_error", step=step.id, step_type=step.type,
                             error=f"{type(exc).__name__}: {exc}")
            if record.status != STATUS_FAILED:
                return
        if tries > 1 and record.retries:
            record.error = f"{record.error}（已重试 {record.retries} 次）"

    # ── 各类型步骤 ────────────────────────────────────

    async def _run_tool(self, step: StepSpec, record: StepReport) -> None:
        """`tool` 步骤：走注入的注册表 dispatch。

        为什么统一走 `ToolRegistry.dispatch` 而不是 `registry.get(name).execute()`：
        `dispatch` 是仓库里**唯一**保证"无论发生什么返回的都是 ToolResult"的入口
        （工具名写错、参数名写错、工具自己抛异常，都会转成失败结果 + 可照抄的建议）。
        工作流里一次工具名拼错就是一次生产事故排查，不该因为绕开 dispatch 而丢失
        那层已有的容错与建议。
        """
        if self.registry is None:
            raise WorkflowExecutionError(
                f"步骤 {step.id} 是 tool 类型，但执行器没有注入 registry。"
                "请把 agent 的 tool_registry 传进来（见 docs/WORKFLOWS.md 的接线片段）")

        # 工具名本身也允许模板（少数场景要按入参选工具），先渲染再派发
        tool_name = render(step.tool, self._context, position=step.position,
                           path_name=f"steps.{step.id}.tool", strict=self.strict_templates)
        tool_name = str(tool_name).strip()
        if not tool_name:
            record.status = STATUS_FAILED
            record.error = f"步骤 {step.id} 的 tool 字段渲染后为空"
            return

        missing = [name for text in _walk_strings(step.args) for name in secret_refs(text)
                   if name not in self._secrets]
        if missing and not self.dry_run:
            record.status = STATUS_FAILED
            record.error = (f"凭据 {'、'.join(dict.fromkeys(missing))} 未配置：请在企业版「凭据保管」"
                            "里添加（值不会进报告与日志），或检查名字是否写对")
            return

        #: 每次尝试的参数都被记录 —— 重试时参数可能因模板取值不同而不同，
        #: 报告里只留最后一次容易让人误以为"一直都在用这套参数"。
        attempts: list[dict[str, Any]] = []
        result: ToolResult | None = None
        policy = step.on_failure

        for attempt in range(policy.max_attempts):
            args = render_structure(step.args, self._context, position=step.position,
                                    path_name=f"steps.{step.id}.args",
                                    strict=self.strict_templates)
            if not isinstance(args, dict):             # 加载期已保证是映射
                args = {}
            if attempt:
                record.retries = attempt
                logger.info("workflow_step_retry", step=step.id, tool=tool_name,
                            attempt=attempt + 1)
            attempts.append({"tool": tool_name, "args": args})
            record.detail["tool"] = tool_name
            result = await self.registry.dispatch(tool_name, **args)
            if result.success:
                break
            if policy.kind != "retry":
                break

        record.detail["attempts"] = len(attempts)
        record.detail["args"] = attempts[-1]["args"]
        if result is None:                             # pragma: no cover - 防御性
            record.status = STATUS_FAILED
            record.error = "工具未被执行（执行器内部状态异常）"
            return

        record.detail["tool_duration_ms"] = round(getattr(result, "duration_ms", 0.0), 1)
        if result.success:
            record.status = STATUS_OK
            record.output_digest = digest_of(result.output)
            self._record_output(step, result.output)
            return

        # 失败：**无论哪种策略都如实标失败**。
        # `abort` 与 `continue` 的区别由 `_run_step` 末尾的策略收口统一处理
        # （abort 会被升级成 aborted 从而中止整轮），这里不重复判断。
        record.status = STATUS_FAILED
        record.error = result.error or "工具执行失败（未给出原因）"
        record.detail["on_failure"] = policy.raw
        # 失败步骤也保留 error 供后续模板 `{{ steps.x.error }}` 引用 ——
        # "失败了就往下走"这种流程（如"取不到就发一封告警邮件"）需要读到原因
        self._record_output(step, None, error=record.error)
        if policy.kind == "retry" and record.retries:
            record.error = f"{record.error}（已重试 {record.retries} 次）"
        logger.warning("workflow_step_failed", step=step.id, tool=tool_name,
                       on_failure=policy.raw, error=self._redact(record.error))
        return

    async def _run_llm(self, step: StepSpec, record: StepReport) -> None:
        """`llm` 步骤：**只做一次生成**，不参与规划。

        这是与 ReAct/Plan 最本质的区别：模型在这里是"一个把文本变好的函数"，
        而不是"决定下一步的人"。它返回什么都不会改变后续步骤的顺序。
        """
        if self.llm is None:
            raise WorkflowExecutionError(
                f"步骤 {step.id} 是 llm 类型，但执行器没有注入 llm。"
                "llm 步骤只做一次文本生成（不参与规划）；"
                "若不需要模型参与，请把该步骤改成 tool 类型")

        prompt = to_text(render(step.prompt, self._context, position=step.position,
                                path_name=f"steps.{step.id}.prompt", strict=self.strict_templates))
        messages: list[dict[str, Any]] = []
        if step.system:
            messages.append({"role": "system", "content": to_text(render(
                step.system, self._context, position=step.position,
                path_name=f"steps.{step.id}.system", strict=self.strict_templates))})
        messages.append({"role": "user", "content": prompt})

        response = await self.llm.generate(messages)
        # token 统计（v1.11.16）：响应里带了多少就记多少；不同 SDK 的字段名不统一，
        # 由 _response_tokens 归一化。拿不到就记 0 —— 不猜、不按字数估算。
        usage = _response_tokens(response)
        if usage["total"]:
            record.tokens = dict(usage)
            self._add_tokens(usage["prompt"], usage["completion"])
        text = _extract_text(response)
        if text is None:
            record.status = STATUS_FAILED
            record.error = (f"llm 返回了无法识别的响应类型：{type(response).__name__}。"
                            "需要的接口是 async generate(messages) → 带 .text 的响应或 str")
            return

        record.status = STATUS_OK
        record.output_digest = digest_of(text)
        record.detail["prompt_chars"] = len(prompt)
        # 提示词可能含敏感信息（工单内容、内部 URL），报告里只留长度与摘要，
        # 不整段回显 —— 报告会进日志、进前端、进 CI 产物。
        record.detail["prompt_digest"] = digest_of(prompt)
        self._record_output(step, text)
        return

    async def _run_human(self, step: StepSpec, record: StepReport) -> None:
        """`human` 步骤：等一次人工批准。

        **没有审批回调 = 拒绝**（fail-closed），不是放行。

        为什么必须这样：本执行器会被用在无人值守的场景（CI、定时任务、
        服务端后台）。如果"问不到人就默认通过"，那么任何一次**审批通道故障**
        （回调没注入、Web 层断连、配置漏了）都会静默地把需要人批的变更单
        直接执行掉 —— 而且报告里还是绿的。相比之下，"没人批 → 停下来"
        最坏只是流程没走完，可以重跑；前者是不可逆的生产事故
        （见 plan_executor.py:449 对同一问题的处置，这里保持一致的语义）。
        """
        prompt = to_text(render(step.prompt, self._context, position=step.position,
                                path_name=f"steps.{step.id}.prompt", strict=self.strict_templates))

        if self.approval is None:
            record.status = STATUS_FAILED
            record.error = ("需要人工审批，但当前没有可用的审批通道，已按**拒绝**处理"
                            "（不会默认放行）。请在调用时注入 approval 回调："
                            "async (step, rendered_prompt) -> bool；"
                            "无人值守的流程不应包含 human 步骤")
            record.detail["approval"] = "unavailable"
            self._record_output(step, None, error=record.error)
            logger.warning("workflow_human_no_channel", step=step.id)
            return

        # v1.10.0（R-303）：approvers + mode。mode=all 按声明顺序逐级审批，每一级都要通过；
        # mode=any 只发一次审批，列出的任一审批人通过即可。没写 approvers 时与此前完全一样
        # （一次审批，谁看到弹窗谁就能批）。每一次裁决（谁、什么时候、批没批）都进报告 ——
        # 运行审计导出（R-304）要的就是这一份。
        if step.approvers and step.approval_mode == "all":
            levels = [[a] for a in step.approvers]
        else:
            levels = [list(step.approvers)]
        approvals: list[dict[str, Any]] = []
        outcome = ApprovalOutcome(approved=False)
        for level_no, required in enumerate(levels, 1):
            kwargs = ({"required": required, "level": level_no, "levels": len(levels)}
                      if step.approvers else {})
            raw: Any = None
            try:
                raw = await self.approval(step, prompt, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 审批通道自己炸了 → 同样按拒绝（与 plan_executor 的处理一致）
                outcome = ApprovalOutcome(approved=False,
                                          comment=f"审批通道异常（{type(exc).__name__}）：{exc}")
            else:
                outcome = ApprovalOutcome.normalize(raw)
            by = raw.get("approver") if isinstance(raw, dict) else None
            approvals.append({"level": level_no, "required": required,
                              "approved": outcome.approved, "by": jsonable(by) if by else None,
                              "at": _now(), "comment": outcome.comment or ""})
            if not outcome.approved:
                break
        record.detail["approvals"] = approvals
        if step.approvers:
            record.detail["approvers"] = list(step.approvers)
            record.detail["approval_mode"] = step.approval_mode

        record.detail["approval"] = "approved" if outcome.approved else "denied"
        if outcome.comment:
            record.detail["comment"] = outcome.comment
        if outcome.modified:
            record.detail["modified_args"] = jsonable(outcome.arguments)

        if outcome.approved:
            record.status = STATUS_OK
            # 输出固定为 "approved" 这类可判定的词，方便 branch 拿它做受限比较
            self._record_output(step, "approved")
            return

        record.status = STATUS_FAILED
        detail = f"：{outcome.comment}" if outcome.comment else ""
        level = (f"（第 {len(approvals)}/{len(levels)} 级，需要 {'、'.join(levels[len(approvals) - 1])}）"
                 if step.approvers and len(levels) > 1 else "")
        record.error = f"人工审批未通过{level}{detail}"
        self._record_output(step, None, error=record.error)
        return

    async def _run_agent(self, step: StepSpec, record: StepReport) -> None:
        """`agent` 步骤（v1.8.0，R-106）：把一个子目标交给 Agent 自主完成。

        与 `llm` 步骤的区别：llm 只做一次生成；agent 会多轮"思考 → 调工具"，
        但**工具集被 allowed_tools 圈定**、步数被 max_steps 圈定、token 被
        token_budget 圈定 —— 自主性关在一个评审方看得见的笼子里。

        成功的判据是"Agent 自己宣布完成"（stop_reason = no_more_tools）。步数 /
        预算耗尽、原地打转时产出的是**部分交付**，如实记失败（但输出仍保留，
        后续步骤可用 `{{ steps.x.output.answer }}` 读到它做到了哪一步）。
        """
        if self.agent_runner is None:
            raise WorkflowExecutionError(
                f"步骤 {step.id} 是 agent 类型，但执行器没有注入 agent_runner。"
                "请通过 Web 界面或 REST 接口运行（服务端会自动接上）；命令行不接模型，"
                "只能 --dry-run。嵌入调用见 automind/workflow/agent_step.py")
        goal = to_text(render(step.goal, self._context, position=step.position,
                              path_name=f"steps.{step.id}.goal", strict=self.strict_templates))
        raw = await self.agent_runner(step, goal)
        out = raw if isinstance(raw, dict) else {"answer": to_text(raw)}
        result = {
            "answer": str(out.get("answer") or ""),
            "artifacts": list(out.get("artifacts") or []),
            "actions_ok": int(out.get("actions_ok") or 0),
            "actions_failed": int(out.get("actions_failed") or 0),
            "stop_reason": str(out.get("stop_reason") or ""),
        }
        denied = list(out.get("denied_tools") or [])
        # token 统计（v1.11.16）：agent_runner 的 D6 输出里带 tokens（见 agent_step.py）
        agent_tokens = int(out.get("tokens") or 0)
        if agent_tokens:
            record.tokens = {"prompt": 0, "completion": 0, "total": agent_tokens}
            self._add_tokens(0, agent_tokens)
        record.detail.update({
            "goal_chars": len(goal), "goal_digest": digest_of(goal),
            "stop_reason": result["stop_reason"],
            "actions_ok": result["actions_ok"], "actions_failed": result["actions_failed"],
            "artifacts": result["artifacts"][:20],
            "allowed_tools": out.get("allowed_tools", step.allowed_tools),
            "iterations": out.get("iterations"), "tokens": out.get("tokens"),
        })
        if denied:
            # 越权调用必须进报告：评审方要能看到"它试图做允许范围之外的事"
            record.detail["denied_tools"] = denied[:20]
        self._record_output(step, result)
        record.output_digest = digest_of(result)
        if result["stop_reason"] in _AGENT_DONE:
            record.status = STATUS_OK
            return
        record.status = STATUS_FAILED
        record.error = (f"agent 步骤未完成（{_AGENT_STOP_ZH.get(result['stop_reason'], result['stop_reason'] or '原因未知')}）："
                        f"成功动作 {result['actions_ok']} 个、失败 {result['actions_failed']} 个；"
                        "部分结果已保留在 output 中")
        # 失败时 error 字段也要能被 `{{ steps.x.error }}` 读到
        self._context["steps"][step.id]["error"] = record.error

    async def _run_foreach(self, step: StepSpec, record: StepReport) -> None:
        """`foreach` 步骤（v1.8.0，R-107）：对列表逐项执行子步骤。

        语义（与 docs/WORKFLOWS.md 逐字对应）：
          · 子步骤 `on_failure: abort`（默认）失败 → **本 foreach 立即停止**，
            剩余项不再执行，整步记失败（其自身的 on_failure 决定整轮是否继续）；
          · 子步骤 `on_failure: continue` 失败 → 该项记失败，继续本项后续子步骤与后续各项；
          · 任一项失败 → 整步记失败（部分成功不许记成成功），输出仍是逐项列表。
        上限在**展开之后、执行第一项之前**判定：超限一项都不跑。
        """
        items = self._foreach_items(step)
        child_ids = [c.id for c in step.substeps]
        stop = asyncio.Event()
        stopped: list[tuple[int, str]] = []

        async def _one(index: int, item: Any) -> dict[str, Any]:
            entry = await self._run_item(step, index, item)
            if entry.get("stopped"):
                stopped.append((index, entry["stopped"]))
                stop.set()
            await self._emit("iteration_end", {
                "id": step.id, "index": index, "total": len(items),
                "status": entry["status"]})
            return entry

        jobs = [(lambda i=i, it=it: _one(i, it)) for i, it in enumerate(items)]
        if step.max_concurrency > 1:
            raw = await self.group_runner(jobs, max_concurrency=step.max_concurrency, stop=stop)
        else:
            raw = await run_group_sequential(jobs, stop=stop)
        # 并发时结果按项的顺序收（不是按完成顺序）：报告要能和输入逐项对照
        results = [r for r in raw if r is not None]
        steps_ctx = self._context.setdefault("steps", {})
        for cid in child_ids:                       # 子步骤的输出只在循环内可见
            steps_ctx.pop(cid, None)
        self._context.pop("loop", None)

        failed = [r for r in results if r["status"] != STATUS_OK]
        output = [{"index": r["index"], "item": r["item"], "status": r["status"],
                   "outputs": r["outputs"]} for r in results]
        record.detail.update({
            "items": len(items), "executed": len(results), "failed_items": len(failed),
            "not_started": len(items) - len(results),
            # 大规模迭代（专业版上限 1000）时报告只逐项展开前 50 项；
            # 失败项的序号全部列出（最多 200 个）—— 排障要的就是"哪几项坏了"
            "failed_indexes": [r["index"] for r in failed][:200],
            "iterations": [{"index": r["index"], "status": r["status"], "steps": r["steps"]}
                           for r in results[:50]],
        })
        if step.max_concurrency > 1:
            record.detail["max_concurrency"] = step.max_concurrency
        self._record_output(step, output)
        record.output_digest = digest_of(output)
        if stopped:
            # 并发时可能有不止一项 abort：报告里写第一个（按项序），其余见 failed_indexes
            record.status = STATUS_FAILED
            record.error = min(stopped)[1]
        elif failed:
            record.status = STATUS_FAILED
            record.error = (f"{len(failed)}/{len(items)} 项有子步骤失败"
                            f"（第 {', '.join(str(r['index'] + 1) for r in failed[:10])} 项）")
        else:
            record.status = STATUS_OK
        if record.status == STATUS_FAILED:
            self._context["steps"][step.id]["error"] = record.error

    async def _run_item(self, step: StepSpec, index: int, item: Any) -> dict[str, Any]:
        """执行 foreach 的一项：在**本项自己的上下文**里依次跑子步骤。

        本项上下文 = 整轮上下文的浅拷贝 + ``loop`` + 本项子步骤的输出；
        逐项并发时各项互不可见（见 ``_ITEM_CONTEXT`` 的说明）。
        """
        base = self._base_context
        item_ctx = {**base, "steps": dict(base.get("steps") or {}),
                    "loop": {"item": item, "index": index}}
        token = _ITEM_CONTEXT.set((self, item_ctx))
        entry: dict[str, Any] = {"index": index, "item": jsonable(item),
                                 "status": STATUS_OK, "outputs": {}, "steps": []}
        try:
            for child in step.substeps:
                child_record, _ = await self._run_step(child)
                entry["steps"].append({
                    "id": child.id, "status": child_record.status,
                    "error": child_record.error,
                    "duration_ms": round(child_record.duration_ms, 1)})
                if child_record.status in (STATUS_OK, STATUS_SKIPPED):
                    # 子步骤被 if / unless 条件跳过（v1.11.16）不算这一项失败：
                    # "条件不成立所以没做"是流程设计的一部分，不是故障 ——
                    # 与真正的 failed / aborted 必须分开记。
                    if child_record.status == STATUS_OK:
                        entry["outputs"][child.id] = (item_ctx["steps"].get(child.id)
                                                      or {}).get("output")
                    continue
                entry["status"] = STATUS_FAILED
                if child_record.status == STATUS_ABORTED:
                    entry["stopped"] = (f"第 {index + 1} 项的子步骤 {child.id} 失败"
                                        f"（{child_record.error}），策略为 abort，剩余项不再执行")
                    break
        finally:
            _ITEM_CONTEXT.reset(token)
        return entry

    async def _run_parallel(self, step: StepSpec, record: StepReport) -> None:
        """`parallel` 并行组（v1.9.0，R-204）：组内子步骤并发执行。

        语义（与 docs/WORKFLOWS.md 逐字对应）：
          · 最多同时跑 ``max_concurrency`` 个（默认 4）；
          · 子步骤 ``on_failure: abort``（默认）失败 → **还没启动的**子步骤不再启动，
            已经在跑的跑完为止（中途掐断一个外部调用比让它跑完更难对账）；
          · 任一子步骤失败 → 整组记失败（部分成功不许记成成功），组自己的
            ``on_failure`` 决定整轮是否继续；
          · 组的输出是 ``{子步骤 id: 输出}``；组结束后也可以直接引用
            ``{{ steps.<子步骤>.output }}``；
          · 续跑（D2）：组内已成功的子步骤沿用上次结果，只重跑其余的。
        """
        stop = asyncio.Event()

        async def _job(child: StepSpec) -> StepReport:
            if child.id in self._resume_steps and not self.dry_run:
                return await self._restore_child(child, parent=step.id)
            await self._emit_step_start(child)
            child_record, _ = await self._run_step(child)
            self._records[child.id] = child_record
            await self._emit_step_end(child_record, parent=step.id)
            if child_record.status == STATUS_ABORTED:
                stop.set()
            # 每个子步骤结束就落一次盘：组跑到一半被停掉，已成功的那几个续跑时也不再执行
            await self._checkpoint()
            return child_record

        jobs = [(lambda c=child: _job(c)) for child in step.substeps]
        raw = await self.group_runner(jobs, max_concurrency=step.max_concurrency, stop=stop)
        children: list[dict[str, Any]] = []
        output: dict[str, Any] = {}
        steps_ctx = self._context.setdefault("steps", {})
        for child, child_record in zip(step.substeps, raw, strict=True):
            if child_record is None:
                child_record = StepReport(
                    id=child.id, type=child.type, status=STATUS_SKIPPED, line=child.position.line,
                    error="未启动：组内有子步骤失败且策略为 abort")
                self._records[child.id] = child_record
            children.append({"id": child.id, "type": child.type, "status": child_record.status,
                             "error": child_record.error, "line": child_record.line,
                             "duration_ms": round(child_record.duration_ms, 1),
                             "resumed": bool(child_record.detail.get("resumed"))})
            if child_record.status == STATUS_OK:
                output[child.id] = (steps_ctx.get(child.id) or {}).get("output")
        failed = [c for c in children if c["status"] not in (STATUS_OK, STATUS_SKIPPED)]
        skipped_children = [c["id"] for c in children if c["status"] == STATUS_SKIPPED]
        record.detail.update({"max_concurrency": step.max_concurrency, "children": children})
        if skipped_children:
            # 条件跳过的子步骤在组里既不算成功也不算失败：报告里如实列出来，
            # 让评审方看到"这一路本来就不会跑"（而不是悄无声息地少一行）。
            record.detail["skipped_children"] = skipped_children
        self._record_output(step, output)
        record.output_digest = digest_of(output)
        if not failed:
            record.status = STATUS_OK
            return
        record.status = STATUS_FAILED
        first = next((c for c in failed if c["status"] != STATUS_SKIPPED), failed[0])
        not_started = sum(1 for c in children
                          if c["status"] == STATUS_SKIPPED
                          and "未启动" in str(c.get("error") or ""))
        record.error = (f"并行组 {len(failed)}/{len(children)} 个子步骤未成功"
                        f"（首个：{first['id']} —— {first['error']}）"
                        + (f"；{not_started} 个因 abort 未启动" if not_started else ""))
        self._context["steps"][step.id]["error"] = record.error

    async def _restore_child(self, child: StepSpec, *, parent: str) -> StepReport:
        """续跑时组内已成功的子步骤：沿用上次输出，不再执行（D2）。"""
        saved = self._resume_steps[child.id]
        output = saved.get("output")
        await self._emit_step_start(child)
        now = _now()
        record = StepReport(
            id=child.id, type=child.type, status=STATUS_OK, started_at=now, finished_at=now,
            line=child.position.line,
            output_digest=str(saved.get("output_digest") or digest_of(output)),
            detail={"resumed": True, "from_run": self._resume_from})
        self._record_output(child, output)
        self._records[child.id] = record
        self._resumed.append(child.id)
        await self._emit_step_end(record, parent=parent)
        return record

    def _foreach_items(self, step: StepSpec, *, dry_run: bool = False) -> list[Any]:
        """展开 items 并做上限检查（超限直接抛，一项都不跑）。

        dry-run 时上游输出是占位对象，数不出有几项 —— 按"一项占位"预览子步骤。
        """
        if isinstance(step.items, list):
            items: Any = render_structure(step.items, self._context, position=step.position,
                                          path_name=f"steps.{step.id}.items",
                                          strict=self.strict_templates)
        else:
            items = render(str(step.items or ""), self._context, position=step.position,
                           path_name=f"steps.{step.id}.items", strict=self.strict_templates)
        if isinstance(items, str):
            # 上游工具常把列表以 JSON 文本返回 —— 能无歧义解析成列表就认，其余不猜
            try:
                parsed = json.loads(items)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                items = parsed
        if dry_run and isinstance(items, _DryRunPlaceholder):
            return [items]
        if isinstance(items, dict) and not isinstance(items, _DryRunPlaceholder):
            raise WorkflowExecutionError(
                f"foreach 步骤 {step.id} 的 items 渲染结果是映射而不是列表"
                f"（键：{'、'.join(list(map(str, items))[:10])}）。请引用其中的列表字段，"
                "如 {{ steps.x.output.rows }}")
        if not isinstance(items, (list, tuple)):
            raise WorkflowExecutionError(
                f"foreach 步骤 {step.id} 的 items 渲染结果不是列表，而是 "
                f"{type(items).__name__}：{str(items)[:120]}")
        items = list(items)
        if self.foreach_max is not None and len(items) > self.foreach_max:
            raise WorkflowExecutionError(
                f"foreach 步骤 {step.id} 展开后有 {len(items)} 项，超过当前版本单次最大迭代数 "
                f"{self.foreach_max}（一项都未执行）。请在上游步骤里筛选/分批，"
                "或升级到更高版本（专业版上限 1000）")
        return items

    async def _run_branch(self, step: StepSpec, record: StepReport) -> None:
        """`branch` 步骤：受限比较，选一个**前面**的步骤跳过去。

        不做表达式求值（理由见 template.py）。比较的两侧各自渲染成文本后
        做 `==` / `!=` / `contains`，因此：
          · 判断"某步骤输出里有没有某个字段值" → `{{ steps.a.output.status }} == ok`
          · 判断"输出里提没提到某关键词"     → `{{ steps.a.output }} contains 失败`
        两侧都是模板时比较的是**渲染后的文本**，不是 Python 对象 ——
        这也是刻意的：文本比较没有隐式类型转换的坑（`1 == "1"` 到底成不成立，
        在 YAML 里不该是个需要推理的问题）。

        跳转去向写在 `record.detail["goto"]`，由 `_run_step` 统一解析成下标 ——
        见那里的说明。
        """
        condition = step.condition
        operator, left, right = _split_condition(condition)
        if operator is None:                           # 加载期已校验，防御性兜底
            record.status = STATUS_FAILED
            record.error = f"无法解析的 branch 条件：{condition!r}"
            return

        try:
            matched, lhs, rhs = self._evaluate_condition(step, operator, left, right)
        except _ConditionError as exc:
            # 比较本身无法进行（数值比较遇到非数字、in 的右侧不是列表）→ 判**失败**，
            # 不静默走 else：走错分支而报告全绿，是 branch 最危险的错
            record.status = STATUS_FAILED
            record.error = f"branch 条件无法判定：{exc}"
            record.detail.update({"condition": condition, "operator": operator})
            return

        target_id = step.then if matched else step.else_
        other_id = step.else_ if matched else step.then
        record.status = STATUS_OK
        record.output_digest = digest_of(target_id)
        record.detail.update({"condition": condition, "matched": matched,
                              "left": lhs, "right": rhs, "goto": target_id,
                              "skipped_branch": other_id})
        self._record_output(step, target_id)

        # 二选一的语义：**没走的那条路整段标 skipped**。
        #
        # 只把跳转目标交给主循环是不够的 —— 主循环从目标继续往下走，于是
        # `branch(then=good, else=bad)` 会先跑 good、紧接着又跑 bad：两条互斥
        # 分支全被执行了（实测踩过这个坑，且它是最危险的一类错 —— 报告全绿、
        # 流程看起来跑通了，实际多执行了一条**不该走**的分支）。
        #
        # 约定（已写进 docs/WORKFLOWS.md）：branch 之后是"两个互斥区间" ——
        # then 区间从 then 起、到 else 之前；else 区间从 else 起、到文件末尾。
        # 因此没被选中的那一侧一律跳过。没有落进这两个区间的步骤由
        # `_mark_remaining` 兜底，报告永远是完整的一份。
        target_index = self.schema.index_of(target_id)
        other_index = self.schema.index_of(other_id)
        if target_index >= 0 and other_index >= 0:
            if matched:
                self._skip_range(other_index, len(self.schema.steps),
                                 f"branch 步骤 {step.id} 判定为真，本步骤属于 else 分支，未执行")
            else:
                self._skip_range(other_index, max(target_index, other_index),
                                 f"branch 步骤 {step.id} 判定为假，本步骤属于 then 分支，未执行")

    def _evaluate_condition(self, step: StepSpec, operator: str, left: str,
                            right: str, *, field_name: str = "condition"
                            ) -> tuple[bool, str, str]:
        """按 conditions.py 的语义判定；返回 ``(是否成立, 左侧文本, 右侧文本)``。

        ``field_name`` 只用于报错路径（branch.condition / if / unless 共用同一份实现）。
        """
        path_name = f"steps.{step.id}.{field_name}"
        if operator == "exists":
            present = self._exists(left)
            return present, ("存在" if present else "不存在"), ""

        def _text(side: str) -> str:
            return to_text(render(side, self._context, position=step.position,
                                  path_name=path_name, strict=self.strict_templates))

        lhs = _text(left)
        if operator in LIST_OPS:
            options = parse_literal_list(right)
            if options is None:
                value = render(right, self._context, position=step.position,
                               path_name=path_name, strict=self.strict_templates)
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        pass
                if not isinstance(value, (list, tuple)):
                    raise _ConditionError(
                        f"{operator} 的右侧渲染结果不是列表（{type(value).__name__}：{str(value)[:80]}）")
                options = [_option_text(v) for v in value]
            shown = json.dumps(options[:50], ensure_ascii=False)
            if operator == "contains_any":
                # 空串关键词会让任何文本都"命中"，几乎一定是笔误 —— 不算命中
                return any(o and o in lhs for o in options), lhs, shown
            return lhs in options, lhs, shown
        rhs = _text(right)
        if operator in NUMERIC_OPS:
            a, b = as_number(lhs), as_number(rhs)
            if a is None or b is None:
                bad = lhs if a is None else rhs
                raise _ConditionError(f"数值比较 {operator!r} 的一侧不是数字：{bad[:80]!r}")
            return {">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b}[operator], lhs, rhs
        if operator == "==":
            return lhs == rhs, lhs, rhs
        if operator == "!=":
            return lhs != rhs, lhs, rhs
        return rhs in lhs, lhs, rhs                    # contains

    def _exists(self, text: str) -> bool:
        """``exists``：单个占位符引用的值取得到且不为 null（dry-run 占位视为存在）。"""
        tokens = find_placeholders(text)
        if len(tokens) != 1 or not tokens[0].is_valid:
            return False
        current: Any = self._context
        for part in tokens[0].path:
            if isinstance(current, dict):
                try:
                    current = current[part]
                except KeyError:
                    return False
            elif isinstance(current, (list, tuple)) and part.isdigit() \
                    and int(part) < len(current):
                current = current[int(part)]
            else:
                return False
        return current is not None

    def _skip_range(self, start: int, stop: int, reason: str) -> None:
        """把 `[start, stop)` 区间里**尚未执行**的步骤标记为 skipped。

        只标未执行的：已经跑过的步骤不能被改写状态 —— 把真实执行过的步骤
        说成"没跑"，比漏标一行严重得多（审计时无从判断到底做没做）。
        """
        for i in range(max(0, start), min(stop, len(self.schema.steps))):
            step = self.schema.steps[i]
            if step.id in self._records:
                continue
            self._records[step.id] = StepReport(
                id=step.id, type=step.type, status=STATUS_SKIPPED,
                line=step.position.line, error=reason)

    # ── dry-run ───────────────────────────────────────

    async def _dry_run_step(self, step: StepSpec, record: StepReport) -> None:
        """试运行：只渲染参数并说明"将执行什么"，**不产生任何副作用**。

        具体保证（tests/workflow 有断言）：
          · 不调 `registry.dispatch`，因此工具一次都不会被调用；
          · 不调 `llm.generate`，不消耗 token；
          · 不调审批回调，不弹窗；
          · 工具**参数照常渲染**，所以"模板引错了变量"这类问题在 dry-run
            阶段就会暴露 —— 这正是 dry-run 的主要用途（客户评审前先跑一遍）。
        工具若自带 `get_execution_plan`（见 tools/base.py），一并拿来做预览；
        拿不到就退回"参数原文"，绝不因为"预览生成失败"而把整轮标成失败。
        """
        record.status = STATUS_OK
        preview: dict[str, Any] = {"action": step.type}
        try:
            if step.type == "tool":
                tool_name = str(render(step.tool, self._context, position=step.position,
                                       path_name=f"steps.{step.id}.tool", strict=True)).strip()
                args = render_structure(step.args, self._context, position=step.position,
                                        path_name=f"steps.{step.id}.args", strict=True)
                preview.update({"tool": tool_name, "args": args})
                record.detail["tool"] = tool_name
                record.detail["args"] = args
                if self.registry is not None:
                    known = tool_name in getattr(self.registry, "_tools", {}) \
                        or tool_name in set(self.registry.list_names())
                    preview["tool_registered"] = known
                    if not known:
                        # dry-run 阶段就把"工具没挂上"点出来：真跑时它会失败，
                        # 而客户评审时最该知道的就是"这步现在跑不了"
                        record.detail["warning"] = (
                            f"工具 {tool_name!r} 不在当前注册表中，真跑时该步骤会失败")
                    else:
                        try:
                            tool = self.registry.get(tool_name)
                            preview["plan"] = tool.get_execution_plan(**args)
                        except Exception:              # pragma: no cover - 预览是尽力而为
                            pass
            elif step.type == "llm":
                prompt = to_text(render(step.prompt, self._context, position=step.position,
                                        path_name=f"steps.{step.id}.prompt", strict=True))
                preview.update({"llm": True, "prompt_chars": len(prompt),
                                "prompt_preview": prompt[:200]})
                record.detail["llm_injected"] = self.llm is not None
            elif step.type == "human":
                prompt = to_text(render(step.prompt, self._context, position=step.position,
                                        path_name=f"steps.{step.id}.prompt", strict=True))
                preview.update({"human": True, "prompt_preview": prompt[:200]})
                if step.approvers:
                    preview.update({"approvers": list(step.approvers),
                                    "approval_mode": step.approval_mode})
                record.detail["approval_channel"] = self.approval is not None
                # 试运行下 human 步骤的输出按"已批准"记账（试运行本来就不会真的问人），
                # 这样后面若用 branch 判断审批结果，试运行也能把两条路都走一遍
                self._record_output(step, "approved", dry_run=True)
            elif step.type == "agent":
                goal = to_text(render(step.goal, self._context, position=step.position,
                                      path_name=f"steps.{step.id}.goal", strict=True))
                preview.update({"agent": True, "goal_preview": goal[:200],
                                "allowed_tools": step.allowed_tools,
                                "max_steps": step.max_steps,
                                "token_budget": step.token_budget})
                record.detail["agent_injected"] = self.agent_runner is not None
            elif step.type == "foreach":
                failure = await self._dry_run_foreach(step, preview)
                if failure:
                    record.status = STATUS_FAILED
                    record.error = failure
                    record.detail.update(preview)
                    return
            elif step.type == "parallel":
                failure = await self._dry_run_parallel(step, preview)
                if failure:
                    record.status = STATUS_FAILED
                    record.error = failure
                    record.detail.update(preview)
                    return
            elif step.type == "branch":
                operator, left, right = _split_condition(step.condition)
                lhs = to_text(render(left, self._context, position=step.position,
                                     path_name=f"steps.{step.id}.condition", strict=True)) \
                    if operator and operator != "exists" else left
                rhs = to_text(render(right, self._context, position=step.position,
                                     path_name=f"steps.{step.id}.condition", strict=True)) \
                    if operator and operator != "exists" and operator not in LIST_OPS or (
                        operator in LIST_OPS and parse_literal_list(right) is None) else right
                preview.update({"condition": step.condition, "left_preview": lhs[:120],
                                "right": rhs, "then": step.then, "else": step.else_})
        except TemplateError as exc:
            # dry-run 的价值就在于把这类问题提前暴露，因此这里如实标失败
            record.status = STATUS_FAILED
            record.error = f"试运行渲染失败：{exc.format()}"
            record.detail.update(preview)
            return

        record.detail.update(preview)
        record.detail["dry_run"] = True
        # 输出用**占位对象**而不是真实结果：dry-run 没有真实结果，编一个假的会让
        # 后续渲染出看起来合理的参数，反而误导评审。用占位对象而不是空字符串，
        # 是因为后面的步骤常写 `{{ steps.a.output.field }}` —— 空串上取不到 `.field`，
        # 会让一次**本来完全合法**的试运行报错（见 _DryRunPlaceholder 的说明）。
        self._record_output(step, _DryRunPlaceholder(), dry_run=True)

    async def _dry_run_foreach(self, step: StepSpec, preview: dict[str, Any]) -> str:
        """试运行 foreach：展开 items、查上限，并把子步骤在前几项上各渲染一遍。

        返回失败原因（空串 = 通过）。只预览前 3 项 —— 试运行要回答的是
        "模板写得对不对、会不会超限"，不是把 20 项的参数全铺出来。
        """
        try:
            items = self._foreach_items(step, dry_run=True)
        except WorkflowExecutionError as exc:
            return f"试运行失败：{exc.message}"
        preview_items = items[:3]
        iterations: list[dict[str, Any]] = []
        steps_ctx = self._context.setdefault("steps", {})
        try:
            for index, item in enumerate(preview_items):
                self._context["loop"] = {"item": item, "index": index}
                rows = []
                for child in step.substeps:
                    child_record = StepReport(id=child.id, type=child.type,
                                              line=child.position.line)
                    try:
                        self._dry_run_conditions(child, child_record)
                    except TemplateError as exc:
                        child_record.status = STATUS_FAILED
                        child_record.error = f"试运行渲染失败：{exc.format()}"
                    else:
                        await self._dry_run_step(child, child_record)
                    rows.append({"id": child.id, "status": child_record.status,
                                 "error": child_record.error,
                                 "detail": jsonable(child_record.detail)})
                    if child_record.status != STATUS_OK:
                        return (f"试运行第 {index + 1} 项的子步骤 {child.id} 失败："
                                f"{child_record.error}")
                iterations.append({"index": index, "steps": rows})
        finally:
            self._context.pop("loop", None)
            for child in step.substeps:
                steps_ctx.pop(child.id, None)
        placeholder = len(items) == 1 and isinstance(items[0], _DryRunPlaceholder)
        preview.update({"foreach": True,
                        "items": "⟨试运行占位：真跑时才知道有几项⟩" if placeholder else len(items),
                        "limit": self.foreach_max, "preview": iterations})
        return ""

    async def _dry_run_parallel(self, step: StepSpec, preview: dict[str, Any]) -> str:
        """试运行 parallel 组：逐个渲染子步骤（不并发、无副作用）；返回失败原因。"""
        rows = []
        for child in step.substeps:
            child_record = StepReport(id=child.id, type=child.type, line=child.position.line)
            try:
                self._dry_run_conditions(child, child_record)
            except TemplateError as exc:
                child_record.status = STATUS_FAILED
                child_record.error = f"试运行渲染失败：{exc.format()}"
            else:
                await self._dry_run_step(child, child_record)
            rows.append({"id": child.id, "type": child.type, "status": child_record.status,
                         "error": child_record.error, "detail": jsonable(child_record.detail)})
            if child_record.status != STATUS_OK:
                return f"试运行并行组的子步骤 {child.id} 失败：{child_record.error}"
        preview.update({"parallel": True, "max_concurrency": step.max_concurrency,
                        "children": rows})
        return ""

    # ── 上下文与记录 ──────────────────────────────────

    def _build_context(self) -> dict[str, Any]:
        """模板可见的全部变量。

        只有三个根：`inputs` / `steps` / `env`。刻意**不暴露**执行器内部状态
        （时间、随机数、宿主路径）—— 工作流的可复现性依赖于"同样的输入产生
        同样的参数"，多一个变量就多一处不可复现的来源。
        """
        context: dict[str, Any] = {
            "inputs": {name: self.inputs.get(name) for name in self.schema.inputs},
            "steps": {},
            "env": dict(self.env),
        }
        # v1.10.0：凭据根。试运行不碰真值（任何名字都渲染成占位）
        context["secrets"] = _DryRunSecrets() if self.dry_run else dict(self._secrets)
        return context

    def _redact(self, value: Any) -> Any:
        """把凭据明文从任意结构里抹掉（报告 / 事件 / 快照用；模板上下文里保留真值）。"""
        if not self._redact_values:
            return value
        if isinstance(value, str):
            for secret in self._redact_values:
                if secret in value:
                    value = value.replace(secret, REDACTED)
            return value
        if isinstance(value, dict) and type(value).__str__ is dict.__str__:
            return {k: self._redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self._redact(v) for v in value)
        return value

    def _add_tokens(self, prompt: int = 0, completion: int = 0) -> dict[str, int]:
        """把一次模型调用的 token 用量累加进整轮统计（v1.11.16）。"""
        prompt = max(0, int(prompt or 0))
        completion = max(0, int(completion or 0))
        self._tokens["prompt"] += prompt
        self._tokens["completion"] += completion
        self._tokens["total"] = self._tokens["prompt"] + self._tokens["completion"]
        return dict(self._tokens)

    def _record_output(self, step: StepSpec, output: Any, *, error: str = "",
                       dry_run: bool = False) -> None:
        """把步骤结果放进模板上下文。

        `skipped`/`dry_run` 的步骤也会写入占位（None），这样后续模板
        `{{ steps.x.output }}` 会**明确报错**（"取不到键"）而不是渲染成空串 ——
        见 template.py 开头对"空串会一路往下走"的说明。
        """
        entry: dict[str, Any] = {"output": output}
        if error:
            entry["error"] = error
        if dry_run:
            entry["dry_run"] = True
        self._context.setdefault("steps", {})[step.id] = entry

    def _mark_remaining(self, run: WorkflowRun, aborted: bool) -> None:  # noqa: ARG002
        """给没执行到的步骤补一行 `skipped`，并说明原因。

        `aborted` 只影响措辞。走到这里还没记录的步骤只有一种情形：
        循环提前结束了（abort 中止、或 branch 跳过了尾部区间），
        因此原因只有两种可能，如实写出来即可。
        """
        for step in self.schema.steps:
            if step.id in self._records:
                continue
            self._records[step.id] = StepReport(
                id=step.id, type=step.type, status=STATUS_SKIPPED,
                line=step.position.line,
                error=("未被执行：前序步骤失败且策略为 abort，整轮已中止"
                       if aborted else
                       "未被执行：执行路径未经过该步骤（被 branch 跳过，或整轮提前结束）"))

    def _finish_records(self, run: WorkflowRun) -> None:
        """按**文件声明顺序**把记录装进报告。

        顺序固定这件事本身就是"可评审"的一部分：报告与文件逐行对得上，
        评审方可以拿 diff 比对两版报告。
        """
        for step in self.schema.steps:
            record = self._records.get(step.id)
            if record is None:
                record = StepReport(id=step.id, type=step.type, status=STATUS_SKIPPED,
                                    line=step.position.line,
                                    error="未被执行（本轮提前结束）")
                self._records[step.id] = record
            if record.status == STATUS_STARTED:        # 取消时可能停在这一态
                record.status = STATUS_CANCELLED
                record.error = record.error or "执行被中断"
            run.steps.append(record)
            if record.status == STATUS_OK:
                entry = self._context.get("steps", {}).get(step.id) or {}
                run.outputs[step.id] = self._redact(entry.get("output"))
        run.resumed_steps = list(self._resumed)
        # 整轮 token 汇总（v1.11.16）：看板 / 通知都从报告读它。
        run.tokens = dict(self._tokens)
        if self.run_cancelled and run.status == RUN_CANCELLED:
            run.error = run.error or "工作流被取消"

    def _step_no(self) -> int:
        """当前步骤在文件里的序号（1 基），用于错误信息。"""
        for i, step in enumerate(self.schema.steps, 1):
            if step.id == self._current_id:
                return i
        return 0

    # ── 事件 ──────────────────────────────────────────

    async def _emit(self, event: str, payload: dict[str, Any]) -> None:
        """把进度事件交给注入的回调（CLI 打印 / 服务端推给前端）。

        回调支持同步与异步两种形态：CLI 用同步的 print，服务端用异步的
        `ws.send_json`。事件本身的失败**不影响**执行 —— 进度是观测，
        不是流程的一部分；一次发送失败不该让已经跑了几步的流程白费。
        """
        if self.on_event is None:
            return
        try:
            result = self.on_event(event, self._redact(payload))
            if asyncio.iscoroutine(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:                       # pragma: no cover - 观测兜底
            # 注意参数名不能叫 `event`：`get_logger()` 返回的适配器签名是
            # `warning(event, **kw)`，用 `event=` 传参会在异常处理路径上再抛
            # "got multiple values for argument 'event'" —— 观测兜底自己炸掉，
            # 把真正的问题盖住（实测踩过）。
            logger.warning("workflow_event_sink_failed", evt=event,
                           error=f"{type(exc).__name__}: {exc}")

    async def _emit_step_start(self, step: StepSpec) -> None:
        payload = {"id": step.id, "type": step.type, "line": step.position.line,
                   "index": self._step_no(), "total": len(self.schema.steps)}
        if step.parent:                                # parallel 组内的子步骤
            payload["parent"] = step.parent
        await self._emit("step_start", payload)

    async def _emit_step_end(self, record: StepReport, *, parent: str = "") -> None:
        payload = record.as_dict()
        if parent:
            payload["parent"] = parent
        await self._emit("step_end", payload)

    # ── 取消辅助（供外部读取）──────────────────────────

    @property
    def run_cancelled(self) -> bool:
        """本轮是否因取消而结束。"""
        return self._cancelled


# ═══════════════════════════════════════════════════════════════
# 模块级便捷入口（父 agent 接线用）
# ═══════════════════════════════════════════════════════════════


async def run_workflow(
    schema: WorkflowSchema,
    inputs: dict[str, Any] | None = None,
    *,
    registry: Any = None,
    llm: Any = None,
    approval: Callable[..., Awaitable[Any]] | None = None,
    env: dict[str, str] | None = None,
    dry_run: bool = False,
    on_event: Callable[[str, dict[str, Any]], Any] | None = None,
    agent_runner: Callable[..., Awaitable[Any]] | None = None,
    run_id: str = "",
    resume: dict[str, Any] | None = None,
    on_checkpoint: Callable[[dict[str, Any]], Any] | None = None,
    foreach_max: Any = _AUTO_LIMIT,
    group_runner: GroupRunner | None = None,
    secrets: dict[str, str] | None = None,
) -> WorkflowRun:
    """执行一份工作流并拿回报告 —— 服务端与 CLI 的统一入口。

    **不抛业务异常**：步骤失败体现在报告的 `status` / `steps[].status` 里，
    调用方看 `run.ok` / `run.exit_code` 即可。唯一会抛的是
    `asyncio.CancelledError`（取消必须传播，见 executor 模块开头第 3 条）。
    """
    executor = WorkflowExecutor(schema, inputs=inputs, registry=registry, llm=llm,
                                approval=approval, env=env, dry_run=dry_run,
                                on_event=on_event, agent_runner=agent_runner,
                                run_id=run_id, resume=resume, on_checkpoint=on_checkpoint,
                                foreach_max=foreach_max, group_runner=group_runner,
                                secrets=secrets)
    return await executor.run()


def check_inputs(schema: WorkflowSchema, provided: dict[str, Any]
                 ) -> tuple[dict[str, Any], list[str], list[str]]:
    """校验并补全入参，返回 `(最终入参, 错误, 警告)`。

    放在执行器模块而不是 CLI 里：服务端 `POST /api/workflow/run` 要用**同一套**
    规则判断"入参给全了没"。两处各写一份，迟早出现"CLI 说缺参数、接口却能跑"
    这种自相矛盾的行为。

    规则：
      · 必填且无默认值 → 缺了就报错（作为 error 返回，由调用方决定退出码）；
      · 有默认值 → 缺了就用默认值；
      · 传了未声明的入参 → 警告（不报错：CI 里多传变量是常事，
        但"写错的变量名被静默丢掉"必须被看见）。
    """
    errors: list[str] = []
    warnings: list[str] = []
    final: dict[str, Any] = {}

    for name, spec in schema.inputs.items():
        if name in provided and provided[name] is not None:
            final[name] = provided[name]
        elif spec.default is not None:
            final[name] = spec.default
        elif spec.required:
            errors.append(f"缺少必需入参 '{name}'（{spec.type}）"
                          + (f"：{spec.description}" if spec.description else ""))
        else:
            # 可选且无默认值 → 显式给 None，而不是让模板报"未定义"：
            # 这样 `{{ inputs.optional }}` 渲染成空串是**声明过的行为**，
            # 而不是一次误引用
            final[name] = None

    for name in provided:
        if name not in schema.inputs:
            candidates = [n for n in schema.inputs if n != name]
            hint = ""
            import difflib

            close = difflib.get_close_matches(str(name), candidates, n=3, cutoff=0.6)
            if close:
                hint = f"（你是不是想传：{'、'.join(close)}？）"
            warnings.append(f"传入了未声明的入参 '{name}'，它不会被任何模板引用到{hint}")

    return final, errors, warnings


# ═══════════════════════════════════════════════════════════════
# 内部工具
# ═══════════════════════════════════════════════════════════════


class _ConditionError(Exception):
    """branch 条件在执行期无法判定（不是"不成立"，是"没法比"）。"""


def _option_text(value: Any) -> str:
    """in 的列表项 → 比较用的文本（与 conditions.parse_literal_list 的口径一致）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(jsonable(value), ensure_ascii=False, sort_keys=True)
    return str(value)


def _split_condition(condition: str) -> tuple[str | None, str, str]:
    """把 `左 <算子> 右` 拆开；解析不了返回 `(None, "", "")`。

    解析规则与加载期校验共用 conditions.py 的同一份实现（v1.9.0）——
    两边各写一份"找算子"，迟早出现"校验说是 A、执行按 B 判"。
    """
    cond = _parse_condition(condition)
    if cond is None:
        return None, "", ""
    return cond.op, cond.left, cond.right


def _walk_strings(value: Any) -> list[str]:
    """嵌套结构里的全部字符串（找凭据引用用）。"""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _walk_strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _walk_strings(v)]
    return []


def _extract_text(response: Any) -> str | None:
    """从 LLM 响应里取文本。

    只认两种形态：`str` 与带 `.text` 的对象（`automind.core.types.LLMResponse`
    就是后者）。为什么不猜别的字段名：取错字段会把"模型没回答"变成
    "回答是空字符串"，而空字符串会一路往下渲染成一个看起来正常的空正文。
    """
    if response is None:
        return None
    if isinstance(response, str):
        return response
    text = getattr(response, "text", None)
    return text if isinstance(text, str) else None


def _response_tokens(response: Any) -> dict[str, int]:
    """从 LLM 响应里归一化出 token 用量（v1.11.16）。

    ``LLMResponse`` 有 ``prompt_tokens`` / ``completion_tokens``；部分 SDK 用
    ``usage`` / ``token_usage`` 子对象。取不到一律记 0：**不按字符数估算** ——
    估算值和真实用量混在一起，看板上的趋势就失去意义了。
    """

    def _int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    prompt = _int(getattr(response, "prompt_tokens", 0))
    completion = _int(getattr(response, "completion_tokens", 0))
    if not (prompt or completion):
        usage = getattr(response, "usage", None) or getattr(response, "token_usage", None)
        if isinstance(usage, dict):
            prompt = _int(usage.get("prompt_tokens") or usage.get("input_tokens"))
            completion = _int(usage.get("completion_tokens") or usage.get("output_tokens"))
        elif usage is not None:
            prompt = _int(getattr(usage, "prompt_tokens", 0)
                          or getattr(usage, "input_tokens", 0))
            completion = _int(getattr(usage, "completion_tokens", 0)
                              or getattr(usage, "output_tokens", 0))
    return {"prompt": prompt, "completion": completion,
            "total": prompt + completion}


def _os_env() -> dict[str, str]:
    """默认的环境变量来源。

    只在**执行器被真正使用**时才读 `os.environ`（而不是 import 时快照），
    这样服务端在运行中设置的变量（例如每次请求注入的凭据）也能被读到。
    """
    import os

    return dict(os.environ)


def iter_positions(schema: WorkflowSchema) -> Iterable[tuple[str, Position]]:
    """遍历 `(步骤 id, 定义位置)` —— 排障与前端"点击报告跳到定义"用。"""
    for step in schema.steps:
        yield step.id, step.position


def report_to_json(run: WorkflowRun, *, include_outputs: bool = False,
                   indent: int | None = None) -> str:
    """把报告转成 JSON 字符串 —— 服务端 `POST /api/workflow/run` 直接返回它。

    单独一个函数而不是让调用方写 `json.dumps(run.as_dict())`：报告里可能塞进
    任意工具的原始输出（datetime、bytes、Path…），裸 `json.dumps` 遇到这些会抛。
    这里统一带 `default=str` 兜底，保证"任何情况下都能把报告交出去" ——
    接口返回 500、而报告其实已经生成好了，是最没必要的一种失败。
    """
    return json.dumps(run.as_dict(include_outputs=include_outputs),
                      ensure_ascii=False, indent=indent, default=str)


__all__ = [
    "EXIT_BAD_WORKFLOW",
    "EXIT_CANCELLED",
    "EXIT_OK",
    "EXIT_STEP_FAILED",
    "RUN_ABORTED",
    "RUN_CANCELLED",
    "RUN_DRY_RUN",
    "RUN_FAILED",
    "RUN_OK",
    "RUN_PARTIAL",
    "STATUS_ABORTED",
    "STATUS_CANCELLED",
    "STATUS_FAILED",
    "STATUS_OK",
    "STATUS_SKIPPED",
    "STATUS_STARTED",
    "StepReport",
    "WorkflowExecutor",
    "WorkflowRun",
    "REDACTED",
    "GroupRunner",
    "check_inputs",
    "digest_of",
    "iter_positions",
    "jsonable",
    "report_to_json",
    "run_group_sequential",
    "run_workflow",
    "to_text",
]
