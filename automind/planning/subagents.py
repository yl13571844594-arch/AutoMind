"""子智能体委派运行时（v1.11.11 专业版 multi_agent MVP）。

为什么需要这一层：专业版已有的「协同模式」是一个**编排器** —— 一次把任务拆给
多个角色跑完。可模型在 ReAct 循环里真正频繁需要的是另一件事：执行到一半，
发现有个**自包含的小任务**可以派出去（查一批文件、跑一遍测试、做一次独立调研），
自己继续往下走，回来再取结果。没有这一层，它只能要么串行做完，要么放弃。

直接原地 ``asyncio.create_task`` 一个 Agent 会立刻撞上三件事：

  · 两个任务共享同一个 Agent 实例的执行态（``context_mgr`` / ``_mode`` /
    ``llm.usage`` 全是可变的），并发跑必然串上下文、互相清零计数；
  · 派出去以后**没有句柄**：查不了进度、补不了要求、停不下来，也拿不回结果；
  · 无限递归派生（子生孙、孙生曾孙）几轮就能把 token 预算烧光。

所以这里只做**一次性委派**的最小闭环：``clone_for_session()`` 派生执行态独立的
子 Agent，后台跑 ``run_subtask``，父任务拿到 ``agent_id`` 立即返回；结果保留
最近 20 个，``list`` / ``get`` 读回，``send`` 追加要求，``stop`` 取消。

边界（都是有意的）：
  · 并发上限 4 / 父会话：每个子 Agent 都要独立上下文、独立模型预算，再多就把
    父任务自己的预算挤没了；
  · 嵌套深度上限 1：只有顶层任务能派生；子智能体内部再调 ``subagent`` 会被拒绝。
    深度经 ``session_ctx.subagent_depth()`` 下发（由 ``run_subtask`` 在子执行
    作用域内绑定为父深度 + 1 —— 否则「子 Agent 的工具看到父深度」就等于没限制）；
  · 子任务**一次性**：完成 / 失败 / 取消都是终态，不能复活；需要继续就重新派发；
  · 事件经父 Agent 的 ``event_sink``（退化到 ``session_ctx.event_sink()``）推出，
    字段一律带 ``session_id`` —— 界面据此把子智能体挂到正确的会话/标签页上。

本模块不碰模型、不碰 ReAct，只做编排；子任务的真实执行由
``AutoMindAgent.run_subtask`` 负责（工具白名单、步数 / token 上限都在那边落地，
白名单复用 ``workflow.agent_step.RestrictedToolRegistry``，这里只传名字）。
"""

from __future__ import annotations

import asyncio
import inspect
import itertools
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from automind.core.logging import get_logger

logger = get_logger("automind.planning.subagents")

# ═══════════════════════════════════════════════════════════
# 硬边界 —— 全部写在模块常量里，便于界面/测试引用同一个数字
# ═══════════════════════════════════════════════════════════

#: 单个父会话同时运行的子智能体上限。
#: 为什么是 4：每个子 Agent 都要独立上下文 + 独立模型预算，4 个并发在普通
#: 桌面上已经能吃到 4 份 prompt 开销；再多父任务自己的预算先被挤没了。
MAX_CONCURRENT = 4

#: 允许的嵌套深度（从顶层任务算起）。0 = 顶层；>0 表示"我已经是子智能体"，
#: 此时再派发一律拒绝 —— 防止子生孙、孙生曾孙的指数式烧钱。
MAX_DEPTH = 1

#: 每个父会话保留的最近结果条数（只清理**已结束**的，运行中的永远不删）。
KEEP_RESULTS = 20

#: 派发时未指定 max_steps 的默认值。刻意给得比父任务小：子任务是"一件事"，
#: 不是第二个完整任务；20 步够它查文件/跑测试，又不至于跑成无底洞。
DEFAULT_MAX_STEPS = 20

#: max_steps / max_tokens 的允许范围（工具层校验，防止模型传 10^9 把预算一次烧完）。
MAX_MAX_STEPS = 100
MAX_MAX_TOKENS = 200_000

#: 结果记录里保留的输出字符数（超长截断并标注；完整内容不落进父上下文）。
#:
#: v1.11.18：从 8000 提到 20000。``subagent_list(agent_id=...)``（工具层）允许
#: 回传 20000 字符的"完整输出"，但记录在这里先被 8000 截断 —— 上层看到的
#: 永远只有 8000，详情能力名不副实。两处数字必须对齐（工具层上限
#: ``automind/tools/subagents.py::DETAIL_OUTPUT_CHARS``）。
#: 20k 只存在于**子智能体记录**里（每个父会话最多保留 KEEP_RESULTS=20 条已结束
#: 记录），父上下文仍由列表的 4000 字符/条 + 总 12000 上限约束，不会撑爆。
OUTPUT_KEEP_CHARS = 20_000

#: 列表里给的输出尾部字符数（模型用来判断"跑得对不对"，不需要全文）。
OUTPUT_TAIL_CHARS = 300

#: ``subagent_done`` 事件里 output 的字符上限 —— 事件会经 WebSocket 广播，
#: 几万字的输出会把消息帧撑爆，尾部由界面子智能体面板展示即可。
EVENT_OUTPUT_CHARS = 4000

#: ``subagent_started`` 事件里 task 的字符上限（界面只显示预览，不用全文）。
EVENT_TASK_CHARS = 2000

#: 进度事件（subagent_update）的最小间隔（秒）。子任务每步都可能推一条，
#: 不节流时一条长任务能刷出上百条 —— 界面反而看不清，WebSocket 也白忙。
MIN_UPDATE_INTERVAL_S = 0.5

#: stop() 等待子任务响应取消的时长（秒）。超过就如实告诉调用方"已发停止请求
#: 但还没结束"，而不是谎称"已停止"。
STOP_WAIT_S = 2.0

#: 自动生成标题时的字符数上限。
DEFAULT_TITLE_CHARS = 30

#: list 快照里 task 的预览长度（模型看列表时不需要完整 prompt）。
TASK_PREVIEW_CHARS = 300

_seq = itertools.count(1)


# ═══════════════════════════════════════════════════════════
# 异常 —— 消息必须是"下一步该怎么做"
# ═══════════════════════════════════════════════════════════


class SubagentError(RuntimeError):
    """子智能体运行时错误（派发失败 / 参数非法 / 结果契约不符）。"""


class SubagentLimitError(SubagentError):
    """触达硬边界（并发上限 / 嵌套深度）—— 与"实现坏了"分开，工具会原样回给模型。"""


# ═══════════════════════════════════════════════════════════
# 会话上下文桥接（Lead 在 core/session_ctx.py 提供这些绑定）
# ═══════════════════════════════════════════════════════════
#
# 为什么用「取属性 + callable」而不是直接 ``session_ctx.agent()``：
# 这些绑定与负责接线的模块（agent.py / session_ctx.py）可能分开发布/灰测，
# 工具在旧运行时上被误注册时不应整体崩掉，而应给出「没有可用 Agent」的
# 可操作失败。绑定缺失是真问题，但它的处置是"告诉用户/让模型别重试"，
# 不是抛异常打断任务 —— 这里每次读取都会在缺绑定时返回明确的空值。


def _session_ctx() -> Any:
    # 延迟 import：运行时常量在模块导入期不需要它，也让本模块可被单独测试
    from automind.core import session_ctx

    return session_ctx


def _binding(name: str) -> Any | None:
    """读一个会话上下文绑定：既支持 ``fn()``，也支持直接暴露的 ContextVar。

    Lead 的接线契约是 ``agent()`` / ``subagent_depth()`` 两个函数；但这两者本质
    都是 contextvar，某次重构顺手改成直接暴露 ContextVar 也不该让四个工具整体
    失效 —— 这里两种形状都认，读取失败由调用方记日志并退化为"没有绑定"。
    """
    holder = getattr(_session_ctx(), name, None)
    if callable(holder):
        return holder()
    if holder is not None and hasattr(holder, "get"):
        return holder.get()
    return None


def current_agent() -> Any | None:
    """当前任务的执行 Agent；不在任务上下文中 / 接线缺失时返回 None。"""
    try:
        return _binding("agent")
    except Exception as e:                    # pragma: no cover - 接线故障兜底
        logger.warning("subagent_agent_binding_failed", error=f"{type(e).__name__}: {e}")
        return None


def current_depth() -> int:
    """当前任务的嵌套深度（0 = 顶层任务）。

    读取失败时按 0 处理并记日志：深度读不到就拒绝派发会误伤正常任务，
    而"误放行"由 ``run_subtask`` 的绑定兜底（见模块文档的深度说明）。
    """
    try:
        return max(0, int(_binding("subagent_depth") or 0))
    except (TypeError, ValueError) as e:
        logger.warning("subagent_depth_unreadable", error=str(e))
        return 0
    except Exception as e:                    # pragma: no cover - 接线故障兜底
        logger.warning("subagent_depth_binding_failed", error=f"{type(e).__name__}: {e}")
        return 0


def current_session_id() -> str:
    """当前父会话标识；未绑定（CLI 单用户 / 单测）时退化为 ``"default"``。"""
    try:
        return str(_binding("session_id") or "default")
    except Exception as e:                    # pragma: no cover - 接线故障兜底
        logger.warning("subagent_session_id_unreadable", error=f"{type(e).__name__}: {e}")
        return "default"


def current_event_sink() -> Any | None:
    """当前任务的事件推送回调（父 Agent 未提供 sink 时的退路）。"""
    try:
        return _binding("event_sink")
    except Exception as e:                    # pragma: no cover - 接线故障兜底
        logger.warning("subagent_event_sink_unreadable", error=f"{type(e).__name__}: {e}")
        return None


# ═══════════════════════════════════════════════════════════
# 小工具
# ═══════════════════════════════════════════════════════════


def derive_title(task: str, limit: int = DEFAULT_TITLE_CHARS) -> str:
    """从任务正文生成短标题（界面列表用）。

    为什么不用模型生成：标题只是给人看的索引，为它多打一次模型不值得；
    取首行前 N 个字符足够区分「查 A 目录」和「改 B 模块」。
    """
    body = " ".join(str(task or "").split())
    if not body:
        return "子任务"
    return body if len(body) <= limit else body[: max(1, limit - 1)] + "…"


def _clip(text: str, limit: int) -> str:
    """保留头部，超出部分标注原长（不静默丢内容）。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[已截断，原长 {len(text)} 字符]"


def _tail(text: str, limit: int) -> str:
    """保留尾部 —— 工具输出/报错最有价值的信息通常在结尾。"""
    if len(text) <= limit:
        return text
    return "…[前文略]\n" + text[-limit:]


def _as_text(value: Any) -> str:
    """把 run_subtask 的 output 归一到字符串（dict/list 也保留内容，不丢弃）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _as_int(value: Any, default: int = 0) -> int:
    """把步数/token 归一到非负整数；转不了就沿用旧值（不把一次坏上报当 0）。"""
    if isinstance(value, bool) or value is None:
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


# ═══════════════════════════════════════════════════════════
# 记录
# ═══════════════════════════════════════════════════════════


@dataclass
class SubagentRecord:
    """一个子智能体的全部状态（list/get 的快照都从这里出）。"""

    seq: int
    agent_id: str
    session_id: str
    title: str
    task: str
    status: str = "running"              # running | done | failed | cancelled | error | budget_exhausted
    output: str = ""                     # 最新输出（运行时是进度快照，结束后是最终结果）
    output_tail: str = ""                # 给列表/进度事件的尾部
    error: str = ""
    steps: int = 0
    tokens: int = 0
    #: 结束原因（v1.11.16）：budget_exhausted = Token 预算耗尽、可重新派发；
    #: 父模型据此决定"接着做"而不是把它当成已完成。
    stop_reason: str = ""
    #: 是否可重试/重派（预算耗尽为 True；父级不应静默当成功）
    resumable: bool = False
    allowed_tools: list[str] | None = None
    max_steps: int = DEFAULT_MAX_STEPS
    max_tokens: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    last_update_at: float = 0.0
    #: 是否已请求停止 —— 取消可能晚于子任务自然结束，终态要按"用户停的"算
    stop_requested: bool = False
    parent: Any = None
    child: Any = None
    sink: Any = None
    approval_callback: Any = None
    task_obj: asyncio.Task | None = None

    @property
    def elapsed_s(self) -> float:
        return round((self.finished_at or time.time()) - self.started_at, 2)

    @property
    def running(self) -> bool:
        return self.status == "running"

    def snapshot(self, tail_chars: int = OUTPUT_TAIL_CHARS) -> dict[str, Any]:
        """给模型/界面看的轻量快照（结果正文见 :meth:`detail`）。"""
        tail = self.output_tail or _tail(self.output, tail_chars)
        return {
            "agent_id": self.agent_id,
            "title": self.title,
            "task": _clip(self.task, TASK_PREVIEW_CHARS),
            "status": self.status,
            "steps": int(self.steps),
            "tokens": int(self.tokens),
            "error": self.error,
            # v1.11.16：预算耗尽时明确告诉父模型"可重派"，而不是只看到一个失败状态
            "stop_reason": self.stop_reason,
            "resumable": bool(self.resumable),
            "output_tail": _tail(tail, tail_chars) if tail else "",
            "running": self.running,
            "elapsed_s": self.elapsed_s,
        }

    def detail(self) -> dict[str, Any]:
        """完整快照：含最终 output 与派发时的限额（subagent_list 回传结果用）。"""
        data = self.snapshot()
        data["output"] = _clip(self.output, OUTPUT_KEEP_CHARS)
        data["allowed_tools"] = list(self.allowed_tools) if self.allowed_tools else None
        data["max_steps"] = int(self.max_steps)
        data["max_tokens"] = int(self.max_tokens)
        return data


# ═══════════════════════════════════════════════════════════
# 管理器
# ═══════════════════════════════════════════════════════════


class SubagentManager:
    """按父会话分组的子智能体注册表 + 后台任务生命周期。

    一个进程一个实例（见 :func:`default_manager`）：工具注册表是会话克隆之间
    共享的，若每个工具实例各持一个管理器，「列表/停止」就会看不到彼此派发的
    子智能体。管理器本身是**纯编排**，不读全局状态；会话标识、深度、事件
    sink 都通过参数或 ``session_ctx`` 桥接函数在调用时取。
    """

    def __init__(
        self,
        *,
        emit: Callable[[dict[str, Any]], Any] | None = None,
        max_concurrent: int = MAX_CONCURRENT,
        max_results: int = KEEP_RESULTS,
        depth_getter: Callable[[], int] | None = None,
        session_getter: Callable[[], str] | None = None,
    ) -> None:
        #: 会话 → (agent_id → 记录)。按会话隔离：A 标签页看不到 B 标签页的子智能体
        self._by_session: dict[str, dict[str, SubagentRecord]] = {}
        #: agent_id → 记录（全局索引，get/send/stop 用；跨会话访问仍会被拒绝）
        self._index: dict[str, SubagentRecord] = {}
        #: 显式注入的事件回调（测试/服务端统一广播用）；优先于父 Agent 的 event_sink
        self._emit_override = emit
        self.max_concurrent = max(1, int(max_concurrent))
        self.max_results = max(1, int(max_results))
        self._depth_getter = depth_getter
        self._session_getter = session_getter

    # ── 上下文取值 ─────────────────────────────────────────

    def _sid(self, session_id: str | None) -> str:
        if session_id:
            return str(session_id)
        if self._session_getter is not None:
            return str(self._session_getter())
        return current_session_id()

    def _depth(self) -> int:
        if self._depth_getter is None:
            return current_depth()
        try:
            return max(0, int(self._depth_getter()))
        except (TypeError, ValueError):
            return 0

    def _resolve_sink(self, parent: Any) -> Any:
        """事件出口：显式注入 > 父 Agent 的 event_sink > session_ctx 的 sink。

        在 **start() 的父任务上下文里**解析并存在记录上：后台任务里的
        contextvar 只是父上下文的拷贝，父 Agent 换 sink（下一轮任务）时
        旧记录不该跟着换出口。
        """
        sink = self._emit_override
        if sink is None:
            sink = getattr(parent, "event_sink", None)
        if sink is None:
            sink = current_event_sink()
        return sink if callable(sink) else None

    # ── 派发 ───────────────────────────────────────────────

    async def start(
        self,
        parent: Any,
        task: str,
        *,
        title: str = "",
        allowed_tools: list[str] | None = None,
        max_steps: int = 0,
        max_tokens: int = 0,
        session_id: str | None = None,
        approval_callback: Any = None,
    ) -> str:
        """派生并启动一个子智能体，立即返回 ``agent_id``（不等它跑完）。

        Args:
            parent: 父会话 Agent（须支持 ``clone_for_session``）；这是唯一的
                执行态来源，子 Agent 的模型/工具/记忆都从它克隆。
            task: 自包含的子任务（子 Agent 看不到父对话，必须写全）。
            title: 列表/事件里显示的名字，缺省从 task 生成。
            allowed_tools: 工具白名单；None = 只读（SAFE）工具（由
                ``run_subtask`` 内部的 RestrictedToolRegistry 落地）。
            max_steps / max_tokens: 子任务上限；0 = 用模块默认/不单独限。
            session_id: 分组用的父会话标识；缺省取当前上下文。
            approval_callback: 审批回调；缺省继承父 Agent 的。

        Returns:
            子智能体标识（``list`` / ``send`` / ``stop`` 都靠它）。

        Raises:
            SubagentLimitError: 触达嵌套深度或并发上限（消息可直接回给用户/模型）。
            SubagentError: 参数非法或父会话无法克隆。
        """
        body = str(task or "").strip()
        if not body:
            raise SubagentError(
                "子任务内容不能为空：task 必须写清子智能体要完成什么"
                "（它看不到父任务的对话，需要自包含的说明）。")

        depth = self._depth()
        if depth >= MAX_DEPTH:
            raise SubagentLimitError(
                f"子智能体不能再派生新的子智能体：当前嵌套深度 {depth}，"
                f"上限 {MAX_DEPTH} 层（只有顶层任务可以派发）。"
                f"请直接在当前任务里完成这项拆分，不再派发。")

        sid = self._sid(session_id)
        records = self._by_session.setdefault(sid, {})
        running = [r for r in records.values() if r.running]
        if len(running) >= self.max_concurrent:
            ids = "、".join(r.agent_id for r in sorted(running, key=lambda r: r.seq))
            raise SubagentLimitError(
                f"本会话同时运行的子智能体已达上限（{len(running)}/{self.max_concurrent}）："
                f"{ids}。请先用 subagent_stop 停止不再需要的，或用 subagent_list "
                f"查看进度、等它们结束后再派发。")

        # 先清理超量旧结果，避免"结果表无限涨"；运行中的记录永不被清
        self._prune(sid)

        clone = getattr(parent, "clone_for_session", None)
        if not callable(clone):
            raise SubagentError(
                "父会话 Agent 不支持派生子智能体（缺少 clone_for_session）——"
                "这是运行时接线问题，不是本次调用的参数问题，请报告给开发者。")
        try:
            child = clone()
        except Exception as e:
            raise SubagentError(
                f"派生子智能体失败（父会话无法克隆执行态）："
                f"{type(e).__name__}: {e}") from e
        if child is None:                      # pragma: no cover - 契约防御
            raise SubagentError(
                "派生子智能体失败：clone_for_session() 返回了 None ——"
                "这是运行时实现 bug，不是本次调用的参数问题，请报告给开发者。")

        seq = next(_seq)
        agent_id = f"sa{seq}"
        steps = _as_int(max_steps, DEFAULT_MAX_STEPS) or DEFAULT_MAX_STEPS
        steps = min(steps, MAX_MAX_STEPS)
        tokens = min(_as_int(max_tokens, 0), MAX_MAX_TOKENS)
        tools = [str(t) for t in allowed_tools] if allowed_tools is not None else None
        title = str(title or "").strip() or derive_title(body)
        record = SubagentRecord(
            seq=seq,
            agent_id=agent_id,
            session_id=sid,
            title=title[:120],
            task=body,
            allowed_tools=tools,
            max_steps=steps,
            max_tokens=tokens,
            parent=parent,
            child=child,
            sink=self._resolve_sink(parent),
            approval_callback=(
                approval_callback if approval_callback is not None
                else getattr(parent, "approval_callback", None)),
        )
        records[agent_id] = record
        self._index[agent_id] = record

        # 先发 started 再创建任务：保证界面看到的事件顺序是
        # "started → update → done"，不会出现先收进度后收派发的情况
        await self._emit(record, "subagent_started", {
            "agent_id": agent_id,
            "title": record.title,
            "task": _clip(record.task, EVENT_TASK_CHARS),
            "status": record.status,
        })
        record.task_obj = asyncio.create_task(
            self._run(record), name=f"automind-subagent-{agent_id}")
        logger.info("subagent_started", agent_id=agent_id, session=sid,
                    title=record.title, max_steps=record.max_steps,
                    max_tokens=record.max_tokens,
                    tools=("safe_only" if tools is None else ",".join(tools)))
        return agent_id

    async def _run(self, record: SubagentRecord) -> None:
        """后台执行体：所有异常都收敛到记录上，绝不逸出到事件循环。"""
        try:
            result = await record.child.run_subtask(
                record.task,
                allowed_tools=record.allowed_tools,
                max_steps=record.max_steps,
                max_tokens=record.max_tokens,
                event_sink=self._make_child_sink(record),
                approval_callback=record.approval_callback,
            )
        except asyncio.CancelledError:
            # 取消是 stop() 的效果，不是故障：终态记 cancelled，并且**照样把
            # done 事件发出去** —— 否则界面上的子智能体会永远停在"运行中"。
            record.status = "cancelled"
            record.error = record.error or "子智能体已被用户停止"
            record.stop_reason = record.stop_reason or "cancelled"
            record.finished_at = record.finished_at or time.time()
            try:
                await self._emit_done(record)
            finally:
                self._prune(record.session_id)
            logger.info("subagent_cancelled", agent_id=record.agent_id,
                        elapsed_s=record.elapsed_s, steps=record.steps)
            raise
        except Exception as e:
            # 子任务抛错（模型连不上 / 工具异常 / run_subtask 自身 bug）——
            # 不能拖垮父任务，也不能当成功；记进记录并通过 done 事件如实交代
            record.status = "error"
            record.error = f"{type(e).__name__}: {e}"
            record.stop_reason = record.stop_reason or "error"
            logger.warning("subagent_failed", agent_id=record.agent_id,
                           session=record.session_id, error=record.error)
        else:
            self._apply_result(record, result)

        record.finished_at = record.finished_at or time.time()
        # shield：收尾瞬间恰好撞上 stop() 时，终态事件仍要发完；
        # 取消本身继续向上传播（任务该被取消还是取消）。
        try:
            await asyncio.shield(self._emit_done(record))
        finally:
            self._prune(record.session_id)

    def _apply_result(self, record: SubagentRecord, result: Any) -> None:
        """把 ``run_subtask`` 的返回值收敛成终态；契约不符按失败处理、不装成功。"""
        if record.stop_requested:
            # 用户点了停止，但子任务在取消送达前刚好跑完：终态必须是 cancelled
            # （否则用户会以为没停掉）。已产出的内容仍然保留，如实告知是部分结果。
            record.status = "cancelled"
            record.error = record.error or "子智能体已被用户停止"
            if isinstance(result, dict):
                record.output = _clip(_as_text(result.get("output")), OUTPUT_KEEP_CHARS)
                record.output_tail = _tail(record.output, OUTPUT_TAIL_CHARS)
                record.steps = _as_int(result.get("steps"), record.steps)
                record.tokens = _as_int(result.get("tokens"), record.tokens)
            return

        if not isinstance(result, dict):
            record.status = "failed"
            record.output = _clip(_as_text(result), OUTPUT_KEEP_CHARS)
            record.error = (
                "run_subtask 返回值不符合契约（期望 dict："
                "success/output/steps/tokens/error，实际 "
                f"{type(result).__name__}）—— 这是运行时实现 bug，"
                "不是本次任务的失败原因，请报告给开发者。")
            return

        success = result.get("success")
        error = _as_text(result.get("error")).strip()
        output = _as_text(result.get("output"))
        stop_reason = _as_text(result.get("stop_reason")).strip()
        resumable = bool(result.get("resumable"))
        if success is None:
            # 没给 success 就按"有没有 error"判定：报错即失败，没报错当成功
            success = not error and stop_reason != "budget_exhausted"
        record.steps = _as_int(result.get("steps"), record.steps)
        record.tokens = _as_int(result.get("tokens"), record.tokens)
        record.stop_reason = stop_reason
        record.output = _clip(output, OUTPUT_KEEP_CHARS)
        if output:
            record.output_tail = _tail(output, OUTPUT_TAIL_CHARS)
        if not success and stop_reason == "budget_exhausted":
            # v1.11.16：预算耗尽**不是**普通失败，也不是完成 —— 明确记为
            # budget_exhausted + resumable，并把"已有部分输出 + 可以重新派发"
            # 写进 error，父模型据此接着做，而不是把半截结果当成功。
            record.status = "budget_exhausted"
            record.resumable = True
            record.error = error or (
                "子智能体 Token 预算已用尽：上面是已产出的部分结果；"
                "可重新派发一个子任务接着做（不要当成已完成）。")
            return
        record.resumable = bool(resumable)
        record.error = error
        if success:
            record.status = "done"
        else:
            record.status = "failed"
            if not record.error:
                record.error = ("子智能体报告失败，但没有给出原因"
                                "（run_subtask 返回 success=False 且 error 为空）。")

    # ── 子任务进度 ─────────────────────────────────────────

    def _make_child_sink(self, record: SubagentRecord) -> Any:
        """构造传给 ``run_subtask(event_sink=...)`` 的进度回调桥。

        约定（与 ``AutoMindAgent.run_subtask`` 的接线契约）：子任务在步骤边界
        调用 ``event_sink(payload)``，payload 是 dict，支持这些键（都可选）：

          · ``output`` / ``output_tail`` / ``text``：**最新完整输出**
            （不是增量片段；管理器直接替换而不是拼接，避免重复累积）；
          · ``steps`` / ``iterations``：已执行步数；
          · ``tokens``：已用 token 数；
          · ``status``：可选的状态说明（仅记日志，不入事件字段）。

        非 dict 的 payload（例如一段字符串）按"最新输出"处理 —— 让子任务实现
        不必为了推一条进度而构造完整字典。

        返回的是 :class:`_ChildEventBridge` 而不是裸 ``async def``：它同时兼容
        ``await event_sink(payload)`` 与 ``event_sink(payload)`` 两种写法 ——
        记录更新是同步生效的，事件推送以可等待的任务给出。接线方不必先读文档
        才能写对，也就不会因为少写一个 ``await`` 把进度事件整条丢掉。
        """
        return _ChildEventBridge(self, record)

    def _absorb_child_event(self, record: SubagentRecord, payload: Any) -> bool:
        """把一条进度合并进记录；返回是否应当推 ``subagent_update``（含节流）。

        同步部分单独拆出来，是为了让"同步调用 event_sink"也能立即更新记录 ——
        事件可以晚到，但 ``subagent_list`` 看到的步数/输出尾部必须是真的。
        """
        if not record.running:
            return False                       # 终态后的迟到进度直接丢弃（无意义）
        tail = ""
        steps: Any = None
        tokens: Any = None
        if isinstance(payload, dict):
            tail = (payload.get("output_tail") or payload.get("output")
                    or payload.get("text") or payload.get("content") or "")
            steps = payload.get("steps", payload.get("iterations"))
            tokens = payload.get("tokens")
            if payload.get("status"):
                logger.debug("subagent_progress", agent_id=record.agent_id,
                             status=str(payload.get("status"))[:80])
        elif payload is not None:
            tail = str(payload)

        if steps is None and tokens is None and not tail:
            # 既没有输出也没有计数（例如只带一个 type 的内部事件）→ 不是进度，
            # 忽略掉，免得每步都推一条内容为空的 subagent_update 刷屏
            return False

        if steps is not None:
            record.steps = _as_int(steps, record.steps)
        if tokens is not None:
            record.tokens = _as_int(tokens, record.tokens)
        if tail:
            text = _as_text(tail)
            # 子任务推的是"最新完整输出"，因此替换而不是拼接
            record.output = _clip(text, OUTPUT_KEEP_CHARS)
            record.output_tail = _tail(text, OUTPUT_TAIL_CHARS)

        now = time.monotonic()
        if now - record.last_update_at < MIN_UPDATE_INTERVAL_S:
            return False                       # 节流：记录照更新，事件不刷屏
        record.last_update_at = now
        return True

    async def _emit_update(self, record: SubagentRecord) -> None:
        await self._emit(record, "subagent_update", {
            "agent_id": record.agent_id,
            "status": record.status,
            "output_tail": record.output_tail,
            "steps": int(record.steps),
            "tokens": int(record.tokens),
        })

    # ── 查询 ───────────────────────────────────────────────

    def list(self, *, session_id: str | None = None) -> list[dict[str, Any]]:
        """当前父会话的子智能体快照，最新的在前。"""
        sid = self._sid(session_id)
        records = self._by_session.get(sid) or {}
        ordered = sorted(records.values(), key=lambda r: (r.started_at, r.seq), reverse=True)
        return [r.snapshot() for r in ordered]

    def get(self, agent_id: str, *, session_id: str | None = None) -> dict[str, Any] | None:
        """单个子智能体的完整快照（含最终 output）；不存在/不属于本会话返回 None。"""
        record = self._find(agent_id, session_id)
        return record.detail() if record is not None else None

    def get_record(self, agent_id: str, *,
                   session_id: str | None = None) -> SubagentRecord | None:
        """取内部记录（服务端/测试需要 task 句柄时用；工具层不要用）。"""
        return self._find(agent_id, session_id)

    def _find(self, agent_id: str, session_id: str | None) -> SubagentRecord | None:
        aid = str(agent_id or "").strip()
        if not aid:
            return None
        record = self._index.get(aid)
        if record is None:
            return None
        if record.session_id != self._sid(session_id):
            # 跨会话访问一律当"不存在"：既防止标签页之间互相看到/停止对方
            # 的子智能体，也让错误消息不泄露其它会话的 id 是否存在。
            return None
        return record

    def _running_ids(self, session_id: str | None) -> list[str]:
        sid = self._sid(session_id)
        records = self._by_session.get(sid) or {}
        running = sorted((r for r in records.values() if r.running), key=lambda r: r.seq)
        return [r.agent_id for r in running]

    def _not_found(self, agent_id: str, session_id: str | None) -> str:
        ids = self._running_ids(session_id)
        hint = (f"当前会话正在运行的子智能体：{'、'.join(ids)}。"
                if ids else "当前会话没有正在运行的子智能体。")
        return (f"没有找到子智能体 '{agent_id}'：可能写错了 id、它属于其它会话，"
                f"或已超出最近 {self.max_results} 条的结果保留上限。{hint}")

    # ── 追加消息 ───────────────────────────────────────────

    async def send(self, agent_id: str, message: str,
                   *, session_id: str | None = None) -> dict[str, Any]:
        """给**仍在运行**的子智能体追加一句要求（语义同用户中途插话）。

        注意「收下 ≠ 生效」：这里只把消息放进子 Agent 的插话队列，子任务在
        下一个步骤边界才会读到它。所以回执刻意写成 ``queued`` 而不是
        ``applied`` —— 父模型据此知道"还要再查一次列表确认"。
        """
        record = self._find(agent_id, session_id)
        if record is None:
            return {"ok": False, "error": self._not_found(agent_id, session_id)}
        body = str(message or "").strip()
        if not body:
            return {"ok": False, "agent_id": record.agent_id,
                    "error": "追加的消息不能为空：请写清要补充/更正的内容。"}
        if not record.running:
            return {"ok": False, "agent_id": record.agent_id, "status": record.status,
                    "error": (f"子智能体 {record.agent_id} 已经结束（{record.status}），"
                              f"无法再追加消息。请用 subagent_list 读取它的结果；"
                              f"若还需要它继续工作，请用 subagent 重新派发一个新任务。")}
        interject = getattr(record.child, "interject", None)
        if not callable(interject):
            return {"ok": False, "agent_id": record.agent_id, "status": record.status,
                    "error": ("该子智能体不支持中途追加消息（子 Agent 缺少 interject）——"
                              "这是运行时实现问题，请报告给开发者；"
                              "仍可用 subagent_stop 停止它、用 subagent_list 查结果。")}
        try:
            interject(body)
        except Exception as e:
            # InterjectionTooLong 等拒绝原因必须原样回给调用方：静默吞掉的话，
            # 父模型会以为自己"补充成功"继续往下做
            return {"ok": False, "agent_id": record.agent_id, "status": record.status,
                    "error": f"追加消息未被接受：{type(e).__name__}: {e}"}
        logger.info("subagent_interjection", agent_id=record.agent_id, chars=len(body))
        return {
            "ok": True,
            "agent_id": record.agent_id,
            "status": record.status,
            "queued": True,
            "note": ("消息已放进子智能体的插话队列，它在下一个步骤边界读取。"
                     "收下 ≠ 已并入：请用 subagent_list 观察进度；"
                     "如果它刚好结束，这条消息不会生效。"),
        }

    # ── 停止 ───────────────────────────────────────────────

    async def stop(self, agent_id: str, *,
                   session_id: str | None = None) -> dict[str, Any]:
        """取消一个仍在运行的子智能体（幂等：已结束的返回 already_finished）。"""
        record = self._find(agent_id, session_id)
        if record is None:
            return {"ok": False, "error": self._not_found(agent_id, session_id)}
        task = record.task_obj
        if not record.running or task is None or task.done():
            return {"ok": True, "agent_id": record.agent_id, "status": record.status,
                    "already_finished": True,
                    "note": (f"子智能体已经结束（{record.status}），无需停止；"
                             f"结果可用 subagent_list 读取。")}
        # 先立旗再取消：取消可能晚于子任务自然结束，终态要按"用户停的"算
        record.stop_requested = True
        record.error = record.error or "子智能体已被用户停止"
        task.cancel()
        try:
            await asyncio.wait({task}, timeout=STOP_WAIT_S)
        except asyncio.CancelledError:
            # 当前工具调用自己被取消（服务关闭 / 上层停止）：停止请求已发出，
            # 取消必须继续向上传播，不能当成"停止完成"返回成功
            raise
        if not task.done():
            logger.warning("subagent_stop_timeout", agent_id=record.agent_id,
                           timeout_s=STOP_WAIT_S)
            return {
                "ok": True,
                "agent_id": record.agent_id,
                "status": record.status,
                "stop_requested": True,
                "note": (f"已发出停止请求，但子智能体没有在 {STOP_WAIT_S:.0f} 秒内结束；"
                         f"取消要到下一个可中断点才生效，它可能仍在收尾。"
                         f"稍后用 subagent_list 确认状态。"),
            }
        # 取消可能落在子任务协程体**开始之前**（stop 紧跟在 start 之后）：
        # 此时 _run 的取消分支根本不会执行，记录会永远停在 running —— 在这里兜底结算。
        await self._settle_if_stuck(record)
        note = ("子智能体已停止（停止前已产出的内容保留在结果里）。"
                if record.status == "cancelled"
                else f"子智能体已结束（{record.status}），停止请求没有改变终态。")
        return {"ok": True, "agent_id": record.agent_id, "status": record.status,
                "stop_requested": True, "note": note}

    # ── 会话收尾 ───────────────────────────────────────────

    async def cancel_session(self, *, session_id: str | None = None) -> int:
        """取消某父会话下所有仍在运行的子智能体，返回处理条数。

        为什么需要它：子智能体是**后台任务**，父任务跑完后没有人会再等它们 ——
        不收尾的话它们会继续烧 token，产出的结果也没人读（服务端/会话关闭时
        由接线方调用；取消仍会走正常的终态路径，界面能收到 cancelled 的 done
        事件，不会留下"永远运行中"的幻觉）。
        """
        sid = self._sid(session_id)
        records = [r for r in (self._by_session.get(sid) or {}).values() if r.running]
        if not records:
            return 0
        for record in records:
            record.stop_requested = True
            record.error = record.error or "父任务已结束，子智能体随之停止"
            if record.task_obj is not None and not record.task_obj.done():
                record.task_obj.cancel()
        tasks = [r.task_obj for r in records if r.task_obj is not None]
        if tasks:
            try:
                await asyncio.wait(tasks, timeout=STOP_WAIT_S)
            except asyncio.CancelledError:
                raise
        for record in records:
            await self._settle_if_stuck(record)
        logger.info("subagent_session_cancelled", session=sid, count=len(records))
        return len(records)

    async def _settle_if_stuck(self, record: SubagentRecord) -> None:
        """任务在 ``_run`` 入口之前就被取消时补结算（否则记录永远停在 running）。

        取消一个"还没轮到执行"的 asyncio.Task 不会进入协程体：``_run`` 里的
        取消分支看不到它，记录状态就会永远停在 running —— 界面显示"运行中"、
        结果列表里却什么都没有。这里按取消语义补终态并补发 done 事件。
        """
        if not record.running:
            return
        record.status = "cancelled"
        record.error = record.error or "子智能体已被用户停止"
        record.finished_at = record.finished_at or time.time()
        await self._emit_done(record)
        self._prune(record.session_id)

    # ── 事件 / 清理 ────────────────────────────────────────

    async def _emit(self, record: SubagentRecord, event_type: str,
                    fields: dict[str, Any]) -> None:
        """推一条子智能体事件；推送失败只记日志，绝不影响子任务执行。"""
        sink = self._emit_override or record.sink
        if sink is None:
            return
        event = {"type": event_type, **fields, "session_id": record.session_id}
        try:
            result = sink(event)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 界面"子智能体面板不动"的投诉基本都出在这里；不能静默
            logger.warning("subagent_event_emit_failed", agent_id=record.agent_id,
                           event_type=event_type, error=f"{type(e).__name__}: {e}")

    async def _emit_done(self, record: SubagentRecord) -> None:
        await self._emit(record, "subagent_done", {
            "agent_id": record.agent_id,
            "title": record.title,
            "status": record.status,
            "output": _clip(record.output, EVENT_OUTPUT_CHARS),
            "error": record.error or None,
            "steps": int(record.steps),
            "tokens": int(record.tokens),
            # v1.11.16：预算耗尽等"可重试"结局要如实说明，父级/界面据此
            # 重新派发而不是把它当成已完成
            "stop_reason": record.stop_reason,
            "resumable": bool(record.resumable),
        })

    def _prune(self, session_id: str) -> None:
        """只清理**已结束**的旧结果，保留运行中的与最近的 ``max_results`` 条。"""
        records = self._by_session.get(session_id)
        if not records:
            return
        finished = [r for r in records.values() if not r.running]
        overflow = len(finished) - self.max_results
        if overflow <= 0:
            return
        finished.sort(key=lambda r: (r.finished_at or 0.0, r.seq))
        for record in finished[:overflow]:
            records.pop(record.agent_id, None)
            self._index.pop(record.agent_id, None)
        logger.info("subagent_results_pruned", session=session_id, pruned=overflow)

    def reset_for_tests(self) -> None:
        """清空内存状态（仅测试用；不取消正在跑的任务，调用方自己收尾）。"""
        self._by_session.clear()
        self._index.clear()


class _ChildEventBridge:
    """``run_subtask(event_sink=...)`` 的进度回调对象（同时兼容两种调用方式）。

    为什么不是普通 ``async def``：接线方少写一个 ``await`` 时，普通协程会触发
    "coroutine was never awaited" 并且**进度事件全丢**（父界面看不到任何输出）。
    这里把"更新记录"做成同步立即生效、"推送事件"包装成可等待任务返回 ——
    两种写法都能工作，写错也不会静默丢数据。
    """

    __slots__ = ("_manager", "_record")

    def __init__(self, manager: SubagentManager, record: SubagentRecord) -> None:
        self._manager = manager
        self._record = record

    def __call__(self, payload: Any) -> Any:
        record = self._record
        if not self._manager._absorb_child_event(record, payload):
            return self._noop()                # 节流/终态：返回可 await 的空操作
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:                   # pragma: no cover - 不在事件循环里
            logger.warning("subagent_progress_outside_loop", agent_id=record.agent_id)
            return None
        return loop.create_task(self._manager._emit_update(record))

    @staticmethod
    def _noop() -> Any:
        """一个已完成的 awaitable：节流时 ``await event_sink(...)`` 也必须成立。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:                   # pragma: no cover - 同步无事件循环
            return None
        future = loop.create_future()
        future.set_result(None)
        return future


# ═══════════════════════════════════════════════════════════
# 进程级单例（工具注册表在会话克隆间共享，管理器也必须只有一个）
# ═══════════════════════════════════════════════════════════

_default_manager: SubagentManager | None = None


def default_manager() -> SubagentManager:
    """进程级共享管理器 —— 所有 ``subagent*`` 工具都经它操作同一份注册表。"""
    global _default_manager
    if _default_manager is None:
        _default_manager = SubagentManager()
    return _default_manager


def reset_for_tests() -> None:
    """重置进程级管理器（仅测试用；下一个 default_manager() 会新建实例）。"""
    global _default_manager
    _default_manager = None


async def cancel_session(session_id: str | None = None) -> int:
    """取消该会话下所有仍在运行的子智能体（父任务结束/会话关闭时调用）。"""
    return await default_manager().cancel_session(session_id=session_id)


__all__ = [
    "DEFAULT_MAX_STEPS",
    "DEFAULT_TITLE_CHARS",
    "EVENT_OUTPUT_CHARS",
    "EVENT_TASK_CHARS",
    "KEEP_RESULTS",
    "MAX_CONCURRENT",
    "MAX_DEPTH",
    "MAX_MAX_STEPS",
    "MAX_MAX_TOKENS",
    "MIN_UPDATE_INTERVAL_S",
    "OUTPUT_KEEP_CHARS",
    "OUTPUT_TAIL_CHARS",
    "STOP_WAIT_S",
    "SubagentError",
    "SubagentLimitError",
    "SubagentManager",
    "SubagentRecord",
    "cancel_session",
    "current_agent",
    "current_depth",
    "current_event_sink",
    "current_session_id",
    "default_manager",
    "derive_title",
    "reset_for_tests",
]
