"""net_resilience —— 网络中断时的自动等待与重试（v1.11.6 内置插件）。

提供三样东西（机制见 ``automind/core/netresilience.py``）：

* **分派中间件**：``terminal`` 里的依赖下载 / 代码拉取、``http_request`` 的 GET、
  ``web_search`` / ``web_fetch`` 遇到网络类失败时，先探测网络、没恢复就等（最多 60 秒），
  再按 2s / 5s / 10s 退避重试最多 3 次；pip / npm 第二次重试起改用国内镜像
  （``AUTOMIND_NET_MIRROR=0`` 关闭）。会产生远端副作用的调用（git push、POST……）不重做。
* ``net_check`` 工具：一次看清**本地服务 / 网关 / 公网**三段，并给出可执行建议。
* ``wait_for_network`` 工具：网络断了时让 Agent 明确地"等网络回来"，而不是换着花样重试。

v1.11.14（内网离线三件套）改造：
  * net_check 的结论分三态（``online`` / ``expected_offline`` / ``degraded``），
    离线策略下公网段明确写"预期离线，不要重试"；
  * 探测目标可用 ``AUTOMIND_PROBE_HOSTS`` / ``AUTOMIND_GATEWAY_PROBE`` /
    ``AUTOMIND_LOCAL_PROBES`` 覆盖，不再硬编码公网；
  * 离线模式下 ``wait_for_network`` 立即返回（不会真的等 60 秒再告诉你"还是不通"），
    并明确"离线模式下不存在等网络恢复这回事"，避免模型反复空等。

在「插件」页停用本插件即可回到原始行为（熔断器对网络类失败的半开放行仍然生效）。
"""

from __future__ import annotations

from typing import Any

from automind.core import offline
from automind.core.hooks import AgentHooks
from automind.core.netresilience import (
    ResilientDispatch,
    network_state,
    wait_for_network,
)
from automind.core.types import PermissionTier, ToolResult
from automind.tools.base import AbstractTool

_DISPATCH = ResilientDispatch()


class NetCheckTool(AbstractTool):
    name = "net_check"
    description = (
        "Diagnose network connectivity in three sections: local services (AUTOMIND_LOCAL_PROBES), "
        "the model gateway / relay (AUTOMIND_GATEWAY_PROBE) and the public internet "
        "(AUTOMIND_PROBE_HOSTS). Returns a three-state verdict: online / expected_offline "
        "(offline policy, not a fault) / degraded (gateway or local service unreachable), plus "
        "the current egress policy. Use it when a command fails with timeouts / could not "
        "resolve host, and before retrying."
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
        timeout = max(0.5, min(float(kwargs.get("timeout") or 3), 10.0))
        state = await network_state(timeout=timeout)
        policy = state["policy"]
        return ToolResult(tool_name=self.name, success=True, output={
            # 三态结论（给模型看的主字段）
            "state": state["state"],
            "online": state["online"],
            "expected_offline": state["expected_offline"],
            "degraded": state["degraded"],
            "detail": state["detail"],
            "policy": policy,
            # 三段式
            "local": state["local"],
            "gateway": state["gateway"],
            "public": state["public"],
            "sections": state["sections"],
            # 兼容旧字段（v1.11.6 起就有：domestic / overseas / targets / proxy）
            "domestic": state["domestic"],
            "overseas": state["overseas"],
            "targets": state["targets"],
            "proxy": state["proxy"],
            "advice": state["advice"],
            "retry_stats": dict(_DISPATCH.stats),
            "next": _next_hint(state),
        })


def _next_hint(state: dict[str, Any]) -> str:
    """给模型一句"下一步做什么"，避免它自己脑补重试。"""
    if state["state"] == "expected_offline":
        return ("离线模式是预期状态：不要再用 wait_for_network 等待或反复重试联网命令；"
                "改用本地 / 内网能力，并在交付说明里注明哪些步骤需要出网。")
    if state["state"] == "degraded":
        return ("检测到内网 / 公网链路故障：先按 advice 排查（本地服务 / 网关 / DNS / 代理），"
                "不要靠重复执行同一条命令来'试'。")
    return "网络正常：若命令仍失败，问题多半在命令本身或远端服务，请检查错误详情的其余部分。"


class WaitForNetworkTool(AbstractTool):
    name = "wait_for_network"
    description = (
        "Block until the network is reachable again (or the timeout elapses). Call this "
        "after a network-related failure instead of retrying the same command in a loop. "
        "In offline mode (AUTOMIND_OFFLINE=1 / AUTOMIND_NETWORK_POLICY=offline) it returns "
        "immediately with an expected_offline verdict: waiting can never help there."
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
        # 离线模式：不存在"等网络恢复"这回事，立即返回，绝不白等
        if offline.is_offline():
            return ToolResult(tool_name=self.name, success=True, output={
                "online": False,
                "state": "expected_offline",
                "skipped": True,
                "waited_s": 0.0,
                "policy": offline.policy_status(),
                "detail": offline.offline_hint(),
                "next": "离线模式下等待无意义：请使用离线 / 内网能力；如需出网请联系管理员放通白名单。",
            })
        timeout = min(600.0, max(1.0, float(kwargs.get("timeout") or 60)))
        ok, waited = await wait_for_network(timeout)
        if ok:
            return ToolResult(tool_name=self.name, success=True, output={
                "online": True, "state": "online", "waited_s": round(waited, 1),
                "policy": offline.policy_status(),
                "next": "网络已恢复，可以重新执行刚才失败的命令。"})
        state = await network_state(timeout=2.0, fresh=True)
        policy = state["policy"]
        note = ""
        if policy["mode"] == offline.MODE_ALLOWLIST:
            note = ("当前为出网白名单模式：只有 AUTOMIND_EGRESS_ALLOW 中的目标可出网，"
                    "请确认目标在白名单内，或把 AUTOMIND_PROBE_HOSTS 设为可达目标。")
        return ToolResult(tool_name=self.name, success=False, output={
            "online": False, "state": state["state"], "waited_s": round(waited, 1),
            "detail": state["detail"], "policy": policy,
            "next": note or "可再等一会儿，或先完成不需要联网的步骤。"},
            error=(f"等待 {waited:.0f} 秒网络仍未恢复（{state['detail']}）。{note}"
                   "可先完成不需要联网的步骤，并在交付说明里写明哪些步骤因网络未完成。"))


def get_tools() -> list[AbstractTool]:
    return [NetCheckTool(), WaitForNetworkTool()]


def get_middleware() -> Any:
    return _DISPATCH


def get_hooks() -> AgentHooks:
    return AgentHooks()
