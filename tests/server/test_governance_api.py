"""治理中心 REST API 回归测试（v1.11.19 / WS-GOV-SURFACE）。

覆盖四件事：

1. ``GET /api/governance/summary`` 的响应契约：``summary / policy / ledger / chain``
   字段齐全，策略快照实时读当前 Agent（审批模式 / 工作区隔离 / 写冲突策略 /
   审计开关 / 工具风险分级 / 高危工具清单）；
2. ``GET /api/governance/audit`` 的过滤（tool / decision / source / kind / session）、
   limit 上限与**非法参数 400**；
3. ``GET /api/governance/verify`` 的链完整性与**篡改检测** —— 这是整套治理能力的
   信任根：如果改了账本还能报 ok，哈希链就只是装饰；``GET /api/governance/export``
   的结构；
4. 管理员守卫：``/api/governance/*`` 与 ``DELETE /api/audit`` 同权（远程无令牌
   403、带对 ``X-Admin-Token`` 放行、本机回环放行），且**不误伤**其它只读端点；
   读取故障必须 fail-open（500 + 可操作错误，而不是拖垮 server 或静默返回空）。

账本一律用 ``tmp_path`` + ``governance.reset_for_tests`` 隔离，不写仓库 .automind；
Agent 用桩对象，不构造真实 LLM、不联网。前端部分用源码级断言守 AuditView.tsx 的
治理入口与旧数据回退（不跑浏览器，构建由 Lead 统一做）。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import automind.server as srv
from automind.core.types import PermissionTier

ROOT = Path(__file__).resolve().parents[2]
AUDIT_VIEW = ROOT / "web" / "src" / "components" / "views" / "AuditView.tsx"

GOVERNANCE_PATHS = (
    "/api/governance/summary",
    "/api/governance/audit",
    "/api/governance/verify",
    "/api/governance/export",
)


# ═══════════════════════════════════════════════════════════
# 桩：Agent / 工具 / 账本隔离
# ═══════════════════════════════════════════════════════════


class _StubTool:
    def __init__(self, name: str, tier: PermissionTier) -> None:
        self.name = name
        self.permission_tier = tier


def _stub_agent() -> SimpleNamespace:
    """只提供策略快照需要的属性；不构造真实 Agent / LLM。"""
    return SimpleNamespace(
        config=SimpleNamespace(execution=SimpleNamespace(
            isolate_workspace=True,
            write_conflict_policy="block",
            approval_mode="ask",
        )),
        permissions=SimpleNamespace(approval_mode="ask"),
        _interaction=SimpleNamespace(value="work"),
        tool_registry=SimpleNamespace(list_all=lambda: [
            _StubTool("terminal", PermissionTier.DANGEROUS),
            _StubTool("file_read", PermissionTier.SAFE),
            _StubTool("web_fetch", PermissionTier.SENSITIVE),
        ]),
    )


@pytest.fixture(autouse=True)
def _isolate_server(monkeypatch):
    """不连真实配置 / 不构造真实 Agent / 不受本机管理员令牌环境影响。"""
    monkeypatch.setattr(srv, "_AUTH_TOKEN", "", raising=False)
    monkeypatch.setattr(srv, "_read_config", lambda: {}, raising=False)
    monkeypatch.delenv("AUTOMIND_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(srv, "get_agent", lambda: _stub_agent())


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    """把治理账本隔离到 tmp_path；用例结束恢复模块状态，避免污染其它测试。"""
    from automind.core import governance

    monkeypatch.delenv("AUTOMIND_GOVERNANCE_AUDIT", raising=False)
    saved = (governance._root_override, governance._enabled_override, governance._ledger)
    governance.reset_for_tests(tmp_path / "governance")
    try:
        yield governance
    finally:
        (governance._root_override, governance._enabled_override,
         governance._ledger) = saved


@pytest.fixture()
def local():
    """本机回环客户端（管理动作放行）。"""
    return TestClient(srv.app, client=("127.0.0.1", 51000))


def _seed(governance) -> None:
    """写入 4 条覆盖不同 tool / decision / source / kind 的记录。"""
    governance.record_decision(
        tool="terminal", tier="dangerous", risk=90, decision="deny_unattended",
        reason="无人值守通道高危拦截", source="direct", session_id="s1",
        params={"command": "rm -rf /tmp/x", "api_key": "sk-abc123"})
    governance.record_decision(
        tool="file_read", tier="safe", risk=10, decision="allow",
        reason="只读操作", source="react", session_id="s2")
    governance.record_execution(
        tool="file_read", tier="safe", success=True, duration_ms=12.5,
        source="react", session_id="s2")
    governance.record_decision(
        tool="web_fetch", tier="sensitive", risk=50, decision="deny",
        reason="策略拒绝", source="workflow", session_id="s1")


# ═══════════════════════════════════════════════════════════
# 1. summary：契约与策略快照
# ═══════════════════════════════════════════════════════════


class TestSummary:
    def test_contract_fields(self, local, ledger):
        _seed(ledger)
        r = local.get("/api/governance/summary")

        assert r.status_code == 200, r.text
        data = r.json()
        assert {"summary", "policy", "ledger", "chain", "generated_at"} <= set(data)
        assert isinstance(data["generated_at"], float)

        summary = data["summary"]
        assert summary["total"] == 4
        assert summary["by_source"]["react"] == 2
        assert summary["denied"] >= 1

        policy = data["policy"]
        assert {
            "approval_mode", "interaction", "workspace_isolation", "write_conflict_policy",
            "audit_enabled", "tools", "sensitive", "edition",
        } <= set(policy)
        assert policy["approval_mode"] == "ask"        # 实时读 permissions.approval_mode
        assert policy["interaction"] == "work"
        assert policy["workspace_isolation"] is True
        assert policy["write_conflict_policy"] == "block"
        assert policy["audit_enabled"] is True
        assert policy["tools"]["total"] == 3
        assert policy["tools"]["by_tier"]["dangerous"] == 1
        assert policy["tools"]["by_tier"]["sensitive"] == 1
        assert policy["tools"]["dangerous"] == ["terminal"]
        assert policy["sensitive"]["scope"] == "web"
        assert isinstance(policy["sensitive"]["data_dir"], str)
        assert policy["edition"] in ("community", "pro", "enterprise")

        info = data["ledger"]
        assert {"root", "files", "bytes", "enabled", "last_seq"} <= set(info)
        assert info["enabled"] is True
        assert info["files"] >= 1
        assert info["bytes"] > 0
        assert info["last_seq"] == 4

        chain = data["chain"]
        assert {"ok", "checked", "broken_at"} <= set(chain)
        assert chain["ok"] is True
        assert chain["checked"] == 4
        assert chain["broken_at"] is None

    def test_empty_ledger_is_honest_zero(self, local, ledger):
        """空账本要如实显示 0 / 校验通过 0 条，而不是报错或空白。"""
        r = local.get("/api/governance/summary")

        assert r.status_code == 200
        data = r.json()
        assert data["summary"]["total"] == 0
        assert data["ledger"]["files"] == 0
        assert data["ledger"]["last_seq"] == 0
        assert data["chain"]["ok"] is True
        assert data["chain"]["checked"] == 0

    def test_policy_read_failure_does_not_break_summary(self, local, ledger, monkeypatch):
        """策略快照读不到 Agent 时仍要返回可解释的默认值（治理中心先能打开）。"""
        monkeypatch.setattr(srv, "get_agent", lambda: (_ for _ in ()).throw(RuntimeError("agent down")))

        r = local.get("/api/governance/summary")

        assert r.status_code == 200, r.text
        policy = r.json()["policy"]
        assert policy["approval_mode"] == "auto"
        assert policy["tools"]["total"] == 0

    def test_audit_switch_is_reflected(self, local, ledger, monkeypatch):
        """AUTOMIND_GOVERNANCE_AUDIT=0 时策略快照要如实显示"审计开关=关闭"。"""
        monkeypatch.setenv("AUTOMIND_GOVERNANCE_AUDIT", "0")

        policy = local.get("/api/governance/summary").json()["policy"]

        assert policy["audit_enabled"] is False


# ═══════════════════════════════════════════════════════════
# 2. audit：过滤 / limit / 非法参数
# ═══════════════════════════════════════════════════════════


class TestAudit:
    def test_entries_are_old_to_new(self, local, ledger):
        _seed(ledger)

        r = local.get("/api/governance/audit")

        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 4
        assert data["truncated"] is False
        seqs = [e["seq"] for e in data["entries"]]
        assert seqs == sorted(seqs), "账本记录必须按 seq 正序（旧→新）返回"
        assert data["entries"][0]["tool"] == "terminal"
        assert {"ts", "kind", "tool", "tier", "decision", "reason", "source",
                "governed", "params", "seq"} <= set(data["entries"][0])

    def test_empty_ledger_returns_no_entries(self, local, ledger):
        r = local.get("/api/governance/audit")

        assert r.status_code == 200
        data = r.json()
        assert data["entries"] == []
        assert data["count"] == 0
        assert data["truncated"] is False

    def test_params_are_redacted_on_the_surface(self, local, ledger):
        """接口层不能把明文密钥带出去（账本落盘前的脱敏要一路透传到响应）。"""
        _seed(ledger)
        data = local.get("/api/governance/audit").json()
        first = data["entries"][0]
        assert first["params"]["api_key"] == "***REDACTED***"
        assert "sk-abc123" not in json.dumps(data, ensure_ascii=False)

    @pytest.mark.parametrize("query, expected", [
        ("tool=file_read", 2),
        ("tool=terminal", 1),
        ("decision=deny", 1),
        ("source=react", 2),
        ("source=workflow", 1),
        ("kind=execution", 1),
        ("kind=decision", 3),
        ("session_id=s1", 2),
        ("source=react&kind=execution", 1),
    ])
    def test_filters(self, local, ledger, query, expected):
        _seed(ledger)
        r = local.get(f"/api/governance/audit?{query}")

        assert r.status_code == 200, r.text
        assert r.json()["count"] == expected, f"{query} 过滤结果不对：{r.text[:300]}"

    def test_filters_are_echoed(self, local, ledger):
        _seed(ledger)
        data = local.get("/api/governance/audit?tool=file_read&source=react").json()

        assert data["filters"]["tool"] == "file_read"
        assert data["filters"]["source"] == "react"
        assert data["filters"]["limit"] == 200

    def test_limit_returns_the_latest_n(self, local, ledger):
        _seed(ledger)

        data = local.get("/api/governance/audit?limit=2").json()

        assert data["count"] == 2
        assert data["truncated"] is True, "触到 limit 要如实标记可能被截断"
        assert [e["seq"] for e in data["entries"]] == [3, 4]

    def test_large_limit_is_not_truncated(self, local, ledger):
        _seed(ledger)
        assert local.get("/api/governance/audit?limit=100").json()["truncated"] is False

    @pytest.mark.parametrize("query", [
        "limit=0", "limit=-1", "limit=1001", "limit=abc", "limit=1.5",
        "kind=bogus",
    ])
    def test_invalid_params_return_400(self, local, ledger, query):
        r = local.get(f"/api/governance/audit?{query}")

        assert r.status_code == 400, f"{query} 应当 400，实际 {r.status_code}：{r.text[:200]}"
        assert "error" in r.json()


# ═══════════════════════════════════════════════════════════
# 3. verify / export
# ═══════════════════════════════════════════════════════════


class TestVerify:
    def test_intact_chain_passes(self, local, ledger):
        _seed(ledger)

        r = local.get("/api/governance/verify")

        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["checked"] == 4
        assert data["broken_at"] is None
        assert data["bytes"] > 0
        assert isinstance(data["files"], list)

    def test_tampered_record_is_detected(self, local, ledger, tmp_path):
        """把第一条决策从「拦截」改成「放行」—— 必须报断链并指出位置与哈希。"""
        _seed(ledger)
        ledger_dir = tmp_path / "governance"
        files = sorted(ledger_dir.glob("ledger-*.jsonl"))
        assert files, "测试账本没有落盘"
        lines = files[-1].read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        first["decision"] = "allow"                     # 篡改内容、hash 保持不变
        lines[0] = json.dumps(first, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"))
        files[-1].write_text("\n".join(lines) + "\n", encoding="utf-8")
        ledger.reset_for_tests(ledger_dir)              # 模拟重启后重新从磁盘读

        r = local.get("/api/governance/verify")

        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is False, "篡改后的账本竟然校验通过 —— 哈希链形同虚设"
        assert data["broken_at"] is not None
        assert data["broken_at"] == first["seq"]
        assert data["reason"] == "record_hash_mismatch"
        assert data["expected"] and data["actual"]
        assert data["expected"] != data["actual"]

    def test_tamper_is_visible_in_summary_chain(self, local, ledger, tmp_path):
        _seed(ledger)
        ledger_dir = tmp_path / "governance"
        files = sorted(ledger_dir.glob("ledger-*.jsonl"))
        lines = files[-1].read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        first["reason"] = "TAMPERED"
        lines[0] = json.dumps(first, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"))
        files[-1].write_text("\n".join(lines) + "\n", encoding="utf-8")
        ledger.reset_for_tests(ledger_dir)

        chain = local.get("/api/governance/summary").json()["chain"]

        assert chain["ok"] is False
        assert chain["broken_at"] is not None

    @pytest.mark.parametrize("query", ["max_records=0", "max_records=-3",
                                       "max_records=abc", "max_records=999999999"])
    def test_invalid_max_records_return_400(self, local, ledger, query):
        r = local.get(f"/api/governance/verify?{query}")

        assert r.status_code == 400, f"{query} 应当 400，实际 {r.status_code}"


class TestExport:
    def test_structure(self, local, ledger):
        _seed(ledger)

        r = local.get("/api/governance/export?limit=100")

        assert r.status_code == 200
        data = r.json()
        assert {"format", "entries", "chain", "exported_at"} <= set(data)
        assert data["format"] == "json"
        assert len(data["entries"]) == 4
        assert data["chain"]["ok"] is True
        assert isinstance(data["exported_at"], float)
        assert "attachment" in r.headers.get("content-disposition", "")

    def test_default_limit_and_validation(self, local, ledger):
        _seed(ledger)
        assert local.get("/api/governance/export").status_code == 200
        assert local.get("/api/governance/export?limit=0").status_code == 400
        assert local.get("/api/governance/export?limit=1001").status_code == 400


# ═══════════════════════════════════════════════════════════
# 4. 管理员守卫 / fail-open
# ═══════════════════════════════════════════════════════════


class TestAdminGuard:
    @pytest.mark.parametrize("path", GOVERNANCE_PATHS)
    def test_remote_without_token_is_refused(self, path):
        """TestClient 默认 client 是 "testclient"（非回环）—— 正好当远程用。"""
        r = TestClient(srv.app).get(path)

        assert r.status_code == 403, f"{path} 远程无令牌竟然放行：{r.status_code} {r.text[:120]}"
        assert "AUTOMIND_ADMIN_TOKEN" in r.json()["error"], "要告诉运维怎么开，而不是一句 403"

    def test_remote_with_the_right_token_passes(self, ledger, monkeypatch):
        monkeypatch.setenv("AUTOMIND_ADMIN_TOKEN", "gov-admin")
        _seed(ledger)

        r = TestClient(srv.app).get(
            "/api/governance/summary", headers={"X-Admin-Token": "gov-admin"})

        assert r.status_code == 200, r.text[:300]

    def test_remote_with_the_wrong_token_is_refused(self, ledger, monkeypatch):
        monkeypatch.setenv("AUTOMIND_ADMIN_TOKEN", "gov-admin")

        r = TestClient(srv.app).get(
            "/api/governance/summary", headers={"X-Admin-Token": "wrong"})

        assert r.status_code == 403

    def test_local_loopback_is_the_admin_console(self, local, ledger):
        _seed(ledger)
        assert local.get("/api/governance/summary").status_code == 200

    def test_paths_are_classified_as_admin(self):
        for path in GOVERNANCE_PATHS:
            assert srv._is_admin_path(path), f"{path} 没有被登记为管理动作"
            assert srv._is_admin_read_path(path), f"{path} 缺少只读管理门"

    def test_gating_is_narrow(self):
        """只收窄治理读接口，其它只读端点 / 旧审计读接口保持可读。"""
        assert not srv._is_admin_read_path("/api/skills")
        assert not srv._is_admin_read_path("/api/audit")
        remote = TestClient(srv.app)
        assert remote.get("/api/audit").status_code == 200, "旧 /api/audit 的 GET 被误伤"
        assert remote.get("/api/governance/summary").status_code == 403

    def test_legacy_delete_audit_still_admin_gated(self):
        """DELETE /api/audit 的既有管理员守卫不能被这次改动带坏。"""
        r = TestClient(srv.app).delete("/api/audit")
        assert r.status_code == 403


class TestFailOpen:
    def test_missing_governance_module_returns_503(self, monkeypatch):
        def _boom():
            raise ImportError("no automind.core.governance")

        monkeypatch.setattr(srv, "_governance_module", _boom)
        r = TestClient(srv.app, client=("127.0.0.1", 51000)).get(
            "/api/governance/summary")

        assert r.status_code == 503
        assert "治理" in r.json()["error"]

    def test_read_failure_returns_500_with_actionable_error(self, monkeypatch):
        class _Broken:
            def enabled(self):
                return True

            def __getattr__(self, name):
                def _raise(*args, **kwargs):
                    raise OSError("账本目录不可读")
                return _raise

        monkeypatch.setattr(srv, "_governance_module", lambda: _Broken())
        client = TestClient(srv.app, client=("127.0.0.1", 51000))

        for path in GOVERNANCE_PATHS:
            r = client.get(path)
            assert r.status_code == 500, f"{path} 读取故障应当 500，实际 {r.status_code}"
            assert "服务端日志" in r.json()["error"]
            assert "账本目录不可读" in r.json()["error"]


# ═══════════════════════════════════════════════════════════
# 5. 前端源码级回归（不跑浏览器）
# ═══════════════════════════════════════════════════════════


class TestAuditViewSource:
    @pytest.fixture(scope="class")
    @classmethod
    def src(cls) -> str:
        return AUDIT_VIEW.read_text(encoding="utf-8")

    def test_consumes_the_persistent_ledger(self, src):
        # 前端 apiGet 的路径不带 /api 前缀（client 统一拼接）；注释里保留完整路径便于检索
        assert "'/governance/audit" in src or "/api/governance/audit" in src, \
            "审计页没有消费持久化治理账本"
        assert "/governance/summary" in src, "审计页没有读取治理摘要"

    def test_has_chain_verification_entry(self, src):
        assert "验证审计链" in src
        assert "/governance/verify" in src, "审计页缺少验证审计链的接口调用"

    def test_has_json_export_entry(self, src):
        assert "导出 JSON" in src
        assert "/governance/export" in src, "审计页缺少导出治理账本的接口调用"

    def test_governance_section_is_labelled(self, src):
        assert "运行底座治理" in src
        for label in ("审批模式", "工作区隔离", "写冲突策略", "审计开关", "高危工具"):
            assert label in src, f"治理策略快照缺少「{label}」"

    def test_legacy_audit_fallback_is_kept(self, src):
        assert "apiGet('/audit')" in src, "旧 /api/audit 回退被删掉了"
        assert "内存审计" in src, "缺少『账本为空时展示内存审计』的说明"

    def test_clear_button_only_clears_memory_audit(self, src):
        assert "apiDelete('/audit')" in src
        assert "apiDelete('/governance" not in src, "账本文件不应提供界面清空入口"

    def test_no_exotic_number_formatting(self, src):
        """本页只需整数/一位小数展示，不应引入 toFixed 之类的格式化地雷。"""
        assert "toFixed" not in src
        assert "undefined" not in src, "本页不应渲染 undefined（数字/字段一律先兜底）"
