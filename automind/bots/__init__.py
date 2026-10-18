"""IM 机器人（v1.11.6）：飞书 / 钉钉 / 企业微信 / 微信公众号 / 通用 Webhook。

在聊天里下达任务 → Agent 执行（工具调用照常走审批与熔断）→ 过程与结果发回同一个聊天窗口。
"""

from automind.bots.channels import CHANNELS, ChannelError, HttpReply, HttpReq, Incoming
from automind.bots.manager import BotManager, ProgressRelay

__all__ = ["CHANNELS", "BotManager", "ChannelError", "HttpReply", "HttpReq", "Incoming",
           "ProgressRelay"]
