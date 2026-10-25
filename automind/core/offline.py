"""内网 / 离线出网策略 —— 离线总闸 + 出网白名单 + 出网审计的**唯一判定出口**（v1.11.14）。

为什么需要这一层
----------------
``docs/OFFLINE_DEPLOYMENT.md`` 第 5 节列出的 6 处缺口里，前三条都指向同一件事：
"系统不知道自己该不该出网"。没有总闸时，内网机器上的每个出站工具都要先超时
30 秒才报错；没有白名单时，模型被提示词注入后可以把内网数据发到任意公网地址；
探测目标硬编码公网时，``net_check`` 在内网必然误报"网络全断"。

因此这里集中提供三件事，**所有模块都只从本模块取判定**，不在各处散落
``os.environ.get(...)`` —— 散落是实现漂移的起点（某天加了新工具，忘了查总闸）。

1. 三种模式（优先级：显式 policy > OFFLINE 开关 > 默认 open）：

   * ``open``（默认）：与 v1.11.13 行为完全一致，不做任何出网限制；
   * ``allowlist``：只有 ``AUTOMIND_EGRESS_ALLOW`` / ``AUTOMIND_EGRESS_ALLOW_FILE``
     中的目标可以出网，其余**当场拒绝**（不再先超时再报错）；
   * ``offline``：禁止一切公网出网（``AUTOMIND_OFFLINE=1`` 等价）。

2. 统一判定：``policy_status()`` / ``check_egress()`` / ``is_offline()`` /
   ``is_allowlist()``。``check_egress`` 拒绝时抛 :class:`EgressBlocked`，消息里
   一定包含"当前是什么策略"与"怎么放行"——错误信息不可执行等于没报错。

3. 出网审计：每次放行 / 拒绝写一行 JSONL 到 ``<数据目录>/logs/egress.log``
   （``AUTOMIND_EGRESS_AUDIT=0`` 可关），供客户安全评审核对"到底访问了哪些外网
   地址"。**脱敏**：只记录 ``scheme://host:port/path`` 前 200 字符，query、
   fragment 与 userinfo 一律不落盘 —— 不少网关把 token 放在 query 里
   （``?api_key=...``），原样记日志等于把密钥抄一份到磁盘。

设计边界（写进注释，免得后来者误解）
------------------------------------
* 内网 / 回环目标在 offline 与 allowlist 下**默认放行**：``offline`` 管的是
  "不要出公网"，不是"连本机 Ollama、内网 SearXNG、内网 relay 都不能用"。
  需要更严（连内网也必须显式白名单）时设 ``AUTOMIND_EGRESS_STRICT=1``。
* 本模块**不 import** ``automind.tools`` 下任何模块（避免循环导入）：
  ``tools/_toolkit.py`` 反向 import 本模块，把策略层叠加在既有 SSRF 校验之后。
* 这是**应用层协作**：它拦不住 ``terminal`` 里的 ``curl``、连接器与 MCP。
  真正的强制执行必须在网络层（正向代理 / 防火墙 / DNS），两者叠加才叫设计。
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import threading
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from automind.core.logging import get_logger
from automind.core.paths import data_dir

logger = get_logger("automind.core.offline")

# ═══════════════════════════════════════════════════════════════
# 1. 常量与模式判定
# ═══════════════════════════════════════════════════════════════

MODE_OPEN = "open"
MODE_ALLOWLIST = "allowlist"
MODE_OFFLINE = "offline"
MODES: tuple[str, ...] = (MODE_OPEN, MODE_ALLOWLIST, MODE_OFFLINE)

#: 认作"真"的取值；其余（含空串）按未开启处理
_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "y", "t"})
#: 认作显式"假"的取值（``AUTOMIND_OFFLINE=0`` 不强制 offline）
_FALSE_VALUES = frozenset({"0", "false", "no", "off", "n", "f", ""})

#: URL scheme -> 默认端口（判定 host:port 白名单时要用）
_DEFAULT_PORTS = {"http": 80, "https": 443}
#: 视为"内网 / 本机"的域名后缀（尽力而为；不依赖 DNS，因为离线时 DNS 本身不可用）
_INTERNAL_SUFFIXES = (
    ".localhost", ".local", ".localdomain", ".internal", ".intranet", ".lan",
    ".corp", ".home", ".home.arpa", ".svc", ".cluster.local", ".test",
)
_INTERNAL_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})


class BlockedTarget(ValueError):
    """目标被拒绝（SSRF 私网防护 / 出网策略）。

    历史上 ``tools/_toolkit.py`` 自带一个同名类；v1.11.14 起统一放在本模块定义，
    ``_toolkit`` 只是 re-export —— 这样"出网策略拒绝"与"SSRF 拒绝"能共享一个
    ``except`` 分支，同时 :class:`EgressBlocked` 又允许调用方区分两者。
    """


class EgressBlocked(BlockedTarget):
    """出网策略拒绝（offline / allowlist 未命中 / deny 命中）。

    Attribute:
        kind: 恒为 ``"egress"``，便于调用方在 ``except BlockedTarget`` 里区分
              SSRF 拒绝与策略拒绝（SSRF 的 ``kind`` 为 ``"ssrf"``）。
        policy_mode: 拒绝时的策略模式（open/allowlist/offline）。
        host / port: 被拒绝的目标（已小写、去端口）。
        reason: 机器可读的拒绝原因（``offline_policy`` / ``allowlist_miss`` /
                ``deny_list``）。
    """

    def __init__(self, message: str, *, reason: str = "", policy_mode: str = "",
                 host: str = "", port: int | None = None) -> None:
        super().__init__(message)
        self.kind = "egress"
        self.reason = reason
        self.policy_mode = policy_mode
        self.host = host
        self.port = port


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    # 无法识别的值按"开启了"处理：安全开关宁可多禁一点
    logger.warning("offline_env_unknown_value", variable=name, value=raw[:40])
    return True


def resolve_mode() -> tuple[str, str, str]:
    """返回 ``(mode, source, note)``。

    优先级：``AUTOMIND_NETWORK_POLICY``（显式）> ``AUTOMIND_OFFLINE`` > ``open``。
    显式 policy 写了非法值 → **fail-closed 按 offline**：安全开关被写坏时，
    静默退回 open 是最危险的一种"默认值"。
    """
    explicit = os.environ.get("AUTOMIND_NETWORK_POLICY", "").strip().lower()
    if explicit:
        if explicit in MODES:
            return explicit, "AUTOMIND_NETWORK_POLICY", ""
        logger.warning("network_policy_invalid_value", value=explicit[:40])
        return (MODE_OFFLINE, "AUTOMIND_NETWORK_POLICY",
                f"AUTOMIND_NETWORK_POLICY='{explicit[:40]}' 不是合法取值"
                f"（open/allowlist/offline），已按 offline 处理")
    if _env_truthy("AUTOMIND_OFFLINE"):
        return MODE_OFFLINE, "AUTOMIND_OFFLINE", ""
    return MODE_OPEN, "default", ""


def policy_mode() -> str:
    """当前生效的模式（``open`` / ``allowlist`` / ``offline``）。"""
    return resolve_mode()[0]


def is_offline() -> bool:
    """当前离线总闸是否生效。"""
    return policy_mode() == MODE_OFFLINE


def is_allowlist() -> bool:
    """当前是否为出网白名单模式。"""
    return policy_mode() == MODE_ALLOWLIST


def is_open() -> bool:
    """当前是否为默认 open 模式（不做任何出网限制）。"""
    return policy_mode() == MODE_OPEN


def _strict_intranet() -> bool:
    """``AUTOMIND_EGRESS_STRICT=1``：内网目标也必须显式放行（默认放行内网）。"""
    return _env_truthy("AUTOMIND_EGRESS_STRICT")


# ═══════════════════════════════════════════════════════════════
# 2. 白名单 / 拒配解析
# ═══════════════════════════════════════════════════════════════

#: 规则形如 {"raw": 原始写法, "host": 小写主机, "port": int|None,
#:           "wildcard": bool, "network": ip_network|None}
Rule = dict[str, Any]


def _strip_comment(text: str) -> str:
    """去掉 ``#`` 之后的内容（配置文件里放中文注释用）。"""
    idx = text.find("#")
    return text[:idx] if idx >= 0 else text


def _parse_port(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    try:
        port = int(value)
    except ValueError:
        return None
    return port if 0 < port <= 65535 else None


def _normalize_host(value: str) -> str:
    """主机名规范化：小写、去首尾空白与结尾点、去 URL 的 userinfo/协议残留。"""
    host = value.strip().lower().strip("[]").rstrip(".")
    return host


def _parse_rule(raw: str) -> Rule | None:
    """把一条白名单写成结构化规则；无法识别返回 None（调用方记 warning）。

    支持的写法（大小写不敏感）：

    * ``api.deepseek.com`` —— 该主机任意端口；
    * ``api.deepseek.com:443`` —— 仅该端口；
    * ``*.example.com`` —— ``example.com`` 本身及其所有子域；
    * ``10.0.0.0/8`` / ``192.168.1.5`` —— IP 或 CIDR（方便内网网段整体放行）；
    * ``https://api.deepseek.com/v1`` —— 带协议的写法也认，只取主机与端口。
    """
    text = _strip_comment(str(raw or "")).strip().strip('"').strip("'")
    if not text:
        return None
    # 带协议：只取 host / port
    if "://" in text:
        try:
            parsed = urlsplit(text)
        except ValueError:
            return None
        host = _normalize_host(parsed.hostname or "")
        port = parsed.port
        if not host:
            return None
        return {"raw": text, "host": host, "port": port,
                "wildcard": host.startswith("*."), "network": None}
    # 去路径 / query / fragment：白名单条目不应该带这些
    for sep in ("/", "?", "#"):
        text = text.split(sep, 1)[0]
    text = text.strip()
    if not text:
        return None
    # IPv6 带端口：[::1]:8080
    if text.startswith("["):
        host_part, _, rest = text[1:].partition("]")
        port = _parse_port(rest[1:]) if rest.startswith(":") else None
        host = _normalize_host(host_part)
        if not host:
            return None
        return {"raw": host_part, "host": host, "port": port,
                "wildcard": False, "network": None}
    # CIDR / 纯 IP（IPv6 裸写法也有多个冒号，优先按 IP 解析）
    try:
        network = ipaddress.ip_network(text, strict=False)
        return {"raw": text, "host": str(network.network_address), "port": None,
                "wildcard": False, "network": network}
    except ValueError:
        pass
    if text.count(":") == 1:
        host_part, _, port_part = text.partition(":")
        port = _parse_port(port_part)
        if port is None:
            return None
        host = _normalize_host(host_part)
        if not host:
            return None
        return {"raw": text, "host": host, "port": port,
                "wildcard": host.startswith("*."), "network": None}
    host = _normalize_host(text)
    if not host:
        return None
    return {"raw": text, "host": host, "port": None,
            "wildcard": host.startswith("*."), "network": None}


def _split_rule_text(text: str) -> list[str]:
    """按逗号 / 分号 / 空白拆成多条；逐段去 ``#`` 注释。"""
    out: list[str] = []
    for chunk in re.split(r"[;,]", text or ""):
        chunk = _strip_comment(chunk)
        out.extend(part for part in re.split(r"\s+", chunk) if part)
    return out


def _load_allow_file(path: str) -> list[str]:
    """读白名单文件（每行一条，# 注释）；文件不可读时记 warning 而不是崩溃。"""
    if not path:
        return []
    p = Path(path).expanduser()
    try:
        if not p.is_file():
            logger.warning("egress_allow_file_missing", path=str(p))
            return []
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning("egress_allow_file_unreadable", path=str(p), error=str(e))
        return []
    items: list[str] = []
    for line in raw.splitlines():
        items.extend(_split_rule_text(line))
    return items


def _rules_from(items: list[str], *, source: str) -> list[Rule]:
    rules: list[Rule] = []
    for item in items:
        rule = _parse_rule(item)
        if rule is None:
            logger.warning("egress_rule_ignored", source=source, rule=item[:80])
            continue
        rules.append(rule)
    return rules


def allow_rules() -> list[Rule]:
    """当前生效的放行规则（环境变量 + 文件叠加，先 env 后文件）。"""
    items = _split_rule_text(os.environ.get("AUTOMIND_EGRESS_ALLOW", ""))
    items += _load_allow_file(os.environ.get("AUTOMIND_EGRESS_ALLOW_FILE", "").strip())
    return _rules_from(items, source="allow")


def deny_rules() -> list[Rule]:
    """当前生效的拒绝规则（``AUTOMIND_EGRESS_DENY``，优先级高于放行）。"""
    items = _split_rule_text(os.environ.get("AUTOMIND_EGRESS_DENY", ""))
    return _rules_from(items, source="deny")


def _rule_matches(rule: Rule, host: str, port: int | None) -> bool:
    """单条规则是否命中目标。"""
    network = rule.get("network")
    if network is not None:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        return ip in network
    rule_host = rule["host"]
    if rule_host.startswith("*."):
        base = rule_host[2:]
        host_ok = host == base or host.endswith("." + base)
    else:
        host_ok = host == rule_host
    if not host_ok:
        return False
    rule_port = rule.get("port")
    if rule_port is None:
        return True                     # 未限定端口 -> 任意端口
    return port is not None and int(port) == int(rule_port)


def _match_any(rules: list[Rule], host: str, port: int | None) -> Rule | None:
    return next((r for r in rules if _rule_matches(r, host, port)), None)


def _format_rules(rules: list[Rule], limit: int = 12) -> str:
    shown = [str(r.get("raw") or r.get("host") or "") for r in rules[:limit]]
    more = f" 等 {len(rules)} 条" if len(rules) > limit else ""
    return "、".join(shown) + more if shown else "（空）"


# ═══════════════════════════════════════════════════════════════
# 3. 目标解析与内网判定
# ═══════════════════════════════════════════════════════════════


def _looks_internal(host: str) -> bool:
    """目标是否属于"内网 / 本机"（不查 DNS，纯字符串判定）。

    离线环境里 DNS 本身不可用，而"本机 Ollama / 内网 relay / 内网 SearXNG"
    恰恰必须能用，所以这里只能做尽力而为的启发式判定：

    * IP 字面量：私网 / 回环 / 链路本地 / 保留段 → 内网；
    * ``localhost`` 及其子域、常见内网后缀（``.local`` / ``.internal`` / ``.lan``
      / ``.corp`` / ``.svc`` 等）→ 内网；
    * 单标签主机名（Docker / K8s 服务名、内网短域名）→ 内网。

    其余一律视为公网目标，交给白名单判定（fail-closed）。
    """
    host = (host or "").strip().lower().strip("[]").rstrip(".")
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified)
    if host in _INTERNAL_NAMES:
        return True
    if "." not in host:
        return True
    return any(host.endswith(suffix) for suffix in _INTERNAL_SUFFIXES)


def _parse_target(target: Any, port: int | None = None) -> tuple[str, int | None, str, str]:
    """把 URL / ``host[:port]`` 解析成 ``(host, port, scheme, path)``。

    解析不出来 → 抛 :class:`EgressBlocked`（fail-closed，而不是"看不懂就放行"）。
    """
    raw = str(target or "").strip()
    if not raw:
        raise EgressBlocked(
            "出网目标为空：无法判定是否允许出网，已按 fail-closed 拒绝。"
            "请检查调用参数（url / endpoint）是否为空。")
    if "://" in raw:
        try:
            parsed = urlsplit(raw)
        except ValueError as e:
            raise EgressBlocked(f"无法解析出网目标 {raw[:120]!r}：{e}") from e
        host = _normalize_host(parsed.hostname or "")
        if not host:
            raise EgressBlocked(f"出网目标缺少主机名：{raw[:120]}，已拒绝（fail-closed）。")
        try:
            url_port = parsed.port
        except ValueError:
            url_port = None
        eff_port = port if port is not None else url_port
        if eff_port is None:
            eff_port = _DEFAULT_PORTS.get(parsed.scheme.lower())
        return host, eff_port, parsed.scheme.lower(), parsed.path or "/"
    # 裸 host / host:port（去 path、query、fragment）
    token = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].strip()
    if token.startswith("["):
        host_part, _, rest = token[1:].partition("]")
        host = _normalize_host(host_part)
        eff_port = port
        if eff_port is None and _parse_port(rest[1:]) is not None:
            eff_port = _parse_port(rest[1:])
    elif token.count(":") == 1:
        host_part, _, port_part = token.partition(":")
        host = _normalize_host(host_part)
        eff_port = port if port is not None else _parse_port(port_part)
    else:
        host = _normalize_host(token)
        eff_port = port
    if not host:
        raise EgressBlocked(f"无法从 {raw[:120]!r} 解析出主机名，已拒绝（fail-closed）。")
    return host, eff_port, "", ""


def _display_target(host: str, port: int | None, scheme: str = "", path: str = "/") -> str:
    """错误信息 / 日志里的目标展示（不含 query、fragment、userinfo）。"""
    if scheme:
        suffix = f":{port}" if port is not None else ""
        return f"{scheme}://{host}{suffix}{path}"[:200]
    suffix = f":{port}" if port is not None else ""
    return f"{host}{suffix}"[:200]


def sanitize_target(target: Any) -> str:
    """审计脱敏：``scheme://host:port/path`` 前 200 字符，**丢掉 query / userinfo**。

    URL 里放 token 是常见做法（``?api_key=...``），审计日志不能把它抄一份；
    但主机、端口、路径对排障是必要的，所以保留。
    """
    raw = str(target or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        try:
            parsed = urlsplit(raw)
        except ValueError:
            return raw.split("?", 1)[0].split("#", 1)[0][:200]
        host = (parsed.hostname or "").lower()
        try:
            port = parsed.port
        except ValueError:
            port = None
        scheme = parsed.scheme.lower()
        if port is None:
            port = _DEFAULT_PORTS.get(scheme)
        text = _display_target(host, port, scheme, parsed.path or "/")
        return text[:200]
    return raw.split("?", 1)[0].split("#", 1)[0][:200]


# ═══════════════════════════════════════════════════════════════
# 4. 放行提示（错误文案必须可执行）
# ═══════════════════════════════════════════════════════════════


def allow_hint(host: str = "", port: int | None = None) -> str:
    """"怎么放行"的统一文案：三个出路，照抄即可。"""
    entry = f"{host}:{port}" if host and port else (host or "<目标域名>")
    return (
        "如需出网，任选其一："
        f"① 在环境变量 AUTOMIND_EGRESS_ALLOW 中加入 {entry}"
        f"（或写入 AUTOMIND_EGRESS_ALLOW_FILE 指定的文件，每行一条，支持 *.example.com）；"
        "② 若只是要临时放开，可设 AUTOMIND_NETWORK_POLICY=allowlist 并放通必要域名；"
        "③ 若本来就不需要出网，保持离线模式即可 —— 本地模型 / 内网服务不受影响。"
        "（网络层仍需代理/防火墙同步配置白名单，应用层只是第一道闸。）"
    )


def offline_hint() -> str:
    """离线模式下给用户看的一句话说明。"""
    return ("当前为离线模式（AUTOMIND_NETWORK_POLICY=offline 或 AUTOMIND_OFFLINE=1）："
            "公网访问会当场被拒绝、不会发起连接，这是**预期行为**。")


def policy_status() -> dict[str, Any]:
    """当前策略快照（健康检查 / 诊断 / 审计接口共用）。"""
    mode, source, note = resolve_mode()
    allow = allow_rules()
    deny = deny_rules()
    allow_file = os.environ.get("AUTOMIND_EGRESS_ALLOW_FILE", "").strip()
    return {
        "mode": mode,
        "source": source,
        "note": note,
        "offline": mode == MODE_OFFLINE,
        "allowlist": mode == MODE_ALLOWLIST,
        "open": mode == MODE_OPEN,
        "strict": _strict_intranet(),
        "allow": [str(r.get("raw") or r.get("host") or "") for r in allow],
        "deny": [str(r.get("raw") or r.get("host") or "") for r in deny],
        "allow_file": allow_file,
        "audit": _audit_enabled(),
        "audit_log": str(audit_path()),
        "hint": offline_hint() if mode == MODE_OFFLINE else "",
    }


# ═══════════════════════════════════════════════════════════════
# 5. 出网审计（JSONL，脱敏）
# ═══════════════════════════════════════════════════════════════

_AUDIT_LOCK = threading.Lock()
_RECENT_EVENTS: deque[dict[str, Any]] = deque(maxlen=100)
_AUDIT_COUNTERS = {"allow": 0, "deny": 0}


def _audit_enabled() -> bool:
    raw = os.environ.get("AUTOMIND_EGRESS_AUDIT", "1").strip().lower()
    return raw not in _FALSE_VALUES


def audit_path() -> Path:
    """出网审计日志路径：``<数据目录>/logs/egress.log``。"""
    return Path(data_dir()) / "logs" / "egress.log"


def audit_enabled() -> bool:
    """出网审计是否开启（``AUTOMIND_EGRESS_AUDIT=0`` 可关）。"""
    return _audit_enabled()


def _audit(event: str, *, tool: str, target: str, host: str, port: int | None,
           reason: str, mode: str, source: str) -> None:
    """写一行 JSONL；任何失败都不能把工具调用带崩（审计是旁路）。"""
    record = {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "event": event,                       # allow / deny
        "tool": str(tool or "")[:80],
        "target": sanitize_target(target),
        "host": str(host or "")[:200],
        "port": port,
        "mode": mode,
        "source": source,
        "reason": str(reason or "")[:200],
    }
    with _AUDIT_LOCK:
        _RECENT_EVENTS.append(record)
        _AUDIT_COUNTERS[event] = _AUDIT_COUNTERS.get(event, 0) + 1
        if not _audit_enabled():
            return
        try:
            path = audit_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as e:                  # pragma: no cover - 磁盘满/只读等
            logger.debug("egress_audit_write_failed", error=str(e))


def recent_events(limit: int = 20) -> list[dict[str, Any]]:
    """最近若干条出网审计事件（内存镜像，供 /api/health 等接口展示）。"""
    with _AUDIT_LOCK:
        items = list(_RECENT_EVENTS)
    return items[-max(1, min(int(limit), 100)):]


def audit_stats() -> dict[str, Any]:
    """审计统计：放行 / 拒绝计数 + 日志路径。"""
    with _AUDIT_LOCK:
        counters = dict(_AUDIT_COUNTERS)
    counters.update({"log_path": str(audit_path()), "enabled": _audit_enabled()})
    return counters


def recent_denials(limit: int = 5) -> list[dict[str, Any]]:
    """最近被拒绝的出站目标（健康检查里给运维看"谁在撞墙"）。"""
    with _AUDIT_LOCK:
        items = [e for e in _RECENT_EVENTS if e.get("event") == "deny"]
    return items[-max(1, min(int(limit), 20)):]


# ═══════════════════════════════════════════════════════════════
# 6. 统一判定入口
# ═══════════════════════════════════════════════════════════════


def check_egress(target: Any, *, port: int | None = None, tool: str = "",
                 intranet: bool = False) -> None:
    """判定一个出站目标是否允许；允许返回 ``None``，拒绝抛 :class:`EgressBlocked`。

    Args:
        target: URL（``https://api.deepseek.com/v1``）或 ``host[:port]``。
        port: 显式端口，覆盖从 URL 推断的端口（一般不用传）。
        tool: 触发判定的工具名（写审计用）。
        intranet: 调用方明确声明"这是内网目标"（如自建 SearXNG / 内网 relay）。
            声明后即使默认严格模式也会放行内网目标；公网目标仍按策略判定。

    判定顺序（重要，决定了"拒绝得快不快"）：

    1. **解析目标**（纯字符串，不查 DNS）→ 解析失败 fail-closed；
    2. **拒绝列表**优先（显式黑名单永远最高优先级）；
    3. **open**：放行；
    4. **内网目标**（或调用方声明 ``intranet``）：默认放行，``EGRESS_STRICT=1``
       时与公网同样要求白名单（offline 下直接拒绝）；
    5. **offline**：拒绝；
    6. **allowlist**：命中放行，未命中拒绝。

    第 3 步在 open 模式也会写一条 allow 审计 —— "放行也要留痕"是评审要求。
    """
    host, eff_port, scheme, path = _parse_target(target, port)
    display = _display_target(host, eff_port, scheme, path)
    mode, source, _note = resolve_mode()
    allowed_text = _format_rules(allow_rules())
    is_internal = bool(intranet) or _looks_internal(host)

    def deny(reason: str, message: str) -> None:
        _audit("deny", tool=tool, target=display, host=host, port=eff_port,
               reason=reason, mode=mode, source=source)
        raise EgressBlocked(message, reason=reason, policy_mode=mode,
                            host=host, port=eff_port)

    def allow(reason: str) -> None:
        _audit("allow", tool=tool, target=display, host=host, port=eff_port,
               reason=reason, mode=mode, source=source)

    # 拒绝列表优先：显式 deny 永远最高优先级
    denied = _match_any(deny_rules(), host, eff_port)
    if denied is not None:
        deny("deny_list",
             f"出网被策略拒绝：{display} 命中拒绝列表 AUTOMIND_EGRESS_DENY"
             f"（{denied.get('raw')}）。{allow_hint(host, eff_port)}")
        return

    if mode == MODE_OPEN:
        allow("open_mode")
        return

    if is_internal and not _strict_intranet():
        allow("intranet_target")
        return

    if mode == MODE_OFFLINE:
        deny("offline_policy",
             f"出网被策略拒绝：{offline_hint()}"
             f"被拒绝的目标：{display}。{allow_hint(host, eff_port)}")
        return

    # allowlist
    if not allow_rules():
        deny("allowlist_empty",
             f"出网被策略拒绝：当前为白名单模式（AUTOMIND_NETWORK_POLICY=allowlist），"
             f"但白名单为空，{display} 未被放行。{allow_hint(host, eff_port)}")
        return
    if _match_any(allow_rules(), host, eff_port) is not None:
        allow("allowlist")
        return
    deny("allowlist_miss",
         f"出网被策略拒绝：{display} 不在出网白名单内（当前白名单：{allowed_text}）。"
         f"{allow_hint(host, eff_port)}")


__all__ = [
    "MODES", "MODE_ALLOWLIST", "MODE_OFFLINE", "MODE_OPEN",
    "BlockedTarget", "EgressBlocked",
    "allow_hint", "allow_rules", "audit_enabled", "audit_path", "audit_stats",
    "check_egress",
    "deny_rules", "is_allowlist", "is_offline", "is_open", "offline_hint",
    "policy_mode", "policy_status", "recent_denials", "recent_events",
    "resolve_mode", "sanitize_target",
]
