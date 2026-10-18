"""v1.11.2：输入框 @ 引用（文件 / 目录 / 对话）与 / 指令点名的工具。

盯两件事：**真的生效**（内容读出来并交给模型、点名的工具必下发），以及
**不生效时如实回执**（越界、敏感文件、二进制、不存在、超量都要说清）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import automind.server as srv


@pytest.fixture
def proj(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('hello app')\n", encoding="utf-8")
    (root / "src" / "pkg" / "util.py").write_text("X = 1\n", encoding="utf-8")
    (root / "README.md").write_text("# Demo\n", encoding="utf-8")
    (root / "logo.bin").write_bytes(b"\x89PNG\0\0\0binary")
    (root / "node_modules" / "dep").mkdir(parents=True)
    (root / "node_modules" / "dep" / "app.py").write_text("no", encoding="utf-8")
    (root / ".automind_config.json").write_text('{"api_keys": {"x": "secret"}}', encoding="utf-8")
    (tmp_path / "outside.txt").write_text("outside", encoding="utf-8")
    monkeypatch.setattr(srv, "_editor_root", lambda: root.resolve())
    return root


# ── 文件查找 ──────────────────────────────────────────────


def test_files_search_ranks_name_prefix_first_and_skips_deps(proj):
    items = srv._files_search_sync(proj.resolve(), "app", 20, True)
    paths = [i["path"] for i in items]
    assert paths[0] == "src/app.py"
    assert not any(p.startswith("node_modules") for p in paths), "依赖目录不能出现在候选里"


def test_files_search_endpoint(proj):
    with TestClient(srv.app, client=("127.0.0.1", 51000)) as c:
        r = c.get("/api/files/search", params={"q": "util"})
    assert r.status_code == 200
    assert r.json()["items"][0]["path"] == "src/pkg/util.py"


def test_files_search_subsequence(proj):
    paths = [i["path"] for i in srv._files_search_sync(proj.resolve(), "spu", 20, False)]
    assert "src/pkg/util.py" in paths


# ── @ 引用注入 ─────────────────────────────────────────────


def _apply(task, mentions=None, **extra):
    data = {"task": task, "overrides": {"mentions": mentions or []}, **extra}
    rec: dict = {}
    out, key = srv._apply_mentions(task, data, rec)
    return out, key, rec.get("mentions", {})


def test_file_mention_is_injected(proj):
    out, key, rec = _apply("看看这个", [{"type": "file", "path": "src/app.py"}])
    assert "print('hello app')" in out and "引用文件：src/app.py" in out
    assert key.startswith("\n[mentions:"), "带引用的消息不能命中只看原文的语义缓存"
    assert rec["applied"] == [{"type": "file", "name": "src/app.py"}]


def test_dir_mention_lists_tree(proj):
    out, _, rec = _apply("x", [{"type": "dir", "path": "src"}])
    assert "app.py" in out and "pkg/" in out and rec["applied"]


@pytest.mark.parametrize("path,why", [
    ("../outside.txt", "超出项目"),
    (".automind_config.json", ""),           # 敏感文件：原因文案由 sensitive 模块给出
    ("logo.bin", "二进制"),
    ("nope.py", "不存在"),
])
def test_bad_mentions_are_dropped_with_reason(proj, path, why):
    out, key, rec = _apply("x", [{"type": "file", "path": path}])
    assert out == "x" and key == ""
    assert rec["dropped"] and rec["dropped"][0]["name"] == path
    assert why in rec["dropped"][0]["reason"]
    assert "secret" not in out


def test_typed_at_path_is_picked_up_but_people_are_not(proj):
    out, _, rec = _apply("请检查 @src/app.py ，然后告诉 @张三")
    assert "hello app" in out
    assert [a["name"] for a in rec["applied"]] == ["src/app.py"]


def test_conversation_mention_uses_transcript(proj, monkeypatch):
    monkeypatch.setattr(srv, "_conv_store", lambda: SimpleNamespace(
        get=lambda _sid: {"title": "上周的讨论", "summary": "定了用 PostgreSQL"}))
    out, _, rec = _apply("接着那个对话做", [{
        "type": "conversation", "sid": "s_x_cabc", "title": "旧标题",
        "transcript": [{"role": "user", "content": "数据库选哪个"},
                       {"role": "assistant", "content": "建议 PostgreSQL"}]}])
    assert "引用对话：上周的讨论" in out
    assert "用户：数据库选哪个" in out and "助手：建议 PostgreSQL" in out
    assert "要点：定了用 PostgreSQL" in out
    assert rec["applied"][0]["type"] == "conversation"


def test_empty_conversation_is_reported(proj, monkeypatch):
    monkeypatch.setattr(srv, "_conv_store", lambda: SimpleNamespace(get=lambda _sid: {}))
    monkeypatch.setattr(srv, "_get_session_history", lambda _sid: [])
    out, _, rec = _apply("x", [{"type": "conversation", "sid": "s_empty", "title": "空"}])
    assert out == "x" and "没有内容" in rec["dropped"][0]["reason"]


def test_mention_count_limit(proj):
    many = [{"type": "file", "path": "README.md"}] * (srv._MENTION_MAX + 2)
    _, _, rec = _apply("x", many)
    assert len(rec["applied"]) == srv._MENTION_MAX
    assert any("最多引用" in d["reason"] for d in rec["dropped"])


def test_no_mentions_is_noop(proj):
    assert _apply("普通消息 a@b.com") == ("普通消息 a@b.com", "", {})


# ── / 指令点名的工具 ──────────────────────────────────────


class _Tool:
    def __init__(self, name, desc="d"):
        self.name, self.description = name, desc


def test_tool_refs_resolve_and_force_send(monkeypatch):
    names = ["file_read", "mcp__github__create_issue", "plugin_weather"]
    reg = SimpleNamespace(list_names=lambda: names,
                          get=lambda n: _Tool(n, f"{n} 的说明"))
    monkeypatch.setattr(srv, "get_agent", lambda: SimpleNamespace(tool_registry=reg))
    rec: dict = {}
    task, prefer = srv._apply_references(
        "建个 issue", {"overrides": {"tools": ["github.create_issue", "weather", "nope"]}}, rec)
    assert prefer == ["mcp__github__create_issue", "plugin_weather"]
    assert "【指定工具】" in task and "mcp__github__create_issue" in task
    assert rec["tools"]["applied"][0] == {"name": "github.create_issue",
                                          "tool": "mcp__github__create_issue"}
    assert rec["tools"]["dropped"][0]["name"] == "nope"
