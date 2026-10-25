"""离线 / 出网白名单冒烟自检（v1.11.14）。

被 ``verify.ps1`` 调用，也可手工执行：

    python scripts/offline/smoke_offline.py            # 人类可读
    python scripts/offline/smoke_offline.py --json     # 机器可读（verify.ps1 用）

它只做**只读**检查，不写配置、不装包、不联网（离线模式下连探测都不会有）。
检查项：
  1. 版本与策略：``automind.__version__``、``policy_status()`` 的模式 / 来源；
  2. 探测三态：``network_state()`` 的 state / 公网段是否跳过；
  3. ``network_up()`` 的返回值（离线时应为 expected_offline 且不探测）；
  4. 更新检查短路：offline / allowlist 下 ``updater.check()`` 必须 skipped；
  5. 出网判定抽样：公网目标与内网目标在策略层分别放行 / 拒绝；
  6. 出网审计日志存在性与最近拒绝记录（有日志时）。

退出码：0 = 全部符合预期；1 = 至少一项不符合（输出里会说明哪一项）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# 允许从仓库根直接运行（scripts/offline/ 不是包）
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _probe_egress(target: str) -> dict[str, Any]:
    """只做策略判定（不联网），把结果转成可读字典。"""
    from automind.core import offline

    try:
        offline.check_egress(target, tool="smoke_offline")
    except offline.EgressBlocked as e:
        return {"target": offline.sanitize_target(target), "allowed": False,
                "reason": e.reason, "message": str(e)[:300]}
    return {"target": offline.sanitize_target(target), "allowed": True}


def run_smoke(*, skip_network: bool = False) -> dict[str, Any]:
    from automind import __version__
    from automind.core import netresilience as nr
    from automind.core import offline, updater

    policy = offline.policy_status()
    mode = policy["mode"]
    if skip_network:
        network = {
            "state": "skipped", "reason": "skip_network",
            "detail": "已按 --skip-network 跳过网络探测",
            "public": {"skipped": True}, "sections": [],
        }
        up_ok, up_detail = False, "已跳过网络探测（--skip-network）"
    else:
        network = nr.network_state_sync(timeout=1.0)
        up_ok, up_detail = asyncio.run(nr.network_up(timeout=1.0, fresh=True))
    update_info = updater.check(force=False)
    egress_checks = [
        _probe_egress("https://api.deepseek.com/v1/chat/completions"),
        _probe_egress("http://127.0.0.1:11434/v1/chat/completions"),
        _probe_egress("http://127.0.0.1:8765/api/health"),
    ]

    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    add("version", bool(__version__), f"automind.__version__={__version__}")
    add("policy", mode in offline.MODES,
        f"mode={mode} source={policy['source']} offline={policy['offline']} "
        f"allowlist={policy['allowlist']}")
    if skip_network:
        add("network_state", True, "已按 --skip-network 跳过网络探测")
    else:
        add("network_state", network["state"] in ("online", "expected_offline", "degraded"),
            f"state={network['state']} reason={network['reason']} detail={network['detail']}")
    if mode == offline.MODE_OFFLINE:
        if skip_network:
            add("offline_skips_public_probe", True, "已按 --skip-network 跳过探测")
            add("network_up_expected_offline", True, "已按 --skip-network 跳过探测")
        else:
            public_skipped = bool(network.get("public", {}).get("skipped"))
            add("offline_skips_public_probe", public_skipped,
                "离线模式下公网探测已跳过" if public_skipped else "离线模式下仍探测了公网")
            add("network_up_expected_offline",
                (not up_ok) and "expected_offline" in up_detail,
                f"network_up ok={up_ok} detail={up_detail}")
        add("updater_skipped", bool(update_info.get("skipped")),
            str(update_info.get("skip_reason") or "更新检查未被跳过"))
        add("egress_denied_public", not egress_checks[0]["allowed"],
            "公网目标被拒绝" if not egress_checks[0]["allowed"] else "公网目标被放行")
        add("egress_allows_intranet", bool(egress_checks[1]["allowed"]),
            "本地模型 / 内网服务放行" if egress_checks[1]["allowed"] else "内网目标被拒绝")
    elif mode == offline.MODE_ALLOWLIST:
        add("updater_skipped", bool(update_info.get("skipped")),
            str(update_info.get("skip_reason") or "更新检查未被跳过"))
        add("egress_policy_active", policy["allowlist"],
            "白名单已生效，allow=" + ("、".join(policy.get("allow") or []) or "（空）"))
    else:
        add("open_mode", True, "open 模式：不做任何出网限制（与既有行为一致）")

    audit_events = offline.recent_events(limit=20)
    add("audit_path", bool(offline.audit_path()),
        f"audit_log={offline.audit_path()} enabled={offline.audit_enabled()}")

    return {
        "version": __version__,
        "policy": policy,
        "network": {
            "state": network["state"],
            "reason": network["reason"],
            "detail": network["detail"],
            "public_skipped": bool(network.get("public", {}).get("skipped")),
            "sections": network["sections"],
        },
        "network_up": {"ok": up_ok, "detail": up_detail},
        "updater": {
            "skipped": bool(update_info.get("skipped")),
            "skip_reason": update_info.get("skip_reason", ""),
            "available": bool(update_info.get("available")),
        },
        "egress": egress_checks,
        "audit": {
            "log_path": str(offline.audit_path()),
            "enabled": offline.audit_enabled(),
            "recent_events": audit_events[-5:],
            "stats": offline.audit_stats(),
        },
        "checks": checks,
        "ok": all(c["ok"] for c in checks),
    }


def _print_human(result: dict[str, Any]) -> None:
    print("AutoMind 离线 / 出网策略冒烟自检")
    print(f"  版本: {result['version']}")
    policy = result["policy"]
    print(f"  策略: {policy['mode']}（来源 {policy['source']}，审计 "
          f"{'开' if policy['audit'] else '关'}）")
    print(f"  网络: {result['network']['state']} — {result['network']['detail']}")
    print(f"  network_up: {result['network_up']}")
    for item in result["checks"]:
        mark = "PASS" if item["ok"] else "FAIL"
        print(f"  [{mark}] {item['name']}: {item['detail']}")
    print("结论:", "通过" if result["ok"] else "未通过")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AutoMind 离线 / 出网策略冒烟自检")
    parser.add_argument("--json", action="store_true", help="输出 JSON（verify.ps1 使用）")
    parser.add_argument("--skip-network", action="store_true",
                        help="跳过网络探测（无外网调试 / 验收时不想产生探测流量）")
    args = parser.parse_args(argv)
    try:
        result = run_smoke(skip_network=args.skip_network)
    except Exception as e:                       # 冒烟脚本本身不能抛一堆栈给运维看
        print(f"冒烟自检失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_human(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
