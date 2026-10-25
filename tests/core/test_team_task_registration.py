"""v1.11.18：团队共享任务工具必须真的注册进 Agent（不只是写了个类）。

为什么单独一条集成测试：``TeamTaskTool`` 再完整，只要 ``_register_default_tools``
没把它挂进注册表，模型就永远看不到它 —— 这项目历史上已经出现过多次
"实现了但没接上"（工具、技能、路由都有先例），而单测直接实例化工具是发现
不了这种断线的。这里走真实的 ``AutoMindAgent`` 构造路径，把注册行为钉死。
"""

from __future__ import annotations

import pytest

from automind.core.config import AgentConfig
from automind.core.types import PermissionTier


@pytest.fixture()
def agent(tmp_path):
    from automind.agent import AutoMindAgent

    a = AutoMindAgent(AgentConfig(project_root=str(tmp_path)))
    yield a
    # 不 close：本用例不建 MCP / memory 外部连接，close 反而会去碰网络；
    # 仅释放后台任务表，避免跨用例残留。
    try:
        from automind.tools import background

        background.reset_for_tests()
    except Exception:
        pass


def test_team_task_tool_is_registered(agent):
    assert "team_task" in agent.tool_registry.list_names()
    tool = agent.tool_registry.get("team_task")
    assert tool is not None
    assert tool.name == "team_task"
    # 写共享看板 = 敏感操作：auto 模式下 risk=40 放行，ask 模式下会请求审批，
    # 但绝不能标成 SAFE（否则"问都不问就改别人的任务"）。
    assert tool.permission_tier is PermissionTier.SENSITIVE


def test_team_task_schema_is_model_usable(agent):
    """schema 必须是模型能读懂、能照着调的形状（action 是唯一必填）。"""
    tool = agent.tool_registry.get("team_task")
    params = tool.parameters
    assert params.get("required") == ["action"]
    action = params["properties"]["action"]
    assert set(action["enum"]) >= {"list", "create", "update", "claim", "comment"}
    # 关键参数要有描述：空 description 的 schema 会让模型靠猜参数名
    for key in ("task_id", "title", "status", "text"):
        assert params["properties"][key].get("description")


def test_team_task_not_a_pro_gated_tool(agent):
    """社区版也必须注册（团队看板是社区能力，不是专业版特性）。"""
    from automind.core import edition

    assert not edition.has_feature("multi_agent"), (
        "本用例环境应是社区版；若装了专业版扩展包，此断言需要按档位调整")
    assert "team_task" in agent.tool_registry.list_names()
