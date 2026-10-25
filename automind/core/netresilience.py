"""网络韧性 —— 让"网络抖一下"不再等于"工具连续失败 3 次被熔断"（v1.11.6）。

问题（用户实测）：执行 ``pip install`` / ``git clone`` / ``npm install`` 时网络短暂中断，
terminal 连续三次报错，熔断器把 terminal 整个关掉（"已连续失败 3 次，停止重试"），
之后哪怕网络恢复了，这一轮任务里 terminal 也再不能用 —— 任务只能失败交付。

这一层把"网络类失败"从"工具坏了"里分出来，分三处生效：

1. **识别**（``is_network_error``）：按报错文本识别 DNS 失败、连接重置、超时、
   证书握手中断、代理不可达等，中英文都认；本机地址（localhost / 127.0.0.1）的
   "连接被拒绝"不算 —— 那是服务没起来，不是网络问题，重试也没用。
2. **自动重试**（``ResilientDispatch``，由内置插件 ``net_resilience`` 挂到工具分派上）：
   只对**可安全重做**的调用重试 —— 下载依赖、克隆 / 拉取代码、GET 请求、搜索；
   ``curl -X POST``、``git push`` 这类重做会产生重复副作用的一律不自动重试。
   重试前先探测网络，没恢复就等（有上限），并按 2s / 5s / 10s 退避；
   第二次起对 pip / npm 自动改用国内镜像（``AUTOMIND_NET_MIRROR=0`` 可关）。
3. **熔断半开**（``react_executor``）：网络类失败单独计数、阈值更高；熔断后只要
   探测到网络已恢复就重新放行，而不是整轮任务都不再调用这个工具。

v1.11.9：**文件锁重试从这里整体移除**，统一交给内置插件 ``fs_lock_guard``。
此前两边都在做（core 这里 0.5/1/2s、插件 0/0.4/1.2s），同一次写文件被占用时
最坏要执行 12 次、干等约 12 秒 —— 两层退避叠乘成了"重试风暴"。
``is_transient_lock`` 保留为公共判定助手（插件与测试在用）。

所有探测都是"连得上就算通"的 TCP 级判定，不发任何业务请求，不需要外网凭据。

v1.11.14（内网离线三件套）：探测不再是"一串硬编码公网目标，连不上就报网络坏"，
而是**三段式 + 三态**：
  * 三段：本地服务（``AUTOMIND_LOCAL_PROBES``）/ 网关（``AUTOMIND_GATEWAY_PROBE``）
    / 公网（``AUTOMIND_PROBE_HOSTS`` 可覆盖，默认仍是国内外站点）；
  * 三态：``online`` / ``expected_offline``（离线策略下公网不可达是预期，不是故障）
    / ``degraded``（网关或本地服务不可达 = 真故障）；
  * 离线模式下 ``network_up()`` **不发起任何公网探测**，立即返回
    ``expected_offline``；``ResilientDispatch`` 也不再"等 60 秒 × 3 次"空转。
"""

from __future__ import annotations

import asyncio
import concurrent.futures as _cf
import os
import re
import socket
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

from automind.core import offline
from automind.core.logging import get_logger

logger = get_logger("automind.core.netresilience")

# ═══════════════════════════════════════════════════════════════
# 1. 识别
# ═══════════════════════════════════════════════════════════════

_NET_PATTERNS = re.compile("|".join([
    # DNS
    r"name or service not known", r"temporary failure in name resolution",
    r"could not resolve host", r"getaddrinfo (?:failed|enotfound)", r"\benotfound\b",
    r"nodename nor servname", r"no address associated with hostname", r"11001",
    r"dns (?:lookup|resolution) failed", r"failed to resolve",
    # 连接
    r"connection (?:reset|aborted|timed out|closed by remote)", r"\beconnreset\b",
    r"\betimedout\b", r"\beconnaborted\b", r"\beai_again\b", r"\besockettimedout\b",
    r"network is unreachable", r"no route to host", r"\benetunreach\b", r"\behostunreach\b",
    r"remote end closed connection", r"remotedisconnected", r"connection broken",
    r"incompleteread", r"read timed out", r"connect timeout", r"connecttimeout",
    r"readtimeout", r"timed out while", r"operation timed out", r"10060", r"10054", r"10051",
    r"max retries exceeded", r"newconnectionerror", r"protocolerror",
    r"failed to establish a new connection", r"unable to access '.+': ",
    r"early eof", r"rpc failed", r"the remote end hung up", r"gnutls_handshake",
    r"ssl: (?:unexpected_eof|decryption_failed)", r"eof occurred in violation of protocol",
    r"unexpected eof while reading", r"tlsv1 alert", r"proxyerror", r"tunnel connection failed",
    r"socket hang up", r"network error", r"request to .* failed, reason:",
    r"could not fetch url", r"there was a problem confirming the ssl certificate",
    r"no matching distribution found.*\n?.*(?:connection|timed out)",
    # 中文提示
    r"网络(?:连接)?(?:中断|异常|不可达|错误|超时)", r"无法解析(?:主机|域名)", r"连接(?:超时|被重置|中断)",
    r"由于连接方在一段时间后没有正确答复", r"远程主机强迫关闭了一个现有的连接",
    r"请求的名称有效，但是找不到请求类型的数据", r"不知道这样的主机",
]), re.IGNORECASE)

#: 本机地址上的失败不是"网络问题"（多半是服务没起来），不按网络错误处理
_LOCAL_HINT = re.compile(r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|::1)(?::\d+)?", re.I)
_REFUSED = re.compile(r"connection refused|econnrefused|actively refused|积极拒绝", re.I)

#: 出网策略拒绝是**确定性**的：文案里出现这些词就不是"网络抖动"，
#: 不能交给"等网络恢复 + 重试"去处理（重试一万次也还是被拒绝）。
_POLICY_REJECT = re.compile(
    r"出网被策略拒绝|cannot be reached under the current egress policy|egress_denied"
    r"|不在出网白名单|离线模式已生效|AUTOMIND_EGRESS_ALLOW|AUTOMIND_NETWORK_POLICY"
    r"|AUTOMIND_OFFLINE",
    re.I,
)


def is_network_error(text: Any) -> bool:
    """报错文本是否属于"网络暂时不通"一类（可等待 / 重试解决）。

    v1.11.14：出网策略（离线 / 白名单）导致的拒绝**不算**网络错误 ——
    它是确定性拒绝，等多久都不会恢复；判成网络错误会触发无谓的等待与重试。
    """
    s = str(text or "")
    if not s:
        return False
    if _POLICY_REJECT.search(s):
        return False
    if _REFUSED.search(s):
        # "连接被拒绝"：远端地址才算网络问题；本机端口拒绝 = 服务没起来
        return not _LOCAL_HINT.search(s)
    if _NET_PATTERNS.search(s):
        return not (_LOCAL_HINT.search(s) and not re.search(r"proxy|代理", s, re.I))
    return False


#: 文件被占用 / 被锁：Windows 上杀毒扫描、Excel、编辑器常把文件短暂锁住。
#: v1.11.9 起这里是锁判定的**单一事实来源**（fs_lock_guard 插件调用它决定是否
#: 重试；重试动作本身自该版起统一由插件负责，core 不再重放 —— 见模块 docstring）。
#: 模式清单是原 core 与原插件两份清单的并集，中英文都认、小写比较。
_LOCK_PATTERNS = re.compile("|".join([
    # Windows：另一个程序正在使用 / 拒绝访问（同步盘、杀软并发扫描常见）
    r"winerror 32", r"winerror 33", r"winerror 5",
    r"being used by another process", r"the process cannot access",
    r"cannot access the file", r"另一个程序正在使用此文件", r"进程无法访问", r"拒绝访问",
    # POSIX / 跨平台
    r"permission denied", r"\[errno 11\]", r"errno 13", r"\bebusy\b",
    r"text file busy", r"resource temporarily unavailable",
    # 数据库
    r"database is locked", r"database table is locked",
]), re.IGNORECASE)


def is_transient_lock(text: Any) -> bool:
    """报错文本是否属于"文件被占用 / 被锁"（等一下就可能好的那类）。"""
    return bool(_LOCK_PATTERNS.search(str(text or "")))


def result_text(result: Any) -> str:
    """把 ToolResult 里可能含报错的部分拼成一段文本（error + stderr + stdout 尾部）。"""
    parts = [str(getattr(result, "error", "") or "")]
    out = getattr(result, "output", None)
    if isinstance(out, dict):
        for k in ("stderr", "stdout", "error", "message"):
            v = out.get(k)
            if v:
                parts.append(str(v)[-4000:])
    elif out:
        parts.append(str(out)[-2000:])
    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════
# 2. 探测
# ═══════════════════════════════════════════════════════════════

#: 默认探测目标：国内外各一组，任一能连上即视为网络可用（不发业务请求，只做 TCP 连接）
PROBE_HOSTS: tuple[tuple[str, int], ...] = (
    ("www.baidu.com", 443), ("mirrors.tuna.tsinghua.edu.cn", 443),
    ("pypi.org", 443), ("github.com", 443), ("1.1.1.1", 443), ("223.5.5.5", 53),
)

#: 展示名（net_check 输出用；未登记的主机名原样显示）
PROBE_LABELS: dict[str, str] = {
    "www.baidu.com": "百度", "mirrors.tuna.tsinghua.edu.cn": "清华镜像",
    "pypi.tuna.tsinghua.edu.cn": "清华 PyPI", "registry.npmmirror.com": "npmmirror",
    "pypi.org": "PyPI 官方", "registry.npmjs.org": "npm 官方", "github.com": "GitHub",
    "1.1.1.1": "Cloudflare DNS", "223.5.5.5": "阿里 DNS",
}

#: 归为"国内站点"的后缀（只影响 net_check 的 domestic/overseas 归纳，不影响判定）
_DOMESTIC_SUFFIXES = ("baidu.com", "tsinghua.edu.cn", "npmmirror.com", "aliyun.com", "qq.com")

#: 公网探测结果缓存（5 秒）：一次重试里不要反复探测
_PROBE_TTL = 5.0
#: 三态快照缓存（15 秒）：/api/health 每次都探测会把健康检查拖成秒级
_STATE_TTL = 15.0
_public_cache: dict[str, Any] = {"at": 0.0, "data": None}
_state_cache: dict[str, Any] = {"at": 0.0, "data": None}
_state_lock = threading.Lock()
_state_refreshing = {"active": False}


# ── 配置解析：探测目标全部可用环境变量覆盖 ──────────────────


def _endpoint_str(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _parse_endpoint(raw: Any, default_port: int = 443) -> tuple[str, int] | None:
    """解析 ``host:port``（不带端口时用 default_port；IPv6 写成 ``[::1]:443``）。

    解析失败返回 None —— 由调用方决定"回退默认值 + warning"还是"视为未配置"。
    """
    text = str(raw or "").strip().strip('"').strip("'")
    text = text.split("#", 1)[0].split("/", 1)[0].strip()
    if not text:
        return None
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        if not host:
            return None
        port = _parse_port(rest[1:]) if rest.startswith(":") else default_port
        return (host.strip().lower(), port) if port else None
    if text.count(":") == 1:
        host, _, port_text = text.partition(":")
        port = _parse_port(port_text)
        return (host.strip().lower(), port) if host.strip() and port else None
    if ":" in text:                                  # 裸 IPv6：不带端口
        return text.strip().lower(), default_port
    return text.strip().lower(), default_port


def _parse_port(value: str) -> int | None:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return port if 0 < port <= 65535 else None


def _parse_endpoint_list(raw: str, default_port: int = 443) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for chunk in re.split(r"[,;\s]+", str(raw or "").strip()):
        chunk = chunk.split("#", 1)[0].strip()
        if not chunk:
            continue
        endpoint = _parse_endpoint(chunk, default_port)
        if endpoint is None:
            logger.warning("probe_endpoint_invalid", value=chunk[:80])
            continue
        out.append(endpoint)
    return out


def probe_hosts() -> tuple[tuple[str, int], ...]:
    """公网探测目标：``AUTOMIND_PROBE_HOSTS``（``host:port,host:port``）可覆盖。

    为什么必须可覆盖：内网 / 白名单部署里默认的公网目标**本来就不可达**，
    继续探测只会得到"网络全断"的假象。覆盖值非法时回退默认值并打 warning
    —— 静默回退会让人以为配置生效了。

    兼容旧社区的 ``AUTOMIND_NET_PROBE``（早期设计文档里的名字），
    两个都设时以 ``AUTOMIND_PROBE_HOSTS`` 为准。
    """
    raw = (os.environ.get("AUTOMIND_PROBE_HOSTS", "").strip()
           or os.environ.get("AUTOMIND_NET_PROBE", "").strip())
    if not raw:
        return PROBE_HOSTS
    parsed = _parse_endpoint_list(raw)
    if not parsed:
        logger.warning("probe_hosts_invalid_fallback", value=raw[:120])
        return PROBE_HOSTS
    return tuple(parsed)


def gateway_probe() -> tuple[str, int] | None:
    """网关 / relay 探测目标：``AUTOMIND_GATEWAY_PROBE=host:port``（可选）。"""
    raw = os.environ.get("AUTOMIND_GATEWAY_PROBE", "").strip()
    if not raw:
        return None
    endpoint = _parse_endpoint(raw)
    if endpoint is None:
        logger.warning("gateway_probe_invalid", value=raw[:120])
    return endpoint


def local_probes() -> tuple[tuple[str, int], ...]:
    """本地服务探测目标：``AUTOMIND_LOCAL_PROBES=127.0.0.1:8765,...``（可选）。"""
    raw = os.environ.get("AUTOMIND_LOCAL_PROBES", "").strip()
    if not raw:
        return ()
    return tuple(_parse_endpoint_list(raw))


def _proxy_env() -> dict[str, str]:
    return {k.upper(): v for k, v in os.environ.items()
            if k.upper() in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")}


def _proxy_target() -> tuple[str, int] | None:
    """配了代理时先看代理本身通不通（直连外网本来就可能不通）。"""
    env = _proxy_env()
    proxy = env.get("HTTPS_PROXY") or env.get("ALL_PROXY") or ""
    match = re.match(r"^\w+://(?:[^@/]+@)?([^:/]+)(?::(\d+))?", proxy)
    if not match:
        return None
    return match.group(1), int(match.group(2) or 80)


# ── 探测原子操作 ────────────────────────────────────────────


def probe_target(host: str, port: int, timeout: float = 3.0, *, name: str = "") -> dict[str, Any]:
    """对单个 ``host:port`` 做 DNS + TCP 可达探测（只建连接，不发业务数据）。

    返回的字典保留 DNS 明细：内网里"域名解析失败"与"端口不通"是两种完全不同的
    故障，合成一句"不可达"会让运维无从下手。
    """
    started = time.perf_counter()
    row: dict[str, Any] = {"name": name or PROBE_LABELS.get(host, host),
                           "host": host, "port": port}
    try:
        ip = socket.gethostbyname(host)
    except OSError as e:
        row.update({"dns": False, "reachable": False, "error": f"DNS 失败：{e}"})
        return row
    row.update({"dns": True, "ip": ip})
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            row.update({"reachable": True,
                        "ms": round((time.perf_counter() - started) * 1000)})
    except OSError as e:
        row.update({"reachable": False, "error": f"{type(e).__name__}: {e}"})
    return row


def _probe_many(targets: list[tuple[str, int]] | tuple[tuple[str, int], ...],
                timeout: float) -> list[dict[str, Any]]:
    """并发探测一组目标（单个 timeout 上限，整体 ≈ timeout 而非 N×timeout）。"""
    if not targets:
        return []
    workers = min(8, len(targets))
    with _cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="automind-probe") as ex:
        futures = [ex.submit(probe_target, host, port, timeout) for host, port in targets]
        return [f.result() for f in futures]


def _is_domestic(host: str) -> bool:
    host = (host or "").lower()
    if host == "223.5.5.5" or host == "119.29.29.29":
        return True
    return any(host == s or host.endswith("." + s) for s in _DOMESTIC_SUFFIXES)


def _group_of(host: str) -> str:
    """公网目标的国内 / 国外分组（只用于结果归纳）。"""
    return "domestic" if _is_domestic(host) else "overseas"


def _summarize_rows(rows: list[dict[str, Any]], *, limit: int = 4) -> str:
    if not rows:
        return "（无探测目标）"
    reachable = [r for r in rows if r.get("reachable")]
    if reachable:
        head = _endpoint_str(reachable[0]["host"], reachable[0]["port"])
        return f"{len(reachable)}/{len(rows)} 个目标可达（如 {head}）"
    errors = []
    for row in rows[:limit]:
        reason = str(row.get("error") or "")
        reason = reason.split("\n", 1)[0][:80]
        errors.append(f"{_endpoint_str(row['host'], row['port'])} {reason}")
    return "全部不可达：" + "；".join(errors)


# ── 三组探测（公网 / 网关 / 本地）──────────────────────────


def _public_probe_sync(timeout: float, *, fresh: bool = False) -> dict[str, Any]:
    """公网可达性探测（带 5 秒缓存）。"""
    now = time.monotonic()
    cached = _public_cache["data"]
    if not fresh and cached is not None and now - _public_cache["at"] < _PROBE_TTL:
        return dict(cached)
    targets = list(probe_hosts())
    proxy = _proxy_target()
    if proxy is not None and proxy not in targets:
        targets.insert(0, proxy)
    rows = _probe_many(targets, timeout)
    for row in rows:
        row["group"] = _group_of(row["host"])
    domestic = any(r.get("reachable") and r.get("group") == "domestic" for r in rows)
    overseas = any(r.get("reachable") and r.get("group") != "domestic" for r in rows)
    data = {
        "configured": True,
        "skipped": False,
        "reachable": any(r.get("reachable") for r in rows),
        "domestic": domestic,
        "overseas": overseas,
        "targets": rows,
        "detail": _summarize_rows(rows),
        "proxy": _proxy_env(),
    }
    _public_cache.update(at=time.monotonic(), data=data)
    return dict(data)


def _local_probe_sync(timeout: float) -> dict[str, Any]:
    targets = local_probes()
    if not targets:
        return {"configured": False, "reachable": None, "targets": [],
                "detail": "未配置本地服务探测（可选：AUTOMIND_LOCAL_PROBES=127.0.0.1:8765）"}
    rows = _probe_many(targets, timeout)
    return {"configured": True, "reachable": any(r.get("reachable") for r in rows),
            "targets": rows, "detail": _summarize_rows(rows)}


def _gateway_probe_sync(timeout: float) -> dict[str, Any]:
    endpoint = gateway_probe()
    if endpoint is None:
        return {"configured": False, "reachable": None, "target": "",
                "detail": "未配置网关探测（可选：AUTOMIND_GATEWAY_PROBE=relay.corp.local:443）"}
    row = probe_target(endpoint[0], endpoint[1], timeout, name="网关")
    return {"configured": True, "reachable": bool(row.get("reachable")),
            "target": _endpoint_str(*endpoint), "row": row,
            "detail": _summarize_rows([row])}


def classify_state(*, mode: str, public: dict[str, Any],
                   gateway: dict[str, Any], local: dict[str, Any]) -> tuple[str, str]:
    """三态归类（纯函数，便于单测）。

    * ``degraded``：配置了网关 / 本地服务但不可达 —— 这是**真故障**；
    * ``expected_offline``：离线策略下公网不可达 —— **预期**，不是故障；
    * ``online``：公网可达。

    优先级：故障 > 预期离线 > 在线。离线部署里网关不通同样是故障，
    不能因为"反正离线"就把它一起说成预期。
    """
    if local.get("configured") and local.get("reachable") is False:
        return "degraded", "local_service_unreachable"
    if gateway.get("configured") and gateway.get("reachable") is False:
        return "degraded", "gateway_unreachable"
    if mode == offline.MODE_OFFLINE:
        return "expected_offline", "offline_policy"
    if public.get("reachable"):
        return "online", "public_reachable"
    return "degraded", "public_unreachable"


def _sections_and_advice(*, policy: dict[str, Any], state: str,
                         public: dict[str, Any], gateway: dict[str, Any],
                         local: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """net_check / /api/health 的三段式结论与建议（文案即产品）。"""
    mode = str(policy.get("mode") or offline.MODE_OPEN)
    sections: list[dict[str, Any]] = []
    advice: list[str] = []

    # ① 本地服务
    if not local.get("configured"):
        sections.append({"key": "local", "name": "本地服务", "status": "not_configured",
                         "conclusion": local["detail"]})
    elif local.get("reachable"):
        sections.append({"key": "local", "name": "本地服务", "status": "ok",
                         "conclusion": f"本地服务可达：{local['detail']}"})
    else:
        sections.append({"key": "local", "name": "本地服务", "status": "unreachable",
                         "conclusion": f"故障：本地服务不可达（{local['detail']}）—— "
                                       "请检查本机服务 / 端口是否已启动。"})
        advice.append("故障：本地服务不可达。请检查本机服务是否启动、端口是否被占用"
                      "（AUTOMIND_LOCAL_PROBES）。")

    # ② 网关 / relay
    if not gateway.get("configured"):
        sections.append({"key": "gateway", "name": "网关", "status": "not_configured",
                         "conclusion": gateway["detail"]})
    elif gateway.get("reachable"):
        sections.append({"key": "gateway", "name": "网关", "status": "ok",
                         "conclusion": f"网关可达：{gateway['target']}"})
    else:
        sections.append({"key": "gateway", "name": "网关", "status": "unreachable",
                         "conclusion": f"故障：模型网关 / relay 不可达（{gateway['detail']}）"
                                       "—— 请检查 relay、代理与网络策略。"})
        advice.append("故障：模型网关 / relay 不可达。请检查 relay 进程、代理环境变量"
                      "（HTTPS_PROXY）与网络策略；这属于故障，不是预期离线。")

    # ③ 公网
    if mode == offline.MODE_OFFLINE:
        sections.append({"key": "public", "name": "公网", "status": "expected_offline",
                         "conclusion": "预期离线：离线策略下公网不可达属于预期，不是故障；"
                                       "无需等待或重试，也不要用 wait_for_network 空等。"})
    elif public.get("reachable"):
        sections.append({"key": "public", "name": "公网", "status": "ok",
                         "conclusion": f"公网可达：{public['detail']}"})
    else:
        sections.append({"key": "public", "name": "公网", "status": "unreachable",
                         "conclusion": f"公网不可达：{public['detail']}。"
                                       "若需出网请检查网络 / DNS / 代理；"
                                       "白名单模式下请把 AUTOMIND_PROBE_HOSTS 设为允许出网的目标。"})
        if mode == offline.MODE_ALLOWLIST:
            advice.append("当前为出网白名单模式：公网探测目标可能全部不在白名单（探测本身"
                          "只是诊断，不影响放行判定）。请把 AUTOMIND_PROBE_HOSTS 设置为"
                          "白名单内、确实可达的目标。")
        else:
            advice.append("公网不可达：网络已断开或 DNS 失效。可调用 wait_for_network 等待恢复，"
                          "不要反复执行同一条命令。")

    # 策略说明（永远给出，让模型知道"为什么能被拒绝"）
    if mode == offline.MODE_OFFLINE:
        advice.insert(0, offline.offline_hint()
                         + "公网工具会当场拒绝；本地模型 / 内网服务不受影响。")
    elif mode == offline.MODE_ALLOWLIST:
        shown = "、".join(policy.get("allow") or []) or "（空）"
        advice.insert(0, f"当前为出网白名单模式：仅 {shown} 可出网，其余目标会被当场拒绝"
                         f"（配置 AUTOMIND_EGRESS_ALLOW / AUTOMIND_EGRESS_ALLOW_FILE）。")
    elif state == "online":
        advice.append("网络正常：若命令仍失败，问题多半在命令或远端服务本身。")

    # DNS 明细：部分域名解析失败但整体可达时值得单独提示
    rows = list(public.get("targets") or [])
    if any(r.get("reachable") for r in rows) and any(
            r.get("dns") is False for r in rows):
        advice.append("部分探测域名解析失败：可检查 DNS 设置（如改用 223.5.5.5 / 119.29.29.29）。")
    return sections, advice


def _state_detail(*, state: str, reason: str, policy: dict[str, Any],
                  public: dict[str, Any], gateway: dict[str, Any],
                  local: dict[str, Any]) -> str:
    mode = str(policy.get("mode") or "")
    if reason == "local_service_unreachable":
        return f"内网诊断：本地服务不可达（{local.get('detail', '')}）"
    if reason == "gateway_unreachable":
        return f"内网诊断：网关不可达（{gateway.get('detail', '')}）"
    if state == "expected_offline":
        return "预期离线：离线策略已生效，已跳过公网探测"
    if state == "online":
        return f"网络正常：{public.get('detail', '')}"
    return f"网络异常：{public.get('detail', '')}（当前策略 {mode}）"


def network_state_sync(timeout: float = 3.0, *, fresh: bool = False) -> dict[str, Any]:
    """同步计算三态网络状态（探测是阻塞的，异步调用方请用 ``network_state``）。"""
    policy = offline.policy_status()
    mode = str(policy["mode"])
    public = ({"configured": True, "skipped": True, "reachable": False,
               "domestic": False, "overseas": False, "targets": [],
               "detail": "离线策略：已跳过公网探测（不发起任何公网连接）",
               "proxy": _proxy_env()}
              if mode == offline.MODE_OFFLINE
              else _public_probe_sync(timeout, fresh=fresh))
    local = _local_probe_sync(timeout)
    gateway = _gateway_probe_sync(timeout)
    state, reason = classify_state(mode=mode, public=public,
                                   gateway=gateway, local=local)
    sections, advice = _sections_and_advice(policy=policy, state=state, public=public,
                                            gateway=gateway, local=local)
    data = {
        "state": state,
        "reason": reason,
        "online": state == "online",
        "expected_offline": state == "expected_offline",
        "degraded": state == "degraded",
        "mode": mode,
        "policy": policy,
        "local": local,
        "gateway": gateway,
        "public": public,
        # 兼容 net_check 旧字段
        "domestic": bool(public.get("domestic")),
        "overseas": bool(public.get("overseas")),
        "targets": list(public.get("targets") or []),
        "proxy": public.get("proxy") or _proxy_env(),
        "sections": sections,
        "advice": advice,
        "detail": _state_detail(state=state, reason=reason, policy=policy,
                                public=public, gateway=gateway, local=local),
        "checked_at": time.time(),
        "fresh": bool(fresh),
    }
    with _state_lock:
        _state_cache.update(at=time.monotonic(), data=data)
    return dict(data)


async def network_state(timeout: float = 3.0, *, fresh: bool = False) -> dict[str, Any]:
    """异步三态网络状态（``online`` / ``expected_offline`` / ``degraded``）。"""
    return await asyncio.to_thread(network_state_sync, timeout, fresh=fresh)


async def probe(timeout: float = 3.0, *, fresh: bool = False) -> dict[str, Any]:
    """``network_state`` 的别名（探测 API 的直观名字）。"""
    return await network_state(timeout=timeout, fresh=fresh)


def _refresh_state_worker(timeout: float) -> None:
    try:
        network_state_sync(timeout, fresh=True)
    except Exception as e:                            # pragma: no cover - 后台线程兜底
        logger.debug("net_state_refresh_failed", error=str(e))
    finally:
        with _state_lock:
            _state_refreshing["active"] = False


def network_state_cached(timeout: float = 2.0) -> dict[str, Any]:
    """健康检查专用：立刻返回**缓存**的三态快照，过期则后台刷新（绝不阻塞）。

    ``/api/health`` 是 kubernetes / 负载均衡每几秒打一次的接口；在这里做一次
    3 秒的 TCP 探测会把健康检查拖成秒级，甚至让探针超时。所以这里只读缓存，
    首次或过期时踢一个后台线程去刷新。
    """
    now = time.monotonic()
    with _state_lock:
        cached = _state_cache["data"]
        is_fresh = cached is not None and now - _state_cache["at"] < _STATE_TTL
        kick = not is_fresh and not _state_refreshing["active"]
        if kick:
            _state_refreshing["active"] = True
    if is_fresh:
        return dict(cached)
    if kick:
        threading.Thread(target=_refresh_state_worker, args=(timeout,),
                         name="automind-net-state", daemon=True).start()
    if cached is not None:
        return {**cached, "stale": True}
    return {"state": "unknown", "reason": "probe_pending",
            "online": False, "expected_offline": False, "degraded": False,
            "policy": offline.policy_status(), "sections": [],
            "advice": ["网络状态首次探测中（不阻塞健康检查，稍后重试 /api/health）。"],
            "detail": "网络状态探测中", "fresh": False}


def _probe_sync(timeout: float) -> tuple[bool, str]:
    """兼容旧接口：公网可达性 (bool, detail)。"""
    public = _public_probe_sync(timeout, fresh=True)
    return bool(public["reachable"]), str(public["detail"])


async def network_up(timeout: float = 3.0, *, fresh: bool = False) -> tuple[bool, str]:
    """公网是否可用（结果缓存 5 秒，避免一次重试里反复探测）。

    v1.11.14：离线模式下**不做任何公网探测**，立即返回
    ``(False, "expected_offline：...")`` —— 离线策略下"连不上公网"是预期，
    继续探测既没意义又会让调用方误以为在等网络恢复。
    """
    if offline.is_offline():
        return False, ("expected_offline：离线模式（AUTOMIND_NETWORK_POLICY=offline / "
                       "AUTOMIND_OFFLINE=1）已生效，已跳过公网探测")
    public = await asyncio.to_thread(_public_probe_sync, timeout, fresh=fresh)
    return bool(public["reachable"]), str(public["detail"])


async def wait_for_network(max_wait: float = 60.0, interval: float = 3.0) -> tuple[bool, float]:
    """等到网络恢复或超时；返回 (是否恢复, 实际等待秒数)。

    离线模式下不存在"等网络恢复"这回事（策略禁止出网），立即返回
    ``(False, 0.0)``，避免白等 60 秒。
    """
    if offline.is_offline():
        return False, 0.0
    start = time.monotonic()
    while True:
        ok, _ = await network_up(fresh=True)
        waited = time.monotonic() - start
        if ok or waited >= max_wait:
            return ok, waited
        await asyncio.sleep(min(interval, max(0.0, max_wait - waited)))


# ═══════════════════════════════════════════════════════════════
# 3. 哪些调用可以安全重做
# ═══════════════════════════════════════════════════════════════

#: terminal 命令里"可安全重做"的网络动作（重做不会产生重复的远端副作用）
_SAFE_CMDS = [
    re.compile(p, re.I) for p in (
        r"\bpip3?\s+(?:install|download|wheel)\b", r"\bpython[\d.]*\s+-m\s+pip\s+(?:install|download)\b",
        r"\buv\s+(?:pip\s+install|sync|add|lock)\b", r"\bpoetry\s+(?:install|add|lock|update)\b",
        r"\bconda\s+(?:install|create|update)\b", r"\bmamba\s+install\b",
        r"\b(?:npm|cnpm)\s+(?:install|i|ci|add|update)\b", r"\bpnpm\s+(?:install|i|add|update|fetch)\b",
        r"\byarn(?:\s+(?:install|add))?\s*(?:$|[;&|])", r"\byarn\s+(?:install|add)\b",
        r"\bnpx\s+", r"\bgit\s+(?:clone|fetch|pull|ls-remote|submodule\s+update)\b",
        r"\bgo\s+(?:get|mod\s+download|mod\s+tidy|install)\b", r"\bcargo\s+(?:fetch|build|install|update)\b",
        r"\bdocker\s+pull\b", r"\bapt(?:-get)?\s+(?:update|install)\b", r"\bbrew\s+(?:install|update)\b",
        r"\bwinget\s+install\b", r"\bchoco\s+install\b", r"\bgradle\w*\s+(?:build|dependencies)\b",
        r"\bmvn\s+(?:install|dependency:)\b", r"\bping\b", r"\bnslookup\b",
        r"\bwget\b", r"\bcurl\b", r"\bInvoke-WebRequest\b", r"\biwr\b",
    )
]
#: 出现这些就不重做：会改远端状态，重做 = 重复提交
_UNSAFE_CMDS = re.compile(
    r"\bgit\s+push\b|\bnpm\s+publish\b|\btwine\s+upload\b|\bdocker\s+push\b"
    r"|\bcurl\b[^|;&]*(?:\s-X\s*(?:POST|PUT|PATCH|DELETE)|\s--request\s+(?:POST|PUT|PATCH|DELETE)"
    r"|\s-d\b|\s--data\b|\s-F\b|\s--form\b|\s-T\b|\s--upload-file\b)"
    r"|\bwget\b[^|;&]*--post-(?:data|file)|-Method\s+(?:Post|Put|Patch|Delete)",
    re.I)


def is_retry_safe(tool_name: str, kwargs: dict[str, Any]) -> bool:
    """这次调用遇到网络失败时能否自动重做。"""
    if tool_name == "terminal":
        if kwargs.get("background"):
            return False                    # 后台任务由 terminal_background 轮询，不在这里重做
        cmd = str(kwargs.get("command") or "")
        if not cmd or _UNSAFE_CMDS.search(cmd):
            return False
        return any(p.search(cmd) for p in _SAFE_CMDS)
    if tool_name == "http_request":
        return str(kwargs.get("method") or "GET").upper() in ("GET", "HEAD", "OPTIONS")
    if tool_name in ("web_search", "web_fetch"):
        return True
    if tool_name == "git_tool":
        action = str(kwargs.get("action") or kwargs.get("command") or "").lower()
        return action in ("clone", "fetch", "pull", "ls-remote", "status", "log")
    return False


# ═══════════════════════════════════════════════════════════════
# 4. 镜像改写（只在网络失败后的重试里用）
# ═══════════════════════════════════════════════════════════════

PIP_MIRROR = os.environ.get("AUTOMIND_PIP_MIRROR", "https://pypi.tuna.tsinghua.edu.cn/simple")
NPM_MIRROR = os.environ.get("AUTOMIND_NPM_MIRROR", "https://registry.npmmirror.com")


def mirror_enabled() -> bool:
    return os.environ.get("AUTOMIND_NET_MIRROR", "1").strip().lower() not in ("0", "false", "off", "no")


def with_mirror(command: str) -> str | None:
    """给 pip / npm 类命令加上国内镜像；已经指定了源、或不是这类命令时返回 None。"""
    cmd = command
    if re.search(r"\bpip3?\s+(?:install|download)\b|\bpython[\d.]*\s+-m\s+pip\s+(?:install|download)\b", cmd, re.I):
        if re.search(r"(?:\s-i\s|--index-url|--extra-index-url|PIP_INDEX_URL)", cmd):
            return None
        return re.sub(r"(\bpip3?\s+(?:install|download)\b)", rf"\1 -i {PIP_MIRROR}", cmd, count=1, flags=re.I)
    if re.search(r"\b(?:npm|pnpm|yarn)\s+(?:install|i|ci|add)\b|\byarn\s*$", cmd, re.I):
        if "--registry" in cmd:
            return None
        return re.sub(r"(\b(?:npm|pnpm|yarn)\s+(?:install|i|ci|add)\b)", rf"\1 --registry={NPM_MIRROR}",
                      cmd, count=1, flags=re.I)
    return None


# ═══════════════════════════════════════════════════════════════
# 5. 分派中间件
# ═══════════════════════════════════════════════════════════════

CallNext = Callable[[dict[str, Any]], Awaitable[Any]]


class ResilientDispatch:
    """工具分派中间件：可安全重做的调用遇到网络失败 → 等网络恢复 + 退避重试。"""

    def __init__(self, retries: int = 3, backoff: tuple[float, ...] = (2.0, 5.0, 10.0),
                 max_wait: float = 60.0) -> None:
        self.retries = retries
        self.backoff = backoff
        self.max_wait = max_wait
        #: 观测：累计重试 / 救回的调用次数
        self.stats = {"retried": 0, "recovered": 0, "gave_up": 0, "mirror": 0}

    async def __call__(self, tool_name: str, kwargs: dict[str, Any], call_next: CallNext) -> Any:
        result = await call_next(kwargs)
        if getattr(result, "success", False):
            return result
        if not is_network_error(result_text(result)):
            # 文件锁类失败也不在这里处理：fs_lock_guard 插件（分派链更内层）
            # 已经按退避重试过了，这里再重试就是叠乘的重试风暴（v1.11.9 移除）。
            return result
        if offline.is_offline():
            # 离线模式下"网络类失败"是预期：不探测、不等待、不重试（否则 60s×3 空转）
            return self._annotate_offline(result, tool_name=tool_name)
        if not is_retry_safe(tool_name, kwargs):
            # 不能自动重做也要把"这是网络问题"标出来：熔断器据此走半开逻辑，
            # 模型也能看到"等网络恢复后再试"，而不是去改一个本来没错的命令
            return self._annotate(result, tries=0, notes=["该操作重做可能产生重复副作用，未自动重试"])
        notes: list[str] = []
        args = dict(kwargs)
        for attempt in range(1, self.retries + 1):
            ok, detail = await network_up()
            if not ok:
                recovered, waited = await wait_for_network(self.max_wait)
                notes.append(f"第 {attempt} 次重试前网络不可用（{detail}），"
                             + (f"等待 {waited:.0f}s 后恢复" if recovered else f"等待 {waited:.0f}s 仍未恢复"))
                if not recovered:
                    break
            await asyncio.sleep(self.backoff[min(attempt - 1, len(self.backoff) - 1)])
            if attempt >= 2 and tool_name == "terminal" and mirror_enabled():
                mirrored = with_mirror(str(args.get("command") or ""))
                if mirrored and mirrored != args.get("command"):
                    args["command"] = mirrored
                    self.stats["mirror"] += 1
                    notes.append(f"改用国内镜像重试：{mirrored[:160]}")
            self.stats["retried"] += 1
            logger.info("net_retry", tool=tool_name, attempt=attempt)
            result = await call_next(args)
            if getattr(result, "success", False):
                self.stats["recovered"] += 1
                notes.append(f"网络恢复后第 {attempt} 次重试成功")
                return self._annotate(result, tries=attempt, notes=notes, recovered=True)
            if not is_network_error(result_text(result)):
                notes.append(f"第 {attempt} 次重试已连上网络，但命令本身失败")
                return self._annotate(result, tries=attempt, notes=notes, network=False)
        self.stats["gave_up"] += 1
        return self._annotate(result, tries=self.retries, notes=notes)

    @staticmethod
    def _annotate_offline(result: Any, *, tool_name: str) -> Any:
        """离线模式：把网络类失败标成"预期"，明确告诉模型不要重试。"""
        tip = (f"当前为离线模式（AUTOMIND_NETWORK_POLICY=offline / AUTOMIND_OFFLINE=1），"
               f"工具 {tool_name} 的网络类失败属于**预期**：不会自动等待或重试。"
               "请改用离线能力（本地模型 / 内网服务）；如确需出网，请让管理员在 "
               "AUTOMIND_EGRESS_ALLOW 中放行目标并切换 allowlist，或关闭离线总闸。")
        md = dict(getattr(result, "metadata", None) or {})
        md.update({"network_error": False, "net_policy": "offline", "net_retries": 0,
                   "net_recovered": False,
                   "net_notes": ["离线模式：网络类失败属于预期，未自动等待 / 重试"]})
        try:
            result.metadata = md
        except Exception:                      # pragma: no cover - 非 pydantic 结果
            pass
        result.error = f"{getattr(result, 'error', '') or ''}\n{tip}".strip()
        return result

    @staticmethod
    def _annotate(result: Any, *, tries: int, notes: list[str], recovered: bool = False,
                  network: bool = True) -> Any:
        md = dict(getattr(result, "metadata", None) or {})
        md.update({"network_error": network and not recovered, "net_retries": tries,
                   "net_recovered": recovered, "net_notes": notes,
                   "net_policy": offline.policy_mode()})
        try:
            result.metadata = md
        except Exception:                      # pragma: no cover - 非 pydantic 结果
            pass
        if recovered:
            out = getattr(result, "output", None)
            if isinstance(out, dict):
                out = {**out, "network_note": "；".join(notes)}
                result.output = out
        elif network:
            tip = ("检测到网络中断 / 远端不可达"
                   + (f"，已自动等待并重试 {tries} 次" if tries else "")
                   + ("（" + "；".join(notes) + "）" if notes else "")
                   + "。这不是命令写错了：可调用 net_check 查看网络状况，"
                     "或用 wait_for_network 等网络恢复后再执行同一条命令；"
                     "pip / npm 可加国内镜像（-i https://pypi.tuna.tsinghua.edu.cn/simple、"
                     "--registry=https://registry.npmmirror.com）。")
            result.error = f"{getattr(result, 'error', '') or ''}\n{tip}".strip()
        return result


__all__ = [
    "PROBE_HOSTS", "PROBE_LABELS", "ResilientDispatch", "classify_state",
    "gateway_probe", "is_network_error", "is_retry_safe", "local_probes",
    "mirror_enabled", "network_state", "network_state_cached", "network_state_sync",
    "network_up", "probe", "probe_hosts", "probe_target", "result_text",
    "wait_for_network", "with_mirror",
]
