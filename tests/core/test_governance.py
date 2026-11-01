"""治理审计账本（v1.11.19）回归测试。

盯住四类"事后说不清"的风险：

1. **链可验证**：记录按序哈希链接，篡改/删中间记录必须被 ``verify_chain``
   指到具体 seq，而不是"看起来没问题"；
2. **可持久**：轮转、跨进程重启后 seq 与 prev 连续（state.json + 磁盘自愈）；
3. **不泄密**：params 脱敏 + 截断，密钥/文件内容明文不落盘；
4. **不添乱**：任何写入/读取故障都 fail-open（返回 None/空结果、不抛异常）。

全部用例用 tmp_path + reset_for_tests 隔离账本，不污染仓库 .automind。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

import pytest

from automind.core import governance

# ═══════════════════════════════════════════════════════════
# 夹具：每个用例一个独立账本目录
# ═══════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path):
    """把进程级账本切到临时目录；收尾再切走，避免影响后续用例。"""
    governance.reset_for_tests(tmp_path / "governance", enabled=True)
    yield
    governance.reset_for_tests(tmp_path / "governance-teardown", enabled=True)


def _ledger_files(root: Path) -> list[Path]:
    return sorted(root.glob("ledger-*.jsonl"))


def _read_records(root: Path) -> list[dict]:
    records: list[dict] = []
    for path in _ledger_files(root):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def _canonical_hash(record: dict) -> str:
    body = {k: v for k, v in record.items() if k != "hash"}
    blob = json.dumps(body, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _write_decisions(count: int, *, session: str = "s1") -> list[dict]:
    return [
        governance.record_decision(
            tool=f"tool_{i}", tier="safe", risk=10, decision="allow",
            reason=f"第 {i} 条", source="direct", session_id=session,
            params={"i": str(i)})
        for i in range(count)
    ]


# ═══════════════════════════════════════════════════════════
# 1. 哈希链：自洽、可复算、篡改/删除可定位
# ═══════════════════════════════════════════════════════════


class TestHashChain:
    def test_records_are_chained_and_verifiable(self, tmp_path):
        records = _write_decisions(3)
        assert all(r is not None for r in records), "账本默认应启用并成功落盘"
        assert [r["seq"] for r in records] == [1, 2, 3]
        assert records[0]["prev"] == ""
        assert records[1]["prev"] == records[0]["hash"]
        assert records[2]["prev"] == records[1]["hash"]
        for record in records:
            assert record["hash"] == _canonical_hash(record), "hash 必须可按内容复算"
            assert len(record["hash"]) == 64

        result = governance.verify_chain()
        assert result["ok"] is True
        assert result["checked"] == 3
        assert result["broken_at"] is None
        assert result["files"] == ["ledger-" + time.strftime("%Y%m%d") + ".jsonl"]

    def test_tampered_record_is_detected(self, tmp_path):
        _write_decisions(3)
        path = _ledger_files(tmp_path / "governance")[0]
        lines = path.read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[1])
        record["reason"] = "被篡改的原因"          # 改内容不改 hash
        lines[1] = json.dumps(record, ensure_ascii=False)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = governance.verify_chain()
        assert result["ok"] is False
        assert result["broken_at"] == 2
        assert result["reason"] == "record_hash_mismatch"
        assert result["expected"] == record["hash"]
        assert result["actual"] and result["actual"] != record["hash"]
        assert result["checked"] == 2, "校验应在断点处停下并如实报告已校验条数"

    def test_deleted_middle_record_is_detected_as_seq_gap(self, tmp_path):
        _write_decisions(3)
        path = _ledger_files(tmp_path / "governance")[0]
        lines = path.read_text(encoding="utf-8").splitlines()
        del lines[1]                              # 删掉第 2 条
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = governance.verify_chain()
        assert result["ok"] is False
        assert result["broken_at"] == 3
        assert result["reason"] == "seq_gap"
        assert result["expected"] == "2" and result["actual"] == "3"

    def test_empty_ledger_is_ok(self):
        result = governance.verify_chain()
        assert result["ok"] is True
        assert result["checked"] == 0
        assert result["files"] == []

    def test_max_records_truncates_but_verifies_recent(self):
        _write_decisions(10)
        result = governance.verify_chain(max_records=4)
        assert result["ok"] is True
        assert result["checked"] == 4
        assert result["truncated"] is True

    def test_json_invalid_line_is_reported(self, tmp_path):
        _write_decisions(2)
        path = _ledger_files(tmp_path / "governance")[0]
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"seq": 3, "broken"')     # 模拟写一半就崩溃
        result = governance.verify_chain()
        assert result["ok"] is False
        assert result["reason"] == "json_invalid"
        assert result["actual"] == "<invalid json line>"


# ═══════════════════════════════════════════════════════════
# 2. 可持久：轮转 / 重启后链与序号连续
# ═══════════════════════════════════════════════════════════


class TestPersistence:
    def test_state_json_tracks_chain_head(self, tmp_path):
        records = _write_decisions(2)
        state = json.loads(
            (tmp_path / "governance" / "state.json").read_text(encoding="utf-8"))
        assert state["seq"] == 2
        assert state["last_hash"] == records[-1]["hash"]
        assert state["file"].startswith("ledger-")

    def test_rotation_keeps_chain_continuous(self, tmp_path, monkeypatch):
        # 用环境变量把单文件上限压到很小，制造同日多次轮转
        monkeypatch.setenv("AUTOMIND_GOVERNANCE_MAX_FILE_BYTES", "3000")
        root = tmp_path / "governance-rot"
        governance.reset_for_tests(root, enabled=True)
        records = _write_decisions(40)
        assert all(r is not None for r in records)

        files = _ledger_files(root)
        assert len(files) > 1, "上限 3000 字节时 40 条记录必须触发轮转"
        assert [r["seq"] for r in records] == list(range(1, 41)), "轮转不能打乱序号"
        assert records[0]["prev"] == ""
        for previous, current in zip(records, records[1:]):
            assert current["prev"] == previous["hash"], "跨文件 prev 必须连续"

        result = governance.verify_chain()
        assert result["ok"] is True, result
        assert result["checked"] == 40
        assert len(result["files"]) == len(files)

    def test_restart_continues_seq_and_chain(self, tmp_path):
        root = tmp_path / "governance-restart"
        governance.reset_for_tests(root, enabled=True)
        first = _write_decisions(3)
        last_hash = first[-1]["hash"]

        # 同一个 root 再 reset = 模拟进程重启（实例状态清空、从磁盘恢复）
        governance.reset_for_tests(root, enabled=True)
        assert governance.ledger_info()["last_seq"] == 3
        more = _write_decisions(2)
        assert [r["seq"] for r in more] == [4, 5], "重启后序号必须接着 3 往后排"
        assert more[0]["prev"] == last_hash, "重启后链必须接上重启前的最后一条"
        assert governance.verify_chain()["ok"] is True

    def test_failed_append_does_not_advance_state(self, tmp_path, monkeypatch):
        root = tmp_path / "governance-fail"
        governance.reset_for_tests(root, enabled=True)
        _write_decisions(1)

        def _boom(self, path, line):
            raise OSError("磁盘满了（模拟）")

        monkeypatch.setattr(governance._Ledger, "_append_line", _boom)
        assert governance.record_decision(
            tool="x", tier="safe", risk=10, decision="allow") is None
        assert governance.ledger_info()["last_seq"] == 1, "写失败不能推进 seq"

        monkeypatch.undo()
        record = governance.record_decision(
            tool="y", tier="safe", risk=10, decision="allow")
        assert record is not None and record["seq"] == 2
        assert governance.verify_chain()["ok"] is True


# ═══════════════════════════════════════════════════════════
# 3. 不泄密：params 脱敏 + 截断
# ═══════════════════════════════════════════════════════════


class TestSanitization:
    def test_sensitive_keys_and_secrets_are_redacted(self, tmp_path):
        secret = "sk-" + "A" * 32
        governance.record_decision(
            tool="http_request", tier="sensitive", risk=50, decision="allow",
            session_id="s1",
            params={
                "api_key": secret,
                "password": "hunter2secret",
                "note": f"token={secret}",
                "url": "https://example.com",
            })
        raw = _ledger_files(tmp_path / "governance")[0].read_text(encoding="utf-8")
        assert secret not in raw, "密钥明文绝不能落盘"
        assert "hunter2secret" not in raw
        assert "***REDACTED***" in raw
        params = json.loads(raw)["params"]
        assert params["api_key"] == "***REDACTED***"
        assert params["password"] == "***REDACTED***"
        assert params["url"] == "https://example.com"

    def test_long_values_are_truncated_and_key_count_capped(self, tmp_path):
        params: dict[str, str] = {"content": "字" * 500}
        params.update({f"k{i}": f"v{i}" for i in range(20)})
        governance.record_decision(
            tool="file_write", tier="sensitive", risk=50, decision="allow",
            params=params)
        raw = json.loads(
            _ledger_files(tmp_path / "governance")[0].read_text(encoding="utf-8"))
        stored = raw["params"]
        assert len(stored) == 12, "最多保留 12 个键"
        assert len(stored["content"]) <= 200, "每个值最多 200 字符"
        assert len(stored["content"]) < 500, "文件内容不能被完整落盘"

    def test_error_and_reason_are_bounded(self, tmp_path):
        governance.record_decision(
            tool="x", tier="safe", risk=10, decision="error",
            reason="因" * 1000)
        governance.record_execution(
            tool="x", tier="safe", success=False, error="错" * 1000)
        records = _read_records(tmp_path / "governance")
        assert len(records[0]["reason"]) <= 500
        assert len(records[1]["error"]) <= 300

    def test_params_not_leaked_by_nested_content(self, tmp_path):
        content = "-----BEGIN RSA PRIVATE KEY-----\n" + "MIIE" * 200
        governance.record_decision(
            tool="file_write", tier="sensitive", risk=50, decision="allow",
            params={"content": content})
        raw = _ledger_files(tmp_path / "governance")[0].read_text(encoding="utf-8")
        assert content not in raw, "文件内容不能被完整落盘"
        record = json.loads(raw)
        assert len(record["params"]["content"]) <= 200, "只允许保留截断后的摘要"


# ═══════════════════════════════════════════════════════════
# 4. fail-open：账本故障绝不影响任务执行
# ═══════════════════════════════════════════════════════════


class TestFailOpen:
    def test_write_failure_returns_none_without_raising(self, monkeypatch):
        def _boom(self, path, line):
            raise RuntimeError("模拟写入故障")

        monkeypatch.setattr(governance._Ledger, "_append_line", _boom)
        assert governance.record_decision(
            tool="x", tier="safe", risk=10, decision="allow") is None
        assert governance.record_execution(
            tool="x", tier="safe", success=True) is None

    def test_unusable_root_does_not_raise(self, tmp_path):
        # 让账本根路径是一个**文件**：mkdir / append 必然失败
        bad = tmp_path / "not-a-dir"
        bad.write_text("x", encoding="utf-8")
        governance.reset_for_tests(bad, enabled=True)
        assert governance.record_decision(
            tool="x", tier="safe", risk=10, decision="allow") is None
        assert governance.ledger_info()["enabled"] is True

    def test_read_failures_degrade_to_empty(self, monkeypatch):
        def _boom(*_args, **_kwargs):
            raise RuntimeError("模拟读取故障")

        monkeypatch.setattr(governance, "_get_ledger", _boom)
        assert governance.tail(10) == []
        summary = governance.summary()
        assert summary["total"] == 0
        assert summary["denied"] == 0
        assert "error" in summary
        verify = governance.verify_chain()
        assert verify["ok"] is False and verify["checked"] == 0
        info = governance.ledger_info()
        assert info["files"] == 0

    def test_disabled_ledger_writes_nothing(self, tmp_path):
        root = tmp_path / "governance-off"
        governance.reset_for_tests(root, enabled=False)
        assert governance.enabled() is False
        assert governance.record_decision(
            tool="x", tier="safe", risk=10, decision="allow") is None
        assert _ledger_files(root) == []


# ═══════════════════════════════════════════════════════════
# 5. tail / summary 过滤与统计
# ═══════════════════════════════════════════════════════════


def _seed_mixed_ledger() -> None:
    governance.record_decision(
        tool="terminal", tier="dangerous", risk=95, decision="deny",
        reason="高危命令", source="direct", session_id="s1")
    governance.record_decision(
        tool="file_read", tier="safe", risk=10, decision="allow",
        reason="只读", source="react", session_id="s1")
    governance.record_decision(
        tool="terminal", tier="dangerous", risk=95, decision="deny_unattended",
        reason="无人值守拒绝", source="workflow", session_id="s2")
    governance.record_execution(
        tool="file_read", tier="safe", success=True, duration_ms=3.5,
        source="react", session_id="s1")
    # 模拟"未配置权限引擎"的裸奔调用：governed=False
    governance.record_decision(
        tool="terminal", tier="unknown", risk=0, decision="allow",
        reason="未配置权限引擎", source="direct", session_id="s3",
        governed=False)


class TestFiltersAndSummary:
    def test_tail_filters_and_order(self):
        _seed_mixed_ledger()
        records = governance.tail(10)
        assert [r["seq"] for r in records] == [1, 2, 3, 4, 5], "tail 必须按 seq 升序"
        assert [r["seq"] for r in governance.tail(2)] == [4, 5], "limit 取最后 N 条"
        assert governance.tail(0) == []
        assert [r["seq"] for r in governance.tail(10, session_id="s1")] == [1, 2, 4]
        assert [r["seq"] for r in governance.tail(10, tool="terminal")] == [1, 3, 5]
        assert [r["seq"] for r in governance.tail(10, decision="deny")] == [1]
        assert [r["seq"] for r in governance.tail(10, kind="execution")] == [4]
        assert [r["seq"] for r in governance.tail(10, source="workflow")] == [3]
        assert set(governance.tail(10)[0]) >= {
            "seq", "ts", "kind", "session", "tool", "tier", "risk", "decision",
            "reason", "source", "governed", "params", "call_id", "duration_ms",
            "success", "error", "prev", "hash"}

    def test_summary_counts(self):
        _seed_mixed_ledger()
        summary = governance.summary()
        assert summary["total"] == 5
        assert summary["by_decision"] == {
            "deny": 1, "allow": 2, "deny_unattended": 1, "executed": 1}
        assert summary["by_tier"]["dangerous"] == 2
        assert summary["by_source"]["direct"] == 2
        assert summary["denied"] == 2
        assert summary["dangerous"] == 2
        assert summary["ungoverned"] == 1
        assert summary["sessions"] == 3
        assert summary["files"] == 1
        assert summary["bytes"] > 0
        assert summary["oldest_ts"] <= summary["newest_ts"]

        only_s1 = governance.summary(session_id="s1")
        assert only_s1["total"] == 3
        assert only_s1["sessions"] == 1

    def test_execution_record_fields(self):
        _seed_mixed_ledger()
        record = governance.tail(10, kind="execution")[0]
        assert record["decision"] == "executed"
        assert record["success"] is True
        assert record["duration_ms"] == 3.5
        assert record["params"] == {}, "执行记录不携带参数"


# ═══════════════════════════════════════════════════════════
# 6. 开关 / 配置 / 来源 ContextVar
# ═══════════════════════════════════════════════════════════


class TestEnabledAndConfigure:
    def test_env_can_hard_disable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTOMIND_GOVERNANCE_AUDIT", "0")
        assert governance.enabled() is False
        governance.reset_for_tests(tmp_path / "off", enabled=True)
        assert governance.enabled() is False, "env=0 是硬关闭，reset 也不能打开"
        assert governance.record_decision(
            tool="x", tier="safe", risk=10, decision="allow") is None

    def test_configure_switches_root_and_writes(self, tmp_path):
        root = tmp_path / "custom-governance"
        governance.configure(root=root, enabled=True)
        assert governance.enabled() is True
        assert Path(governance.ledger_info()["root"]) == root
        record = governance.record_decision(
            tool="x", tier="safe", risk=10, decision="allow")
        assert record is not None and record["seq"] == 1
        assert _ledger_files(root)

    def test_reset_disabled_then_configure_enabled(self, tmp_path):
        governance.reset_for_tests(tmp_path / "toggle", enabled=False)
        assert governance.enabled() is False
        governance.configure(enabled=True)
        assert governance.enabled() is True
        assert governance.record_decision(
            tool="x", tier="safe", risk=10, decision="allow") is not None

    def test_ledger_info_contract(self):
        _write_decisions(2)
        info = governance.ledger_info()
        for key in ("root", "files", "bytes", "enabled", "last_seq"):
            assert key in info, f"ledger_info 缺字段 {key}"
        assert info["enabled"] is True
        assert info["files"] == 1
        assert info["last_seq"] == 2
        assert info["bytes"] > 0


class TestSourceContext:
    def test_bind_and_reset(self):
        assert governance.current_source() == "direct"
        token = governance.bind_source("workflow")
        assert governance.current_source() == "workflow"
        nested = governance.bind_source("skill")
        assert governance.current_source() == "skill"
        governance.reset_source(nested)
        assert governance.current_source() == "workflow"
        governance.reset_source(token)
        assert governance.current_source() == "direct"

    def test_concurrent_tasks_do_not_share_source(self):
        async def worker(source: str) -> str:
            token = governance.bind_source(source)
            try:
                await asyncio.sleep(0.01)      # 让三个任务交错执行
                return governance.current_source()
            finally:
                governance.reset_source(token)

        async def run_all() -> list[str]:
            return list(await asyncio.gather(
                *(worker(s) for s in ("react", "workflow", "plan"))))

        results = asyncio.run(run_all())
        assert results == ["react", "workflow", "plan"]
        assert governance.current_source() == "direct"

    def test_default_source_is_direct(self):
        """不传 source 时默认 direct（执行器会显式传当前来源，不依赖默认值）。"""
        record = governance.record_decision(
            tool="x", tier="safe", risk=10, decision="allow")
        assert record is not None and record["source"] == "direct"

    def test_reset_source_tolerates_bad_token(self):
        # Token 只能 reset 一次；重复 reset 不能抛（执行器 finally 里可能重复调用）
        token = governance.bind_source("react")
        governance.reset_source(token)
        governance.reset_source(token)          # 不抛即通过
        assert governance.current_source() == "direct"
