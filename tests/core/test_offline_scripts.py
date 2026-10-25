"""离线运维脚本的存在性 / 语法 / 关键参数 / 干跑测试（v1.11.14）。

``scripts/offline/`` 是交付给客户照着执行的脚本，语法错误或参数缺失的代价
远高于普通代码（客户在断网环境里没有任何调试手段）。所以这里做四层校验：

1. 文件存在 + UTF-8 BOM（Windows PowerShell 5.1 无 BOM 会把中文按 ANSI 解码，
   实测直接解析失败 —— 这是踩过的坑）；
2. 用 PowerShell 自己的 Parser 做**真实语法解析**（比"跑一下试试"更早失败）；
3. 关键参数 / 安全开关仍在（防止后续重构把 -Package、--no-index、SHA-256 删掉）；
4. 干跑（-WhatIf）与备份/回滚往返：退出码 0、不产生副作用、数据能恢复。

没有 PowerShell 的机器（如纯 Linux CI）自动 skip 运行类用例，仅做静态检查。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_DIR = _REPO_ROOT / "scripts" / "offline"
_BOM = b"\xef\xbb\xbf"

_PS = shutil.which("powershell") or shutil.which("pwsh")
_PS_ARGS = ["-NoProfile", "-ExecutionPolicy", "Bypass"]
requires_ps = pytest.mark.skipif(_PS is None, reason="没有 PowerShell，跳过运行类用例")

_ENV_KEYS = (
    "AUTOMIND_OFFLINE", "AUTOMIND_NETWORK_POLICY", "AUTOMIND_EGRESS_ALLOW",
    "AUTOMIND_EGRESS_ALLOW_FILE", "AUTOMIND_EGRESS_DENY", "AUTOMIND_EGRESS_AUDIT",
    "AUTOMIND_EGRESS_STRICT", "AUTOMIND_DATA_DIR", "AUTOMIND_PROBE_HOSTS",
    "AUTOMIND_GATEWAY_PROBE", "AUTOMIND_LOCAL_PROBES",
)


def _clean_env(**overrides: str) -> dict[str, str]:
    env = dict(os.environ)
    for key in _ENV_KEYS:
        env.pop(key, None)
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(overrides)
    return env


def _run_ps(script: Path, *args: str, env: dict[str, str] | None = None,
            timeout: float = 120.0) -> subprocess.CompletedProcess:
    assert _PS is not None
    return subprocess.run(
        [_PS, *_PS_ARGS, "-File", str(script), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(_REPO_ROOT), env=env or _clean_env(), timeout=timeout,
    )


def _run_py(script: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(_REPO_ROOT), env=env or _clean_env(), timeout=120.0,
    )


# ═══════════════════════════════════════════════════════════════
# 1. 存在性 / 编码 / 参数
# ═══════════════════════════════════════════════════════════════

_SCRIPTS = ("install.ps1", "backup.ps1", "rollback.ps1", "verify.ps1")


@pytest.mark.parametrize("name", _SCRIPTS)
def test_script_exists_with_bom_and_help(name):
    path = _SCRIPT_DIR / name
    assert path.is_file(), f"缺少交付脚本：{path}"
    raw = path.read_bytes()
    assert raw.startswith(_BOM), (
        f"{name} 必须以 UTF-8 BOM 开头：Windows PowerShell 5.1 对无 BOM 的 UTF-8 "
        "脚本按 ANSI 解码，中文会乱码甚至语法错误（实测）。")
    text = raw.decode("utf-8-sig")
    assert ".SYNOPSIS" in text and ".PARAMETER" in text, "脚本需要注释式帮助（可 -? 查看）"
    assert "exit 0" in text or "exit 1" in text, "脚本需要明确退出码"


@pytest.mark.parametrize(("name", "markers"), [
    ("install.ps1", ["-Package", "AUTOMIND_NETWORK_POLICY", "AUTOMIND_OFFLINE",
                     "ExpectedVersion", "RegisterAutostart", "--no-index"]),
    ("backup.ps1", ["Get-FileHash", ".sha256", "RetainDays", "KeepLast",
                    "CreateFromDirectory"]),
    ("rollback.ps1", ["Get-FileHash", "before-rollback", "SkipStop", "Yes"]),
    ("verify.ps1", ["smoke_offline.py", "EgressLog", "ExpectedVersion", "Json",
                    "SkipNetwork"]),
])
def test_script_key_parameters_present(name, markers):
    text = (_SCRIPT_DIR / name).read_text(encoding="utf-8-sig")
    for marker in markers:
        assert marker in text, f"{name} 缺少关键参数 / 能力：{marker}"


def test_smoke_script_is_deliverable_tool():
    """冒烟脚本不是一次性文件：必须有 --help、--json、--skip-network 且可重复运行。"""
    path = _SCRIPT_DIR / "smoke_offline.py"
    assert path.is_file()
    result = _run_py(path, "--help")
    assert result.returncode == 0
    for marker in ("--json", "--skip-network"):
        assert marker in result.stdout


@requires_ps
@pytest.mark.parametrize("name", _SCRIPTS)
def test_script_powershell_syntax(name):
    """用 PowerShell 自己的 Parser 做语法解析（PS 5.1 兼容性由该 Parser 保证）。"""
    path = _SCRIPT_DIR / name
    ps_cmd = (
        "$errs=$null;"
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{path}', [ref]$null, [ref]$errs);"
        "if ($errs.Count -gt 0) {"
        "  $errs | ForEach-Object { Write-Output ('ERR ' + $_.Message) }; exit 1"
        "} else { Write-Output 'SYNTAX_OK'; exit 0 }"
    )
    result = subprocess.run(
        [_PS, *_PS_ARGS, "-Command", ps_cmd],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(_REPO_ROOT), env=_clean_env(), timeout=60.0,
    )
    assert result.returncode == 0, f"{name} 语法解析失败：\n{result.stdout}\n{result.stderr}"
    assert "SYNTAX_OK" in result.stdout


# ═══════════════════════════════════════════════════════════════
# 2. 冒烟脚本（可重复运行，离线 JSON）
# ═══════════════════════════════════════════════════════════════


def test_smoke_offline_json_repeatable(tmp_path):
    data = tmp_path / "data"
    env = _clean_env(AUTOMIND_OFFLINE="1", AUTOMIND_DATA_DIR=str(data))
    outputs = []
    for _ in range(2):
        result = _run_py(_SCRIPT_DIR / "smoke_offline.py", "--json", "--skip-network", env=env)
        assert result.returncode == 0, result.stderr
        start = result.stdout.find("{")
        assert start >= 0, result.stdout
        payload = json.loads(result.stdout[start:])
        outputs.append(payload)
    payload = outputs[0]
    assert payload["ok"] is True
    assert payload["policy"]["mode"] == "offline"
    assert payload["updater"]["skipped"] is True
    assert any(c["name"] == "egress_denied_public" and c["ok"] for c in payload["checks"])
    assert outputs[1]["ok"] is True          # 可重复运行（幂等、无残留状态）


# ═══════════════════════════════════════════════════════════════
# 3. 安装 / 备份 / 回滚 / 验收 干跑与往返
# ═══════════════════════════════════════════════════════════════


@requires_ps
def test_install_dry_run_has_no_side_effects(tmp_path):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "automind_agent-1.11.14-py3-none-any.whl").write_bytes(b"fake")
    data = tmp_path / "data"
    result = _run_ps(
        _SCRIPT_DIR / "install.ps1",
        "-Package", str(wheels), "-DataDir", str(data), "-Mode", "offline",
        "-Force", "-WhatIf",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DRY-RUN" in result.stdout
    assert not data.exists(), "安装干跑不应创建数据目录"
    assert "pip -m pip install --no-index" in result.stdout


@requires_ps
def test_backup_dry_run_creates_nothing(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "automind.db").write_text("x", encoding="utf-8")
    out = tmp_path / "backups"
    result = _run_ps(_SCRIPT_DIR / "backup.ps1", "-DataDir", str(data),
                     "-OutDir", str(out), "-WhatIf")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DRY-RUN" in result.stdout
    assert not out.exists() or not list(out.glob("*.zip"))


@requires_ps
def test_backup_and_rollback_roundtrip(tmp_path):
    data = tmp_path / "data"
    (data / "logs").mkdir(parents=True)
    (data / "automind.db").write_text("original-db", encoding="utf-8")
    (data / "notes.txt").write_text("original-notes", encoding="utf-8")
    config = tmp_path / ".automind_config.json"
    config.write_text('{"api_keys": {}}', encoding="utf-8")
    license_file = tmp_path / ".automind_license"
    license_file.write_text("license-fake", encoding="utf-8")
    backups = tmp_path / "backups"

    backup = _run_ps(
        _SCRIPT_DIR / "backup.ps1", "-DataDir", str(data), "-OutDir", str(backups),
        "-ConfigFile", str(config), "-LicenseFile", str(license_file))
    assert backup.returncode == 0, backup.stdout + backup.stderr
    zips = list(backups.glob("automind-backup-*.zip"))
    assert len(zips) == 1
    sha_file = Path(str(zips[0]) + ".sha256")
    manifest_file = Path(str(zips[0]) + ".manifest.json")
    assert sha_file.is_file() and manifest_file.is_file(), "备份必须带 SHA-256 清单与 manifest"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
    assert len(manifest["sha256"]) == 64 and manifest["file"] == zips[0].name

    # 破坏当前状态后回滚，必须恢复备份时的内容
    (data / "notes.txt").write_text("changed-after-backup", encoding="utf-8")
    rollback = _run_ps(
        _SCRIPT_DIR / "rollback.ps1", "-Backup", str(zips[0]),
        "-DataDir", str(data), "-SkipStop", "-Yes")
    assert rollback.returncode == 0, rollback.stdout + rollback.stderr
    assert (data / "notes.txt").read_text(encoding="utf-8").startswith("original-notes")
    assert (data / "automind.db").read_text(encoding="utf-8") == "original-db"
    # 回滚前的状态必须保留（"怎么都不会把当前状态弄丢"）
    assert list(tmp_path.glob("data.before-rollback-*")), "回滚前的数据目录应保留一份"


@requires_ps
def test_verify_passes_for_offline_acceptance(tmp_path):
    data = tmp_path / "data"
    (data / "logs").mkdir(parents=True)
    (data / "config").mkdir()
    (data / "config" / "offline-config.json").write_text(
        json.dumps({"version": "1.11.14", "mode": "offline"}), encoding="utf-8")
    record = {
        "ts": "2026-10-21T12:00:00+00:00", "event": "deny", "tool": "http_request",
        "target": "https://api.deepseek.com:443/v1", "host": "api.deepseek.com",
        "port": 443, "mode": "offline", "source": "AUTOMIND_OFFLINE",
        "reason": "offline_policy",
    }
    (data / "logs" / "egress.log").write_text(json.dumps(record, ensure_ascii=False) + "\n",
                                             encoding="utf-8")
    env = _clean_env(AUTOMIND_OFFLINE="1", AUTOMIND_DATA_DIR=str(data))
    result = _run_ps(
        _SCRIPT_DIR / "verify.ps1", "-DataDir", str(data), "-ExpectMode", "offline",
        "-SkipServiceCheck", "-SkipNetwork", "-Json", env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    start = result.stdout.find("{")
    assert start >= 0, result.stdout
    payload = json.loads(result.stdout[start:])
    assert payload["ok"] is True
    assert payload["mode"] == "offline"
    assert payload["failed"] == 0


@requires_ps
def test_verify_fails_when_expected_mode_mismatches(tmp_path):
    data = tmp_path / "data"
    (data / "logs").mkdir(parents=True)
    env = _clean_env(AUTOMIND_OFFLINE="1", AUTOMIND_DATA_DIR=str(data))
    result = _run_ps(
        _SCRIPT_DIR / "verify.ps1", "-DataDir", str(data), "-ExpectMode", "allowlist",
        "-SkipServiceCheck", "-SkipNetwork", env=env)
    assert result.returncode == 1, "策略与期望不符时验收必须失败（不能假绿）"
    assert "policy_mode" in result.stdout
