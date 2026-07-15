"""版本限额（Quota）— 按版本（社区/专业/企业）限制每日任务数与工作区数量。

设计原则（与 edition.py 的开源核心约定一致）：
    - 限额逻辑全部放在社区核心，规则表按版本查表 —— 专业/企业版无需
      任何扩展代码即可自动解除限制（edition 由许可证决定）；
    - 每日任务计数持久化到 ``.automind/quota.json``，重启服务不清零；
      跨天自动滚动（本地时区日期）。

限额规则（None = 不限）：
    ============ ============ ==========
    版本          每日任务数     工作区数量
    ============ ============ ==========
    community     100          3
    pro           不限          30
    enterprise    不限          不限
    ============ ============ ==========
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from automind.core import edition as _edition

#: 版本 → {daily_tasks, workspaces}；None 表示不限
QUOTA_RULES: dict[str, dict[str, int | None]] = {
    "community": {"daily_tasks": 100, "workspaces": 3},
    "pro": {"daily_tasks": None, "workspaces": 30},
    "enterprise": {"daily_tasks": None, "workspaces": None},
}

_QUOTA_FILE = Path(".automind") / "quota.json"   # 旧文件（仅迁移用）
_lock = threading.Lock()
_state: dict = {"date": "", "tasks": 0}
_loaded = False


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _load() -> None:
    """加载计数（v1.1 起 SQLite kv 表；旧 quota.json 自动一次性迁移）。"""
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        from automind.core.db import get_db, migrate_json_once
        db = get_db()
        migrate_json_once(
            db, "quota", _QUOTA_FILE,
            lambda data: db.kv_set("quota", data) if isinstance(data, dict) else None)
        data = db.kv_get("quota")
        if isinstance(data, dict):
            _state.update({"date": data.get("date", ""),
                           "tasks": int(data.get("tasks", 0))})
    except Exception:
        # SQLite 不可用（极端环境）→ 回退旧 JSON 读取
        try:
            if _QUOTA_FILE.exists():
                data = json.loads(_QUOTA_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    _state.update({"date": data.get("date", ""),
                                   "tasks": int(data.get("tasks", 0))})
        except Exception:
            pass


def _save() -> None:
    try:
        from automind.core.db import get_db
        get_db().kv_set("quota", dict(_state))
    except Exception:
        pass


def _rollover() -> None:
    """跨天清零（调用方需持有 _lock）。"""
    today = _today()
    if _state["date"] != today:
        _state["date"] = today
        _state["tasks"] = 0


def rules() -> dict[str, int | None]:
    """当前版本的限额规则。"""
    return QUOTA_RULES.get(_edition.get_edition(), QUOTA_RULES["community"])


def daily_limit() -> int | None:
    return rules()["daily_tasks"]


def workspace_limit() -> int | None:
    return rules()["workspaces"]


def tasks_used_today() -> int:
    with _lock:
        _load()
        _rollover()
        return _state["tasks"]


def try_consume_task() -> tuple[bool, str]:
    """尝试消费一次任务额度。

    返回 ``(允许执行, 拒绝原因)``；允许时已完成计数 +1。
    """
    limit = daily_limit()
    with _lock:
        _load()
        _rollover()
        if limit is not None and _state["tasks"] >= limit:
            return False, (
                f"今日任务次数已达社区版上限（{limit} 次/天）。"
                "升级到专业版可解除每日任务限制（安装 automind-pro 并配置许可证）。"
            )
        _state["tasks"] += 1
        _save()
        return True, ""


def refund_task() -> None:
    """任务未真正开始（如 LLM 未初始化）时退还额度。"""
    with _lock:
        _load()
        _rollover()
        _state["tasks"] = max(0, _state["tasks"] - 1)
        _save()


def check_workspace(count_existing: int) -> tuple[bool, str]:
    """新增工作区前校验数量上限。``count_existing`` 为不含本次新增的现有数量。"""
    limit = workspace_limit()
    if limit is not None and count_existing >= limit:
        tier = _edition.get_edition()
        nxt = "专业版（30 个）或企业版（不限）" if tier == "community" else "企业版（不限）"
        return False, f"工作区数量已达{_edition.edition_label()}上限（{limit} 个）。升级到{nxt}可扩容。"
    return True, ""


def snapshot() -> dict:
    """限额快照（供 /api/quota 与前端展示）。"""
    with _lock:
        _load()
        _rollover()
        used = _state["tasks"]
    r = rules()
    return {
        "edition": _edition.get_edition(),
        "daily_tasks": {"used": used, "limit": r["daily_tasks"]},
        "workspaces": {"limit": r["workspaces"]},
    }


def reset_for_tests() -> None:
    with _lock:
        _state.update({"date": _today(), "tasks": 0})
