"""团队任务默认存储的一次性 JSON→SQLite 迁移（v1.11.18）。

为什么单独测：AI 工具 ``team_task`` 走的是 ``core.team`` 的**默认**存储。
若迁移只在 server 的 loader 里做，Agent 先写入的新任务会在团队看板第一次
加载时被旧 ``team_tasks.json`` 的整表替换覆盖 —— 表现为"AI 说已认领，看板上
却没有"。把迁移下沉到默认读写层后，这里钉住三件事：旧数据会被导入、
迁移只做一次、迁移后 AI 写入的任务不会被再次覆盖。
"""

from __future__ import annotations

import json

from automind.core import team
from automind.core.db import Database


def _patch_default_store(monkeypatch, tmp_path):
    """把默认存储的 DB 与 legacy 文件都指到 tmp_path（不碰真实数据目录）。"""
    db = Database(tmp_path / "automind.db")
    legacy = tmp_path / "team_tasks.json"

    monkeypatch.setattr("automind.core.db.get_db", lambda: db)
    monkeypatch.setattr(
        "automind.core.paths.legacy_file",
        lambda name: legacy if name == "team_tasks.json" else tmp_path / name)
    return db, legacy


def test_legacy_json_is_migrated_into_sqlite(monkeypatch, tmp_path):
    _db, legacy = _patch_default_store(monkeypatch, tmp_path)
    legacy.write_text(json.dumps([
        {"id": "old1", "title": "旧任务", "status": "todo",
         "assignee": "", "created": "2026-10-01 10:00"},
    ], ensure_ascii=False), encoding="utf-8")

    tasks = team.TeamStore().load()
    assert [t["title"] for t in tasks] == ["旧任务"]
    # 旧字段缺失时由 normalize_task 补齐，旧 id 原样保留（前后端兼容契约）
    assert tasks[0]["id"] == "old1"
    assert tasks[0]["priority"] == team.DEFAULT_PRIORITY


def test_migration_happens_only_once_and_does_not_clobber_ai_writes(monkeypatch, tmp_path):
    _db, legacy = _patch_default_store(monkeypatch, tmp_path)
    legacy.write_text(json.dumps([
        {"id": "old1", "title": "旧任务", "created": "2026-10-01 10:00"},
    ], ensure_ascii=False), encoding="utf-8")

    store = team.TeamStore()
    assert len(store.load()) == 1
    # Agent（team_task 工具）通过同一个默认存储创建新任务
    created = store.create(title="AI 认领的任务", actor="会话 s1")
    assert created["id"]
    # 再次加载不得重新导入旧 JSON（否则会把新任务整表替换掉）
    titles = [t["title"] for t in store.load()]
    assert "AI 认领的任务" in titles
    assert titles.count("旧任务") == 1


def test_no_legacy_file_is_not_an_error(monkeypatch, tmp_path):
    _db, _legacy = _patch_default_store(monkeypatch, tmp_path)
    assert team.TeamStore().load() == []
