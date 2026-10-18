"""用户提问的进程内等待表（v1.11.11）。

## 为什么需要它

工具 `ask_user_question` 要**阻塞等待**用户回答，而回答来自 WebSocket/前端。
工具与 WS 处理器不在同一条调用链上，所以中间需要一张"问题 id → Future"的表：
工具登记问题并 await；WS 收到答案后按 id 投递，工具立刻拿到结果继续。

## 边界

* 纯进程内、不持久化：页面关掉/进程重启后未回答的问题自然消失（工具会超时返回失败，
  绝不假装已批准）；
* 超时/取消时由工具负责清理；`resolve` 对未知/已结束的问题返回 None，
  server 映射成 `user_question_stale`（迟到的点击不能“复活”旧问题）；
* 子智能体默认不应向用户提问（无回答界面），由工具层按深度拒绝。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from automind.core.logging import get_logger

logger = get_logger("automind.user_questions")

#: 一次最多问几个问题（再多用户不答，模型也该拆开问）
MAX_QUESTIONS = 4
#: 每个问题最多几个选项
MAX_OPTIONS = 6
#: 问题正文 / 选项标签长度上限
MAX_QUESTION_CHARS = 1000
MAX_OPTION_CHARS = 120
#: 同时挂起的问题上限 —— 防止页面反复断开/重连时泄漏 Future
MAX_PENDING = 32

#: question_id -> 记录（含 future）。只允许同进程访问。
_PENDING: dict[str, dict[str, Any]] = {}


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def normalize_questions(questions: Any) -> list[dict[str, Any]]:
    """校验并归一化问题列表；非法输入抛 ``ValueError``（带可照做的说明）。"""
    if not isinstance(questions, list) or not questions:
        raise ValueError("questions 必须是非空数组")
    if len(questions) > MAX_QUESTIONS:
        raise ValueError(f"一次最多提 {MAX_QUESTIONS} 个问题，请拆开再问")
    out: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for i, item in enumerate(questions):
        if not isinstance(item, dict):
            raise ValueError(f"第 {i + 1} 个问题不是对象")
        qid = _text(item.get("id") or f"q{i + 1}", 40) or f"q{i + 1}"
        while qid in used_ids:
            qid = f"{qid}_{i + 1}"[:40]
        used_ids.add(qid)
        question = _text(item.get("question"), MAX_QUESTION_CHARS)
        if not question:
            raise ValueError(f"第 {i + 1} 个问题缺少 question")
        options_in = item.get("options") or []
        if not isinstance(options_in, list):
            raise ValueError(f"第 {i + 1} 个问题的 options 必须是数组")
        if len(options_in) > MAX_OPTIONS:
            raise ValueError(f"第 {i + 1} 个问题最多 {MAX_OPTIONS} 个选项")
        options: list[dict[str, str]] = []
        for j, opt in enumerate(options_in):
            if isinstance(opt, str):
                label, desc = opt, ""
            elif isinstance(opt, dict):
                label = _text(opt.get("label"), MAX_OPTION_CHARS)
                desc = _text(opt.get("description"), 240)
            else:
                raise ValueError(f"第 {i + 1} 个问题的第 {j + 1} 个选项格式不对")
            if not label:
                raise ValueError(f"第 {i + 1} 个问题的第 {j + 1} 个选项缺少 label")
            options.append({"label": label, "description": desc})
        out.append({
            "id": qid,
            "question": question,
            "header": _text(item.get("header"), 40),
            "options": options,
            "multi": bool(item.get("multi")),
        })
    return out


def normalize_answers(questions: list[dict[str, Any]], answers: Any) -> list[dict[str, Any]]:
    """把用户回答归一化成 ``[{id, selected:[...], custom?}]``；非法抛 ``ValueError``。"""
    if not isinstance(answers, list) or not answers:
        raise ValueError("answers 必须是非空数组")
    by_id = {str(q["id"]): q for q in questions}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, item in enumerate(answers):
        if not isinstance(item, dict):
            raise ValueError(f"第 {i + 1} 条回答不是对象")
        qid = str(item.get("id") or "").strip()
        if qid not in by_id:
            raise ValueError(f"未知的问题 id：{qid or '(空)'}")
        if qid in seen:
            raise ValueError(f"问题 {qid} 重复回答")
        seen.add(qid)
        selected_in = item.get("selected") or []
        if not isinstance(selected_in, list):
            raise ValueError(f"问题 {qid} 的 selected 必须是数组")
        selected = [str(s) for s in selected_in if str(s).strip()]
        custom = str(item.get("custom") or "").strip()[:1000]
        if not selected and not custom:
            # 跳过本题 = 空 selected/无 custom：允许，模型据此知道用户没选
            out.append({"id": qid, "selected": []})
            continue
        out.append({"id": qid, "selected": selected, **({"custom": custom} if custom else {})})
    return out


def create(session_id: str, questions: Any, timeout_s: float = 300.0
           ) -> tuple[str, asyncio.Future]:
    """登记一个问题并返回 ``(question_id, future)``。"""
    normalized = normalize_questions(questions)
    if len(_PENDING) >= MAX_PENDING:
        raise ValueError("当前待回答问题过多，请稍后再问或先处理已有问题")
    question_id = f"q_{uuid.uuid4().hex[:12]}"
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    _PENDING[question_id] = {
        "question_id": question_id,
        "session_id": session_id or "default",
        "questions": normalized,
        "future": future,
        "created": time.time(),
        "timeout_s": float(timeout_s or 300.0),
    }
    return question_id, future


def _public(rec: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in rec.items() if k != "future"}


def resolve(question_id: str, answers: Any) -> dict[str, Any] | None:
    """投递用户回答；未知/已结束返回 None（调用方据此回 stale）。"""
    rec = _PENDING.pop(question_id, None)
    if rec is None:
        return None
    future = rec["future"]
    if future.done():
        return None
    try:
        normalized = normalize_answers(rec["questions"], answers)
    except ValueError as e:
        # 结构非法：把问题放回去，让用户能重新提交（不结束本次提问）
        _PENDING[question_id] = rec
        logger.warning("user_answer_invalid", question_id=question_id, error=str(e)[:200])
        return None
    out = _public(rec)
    out["answers"] = normalized
    future.set_result({"answers": normalized})
    return out


def cancel(question_id: str, reason: str = "cancelled") -> dict[str, Any] | None:
    """结束一个问题（超时/任务取消），等待方拿到 cancelled 结果而不是无限挂起。"""
    rec = _PENDING.pop(question_id, None)
    if rec is None or rec["future"].done():
        return None
    rec["future"].set_result({"cancelled": True, "reason": reason})
    return _public(rec)


def cancel_session(session_id: str, reason: str = "session closed") -> int:
    """取消某会话下所有待答问题（页面关闭/会话结束时的兜底清理）。"""
    n = 0
    for qid, rec in list(_PENDING.items()):
        if rec.get("session_id") == (session_id or "default"):
            if cancel(qid, reason) is not None:
                n += 1
    return n


def pending_for_session(session_id: str) -> list[dict[str, Any]]:
    return [_public(r) for r in _PENDING.values()
            if r.get("session_id") == (session_id or "default")]


def pending_count() -> int:
    return len(_PENDING)
