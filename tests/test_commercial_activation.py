"""商业激活闭环（v1.11.14 / v1.11.15）：发行方公钥必须真置备、wheel 必须与主程序同号。

``pro/`` 是闭源商业包，**不随社区源码分发**：没有 ``pro/`` 的环境（社区 CI、
公开源码包）整文件 skip；在内部发版仓库（含 pro/）里，它就是激活链路的守门员。

深层回归（签约/验签/装包/激活、doctor、keygen --write-public-key）在
``pro/tests/test_commercial_activation_pro.py``；许可证协议 v3 / 批量发证 /
CRL / 机器绑定 / 周期复检在 ``pro/tests/test_license_v3.py``。

v1.11.15 起本文件还守住**商业运营闭环**的三条跨仓库底线：

1. 升级面板（``/api/license`` = ``edition.upgrade_status()``）能拿到本机指纹、
   序列号、宽限/复检/CRL/时钟状态与具体失败原因 —— 客服不用命令行也能排障；
2. ``activate_license`` 对"过期 / 尚未生效 / 机器不符 / 被吊销 / 时钟异常"
   给出具体原因，而不是笼统的"许可证无效"；
3. 周期复检发现许可证失效时**自动降级**社区版（保留扩展包与许可证文件），
   续期/修正后**自动恢复**，不需要重启（长驻进程的过期不再"静默续命"）。
"""

from __future__ import annotations

import json
import re
import secrets
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

import automind
from automind.core import edition

ROOT = Path(__file__).resolve().parents[1]
PRO = ROOT / "pro"

pytestmark = pytest.mark.skipif(
    not PRO.is_dir(), reason="社区源码包不含 pro/（闭源商业包，独立仓库分发）")

_KEYS_PY = PRO / "automind_pro" / "licensing" / "keys.py"
_SOURCE_KEY_RE = re.compile(r'(?m)^PUBLIC_KEY_HEX\s*=\s*"([0-9a-fA-F]{64})"')

MACHINE_A = "0123456789abcdef"
MACHINE_B = "fedcba9876543210"


# ═══════════════════════════════════════════════════════════
# 0. 发行方公钥 / wheel 版本（v1.11.14 起的两道底线）
# ═══════════════════════════════════════════════════════════


def test_issuer_public_key_is_provisioned():
    """占位公钥 = 商业版永远激活不了（fail-closed），发布前必须置备真公钥。"""
    assert _KEYS_PY.is_file(), "商业包缺少 licensing/keys.py"
    match = _SOURCE_KEY_RE.search(_KEYS_PY.read_text(encoding="utf-8"))
    assert match, "keys.py 里找不到 PUBLIC_KEY_HEX = \"<64 hex>\""
    raw = bytes.fromhex(match.group(1))
    assert len(raw) == 32
    assert any(raw), "PUBLIC_KEY_HEX 仍是 64 个 0：发行方还没 keygen --write-public-key"


def test_dist_wheel_version_matches_core():
    """pro/dist 里的 wheel 必须与主程序同号（否则 install_upgrade 会装旧代码）。"""
    wheels = sorted((PRO / "dist").glob("automind_pro-*.whl"))
    if not wheels:
        pytest.skip("pro/dist 里没有 wheel（干净检出 / CI）")
    versions = {}
    for wheel in wheels:
        match = re.match(r"automind_pro-([^-]+)-", wheel.name)
        assert match, f"wheel 文件名不规范：{wheel.name}"
        versions[wheel.name] = match.group(1)
    stale = {name: ver for name, ver in versions.items() if ver != automind.__version__}
    assert not stale, (
        f"pro/dist 里有与主程序 v{automind.__version__} 不一致的 wheel：{stale}")


# ═══════════════════════════════════════════════════════════
# 1. 商业运营状态（v1.11.15）：面板可见 / 具体错误 / 周期复检
# ═══════════════════════════════════════════════════════════


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


@pytest.fixture
def ops_home(tmp_path, monkeypatch):
    """隔离 HOME / 数据目录 / 许可证来源，并准备一把**临时测试签名密钥**。

    不使用真实发行私钥：测试只需要"发行方等价闭环"，私钥随用随弃。
    """
    pytest.importorskip("automind_pro")
    from automind_pro.licensing import guard
    from automind_pro.licensing.ed25519 import public_key_from_seed

    seed = secrets.token_bytes(32)
    monkeypatch.setenv("AUTOMIND_LICENSE_PUBLIC_KEY", public_key_from_seed(seed).hex())
    monkeypatch.setenv("AUTOMIND_LICENSE_PRIVATE_KEY", seed.hex())
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("AUTOMIND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AUTOMIND_MACHINE_ID", MACHINE_A)
    monkeypatch.setenv("AUTOMIND_LICENSE_RECHECK", "0")
    monkeypatch.delenv("AUTOMIND_LICENSE", raising=False)
    monkeypatch.delenv("AUTOMIND_LICENSE_CRL", raising=False)
    monkeypatch.delenv("AUTOMIND_LICENSE_GRACE_DAYS", raising=False)
    monkeypatch.chdir(tmp_path)
    edition.reset_for_tests()
    guard.reset_for_tests()
    yield tmp_path
    edition.reset_for_tests()
    guard.reset_for_tests()


def _license(*, expiry: str = "20991231", serial: str = "", machine: str = "",
             not_before: str = ""):
    from automind_pro.licensing import generate_license

    return generate_license("PRO", expiry, "acme", serial=serial,
                            machine=machine, not_before=not_before)


def _day(offset: int) -> str:
    return (date.today() + timedelta(days=offset)).strftime("%Y%m%d")


def _home_file(tmp_path: Path, name: str) -> Path:
    return tmp_path / "home" / ".automind" / name


def _write_state(tmp_path: Path, last_seen: int) -> None:
    path = _home_file(tmp_path, "license_state.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_seen": last_seen}), encoding="utf-8")


def _write_crl(tmp_path: Path, entries) -> None:
    path = _home_file(tmp_path, "license_crl.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"revoked": entries}), encoding="utf-8")


class TestUpgradeStatusOperationsFields:
    def test_status_exposes_operations_fields(self, ops_home):
        status = edition.upgrade_status()
        for key in ("machine_id", "license_serial", "machine_bound", "grace_days",
                    "grace_left", "recheck_seconds", "crl_revoked_count",
                    "clock_ok", "last_check", "reason_code", "detail"):
            assert key in status, f"升级面板拿不到运营字段：{key}"
        assert status["machine_id"] == MACHINE_A
        assert status["grace_days"] == 0
        assert status["clock_ok"] is True
        assert status["crl_revoked_count"] == 0
        assert status["license_version"] == 3

    def test_activated_license_exposes_serial(self, ops_home):
        result = edition.activate_license(_license())
        assert result["ok"], result
        status = edition.upgrade_status()
        assert status["step"] == "active"
        assert status["license_serial"], "v2 证也要有稳定派生序列号（sha256 前 12 位）"
        assert len(status["license_serial"]) == 12
        assert status["machine_bound"] is False
        assert status["machine_id"] == MACHINE_A
        # 激活返回里也带本机指纹 / 序列号（客服排障用）
        assert result["machine_id"] == MACHINE_A
        assert result["license_serial"] == status["license_serial"]

    def test_grace_and_recheck_config_visible(self, ops_home, monkeypatch):
        monkeypatch.setenv("AUTOMIND_LICENSE_GRACE_DAYS", "7")
        monkeypatch.delenv("AUTOMIND_LICENSE_RECHECK")
        status = edition.upgrade_status()
        assert status["grace_days"] == 7
        assert status["recheck_seconds"] == 3600

    def test_crl_count_visible(self, ops_home):
        _write_crl(ops_home, ["SERIAL0001", {"serial": "SERIAL0002"}])
        status = edition.upgrade_status()
        assert status["crl_revoked_count"] == 2
        assert status["crl_path"].endswith("license_crl.json")


class TestActivateLicenseSpecificErrors:
    def test_expired_license_has_specific_code(self, ops_home):
        result = edition.activate_license(_license(expiry=_day(-1)))
        assert result["ok"] is False
        assert result["code"] == "expired"
        assert "过期" in result["error"]
        assert not edition.license_file().exists(), "无效许可证不能落盘"

    def test_not_yet_active_has_specific_code(self, ops_home):
        result = edition.activate_license(_license(not_before=_day(3)))
        assert result["ok"] is False
        assert result["code"] == "not_yet_active"
        assert "生效" in result["error"]

    def test_machine_mismatch_names_both_fingerprints(self, ops_home):
        raw = _license(machine=MACHINE_A)
        # 换到另一台机器上激活：许可证绑定的是 A
        import os

        os.environ["AUTOMIND_MACHINE_ID"] = MACHINE_B
        try:
            result = edition.activate_license(raw)
        finally:
            os.environ["AUTOMIND_MACHINE_ID"] = MACHINE_A
        assert result["ok"] is False
        assert result["code"] == "machine_mismatch"
        assert MACHINE_A in result["error"] and MACHINE_B in result["error"]

    def test_revoked_license_has_specific_code(self, ops_home):
        raw = _license(serial="ACME20260099")
        _write_crl(ops_home, [{"serial": "ACME20260099", "reason": "refund",
                               "time": "2026-10-22T10:00:00"}])
        result = edition.activate_license(raw)
        assert result["ok"] is False
        assert result["code"] == "revoked"
        assert "吊销" in result["error"]

    def test_clock_rollback_has_specific_code(self, ops_home):
        raw = _license()
        _write_state(ops_home, int(time.time()) + 3 * 86400)
        result = edition.activate_license(raw)
        assert result["ok"] is False
        assert result["code"] == "clock_rollback"
        assert "时钟" in result["error"] or "回拨" in result["error"]

    def test_signature_invalid_keeps_legacy_code(self, ops_home):
        result = edition.activate_license("AMP2-garbage")
        assert result["ok"] is False and result["code"] == "invalid"

    def test_machine_bound_license_activates_on_bound_machine(self, ops_home):
        raw = _license(machine=MACHINE_A, serial="ACME20260100")
        result = edition.activate_license(raw)
        assert result["ok"], result
        status = edition.upgrade_status()
        assert status["machine_bound"] is True
        assert status["license_serial"] == "ACME20260100"
        assert result["machine_bound"] is True


class TestWatchdogDowngradeRecovery:
    def test_clock_rollback_downgrades_then_recovers(self, ops_home, monkeypatch):
        """短间隔复检 + 临时 HOME：时钟回拨降级社区版，修正后自动恢复。"""
        from automind_pro.licensing import guard

        monkeypatch.setenv("AUTOMIND_LICENSE_RECHECK", "0.02")
        events: list[dict] = []
        guard.register_license_change_listener(events.append)
        result = edition.activate_license(_license())
        assert result["ok"], result
        assert edition.get_edition() == "pro"
        assert _wait_until(guard.watchdog_running), "激活后必须启动周期复检线程"

        # 模拟时钟回拨：last_seen 被写到未来 → 复检判定 clock_rollback → 降级
        _write_state(ops_home, int(time.time()) + 3 * 86400)
        assert _wait_until(lambda: edition.get_edition() == "community"), (
            "时钟回拨后周期复检必须把版本降级为社区版")
        assert edition.license_info() is None
        assert not any(edition.feature_flags().values())
        # 降级保留扩展包安装状态与许可证文件（续期/修正后可直接恢复）
        assert edition.pro_package_info()["installed"] is True
        assert edition.license_file().is_file()
        assert edition._state["license_reason"]["code"] == "clock_rollback"
        invalid = [e for e in events if e.get("event") == "license_invalid"]
        assert invalid and invalid[0]["code"] == "clock_rollback"

        # 修正时钟锚点 → 复检发现恢复有效 → 自动重新激活
        _write_state(ops_home, int(time.time()) - 60)
        assert _wait_until(lambda: edition.get_edition() == "pro"), (
            "许可证恢复有效后周期复检必须自动重新激活")
        assert any(e.get("event") == "license_restored" for e in events)
        assert edition.license_info() is not None
        guard.stop_watchdog()
        edition.reset_for_tests()
        assert guard.watchdog_running() is False

    def test_expired_license_downgrades(self, ops_home, monkeypatch):
        """许可证在长驻进程里过期：复检自动降级（不再"静默续命"）。"""
        from automind_pro.licensing import guard

        monkeypatch.setenv("AUTOMIND_LICENSE_RECHECK", "0.02")
        result = edition.activate_license(_license(expiry=_day(1)))
        assert result["ok"], result
        assert edition.get_edition() == "pro"
        assert _wait_until(guard.watchdog_running)

        # 把 guard 的"现在"拨到 3 天后：许可证已过期 → 下一轮复检降级
        real_now = guard._now_ts()
        monkeypatch.setattr(guard, "_now_ts", lambda: real_now + 3 * 86400)
        assert _wait_until(lambda: edition.get_edition() == "community"), (
            "许可证在长驻进程里过期后必须自动降级")
        assert edition._state["license_reason"]["code"] == "expired"
        guard.stop_watchdog()
        edition.reset_for_tests()
