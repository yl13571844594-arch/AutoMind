"""会话待办清单 —— 模型可维护的短清单（v1.11.11）。

## 为什么需要它

AutoMind 的「计划树」是执行前生成、按步骤推进的**生成式计划**；任务中途
出现新子任务、或模型发现自己漏了一步时，没有一个"可以随时整表重写、用户
又能看见"的清单。DSH 的 todo 工具补的就是这个空档。

## 边界

* 按**会话**（chat_sid，不是单次运行）保存，跨多轮任务可见；
* **整表替换**语义：模型每次提交完整清单，避免增量合并出的"幽灵条目"；
* 只存清单本身，不把清单塞回模型上下文（那会白烧 token）；
* 存 SQLite 的 kv 表（`automind/core/db.py`），损坏/读失败一律按空清单处理，
  绝不让一条待办把任务带崩。
"""

from __future__ import annotations

from typing import Any

from automind.core.logging import get_logger

logger = get_logger("automind.todo")

#: 单会话最多多少条 —— 再长用户也不会看，模型维护成本也高
MAX_ITEMS = 50
#: 单条内容字符上限（超出截断而不是整表失败，模型写长句很常见）
MAX_CONTENT = 500
#: 允许的状态（与 DSH 的 todo 语义一致）
STATUSES = ("pending", "in_progress", "completed")

#: 模型常见的写法归一化 —— 不能因为写了 "doing" 就让整次更新失败
_STATUS_ALIASES = {
    "pending": "pending", "todo": "pending", "not_started": "pending",
    "in_progress": "in_progress", "in-progress": "in_progress",
    "inprogress": "in_progress", "doing": "in_progress", "running": "in_progress",
    "completed": "completed", "complete": "completed", "done": "completed",
    "finished": "completed",
}


def _key(session_id: str) -> str:
    return f"todo:{session_id or 'default'}"


def _db() -> Any:
    # 延迟导入：工具/模块被导入时不应该顺带建库（CLI 的 --help 也会走到这里）
    from automind.core.db import get_db

    return get_db()


def normalize(todos: Any) -> list[dict[str, str]]:
    """校验并归一化模型提交的清单；非法输入抛 ``ValueError``（带可照做的说明）。"""
    if not isinstance(todos, list):
        raise ValueError("todos 必须是数组：[{\"content\": \"...\", \"status\": \"pending\"}]")
    if len(todos) > MAX_ITEMS:
        raise ValueError(f"待办最多 {MAX_ITEMS} 条，请合并或删减后再提交（当前 {len(todos)} 条）")
    out: list[dict[str, str]] = []
    used_ids: set[str] = set()
    for i, item in enumerate(todos):
        if not isinstance(item, dict):
            raise ValueError(f"第 {i + 1} 条不是对象，应形如 {{\"content\": \"...\", \"status\": \"pending\"}}")
        content = str(item.get("content") or "").strip()
        if not content:
            raise ValueError(f"第 {i + 1} 条缺少 content")
        if len(content) > MAX_CONTENT:
            content = content[:MAX_CONTENT]
        raw_status = str(item.get("status") or "pending").strip().lower()
        status = _STATUS_ALIASES.get(raw_status)
        if status is None:
            raise ValueError(
                f"第 {i + 1} 条的 status「{raw_status}」无效；只能是 "
                + " / ".join(STATUSES))
        tid = str(item.get("id") or f"t{i + 1}").strip()[:32] or f"t{i + 1}"
        while tid in used_ids:                 # id 重复时补后缀，绝不丢条目
            tid = f"{tid}_{i + 1}"[:32]
        used_ids.add(tid)
        out.append({"id": tid, "content": content, "status": status})
    return out


def load(session_id: str) -> list[dict[str, str]]:
    """读会话待办；任何存储异常都返回空清单（不让展示把任务带崩）。"""
    try:
        raw = _db().kv_get(_key(session_id), [])
        if not isinstance(raw, list):
            return []
        return [
            {"id": str(x.get("id", "")), "content": str(x.get("content", "")),
             "status": str(x.get("status", "pending"))}
            for x in raw if isinstance(x, dict)
        ]
    except Exception as e:
        logger.warning("todo_load_failed", session=session_id, error=str(e)[:200])
        return []


def replace(session_id: str, todos: Any) -> list[dict[str, str]]:
    """整表替换并返回落库后的清单；存储失败会抛异常（由工具如实报给模型）。"""
    normalized = normalize(todos)
    _db().kv_set(_key(session_id), normalized)
    return normalized


def clear(session_id: str) -> None:
    try:
        _db().kv_set(_key(session_id), [])
    except Exception as e:
        logger.warning("todo_clear_failed", session=session_id, error=str(e)[:200])


def counts(todos: list[dict[str, Any]]) -> dict[str, int]:
    out = {"pending": 0, "in_progress": 0, "completed": 0}
    for item in todos or []:
        status = str(item.get("status") or "pending")
        if status in out:
            out[status] += 1
    return out
