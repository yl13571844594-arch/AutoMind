"""同一屏上的数字不能自相矛盾。

`/api/stats` 的「累计 Token」原本读的是进程内存计数器 `_token_totals`
（重启即清零），而「按模式聚合」读的是持久化的任务历史 —— 于是重启之后，
统计页会同时显示「累计 Token 0」和「编程模式 108,203 tk」。
两个数都不报错，但至少有一个是错的，用户没有任何办法判断该信哪个。

现在两者出自同一份历史数据，结构上就不可能对不上。
"""

from __future__ import annotations

import pytest


@pytest.fixture
def srv(tmp_path):
    import automind.server as s
    s._store.config_file = tmp_path / "config.json"
    s._store.chat_file = tmp_path / "chat.json"
    s._task_history.clear()
    s._token_totals.update({"prompt": 0, "completion": 0, "total": 0, "tasks": 0})
    return s


def _record(mode: str, prompt: int, completion: int, ok: bool = True) -> dict:
    return {
        "session_id": f"s{prompt}", "task": "t", "success": ok, "output": "",
        "steps": 1, "backtracks": 0, "errors_corrected": 0,
        "tokens": prompt + completion, "prompt_tokens": prompt,
        "completion_tokens": completion, "duration_ms": 100.0,
        "plan": None, "interaction": mode,
    }


async def test_token_total_matches_sum_of_modes(srv):
    srv._task_history.extend([
        _record("chat", 100, 50),
        _record("coding", 1000, 400),
        _record("work", 20, 5, ok=False),
    ])
    r = await srv.api_stats()

    by_mode_sum = sum(v["tokens"] for v in r["by_mode"].values())
    assert r["tokens"]["total"] == by_mode_sum, "总计与分模式聚合对不上"
    assert r["tokens"]["total"] == 1575
    assert r["tokens"]["prompt"] == 1120
    assert r["tokens"]["completion"] == 455
    assert r["tokens"]["tasks"] == 3


async def test_totals_survive_restart_of_the_in_memory_counter(srv):
    """重启后内存计数器归零，历史合计不能跟着变成 0。"""
    srv._task_history.append(_record("coding", 1000, 400))
    before = (await srv.api_stats())["tokens"]

    srv._token_totals.update({"prompt": 0, "completion": 0, "total": 0, "tasks": 0})
    after = (await srv.api_stats())["tokens"]

    assert after == before, "历史合计被内存计数器的清零带偏了"
    assert after["total"] == 1400


async def test_session_counter_is_reported_separately(srv):
    """本次运行的实时增量仍要能拿到，只是不能冒充历史合计。"""
    srv._task_history.append(_record("chat", 100, 50))
    srv._token_totals.update({"prompt": 7, "completion": 3, "total": 10, "tasks": 1})

    r = await srv.api_stats()
    assert r["tokens"]["total"] == 150, "历史合计"
    assert r["tokens_session"]["total"] == 10, "本次运行增量"


async def test_records_without_interaction_land_in_one_bucket(srv):
    """早期记录没存 interaction —— 归桶要稳定，不能一半 None 一半 'other'。"""
    srv._task_history.extend([
        {**_record("chat", 10, 5), "interaction": None},
        {**_record("chat", 10, 5), "interaction": ""},
    ])
    del srv._task_history[0]["interaction"]

    r = await srv.api_stats()
    assert set(r["by_mode"]) == {"other"}
    assert r["by_mode"]["other"]["count"] == 2
