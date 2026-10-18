"""会话目录（v1.7.6）—— 工作区内的多会话、归档与上下文延续。

此前每个工作区只有**一条**会话（``SID + 工作区后缀``）：想开个新话题只能
"清空对话"，旧内容就此消失；换个项目回来，Agent 对之前聊过什么一无所知。

这里给每个工作区维护一份会话目录：

* **多会话**：新建 / 切换 / 重命名 / 置顶 / 删除。会话 sid 由前端拼出
  （``SID + 工作区后缀 + _c<id>``），与既有的"按 sid 隔离历史与 Agent 克隆"
  机制完全兼容 —— 服务端其余代码不需要知道"会话"这个概念。
* **归档**：归档时生成一段摘要（能调模型就让模型写，否则抽取式兜底），
  摘要写进该工作区的长期记忆，之后任何会话里 ``retrieve_relevant`` 都能
  检索到 —— 这是"长期记忆"真正落地的地方。
* **上下文延续**：从某个会话"接着聊"会新建一个会话，并把源会话摘要作为
  种子；新会话的**第一轮**任务前注入该种子，以及本工作区最近几条归档摘要
  （"项目记忆"）。只注入第一轮：之后它已经在会话历史里了，再发是重复付费。

按 ``owner``（浏览器级会话 ID）+ ``workspace``（工作区后缀）隔离：同一台
服务上的多个用户、同一用户的多个项目，彼此看不到对方的会话。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

from automind.core.logging import get_logger

logger = get_logger("automind.conversations")

#: 默认会话（旧版唯一那条）的显示名
DEFAULT_TITLE = "默认会话"
#: 新建会话的占位标题 —— 首轮任务到来时自动改成任务摘要
PLACEHOLDER_TITLE = "新会话"
#: 注入到首轮的"项目记忆"最多带几条归档摘要 / 多少字
PROJECT_MEMORY_ITEMS = 3
PROJECT_MEMORY_CHARS = 1500
#: 摘要长度上限（字符）
SUMMARY_CHARS = 1200

_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def _clean(s: str, n: int = 64) -> str:
    return _SAFE.sub("", s or "")[:n]


def auto_title(task: str, limit: int = 28) -> str:
    """从首轮任务里取一个短标题：去掉注入块/换行，截到 ``limit`` 字。"""
    text = re.sub(r"【[^】]{1,12}】.*?(?:\n---\n|$)", "", task or "", flags=re.S)
    text = " ".join(text.split())
    return (text[:limit] + ("…" if len(text) > limit else "")) or PLACEHOLDER_TITLE


def extractive_summary(transcript: list[dict[str, Any]], limit: int = SUMMARY_CHARS) -> str:
    """零成本兜底摘要：首个诉求 + 最近几轮往来 + 最后结论。

    模型不可用（未配置 / 断网 / 预算用尽）时也必须能归档 —— 归档是整理动作，
    不该因为"写不了摘要"而失败。
    """
    msgs = [m for m in transcript or []
            if isinstance(m, dict) and str(m.get("content") or "").strip()]
    if not msgs:
        return ""
    def _t(m: dict[str, Any], n: int) -> str:
        return " ".join(str(m.get("content") or "").split())[:n]
    users = [m for m in msgs if m.get("role") == "user"]
    agents = [m for m in msgs if m.get("role") in ("assistant", "agent")]
    parts: list[str] = []
    if users:
        parts.append(f"最初诉求：{_t(users[0], 200)}")
    if len(users) > 1:
        later = "；".join(_t(u, 80) for u in users[1:][-4:])
        parts.append(f"后续要求：{later}")
    if agents:
        parts.append(f"最后结论：{_t(agents[-1], 500)}")
    return "\n".join(parts)[:limit]


class ConversationStore:
    """会话目录的持久化（SQLite ``conversations`` 表）。"""

    def __init__(self, db: Any = None) -> None:
        self._db_obj = db

    def _db(self) -> Any:
        if self._db_obj is None:
            from automind.core.db import get_db
            self._db_obj = get_db()
        return self._db_obj

    # ── 读 ─────────────────────────────────────────────
    @staticmethod
    def _row(data: str, archived: int) -> dict[str, Any]:
        try:
            d = json.loads(data)
        except Exception:
            d = {}
        d["archived"] = bool(archived)
        return d

    def get(self, sid: str) -> dict[str, Any] | None:
        rows = self._db().query(
            "SELECT data, archived FROM conversations WHERE sid=?", (sid,))
        return self._row(*rows[0]) if rows else None

    def list(self, owner: str, workspace: str, archived: bool | None = None
             ) -> list[dict[str, Any]]:
        sql = "SELECT data, archived FROM conversations WHERE owner=? AND workspace=?"
        params: tuple = (owner, workspace)
        if archived is not None:
            sql += " AND archived=?"
            params += (1 if archived else 0,)
        rows = self._db().query(sql + " ORDER BY updated DESC", params)
        items = [self._row(*r) for r in rows]
        # 置顶优先，其余按最近活动
        items.sort(key=lambda c: (not c.get("pinned"), -float(c.get("updated") or 0)))
        return items

    # ── 写 ─────────────────────────────────────────────
    def _save(self, conv: dict[str, Any]) -> dict[str, Any]:
        conv = dict(conv)
        archived = 1 if conv.pop("archived", False) else 0
        self._db().execute(
            "INSERT INTO conversations(sid, owner, workspace, data, archived, updated) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(sid) DO UPDATE SET "
            "data=excluded.data, archived=excluded.archived, updated=excluded.updated",
            (conv["sid"], conv.get("owner", ""), conv.get("workspace", ""),
             json.dumps(conv, ensure_ascii=False), archived,
             float(conv.get("updated") or time.time())))
        conv["archived"] = bool(archived)
        return conv

    def ensure_default(self, owner: str, workspace: str) -> dict[str, Any]:
        """确保旧版那条"默认会话"在目录里（升级后原有聊天记录不会凭空消失）。"""
        owner, workspace = _clean(owner), _clean(workspace)
        sid = (owner or "default") + workspace
        conv = self.get(sid)
        if conv is None:
            now = time.time()
            conv = self._save({
                "sid": sid, "suffix": "", "owner": owner, "workspace": workspace,
                "title": DEFAULT_TITLE, "title_auto": False, "pinned": False,
                "created": now, "updated": now, "turns": 0,
                "summary": "", "seed": "", "seed_from": "", "archived": False,
            })
        return conv

    def create(self, owner: str, workspace: str, title: str = "",
               seed: str = "", seed_from: str = "") -> dict[str, Any]:
        owner, workspace = _clean(owner), _clean(workspace)
        suffix = "_c" + uuid.uuid4().hex[:8]
        now = time.time()
        title = (title or "").strip()[:60]
        return self._save({
            "sid": (owner or "default") + workspace + suffix, "suffix": suffix,
            "owner": owner, "workspace": workspace,
            "title": title or PLACEHOLDER_TITLE, "title_auto": not title,
            "pinned": False, "created": now, "updated": now, "turns": 0,
            "summary": "", "seed": (seed or "")[:SUMMARY_CHARS * 2],
            "seed_from": seed_from, "archived": False,
        })

    def update(self, sid: str, **fields: Any) -> dict[str, Any] | None:
        conv = self.get(sid)
        if conv is None:
            return None
        allowed = {"title", "pinned", "archived", "summary", "seed", "archived_at"}
        for k, v in fields.items():
            if k in allowed:
                conv[k] = v
        if "title" in fields:
            conv["title"] = str(fields["title"] or "").strip()[:60] or conv.get("title")
            conv["title_auto"] = False
        conv["updated"] = time.time()
        return self._save(conv)

    def delete(self, sid: str) -> bool:
        if self.get(sid) is None:
            return False
        self._db().execute("DELETE FROM conversations WHERE sid=?", (sid,))
        return True

    def touch(self, sid: str, task: str = "") -> dict[str, Any] | None:
        """一轮任务开始：刷新活跃时间、轮数，占位标题换成任务摘要。

        未登记的 sid（API 直调、旧客户端）直接忽略 —— 目录是锦上添花，
        绝不能因为它让任务失败。
        """
        conv = self.get(sid)
        if conv is None:
            return None
        if conv.get("title_auto") and task:
            conv["title"] = auto_title(task)
            conv["title_auto"] = False
        conv["turns"] = int(conv.get("turns") or 0) + 1
        conv["updated"] = time.time()
        if conv.get("archived"):
            # 在已归档的会话里继续发消息 = 用户又用上它了，自动取消归档
            conv["archived"] = False
        return self._save(conv)

    # ── 上下文延续 ─────────────────────────────────────
    def project_memory(self, owner: str, workspace: str, exclude: str = "") -> str:
        """本工作区最近几条归档摘要，拼成一段"项目记忆"。"""
        items = [c for c in self.list(_clean(owner), _clean(workspace), archived=True)
                 if c.get("summary") and c.get("sid") != exclude]
        items.sort(key=lambda c: -float(c.get("archived_at") or c.get("updated") or 0))
        out: list[str] = []
        used = 0
        for c in items[:PROJECT_MEMORY_ITEMS]:
            line = f"- 《{c.get('title') or '未命名'}》{c['summary']}"
            if used + len(line) > PROJECT_MEMORY_CHARS:
                line = line[:max(0, PROJECT_MEMORY_CHARS - used)]
            if not line:
                break
            out.append(line)
            used += len(line)
        return "\n".join(out)

    def context_prefix(self, sid: str) -> str:
        """首轮任务前要注入的上下文块（非首轮 / 未登记 → 空串）。"""
        conv = self.get(sid)
        if conv is None or int(conv.get("turns") or 0) > 0:
            return ""
        blocks: list[str] = []
        seed = (conv.get("seed") or "").strip()
        if seed:
            blocks.append("【延续上下文】本会话接续自此前的一段对话，其要点如下"
                          "（据此继续，不必复述）：\n" + seed)
        mem = self.project_memory(conv.get("owner", ""), conv.get("workspace", ""),
                                  exclude=conv.get("seed_from", ""))
        if mem:
            blocks.append("【项目记忆】本工作区此前已归档会话的摘要（仅供参考，"
                          "与当前任务无关时忽略）：\n" + mem)
        return "\n\n".join(blocks)


_store: ConversationStore | None = None


def get_store() -> ConversationStore:
    global _store
    if _store is None:
        _store = ConversationStore()
    return _store
