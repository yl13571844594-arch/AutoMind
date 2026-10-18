"""v1.11.9 交付链回归测试：4 个内置插件协同 + 中间件基础设施的正确性。

覆盖三块：
1. **优先级组装**（PluginManager.assemble_middlewares）：交付链的嵌套顺序
   delivery_manifest(10) → artifact_selfcheck(20) → fs_lock_guard(30)
   → net_resilience(40)，不再由插件名的字母序决定；
2. **重放风暴修复**（ToolRegistry._execute_with_middlewares）：工具已执行后
   中间件再抛异常，绝不能重放整条链（有副作用的工具会被二次执行）；
3. **插件行为**：fs_lock_guard 的重试 / 放弃话术、artifact_selfcheck 的
   回读校验与改判失败、delivery_manifest 的记录与渲染、task_notify 的本地
   日志（webhook 分支不发真实请求）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from automind.core.plugin import PluginManager
from automind.core.types import PermissionTier, ToolSource, ToolResult

_PLUGINS = Path(__file__).resolve().parents[2] / "automind" / "builtin_plugins"


def _load_plugin(name: str) -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(f"_tc_{name}", _PLUGINS / name / "hooks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mw_identity(mw: Any) -> str:
    """把组装出来的中间件反查回插件名（函数看 __module__，实例看其类）。"""
    mod = getattr(mw, "__module__", "") or ""
    for name in ("delivery_manifest", "artifact_selfcheck", "fs_lock_guard"):
        if name in mod:
            return name
    if "netresilience" in mod or type(mw).__name__ == "ResilientDispatch":
        return "net_resilience"
    return mod or type(mw).__name__


# ═══════════════════════════════════════════════════════════
# 1. 优先级组装
# ═══════════════════════════════════════════════════════════


class TestMiddlewarePriority:
    _CHAIN = ("delivery_manifest", "artifact_selfcheck", "fs_lock_guard", "net_resilience")

    def test_delivery_chain_order_is_explicit_not_alphabetical(self):
        """链上 4 个内置中间件按 priority 嵌套，而非字母序。

        字母序是 artifact → delivery → fs → net：artifact（校验）会先于
        delivery（记录）拿到结果 —— 记录到的就不再是最终落盘状态。
        """
        pm = PluginManager()
        pm.discover()
        for n in self._CHAIN:
            assert pm.load(n) is not None, f"{n} 加载失败"
        mws = pm.assemble_middlewares()
        assert len(mws) == 4, "交付链四件套都必须提供中间件"
        names = [_mw_identity(m) for m in mws]
        assert names == list(self._CHAIN), \
            f"中间件嵌套顺序错误：{names}（应为 {list(self._CHAIN)}）"

    def test_priority_from_manifest(self):
        pm = PluginManager()
        metas = {m.name: m for m in pm.discover()}
        assert metas["delivery_manifest"].priority < metas["artifact_selfcheck"].priority
        assert metas["artifact_selfcheck"].priority < metas["fs_lock_guard"].priority
        assert metas["fs_lock_guard"].priority < metas["net_resilience"].priority

    @pytest.mark.parametrize("bad", ["high", "1e9", "true", "null", "{}", "9999"])
    def test_invalid_priority_falls_back(self, tmp_path, bad):
        """priority 非法（非整数 / 布尔 / 越界 / 空）回退 100，不让坏清单炸掉扫描。"""
        d = tmp_path / "p"
        d.mkdir()
        (d / "plugin.json").write_text(
            json.dumps({"name": "p", "version": "1.0.0", "priority": bad}),
            encoding="utf-8")
        pm = PluginManager(plugin_dirs=[tmp_path])
        metas = pm.discover()
        assert len(metas) == 1 and metas[0].priority == 100, \
            f"priority={bad!r} 应回退 100（实际 {metas[0].priority}）"

    def test_hooks_merged_by_priority(self, tmp_path, monkeypatch):
        """生命周期钩子按 priority 合并成一份可调用的 AgentHooks。

        顺带做隔离：task_notify 的 after_run 会写 ``~/.automind/notifications.log``
        —— 把 USERPROFILE 指到 tmp_path，避免测试污染真实用户数据。
        """
        import os
        monkeypatch.setenv("USERPROFILE" if os.name == "nt" else "HOME", str(tmp_path))
        monkeypatch.delenv("AUTOMIND_NOTIFY_WEBHOOK", raising=False)

        pm = PluginManager()
        pm.discover()
        pm.load("delivery_manifest")
        pm.load("task_notify")
        merged = pm.assemble_hooks()
        # 两个插件都挂了 after_run → 合并结果必须非空且可直接 await
        assert merged.after_run is not None
        asyncio.run(merged.after_run(SimpleNamespace(success=True, duration_ms=1)))
        # task_notify 的钩子确实被执行了（通知日志落在了隔离目录）
        assert (tmp_path / ".automind" / "notifications.log").is_file()


# ═══════════════════════════════════════════════════════════
# 2. 重放风暴修复
# ═══════════════════════════════════════════════════════════


class _EchoTool:
    """记录执行次数的最小工具替身（schema 为空 → dispatch 的三道前置修正全放行）。"""

    name = "echo_tool"
    description = "echo"
    parameters: dict = {}
    permission_tier = PermissionTier.SAFE
    risk_score = 0
    source = ToolSource.BUILTIN

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, **kwargs: Any) -> ToolResult:
        self.calls += 1
        return ToolResult(tool_name=self.name, success=True, output={"echo": kwargs})


class TestNoReplayStorm:
    def _registry(self, tool):
        from automind.tools.base import ToolRegistry

        reg = ToolRegistry()
        reg.register(tool)
        return reg

    def test_tool_exception_not_replayed_through_middleware_frames(self):
        """工具抛异常 → 异常穿出中间件帧 → **不能**每层都重跑一遍工具。"""
        calls = {"n": 0}

        class Boom(_EchoTool):
            name = "boom_tool"

            async def execute(self, **kwargs):
                calls["n"] += 1
                raise KeyError("path")

        reg = self._registry(Boom())

        async def passthrough(name, kw, nxt):
            return await nxt(kw)

        # 三层不接异常的中间件：旧实现会让工具被执行 3 次
        reg.set_middlewares([passthrough, passthrough, passthrough])
        result = asyncio.run(reg.dispatch("boom_tool", path="x"))
        assert calls["n"] == 1, f"工具被执行了 {calls['n']} 次 —— 中间件帧在重放"
        assert not result.success
        assert "KeyError" in result.error or "缺少必需参数" in result.error
        assert "不要重做" in result.error, "必须提醒模型先核对再决定是否重做"

    def test_post_execution_middleware_failure_not_replayed(self):
        """中间件在工具成功**之后**做后处理时抛异常 → 不得重放工具。"""
        tool = _EchoTool()
        reg = self._registry(tool)

        async def flaky_post(name, kw, nxt):
            result = await nxt(kw)                 # 工具已执行
            if result.success:
                raise RuntimeError("后处理炸了（比如记账/校验代码有 bug）")
            return result

        async def outer_spy(name, kw, nxt):
            return await nxt(kw)                   # 不接异常：让 RuntimeError 穿上来

        reg.set_middlewares([outer_spy, flaky_post])
        result = asyncio.run(reg.dispatch("echo_tool", path="x"))
        assert tool.calls == 1, f"工具被执行了 {tool.calls} 次 —— 后处理失败触发了重放"
        assert not result.success
        assert "已执行" in result.error and "不要重做" in result.error
        assert result.metadata.get("tool_executed") is True

    def test_pre_execution_middleware_failure_skips_it(self):
        """中间件在工具执行**之前**抛异常 → 跳过它照常执行（旧行为保留）。"""
        tool = _EchoTool()
        reg = self._registry(tool)

        async def broken_pre(name, kw, nxt):
            raise RuntimeError("前置处理炸了")

        reg.set_middlewares([broken_pre])
        result = asyncio.run(reg.dispatch("echo_tool", path="x"))
        assert result.success and tool.calls == 1

    def test_cancellation_still_propagates(self):
        """取消必须继续上浮（不能被"合成失败结果"吞掉）。"""
        tool = _EchoTool()
        reg = self._registry(tool)

        async def canceller(name, kw, nxt):
            await nxt(kw)
            raise asyncio.CancelledError()

        reg.set_middlewares([canceller])

        async def run():
            await reg.dispatch("echo_tool", path="x")

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run())


# ═══════════════════════════════════════════════════════════
# 3. 插件行为
# ═══════════════════════════════════════════════════════════


class TestFsLockGuard:
    def _mw(self):
        return _load_plugin("fs_lock_guard").get_middleware()

    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        async def no_sleep(_s):
            return None
        monkeypatch.setattr("asyncio.sleep", no_sleep)

    def test_retries_lock_error_then_succeeds(self):
        n = {"i": 0}

        async def call_next(kw):
            n["i"] += 1
            if n["i"] < 3:
                return ToolResult(tool_name="file_write", success=False,
                                  error="[WinError 32] 另一个程序正在使用此文件")
            return ToolResult(tool_name="file_write", success=True, output={"path": "a.xlsx"})
        res = asyncio.run(self._mw()("file_write", {"path": "a.xlsx"}, call_next))
        assert res.success and n["i"] == 3

    def test_gives_up_with_actionable_advice(self):
        async def call_next(kw):
            return ToolResult(tool_name="excel_tool", success=False,
                              error="[WinError 32] being used by another process")
        res = asyncio.run(self._mw()("excel_tool", {"path": "a.xlsx"}, call_next))
        assert not res.success
        assert "关闭打开该文件的程序" in res.error
        assert res.metadata.get("fs_lock_guard") is True

    def test_non_lock_failure_passes_through(self):
        async def call_next(kw):
            return ToolResult(tool_name="file_write", success=False,
                              error="路径越界，已拒绝")
        res = asyncio.run(self._mw()("file_write", {"path": "../../etc"}, call_next))
        assert not res.success and "退避重试" not in res.error

    def test_tool_exception_lock_like_is_retried(self):
        n = {"i": 0}

        async def call_next(kw):
            n["i"] += 1
            if n["i"] < 2:
                raise PermissionError(5, "拒绝访问", "a.xlsx")
            return ToolResult(tool_name="file_write", success=True, output={})
        res = asyncio.run(self._mw()("file_write", {"path": "a.xlsx"}, call_next))
        assert res.success and n["i"] == 2

    def test_file_read_now_covered(self):
        """v1.11.9：file_read 纳入重试（core 移除后由此插件兜底）。"""
        n = {"i": 0}

        async def call_next(kw):
            n["i"] += 1
            if n["i"] < 2:
                return ToolResult(tool_name="file_read", success=False,
                                  error="[WinError 32] being used by another process")
            return ToolResult(tool_name="file_read", success=True, output={})
        res = asyncio.run(self._mw()("file_read", {"path": "a.xlsx"}, call_next))
        assert res.success and n["i"] == 2


class TestArtifactSelfcheck:
    def _mod(self):
        return _load_plugin("artifact_selfcheck")

    def test_missing_file_flips_success_to_failure(self, tmp_path):
        """工具自报成功，但文件根本不在 → 改判失败并给出修法。"""
        async def call_next(kw):
            return ToolResult(tool_name="file_write", success=True,
                              output={"path": str(tmp_path / "missing.txt")})
        res = asyncio.run(self._mod().get_middleware()(
            "file_write", {"path": str(tmp_path / "missing.txt")}, call_next))
        assert not res.success
        assert "文件不存在" in res.error and "自检未通过" in res.error
        assert res.metadata.get("artifact_selfcheck") is True

    def test_empty_file_flips_success(self, tmp_path):
        p = tmp_path / "empty.txt"
        p.write_bytes(b"")
        async def call_next(kw):
            return ToolResult(tool_name="file_write", success=True, output={"path": str(p)})
        res = asyncio.run(self._mod().get_middleware()("file_write", {"path": str(p)}, call_next))
        assert not res.success and "0 字节" in res.error

    def test_good_file_passes(self, tmp_path):
        p = tmp_path / "ok.txt"
        p.write_text("hello", encoding="utf-8")
        async def call_next(kw):
            return ToolResult(tool_name="file_write", success=True, output={"path": str(p)})
        res = asyncio.run(self._mod().get_middleware()("file_write", {"path": str(p)}, call_next))
        assert res.success

    def test_corrupt_office_file_detected(self, tmp_path):
        """xlsx 后缀但内容是纯文本 → 打不开（openpyxl 存在时）。"""
        import importlib.util as iu
        if iu.find_spec("openpyxl") is None:
            pytest.skip("未安装 openpyxl")
        p = tmp_path / "bad.xlsx"
        p.write_text("not a real xlsx", encoding="utf-8")
        async def call_next(kw):
            return ToolResult(tool_name="excel_tool", success=True, output={"path": str(p)})
        res = asyncio.run(self._mod().get_middleware()("excel_tool", {"path": str(p)}, call_next))
        assert not res.success and "文件打不开" in res.error

    def test_missing_lib_skips_not_fails(self, tmp_path, monkeypatch):
        """缺解析库时跳过该项检查（不能把好文件误判成坏文件）。

        注意 _OPENERS 在模块加载时就绑定了函数对象，所以要 patch 字典条目，
        patch 模块属性不影响它。
        """
        mod = self._mod()

        def no_openpyxl(p):
            raise mod._MissingLib("openpyxl")
        monkeypatch.setitem(mod._OPENERS, ".xlsx", no_openpyxl)

        p = tmp_path / "data.xlsx"
        p.write_bytes(b"PK\x03\x04whatever")
        async def call_next(kw):
            return ToolResult(tool_name="excel_tool", success=True, output={"path": str(p)})
        res = asyncio.run(mod.get_middleware()("excel_tool", {"path": str(p)}, call_next))
        assert res.success, "缺库必须跳过校验，而不是误判失败"

    def test_non_write_tool_untouched(self):
        async def call_next(kw):
            return ToolResult(tool_name="web_search", success=True, output={"hits": 3})
        res = asyncio.run(self._mod().get_middleware()("web_search", {"query": "x"}, call_next))
        assert res.success


class TestDeliveryManifest:
    def test_records_and_renders(self, tmp_path):
        mod = _load_plugin("delivery_manifest")
        mod.reset_for_tests()

        async def call_next(kw):
            p = tmp_path / kw["path"]
            p.write_text("内容" * 10, encoding="utf-8")
            return ToolResult(tool_name="file_write", success=True, output={"path": kw["path"]})

        result = asyncio.run(mod.get_middleware()("file_write", {"path": "report.md"}, call_next))
        assert result.success
        assert mod._records, "成功写入必须被记录"

        rendered = mod._render(mod._records)
        assert "📦 交付清单" in rendered
        assert "report.md" in rendered
        assert "file_write" in rendered

    def test_only_write_tools_recorded(self):
        mod = _load_plugin("delivery_manifest")
        mod.reset_for_tests()

        async def call_next(kw):
            return ToolResult(tool_name="web_search", success=True, output={"hits": 1})
        asyncio.run(mod.get_middleware()("web_search", {"query": "x"}, call_next))
        assert not mod._records, "读类工具不该进交付清单"

    def test_failed_writes_not_recorded(self, tmp_path):
        """失败的写不进清单 —— 交付清单只记"实际落盘成功"的。"""
        mod = _load_plugin("delivery_manifest")
        mod.reset_for_tests()

        async def call_next(kw):
            return ToolResult(tool_name="file_write", success=False, error="磁盘满")
        asyncio.run(mod.get_middleware()("file_write", {"path": "x.md"}, call_next))
        assert not mod._records

    def test_before_run_clears_records(self):
        mod = _load_plugin("delivery_manifest")
        mod.reset_for_tests()
        mod._records.append({"tool": "file_write", "path": "x", "size": 1, "hash": "", "ts": 0})
        hooks = mod.get_hooks()
        asyncio.run(hooks.before_run("新任务"))
        assert not mod._records, "任务开始必须清零（不能把上个会话的写入算进来）"


class TestTaskNotify:
    def _mod(self):
        return _load_plugin("task_notify")

    def test_local_log_written(self, tmp_path, monkeypatch):
        mod = self._mod()
        log = tmp_path / "notifications.log"
        monkeypatch.setattr(mod, "_LOG_PATH", log)
        monkeypatch.delenv("AUTOMIND_NOTIFY_WEBHOOK", raising=False)

        hooks = mod.get_hooks()
        asyncio.run(hooks.after_run(SimpleNamespace(
            success=True, duration_ms=1500,
            plan=SimpleNamespace(task_description="写周报"))))
        text = log.read_text(encoding="utf-8")
        assert "成功" in text and "1.5 秒" in text and "写周报" in text

    def test_webhook_not_called_when_unset(self, tmp_path, monkeypatch):
        mod = self._mod()
        monkeypatch.setattr(mod, "_LOG_PATH", tmp_path / "n.log")
        monkeypatch.delenv("AUTOMIND_NOTIFY_WEBHOOK", raising=False)

        called = {"n": 0}

        def fake_urlopen(*a, **k):
            called["n"] += 1
            raise AssertionError("没配 webhook 不该发请求")
        monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)

        asyncio.run(mod.get_hooks().after_run(SimpleNamespace(success=False, duration_ms=10)))
        assert called["n"] == 0
