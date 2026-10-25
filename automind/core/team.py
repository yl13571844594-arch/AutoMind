"""团队任务看板的领域逻辑（v1.11.15）。

设计要点
--------
1. **纯逻辑 + 可注入存储**：规范化 / 增删改 / 评论 / 认领 / 过滤 / 摘要全部
   只操作 ``dict`` 与 ``list``，不 import FastAPI，也不直接摸 SQLite。读写经
   :class:`TeamStore` 注入（server 端用 SQLite，测试用内存列表）。
2. **旧数据零迁移**：v1.11.14 之前的任务是
   ``{id,title,assignee,desc,status,creator,created}``；:func:`normalize_task`
   就地补齐 priority/due/tags/comments/activity/updated 等新字段，``id`` 原样
   保留（旧前端与旧接口都靠它定位）。
3. **时间口径**：本地时间字符串（``time.strftime``），不做时区换算 —— 团队
   看板是"同一台服务器、同一群人"的场景，本地时间最直观。日期字段统一
   ``YYYY-MM-DD``，逾期判断按本地日期比较。
4. **摘要可测试**：:func:`summarize` 接受 ``today`` 参数，测试不必依赖系统时钟。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

# ── 常量（与前端、server 的取值保持一致）────────────────────────
STATUSES: tuple[str, ...] = ("todo", "doing", "blocked", "done")
PRIORITIES: tuple[str, ...] = ("low", "normal", "high", "urgent")
DEFAULT_PRIORITY = "normal"
DEFAULT_STATUS = "todo"

MAX_TASKS = 200          # 与 db.team_replace 的上限、旧接口一致
MAX_COMMENTS = 200
MAX_ACTIVITY = 200
MAX_TITLE = 120
MAX_DESC = 1000
MAX_TEXT = 1000          # 评论正文
MAX_NAME = 40            # 负责人 / 创建者 / 评论人
MAX_TAGS = 20
MAX_TAG_LEN = 24

#: 允许通过 update 修改的字段（白名单；id/created/creator 不可改）
EDITABLE_FIELDS: tuple[str, ...] = (
    "title", "desc", "status", "priority", "assignee", "due", "tags")

_TIME_FMT = "%Y-%m-%d %H:%M"
_ACT_TIME_FMT = "%Y-%m-%d %H:%M:%S"


class TeamError(ValueError):
    """任务操作不合法（API 层映射为 400，而不是 500）。"""


class TaskNotFound(TeamError):
    """按 id 找不到任务（API 层映射为 404）。"""


# ── 时间助手 ──────────────────────────────────────────────

def now_str() -> str:
    """当前本地时间（任务 created/updated 用的分钟级口径）。"""
    return time.strftime(_TIME_FMT)


def now_act_str() -> str:
    """当前本地时间（评论 / 活动时间线用的秒级口径）。"""
    return time.strftime(_ACT_TIME_FMT)


def today_str() -> str:
    """本地日期 ``YYYY-MM-DD``。"""
    return time.strftime("%Y-%m-%d")


def _text(value: Any, limit: int, default: str = "") -> str:
    """把任意值摊平成去首尾空白的字符串并截断；非字符串按空处理。"""
    if value is None:
        return default
    s = str(value).strip()
    return s[:limit] if s else default


def normalize_due(value: Any) -> str:
    """截止日期规范化成 ``YYYY-MM-DD``；无法识别的一律为空串（= 无截止日期）。

    接受 ``2025-06-15`` / ``2025-06-15T10:00`` / ``2025-06-15 10:00`` 三种常见
    写法 —— 旧数据与手写 API 调用都可能带时间部分，只取日期。
    """
    s = _text(value, 32)
    if not s:
        return ""
    head = s[:10]
    try:
        date.fromisoformat(head)
    except ValueError:
        return ""
    return head


def normalize_tags(value: Any) -> list[str]:
    """标签列表：去空、去重（保持原顺序）、逐个截断、总量封顶。"""
    if isinstance(value, str):
        raw: Iterable[Any] = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        raw = value
    else:
        raw = ()
    out: list[str] = []
    for item in raw:
        tag = _text(item, MAX_TAG_LEN)
        if tag and tag not in out:
            out.append(tag)
        if len(out) >= MAX_TAGS:
            break
    return out


# ── 规范化 ────────────────────────────────────────────────

def _normalize_comment(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    text = _text(raw.get("text"), MAX_TEXT)
    if not text:
        return None
    return {
        "id": _text(raw.get("id"), 32) or uuid.uuid4().hex[:8],
        "author": _text(raw.get("author"), MAX_NAME),
        "text": text,
        "time": _text(raw.get("time"), 19) or now_act_str(),
    }


def _normalize_activity(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    return {
        "time": _text(raw.get("time"), 19) or now_act_str(),
        "kind": _text(raw.get("kind"), 32) or "update",
        "actor": _text(raw.get("actor"), MAX_NAME),
        "detail": _text(raw.get("detail"), 200),
    }


def normalize_task(raw: dict, *, now: str | None = None) -> dict:
    """把任意（含旧版结构）任务补齐成完整结构；``id`` 保持兼容。

    旧字段缺失时的默认值：``priority=normal`` / ``tags=[]`` /
    ``comments=[]`` / ``activity=[]`` / ``assignee=""`` / ``due=""``。
    未知字段原样保留（前向兼容：将来加字段时旧版本不会把它抹掉）。
    """
    if not isinstance(raw, dict):
        raise TeamError("任务必须是对象")
    stamp = now or now_str()
    out: dict[str, Any] = dict(raw)          # 先保留未知字段
    created = _text(raw.get("created"), 19) or stamp
    status = _text(raw.get("status"), 16).lower()
    if status not in STATUSES:
        status = DEFAULT_STATUS
    priority = _text(raw.get("priority"), 16).lower()
    if priority not in PRIORITIES:
        priority = DEFAULT_PRIORITY
    comments: list[dict] = []
    for item in raw.get("comments") or []:
        c = _normalize_comment(item)
        if c:
            comments.append(c)
    activity: list[dict] = []
    for item in raw.get("activity") or []:
        a = _normalize_activity(item)
        if a:
            activity.append(a)
    out.update({
        "id": _text(raw.get("id"), 64) or uuid.uuid4().hex[:8],
        "title": _text(raw.get("title"), MAX_TITLE),
        "desc": _text(raw.get("desc"), MAX_DESC),
        "status": status,
        "priority": priority,
        "assignee": _text(raw.get("assignee"), MAX_NAME),
        "creator": _text(raw.get("creator"), MAX_NAME),
        "due": normalize_due(raw.get("due")),
        "tags": normalize_tags(raw.get("tags")),
        "comments": comments[-MAX_COMMENTS:],
        "activity": activity[-MAX_ACTIVITY:],
        "created": created,
        "updated": _text(raw.get("updated"), 19) or created,
    })
    return out


def _append_activity(task: dict, kind: str, actor: str = "", detail: str = "") -> dict:
    """追加一条活动（上限内保留最近的），返回同一 task（就地修改）。"""
    task["activity"] = (task.get("activity") or []) + [{
        "time": now_act_str(),
        "kind": kind,
        "actor": _text(actor, MAX_NAME),
        "detail": _text(detail, 200),
    }]
    if len(task["activity"]) > MAX_ACTIVITY:
        task["activity"] = task["activity"][-MAX_ACTIVITY:]
    task["updated"] = now_str()
    return task


# ── 纯逻辑操作 ────────────────────────────────────────────

def create_task(
    *,
    title: Any,
    desc: Any = "",
    assignee: Any = "",
    priority: Any = DEFAULT_PRIORITY,
    due: Any = "",
    tags: Any = None,
    creator: Any = "",
    status: Any = DEFAULT_STATUS,
    actor: Any = "",
    now: str | None = None,
) -> dict:
    """新建任务（返回规范化后的完整任务；title 必填）。"""
    name = _text(title, MAX_TITLE)
    if not name:
        raise TeamError("title 必填")
    prio = _text(priority, 16).lower() or DEFAULT_PRIORITY
    if prio not in PRIORITIES:
        raise TeamError(f"priority 必须是 {'/'.join(PRIORITIES)}")
    st = _text(status, 16).lower() or DEFAULT_STATUS
    if st not in STATUSES:
        raise TeamError(f"status 必须是 {'/'.join(STATUSES)}")
    who = _text(actor, MAX_NAME) or _text(creator, MAX_NAME)
    task = normalize_task({
        "id": uuid.uuid4().hex[:8],
        "title": name,
        "desc": desc,
        "status": st,
        "priority": prio,
        "assignee": assignee,
        "creator": creator,
        "due": due,
        "tags": tags,
        "comments": [],
        "activity": [],
        "created": now or now_str(),
        "updated": now or now_str(),
    }, now=now)
    _append_activity(task, "created", who, f"新建任务「{name}」")
    return task


def update_task(task: dict, patch: dict, actor: Any = "", now: str | None = None) -> dict:
    """按白名单更新任务字段；状态必须合法，变更会追加活动记录。

    - 只接受 :data:`EDITABLE_FIELDS` 里的字段（未知字段报错，而不是静默丢弃）；
    - 状态值必须在 :data:`STATUSES` 内（前端看板可自由前后移动，不做转移图限制）；
    - 只有真正发生变化时才追加 activity 并刷新 updated。
    """
    if not isinstance(patch, dict) or not patch:
        raise TeamError("没有可更新的字段")
    unknown = [k for k in patch if k not in EDITABLE_FIELDS]
    if unknown:
        raise TeamError("不支持更新字段: " + ", ".join(sorted(unknown)))
    t = normalize_task(task, now=now)
    if "status" in patch:
        st = _text(patch.get("status"), 16).lower()
        if st not in STATUSES:
            raise TeamError(f"status 必须是 {'/'.join(STATUSES)}")
        patch = {**patch, "status": st}
    if "priority" in patch:
        prio = _text(patch.get("priority"), 16).lower()
        if prio not in PRIORITIES:
            raise TeamError(f"priority 必须是 {'/'.join(PRIORITIES)}")
        patch = {**patch, "priority": prio}
    if "title" in patch:
        title = _text(patch.get("title"), MAX_TITLE)
        if not title:
            raise TeamError("title 不能为空")
        patch = {**patch, "title": title}
    if "due" in patch:
        patch = {**patch, "due": normalize_due(patch["due"])}
    if "tags" in patch:
        patch = {**patch, "tags": normalize_tags(patch["tags"])}

    # 按"值真的变了"逐项处理，顺带生成可读的活动文案
    if "title" in patch and patch["title"] != t["title"]:
        _append_activity(t, "edit", actor, f"标题改为「{patch['title']}」")
    if "desc" in patch:
        desc = _text(patch.get("desc"), MAX_DESC)
        if desc != t["desc"]:
            _append_activity(t, "edit", actor, "更新了描述")
    if "assignee" in patch:
        who = _text(patch.get("assignee"), MAX_NAME)
        if who != t["assignee"]:
            detail = f"负责人改为 {who}" if who else "取消负责人"
            _append_activity(t, "assign", actor, detail)
    if "priority" in patch and patch["priority"] != t["priority"]:
        _append_activity(t, "priority", actor, f"优先级改为 {patch['priority']}")
    if "due" in patch and patch["due"] != t["due"]:
        detail = f"截止日期改为 {patch['due']}" if patch["due"] else "清除截止日期"
        _append_activity(t, "due", actor, detail)
    if "tags" in patch and patch["tags"] != t["tags"]:
        _append_activity(t, "edit", actor, "更新了标签")
    if "status" in patch and patch["status"] != t["status"]:
        kind = "done" if patch["status"] == "done" else "status"
        _append_activity(t, kind, actor, f"{t['status']} → {patch['status']}")

    for key in EDITABLE_FIELDS:
        if key in patch:
            t[key] = patch[key]
    # 以上 _append_activity 会刷新 updated；没有任何变化时也保持原值
    return normalize_task(t, now=now)


def add_comment(task: dict, text: Any, author: Any = "", now: str | None = None) -> dict:
    """追加评论；正文必填。返回更新后的任务（评论在 ``task["comments"][-1]``）。"""
    body = _text(text, MAX_TEXT)
    if not body:
        raise TeamError("评论内容不能为空")
    t = normalize_task(task, now=now)
    comment = {
        "id": uuid.uuid4().hex[:8],
        "author": _text(author, MAX_NAME),
        "text": body,
        "time": now_act_str(),
    }
    t["comments"] = (t.get("comments") or []) + [comment]
    if len(t["comments"]) > MAX_COMMENTS:
        t["comments"] = t["comments"][-MAX_COMMENTS:]
    t["updated"] = now_str()
    return t


def claim(task: dict, member: Any, now: str | None = None) -> dict:
    """认领任务：负责人设为 member、状态置为 doing（已完成任务也可重新认领）。"""
    who = _text(member, MAX_NAME)
    if not who:
        raise TeamError("member 必填")
    t = normalize_task(task, now=now)
    if t["status"] == "doing" and t["assignee"] == who:
        return t                                # 幂等：重复认领不刷活动流
    t["assignee"] = who
    t["status"] = "doing"
    _append_activity(t, "claim", who, f"{who} 认领了任务")
    return t


def delete_task(tasks: list[dict], tid: str) -> tuple[list[dict], dict | None]:
    """从列表里删除一条任务，返回 ``(剩余列表, 被删任务或 None)``。"""
    target = _text(tid, 64)
    kept: list[dict] = []
    removed: dict | None = None
    for t in tasks or []:
        if isinstance(t, dict) and _text(t.get("id"), 64) == target and removed is None:
            removed = t
            continue
        kept.append(t)
    return kept, removed


# ── 过滤 / 摘要 ───────────────────────────────────────────

def _due_overdue(task: dict, today: str) -> bool:
    due = task.get("due") or ""
    return bool(due) and due < today and task.get("status") != "done"


def filter_tasks(
    tasks: list[dict],
    *,
    status: str | None = None,
    assignee: str | None = None,
    q: str | None = None,
    priority: str | None = None,
    overdue_only: bool = False,
    due_sort: bool = False,
    today: str | None = None,
) -> list[dict]:
    """按状态 / 负责人 / 关键词 / 优先级 / 逾期过滤；``due_sort`` 按截止日期升序。

    空值表示"不按该条件过滤"。关键词匹配标题、描述、标签与负责人（不区分大小写）。
    返回新列表，不修改入参。
    """
    day = today or today_str()
    wanted_status = _text(status, 16).lower()
    wanted_prio = _text(priority, 16).lower()
    wanted_who = _text(assignee, MAX_NAME)
    kw = _text(q, 120).lower()
    out: list[dict] = []
    for raw in tasks or []:
        if not isinstance(raw, dict):
            continue
        t = normalize_task(raw)
        if wanted_status and t["status"] != wanted_status:
            continue
        if wanted_prio and t["priority"] != wanted_prio:
            continue
        if wanted_who and t["assignee"] != wanted_who:
            continue
        if overdue_only and not _due_overdue(t, day):
            continue
        if kw:
            haystack = " ".join([t["title"], t["desc"], t["assignee"], *t["tags"]]).lower()
            if kw not in haystack:
                continue
        out.append(t)
    if due_sort:
        # 没填截止日期的排最后；同日的保持原顺序（sorted 是稳定排序）
        out.sort(key=lambda t: t["due"] or "9999-12-31")
    return out


def _done_date(task: dict) -> str:
    """任务完成日期（按活动流里最近一次 done 记录；旧数据回退 updated/created）。"""
    for item in reversed(task.get("activity") or []):
        if item.get("kind") == "done":
            t = _text(item.get("time"), 19)
            if len(t) >= 10:
                return t[:10]
    for key in ("updated", "created"):
        t = _text(task.get(key), 19)
        if len(t) >= 10:
            return t[:10]
    return ""


def _status_counts(task: dict, item: dict) -> None:
    """把一条任务计入 by_assignee 的桶（内部助手）。"""
    item["total"] += 1
    st = task.get("status")
    if st == "done":
        item["done"] += 1
    elif st == "doing":
        item["doing"] += 1


def summarize(tasks: list[dict], today: str | None = None) -> dict:
    """团队总览：状态/优先级分布、逾期、今日/近 7 天完成、按人负载、最近活动。

    ``completion_rate`` 为 0~100 的百分比（保留 1 位小数；无任务时为 0）。
    ``recent_activity`` 为最近 20 条活动（跨任务，按时间倒序，附带 task_id/title）。
    """
    day = today or today_str()
    try:
        today_date = date.fromisoformat(day)
    except ValueError:                          # pragma: no cover - 防御性
        today_date = datetime.now().date()
    normalized = [normalize_task(t) for t in tasks or [] if isinstance(t, dict)]
    by_status = {s: 0 for s in STATUSES}
    by_priority = {p: 0 for p in PRIORITIES}
    overdue = done_today = done_7d = 0
    by_assignee: dict[str, dict] = {}
    recent: list[dict] = []
    for t in normalized:
        by_status[t["status"]] = by_status.get(t["status"], 0) + 1
        by_priority[t["priority"]] = by_priority.get(t["priority"], 0) + 1
        if _due_overdue(t, day):
            overdue += 1
        if t["status"] == "done":
            dd = _done_date(t)
            if dd:
                try:
                    delta = (today_date - date.fromisoformat(dd)).days
                except ValueError:              # pragma: no cover - 防御性
                    delta = -1
                if delta == 0:
                    done_today += 1
                if 0 <= delta < 7:
                    done_7d += 1
        who = t["assignee"] or ""
        bucket = by_assignee.setdefault(
            who, {"assignee": who, "total": 0, "done": 0, "doing": 0, "overdue": 0})
        _status_counts(t, bucket)
        if _due_overdue(t, day):
            bucket["overdue"] += 1
        for act in t.get("activity") or []:
            recent.append({
                "time": act.get("time", ""), "kind": act.get("kind", ""),
                "actor": act.get("actor", ""), "detail": act.get("detail", ""),
                "task_id": t["id"], "task_title": t["title"],
            })
    total = len(normalized)
    recent.sort(key=lambda a: a.get("time", ""), reverse=True)
    rows = list(by_assignee.values())
    # 有任务的人排前面；负载相同按名字（未分配 "" 自然排最后）
    rows.sort(key=lambda r: (-r["total"], r["assignee"]))
    return {
        "total": total,
        "by_status": by_status,
        "by_priority": by_priority,
        "overdue": overdue,
        "done_today": done_today,
        "done_7d": done_7d,
        "by_assignee": rows,
        "recent_activity": recent[:20],
        "completion_rate": round(by_status.get("done", 0) * 100.0 / total, 1) if total else 0.0,
    }


# ── 存储（可注入）────────────────────────────────────────

def _default_load() -> list[dict]:
    """默认存储：项目 SQLite 的 team_tasks 表（含旧 JSON 一次性迁移）。

    v1.11.18：AI 工具（``automind/tools/team_tasks.py``）用的是**默认存储**，
    而旧数据迁移此前只写在 server 的 loader 里 —— 若 Agent 先于团队看板第一次
    API 调用写入新任务，随后看板加载触发的迁移会用旧 JSON **整表替换**，
    AI 刚认领/创建的任务就凭空消失。把迁移下沉到默认读写层，三条入口
    （Web / REST / AI 工具）看到的是同一份数据与同一套迁移语义。
    """
    from automind.core.db import get_db, migrate_json_once
    from automind.core.paths import legacy_file

    db = get_db()
    migrate_json_once(
        db, "team_tasks", legacy_file("team_tasks.json"),
        lambda data: db.team_replace(data) if isinstance(data, list) else None)
    return db.team_load()


def _default_save(tasks: list[dict]) -> None:
    from automind.core.db import get_db
    get_db().team_replace(list(tasks)[-MAX_TASKS:])


class TeamStore:
    """任务清单读写 + 纯逻辑操作的薄包装。

    ``loader`` / ``saver`` 可注入：server 端注入带旧 JSON 迁移的 SQLite 读写，
    测试注入内存列表即可完全不碰磁盘。
    """

    def __init__(
        self,
        loader: Callable[[], list[dict]] | None = None,
        saver: Callable[[list[dict]], None] | None = None,
    ) -> None:
        self.loader = loader or _default_load
        self.saver = saver or _default_save

    def load(self) -> list[dict]:
        raw = self.loader() or []
        return [normalize_task(t) for t in raw if isinstance(t, dict)]

    def save(self, tasks: list[dict]) -> None:
        self.saver(list(tasks)[-MAX_TASKS:])

    def _find(self, tasks: list[dict], tid: str) -> int:
        target = _text(tid, 64)
        for i, t in enumerate(tasks):
            if t.get("id") == target:
                return i
        raise TaskNotFound(f"任务不存在: {target}")

    def create(self, **fields: Any) -> dict:
        task = create_task(**fields)
        tasks = self.load()
        tasks.append(task)
        self.save(tasks)
        return task

    def update(self, tid: str, patch: dict, actor: Any = "") -> dict:
        tasks = self.load()
        idx = self._find(tasks, tid)
        updated = update_task(tasks[idx], patch, actor=actor)
        tasks[idx] = updated
        self.save(tasks)
        return updated

    def delete(self, tid: str) -> dict:
        tasks = self.load()
        kept, removed = delete_task(tasks, tid)
        if removed is None:
            raise TaskNotFound(f"任务不存在: {_text(tid, 64)}")
        self.save(kept)
        return normalize_task(removed)

    def comment(self, tid: str, text: Any, author: Any = "") -> dict:
        tasks = self.load()
        idx = self._find(tasks, tid)
        tasks[idx] = add_comment(tasks[idx], text, author)
        self.save(tasks)
        return tasks[idx]

    def claim(self, tid: str, member: Any) -> dict:
        tasks = self.load()
        idx = self._find(tasks, tid)
        tasks[idx] = claim(tasks[idx], member)
        self.save(tasks)
        return tasks[idx]


# ── 成员昵称持久化（.automind/team_members.json）──────────

_NAME_LOCK = threading.Lock()
_MEMBERS_FILENAME = "team_members.json"


def members_file(path: str | Path | None = None) -> Path:
    """昵称持久化文件路径（可显式指定，测试用临时路径）。"""
    if path is not None:
        return Path(path)
    from automind.core.paths import data_dir
    return data_dir() / _MEMBERS_FILENAME


def load_member_names(path: str | Path | None = None) -> dict[str, str]:
    """读取 {sid: name}；文件缺失 / 损坏一律当作空表（昵称不是关键数据）。"""
    target = members_file(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v)[:MAX_NAME] for k, v in raw.items()
            if str(k).strip() and str(v).strip()}


def save_member_name(sid: Any, name: Any, path: str | Path | None = None) -> dict[str, str]:
    """持久化一条成员昵称（先读后写 + 临时文件原子替换），返回全量映射。"""
    key = _text(sid, 128)
    value = _text(name, MAX_NAME)
    if not key or not value:
        raise TeamError("sid 与 name 均不能为空")
    target = members_file(path)
    with _NAME_LOCK:
        names = load_member_names(target)
        names[key] = value
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(json.dumps(names, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(target)
        except OSError:
            # 昵称写不进去不该让接口失败：本次会话内仍按内存态显示
            pass
        return names
