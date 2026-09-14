"""v1.6.4 治理与证据测试 —— 轨迹落盘 / 并发写冲突 / 输出体积 / 终端超时 / 审批让槽。

这些能力有一个共同点：**它们治的都是"看不见的损失"** —— 覆盖掉别人的成果、
把上下文烧光、超时后状态全丢、审批挂起占着并发槽。因此测试的重点不是
"功能能跑"，而是"损失能被检出、被记录、被明确告知"。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

# ═══════════════════════════════════════════════════════════
# 0. 后台子进程用例的开关
# ═══════════════════════════════════════════════════════════
#
# 真正 spawn 子进程的用例在 Windows 上会让**整个测试进程**退出时挂住：
# shell 启动的命令会派生子进程并继承 stdout/stderr 管道句柄，父进程被杀后
# 句柄仍被孙子进程持有，asyncio 的 proactor 事件循环在收尾时等不到管道关闭。
# 这不是被测代码的缺陷（kill 路径已做有界等待，见 tools/background.py），
# 而是"在测试进程里养一个活着的子进程"这一做法本身的代价。
#
# 因此默认跳过这几条，需要时显式打开：
#     $env:AUTOMIND_RUN_BG_TESTS="1"; python -m pytest tests/core/test_v164_governance.py


def _require_bg_subprocess() -> None:
    if os.environ.get("AUTOMIND_RUN_BG_TESTS", "").strip().lower() not in (
            "1", "true", "yes", "on"):
        pytest.skip("后台子进程用例默认跳过（设 AUTOMIND_RUN_BG_TESTS=1 运行）")

# ═══════════════════════════════════════════════════════════
# 1. 执行证据落盘（session trace）
# ═══════════════════════════════════════════════════════════


class TestSessionTrace:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path):
        from automind.core import observability as obs
        obs.reset()
        obs._trace_reset_for_tests(root=tmp_path / "traces", enabled=True)
        yield
        obs.reset()
        obs._trace_reset_for_tests(root=tmp_path / "traces2", enabled=True)

    def test_events_are_written_as_jsonl(self, tmp_path):
        from automind.core import observability as obs

        obs.record("s1", {"type": "task_start", "interaction": "coding"})
        obs.record("s1", {"type": "plan_step_start", "goal_id": "g1",
                          "description": "写文件"})
        obs.record("s1", {"type": "step_action", "tool": "file_write",
                          "args": {"path": "a.py"}, "success": True,
                          "output": "ok"})
        obs.record("s1", {"type": "task_complete", "success": True, "steps": 1})

        runs = obs.trace_runs("s1")
        assert len(runs) == 1
        path = tmp_path / "traces" / "s1" / f"{runs[0]['run_id']}.jsonl"
        assert path.is_file()
        lines = [json.loads(x) for x in
                 path.read_text(encoding="utf-8").splitlines() if x.strip()]
        types = [x["type"] for x in lines]
        assert types[0] == "task_start"
        assert "step_action" in types
        assert types[-1] == "trace_end"          # 收尾记录
        assert all("ts" in x and "session_id" in x for x in lines)

    def test_survives_process_memory_reset(self, tmp_path):
        """实时 DAG 可以清空，磁盘证据不能跟着消失 —— 这正是本轮要修的缺口。"""
        from automind.core import observability as obs

        obs.record("s2", {"type": "task_start"})
        obs.record("s2", {"type": "task_error", "error": "boom"})
        assert obs.snapshot("s2") is not None
        obs.reset("s2")                                   # 内存图清掉
        assert obs.snapshot("s2") is None
        # 但轨迹仍在磁盘上，且可读回
        runs = obs.trace_runs("s2")
        assert runs, "轨迹文件不应随内存图一起消失"
        events = obs._trace.get_recorder().tail("s2", runs[0]["run_id"])
        assert events[-1]["type"] == "trace_end"
        assert any(e["type"] == "task_error" for e in events)

    def test_secrets_are_redacted(self, tmp_path):
        from automind.core import observability as obs

        obs.record("s3", {"type": "task_start"})
        obs.record("s3", {"type": "step_action", "tool": "http_request",
                          "args": {"api_key": "sk-super-secret-value",
                                   "url": "https://x"},
                          "output": "ok"})
        obs.record("s3", {"type": "task_complete", "success": True})
        run = obs.trace_runs("s3")[0]["run_id"]
        raw = (tmp_path / "traces" / "s3" / f"{run}.jsonl").read_text(encoding="utf-8")
        assert "sk-super-secret-value" not in raw
        assert "***" in raw

    def test_trace_disabled_writes_nothing(self, tmp_path):
        from automind.core import observability as obs
        obs._trace_reset_for_tests(root=tmp_path / "off", enabled=False)
        obs.record("s4", {"type": "task_start"})
        obs.record("s4", {"type": "task_complete"})
        assert obs.trace_runs("s4") == []

    def test_run_rotation_keeps_newest(self, tmp_path):
        from automind.core.trace import TraceRecorder

        rec = TraceRecorder(root=tmp_path / "rot", enabled=True, max_runs=2)
        for i in range(5):
            rec.record("s", f"run{i}", {"type": "task_start", "i": i})
            rec.prune("s")
            import time
            time.sleep(0.01)
        runs = rec.list_runs("s")
        assert len(runs) == 2
        assert {r["run_id"] for r in runs} == {"run3", "run4"}

    def test_per_run_byte_cap_marks_truncation(self, tmp_path):
        from automind.core.trace import TraceRecorder

        rec = TraceRecorder(root=tmp_path / "cap", enabled=True, max_run_bytes=400)
        for i in range(20):
            rec.record("s", "r1", {"type": "step_action", "output": "x" * 200})
        events = rec.tail("s", "r1", limit=100)
        assert any(e["type"] == "trace_truncated" for e in events)
        assert rec.dropped("s", "r1") > 0

    def test_write_failure_never_breaks_task(self, tmp_path):
        """轨迹写不进去（目录被占）也不许影响任务 —— 设施故障不得升级为任务失败。"""
        from automind.core.trace import TraceRecorder

        blocker = tmp_path / "blocked"
        blocker.write_text("I am a file, not a dir", encoding="utf-8")
        rec = TraceRecorder(root=blocker, enabled=True)
        assert rec.record("s", "r", {"type": "task_start"}) == ""
        assert rec.write_errors >= 1

    def test_trace_api_endpoints(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from automind import server as srv
        from automind.core import observability as obs

        monkeypatch.setattr(srv._store, "config_file", tmp_path / "cfg.json",
                            raising=False)
        srv._AUTH_TOKEN = ""
        obs.reset()
        obs._trace_reset_for_tests(root=tmp_path / "apitrace", enabled=True)
        obs.record("sess-api", {"type": "task_start"})
        obs.record("sess-api", {"type": "step_action", "tool": "terminal",
                                "success": True})
        obs.record("sess-api", {"type": "task_complete", "success": True})

        c = TestClient(srv.app)
        r = c.get("/api/observe/traces?session_id=sess-api").json()
        assert r["runs"] and r["stats"]["runs"] == 1
        run_id = r["runs"][0]["run_id"]
        detail = c.get(f"/api/observe/traces/sess-api/{run_id}").json()
        assert detail["count"] >= 3
        only = c.get(f"/api/observe/traces/sess-api/{run_id}?types=step_action").json()
        assert only["count"] == 1 and only["events"][0]["tool"] == "terminal"
        dl = c.get(f"/api/observe/traces/sess-api/{run_id}/download")
        assert dl.status_code == 200
        assert b"task_start" in dl.content
        missing = c.get("/api/observe/traces/sess-api/nope/download")
        assert missing.status_code == 404


# ═══════════════════════════════════════════════════════════
# 2. 并发写冲突（目录级）
# ═══════════════════════════════════════════════════════════


class TestWriteGuard:
    @pytest.fixture(autouse=True)
    def _clean(self):
        from automind.tools import write_guard
        write_guard.reset_for_tests()
        yield
        write_guard.reset_for_tests()

    def test_foreign_write_detected_after_warn(self, tmp_path):
        from automind.tools.write_guard import WriteGuard

        f = tmp_path / "a.txt"
        f.write_text("v1", encoding="utf-8")
        g1 = WriteGuard(session="s1", policy="warn")
        # s2 在 s1 未读过的情况下写了它
        g2 = WriteGuard(session="s2", policy="warn")
        g2.note_write(str(f))
        conflict = g1.check(str(f))
        assert conflict["ok"] is True             # warn 策略：仍允许写
        assert conflict["foreign"] == "s2"
        assert "另一个会话" in conflict["reason"]

    def test_read_after_foreign_write_is_not_a_conflict(self, tmp_path):
        from automind.tools.write_guard import WriteGuard

        f = tmp_path / "a.txt"
        f.write_text("v1", encoding="utf-8")
        g2 = WriteGuard(session="s2")
        g2.note_write(str(f))
        g1 = WriteGuard(session="s1")
        g1.note_read(str(f))                      # s1 读到了 s2 的版本
        assert g1.check(str(f))["ok"] is True
        assert g1.check(str(f))["foreign"] == ""

    def test_block_policy_refuses(self, tmp_path):
        from automind.tools.write_guard import WriteGuard

        f = tmp_path / "a.txt"
        f.write_text("v1", encoding="utf-8")
        WriteGuard(session="s2").note_write(str(f))
        c = WriteGuard(session="s1", policy="block").check(str(f))
        assert c["ok"] is False
        assert "另一个会话" in c["reason"]

    def test_same_session_writes_do_not_conflict(self, tmp_path):
        from automind.tools.write_guard import WriteGuard

        f = tmp_path / "a.txt"
        f.write_text("v1", encoding="utf-8")
        g = WriteGuard(session="s1")
        g.note_write(str(f))
        assert g.check(str(f))["foreign"] == ""

    def test_expected_hash_mismatch_blocks(self, tmp_path):
        from automind.tools.write_guard import WriteGuard, content_hash

        f = tmp_path / "a.txt"
        f.write_text("v2", encoding="utf-8")
        g = WriteGuard(session="s1")
        c = g.check(str(f), expected_hash=content_hash("v1"))
        assert c["ok"] is False and "内容校验失败" in c["reason"]
        c2 = g.check(str(f), expected_hash=content_hash("v2"))
        assert c2["ok"] is True

    def test_off_policy_disables_detection(self, tmp_path):
        from automind.tools.write_guard import WriteGuard

        f = tmp_path / "a.txt"
        WriteGuard(session="s2").note_write(str(f))
        assert WriteGuard(session="s1", policy="off").check(str(f))["foreign"] == ""

    def test_path_lock_serializes_same_path(self):
        from automind.tools.write_guard import path_lock

        order: list[str] = []

        async def worker(n: int, key: str):
            async with path_lock(key):
                order.append(f"in{n}")
                await asyncio.sleep(0.02)
                order.append(f"out{n}")

        async def main():
            await asyncio.gather(worker(1, "p"), worker(2, "p"))

        asyncio.run(main())
        # 同路径必须严格交替，绝不交错
        assert order in (["in1", "out1", "in2", "out2"],
                         ["in2", "out2", "in1", "out1"])


class TestFileToolsHonorGuard:
    @pytest.fixture(autouse=True)
    def _clean(self):
        from automind.core import session_ctx
        from automind.tools import write_guard
        from automind.tools.file_editor import JOURNAL
        write_guard.reset_for_tests()
        JOURNAL.clear()
        yield
        write_guard.reset_for_tests()
        JOURNAL.clear()
        _ = session_ctx

    def test_read_returns_content_hash_and_write_verifies_it(self, tmp_path):
        from automind.tools.file_editor import FileReadTool, FileWriteTool

        f = tmp_path / "x.txt"
        f.write_text("hello", encoding="utf-8")
        rd = asyncio.run(FileReadTool(project_root=tmp_path).execute(path="x.txt"))
        assert rd.success
        h = rd.output["content_hash"]

        wr = FileWriteTool(project_root=tmp_path)
        ok = asyncio.run(wr.execute(path="x.txt", content="world", expected_hash=h))
        assert ok.success

        # 旧指纹 → 拒绝写入（不会静默覆盖别人后来的改动）
        stale = asyncio.run(wr.execute(path="x.txt", content="third", expected_hash=h))
        assert stale.success is False
        assert "内容校验失败" in stale.error
        assert f.read_text(encoding="utf-8") == "world"

    def test_cross_session_write_warns(self, tmp_path):
        from automind.core.session_ctx import bind_session
        from automind.tools.file_editor import FileWriteTool

        f = tmp_path / "shared.txt"
        f.write_text("从 s2 写的", encoding="utf-8")
        tool = FileWriteTool(project_root=tmp_path)
        with bind_session("s2"):
            asyncio.run(tool.execute(path="shared.txt", content="s2 内容"))
        # s1 从未读过该文件，直接覆盖 → 必须给出冲突提示
        with bind_session("s1"):
            r = asyncio.run(tool.execute(path="shared.txt", content="s1 内容"))
        assert r.success
        assert r.output["conflict"]["foreign"] == "s2"
        assert "并发写冲突提示" in r.output["warning"]

    def test_session_workspace_isolation_in_resolution(self, tmp_path):
        """开启目录级隔离后，相对路径落到会话私有目录。"""
        from automind.core.session_ctx import bind_session
        from automind.tools.file_editor import FileWriteTool

        ws = tmp_path / "ws-s9"
        ws.mkdir()
        tool = FileWriteTool(project_root=tmp_path)
        with bind_session("s9", workspace=str(ws)):
            r = asyncio.run(tool.execute(path="out.txt", content="隔离产物"))
        assert r.success
        assert (ws / "out.txt").read_text(encoding="utf-8") == "隔离产物"
        assert not (tmp_path / "out.txt").exists()

    def test_workspace_isolation_still_blocks_escape(self, tmp_path):
        from automind.core.session_ctx import bind_session
        from automind.tools.file_editor import FileWriteTool

        ws = tmp_path / "ws-s9"
        ws.mkdir()
        tool = FileWriteTool(project_root=tmp_path)
        with bind_session("s9", workspace=str(ws)):
            r = asyncio.run(tool.execute(path="../../escape.txt", content="x"))
        assert r.success is False
        assert "越界" in r.error


# ═══════════════════════════════════════════════════════════
# 3. 工具输出体积（token 成本的最大单一来源）
# ═══════════════════════════════════════════════════════════


class TestOutputBudget:
    LIMITS = {"max_chars": 400, "head": 250, "tail": 100, "max_tokens": 0}

    def test_short_output_untouched(self):
        from automind.tools.output_budget import limited_tool_content

        text, stat = limited_tool_content("short", self.LIMITS)
        assert text == "short" and stat["truncated"] is False

    def test_long_output_keeps_both_ends(self):
        from automind.tools.output_budget import limited_tool_content

        body = "HEAD" + "x" * 5000 + "TAIL"
        text, stat = limited_tool_content(body, self.LIMITS, tool="terminal")
        assert stat["truncated"] is True
        assert stat["original_chars"] == len(body)
        assert text.startswith("HEAD")
        assert text.endswith("TAIL")               # 尾部（报错/结论）必须保留
        assert len(text) < len(body)
        # 关键：必须告诉模型**怎么拿回完整内容**，否则它只会原样重试
        assert "重新调用" in text and "offset/limit" in text
        assert stat["est_tokens_saved"] > 0

    def test_dict_output_serialized_and_clipped(self):
        from automind.tools.output_budget import limited_tool_content

        text, stat = limited_tool_content({"rows": ["y" * 500] * 20}, self.LIMITS)
        assert stat["truncated"] is True
        assert text.startswith("{")                # JSON 结构仍在

    def test_zero_max_restores_old_behaviour(self):
        from automind.tools.output_budget import limited_tool_content

        body = "z" * 10_000
        text, stat = limited_tool_content(body, {"max_chars": 0, "head": 0,
                                                 "tail": 0, "max_tokens": 0})
        assert stat["truncated"] is False and text == body

    def test_token_cap_applies_when_chars_unlimited(self):
        from automind.tools.output_budget import limited_tool_content

        body = "q" * 20_000
        text, stat = limited_tool_content(body, {"max_chars": 0, "head": 100,
                                                 "tail": 100, "max_tokens": 100})
        assert stat["truncated"] is True
        assert stat["original_chars"] - stat["kept_chars"] > 15_000

    def test_function_handler_applies_limit_and_reports(self):
        from automind.core.types import ToolCall, ToolResult
        from automind.tools.base import ToolRegistry
        from automind.tools.function_calling import FunctionCallHandler

        h = FunctionCallHandler(ToolRegistry(), output_limits=self.LIMITS)
        tc = ToolCall(id="1", name="terminal", arguments={})
        res = ToolResult(tool_name="terminal", success=True,
                         output={"stdout": "a" * 8000, "stderr": ""})
        msgs = h.tool_results_to_messages([tc], [res])
        assert len(msgs[0]["content"]) < 8000
        rep = h.savings_report()
        assert rep["truncated_results"] == 1
        assert rep["dropped_chars"] > 0

    def test_react_executor_reports_truncation(self):
        from automind.planning.react_executor import ReActExecutor
        from automind.tools.base import AbstractTool, ToolRegistry

        class Big(AbstractTool):
            name = "big"
            description = "big output"
            parameters = {"type": "object", "properties": {}}

            def __init__(self):
                from automind.core.types import PermissionTier
                self.permission_tier = PermissionTier.SAFE

            async def execute(self, **kwargs):
                from automind.core.types import ToolResult
                return ToolResult(tool_name=self.name, success=True,
                                  output="B" * 20_000)

        class LLM:
            n = 0

            async def generate(self, messages, tools=None, **kw):
                from automind.core.types import ToolCall
                self.n += 1
                class R:
                    text = "ok"
                    tool_calls = None
                r = R()
                if self.n == 1:
                    r.tool_calls = [ToolCall(id="1", name="big", arguments={})]
                return r

        reg = ToolRegistry()
        reg.register(Big())
        ex = ReActExecutor(llm=LLM(), tool_registry=reg, max_iterations=3,
                           tool_budget=0, output_limits=self.LIMITS)
        asyncio.run(ex.run("任务"))
        rep = ex.token_report()
        assert rep["truncated_results"] >= 1
        assert rep["chars_dropped"] > 0

    def test_compact_reports_ratio_and_keep_chars(self):
        from automind.planning.react_executor import ReActExecutor
        from automind.tools.base import ToolRegistry

        ex = ReActExecutor(llm=None, tool_registry=ToolRegistry(),
                           obs_keep_chars=100)
        ex.messages = [{"role": "tool", "content": "C" * 5000}] + [
            {"role": "tool", "content": "recent"} for _ in range(10)]
        stat = ex.compact()
        assert stat["folded"] == 1
        assert stat["keep_chars"] == 100
        assert 0 < stat["ratio"] < 1
        assert stat["est_tokens_saved"] > 0
        assert stat["chars_after"] < stat["chars_before"]

    def test_compact_uses_class_default_when_unset(self):
        from automind.planning.react_executor import ReActExecutor
        from automind.tools.base import ToolRegistry

        ex = ReActExecutor(llm=None, tool_registry=ToolRegistry())
        assert ex.obs_keep_chars == ReActExecutor.OBS_KEEP_CHARS


# ═══════════════════════════════════════════════════════════
# 4. 终端超时策略与后台通道
# ═══════════════════════════════════════════════════════════


class TestTerminalTimeoutPolicy:
    """终端超时与后台通道。

    注意：这些用例**只在单个事件循环里**跑完（``async def`` 内一次到底）。
    原因不是风格偏好 —— ``asyncio.run()`` 每次都会新建并关闭一个事件循环，
    而后台子进程的传输层挂在**创建它的那个循环**上。跨循环再 poll，
    轻则任务被销毁拿不到结果，重则在 Windows 上把子进程的管道留在半开状态，
    让后续的循环创建卡住。生产代码里这一点同样成立（见
    ``tools/background.py`` 把 ``asyncio.create_task`` 与进程绑在一起）。
    """

    def test_default_and_cap(self):
        from automind.tools.terminal import TerminalTool

        t = TerminalTool(timeout=300, max_timeout=600)
        assert t._effective_timeout(None)[0] == 300
        secs, note = t._effective_timeout(1200)
        assert secs == 600                        # 夹到上限
        assert "超过上限" in note                  # 且**不静默**夹取
        assert "background" in note

    def test_bad_timeout_falls_back_with_note(self):
        from automind.tools.terminal import TerminalTool

        t = TerminalTool(timeout=120)
        secs, note = t._effective_timeout("abc")
        assert secs == 120 and "无法解析" in note

    def test_timeout_message_is_self_healing(self):
        from automind.tools.terminal import TerminalTool

        t = TerminalTool(timeout=30, max_timeout=600)
        msg = t._timeout_guidance("pip install requests", 30)
        assert "timed out" in msg
        assert "timeout=" in msg                  # 给出具体加大的写法
        assert "background=true" in msg           # 给出长耗时命令的正解
        assert "不要原样重跑" in msg
        assert "长耗时类别" in msg                 # 认得出这是安装类命令

    def test_timeout_result_marks_timed_out(self):
        from automind.tools.terminal import TerminalTool

        t = TerminalTool(timeout=0.3, max_timeout=10, background_enabled=False)
        r = asyncio.run(t.execute(command="python -c \"import time; time.sleep(5)\""))
        assert r.success is False
        assert r.timed_out is True
        assert r.output["timed_out"] is True
        assert "timeout=" in r.error

    def test_async_foreground_still_works(self):
        from automind.tools.terminal import TerminalTool

        t = TerminalTool(timeout=20)
        r = asyncio.run(t.execute(command='python -c "print(123)"'))
        assert r.success and r.output["stdout"].strip() == "123"
        assert r.timed_out is False

    def test_background_lifecycle_in_one_loop(self):
        """发起 → 立刻 poll（必须如实说 running）→ 等到结束 → 输出正确。"""
        _require_bg_subprocess()
        from automind.tools.terminal import TerminalBackgroundTool, TerminalTool

        async def main():
            t = TerminalTool(timeout=30)
            poll = TerminalBackgroundTool()
            started = await t.execute(command='python -c "print(\'bg-ok\')"',
                                      background=True)
            assert started.success
            tid = started.output["task_id"]
            assert started.output["status"] == "running"
            assert "terminal_background" in started.output["note"]

            first = await poll.execute(action="poll", task_id=tid)
            # 仍在跑时绝不能报成功 —— 那会让模型以为已经装好了
            assert first.output["status"] in ("running", "ok", "failed")
            if first.output["status"] == "running":
                assert first.success is False

            for _ in range(100):
                r = await poll.execute(action="poll", task_id=tid)
                if r.output["status"] != "running":
                    return r
                await asyncio.sleep(0.1)
            raise AssertionError("后台任务没有在限期内结束")

        r = asyncio.run(main())
        assert r.output["status"] == "ok"
        assert "bg-ok" in r.output["stdout_tail"]
        assert r.success is True

    def test_background_kill(self):
        _require_bg_subprocess()
        from automind.tools.terminal import TerminalBackgroundTool, TerminalTool

        async def main():
            t = TerminalTool(timeout=60)
            poll = TerminalBackgroundTool()
            started = await t.execute(
                command='python -c "import time; time.sleep(30)"', background=True)
            tid = started.output["task_id"]
            listing = await poll.execute(action="list")
            assert any(x["task_id"] == tid for x in listing.output["tasks"])
            killed = await poll.execute(action="kill", task_id=tid)
            assert killed.output["status"] == "killed"
            return await poll.execute(action="poll", task_id=tid)

        r = asyncio.run(main())
        assert r.output["status"] == "killed"
        assert r.success is False                 # 被终止的任务不算成功
        assert "手动终止" in r.output["error"]
        # 被我们杀掉的进程退出码必然非零 —— 关键是不能因此被记成"命令失败"
        assert "退出码" not in (r.output["error"] or "")

    def test_background_unknown_task_explains(self):
        from automind.tools.terminal import TerminalBackgroundTool

        r = asyncio.run(TerminalBackgroundTool().execute(action="poll",
                                                         task_id="bg404"))
        assert r.success is False
        assert "不存在" in r.error and "action='list'" in r.error

    def test_background_requires_task_id(self):
        from automind.tools.terminal import TerminalBackgroundTool

        r = asyncio.run(TerminalBackgroundTool().execute(action="poll"))
        assert r.success is False and "缺少 task_id" in r.error

    def test_background_channel_can_be_disabled(self):
        from automind.tools.terminal import TerminalTool

        t = TerminalTool(background_enabled=False)
        r = asyncio.run(t.execute(command='echo hi', background=True))
        assert r.success is False
        assert "后台通道已在配置中关闭" in r.error

    def test_agent_registers_background_tool(self, tmp_path):
        from automind.agent import AutoMindAgent
        from automind.core.config import AgentConfig

        a = AutoMindAgent(AgentConfig(project_root=str(tmp_path)))
        assert "terminal_background" in a.tool_registry
        term = a.tool_registry.get("terminal")
        assert term.timeout == a.config.execution.tool_timeout_seconds
        assert term.max_timeout == a.config.execution.tool_timeout_max_seconds


class TestWorkspaceModule:
    """会话工作副本。

    根目录**必须落在项目之内**（``<project>/.automind/workspaces``）：
    ``.automind`` 在 .gitignore 内不会污染仓库，且与文件工具的"限定在
    project_root 之内"这条安全边界一致 —— 副本在项目外时，隔离目录里的
    每一次写入都会被自己的越界防护拒绝（曾真实发生，端到端自检才抓到）。
    """

    def test_prepare_disabled_by_default(self, tmp_path):
        from automind.core import workspace

        plan = workspace.prepare("s1", tmp_path, None)
        assert plan.isolated is False
        assert plan.path == str(tmp_path.resolve())

    def test_root_dir_is_inside_project(self, tmp_path):
        from automind.core import workspace

        r = workspace.root_dir(tmp_path)
        assert r == (tmp_path / ".automind" / "workspaces").resolve()
        assert tmp_path.resolve() in r.parents

    def test_outside_project_override_is_ignored(self, tmp_path, monkeypatch):
        """环境变量把副本指到项目外 → 忽略并回退，而不是制造越界写入。"""
        from automind.core import workspace

        monkeypatch.setenv("AUTOMIND_WORKSPACE_DIR", str(tmp_path.parent / "outside"))
        r = workspace.root_dir(tmp_path)
        assert r == (tmp_path / ".automind" / "workspaces").resolve()

    def test_inside_project_override_is_honored(self, tmp_path, monkeypatch):
        from automind.core import workspace

        monkeypatch.setenv("AUTOMIND_WORKSPACE_DIR",
                           str(tmp_path / ".cache" / "ws"))
        assert workspace.root_dir(tmp_path) == (tmp_path / ".cache" / "ws").resolve()

    def test_prepare_creates_copy_and_reports(self, tmp_path, monkeypatch):
        from automind.core import workspace

        project = tmp_path / "proj"
        project.mkdir()
        (project / "src.py").write_text("x = 1", encoding="utf-8")
        (project / "node_modules").mkdir()
        (project / "node_modules" / "big.js").write_text("junk", encoding="utf-8")

        class Ex:
            isolate_workspace = True
            workspace_keep = 4

        plan = workspace.prepare("sess1", project, Ex())
        assert plan.isolated is True
        target = project / ".automind" / "workspaces" / "sess1"
        assert (target / "src.py").is_file()
        # node_modules 被跳过（不复制可重建的大目录）
        assert not (target / "node_modules").exists()
        # .automind 自己被跳过 —— 否则第二次复制会把上一份副本再拷一层
        assert not (target / ".automind").exists()

    def test_copy_is_usable_by_file_tools(self, tmp_path):
        """副本里的相对路径写入必须被文件工具接受（端到端自检抓到的缺陷）。"""
        import asyncio

        from automind.core import workspace
        from automind.core.session_ctx import bind_session
        from automind.tools.file_editor import FileWriteTool

        project = tmp_path / "proj"
        project.mkdir()

        class Ex:
            isolate_workspace = True
            workspace_keep = 4

        plan = workspace.prepare("sess-w", project, Ex())
        assert plan.isolated
        tool = FileWriteTool(project_root=project)
        with bind_session("sess-w", workspace=plan.path):
            r = asyncio.run(tool.execute(path="out.txt", content="hi"))
        assert r.success, r.error
        assert (Path(plan.path) / "out.txt").is_file()

    def test_prepare_never_recurses_into_itself(self, tmp_path, monkeypatch):
        """工作副本根目录落在项目之内时，绝不能把自己拷进自己。

        历史上的失败形态：复制时把刚建出来的 ``workspaces/sess`` 也当成项目
        内容继续往里拷，路径一层层变长直到 Windows 报 WinError 206 ——
        而异常被兜住后只是"静默降级为共享目录"，用户完全不知道隔离没生效。
        """
        from automind.core import workspace

        project = tmp_path / "proj2"
        project.mkdir()
        (project / "app.py").write_text("print(1)", encoding="utf-8")

        class Ex:
            isolate_workspace = True
            workspace_keep = 4

        plan = workspace.prepare("sess-x", project, Ex())
        assert plan.isolated is True
        target = project / ".automind" / "workspaces" / "sess-x"
        assert (target / "app.py").is_file()
        assert not (target / ".automind").exists()
        assert plan.files == 1

    def test_prepare_reports_reason_on_failure(self, tmp_path, monkeypatch):
        from automind.core import workspace

        class Ex:
            isolate_workspace = True
            workspace_keep = 4
        monkeypatch.setattr(workspace, "MAX_FILES", 2)
        for i in range(6):
            (tmp_path / f"f{i}.py").write_text("x", encoding="utf-8")
        plan = workspace.prepare("sess2", tmp_path, Ex())
        assert plan.isolated is False
        assert "超过" in plan.reason

    def test_export_results(self, tmp_path):
        from automind.core import workspace

        project = tmp_path / "proj3"
        project.mkdir()

        class Ex:
            isolate_workspace = True
            workspace_keep = 4

        plan = workspace.prepare("sess3", project, Ex())
        assert plan.isolated
        target = Path(plan.path)
        (target / "made.txt").write_text("hi", encoding="utf-8")
        out = workspace.export_results("sess3", project)
        assert out["ok"] and out["count"] == 1
        assert (target / workspace.EXPORT_DIRNAME / "made.txt").is_file()

    def test_env_override_controls_isolation(self, tmp_path, monkeypatch):
        from automind.core import workspace

        class Ex:
            isolate_workspace = False
            workspace_keep = 4
        assert workspace.enabled(Ex()) is False
        monkeypatch.setenv("AUTOMIND_ISOLATE_WORKSPACE", "1")
        assert workspace.enabled(Ex()) is True          # 环境变量优先
        monkeypatch.setenv("AUTOMIND_ISOLATE_WORKSPACE", "off")
        class Ex2:
            isolate_workspace = True
            workspace_keep = 4
        assert workspace.enabled(Ex2()) is False


# ═══════════════════════════════════════════════════════════
# 5. 审批等待占槽治理
# ═══════════════════════════════════════════════════════════


class TestApprovalConfig:
    def test_defaults_are_fail_closed(self):
        from automind.core.config import ExecutionConfig

        ex = ExecutionConfig()
        assert ex.approval_timeout_action == "reject"      # 默认自动拒绝
        assert ex.release_slot_on_approval_wait is True
        assert ex.approval_timeout_seconds > 0

    def test_server_reads_config_first(self, tmp_path):
        from automind import server as srv
        from automind.agent import AutoMindAgent
        from automind.core.config import AgentConfig

        cfg = AgentConfig(project_root=str(tmp_path))
        cfg.execution.approval_timeout_seconds = 42
        cfg.execution.approval_timeout_action = "approve"
        a = AutoMindAgent(cfg)
        assert srv._approval_timeout_seconds(a) == 42
        assert srv._approval_timeout_action(a) == "approve"

    def test_server_falls_back_to_env_value(self, tmp_path):
        from automind import server as srv
        from automind.agent import AutoMindAgent
        from automind.core.config import AgentConfig

        cfg = AgentConfig(project_root=str(tmp_path))
        cfg.execution.approval_timeout_seconds = 0
        a = AutoMindAgent(cfg)
        assert srv._approval_timeout_seconds(a) == float(srv._APPROVAL_TIMEOUT_S)

    def test_bad_action_falls_back_to_reject(self, tmp_path):
        from automind import server as srv
        from automind.agent import AutoMindAgent
        from automind.core.config import AgentConfig

        cfg = AgentConfig(project_root=str(tmp_path))
        cfg.execution.approval_timeout_action = "allow-everything"   # 非法
        a = AutoMindAgent(cfg)
        assert srv._approval_timeout_action(a) == "reject"           # fail-closed

    def test_health_exposes_waiting_counter(self, tmp_path, monkeypatch):
        """让监控能区分"在算"和"在等人审批" —— 后者已让出并发槽。"""
        from fastapi.testclient import TestClient

        from automind import server as srv

        monkeypatch.setattr(srv._store, "config_file", tmp_path / "cfg.json",
                            raising=False)
        srv._AUTH_TOKEN = ""
        h = TestClient(srv.app).get("/api/health").json()
        assert "approval_waiting" in h
        assert h["running_tasks"] >= 0
        assert h["version"] == __import__("automind").__version__
