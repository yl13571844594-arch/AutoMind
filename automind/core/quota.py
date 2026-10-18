"""版本限额（Quota）— 按版本（社区/专业/企业）限制每日任务数与工作区数量。

设计原则（与 edition.py 的开源核心约定一致）：
    - 限额逻辑全部放在社区核心，规则表按版本查表 —— 专业/企业版无需
      任何扩展代码即可自动解除限制（edition 由许可证决定）；
    - 每日任务计数持久化到 ``.automind/quota.json``，重启服务不清零；
      跨天自动滚动（本地时区日期）。

限额规则（None = 不限）：
    ============ ============ ========== ========== ============
    版本          每日任务数     工作区数量   工作流数量   foreach 上限
    ============ ============ ========== ========== ============
    community     100          3          20         20
    pro           不限          30         200        1000
    enterprise    不限          不限        不限        不限
    ============ ============ ========== ========== ============

    （工作流的触发器、定时间隔、历史保留见 ``QUOTA_RULES`` 注释。）
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from automind.core import edition as _edition

#: 版本 → 限额；None 表示不限。
#:
#: 工作流相关的五项（v1.8.0，见 docs/WORKFLOW_ROADMAP.md §2.3）：
#:   · workflows              已保存（校验通过）的工作流数；超出的**仍可列出、
#:                            校验、dry-run**，只是不能运行
#:   · workflow_triggers      活跃触发器数（社区版 0 = 无内置触发器，走外部 cron / CI）
#:   · workflow_min_interval  定时触发的最小间隔（秒；社区版无触发器，不适用）
#:   · workflow_history       运行历史保留条数（None = 按天数保留，见下一项）
#:   · workflow_history_days  运行历史保留天数（v1.9.0：专业版 90 天；企业版 None =
#:                            可配置，配置项 workflow_history_days，缺省 365 天）
#:   · workflow_foreach_max   foreach 单次最大迭代数
QUOTA_RULES: dict[str, dict[str, int | None]] = {
    "community": {"daily_tasks": 100, "workspaces": 3,
                  "workflows": 20, "workflow_triggers": 0,
                  "workflow_min_interval": None, "workflow_history": 50,
                  "workflow_history_days": None, "workflow_foreach_max": 20},
    "pro": {"daily_tasks": None, "workspaces": 30,
            "workflows": 200, "workflow_triggers": 30,
            "workflow_min_interval": 300, "workflow_history": None,
            "workflow_history_days": 90, "workflow_foreach_max": 1000},
    "enterprise": {"daily_tasks": None, "workspaces": None,
                   "workflows": None, "workflow_triggers": None,
                   "workflow_min_interval": 60, "workflow_history": None,
                   "workflow_history_days": None, "workflow_foreach_max": None},
}

def _legacy_quota_file() -> Path:
    """旧版 quota.json（仅一次性迁移用；随数据目录解析）。"""
    from automind.core.paths import legacy_file
    return legacy_file("quota.json")


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
            db, "quota", _legacy_quota_file(),
            lambda data: db.kv_set("quota", data) if isinstance(data, dict) else None)
        data = db.kv_get("quota")
        if isinstance(data, dict):
            _state.update({"date": data.get("date", ""),
                           "tasks": int(data.get("tasks", 0))})
    except Exception:
        # SQLite 不可用（极端环境）→ 回退旧 JSON 读取
        try:
            if _legacy_quota_file().exists():
                data = json.loads(_legacy_quota_file().read_text(encoding="utf-8"))
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


def workflow_limit(key: str) -> int | None:
    """工作流相关限额（键见 ``QUOTA_RULES`` 注释）；None = 不限。"""
    return rules().get(key)


def check_workflow_slot(position: int) -> tuple[bool, str]:
    """第 ``position`` 个（1 起，按文件名排序、只数校验通过的）工作流能否运行。

    为什么按"位次"而不是"总数"判：超限时不能让**全部**工作流都跑不了 ——
    用户最早建的那 20 个照常可用，超出的那些列出来、标注原因，
    删掉别的或升级之后自然恢复。
    """
    limit = workflow_limit("workflows")
    if limit is None or position <= limit:
        return True, ""
    return False, (f"超出{_edition.edition_label()}上限（{limit} 个工作流），"
                   "该工作流仍可查看、校验与试运行；升级或删除其他工作流后可运行")


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


def check_workflow_triggers(active_before: int) -> tuple[bool, str]:
    """再启用一个触发器前校验活跃数上限（v1.9.0，R-203）。

    ``active_before`` 为不含本次的现有活跃数。社区版上限是 0：没有内置触发器，
    定时请用外部 cron / CI 调 ``POST /api/workflow/run``。
    """
    limit = workflow_limit("workflow_triggers")
    if limit is None or active_before < limit:
        return True, ""
    tier = _edition.get_edition()
    if tier == "community":
        return False, ("社区版没有内置触发器：请用外部 cron / CI 调用 "
                       "POST /api/workflow/run，或升级到专业版（30 个活跃触发器）")
    return False, (f"活跃触发器已达{_edition.edition_label()}上限（{limit} 个）。"
                   "请先停用其他触发器，或升级到企业版（不限）")


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
        "workflows": {k: r.get(k) for k in (
            "workflows", "workflow_triggers", "workflow_min_interval",
            "workflow_history", "workflow_history_days", "workflow_foreach_max")},
    }


def reset_for_tests() -> None:
    with _lock:
        _state.update({"date": _today(), "tasks": 0})
