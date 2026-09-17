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
from automind.workflow.exceptions import Position, TemplateError, WorkflowExecutionError
from automind.workflow.schema import StepSpec, WorkflowSchema
from automind.workflow.template import render, render_structure

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
    #: 补充信息（工具名、跳转去向、审批结果等），便于审计与排障
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "type": self.type, "status": self.status,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "duration_ms": round(self.duration_ms, 1), "output_digest": self.output_digest,
            "error": self.error, "retries": self.retries, "line": self.line,
            "detail": jsonable(self.detail),
        }


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
            "steps": [s.as_dict() for s in self.steps],
        }
        if include_outputs:
            data["outputs"] = jsonable(self.outputs)
        return data

    def to_json(self, *, include_outputs: bool = False, indent: int | None = 2) -> str:
        return json.dumps(self.as_dict(include_outputs=include_outputs),
                          ensure_ascii=False, indent=indent, default=str)

    def summary_line(self) -> str:
        return (f"工作流「{self.workflow}」{_status_zh(self.status)}："
                f"共 {self.total} 步，成功 {self.succeeded}、失败 {self.failed}、"
                f"跳过 {self.skipped}"
                + (f"、取消 {self.cancelled}" if self.cancelled else "")
                + f"，耗时 {self.duration_ms / 1000:.2f}s")


def _status_zh(status: str) -> str:
    return {
        RUN_OK: "执行成功", RUN_PARTIAL: "部分成功", RUN_FAILED: "执行失败",
        RUN_CANCELLED: "已取消", RUN_DRY_RUN: "试运行（未真正执行）",
        RUN_ABORTED: "已中止",
    }.get(status, status)


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
    * ``env``           —— 模板 `{{ env.NAME }}` 的取值来源

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

        self._visits: dict[str, int] = {}
        self._records: dict[str, StepReport] = {}
        self._context: dict[str, Any] = {}
        self._current_id: str = ""
        self._cancelled = False

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
            run_id=uuid.uuid4().hex[:12], started_at=_now(), dry_run=self.dry_run,
            inputs=self.inputs, source=self.schema.source,
            source_digest=self.schema.source_digest,
            warnings=[w.message for w in self.schema.warnings],
        )
        started = time.perf_counter()
        self._context = self._build_context()
        finished = False

        try:
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

            if record.status == STATUS_ABORTED and not jump:
                aborted = True
                break
            index = jump if jump is not None else index + 1

        # 收尾：没跑到的步骤一律如实标 skipped，说明原因（报告必须完整 ——
        # 客户对照文件逐条看时，"少了一行"和"这行是跳过"是两件不同的事）
        self._mark_remaining(run, aborted)
        run.status = self._final_status(run, aborted)

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
        handler = {
            "tool": self._run_tool,
            "llm": self._run_llm,
            "human": self._run_human,
            "branch": self._run_branch,
        }.get(step.type)

        if self.dry_run:
            # dry-run 只渲染参数、不做任何真实动作。见 _dry_run_step。
            if handler is None:
                record.status = STATUS_FAILED
                record.error = f"未知的步骤类型：{step.type}"
            else:
                await self._dry_run_step(step, record)
            record.duration_ms = (time.perf_counter() - started) * 1000
            record.finished_at = _now()
            return record, None

        try:
            if handler is None:                       # 加载期已拦住，防御性兜底
                record.status = STATUS_FAILED
                record.error = f"未知的步骤类型：{step.type}"
                record.duration_ms = (time.perf_counter() - started) * 1000
                record.finished_at = _now()
                return record, None

            jump: int | None = None
            if step.timeout:
                # 超时是**步骤级**的硬上限，与工具自身的 timeout 参数是两回事：
                # 工具超时管"这一次调用"，步骤超时还兜住"多次重试的总时长"。
                async with asyncio.timeout(step.timeout):
                    await handler(step, record)
            else:
                await handler(step, record)
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
        except TemplateError as exc:
            # 模板错误在加载期已尽量拦掉；能走到这里的是"执行路径导致取不到值"
            # （例如 branch 跳过了产出某变量的步骤）。报错要能指向具体字段。
            record.status = STATUS_FAILED
            record.error = f"模板渲染失败：{exc.format()}"
        except WorkflowExecutionError as exc:
            record.status = STATUS_FAILED
            record.error = exc.message
        except Exception as exc:                       # 兜底：任何异常都不许吞掉
            record.status = STATUS_FAILED
            record.error = f"步骤抛出未预期的异常（{type(exc).__name__}）：{exc}"
            logger.error("workflow_step_error", step=step.id, step_type=step.type,
                         error=f"{type(exc).__name__}: {exc}")

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
            exhausted_retry = policy.kind == "retry" and record.retries >= policy.retries
            if policy.kind == "abort" or exhausted_retry:
                record.status = STATUS_ABORTED
                record.detail.setdefault("on_failure", policy.raw)

        record.duration_ms = (time.perf_counter() - started) * 1000
        record.finished_at = _now()
        return record, jump

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
                       on_failure=policy.raw, error=record.error)
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

        try:
            raw = await self.approval(step, prompt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 审批通道自己炸了 → 同样按拒绝（与 plan_executor 的处理一致）
            outcome = ApprovalOutcome(approved=False,
                                      comment=f"审批通道异常（{type(exc).__name__}）：{exc}")
        else:
            outcome = ApprovalOutcome.normalize(raw)

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
        record.error = f"人工审批未通过{detail}"
        self._record_output(step, None, error=record.error)
        return

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

        lhs = to_text(render(left, self._context, position=step.position,
                             path_name=f"steps.{step.id}.condition", strict=self.strict_templates))
        rhs = to_text(render(right, self._context, position=step.position,
                             path_name=f"steps.{step.id}.condition", strict=self.strict_templates))

        if operator == "==":
            matched = lhs == rhs
        elif operator == "!=":
            matched = lhs != rhs
        else:                                          # contains
            matched = rhs in lhs

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
                record.detail["approval_channel"] = self.approval is not None
                # 试运行下 human 步骤的输出按"已批准"记账（试运行本来就不会真的问人），
                # 这样后面若用 branch 判断审批结果，试运行也能把两条路都走一遍
                self._record_output(step, "approved", dry_run=True)
            elif step.type == "branch":
                operator, left, right = _split_condition(step.condition)
                lhs = to_text(render(left, self._context, position=step.position,
                                     path_name=f"steps.{step.id}.condition", strict=True)) \
                    if operator else left
                rhs = to_text(render(right, self._context, position=step.position,
                                     path_name=f"steps.{step.id}.condition", strict=True)) \
                    if operator else right
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

    # ── 上下文与记录 ──────────────────────────────────

    def _build_context(self) -> dict[str, Any]:
        """模板可见的全部变量。

        只有三个根：`inputs` / `steps` / `env`。刻意**不暴露**执行器内部状态
        （时间、随机数、宿主路径）—— 工作流的可复现性依赖于"同样的输入产生
        同样的参数"，多一个变量就多一处不可复现的来源。
        """
        return {
            "inputs": {name: self.inputs.get(name) for name in self.schema.inputs},
            "steps": {},
            "env": dict(self.env),
        }

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
                run.outputs[step.id] = entry.get("output")
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
            result = self.on_event(event, payload)
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
        await self._emit("step_start", {
            "id": step.id, "type": step.type, "line": step.position.line,
            "index": self._step_no(), "total": len(self.schema.steps)})

    async def _emit_step_end(self, record: StepReport) -> None:
        await self._emit("step_end", record.as_dict())

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
) -> WorkflowRun:
    """执行一份工作流并拿回报告 —— 服务端与 CLI 的统一入口。

    **不抛业务异常**：步骤失败体现在报告的 `status` / `steps[].status` 里，
    调用方看 `run.ok` / `run.exit_code` 即可。唯一会抛的是
    `asyncio.CancelledError`（取消必须传播，见 executor 模块开头第 3 条）。
    """
    executor = WorkflowExecutor(schema, inputs=inputs, registry=registry, llm=llm,
                                approval=approval, env=env, dry_run=dry_run,
                                on_event=on_event)
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


def _split_condition(condition: str) -> tuple[str | None, str, str]:
    """把 `左 <算子> 右` 拆开；解析不了返回 `(None, "", "")`。

    先看两字符算子（`==` / `!=`）再看 `contains`：顺序会影响到
    `a != b == c` 这种（加载期已拒绝）写法的解析结果，先两字符更符合直觉。
    """
    text = str(condition or "").strip()
    for op in ("==", "!="):
        idx = text.find(op)
        if idx > 0:
            return op, text[:idx].strip(), text[idx + len(op):].strip()
    idx = text.find("contains")
    if idx > 0:
        return "contains", text[:idx].strip(), text[idx + len("contains"):].strip()
    return None, "", ""


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
    "check_inputs",
    "digest_of",
    "iter_positions",
    "jsonable",
    "report_to_json",
    "run_workflow",
    "to_text",
]
