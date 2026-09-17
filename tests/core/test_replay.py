"""可重放轨迹测试 —— 默认不落盘 / 开启后字段完整 / 轮转 / 脱敏 / 重放。

这些用例**全部离线**：用一个假的 LLM 后端（继承 ``LLMBackend``，因此会真的
走 ``__init_subclass__`` 那层包装器，也就真的会走到 ``llm.py`` 里的接线点）
产生真实的 ``LLMResponse``，不联网、不需要 API Key。

重点覆盖的失效形态（评测/取证类功能最容易"看起来在工作"）：

  · 关闭时**一个字节都不写**（否则等于偷偷把客户数据落盘）；
  · 长提示词/大文件正文**不被截断**（截断即失去"重建请求"的意义）；
  · 超上限时**有明确事件**而不是静默丢数据；
  · 密钥既不进 key 也不藏在 content 里。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from automind.core import replay
from automind.core.config import LLMProviderConfig
from automind.core.llm import LLMBackend
from automind.core.session_ctx import bind_session
from automind.core.types import LLMResponse, ToolCall

# ═══════════════════════════════════════════════════════════════
# 测试替身
# ═══════════════════════════════════════════════════════════════


class FakeBackend(LLMBackend):
    """最小可用后端：记录收到的入参，返回构造好的响应。

    继承 ``LLMBackend`` 是刻意的 —— ``generate`` 会被 ``__init_subclass__``
    包上用量上报与可重放记录，因此这里的调用路径与真实 provider 完全一致。
    """

    def __init__(self, text: str = "done", tool_calls=None) -> None:
        super().__init__(LLMProviderConfig(
            provider="deepseek", model="deepseek-chat", api_key="sk-test-fake-key",
            temperature=0.3, max_tokens=1234))
        self._model = "deepseek-chat"
        self.reply = text
        self.calls: list[dict] = []
        self._reply_tools = tool_calls

    async def generate(self, messages, tools=None, stop=None):  # type: ignore[override]
        self.calls.append({"messages": messages, "tools": tools})
        return LLMResponse(
            text=self.reply,
            tool_calls=self._reply_tools,
            prompt_tokens=11,
            completion_tokens=7,
            finish_reason="stop",
            provider="deepseek",
            model="deepseek-chat",
        )

    async def generate_stream(self, messages, tools=None):  # type: ignore[override]
        yield self.reply


TOOLS = [{
    "name": "file_write",
    "description": "写文件",
    "parameters": {"type": "object",
                   "properties": {"path": {"type": "string"}},
                   "required": ["path"]},
}]


@pytest.fixture
def recorder(tmp_path, monkeypatch):
    """把进程级记录器指向临时目录，并在用例结束后恢复原状。"""
    for key in ("AUTOMIND_REPLAY", "AUTOMIND_REPLAY_DIR", "AUTOMIND_REPLAY_MAX_BYTES",
                "AUTOMIND_REPLAY_ROTATE"):
        monkeypatch.delenv(key, raising=False)
    rec = replay.configure_recorder(root=tmp_path / "traces", enabled=True)
    yield rec
    replay.configure_recorder(root=tmp_path / "traces-restore", enabled=False)


def _lines(path: Path) -> list[dict]:
    assert path.is_file(), f"轨迹文件不存在：{path}"
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _the_only_call(path: Path) -> dict:
    calls = [r for r in _lines(path) if r["type"] == "llm_call"]
    assert len(calls) == 1
    return calls[0]


def _rotation_order(directory: Path) -> list[Path]:
    """按轮转序号排列分片：``r.replay.jsonl``, ``r.replay.2.jsonl`` … ``.10``。

    直接 ``sorted()`` 会把 ``.10`` 排到 ``.2`` 前面（字符串序），
    于是"第 0 个分片"的断言会随分片个数变化而时对时错。
    """
    def _seq(p: Path) -> int:
        parts = p.name.split(".")
        if len(parts) == 4:                      # r.replay.<n>.jsonl
            return int(parts[2])
        return 1                                 # r.replay.jsonl

    return sorted(directory.glob("*.replay*.jsonl"), key=_seq)


# ═══════════════════════════════════════════════════════════════
# 1. 默认关闭：零写入
# ═══════════════════════════════════════════════════════════════


class TestDisabledByDefault:
    async def test_env_absent_means_disabled(self, tmp_path, monkeypatch):
        for key in ("AUTOMIND_REPLAY", "AUTOMIND_REPLAY_DIR"):
            monkeypatch.delenv(key, raising=False)
        rec = replay.configure_recorder(root=tmp_path / "traces")
        assert rec.enabled is False, "完整提示词含客户数据，默认必须是关闭的"
        try:
            await FakeBackend().generate([{"role": "user", "content": "秘密内容"}],
                                        tools=TOOLS)
        finally:
            replay.configure_recorder(root=tmp_path / "traces2", enabled=False)
        assert list((tmp_path / "traces").rglob("*")) == [], "关闭时不应写任何文件"

    async def test_env_flag_enables(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTOMIND_REPLAY", "1")
        monkeypatch.setenv("AUTOMIND_REPLAY_DIR", str(tmp_path / "traces"))
        rec = replay.configure_recorder()
        assert rec.enabled is True
        try:
            with bind_session("s-env", run_id="r-env"):
                await FakeBackend().generate([{"role": "user", "content": "hi"}])
        finally:
            replay.configure_recorder(root=tmp_path / "x", enabled=False)
        assert rec.path_for("s-env", "r-env").is_file()

    async def test_config_flag_enables(self, tmp_path, monkeypatch):
        """配置字段 ``replay_capture=True`` 也要能开启（走 getattr，不改 config.py）。"""
        monkeypatch.delenv("AUTOMIND_REPLAY", raising=False)
        monkeypatch.setenv("AUTOMIND_REPLAY_DIR", str(tmp_path / "traces"))

        from automind.core import config as cfg_mod

        class _Ex(cfg_mod.ExecutionConfig):
            replay_capture: bool = True

        monkeypatch.setattr(cfg_mod, "ExecutionConfig", _Ex)
        rec = replay.configure_recorder()
        assert rec.enabled is True
        try:
            with bind_session("s-cfg", run_id="r-cfg"):
                await FakeBackend().generate([{"role": "user", "content": "hi"}])
        finally:
            replay.configure_recorder(root=tmp_path / "x", enabled=False)
        assert rec.path_for("s-cfg", "r-cfg").is_file()

    def test_record_call_returns_empty_when_disabled(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AUTOMIND_REPLAY", raising=False)
        rec = replay.configure_recorder(root=tmp_path / "traces")
        try:
            assert rec.enabled is False
            assert replay.record_call(FakeBackend(), LLMResponse(text="x")) == ""
        finally:
            replay.configure_recorder(root=tmp_path / "x", enabled=False)


# ═══════════════════════════════════════════════════════════════
# 2. 开启后：字段完整、可重建请求
# ═══════════════════════════════════════════════════════════════


class TestRecordedFields:
    async def test_everything_needed_to_rebuild_the_request(self, recorder):
        long_prompt = "题" * 9000                     # trace.py 会截到 4000，这里必须留住
        backend = FakeBackend(text="已写入", tool_calls=[
            ToolCall(id="call_1", name="file_write", arguments={"path": "a.py"})])
        with bind_session("sess-1", run_id="run-1"):
            await backend.generate(
                [{"role": "system", "content": "你是助手"},
                 {"role": "user", "content": long_prompt}],
                tools=TOOLS, stop=None)

        path = recorder.path_for("sess-1", "run-1")
        rec = _the_only_call(path)
        req, resp = rec["request"], rec["response"]

        assert rec["session_id"] == "sess-1" and rec["run_id"] == "run-1"
        assert rec["type"] == "llm_call" and rec["seq"] == 1
        # 参数齐备 —— 少了任何一个都重放不出等价请求
        assert req["provider"] == "deepseek"
        assert req["model"] == "deepseek-chat"
        assert req["temperature"] == 0.3
        assert req["max_tokens"] == 1234
        # messages 未截断
        assert req["messages"][1]["content"] == long_prompt
        assert len(req["messages"][1]["content"]) == 9000
        # tools schema 完整
        assert req["tools"][0]["name"] == "file_write"
        assert req["tools"][0]["parameters"]["required"] == ["path"]
        # 响应侧
        assert resp["text"] == "已写入"
        assert resp["tool_calls"][0] == {"id": "call_1", "name": "file_write",
                                         "arguments": {"path": "a.py"}}
        assert resp["prompt_tokens"] == 11 and resp["completion_tokens"] == 7
        assert rec["usage"]["prompt_tokens"] == 11
        assert isinstance(rec["ts"], float) and rec["elapsed_seconds"] >= 0

    async def test_records_tools_bound_on_backend_when_not_passed(self, recorder):
        """provider 的行为是 ``tools or self._tools``，记录也必须是"实际发出"的那份。"""
        backend = FakeBackend()
        backend.register_tools(TOOLS)
        with bind_session("sess-2", run_id="run-2"):
            await backend.generate([{"role": "user", "content": "hi"}])
        rec = _the_only_call(recorder.path_for("sess-2", "run-2"))
        assert [t["name"] for t in rec["request"]["tools"]] == ["file_write"]

    async def test_multiple_calls_are_append_only(self, recorder):
        backend = FakeBackend()
        with bind_session("sess-3", run_id="run-3"):
            for i in range(3):
                await backend.generate([{"role": "user", "content": f"第{i}问"}])
        recs = _lines(recorder.path_for("sess-3", "run-3"))
        assert [r["seq"] for r in recs] == [1, 2, 3]
        assert [r["request"]["messages"][0]["content"] for r in recs] == \
            ["第0问", "第1问", "第2问"]

    async def test_defaults_to_default_ids_without_session(self, recorder):
        await FakeBackend().generate([{"role": "user", "content": "hi"}])
        assert recorder.path_for("default", "default").is_file()


# ═══════════════════════════════════════════════════════════════
# 3. 上限与轮转
# ═══════════════════════════════════════════════════════════════


class TestRotation:
    async def test_rotates_and_records_skips(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AUTOMIND_REPLAY", raising=False)
        rec = replay.configure_recorder(root=tmp_path / "tr", enabled=True,
                                        max_file_bytes=1200, rotate=True)
        try:
            backend = FakeBackend(text="x" * 50)
            with bind_session("s", run_id="r"):
                for i in range(12):
                    await backend.generate([{"role": "user", "content": "长" * 60 + str(i)}])
        finally:
            replay.configure_recorder(root=tmp_path / "x", enabled=False)

        # 注意 glob：轮转文件名是 r.replay.2.jsonl，`*` 不跨 `.`，
        # 用 "*.replay.jsonl" 永远只能看到第一个文件（这正是本用例要防的坑）
        files = _rotation_order(tmp_path / "tr" / "s")
        assert len(files) > 1, "超上限后应轮转到新文件，而不是把后面的轨迹丢掉"
        assert files[0].name == "r.replay.jsonl"
        # 第一个文件末尾必须有明确的截断事件（不是静默丢数据）
        first = _lines(files[0])
        assert any(r["type"] == "replay_truncated" for r in first), \
            "超过上限却没有留下 '已截断' 事件"
        # 第二个文件开头必须有轮转事件，并说明上一文件丢了几次调用
        second = _lines(files[1])
        assert second[0]["type"] == "replay_rotated"
        assert second[0]["skipped_in_previous"] >= 1
        # 落盘的调用数 == 全部调用数：轮转的意义就在这里 —— 超上限后换文件继续
        # 记全，而不是把后半程丢掉（skipped_in_previous 只作审计线索，不重复计数）
        recorded = sum(1 for f in files for r in _lines(f) if r["type"] == "llm_call")
        assert recorded == 12, f"轮转后仍有调用丢失：只记下 {recorded}/12"

    async def test_no_rotation_keeps_single_file_with_notice(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AUTOMIND_REPLAY", raising=False)
        rec = replay.configure_recorder(root=tmp_path / "tr2", enabled=True,
                                        max_file_bytes=900, rotate=False)
        try:
            backend = FakeBackend(text="x" * 40)
            with bind_session("s", run_id="r"):
                for i in range(8):
                    await backend.generate([{"role": "user", "content": "长" * 80 + str(i)}])
        finally:
            replay.configure_recorder(root=tmp_path / "x", enabled=False)

        files = list((tmp_path / "tr2" / "s").glob("*.replay*.jsonl"))
        assert len(files) == 1
        assert rec.stats()["skipped_calls"] >= 1
        kinds = [r["type"] for r in _lines(files[0])]
        assert "replay_truncated" in kinds

    def test_oversized_single_record_is_marked_truncated(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AUTOMIND_REPLAY", raising=False)
        rec = replay.configure_recorder(root=tmp_path / "tr3", enabled=True,
                                        max_file_bytes=2000)
        try:
            rec.record("s", "r", {"request": {"messages": [
                {"role": "user", "content": "巨" * 20000}]}})
        finally:
            replay.configure_recorder(root=tmp_path / "x", enabled=False)
        rec_obj = _lines(rec.path_for("s", "r"))[0]
        assert rec_obj["truncated"] is True
        assert "不能" in rec_obj["truncate_reason"]


# ═══════════════════════════════════════════════════════════════
# 4. 脱敏
# ═══════════════════════════════════════════════════════════════


class TestRedaction:
    async def test_secret_keys_never_reach_disk(self, recorder):
        backend = FakeBackend()
        with bind_session("s", run_id="r"):
            await backend.generate([
                {"role": "user", "content": "hi",
                 "api_key": "sk-live-abcdefghijklmnopqrstuvwxyz",
                 "authorization": "Bearer abcdefghijklmnopqrstuvwxyz"},
                {"role": "tool", "content": "ok", "metadata": {
                    "access_token": "tok-1234567890abcdef"}},
            ])
        text = recorder.path_for("s", "r").read_text(encoding="utf-8")
        assert "sk-live-abcdefghijklmnopqrstuvwxyz" not in text
        assert "tok-1234567890abcdef" not in text
        assert "abcdefghijklmnopqrstuvwxyz" not in text

    async def test_secret_inside_content_is_scrubbed(self, recorder):
        """密钥被粘贴进普通 content 时，也要靠 redact.py 的正则兜住。"""
        backend = FakeBackend()
        with bind_session("s", run_id="r"):
            await backend.generate([{
                "role": "user",
                "content": "我的 key 是 sk-proj-abcdefghijklmnopqrstuvwxyz012345，请记住"}])
        text = recorder.path_for("s", "r").read_text(encoding="utf-8")
        assert "sk-proj-abcdefghijklmnopqrstuvwxyz012345" not in text
        assert "REDACTED" in text

    def test_read_records_sanitizes_on_the_way_in(self, tmp_path):
        """读取别人的轨迹文件时再脱敏一遍：落盘已过滤不能是唯一保证。"""
        p = tmp_path / "evil.replay.jsonl"
        p.write_text(json.dumps({
            "type": "llm_call",
            "request": {"messages": [{"role": "user", "content": "x",
                                      "api_key": "sk-leaked-1234567890abcdef"}]},
        }, ensure_ascii=False), encoding="utf-8")
        rec = replay.read_records(p)[0]
        assert rec["request"]["messages"][0]["api_key"] == "***"


# ═══════════════════════════════════════════════════════════════
# 5. dry-run 与重放
# ═══════════════════════════════════════════════════════════════


class TestDryRun:
    async def test_dry_run_lists_every_call_without_network(self, recorder, capsys):
        backend = FakeBackend()
        with bind_session("s", run_id="r"):
            for i in range(3):
                await backend.generate([{"role": "user", "content": f"问 {i}"}],
                                       tools=TOOLS)
        path = recorder.path_for("s", "r")
        report = replay.dry_run_report(replay.read_records(path))
        assert report["planned_calls"] == 3
        assert report["complete"] is True
        assert report["calls"][0]["tools"] == 1
        assert "问 0" in report["calls"][0]["last_user_preview"]

        code = replay.main([str(path), "--dry-run"])
        out = capsys.readouterr().out
        assert code == 0
        assert "将重放 3 次调用" in out
        assert "deepseek/deepseek-chat" in out

    async def test_dry_run_reports_incompleteness(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("AUTOMIND_REPLAY", raising=False)
        rec = replay.configure_recorder(root=tmp_path / "tr", enabled=True,
                                        max_file_bytes=800, rotate=True)
        try:
            backend = FakeBackend(text="x" * 30)
            with bind_session("s", run_id="r"):
                for i in range(10):
                    await backend.generate([{"role": "user", "content": "长" * 70 + str(i)}])
        finally:
            replay.configure_recorder(root=tmp_path / "x", enabled=False)
        code = replay.main([str(tmp_path / "tr" / "s"), "--dry-run"])
        out = capsys.readouterr().out
        assert code == 0
        assert "不完整" in out


class TestReplay:
    async def test_replay_compares_old_and_new_responses(self, recorder):
        backend = FakeBackend(text="原始回答", tool_calls=[
            ToolCall(id="c1", name="file_write", arguments={"path": "a.py"})])
        with bind_session("s", run_id="r"):
            await backend.generate([{"role": "user", "content": "hi"}], tools=TOOLS)
        path = recorder.path_for("s", "r")

        same = FakeBackend(text="原始回答", tool_calls=[
            ToolCall(id="c2", name="file_write", arguments={"path": "a.py"})])
        report = await replay.replay_file(
            path, backend_factory=lambda _req, _model, _provider: (same, "fake/model"))
        assert report["total"] == 1 and report["failed_calls"] == 0
        r = report["results"][0]
        assert r["text_similarity"] == 1.0 and r["text_equal"] is True
        assert r["tool_names_match"] is True
        assert r["prompt_tokens_delta"] == 0

        other = FakeBackend(text="完全不同的另一段回答", tool_calls=[
            ToolCall(id="c3", name="file_read", arguments={"path": "a.py"})])
        report2 = await replay.replay_file(
            path, backend_factory=lambda _req, _model, _provider: (other, "fake/model"))
        r2 = report2["results"][0]
        assert r2["text_similarity"] < 1.0
        assert r2["tool_names_match"] is False
        assert r2["expected_tools"] == ["file_write"] and r2["actual_tools"] == ["file_read"]

    async def test_replay_reports_failed_calls_instead_of_pretending(self, recorder):
        backend = FakeBackend()
        with bind_session("s", run_id="r"):
            await backend.generate([{"role": "user", "content": "hi"}])
        path = recorder.path_for("s", "r")

        def boom(_req, _model, _provider):
            raise RuntimeError("模型不可用")

        report = await replay.replay_file(path, backend_factory=boom)
        assert report["failed_calls"] == 1
        assert "模型不可用" in report["results"][0]["error"]

    def test_missing_credentials_exits_nonzero(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("AUTOMIND_REPLAY", raising=False)
        p = tmp_path / "t.replay.jsonl"
        p.write_text(json.dumps({
            "type": "llm_call",
            "request": {"provider": "openai", "model": "gpt-4o-mini",
                        "messages": [{"role": "user", "content": "hi"}]},
        }, ensure_ascii=False), encoding="utf-8")
        code = replay.main([str(p)])
        err = capsys.readouterr().err
        assert code == replay.EXIT_NO_CREDENTIALS != 0
        assert "无法评测" not in err          # 这是重放模块，措辞应是"重放需要真实调用"
        assert "API Key" in err

    def test_missing_file_exits_nonzero(self, capsys):
        assert replay.main(["不存在.replay.jsonl", "--dry-run"]) != 0

    def test_similarity_helper(self):
        assert replay.similarity("", "") == 1.0
        assert replay.similarity("abc", "abc") == 1.0
        assert 0.0 <= replay.similarity("abc", "xyz") < 1.0


# ═══════════════════════════════════════════════════════════════
# 6. 参数提取与 sanitize 的边界
# ═══════════════════════════════════════════════════════════════


class TestHelpers:
    def test_split_generate_args_handles_positional_and_keyword(self):
        msgs = [{"role": "user", "content": "hi"}]
        assert replay.split_generate_args((msgs,), {}) == (msgs, None)
        assert replay.split_generate_args((), {"messages": msgs}) == (msgs, None)
        assert replay.split_generate_args((), {}) == ([], None)
        assert replay.split_generate_args((msgs, TOOLS), {}) == (msgs, TOOLS)

    def test_sanitize_is_not_truncating_long_text(self):
        """与 trace.py 的 4000 字符截断划清界限：这是本模块存在的理由。"""
        text = "x" * 50000
        assert replay.sanitize({"content": text})["content"] == text

    def test_tool_schema_keeps_parameters_but_caps_description(self):
        long_desc = "d" * 5000
        out = replay._tool_schema_tools([
            {"type": "function", "function": {
                "name": "t", "description": long_desc,
                "parameters": {"type": "object", "properties": {"a": {"type": "string"}}}}}])
        assert out[0]["name"] == "t"
        assert len(out[0]["description"]) < 5000 and "截断" in out[0]["description"]
        assert out[0]["parameters"]["properties"]["a"] == {"type": "string"}

    def test_stats_reports_enabled_state(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AUTOMIND_REPLAY", raising=False)
        rec = replay.ReplayRecorder(root=tmp_path, enabled=False)
        assert rec.stats()["enabled"] is False
        assert rec.stats()["bytes"] == 0

    def test_namespace_response_is_accepted(self, recorder):
        """响应不是 LLMResponse 而是简单对象时也要能记（重放/三方后端场景）。"""
        fake_backend = FakeBackend()
        resp = SimpleNamespace(text="t", tool_calls=None, prompt_tokens=3,
                               completion_tokens=4, finish_reason="stop",
                               provider="deepseek", model="deepseek-chat")
        with bind_session("s", run_id="r"):
            replay.record_call(fake_backend, resp, 0.5,
                               messages=[{"role": "user", "content": "hi"}])
        rec = _the_only_call(recorder.path_for("s", "r"))
        assert rec["response"]["text"] == "t" and rec["usage"]["completion_tokens"] == 4
