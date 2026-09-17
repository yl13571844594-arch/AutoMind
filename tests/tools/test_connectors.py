"""连接器 SDK：发现、加载、失败记账、reload、目录覆盖（v1.7.3）。

这些用例守的是**一条产品承诺**：用户不改源码、不重启进程，把 ``.py`` 扔进
``~/.automind/connectors/`` 就能多一个工具。承诺最容易碎的地方不是加载成功
那条路径，而是所有失败路径 —— 加载发生在任务开始之前，没有任何一步会因此
报错，于是"装上了其实是坏的"可以长期无人发现。所以这里对**失败**的断言
与对成功的断言一样多。

全部用例都用 ``tmp_path`` + ``monkeypatch`` 把目录钉死在临时目录里：
真去读 ``~/.automind/connectors`` 的测试会随开发机上的文件而漂移，
在 CI 上永远绿、在装了连接器的机器上莫名其妙地红。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

from automind.tools import connectors
from automind.tools.base import AbstractTool, ToolRegistry
from automind.tools.connectors import (
    connector_dirs,
    load_connectors,
    load_failures,
    loaded_connectors,
    reload_connectors,
)

#: 一个"最正常"的连接器：模型能看见、能调用、返回 ToolResult。
GOOD = '''
from typing import Any

from automind.core.types import PermissionTier, ToolResult
from automind.tools.base import AbstractTool


class TicketStatusTool(AbstractTool):
    name = "ticket_status"
    description = "查询工单状态"
    parameters = {
        "type": "object",
        "properties": {"ticket_id": {"type": "string"}},
        "required": ["ticket_id"],
    }
    permission_tier = PermissionTier.SAFE

    async def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True,
                          output={"ticket_id": kwargs.get("ticket_id"), "state": "open"})
'''


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每个用例一套干净的目录 + 干净的账目。

    单例状态必须显式清空：上一用例留下的 hits/failures 会让"删掉文件后
    工具被摘掉"这类断言变成假阳性（工具本来就没注册上，却看着像被摘掉了）。
    """
    d = tmp_path / "connectors"
    d.mkdir()
    monkeypatch.setenv(connectors.ENV_DIR_VAR, str(d))
    connectors.reset_state()
    yield d
    connectors.reset_state()


def _write(dir_path, name: str, source: str):
    p = dir_path / name
    p.write_text(source, encoding="utf-8")
    return p


# ═══════════════════════════════════════════════════════════════
# 1. 正常加载
# ═══════════════════════════════════════════════════════════════


class TestHappyPath:
    def test_loads_a_connector_and_registers_its_tool(self, _isolated):
        _write(_isolated, "ticket.py", GOOD)
        reg = ToolRegistry()

        loaded = load_connectors(reg)

        assert loaded == ["ticket_status"]
        assert "ticket_status" in reg
        tool = reg.get("ticket_status")
        assert tool.description == "查询工单状态"
        assert tool.parameters["required"] == ["ticket_id"]

    async def test_loaded_tool_really_executes(self, _isolated):
        """注册上只是第一步 —— 得能真的跑出结果，否则只是个名字。"""
        _write(_isolated, "ticket.py", GOOD)
        reg = ToolRegistry()
        load_connectors(reg)

        result = await reg.dispatch("ticket_status", ticket_id="T-1")

        assert result.success is True
        assert result.output["state"] == "open"
        assert result.tool_name == "ticket_status"

    def test_one_file_can_contribute_several_tools(self, _isolated):
        _write(_isolated, "ticket.py", GOOD + '''
class TicketListTool(AbstractTool):
    name = "ticket_list"
    description = "列出工单"

    async def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True, output=[])
''')
        reg = ToolRegistry()

        assert load_connectors(reg) == ["ticket_list", "ticket_status"]

    def test_accounting_maps_tools_back_to_their_file(self, _isolated):
        """账目要能回答"这个工具是谁给的" —— reload 摘除全靠它。"""
        _write(_isolated, "ticket.py", GOOD)
        load_connectors(ToolRegistry())

        rows = loaded_connectors()
        assert [r["name"] for r in rows] == ["ticket_status"]
        assert rows[0]["file"] == "ticket.py"
        assert rows[0]["class"] == "TicketStatusTool"
        assert rows[0]["tier"] == "safe"

    def test_empty_dir_is_not_an_error(self, _isolated):
        """没装连接器是完全正常的状态，不该留噪音告警。"""
        reg = ToolRegistry()

        assert load_connectors(reg) == []
        assert load_failures() == []
        assert loaded_connectors() == []

    def test_missing_dir_is_not_an_error(self, tmp_path, monkeypatch):
        """目录还没建（用户没装过任何连接器）时同样不能报错。"""
        monkeypatch.setenv(connectors.ENV_DIR_VAR, str(tmp_path / "根本没有这个目录"))
        connectors.reset_state()

        assert load_connectors(ToolRegistry()) == []
        assert load_failures() == []

    def test_underscore_files_and_non_python_files_are_skipped(self, _isolated):
        """``_`` 开头是明确的"停放位"，用户靠它把草稿留在目录里而不被执行。"""
        _write(_isolated, "_draft.py", GOOD)
        _write(_isolated, "notes.txt", GOOD)
        # 子目录不递归：连接器必须平铺在目录里，"我放哪儿了"才是一句话能答的
        sub = _isolated / "sub"
        sub.mkdir()
        (sub / "deep.py").write_text(GOOD, encoding="utf-8")

        assert load_connectors(ToolRegistry()) == []

    def test_repeated_startup_load_on_the_same_registry_is_idempotent(self, _isolated, caplog):
        """同一个注册表被启动流程调第二次时，不该重新注册、也不该刷告警。

        进程里出现第二个 Agent 实例（各入口各建一个）时会走到这条路：重复注册
        会把"同名工具"告警刷满日志，把真正的告警淹掉 —— 那比不报还糟。
        """
        _write(_isolated, "ticket.py", GOOD)
        reg = ToolRegistry()

        first = load_connectors(reg)
        with caplog.at_level(logging.WARNING):
            second = load_connectors(reg)

        assert first == second == ["ticket_status"]
        assert reg.list_names() == ["ticket_status"]
        assert "connector_duplicate_tool" not in caplog.text

    def test_a_second_registry_gets_its_own_copy(self, _isolated):
        """换一个注册表就是另一次加载 —— 必须真的注册进去，不能因为"扫过"就跳过。"""
        _write(_isolated, "ticket.py", GOOD)
        first, second = ToolRegistry(), ToolRegistry()

        load_connectors(first)
        load_connectors(second)

        assert "ticket_status" in second


# ═══════════════════════════════════════════════════════════════
# 2. 坏文件：必须留下可查的账目，且不连累别人
# ═══════════════════════════════════════════════════════════════


class TestFailuresAreNeverSilent:
    """v1.6.4 / v1.7.2 的铁律：失败不许伪装成成功。

    连接器加载是这条规矩最容易破防的地方 —— 它发生在任务开始之前，
    没有一步会报错，用户只会觉得"模型说没这个工具"。
    """

    def _load_with_broken(self, dir_path, source: str, caplog, name="broken.py"):
        _write(dir_path, name, source)
        _write(dir_path, "ok.py", GOOD)              # 同一个目录里放一个好的
        reg = ToolRegistry()
        with caplog.at_level(logging.WARNING):
            load_connectors(reg)
        return reg

    def test_import_error_is_recorded_and_logged(self, _isolated, caplog):
        reg = self._load_with_broken(_isolated, "import 不存在的包\n", caplog)

        failures = load_failures()
        assert [f["file"] for f in failures] == ["broken.py"]
        assert failures[0]["error"]
        assert failures[0]["hint"], "只报错不给下一步动作，用户只能干瞪眼"
        assert "connector_load_failed" in caplog.text, "没有任何日志"
        assert failures[0]["stage"] == "import"
        # 坏的那个不连累好的 —— 这正是"逐文件 try"存在的理由
        assert len(reg) == 1

    def test_runtime_error_at_import_time_is_recorded(self, _isolated, caplog):
        reg = self._load_with_broken(
            _isolated, "raise RuntimeError('连接器自己炸了')\n", caplog)

        assert load_failures()[0]["error"].endswith("连接器自己炸了")
        assert "连接器自己炸了" in caplog.text
        assert reg.list_names() == ["ticket_status"]

    def test_syntax_error_is_recorded_with_a_location_hint(self, _isolated, caplog):
        reg = self._load_with_broken(_isolated, "def broken(:\n", caplog)

        entry = load_failures()[0]
        assert "SyntaxError" in entry["error"]
        assert "语法错误" in entry["hint"], f"该告诉用户是语法问题并给出位置：{entry}"
        assert reg.list_names() == ["ticket_status"]

    def test_constructor_failure_does_not_leave_half_a_tool(self, _isolated, caplog):
        """构造炸掉 → 一个名字都不许留在注册表里。

        留半个的后果最阴险：模型看得见、调得动，一调就报一个与用户所见
        毫无关系的错。
        """
        _write(_isolated, "boom.py", '''
from typing import Any

from automind.core.types import ToolResult
from automind.tools.base import AbstractTool


class BoomTool(AbstractTool):
    name = "boom_tool"
    description = "构造就炸"

    def __init__(self) -> None:
        raise RuntimeError("缺配置项 API_TOKEN")

    async def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True)
''')
        reg = ToolRegistry()
        with caplog.at_level(logging.WARNING):
            load_connectors(reg)

        assert "boom_tool" not in reg, "构造失败的类不能留下半个注册对象"
        entry = load_failures()[0]
        assert entry["stage"] == "init"
        assert "BoomTool" in entry["error"] and "API_TOKEN" in entry["error"]
        assert "connector_load_failed" in caplog.text

    def test_missing_name_is_recorded(self, _isolated, caplog):
        """name 为空 = 模型没法点名调用它，必须报出来而不是默默收下。"""
        reg = self._load_with_broken(_isolated, '''
from typing import Any

from automind.core.types import ToolResult
from automind.tools.base import AbstractTool


class NamelessTool(AbstractTool):
    description = "忘了写 name"

    async def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(tool_name="", success=True)
''', caplog)

        assert "name 是空的" in load_failures()[0]["error"]
        assert reg.list_names() == ["ticket_status"]

    def test_file_without_any_tool_class_is_recorded(self, _isolated, caplog):
        """文件跑得通但没提供工具 —— 与"炸了"是两种问题，都要报。"""
        reg = self._load_with_broken(_isolated, "x = 1\n", caplog)

        entry = load_failures()[0]
        assert entry["stage"] == "scan"
        assert "AbstractTool" in entry["error"]
        assert reg.list_names() == ["ticket_status"]

    def test_imported_base_class_is_not_re_registered(self, _isolated, caplog):
        """``from ... import HttpRequestTool`` 只是复用，不该把内置工具再注册一遍。"""
        _write(_isolated, "reuse.py", '''
from automind.tools.net_tools import HttpRequestTool  # noqa: F401
''')
        reg = ToolRegistry()

        with caplog.at_level(logging.WARNING):
            loaded = load_connectors(reg)

        assert loaded == []
        assert load_failures(), "文件里没有自己的工具类也要留账，不能静默返回"

    def test_failures_are_pruned_when_the_bad_file_is_removed(self, _isolated, caplog):
        """修好（或删掉）之后告警必须消失 —— 挂着一条已修复的旧账会训练用户忽略面板。"""
        bad = _write(_isolated, "broken.py", "import 不存在的包\n")
        with caplog.at_level(logging.WARNING):
            load_connectors(ToolRegistry())
        assert load_failures()

        bad.unlink()
        load_connectors(ToolRegistry())

        assert load_failures() == []

    def test_failure_entries_are_defensive_copies(self, _isolated, caplog):
        with caplog.at_level(logging.WARNING):
            self._load_with_broken(_isolated, "import 不存在的包\n", caplog)

        load_failures()[0]["file"] = "被改过了"

        assert load_failures()[0]["file"] == "broken.py"

    def test_failure_entry_carries_the_fields_the_ui_reads(self, _isolated, caplog):
        with caplog.at_level(logging.WARNING):
            self._load_with_broken(_isolated, "import 不存在的包\n", caplog)

        entry = load_failures()[0]
        for field in ("file", "path", "error", "hint"):
            assert entry.get(field), f"端点要展示 {field}"
        assert Path(entry["path"]).is_absolute()


# ═══════════════════════════════════════════════════════════════
# 3. reload：摘旧的、扫新的
# ═══════════════════════════════════════════════════════════════


class TestReload:
    def test_reload_removes_tools_whose_file_is_gone(self, _isolated):
        """会话正跑着的时候删掉一个连接器 —— 它的工具必须真的不存在了。

        留着的后果是模型能调用一个已经不存在的连接器：调用时才炸，
        而错误现场离真正的原因（文件删了）已经很远。
        """
        f = _write(_isolated, "ticket.py", GOOD)
        reg = ToolRegistry()
        load_connectors(reg)
        assert "ticket_status" in reg

        f.unlink()
        out = reload_connectors(reg)

        assert "ticket_status" not in reg
        assert out["loaded"] == []
        assert out["removed"] == ["ticket_status"]
        assert loaded_connectors() == []

    def test_reload_picks_up_a_renamed_tool(self, _isolated):
        """改名是开发连接器时的高频动作 —— 旧名字不能留在注册表里。"""
        f = _write(_isolated, "ticket.py", GOOD)
        reg = ToolRegistry()
        load_connectors(reg)

        f.write_text(GOOD.replace("ticket_status", "issue_status"), encoding="utf-8")
        out = reload_connectors(reg)

        assert "issue_status" in reg
        assert "ticket_status" not in reg, "改名之后旧名字还在，模型会调到幽灵工具"
        assert out["loaded"] == ["issue_status"]
        assert out["removed"] == ["ticket_status"]

    def test_reload_applies_edited_body(self, _isolated):
        """改了代码要生效 —— 模块名唯一就是为了这条（同名缓存会假装改了）。"""
        f = _write(_isolated, "ticket.py", GOOD)
        reg = ToolRegistry()
        load_connectors(reg)

        f.write_text(GOOD.replace("查询工单状态", "查询工单状态（新版）"), encoding="utf-8")
        reload_connectors(reg)

        assert reg.get("ticket_status").description == "查询工单状态（新版）"

    def test_reload_reports_the_broken_file_without_dropping_the_good_one(self, _isolated):
        _write(_isolated, "ok.py", GOOD)
        reg = ToolRegistry()
        load_connectors(reg)
        _write(_isolated, "broken.py", "raise ValueError('改坏了')\n")

        out = reload_connectors(reg)

        assert out["loaded"] == ["ticket_status"]
        assert out["failed"] == ["broken.py"]
        assert out["removed"] == []
        assert "ticket_status" in reg

    def test_reload_is_idempotent(self, _isolated):
        _write(_isolated, "ticket.py", GOOD)
        reg = ToolRegistry()
        load_connectors(reg)

        first = reload_connectors(reg)
        second = reload_connectors(reg)

        assert first == second == {"loaded": ["ticket_status"], "failed": [], "removed": []}
        assert reg.list_names() == ["ticket_status"], "连按两次 reload 不该长出重复工具"


# ═══════════════════════════════════════════════════════════════
# 4. 同名覆盖
# ═══════════════════════════════════════════════════════════════


class TestSameNameOverrides:
    def test_later_file_wins_and_the_override_is_warned(self, _isolated, caplog):
        """两个连接器撞名 → 只留一个，但必须留下痕迹。

        静默覆盖属于"功能在、行为却不是你以为的那个"：模型照旧看得见这个名字，
        跑出来是另一个工具的活，排查时无从下手。
        """
        _write(_isolated, "a_ticket.py", GOOD.replace("查询工单状态", "A 版本"))
        _write(_isolated, "b_ticket.py", GOOD.replace("查询工单状态", "B 版本"))
        reg = ToolRegistry()

        with caplog.at_level(logging.WARNING):
            loaded = load_connectors(reg)

        assert loaded == ["ticket_status"], "同名只该有一个"
        assert reg.get("ticket_status").description == "B 版本"
        assert "connector_duplicate_tool" in caplog.text
        assert "a_ticket.py" in caplog.text and "b_ticket.py" in caplog.text

    def test_connector_may_deliberately_replace_a_builtin(self, _isolated):
        """覆盖内置工具是**允许**的（用户可能就想换掉某个内置实现）。

        但覆盖之后 reload 必须摘得掉，不能因为"注册表里本来就有"而把
        内置工具误删或误留。
        """
        reg = ToolRegistry()
        reg.register(_BuiltinStub())
        _write(_isolated, "stub.py", GOOD.replace("ticket_status", "fake_builtin"))
        load_connectors(reg)
        assert reg.get("fake_builtin").description == "查询工单状态"

        reload_connectors(reg)

        assert reg.get("fake_builtin").description == "查询工单状态"

    def test_two_tools_in_one_file_may_not_share_a_name(self, _isolated, caplog):
        _write(_isolated, "dup.py", GOOD + GOOD.replace(
            "class TicketStatusTool", "class TicketStatusTool2"))
        reg = ToolRegistry()

        with caplog.at_level(logging.WARNING):
            loaded = load_connectors(reg)

        assert loaded == ["ticket_status"]
        assert "connector_duplicate_tool" in caplog.text


class _BuiltinStub(AbstractTool):
    """假装是内置工具，用来验证"连接器覆盖内置"这条路走得通。"""

    name = "fake_builtin"
    description = "内置版本"

    async def execute(self, **kwargs):
        from automind.core.types import ToolResult
        return ToolResult(tool_name=self.name, success=True)


# ═══════════════════════════════════════════════════════════════
# 5. 目录：默认值与环境变量覆盖
# ═══════════════════════════════════════════════════════════════


class TestDirectories:
    def test_default_dir_is_the_user_connector_dir(self, monkeypatch):
        monkeypatch.delenv(connectors.ENV_DIR_VAR, raising=False)
        dirs = connector_dirs()
        assert len(dirs) == 1
        assert dirs[0].name == "connectors" and dirs[0].parent.name == ".automind"

    def test_env_override_takes_effect(self, tmp_path, monkeypatch):
        monkeypatch.setenv(connectors.ENV_DIR_VAR, str(tmp_path / "自定义"))
        assert connector_dirs() == [tmp_path / "自定义"]

    def test_multiple_dirs_are_split_by_os_pathsep(self, tmp_path, monkeypatch):
        a, b = tmp_path / "a", tmp_path / "b"
        monkeypatch.setenv(connectors.ENV_DIR_VAR, os.pathsep.join([str(a), str(b)]))
        assert connector_dirs() == [a, b]

    def test_quotes_around_the_path_are_tolerated(self, tmp_path, monkeypatch):
        """从资源管理器复制路径会带引号，直接 Path() 会得到一个含引号的目录名。"""
        target = tmp_path / "quoted"
        monkeypatch.setenv(connectors.ENV_DIR_VAR, f'"{target}"')
        assert connector_dirs() == [target]

    def test_quoted_override_actually_loads(self, tmp_path, monkeypatch):
        d = tmp_path / "带空格 的目录"
        d.mkdir()
        _write(d, "ticket.py", GOOD)
        monkeypatch.setenv(connectors.ENV_DIR_VAR, f'"{d}"')
        connectors.reset_state()
        reg = ToolRegistry()

        assert load_connectors(reg) == ["ticket_status"]

    def test_same_file_reached_through_two_dirs_loads_once(self, tmp_path, monkeypatch):
        """同一个目录被配了两遍（常见于抄配置）不该把工具注册两次。"""
        d = tmp_path / "conn"
        d.mkdir()
        _write(d, "ticket.py", GOOD)
        monkeypatch.setenv(connectors.ENV_DIR_VAR, os.pathsep.join([str(d), str(d)]))
        connectors.reset_state()
        reg = ToolRegistry()

        assert load_connectors(reg) == ["ticket_status"]
        assert reg.list_names() == ["ticket_status"]

    def test_second_dir_can_override_the_first(self, tmp_path, monkeypatch):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        _write(a, "ticket.py", GOOD.replace("查询工单状态", "A 版本"))
        _write(b, "ticket.py", GOOD.replace("查询工单状态", "B 版本"))
        monkeypatch.setenv(connectors.ENV_DIR_VAR, os.pathsep.join([str(a), str(b)]))
        connectors.reset_state()
        reg = ToolRegistry()

        load_connectors(reg)

        assert reg.get("ticket_status").description == "B 版本"

    def test_describe_dirs_reports_what_it_scans(self, _isolated):
        info = connectors.describe_dirs()
        assert info["env_set"] is True
        assert info["dirs"][0]["path"] == str(_isolated)
        assert info["dirs"][0]["exists"] is True


# ═══════════════════════════════════════════════════════════════
# 6. 安全边界
# ═══════════════════════════════════════════════════════════════


class TestBoundaries:
    @pytest.mark.skipif(sys.platform == "win32",
                        reason="Windows 建符号链接需要管理员权限或开发者模式")
    def test_symlink_pointing_outside_is_refused(self, _isolated, tmp_path, caplog):
        """连接器目录里的链接指向别处 = "只扫指定目录"作废。"""
        outside = tmp_path / "outside.py"
        outside.write_text(GOOD, encoding="utf-8")
        (_isolated / "linked.py").symlink_to(outside)
        reg = ToolRegistry()

        with caplog.at_level(logging.WARNING):
            loaded = load_connectors(reg)

        assert loaded == []
        assert "符号链接" in load_failures()[0]["error"]

    def test_reload_never_touches_tools_it_did_not_register(self, _isolated):
        """reload 只摘连接器自己注册的工具，不能顺手把内置工具清掉。"""
        reg = ToolRegistry()
        reg.register(_BuiltinStub())
        _write(_isolated, "ticket.py", GOOD)
        load_connectors(reg)

        reload_connectors(reg)

        assert "fake_builtin" in reg, "内置工具被 reload 误删 —— 这是最严重的一类回归"
        assert reg.get("fake_builtin").description == "内置版本"
