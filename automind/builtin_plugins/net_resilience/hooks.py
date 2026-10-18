"""net_resilience —— 网络中断时的自动等待与重试（v1.11.6 内置插件）。

提供三样东西（机制见 ``automind/core/netresilience.py``）：

* **分派中间件**：``terminal`` 里的依赖下载 / 代码拉取、``http_request`` 的 GET、
  ``web_search`` / ``web_fetch`` 遇到网络类失败时，先探测网络、没恢复就等（最多 60 秒），
  再按 2s / 5s / 10s 退避重试最多 3 次；pip / npm 第二次重试起改用国内镜像
  （``AUTOMIND_NET_MIRROR=0`` 关闭）。会产生远端副作用的调用（git push、POST……）不重做。
* ``net_check`` 工具：一次看清 DNS、国内外站点、代理、包管理器源是否可达，并给出建议。
* ``wait_for_network`` 工具：网络断了时让 Agent 明确地"等网络回来"，而不是换着花样重试。

在「插件」页停用本插件即可回到原始行为（熔断器对网络类失败的半开放行仍然生效）。
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from typing import Any

from automind.core.hooks import AgentHooks
from automind.core.netresilience import ResilientDispatch, network_up, wait_for_network
from automind.core.types import PermissionTier, ToolResult
from automind.tools.base import AbstractTool

_DISPATCH = ResilientDispatch()

#: net_check 依次检查的目标（名称, 主机, 端口）
_TARGETS: tuple[tuple[str, str, int], ...] = (
    ("百度", "www.baidu.com", 443),
    ("清华 PyPI 镜像", "pypi.tuna.tsinghua.edu.cn", 443),
    ("npmmirror", "registry.npmmirror.com", 443),
    ("PyPI 官方", "pypi.org", 443),
    ("npm 官方", "registry.npmjs.org", 443),
    ("GitHub", "github.com", 443),
)


def _check_one(host: str, port: int, timeout: float) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        ip = socket.gethostbyname(host)
    except OSError as e:
        return {"host": host, "dns": False, "reachable": False, "error": f"DNS 失败：{e}"}
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return {"host": host, "dns": True, "ip": ip, "reachable": True,
                    "ms": round((time.perf_counter() - t0) * 1000)}
    except OSError as e:
        return {"host": host, "dns": True, "ip": ip, "reachable": False,
                "error": f"{type(e).__name__}: {e}"}


class NetCheckTool(AbstractTool):
    name = "net_check"
    description = (
        "Diagnose network connectivity: DNS, domestic/overseas sites, PyPI/npm "
        "registries, GitHub and the configured proxy. Use it when a command fails "
        "with timeouts / connection reset / could not resolve host, before retrying."
    )
    parameters = {
        "type": "object",
        "properties": {
            "timeout": {"type": "number", "description": "Per-target timeout in seconds (default 3)."},
        },
    }
    permission_tier = PermissionTier.SAFE
    risk_score = 0

    async def execute(self, **kwargs: Any) -> ToolResult:
        timeout = float(kwargs.get("timeout") or 3)
        rows = await asyncio.gather(*[
            asyncio.to_thread(_check_one, host, port, timeout) for _, host, port in _TARGETS])
        for (label, _, _), row in zip(_TARGETS, rows, strict=True):
            row["name"] = label
        env = {k.upper(): v for k, v in os.environ.items()
               if k.upper() in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")}
        ok_cn = any(r["reachable"] for r in rows[:3])
        ok_global = any(r["reachable"] for r in rows[3:])
        advice: list[str] = []
        if not ok_cn and not ok_global:
            advice.append("所有目标都不可达：网络已断开或 DNS 失效。请调用 wait_for_network 等待恢复，"
                          "不要反复执行同一条命令。")
        elif ok_cn and not ok_global:
            advice.append("国内站点可达、境外不可达：pip 请加 -i https://pypi.tuna.tsinghua.edu.cn/simple，"
                          "npm 请加 --registry=https://registry.npmmirror.com，GitHub 可改用镜像或配置代理。")
        elif not rows[0]["reachable"] and ok_global:
            advice.append("境外可达但国内站点不可达：可能处于代理环境，按代理设置访问即可。")
        else:
            advice.append("网络正常：若命令仍失败，问题多半在命令或远端服务本身。")
        if any(not r["dns"] for r in rows) and any(r["reachable"] for r in rows):
            advice.append("部分域名解析失败：可检查 DNS 设置（如改用 223.5.5.5 / 119.29.29.29）。")
        return ToolResult(tool_name=self.name, success=True, output={
            "online": ok_cn or ok_global, "domestic": ok_cn, "overseas": ok_global,
            "targets": rows, "proxy": env, "advice": advice,
            "retry_stats": dict(_DISPATCH.stats),
        })


class WaitForNetworkTool(AbstractTool):
    name = "wait_for_network"
    description = (
        "Block until the network is reachable again (or the timeout elapses). Call this "
        "after a network-related failure instead of retrying the same command in a loop."
    )
    parameters = {
        "type": "object",
        "properties": {
            "timeout": {"type": "number", "description": "Max seconds to wait (default 60, max 600)."},
        },
    }
    permission_tier = PermissionTier.SAFE
    risk_score = 0

    async def execute(self, **kwargs: Any) -> ToolResult:
        timeout = min(600.0, max(1.0, float(kwargs.get("timeout") or 60)))
        ok, waited = await wait_for_network(timeout)
        _, detail = await network_up()
        if ok:
            return ToolResult(tool_name=self.name, success=True, output={
                "online": True, "waited_s": round(waited, 1), "detail": detail,
                "next": "网络已恢复，可以重新执行刚才失败的命令。"})
        return ToolResult(tool_name=self.name, success=False, output={
            "online": False, "waited_s": round(waited, 1), "detail": detail},
            error=(f"等待 {waited:.0f} 秒网络仍未恢复（{detail}）。可再等一会儿，"
                   "或先完成不需要联网的步骤，并在交付说明里写明哪些步骤因网络未完成。"))


def get_tools() -> list[AbstractTool]:
    return [NetCheckTool(), WaitForNetworkTool()]


def get_middleware() -> Any:
    return _DISPATCH


def get_hooks() -> AgentHooks:
    return AgentHooks()
