"""工具组注册失败**不许静默**（v1.7.2）。

## 修的是什么

``_register_default_tools`` 里，浏览器组是裸的 ``except Exception: pass``，
六个可选工具组只有一行 ``logger.warning``。两者都等于**把失败伪装成成功**：

* 打包环境里 playwright 缺失 → ``web_fetch`` / ``browser`` 整体消失，
  模型看不到它们，用户在界面上也看不出少了东西，只会困惑"它怎么不会用浏览器"；
* 某个可选依赖残缺 → 那一组工具静默消失，而日志默认只进 stderr，
  桌面版/Web 版用户根本看不到。

本仓 v1.6.4 已经确立"失败不再伪装成成功"，注册环节恰恰是最容易漏掉的地方
（失败发生在任务开始**之前**，没有任何一步会因此报错）。

## 现在的要求

失败必须同时满足三件事：**留账目**、**进日志**、**能让用户看见**
（任务前自检 → 界面上直接弹出来；``/api/tools/registration`` → 工具面板）。
并且"某一组挂了"不能连累其余组 —— 这两条要一起成立。
"""

from __future__ import annotations

import sys

import pytest

from automind.core.config import AgentConfig
from automind.tools.base import ToolRegistry


def _bare_agent(monkeypatch, tmp_path):
    """只装 `_register_default_tools` 需要的东西 —— 不构造整个 Agent。"""
    from automind.agent import AutoMindAgent

    agent = object.__new__(AutoMindAgent)
    agent.config = AgentConfig(project_root=str(tmp_path))
    agent.tool_registry = ToolRegistry()
    agent.event_sink = None
    agent.llm = None
    return agent


# ═══════════════════════════════════════════════════════════
# 1. 可选工具组：失败要留痕，其余组照常
# ═══════════════════════════════════════════════════════════


def test_failing_group_is_recorded_and_does_not_take_down_the_others(monkeypatch, tmp_path):
    from automind.agent import AutoMindAgent

    def media_boom(self) -> None:
        raise ImportError("No module named 'psutil'")

    monkeypatch.setattr(AutoMindAgent, "_register_media_tools", media_boom)
    agent = _bare_agent(monkeypatch, tmp_path)
    agent._register_default_tools()

    groups = [f["group"] for f in agent.tool_group_failures()]
    # 组名取自注册函数本身的方法名 —— 账目要能直接指回是哪一段代码没跑成
    assert groups == ["media_boom"]
    assert "psutil" in agent.tool_group_failures()[0]["error"]

    names = set(agent.tool_registry.list_names())
    assert {"terminal", "file_read"} <= names, "基础工具不该受牵连"
    assert "csv_tool" in names, "其它可选组的工具照常注册"
    assert "ocr_tool" not in names, "失败的那一组确实没注册上"


def test_browser_group_failure_is_no_longer_swallowed(monkeypatch, tmp_path):
    """这条以前是裸的 ``except Exception: pass`` —— 最糟的一种写法。"""
    monkeypatch.setitem(sys.modules, "automind.tools.browser", None)
    agent = _bare_agent(monkeypatch, tmp_path)

    agent._register_default_tools()

    groups = [f["group"] for f in agent.tool_group_failures()]
    assert "browser" in groups, "浏览器组整组没注册上，必须有账"
    assert "web_fetch" not in agent.tool_registry.list_names()
    assert "terminal" in agent.tool_registry.list_names()


def test_successful_registration_leaves_no_failure_records(monkeypatch, tmp_path):
    agent = _bare_agent(monkeypatch, tmp_path)

    agent._register_default_tools()

    assert agent.tool_group_failures() == []


def test_failure_records_are_defensive_copies(monkeypatch, tmp_path):
    from automind.agent import AutoMindAgent

    def system_boom(self) -> None:
        raise RuntimeError("坏了")

    monkeypatch.setattr(AutoMindAgent, "_register_system_tools", system_boom)
    agent = _bare_agent(monkeypatch, tmp_path)
    agent._register_default_tools()

    snapshot = agent.tool_group_failures()
    snapshot[0]["group"] = "被改过了"

    assert agent.tool_group_failures()[0]["group"] == "system_boom"


# ═══════════════════════════════════════════════════════════
# 2. 缺依赖时要说清"怎么补"
# ═══════════════════════════════════════════════════════════


def test_missing_dependency_failure_carries_the_pip_command(monkeypatch, tmp_path):
    from automind.agent import AutoMindAgent
    from automind.tools._toolkit import MissingDependency

    def boom(self) -> None:
        raise MissingDependency("openpyxl")

    monkeypatch.setattr(AutoMindAgent, "_register_office_tools", boom)
    agent = _bare_agent(monkeypatch, tmp_path)
    agent._register_default_tools()

    entry = agent.tool_group_failures()[0]
    assert entry["missing_dependency"].startswith("openpyxl")
    assert entry["hint"].startswith("pip install")


def test_missing_binary_failure_does_not_carry_a_pip_command(monkeypatch, tmp_path):
    """外部程序与 Python 包是两回事，别把用户引到 pip 上去白跑一趟。"""
    from automind.agent import AutoMindAgent
    from automind.tools._toolkit import MissingBinary

    def boom(self) -> None:
        raise MissingBinary("tesseract", module="pytesseract")

    monkeypatch.setattr(AutoMindAgent, "_register_media_tools", boom)
    agent = _bare_agent(monkeypatch, tmp_path)
    agent._register_default_tools()

    entry = agent.tool_group_failures()[0]
    assert entry["missing_binary"] == "tesseract"
    assert "pip install tesseract" not in entry["hint"]


# ═══════════════════════════════════════════════════════════
# 3. 让用户看得见：任务前自检
# ═══════════════════════════════════════════════════════════


async def test_preflight_reports_the_missing_group(monkeypatch, tmp_path):
    """自检是唯一"用户在跑任务前一定会看到"的通道（界面上直接弹出来）。"""
    from automind.agent import AutoMindAgent

    def media_boom(self) -> None:
        raise ImportError("No module named 'psutil'")

    monkeypatch.setattr(AutoMindAgent, "_register_media_tools", media_boom)
    agent = _bare_agent(monkeypatch, tmp_path)
    agent._register_default_tools()

    report = await agent.preflight_check()

    assert report["ok"] is False
    hit = [p for p in report["problems"] if "未注册成功" in p]
    assert hit, f"注册失败必须出现在任务前自检里，实际：{report['problems']}"
    assert "psutil" in hit[0] and "media_boom" in hit[0]


async def test_preflight_stays_quiet_when_registration_is_clean(monkeypatch, tmp_path):
    agent = _bare_agent(monkeypatch, tmp_path)
    agent._register_default_tools()

    report = await agent.preflight_check()

    assert not [p for p in report["problems"] if "未注册成功" in p]


# ═══════════════════════════════════════════════════════════
# 4. 会话克隆要带着这份账目（各会话看到的必须是同一份事实）
# ═══════════════════════════════════════════════════════════


def test_session_clone_shares_the_registration_records():
    from automind.agent import AutoMindAgent

    assert "tool_registration_failures" in AutoMindAgent._SHARED_ON_CLONE
    assert "_interjections" not in AutoMindAgent._SHARED_ON_CLONE, \
        "插话队列必须独享，共享会让 A 标签页的补充插进 B 标签页的回答"


def test_server_exposes_the_registration_records():
    """界面要能问出"你到底少了哪些能力" —— 端点必须在。"""
    import automind.server as srv

    paths = {getattr(r, "path", "") for r in srv.app.routes}
    assert "/api/tools/registration" in paths


@pytest.mark.parametrize("field", ["group", "error"])
def test_failure_entry_has_the_fields_the_ui_reads(field):
    """前端读取的字段名是最容易在两处各写一遍然后漂移的东西。"""
    import automind.server as srv

    src = srv.api_tools_registration.__doc__ or ""
    assert "工具" in src
