"""数据目录解析测试 — automind/core/paths.py（桌面封装的路径基座）。"""

from __future__ import annotations

import sys
from pathlib import Path

from automind.core import paths


class TestDataDir:
    def test_default_is_cwd_dot_automind(self, monkeypatch):
        """开发/pip 模式：保持既有 cwd/.automind 行为（零迁移成本）。"""
        monkeypatch.delenv("AUTOMIND_DATA_DIR", raising=False)
        assert paths.data_dir() == Path(".automind")
        assert paths.config_file() == Path(".automind_config.json")

    def test_env_override_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUTOMIND_DATA_DIR", str(tmp_path / "d"))
        assert paths.data_dir() == tmp_path / "d"
        # 显式数据目录时配置文件收敛进目录内
        assert paths.config_file() == tmp_path / "d" / "config.json"
        assert paths.db_file() == tmp_path / "d" / "automind.db"
        assert paths.kb_dir() == tmp_path / "d" / "kb"
        assert paths.skills_dir() == tmp_path / "d" / "skills"

    def test_frozen_uses_platform_dir(self, monkeypatch):
        monkeypatch.delenv("AUTOMIND_DATA_DIR", raising=False)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        d = paths.data_dir()
        assert d.name in ("AutoMind", "automind")
        assert d != Path(".automind")
        # 冻结模式配置文件也在数据目录内
        assert paths.config_file() == d / "config.json"

    def test_windows_frozen_under_appdata(self, monkeypatch, tmp_path):
        if sys.platform != "win32":
            return
        monkeypatch.delenv("AUTOMIND_DATA_DIR", raising=False)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setenv("APPDATA", str(tmp_path))
        assert paths.data_dir() == tmp_path / "AutoMind"

    def test_describe_shape(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUTOMIND_DATA_DIR", str(tmp_path))
        info = paths.describe()
        assert info["env_override"] is True
        assert Path(info["data_dir"]) == tmp_path.resolve()


class TestWiring:
    """各存储模块确实经 paths 取默认路径（AUTOMIND_DATA_DIR 生效）。"""

    def test_database_default(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUTOMIND_DATA_DIR", str(tmp_path))
        from automind.core.db import Database
        db = Database()
        try:
            assert db.path == tmp_path / "automind.db"
        finally:
            db.close()

    def test_kb_default(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUTOMIND_DATA_DIR", str(tmp_path))
        from automind.rag.kb import KnowledgeStore
        store = KnowledgeStore()
        assert store.root == tmp_path / "kb"

    def test_store_defaults(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUTOMIND_DATA_DIR", str(tmp_path))
        from automind.server_store import Store
        s = Store()
        assert s.config_file == tmp_path / "config.json"
        assert s.chats_dir == tmp_path / "chats"
