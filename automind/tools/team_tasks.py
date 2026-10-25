"""团队共享任务看板的 AI 工具 —— ``team_task``（v1.11.18）。

为什么需要它：v1.11.15 起团队任务看板（SQLite + Web 看板 + REST）就是社区能力，
但模型**没有任何入口** —— 用户能在界面上看到共享任务，Agent 却既看不见也认领
不了。补上这个工具后，"让 Agent 领一个共享任务、报进度、留结论"才真正成立。

设计原则：
  · **复用同一套领域逻辑**：所有增删改查都走 :mod:`automind.core.team` 的纯逻辑
    + :class:`~automind.core.team.TeamStore`，不在这里另写一套规则（否则 Web /
    REST / AI 三条路径会慢慢长出不同的状态机）；
  · **存储可注入**：测试注入内存 TeamStore，生产构造时给 ``store=None`` 走默认
    的 SQLite；CLI/单测没有数据目录也不会在"构造工具"这一步就炸；
  · **actor 尽量解析成人**：会话 sid → 持久化昵称 → 在线昵称 → 兜底
    ``会话 <sid>`` / ``AutoMind``。昵称缺失是 CLI/单测里的常态，不能让工具失败；
  · **回执紧凑且可继续操作**：列表只给关键字段 + 计数 + 下一步提示，写操作只回
    该任务精简视图 —— 看板数据（尤其 comments/activity）全量塞回上下文只会把
    模型自己的预算挤没；
  · **变更后 best-effort 广播**：Web 看板靠 ``team_activity`` 事件实时刷新，
    但广播失败/模块未加载绝不能升级成工具失败（CLI 里压根没有 server）。
"""

from __future__ import annotations

import inspect
import sys
from typing import Any

from automind.core import session_ctx, team, team_presence
from automind.core.logging import get_logger
from automind.core.types import PermissionTier, ToolResult
from automind.tools._toolkit import bad, ok
from automind.tools.base import AbstractTool

logger = get_logger("automind.tool.team_task")

#: 允许的 action（模型写错时原样报回允许值，省一次重试）
ACTIONS: tuple[str, ...] = ("list", "summary", "create", "update", "claim", "comment")

#: 列表默认/最大条数。看板最多 200 条，但一次性回给模型既占上下文又没必要：
#: 模型先看 20 条找到自己关心的那几条就够了，更多要用筛选（q / filter_status）。
DEFAULT_LIMIT = 20
MAX_LIMIT = 50

#: 写操作失败后的通用重试提示（错误文本要能"照着做"，不能只说失败了）
_RETRY_HINT = "可用 action='list' 查看最新任务后重试。"


class _ParamError(ValueError):
    """参数不合法（与存储/领域的 ``TeamError`` 分开：前者是模型能直接改的）。"""


# ═══════════════════════════════════════════════════════════
# 小工具
# ═══════════════════════════════════════════════════════════


def _text(value: Any) -> str:
    return str(value or "").strip()


def _brief(task: dict) -> dict[str, Any]:
    """给模型看的任务精简视图（不含 comments/activity 正文，只给评论数）。"""
    return {
        "id": _text(task.get("id")),
        "title": _text(task.get("title"))[:120],
        "status": _text(task.get("status")),
        "priority": _text(task.get("priority")),
        "assignee": _text(task.get("assignee")),
        "due": _text(task.get("due")),
        "tags": list(task.get("tags") or []),
        "comments": len(task.get("comments") or []),
        "updated": _text(task.get("updated")),
    }


def _limit_of(raw: Any) -> int:
    """解析 limit：缺省 20、上限 50；越界/非整数给出可照着改的错误。"""
    if raw is None or raw == "":
        return DEFAULT_LIMIT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise _ParamError(
            f"limit 必须是 1~{MAX_LIMIT} 的整数（收到 {raw!r}）。") from None
    if value < 1:
        raise _ParamError(
            f"limit 必须大于 0（收到 {value}）；允许范围 1~{MAX_LIMIT}。")
    return min(value, MAX_LIMIT)


def _choice(raw: Any, allowed: tuple[str, ...], field: str) -> str:
    """校验枚举参数；返回规范化小写值，非法值直接抛可执行错误。"""
    text = _text(raw).lower()
    if not text:
        return ""
    if text not in allowed:
        raise _ParamError(
            f"{field} 必须是 {'/'.join(allowed)}（收到 {raw!r}）。"
            "请改成允许的取值后重试。")
    return text


def _resolve_actor() -> str:
    """当前操作人：会话 sid → 持久化昵称 → 在线昵称 → 兜底。

    为什么一路兜底到 ``会话 <sid>``：actor 只用于活动记录/广播署名，不是权限
    凭据；为了"名字没设"就拒绝一次工具调用，会让 CLI 用户完全用不了团队看板。
    """
    sid = ""
    try:
        sid = _text(session_ctx.session_id())
    except Exception:                              # pragma: no cover - 防御性
        sid = ""
    if sid:
        try:
            names = team.load_member_names()
        except Exception:                          # pragma: no cover - 防御性
            names = {}
        if names.get(sid):
            return str(names[sid])[: team.MAX_NAME]
        try:
            for member in team_presence.list_members():
                if _text(member.get("sid")) == sid and _text(member.get("name")):
                    return str(member["name"])[: team.MAX_NAME]
        except Exception:                          # pragma: no cover - 防御性
            pass
        return f"会话 {sid}"[: team.MAX_NAME]
    return "AutoMind"


# ═══════════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════════


class TeamTaskTool(AbstractTool):
    """团队共享任务看板：list / summary / create / update / claim / comment。"""

    name = "team_task"
    description = (
        "The TEAM's shared task board (the same board shown in the Web team view). "
        "Actions: 'list' (default) shows tasks with filters — ALWAYS list first to "
        "check whether the work is already tracked and who owns it; 'summary' gives "
        "counts per status/priority, overdue and completion rate; 'create' adds a "
        "task (title required); 'update' changes status/priority/assignee/due/title/"
        "desc/tags; 'claim' takes a task (assignee=member, status=doing); 'comment' "
        "leaves a note on a task. Tasks are shared by everyone: do not reassign, "
        "close or rewrite tasks that are not yours — claim your own work, update "
        "your progress, and comment your result before marking it done."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(ACTIONS),
                "description": (
                    "list | summary | create | update | claim | comment. "
                    "list first to avoid duplicating existing work."),
            },
            "task_id": {
                "type": "string",
                "description": "Target task id (required for update/claim/comment).",
            },
            "title": {"type": "string",
                      "description": "Task title (required for create; can be changed by update)."},
            "desc": {"type": "string", "description": "Task description / acceptance notes."},
            "status": {
                "type": "string",
                "enum": list(team.STATUSES),
                "description": "todo | doing | blocked | done (create default todo).",
            },
            "priority": {
                "type": "string",
                "enum": list(team.PRIORITIES),
                "description": "low | normal | high | urgent (default normal).",
            },
            "assignee": {"type": "string",
                         "description": "Person responsible (member nickname)."},
            "due": {"type": "string",
                    "description": "Due date as YYYY-MM-DD (optional; empty string clears it)."},
            "tags": {
                "description": ("Tags as an array of strings or a comma-separated "
                                "string (e.g. \"frontend,ui\")."),
            },
            "text": {"type": "string", "description": "Comment body (required for comment)."},
            "member": {"type": "string",
                       "description": "Who claims the task (defaults to the current actor)."},
            "q": {"type": "string",
                  "description": "Keyword filter for list (matches title/desc/tags/assignee)."},
            "filter_status": {
                "type": "string",
                "enum": list(team.STATUSES),
                "description": "Only list tasks in this status.",
            },
            "limit": {
                "type": "integer",
                "description": f"Max tasks returned by list (default {DEFAULT_LIMIT}, max {MAX_LIMIT}).",
            },
        },
        "required": ["action"],
    }
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 40

    def __init__(self, store: team.TeamStore | None = None) -> None:
        #: 显式注入的存储（测试/多实例场景）；None = 每次操作时用默认 SQLite store。
        #: **不在构造函数里建 store**：TeamStore() 本身很轻，但这里保持"构造永远
        #: 不碰磁盘"的契约，Agent 注册工具时不会因为数据目录不可用而失败。
        self.store = store

    # ── 存储 / 广播 ────────────────────────────────────────

    def _store(self) -> team.TeamStore:
        return self.store if self.store is not None else team.TeamStore()

    async def _broadcast(self, kind: str, task: dict, actor: str,
                         detail: str = "") -> None:
        """变更后 best-effort 广播 ``team_activity``，让 Web 看板实时刷新。

        kind 用 server 侧既有取值（``task_created`` / ``task_updated`` /
        ``task_moved`` / ``task_claimed`` / ``task_commented``）—— 前端只对这几个
        名字刷新看板，用别的字符串会"广播成功但界面不动"。
        """
        server_mod = sys.modules.get("automind.server")
        broadcast = getattr(server_mod, "_broadcast_team_activity", None) \
            if server_mod is not None else None
        if not callable(broadcast):
            # CLI / 单测里根本没有 server 模块：跳过，不影响写操作本身
            return
        try:
            sid = _text(session_ctx.session_id())
        except Exception:                          # pragma: no cover - 防御性
            sid = ""
        try:
            result = broadcast(kind, task=task, sid=sid, actor=actor, detail=detail)
            # server 的实现是 async；测试里的假模块可能同步——两种都支持，
            # 否则一个同步桩就会让"广播不失败"变成"广播把工具弄失败"。
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            # 广播是旁路：看板没刷新是小事，把已经写成功的任务报成失败才是大事
            logger.debug("team_task_broadcast_failed", kind=kind,
                         error=f"{type(e).__name__}: {e}")

    # ── 入口 ───────────────────────────────────────────────

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = _text(kwargs.get("action")).lower() or "list"
        if action not in ACTIONS:
            return bad(
                self.name,
                f"未知的 action：{action!r}。允许的动作：{' / '.join(ACTIONS)}。"
                "先 list 查重，再 create/claim/update/comment。")
        actor = _resolve_actor()
        try:
            store = self._store()
        except Exception as e:
            return bad(
                self.name,
                f"团队任务存储不可用：{type(e).__name__}: {e}。"
                "这是一次独立操作，不会影响其它工具；请报告环境问题"
                "（数据目录是否可写 / SQLite 是否可用）。")
        try:
            if action == "list":
                return await self._action_list(store, kwargs, actor)
            if action == "summary":
                return await self._action_summary(store, kwargs, actor)
            if action == "create":
                return await self._action_create(store, kwargs, actor)
            if action == "update":
                return await self._action_update(store, kwargs, actor)
            if action == "claim":
                return await self._action_claim(store, kwargs, actor)
            return await self._action_comment(store, kwargs, actor)
        except _ParamError as e:
            # 参数错误是模型能直接改的：原样回给模型，不当作"存储故障"
            return bad(self.name, str(e))
        except team.TaskNotFound as e:
            return bad(self.name, self._not_found_hint(store, str(e)))
        except team.TeamError as e:
            return bad(self.name, f"{e}。{_RETRY_HINT}")
        except Exception as e:                     # pragma: no cover - 防御性
            logger.warning("team_task_failed", action=action,
                           error=f"{type(e).__name__}: {e}")
            return bad(self.name,
                       f"团队任务操作失败：{type(e).__name__}: {e}。{_RETRY_HINT}")

    # ── list ───────────────────────────────────────────────

    async def _action_list(self, store: team.TeamStore, kwargs: dict[str, Any],
                           actor: str) -> ToolResult:
        status = _choice(
            kwargs.get("filter_status") or kwargs.get("status"), team.STATUSES,
            "filter_status")
        keyword = _text(kwargs.get("q") or kwargs.get("query"))
        limit = _limit_of(kwargs.get("limit"))
        tasks = store.load()
        filtered = team.filter_tasks(tasks, status=status or None,
                                     q=keyword or None)
        summary = team.summarize(tasks)
        items = [_brief(t) for t in filtered[:limit]]
        payload: dict[str, Any] = {
            "count": len(filtered),
            "returned": len(items),
            "tasks": items,
            "summary": {
                "total": summary["total"],
                "by_status": summary["by_status"],
                "overdue": summary["overdue"],
                "done_today": summary["done_today"],
            },
            "actor": actor,
        }
        if not filtered:
            payload["note"] = (
                "没有匹配的任务。若这项工作还没有人登记，可用 action='create' "
                "新建（title 必填；assignee 建议填自己）。")
        elif len(filtered) > len(items):
            payload["note"] = (
                f"仅显示前 {len(items)} 条（共 {len(filtered)} 条）。"
                "可用 q / filter_status 缩小范围，或加大 limit（上限 "
                f"{MAX_LIMIT}）。认领用 action='claim'（task_id=...），"
                "改状态用 action='update'，留痕用 action='comment'。")
        else:
            payload["note"] = (
                "认领任务用 action='claim'（task_id=...）；改状态/负责人用 "
                "action='update'；留结论用 action='comment'。"
                "不要替别人改或关掉不属于你的任务。")
        return ok(self.name, **payload)

    # ── summary ────────────────────────────────────────────

    async def _action_summary(self, store: team.TeamStore, kwargs: dict[str, Any],
                              actor: str) -> ToolResult:
        summary = team.summarize(store.load())
        # 按人负载只给前 10 名：看板人数再多，模型需要的是"谁最忙/还差谁"，
        # 不是一份完整通讯录（那会把上下文白白吃掉）。
        people = summary["by_assignee"][:10]
        payload: dict[str, Any] = {
            "total": summary["total"],
            "by_status": summary["by_status"],
            "by_priority": summary["by_priority"],
            "overdue": summary["overdue"],
            "done_today": summary["done_today"],
            "done_7d": summary["done_7d"],
            "completion_rate": summary["completion_rate"],
            "by_assignee": people,
            "recent_activity": summary["recent_activity"][:5],
            "actor": actor,
            "note": (
                "summary 反映整个团队看板（不是过滤后的子集）。"
                "用 action='list' 看具体任务，用 action='claim' 领活。")
        }
        if len(summary["by_assignee"]) > len(people):
            payload["note"] += f" 按人负载仅显示前 {len(people)} 名。"
        return ok(self.name, **payload)

    # ── create ─────────────────────────────────────────────

    async def _action_create(self, store: team.TeamStore, kwargs: dict[str, Any],
                             actor: str) -> ToolResult:
        title = _text(kwargs.get("title"))
        if not title:
            raise _ParamError(
                "title 不能为空：请写一句能看懂的任务标题"
                "（例如「补齐登录页错误提示」），并说明期望结果。")
        status = _choice(kwargs.get("status"), team.STATUSES, "status")
        priority = _choice(kwargs.get("priority"), team.PRIORITIES, "priority")
        due = self._due_of(kwargs)
        tags = team.normalize_tags(kwargs.get("tags"))
        task = store.create(
            title=title,
            desc=_text(kwargs.get("desc")),
            assignee=_text(kwargs.get("assignee")),
            priority=priority or team.DEFAULT_PRIORITY,
            due=due,
            tags=tags,
            creator=actor,
            status=status or team.DEFAULT_STATUS,
            actor=actor,
        )
        await self._broadcast("task_created", task, actor,
                              detail=f"新建任务「{task['title']}」")
        return ok(
            self.name, task=_brief(task), actor=actor,
            note=(f"任务已创建（id={task['id']}，状态 "
                  f"{task['status']}）。下一步：用 action='update' 推进状态，"
                  "用 action='comment' 留下进展/结论。"))

    # ── update ─────────────────────────────────────────────

    async def _action_update(self, store: team.TeamStore, kwargs: dict[str, Any],
                             actor: str) -> ToolResult:
        tid = self._task_id_of(kwargs)
        patch: dict[str, Any] = {}
        if kwargs.get("title") is not None:
            title = _text(kwargs.get("title"))
            if not title:
                raise _ParamError("title 不能为空：要改标题就请给出新标题。")
            patch["title"] = title
        if kwargs.get("desc") is not None:
            patch["desc"] = _text(kwargs.get("desc"))
        if kwargs.get("status") is not None:
            status = _choice(kwargs.get("status"), team.STATUSES, "status")
            if not status:
                raise _ParamError(
                    f"status 不能为空：只接受 {'/'.join(team.STATUSES)}。")
            patch["status"] = status
        if kwargs.get("priority") is not None:
            priority = _choice(kwargs.get("priority"), team.PRIORITIES, "priority")
            if not priority:
                raise _ParamError(
                    f"priority 不能为空：只接受 {'/'.join(team.PRIORITIES)}。")
            patch["priority"] = priority
        if kwargs.get("assignee") is not None:
            patch["assignee"] = _text(kwargs.get("assignee"))[: team.MAX_NAME]
        if "due" in kwargs and kwargs.get("due") is not None:
            patch["due"] = self._due_of(kwargs)
        if kwargs.get("tags") is not None:
            patch["tags"] = team.normalize_tags(kwargs.get("tags"))
        if not patch:
            raise _ParamError(
                "没有可更新的字段：请至少提供 title/desc/status/priority/"
                "assignee/due/tags 中的一项。")

        old = self._find(store, tid)
        updated = store.update(tid, patch, actor=actor)
        moved = "status" in patch and patch["status"] != old.get("status")
        if moved:
            # 状态变化单独用 task_moved：前端把它当"看板列变了"处理（与 REST 一致）
            kind = "task_moved"
            detail = f"{old.get('status')} → {updated.get('status')}"
        else:
            kind = "task_updated"
            detail = "更新了任务"
        await self._broadcast(kind, updated, actor, detail=detail)
        note = ("任务已更新：" + "、".join(sorted(patch)) + "。")
        if updated.get("status") == "done":
            note += "已完成；建议先用 action='comment' 留下结果摘要（便于别人复核）。"
        else:
            note += "推进进度用 action='update'，留痕用 action='comment'。"
        return ok(self.name, task=_brief(updated), actor=actor, note=note)

    # ── claim ──────────────────────────────────────────────

    async def _action_claim(self, store: team.TeamStore, kwargs: dict[str, Any],
                            actor: str) -> ToolResult:
        tid = self._task_id_of(kwargs)
        member = _text(kwargs.get("member"))[: team.MAX_NAME] or actor
        task = store.claim(tid, member)
        await self._broadcast("task_claimed", task, actor,
                              detail=f"{member} 认领了任务")
        return ok(
            self.name, task=_brief(task), actor=actor,
            note=(f"已认领（负责人={member}，状态 doing）。"
                  "做完后用 action='update'（task_id=..., status='done'）收尾，"
                  "并先用 action='comment' 留下结果摘要。"))

    # ── comment ────────────────────────────────────────────

    async def _action_comment(self, store: team.TeamStore, kwargs: dict[str, Any],
                              actor: str) -> ToolResult:
        tid = self._task_id_of(kwargs)
        body = _text(kwargs.get("text") or kwargs.get("comment"))
        if not body:
            raise _ParamError(
                "text 不能为空：请写清要留下的评论内容"
                "（进展 / 阻塞原因 / 下一步 / 结果摘要）。")
        task = store.comment(tid, body, actor)
        comment = (task.get("comments") or [])[-1]
        await self._broadcast("task_commented", task, actor,
                              detail=f"评论：{body[:60]}")
        return ok(
            self.name, task=_brief(task), actor=actor,
            comment={"author": _text(comment.get("author")),
                     "text": _text(comment.get("text")),
                     "time": _text(comment.get("time"))},
            note="评论已记录。继续推进用 action='update'；全部完成时把状态改为 done。")

    # ── 校验助手 ───────────────────────────────────────────

    @staticmethod
    def _task_id_of(kwargs: dict[str, Any]) -> str:
        tid = _text(kwargs.get("task_id") or kwargs.get("id"))
        if not tid:
            raise _ParamError(
                "task_id 不能为空：请先用 action='list' 找到目标任务，"
                "再把它的 id 传进来。")
        return tid

    @staticmethod
    def _due_of(kwargs: dict[str, Any]) -> str:
        """截止日期：空串=清除；有值必须是可识别的 YYYY-MM-DD（含时间部分也可）。"""
        raw = kwargs.get("due")
        text = _text(raw)
        if not text:
            return ""
        due = team.normalize_due(text)
        if not due:
            raise _ParamError(
                f"due 无法识别：{raw!r}。请用 YYYY-MM-DD（例如 2026-10-31）；"
                "要清除截止日期就传空字符串。")
        return due

    @staticmethod
    def _find(store: team.TeamStore, tid: str) -> dict:
        for task in store.load():
            if task.get("id") == tid:
                return task
        raise team.TaskNotFound(f"任务不存在: {tid}")

    def _not_found_hint(self, store: team.TeamStore, message: str) -> str:
        """未知 task_id 的可操作错误：把现有 id 直接列出来，别让模型去猜/重试。"""
        ids: list[str] = []
        try:
            ids = [_text(t.get("id")) for t in store.load()[:10]]
        except Exception:                          # pragma: no cover - 防御性
            ids = []
        shown = "、".join(i for i in ids if i)
        hint = (f"看板现有任务 id：{shown}。" if shown
                else "当前看板还没有任务（可用 action='create' 新建）。")
        return f"{message}。{hint}请用 action='list' 查看最新任务后重试。"

    def get_execution_plan(self, **kwargs: Any) -> str:
        return f"[team_task] {kwargs.get('action', 'list')} " \
               f"{kwargs.get('task_id') or ''}".rstrip()


__all__ = ["ACTIONS", "DEFAULT_LIMIT", "MAX_LIMIT", "TeamTaskTool"]
