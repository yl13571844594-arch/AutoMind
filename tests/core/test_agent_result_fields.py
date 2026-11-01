"""AgentResult 可诊断字段 + server 落库/API 映射（v1.11.21）。

## 背景

此前"任务为什么没成功"只存在于自然语言输出里：历史页/失败卡片只能显示一句
笼统的失败，无法区分"步数上限 / 预算耗尽 / 模型不可达 / 部分交付"，也不知道
有没有可续跑的断点、烧了几次 LLM 调用。本版给 ``AgentResult`` 加了五个带默认
值的字段（向后兼容），server 在落库前统一映射 + 脱敏 + 截断。

这里锁住三件事：
1. 字段默认值/可赋值（老代码不传也不会炸）；
2. server 的映射/脱敏/截断口径；
3. `/api/config/autopilot` 能读写新的修复轮预算参数，并带生效时机信息。
"""

from __future__ import annotations

import pytest

from automind.core.types import AgentResult, TokenUsage

# ═══════════════════════════════════════════════════════════
# 1. AgentResult 字段
# ═══════════════════════════════════════════════════════════


class TestAgentResultFields:
    def test_new_fields_have_safe_defaults(self):
        r = AgentResult(success=True)
        assert r.stop_reason == ""
        assert r.error == ""
        assert r.resumable is False
        assert r.budget_exhausted is False
        assert r.llm_calls == 0

    def test_fields_are_assignable_and_serialized(self):
        r = AgentResult(
            success=False,
            output="部分交付",
            token_usage=TokenUsage(calls=7),
            stop_reason="task_budget",
            error="Token 预算已用尽",
            resumable=True,
            budget_exhausted=True,
            llm_calls=7,
        )
        dumped = r.model_dump()
        assert dumped["stop_reason"] == "task_budget"
        assert dumped["error"] == "Token 预算已用尽"
        assert dumped["resumable"] is True
        assert dumped["budget_exhausted"] is True
        assert dumped["llm_calls"] == 7

    def test_old_style_construction_still_works(self):
        """旧调用方只传 success/output 也不能被新字段破坏（向后兼容）。"""
        r = AgentResult(success=True, output="ok", steps_executed=3)
        assert r.output == "ok" and r.steps_executed == 3
        assert r.stop_reason == ""


# ═══════════════════════════════════════════════════════════
# 2. server 落库映射（脱敏 / 截断 / 缺字段容错）
# ═══════════════════════════════════════════════════════════


class _OldResult:
    """没有新字段的老快照/替身 —— 映射必须给出安全默认值。"""

    success = False
    output = ""
    token_usage = TokenUsage()


class TestServerObservabilityFields:
    def test_maps_result_fields(self):
        import automind.server as srv

        r = AgentResult(
            success=False,
            stop_reason="max_iterations",
            error="跑到步数上限",
            resumable=True,
            budget_exhausted=False,
            llm_calls=12,
        )
        fields = srv._result_observability_fields(r)
        assert fields == {
            "stop_reason": "max_iterations",
            "error": "跑到步数上限",
            "resumable": True,
            "budget_exhausted": False,
            "llm_calls": 12,
        }

    def test_missing_attributes_fall_back_to_defaults(self):
        import automind.server as srv

        fields = srv._result_observability_fields(_OldResult())
        assert fields["stop_reason"] == ""
        assert fields["error"] == ""
        assert fields["resumable"] is False
        assert fields["budget_exhausted"] is False
        assert fields["llm_calls"] == 0

    def test_llm_calls_falls_back_to_token_usage(self):
        """续跑 `_resume_*` 可能没填 llm_calls（0），但 token_usage.calls 是真的。

        Lead 审阅发现：直接用 result.llm_calls 会把历史记录里的调用次数覆盖成
        0。这里锁住回退口径。
        """
        import automind.server as srv

        r = AgentResult(success=False, llm_calls=0,
                        token_usage=TokenUsage(calls=7))
        assert srv._result_observability_fields(r)["llm_calls"] == 7

        # 老结果对象：根本没有 llm_calls 字段，只有 token_usage.calls
        old = _OldResult()
        old.token_usage = TokenUsage(calls=9)
        assert srv._result_observability_fields(old)["llm_calls"] == 9

        # 字段有真实值时以字段为准（不因 token_usage 不同口径而被覆盖）
        r2 = AgentResult(success=False, llm_calls=5,
                         token_usage=TokenUsage(calls=99))
        assert srv._result_observability_fields(r2)["llm_calls"] == 5

    def test_error_is_truncated_before_persisting(self):
        import automind.server as srv

        r = AgentResult(success=False, error="错" * 3000)
        fields = srv._result_observability_fields(r)
        assert len(fields["error"]) == 1000

    def test_error_is_redacted_when_switch_is_on(self, monkeypatch):
        import automind.server as srv

        monkeypatch.setattr(srv, "_REDACT_SECRETS", True)
        secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
        r = AgentResult(success=False, error=f"调用失败：{secret}")
        fields = srv._result_observability_fields(r)
        assert secret not in fields["error"]
        assert "REDACTED" in fields["error"]

    def test_non_string_error_is_sanitized(self):
        import automind.server as srv

        r = AgentResult(success=False)
        r.error = None            # type: ignore[assignment]
        assert srv._result_observability_fields(r)["error"] == ""

    def test_usage_and_resumable_helpers(self):
        from types import SimpleNamespace

        import automind.server as srv

        usage = TokenUsage(calls=5)
        agent = SimpleNamespace(llm=SimpleNamespace(usage=usage))
        assert srv._agent_llm_calls(agent) == 5
        assert srv._usage_calls(None) == 0
        assert srv._agent_llm_calls(SimpleNamespace()) == 0

        agent._agent_state = SimpleNamespace(metadata={"status": "partial"})
        assert srv._agent_resumable(agent) is True
        agent._agent_state = SimpleNamespace(metadata={"status": "completed"})
        assert srv._agent_resumable(agent) is False
        assert srv._agent_resumable(SimpleNamespace()) is False


# ═══════════════════════════════════════════════════════════
# 3. /api/config/autopilot：修复轮预算参数
# ═══════════════════════════════════════════════════════════


class TestAutopilotFixBudgetKnob:
    _ADMIN = {"X-Admin-Token": "test-admin-token"}

    @pytest.fixture(autouse=True)
    def _admin_env(self, monkeypatch):
        monkeypatch.setenv("AUTOMIND_ADMIN_TOKEN", "test-admin-token")

    @pytest.fixture
    def client(self, tmp_path):
        from fastapi.testclient import TestClient

        import automind.server as srv

        prev_agent, prev_auth = srv._agent, srv._AUTH_TOKEN
        prev_cfg = getattr(srv._store, "config_file", None)
        srv._agent = None
        srv._AUTH_TOKEN = ""
        srv._store.config_file = tmp_path / "cfg.json"
        c = TestClient(srv.app, headers=self._ADMIN)
        try:
            yield c
        finally:
            srv._agent = prev_agent
            srv._AUTH_TOKEN = prev_auth
            srv._store.config_file = prev_cfg

    def test_snapshot_exposes_value_and_effective_timing(self, client):
        snap = client.get("/api/config/autopilot").json()
        assert snap["auto_verify_fix_max_iterations"] == 1
        info = snap["effective"]["auto_verify_fix_max_iterations"]
        assert info["timing"] == "next_task"
        assert "修复轮" in info["note"]

    def test_roundtrip_accepts_zero_and_upper_values(self, client):
        r = client.post("/api/config/autopilot",
                        json={"auto_verify_fix_max_iterations": 3}).json()
        assert r["applied"]["auto_verify_fix_max_iterations"] == 3
        assert r["auto_verify_fix_max_iterations"] == 3
        assert isinstance(r["auto_verify_fix_max_iterations"], int)

        # 0 = 关闭自动修复，是合法值
        r0 = client.post("/api/config/autopilot",
                         json={"auto_verify_fix_max_iterations": 0}).json()
        assert r0["applied"]["auto_verify_fix_max_iterations"] == 0

        # 恢复默认，避免影响其它用例（_agent/config 是进程级共享）
        client.post("/api/config/autopilot",
                    json={"auto_verify_fix_max_iterations": 1})

    def test_negative_value_is_rejected(self, client):
        r = client.post("/api/config/autopilot",
                        json={"auto_verify_fix_max_iterations": -1}).json()
        assert "auto_verify_fix_max_iterations" in r["rejected"]
