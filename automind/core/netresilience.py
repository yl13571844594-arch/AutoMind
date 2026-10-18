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
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Any

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


def is_network_error(text: Any) -> bool:
    """报错文本是否属于"网络暂时不通"一类（可等待 / 重试解决）。"""
    s = str(text or "")
    if not s:
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

#: 探测目标：国内外各一组，任一能连上即视为网络可用（不发业务请求，只做 TCP 连接）
PROBE_HOSTS: tuple[tuple[str, int], ...] = (
    ("www.baidu.com", 443), ("mirrors.tuna.tsinghua.edu.cn", 443),
    ("pypi.org", 443), ("github.com", 443), ("1.1.1.1", 443), ("223.5.5.5", 53),
)

_probe_cache: dict[str, Any] = {"at": 0.0, "ok": None, "detail": ""}
_PROBE_TTL = 5.0


def _probe_sync(timeout: float) -> tuple[bool, str]:
    # 小写变量名是 Unix 惯例（curl / pip 都认），Windows 上环境变量本就不分大小写
    env = {k.upper(): v for k, v in os.environ.items() if k.upper() in ("HTTPS_PROXY", "ALL_PROXY")}
    proxy = env.get("HTTPS_PROXY") or env.get("ALL_PROXY") or ""
    targets = list(PROBE_HOSTS)
    if proxy:
        m = re.match(r"^\w+://(?:[^@/]+@)?([^:/]+)(?::(\d+))?", proxy)
        if m:
            # 配了代理时先看代理本身通不通（直连外网本来就可能不通）
            targets.insert(0, (m.group(1), int(m.group(2) or 80)))
    errors: list[str] = []
    for host, port in targets:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True, f"{host}:{port} 可达"
        except OSError as e:
            errors.append(f"{host}:{port} {type(e).__name__}")
    return False, "；".join(errors[:4])


async def network_up(timeout: float = 3.0, *, fresh: bool = False) -> tuple[bool, str]:
    """网络是否可用（结果缓存 5 秒，避免一次重试里反复探测）。"""
    now = time.monotonic()
    if not fresh and _probe_cache["ok"] is not None and now - _probe_cache["at"] < _PROBE_TTL:
        return bool(_probe_cache["ok"]), str(_probe_cache["detail"])
    ok, detail = await asyncio.to_thread(_probe_sync, timeout)
    _probe_cache.update(at=time.monotonic(), ok=ok, detail=detail)
    return ok, detail


async def wait_for_network(max_wait: float = 60.0, interval: float = 3.0) -> tuple[bool, float]:
    """等到网络恢复或超时；返回 (是否恢复, 实际等待秒数)。"""
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
    def _annotate(result: Any, *, tries: int, notes: list[str], recovered: bool = False,
                  network: bool = True) -> Any:
        md = dict(getattr(result, "metadata", None) or {})
        md.update({"network_error": network and not recovered, "net_retries": tries,
                   "net_recovered": recovered, "net_notes": notes})
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
    "PROBE_HOSTS", "ResilientDispatch", "is_network_error", "is_retry_safe",
    "mirror_enabled", "network_up", "result_text", "wait_for_network", "with_mirror",
]
