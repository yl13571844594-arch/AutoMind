"""安全加固测试 — 沙箱修复、路径穿越防护、版本统一（§14.11 / §14.1）。"""

import tempfile
from pathlib import Path

import pytest

from automind.tools.file_editor import (
    FileEditTool,
    FileReadTool,
    FileWriteTool,
    _RootGuard,
)
from automind.tools.sandbox import PythonSandboxTool


class TestPythonSandbox:
    """PythonSandboxTool 在被 import 为模块的语境下必须可用且安全。"""

    @pytest.mark.asyncio
    async def test_basic_execution(self):
        # 回归：修复前 `getattr(__builtins__, ...)` 在模块语境会抛 AttributeError
        r = await PythonSandboxTool().execute(code="print(2 + 2)")
        assert r.success, r.error
        assert r.output["stdout"].strip() == "4"

    @pytest.mark.asyncio
    async def test_whitelisted_import_statement(self):
        r = await PythonSandboxTool().execute(code="import math\nprint(math.sqrt(16))")
        assert r.success, r.error
        assert r.output["stdout"].strip() == "4.0"

    @pytest.mark.asyncio
    async def test_dangerous_import_blocked(self):
        r = await PythonSandboxTool().execute(code="import os\nprint(os.getcwd())")
        assert not r.success
        assert "not allowed" in (r.error or "")

    @pytest.mark.asyncio
    async def test_dunder_import_blocked(self):
        r = await PythonSandboxTool().execute(code="__import__('subprocess')")
        assert not r.success
        assert "not allowed" in (r.error or "")


class TestRootGuard:
    """_RootGuard 路径穿越防护语义。"""

    def test_none_root_is_unrestricted(self):
        g = _RootGuard(None)
        assert g.resolve("/any/abs/path") == Path("/any/abs/path")

    def test_relative_resolves_within_root(self):
        root = tempfile.mkdtemp()
        g = _RootGuard(root)
        assert g.resolve("sub/a.txt") == Path(root).resolve() / "sub" / "a.txt"

    def test_traversal_rejected(self):
        root = tempfile.mkdtemp()
        g = _RootGuard(root)
        with pytest.raises(PermissionError):
            g.resolve("../../etc/passwd")

    def test_prefix_collision_rejected(self):
        # /srv/app 不应误判 /srv/app-evil 为子路径
        root = Path(tempfile.mkdtemp()) / "app"
        root.mkdir()
        sibling = root.parent / "app-evil"
        g = _RootGuard(root)
        with pytest.raises(PermissionError):
            g.resolve(str(sibling / "x.txt"))


class TestFileToolGuard:
    """文件工具在设置 project_root 后强制限定范围。"""

    @pytest.mark.asyncio
    async def test_write_read_inside_root(self):
        root = tempfile.mkdtemp()
        w = FileWriteTool(project_root=root)
        wr = await w.execute(path="a.txt", content="hi")
        assert wr.success, wr.error
        r = FileReadTool(project_root=root)
        rr = await r.execute(path="a.txt")
        assert rr.success and rr.output["content"] == "hi"

    @pytest.mark.asyncio
    async def test_read_traversal_blocked(self):
        root = tempfile.mkdtemp()
        r = FileReadTool(project_root=root)
        out = await r.execute(path="../../../etc/passwd")
        assert not out.success
        assert "越界" in (out.error or "")

    @pytest.mark.asyncio
    async def test_edit_traversal_blocked(self):
        root = tempfile.mkdtemp()
        e = FileEditTool(project_root=root)
        out = await e.execute(path="../x.txt", old_string="a", new_string="b")
        assert not out.success
        assert "越界" in (out.error or "")

    @pytest.mark.asyncio
    async def test_unguarded_backward_compatible(self):
        # 默认构造（project_root=None）行为与升级前一致：可读任意绝对路径
        root = tempfile.mkdtemp()
        target = Path(root) / "out.txt"
        target.write_text("data", encoding="utf-8")
        r = FileReadTool()
        out = await r.execute(path=str(target))
        assert out.success and out.output["content"] == "data"


class TestVersionUnified:
    def test_single_source_of_truth(self):
        import automind
        import automind.server as srv

        # 单一数据源：server 版本必须来自包版本（不锁定具体号，避免每次升版改测试）
        import re
        assert re.fullmatch(r"\d+\.\d+\.\d+", automind.__version__)
        assert srv.app.version == automind.__version__
