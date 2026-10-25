"""内网离线策略（``automind/core/offline.py``）与探测三态的单测（v1.11.14）。

覆盖：
  * 三种模式的判定与优先级（显式 policy > ``AUTOMIND_OFFLINE`` > 默认 open）；
  * 白名单的精确 / 端口 / 通配 / 文件 / 拒绝列表语义；
  * 拒绝文案必须"可执行"（说明当前策略 + 怎么放行）；
  * 出网审计 JSONL 的脱敏（query / userinfo 不落盘）与开关；
  * 探测目标覆盖（``AUTOMIND_PROBE_HOSTS`` / ``_GATEWAY_PROBE`` / ``_LOCAL_PROBES``）；
  * 三态归类（online / expected_offline / degraded）与离线下的行为契约：
    不探测公网、不等待、``ResilientDispatch`` 不重试；
  * net_check / wait_for_network 两个工具在离线 / 故障下的输出。

所有用例都不联网：探测一律用 monkeypatch 替换 ``probe_target`` 或 socket。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import time
from pathlib import Path

import pytest

from automind.core import netresilience as nr
from automind.core import offline
from automind.core.offline import EgressBlocked
from automind.core.types import ToolResult

_PLUGIN_HOOKS = (
    Path(__file__).resolve().parents[2]
    / "automind" / "builtin_plugins" / "net_resilience" / "hooks.py"
)

_ENV_KEYS = (
    "AUTOMIND_OFFLINE", "AUTOMIND_NETWORK_POLICY",
    "AUTOMIND_EGRESS_ALLOW", "AUTOMIND_EGRESS_ALLOW_FILE", "AUTOMIND_EGRESS_DENY",
    "AUTOMIND_EGRESS_AUDIT", "AUTOMIND_EGRESS_STRICT",
    "AUTOMIND_PROBE_HOSTS", "AUTOMIND_NET_PROBE", "AUTOMIND_GATEWAY_PROBE",
    "AUTOMIND_LOCAL_PROBES",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """清掉所有相关环境变量 + 探测缓存，并把审计目录指到临时目录。

    环境变量是全局的：任何一个用例漏 cleanup 都会污染同一 pytest 进程里的
    其它用例，所以这里统一用 monkeypatch（结束时自动还原）。
    """
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AUTOMIND_DATA_DIR", str(tmp_path / "data"))
    nr._public_cache.update(at=0.0, data=None)
    nr._state_cache.update(at=0.0, data=None)
    yield


def _audit_file(tmp_path: Path) -> Path:
    return tmp_path / "data" / "logs" / "egress.log"


# ═══════════════════════════════════════════════════════════════
# 1. 模式判定
# ═══════════════════════════════════════════════════════════════


def test_default_is_open():
    status = offline.policy_status()
    assert status["mode"] == offline.MODE_OPEN
    assert status["source"] == "default"
    assert offline.is_open() and not offline.is_offline() and not offline.is_allowlist()


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_offline_switch_truthy(monkeypatch, value):
    monkeypatch.setenv("AUTOMIND_OFFLINE", value)
    assert offline.is_offline()
    status = offline.policy_status()
    assert status["mode"] == offline.MODE_OFFLINE and status["source"] == "AUTOMIND_OFFLINE"
    assert status["offline"] is True and status["hint"]


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_offline_switch_falsy(monkeypatch, value):
    monkeypatch.setenv("AUTOMIND_OFFLINE", value)
    assert offline.is_open()


def test_explicit_policy_wins_over_offline_switch(monkeypatch):
    """优先级：显式 AUTOMIND_NETWORK_POLICY > AUTOMIND_OFFLINE > 默认 open。"""
    monkeypatch.setenv("AUTOMIND_OFFLINE", "1")
    monkeypatch.setenv("AUTOMIND_NETWORK_POLICY", "open")
    assert offline.is_open()
    monkeypatch.setenv("AUTOMIND_NETWORK_POLICY", "allowlist")
    assert offline.is_allowlist()


def test_invalid_policy_value_fails_closed(monkeypatch):
    """写错的策略值不能静默退回 open —— 安全开关坏了要 fail-closed。"""
    monkeypatch.setenv("AUTOMIND_NETWORK_POLICY", "strick")
    assert offline.is_offline()
    status = offline.policy_status()
    assert status["mode"] == offline.MODE_OFFLINE
    assert "不是合法取值" in status["note"]
    with pytest.raises(EgressBlocked):
        offline.check_egress("https://api.deepseek.com/v1")


# ═══════════════════════════════════════════════════════════════
# 2. 白名单语义
# ═══════════════════════════════════════════════════════════════


def _allowlist(monkeypatch, allowed: str):
    monkeypatch.setenv("AUTOMIND_NETWORK_POLICY", "allowlist")
    monkeypatch.setenv("AUTOMIND_EGRESS_ALLOW", allowed)


def test_allowlist_exact_host(monkeypatch):
    _allowlist(monkeypatch, "api.deepseek.com")
    assert offline.check_egress("https://api.deepseek.com/v1/chat?x=1") is None
    with pytest.raises(EgressBlocked) as exc:
        offline.check_egress("https://example.com/collect")
    err = exc.value
    assert err.kind == "egress" and err.reason == "allowlist_miss"
    assert err.host == "example.com"
    assert "不在出网白名单" in str(err)
    assert "当前白名单" in str(err)
    assert "AUTOMIND_EGRESS_ALLOW" in str(err)      # 怎么放行必须写清楚
    assert "当前为离线模式" not in str(err)          # 这是白名单拒绝，不是离线拒绝


def test_allowlist_port_must_match(monkeypatch):
    _allowlist(monkeypatch, "api.example.com:8443")
    assert offline.check_egress("https://api.example.com:8443/v1") is None
    with pytest.raises(EgressBlocked):
        offline.check_egress("https://api.example.com/v1")        # 443 != 8443


def test_allowlist_wildcard(monkeypatch):
    _allowlist(monkeypatch, "*.example.com")
    assert offline.check_egress("https://a.example.com/x") is None
    assert offline.check_egress("https://deep.a.example.com/x") is None
    assert offline.check_egress("https://example.com/x") is None   # 基域也命中
    with pytest.raises(EgressBlocked):
        offline.check_egress("https://badexample.com/x")           # 后缀相似不算


def test_allowlist_separators_and_case(monkeypatch):
    _allowlist(monkeypatch, "API.DeepSeek.COM; open.feishu.cn,*.larksuite.com")
    assert offline.check_egress("https://api.deepseek.com/v1") is None
    assert offline.check_egress("https://OPEN.FEISHU.CN/open-apis") is None
    assert offline.check_egress("https://tenant.larksuite.com/x") is None
    with pytest.raises(EgressBlocked):
        offline.check_egress("https://api.moonshot.cn/v1")


def test_allowlist_file_merged(monkeypatch, tmp_path):
    allow_file = tmp_path / "egress-allow.txt"
    allow_file.write_text(
        "# 出网白名单（每行一条，# 之后是注释）\n"
        "open.feishu.cn\n"
        "\n"
        "*.corp.example.com:8443  # 内网 IM 网关\n",
        encoding="utf-8",
    )
    _allowlist(monkeypatch, "api.deepseek.com")
    monkeypatch.setenv("AUTOMIND_EGRESS_ALLOW_FILE", str(allow_file))
    status = offline.policy_status()
    assert "api.deepseek.com" in status["allow"]
    assert "open.feishu.cn" in status["allow"]
    assert offline.check_egress("https://open.feishu.cn/open-apis/x") is None
    assert offline.check_egress("https://im.corp.example.com:8443/hook") is None
    with pytest.raises(EgressBlocked):
        offline.check_egress("https://im.corp.example.com/hook")     # 端口不符


def test_allowlist_empty_rejects(monkeypatch):
    monkeypatch.setenv("AUTOMIND_NETWORK_POLICY", "allowlist")
    with pytest.raises(EgressBlocked) as exc:
        offline.check_egress("https://example.com")
    assert exc.value.reason == "allowlist_empty"
    assert "白名单为空" in str(exc.value)


def test_deny_list_is_explicit_and_wins(monkeypatch):
    # 即使在 open 模式下，显式 deny 也优先
    monkeypatch.setenv("AUTOMIND_EGRESS_DENY", "telemetry.example.com")
    with pytest.raises(EgressBlocked) as exc:
        offline.check_egress("https://telemetry.example.com/report")
    assert exc.value.reason == "deny_list"
    assert "拒绝列表" in str(exc.value)
    # deny 优先于 allow
    _allowlist(monkeypatch, "*.example.com")
    monkeypatch.setenv("AUTOMIND_EGRESS_DENY", "evil.example.com")
    with pytest.raises(EgressBlocked):
        offline.check_egress("https://evil.example.com/x")
    assert offline.check_egress("https://good.example.com/x") is None


def test_open_mode_allows(monkeypatch):
    """open 模式必须与 v1.11.13 完全一致：直接放行。"""
    assert offline.check_egress("https://api.deepseek.com/v1") is None
    assert offline.is_open()


def test_internal_targets_exempt_unless_strict(monkeypatch):
    _allowlist(monkeypatch, "api.deepseek.com")
    # 内网 / 回环 / 单标签主机名默认放行：离线场景下本地模型与内网服务必须能用
    for target in ("http://127.0.0.1:8765/api/health",
                   "http://localhost:11434/v1/chat",
                   "http://intranet-app:8080/",
                   "http://relay.corp.local/v1"):
        assert offline.check_egress(target) is None
    monkeypatch.setenv("AUTOMIND_EGRESS_STRICT", "1")
    with pytest.raises(EgressBlocked):
        offline.check_egress("http://127.0.0.1:8765/api/health")


def test_offline_denies_public_but_keeps_intranet(monkeypatch):
    monkeypatch.setenv("AUTOMIND_OFFLINE", "1")
    assert offline.check_egress("http://127.0.0.1:11434/v1/chat") is None
    with pytest.raises(EgressBlocked) as exc:
        offline.check_egress("https://api.deepseek.com/v1?api_key=SECRET")
    assert exc.value.reason == "offline_policy"
    assert "离线模式" in str(exc.value)
    assert "AUTOMIND_EGRESS_ALLOW" in str(exc.value)


# ═══════════════════════════════════════════════════════════════
# 3. 审计（脱敏 + 开关）
# ═══════════════════════════════════════════════════════════════


def test_audit_writes_deny_without_sensitive_query(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOMIND_OFFLINE", "1")
    with pytest.raises(EgressBlocked):
        offline.check_egress(
            "https://user:pass@api.deepseek.com:8443/v1/chat?api_key=TOPSECRET&x=1#frag",
            tool="http_request")
    log = _audit_file(tmp_path)
    assert log.is_file()
    text = log.read_text(encoding="utf-8")
    assert "TOPSECRET" not in text
    assert "pass@" not in text and "user:" not in text
    record = json.loads(text.splitlines()[-1])
    assert record["event"] == "deny"
    assert record["tool"] == "http_request"
    assert record["target"] == "https://api.deepseek.com:8443/v1/chat"
    assert record["host"] == "api.deepseek.com" and record["port"] == 8443
    assert record["mode"] == "offline" and record["reason"] == "offline_policy"
    assert record["ts"]


def test_audit_records_allow(monkeypatch, tmp_path):
    _allowlist(monkeypatch, "api.deepseek.com")
    offline.check_egress("https://api.deepseek.com/v1/chat", tool="http_request")
    records = [json.loads(line) for line in _audit_file(tmp_path).read_text(
        encoding="utf-8").splitlines()]
    assert records[-1]["event"] == "allow" and records[-1]["reason"] == "allowlist"


def test_audit_can_be_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOMIND_OFFLINE", "1")
    monkeypatch.setenv("AUTOMIND_EGRESS_AUDIT", "0")
    with pytest.raises(EgressBlocked):
        offline.check_egress("https://api.deepseek.com/v1")
    assert not _audit_file(tmp_path).exists()
    assert offline.recent_events()[-1]["event"] == "deny"    # 内存镜像仍然保留


def test_sanitize_target_strips_query_and_userinfo():
    assert offline.sanitize_target(
        "https://user:pass@api.deepseek.com:8443/v1/chat?token=abc#x"
    ) == "https://api.deepseek.com:8443/v1/chat"
    assert offline.sanitize_target(
        "https://api.deepseek.com/v1/chat?token=abc"
    ) == "https://api.deepseek.com:443/v1/chat"
    assert offline.sanitize_target("") == ""
    assert offline.sanitize_target("api.deepseek.com:443?token=x") == "api.deepseek.com:443"


# ═══════════════════════════════════════════════════════════════
# 4. 探测目标可配置
# ═══════════════════════════════════════════════════════════════


def test_probe_hosts_override(monkeypatch):
    monkeypatch.setenv("AUTOMIND_PROBE_HOSTS", "probe.corp.local:9443, 10.0.0.1:53")
    assert nr.probe_hosts() == (("probe.corp.local", 9443), ("10.0.0.1", 53))


def test_probe_hosts_legacy_alias(monkeypatch):
    """兼容早期设计文档里的 AUTOMIND_NET_PROBE；两个都设时以新名字为准。"""
    monkeypatch.setenv("AUTOMIND_NET_PROBE", "legacy.corp.local:443")
    assert nr.probe_hosts() == (("legacy.corp.local", 443),)
    monkeypatch.setenv("AUTOMIND_PROBE_HOSTS", "new.corp.local:443")
    assert nr.probe_hosts() == (("new.corp.local", 443),)


def test_probe_hosts_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("AUTOMIND_PROBE_HOSTS", "bad:0,99999:xyz")
    assert nr.probe_hosts() == nr.PROBE_HOSTS


def test_gateway_and_local_probes(monkeypatch):
    monkeypatch.setenv("AUTOMIND_GATEWAY_PROBE", "relay.corp.local:443")
    monkeypatch.setenv("AUTOMIND_LOCAL_PROBES", "127.0.0.1:8765;[::1]:11434")
    assert nr.gateway_probe() == ("relay.corp.local", 443)
    assert nr.local_probes() == (("127.0.0.1", 8765), ("::1", 11434))


def test_gateway_probe_absent_or_invalid(monkeypatch):
    assert nr.gateway_probe() is None
    monkeypatch.setenv("AUTOMIND_GATEWAY_PROBE", "relay:not-a-port")
    assert nr.gateway_probe() is None


# ═══════════════════════════════════════════════════════════════
# 5. 三态归类 + network_state
# ═══════════════════════════════════════════════════════════════


def _fake_probe(reachable: set[str]):
    """按 ``host:port`` 返回探测结果；未列出的目标视为不可达。"""
    calls: list[str] = []

    def fake_probe_target(host: str, port: int, timeout: float = 3.0, *, name: str = ""):
        key = f"{host}:{port}"
        calls.append(key)
        ok = key in reachable
        row = {"name": name or host, "host": host, "port": port, "dns": True,
               "ip": "203.0.113.10", "reachable": ok}
        if ok:
            row["ms"] = 1
        else:
            row["error"] = "TimeoutError: timed out"
        return row

    fake_probe_target.calls = calls
    return fake_probe_target


def test_classify_state_three_states():
    ok_public = {"configured": True, "reachable": True}
    bad_public = {"configured": True, "reachable": False}
    no_gateway = {"configured": False, "reachable": None}
    ok_gateway = {"configured": True, "reachable": True}
    bad_gateway = {"configured": True, "reachable": False}
    bad_local = {"configured": True, "reachable": False}
    assert nr.classify_state(mode="open", public=ok_public, gateway=no_gateway,
                             local=no_gateway) == ("online", "public_reachable")
    assert nr.classify_state(mode="offline", public=bad_public, gateway=no_gateway,
                             local=no_gateway) == ("expected_offline", "offline_policy")
    assert nr.classify_state(mode="open", public=bad_public, gateway=no_gateway,
                             local=no_gateway) == ("degraded", "public_unreachable")
    assert nr.classify_state(mode="offline", public=bad_public, gateway=bad_gateway,
                             local=no_gateway) == ("degraded", "gateway_unreachable")
    assert nr.classify_state(mode="open", public=ok_public, gateway=ok_gateway,
                             local=bad_local) == ("degraded", "local_service_unreachable")


def test_network_state_online(monkeypatch):
    fake = _fake_probe({"www.baidu.com:443"})
    monkeypatch.setattr(nr, "probe_target", fake)
    state = nr.network_state_sync(timeout=0.1)
    assert state["state"] == "online" and state["online"] is True
    assert state["mode"] == "open"
    assert any("网络正常" in a for a in state["advice"])


def test_network_state_degraded_gateway(monkeypatch):
    monkeypatch.setenv("AUTOMIND_GATEWAY_PROBE", "relay.corp.local:443")
    fake = _fake_probe({"www.baidu.com:443"})       # 公网可达，但网关不可达
    monkeypatch.setattr(nr, "probe_target", fake)
    state = nr.network_state_sync(timeout=0.1)
    assert state["state"] == "degraded" and state["reason"] == "gateway_unreachable"
    assert state["gateway"]["configured"] is True
    assert any("故障" in a for a in state["advice"])


def test_network_state_expected_offline_skips_public_probe(monkeypatch):
    monkeypatch.setenv("AUTOMIND_OFFLINE", "1")
    monkeypatch.setenv("AUTOMIND_GATEWAY_PROBE", "127.0.0.1:8443")
    fake = _fake_probe({"127.0.0.1:8443"})           # 网关可达
    monkeypatch.setattr(nr, "probe_target", fake)
    state = nr.network_state_sync(timeout=0.1)
    assert state["state"] == "expected_offline"
    assert state["detail"].startswith("预期离线")
    assert state["public"]["skipped"] is True
    # 关键契约：离线模式下不探测任何公网目标
    assert fake.calls == ["127.0.0.1:8443"]
    sections = {s["key"]: s for s in state["sections"]}
    assert sections["public"]["status"] == "expected_offline"
    assert any("预期" in a for a in state["advice"])


def test_network_up_offline_never_opens_socket(monkeypatch):
    monkeypatch.setenv("AUTOMIND_OFFLINE", "1")

    def _boom(*args, **kwargs):
        raise AssertionError("离线模式下不应发起任何 socket 连接")

    monkeypatch.setattr(nr.socket, "create_connection", _boom)
    ok, detail = asyncio.run(nr.network_up(fresh=True))
    assert ok is False and "expected_offline" in detail


def test_network_state_cached_refreshes_in_background(monkeypatch):
    fake = _fake_probe({"www.baidu.com:443"})
    monkeypatch.setattr(nr, "probe_target", fake)
    first = nr.network_state_cached()
    assert first["state"] in ("unknown", "online")
    for _ in range(50):                       # 等后台线程刷新（不阻塞健康检查）
        if nr.network_state_cached()["state"] == "online":
            break
        time.sleep(0.02)
    assert nr.network_state_cached()["state"] == "online"


# ═══════════════════════════════════════════════════════════════
# 6. 离线下的重试契约
# ═══════════════════════════════════════════════════════════════


def test_resilient_dispatch_offline_does_not_retry_or_wait(monkeypatch):
    monkeypatch.setenv("AUTOMIND_OFFLINE", "1")

    async def forbidden(*args, **kwargs):
        raise AssertionError("离线模式下不应探测 / 等待网络")

    monkeypatch.setattr(nr, "network_up", forbidden)
    monkeypatch.setattr(nr, "wait_for_network", forbidden)
    calls = {"n": 0}

    async def call_next(kw):
        calls["n"] += 1
        return ToolResult(tool_name="terminal", success=False,
                          error="Could not resolve host: pypi.org", exit_code=1)

    res = asyncio.run(
        nr.ResilientDispatch()("terminal", {"command": "pip install x"}, call_next))
    assert calls["n"] == 1
    assert res.metadata["net_policy"] == "offline"
    assert res.metadata["net_retries"] == 0
    assert res.metadata["network_error"] is False
    assert "离线模式" in res.error


def test_policy_rejection_is_not_classified_as_network_error():
    assert not nr.is_network_error("出网被策略拒绝：目标不在出网白名单内。")
    assert not nr.is_network_error("离线模式已生效，已跳过公网探测")
    # 真正的网络错误仍然要认出来（不能因为新增排除把原有判定弄坏）
    assert nr.is_network_error("Could not resolve host: pypi.org")


# ═══════════════════════════════════════════════════════════════
# 7. 插件工具：net_check / wait_for_network
# ═══════════════════════════════════════════════════════════════


def _load_plugin_hooks():
    spec = importlib.util.spec_from_file_location("_am_net_resilience_hooks", _PLUGIN_HOOKS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def plugin_hooks():
    return _load_plugin_hooks()


def test_net_check_offline_three_sections(plugin_hooks, monkeypatch):
    monkeypatch.setenv("AUTOMIND_OFFLINE", "1")

    def _boom(*args, **kwargs):
        raise AssertionError("离线模式 net_check 不应发起探测")

    monkeypatch.setattr(nr.socket, "create_connection", _boom)
    result = asyncio.run(plugin_hooks.NetCheckTool().execute())
    assert result.success
    out = result.output
    assert out["state"] == "expected_offline"
    assert out["online"] is False and out["expected_offline"] is True
    assert out["policy"]["mode"] == "offline"
    assert {s["key"] for s in out["sections"]} == {"local", "gateway", "public"}
    public = next(s for s in out["sections"] if s["key"] == "public")
    assert public["status"] == "expected_offline"
    assert any("预期" in a for a in out["advice"])
    assert "不要" in out["next"]                     # 明确劝止空等 / 重试
    assert "retry_stats" in out


def test_net_check_degraded_when_gateway_unreachable(plugin_hooks, monkeypatch):
    monkeypatch.setenv("AUTOMIND_GATEWAY_PROBE", "relay.corp.local:443")
    fake = _fake_probe({"www.baidu.com:443"})
    monkeypatch.setattr(nr, "probe_target", fake)
    result = asyncio.run(plugin_hooks.NetCheckTool().execute())
    out = result.output
    assert out["state"] == "degraded"
    gateway = next(s for s in out["sections"] if s["key"] == "gateway")
    assert gateway["status"] == "unreachable"
    assert any("故障" in a for a in out["advice"])


def test_wait_for_network_tool_offline_returns_immediately(plugin_hooks, monkeypatch):
    monkeypatch.setenv("AUTOMIND_OFFLINE", "1")

    async def forbidden(*args, **kwargs):
        raise AssertionError("离线模式下 wait_for_network 不应真的等待")

    monkeypatch.setattr(nr, "wait_for_network", forbidden)
    result = asyncio.run(plugin_hooks.WaitForNetworkTool().execute(timeout=60))
    assert result.success
    out = result.output
    assert out["state"] == "expected_offline" and out["waited_s"] == 0.0
    assert out["skipped"] is True
    assert "离线" in out["next"]


# ═══════════════════════════════════════════════════════════════
# 8. 更新检查短路
# ═══════════════════════════════════════════════════════════════


def test_updater_skips_in_offline_and_allowlist(monkeypatch):
    from automind.core import updater

    def forbidden(*args, **kwargs):
        raise AssertionError("离线 / 白名单模式不应发起更新检查请求")

    monkeypatch.setattr(updater, "_open", forbidden)

    monkeypatch.setenv("AUTOMIND_OFFLINE", "1")
    result = updater.check(force=True)
    assert result["skipped"] is True and "离线模式已跳过更新检查" in result["skip_reason"]
    assert result["available"] is False and result["error"] == ""
    apply_result = updater.apply_update()
    assert apply_result["skipped"] is True and "离线" in apply_result["error"]

    monkeypatch.delenv("AUTOMIND_OFFLINE")
    monkeypatch.setenv("AUTOMIND_NETWORK_POLICY", "allowlist")
    result = updater.check(force=True)
    assert result["skipped"] is True and "白名单" in result["skip_reason"]


def test_offline_quality_gate_blocks_egress_fast(monkeypatch):
    """离线拒绝必须是"当场"，而不是把网络类错误留给上层等超时。"""
    monkeypatch.setenv("AUTOMIND_OFFLINE", "1")
    started = time.monotonic()
    with pytest.raises(EgressBlocked):
        offline.check_egress("https://api.deepseek.com/v1")
    assert time.monotonic() - started < 0.5


# ═══════════════════════════════════════════════════════════════
# 9. /api/health 暴露策略与三态
# ═══════════════════════════════════════════════════════════════


def test_api_health_exposes_policy_network_and_audit(monkeypatch):
    """/api/health 必须让运维一条命令看清"策略 + 三态 + 审计"。"""
    monkeypatch.setenv("AUTOMIND_OFFLINE", "1")
    monkeypatch.setenv("AUTOMIND_GATEWAY_PROBE", "127.0.0.1:8443")
    fake = _fake_probe({"127.0.0.1:8443"})
    monkeypatch.setattr(nr, "probe_target", fake)
    # 健康检查只读缓存（首次为 unknown、后台刷新）；先产生一次快照更贴近真实运行
    nr.network_state_sync(timeout=0.1)
    from automind.server import api_health

    health = asyncio.run(api_health())
    assert health["network_policy"]["mode"] == "offline"
    assert health["network_policy"]["offline"] is True
    assert health["network"]["state"] == "expected_offline"
    assert health["network"]["public"]["skipped"] is True
    audit = health["egress_audit"]
    assert {"allow", "deny", "enabled", "log_path"} <= set(audit)
    assert audit["enabled"] is True
