"""PermissionEngine 单元测试 — 三种审批模式、预检正则、路径穿越防护。"""

from automind.core.types import PermissionDecision, PermissionTier
from automind.tools.permissions import PermissionEngine


def _engine(mode="auto"):
    return PermissionEngine(approval_mode=mode)


class TestApprovalModes:
    def test_approve_all_allows_dangerous(self):
        eng = _engine("approve_all")
        d, _ = eng.check("terminal", PermissionTier.DANGEROUS, {"command": "rm -rf /"})
        assert d == PermissionDecision.ALLOW

    def test_ask_mode_safe_passes(self):
        eng = _engine("ask")
        d, _ = eng.check("file_read", PermissionTier.SAFE, {"path": "a.txt"})
        assert d == PermissionDecision.ALLOW

    def test_ask_mode_sensitive_requires_approval(self):
        eng = _engine("ask")
        d, _ = eng.check("file_write", PermissionTier.SENSITIVE, {"path": "a.txt"})
        assert d == PermissionDecision.ASK_USER

    def test_auto_mode_low_risk_sensitive_allowed(self):
        eng = _engine("auto")
        d, _ = eng.check("file_write", PermissionTier.SENSITIVE, {"path": "a.txt"})
        assert d == PermissionDecision.ALLOW

    def test_auto_mode_dangerous_asks(self):
        eng = _engine("auto")
        d, _ = eng.check("terminal", PermissionTier.DANGEROUS, {"command": "rm -rf /"})
        assert d == PermissionDecision.ASK_USER

    def test_invalid_mode_falls_back_to_auto(self):
        eng = _engine("nonsense")
        assert eng.approval_mode == "auto"


class TestPreflight:
    def test_dangerous_command_detected(self):
        eng = _engine()
        assert eng.preflight("rm -rf /tmp/x") == PermissionTier.DANGEROUS
        assert eng.preflight("git push --force") == PermissionTier.DANGEROUS

    def test_safe_command_detected(self):
        eng = _engine()
        assert eng.preflight("ls -la") == PermissionTier.SAFE
        assert eng.preflight("git status") == PermissionTier.SAFE

    def test_audit_log_records_each_check(self):
        eng = _engine()
        eng.check("file_read", PermissionTier.SAFE, {"path": "a.txt"})
        eng.check("terminal", PermissionTier.DANGEROUS, {"command": "rm -rf /"})
        assert len(eng.audit_log) == 2
        assert eng.audit_log[-1].tier == PermissionTier.DANGEROUS


class TestPathTraversal:
    def test_denied_path_blocked(self, tmp_path):
        eng = _engine()
        eng.policy.denied_paths = [str(tmp_path)]
        assert eng.check_path(str(tmp_path / "secret.txt")) is False

    def test_allowed_path_ok(self, tmp_path):
        eng = _engine()
        assert eng.check_path(str(tmp_path / "ok.txt")) is True
