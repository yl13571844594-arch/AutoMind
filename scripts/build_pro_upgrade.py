#!/usr/bin/env python3
"""一键发行 AutoMind Pro 升级包（.ampkg）。

一条命令完成：构建 wheel → 版本一致性校验 → `issue_license.py doctor` 自检 →
签发许可证并打成 `.ampkg` → 打印 sha256 与客户升级步骤。

用法::

    # 专业版，到 2027-12-31
    python scripts/build_pro_upgrade.py --tier PRO --expiry 20271231 --customer acme

    # 企业版，永久，50 席
    python scripts/build_pro_upgrade.py --tier ENT --expiry 00000000 --customer bigcorp --seats 50

    # 同一版本只出一次包时，可跳过重复构建 wheel
    python scripts/build_pro_upgrade.py --tier PRO --no-build ...

前置（发行方一次性，见 RELEASE.md「商业激活闭环」）：
    1. `python pro/tools/issue_license.py keygen --write-public-key`（公钥已置备）
    2. 签发私钥在 `pro/.license-private/private_key.hex` 或环境变量
       `AUTOMIND_LICENSE_PRIVATE_KEY` 里。

本脚本**不读取、不打印私钥内容**，签发全部交给 `pro/tools/issue_license.py`。
退出码：0 成功；2 版本不一致；3 wheel 缺失/构建失败；4 doctor 自检失败；5 出包失败。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRO = ROOT / "pro"
TOOL = PRO / "tools" / "issue_license.py"


def _read_version(init_file: Path) -> str:
    """从 `__init__.py` 里读 __version__（不 import，避免副作用）。"""
    try:
        text = init_file.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r'(?m)^__version__\s*=\s*"([\d.]+)"', text)
    return m.group(1) if m else ""


def _toml_version(path: Path) -> str:
    """读 pyproject.toml 的 [project].version。"""
    try:
        import tomllib

        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("project", {}).get("version", ""))


def _fail(exit_code: int, stage: str, **detail) -> int:
    print(json.dumps({"ok": False, "stage": stage, **detail},
                     ensure_ascii=False, indent=2))
    return exit_code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="build_pro_upgrade",
        description="AutoMind Pro 一键发行：构建 wheel → 自检 → 生成 .ampkg")
    ap.add_argument("--tier", choices=["PRO", "pro", "ENT", "ent"], default="PRO",
                    help="PRO=专业版 / ENT=企业版")
    ap.add_argument("--expiry", default="00000000", help="到期日 YYYYMMDD；00000000 = 永久")
    ap.add_argument("--customer", default="acme", help="客户标识（字母数字，无连字符）")
    ap.add_argument("--seats", type=int, default=0, help="席位数；0 = 不限")
    ap.add_argument("--out", default=str(ROOT / "dist" / "upgrades"),
                    help="升级包输出目录（默认 dist/upgrades/）")
    ap.add_argument("--no-build", action="store_true",
                    help="跳过 wheel 构建（pro/dist 里已有当前版本 wheel 时用）")
    args = ap.parse_args(argv)

    # ── 1. 版本一致性：核心 / 商业包 __init__ / 商业包 pyproject 三处同号 ──
    core = _read_version(ROOT / "automind" / "__init__.py")
    pro_pkg = _read_version(PRO / "automind_pro" / "__init__.py")
    pro_toml = _toml_version(PRO / "pyproject.toml")
    if not core or core != pro_pkg or core != pro_toml:
        return _fail(2, "version_check", core=core, pro_init=pro_pkg, pro_pyproject=pro_toml,
                     hint="三处版本号必须一致；改版本号后重新构建")
    wheel = PRO / "dist" / f"automind_pro-{core}-py3-none-any.whl"

    # ── 2. 构建商业扩展包 wheel ──
    if not args.no_build:
        print(f"[1/3] build automind_pro-{core} wheel ...", flush=True)
        code = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", ".", "--no-deps",
             "--no-build-isolation", "-w", "dist"],
            cwd=str(PRO), check=False).returncode
        if code != 0:
            return _fail(3, "wheel_build", exit_code=code)
    if not wheel.is_file():
        return _fail(3, "wheel_missing", wheel=str(wheel),
                     hint="先执行 python -m pip wheel . --no-deps --no-build-isolation -w dist"
                          "（在 pro/ 目录）")
    print(f"[1/3] wheel ok: {wheel.name} ({wheel.stat().st_size} bytes)", flush=True)

    # ── 3. 发行前自检：公钥/私钥配对、wheel 内置公钥、已有 bundle 可激活 ──
    print("[2/3] issue_license.py doctor ...", flush=True)
    if subprocess.run([sys.executable, str(TOOL), "doctor"], cwd=str(ROOT),
                      check=False).returncode != 0:
        return _fail(4, "doctor", hint="按 doctor 输出逐项修复后再出包")

    # ── 4. 签发许可证 + 打包 .ampkg ──
    print("[3/3] bundle ...", flush=True)
    cmd = [sys.executable, str(TOOL), "bundle", args.tier.upper(), args.expiry,
           args.customer, "--wheel", str(wheel), "--out", args.out]
    if args.seats:
        cmd += ["--seats", str(args.seats)]
    proc = subprocess.run(cmd, cwd=str(ROOT), check=False, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        return _fail(5, "bundle", exit_code=proc.returncode,
                     stderr=(proc.stderr or proc.stdout or "")[-2000:])
    try:
        info = json.loads(proc.stdout)
    except ValueError:
        return _fail(5, "bundle_output", stdout=proc.stdout[-2000:])
    bundle = Path(info["bundle"])
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    report = {
        "ok": True,
        "version": core,
        "bundle": str(bundle),
        "bundle_name": bundle.name,
        "size": bundle.stat().st_size,
        "sha256": digest,
        "tier": info["tier"],
        "customer": info["customer"],
        "expiry": info["expiry"],
        "seats": args.seats,
        "wheel": info.get("wheel") or wheel.name,
        "key_id": info["key_id"],
        "license": info["license"],
        "customer_steps": [
            "打开 AutoMind → 设置 →「升级专业版 / 企业版」",
            "把 .ampkg 拖进「一键升级」",
            "看到「已升级为…」即完成；若提示重启，重启一次后自动激活",
        ],
        "verify_command": f'python pro/tools/issue_license.py verify "{info["license"]}"',
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
