"""v1.11.5 目录选择器：本机可浏览整块磁盘、远程仍锚定、系统目录始终拒绝。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import automind.server as srv


@pytest.fixture
def local():
    with TestClient(srv.app, client=("127.0.0.1", 5555)) as c:
        yield c


def _outside_home() -> Path | None:
    """一个不在主目录 / 项目 / 工作区锚点里的真实目录（找不到就跳过）。"""
    roots = srv._fs_roots()
    bases = [Path(d) for d in srv._list_drives()] if os.name == "nt" else [Path("/")]
    for base in bases:
        try:
            for p in base.iterdir():
                if (p.is_dir() and not p.name.startswith(("$", "."))
                        and not srv._fs_path_denied(p) and not srv._fs_within_roots(p, roots)):
                    return p
        except OSError:
            continue
    return None


def test_local_can_browse_outside_anchors(local):
    target = _outside_home()
    if target is None:
        pytest.skip("找不到锚点之外的目录")
    r = local.get("/api/fs/list", params={"path": str(target)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["full_disk"] is True


def test_full_disk_can_be_disabled(local, monkeypatch):
    target = _outside_home()
    if target is None:
        pytest.skip("找不到锚点之外的目录")
    monkeypatch.setenv("AUTOMIND_FS_FULL_DISK", "0")
    assert local.get("/api/fs/list", params={"path": str(target)}).status_code == 403


def test_remote_with_token_stays_anchored(monkeypatch):
    target = _outside_home()
    if target is None:
        pytest.skip("找不到锚点之外的目录")
    monkeypatch.setattr(srv, "_AUTH_TOKEN", "tok")
    with TestClient(srv.app, client=("192.168.1.50", 5555)) as c:
        r = c.get("/api/fs/list", params={"path": str(target), "token": "tok"})
    assert r.status_code == 403


def test_system_dirs_denied_in_full_disk_mode(local):
    target = os.environ.get("SYSTEMROOT", r"C:\Windows") if os.name == "nt" else "/usr/lib"
    if not Path(target).is_dir():
        pytest.skip("系统目录不存在")
    assert local.get("/api/fs/list", params={"path": target}).status_code == 403
    sub = Path(target) / ("System32" if os.name == "nt" else "python3")
    assert srv._fs_path_denied(sub)


def test_places_and_files(local, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    body = local.get("/api/fs/list", params={"path": str(tmp_path)}).json()
    assert body["dirs"] == ["sub"] and body["files"] == []
    keys = {p["key"] for p in body["places"]}
    assert "home" in keys
    body = local.get("/api/fs/list", params={"path": str(tmp_path), "files": "true"}).json()
    assert body["files"] == ["a.txt"]


def test_mkdir(local, tmp_path):
    r = local.post("/api/fs/mkdir", json={"path": str(tmp_path), "name": "新项目"})
    assert r.status_code == 200 and (tmp_path / "新项目").is_dir()
    assert local.post("/api/fs/mkdir", json={"path": str(tmp_path), "name": "新项目"}).status_code == 409
    for bad in ("", "..", "a/b", "a\\b", "x:y", "trail."):
        assert local.post("/api/fs/mkdir", json={"path": str(tmp_path), "name": bad}).status_code == 400, bad


def test_mkdir_remote_without_token_denied(tmp_path):
    with TestClient(srv.app, client=("192.168.1.50", 5555)) as c:
        r = c.post("/api/fs/mkdir", json={"path": str(tmp_path), "name": "x"})
    assert r.status_code == 403 and not (tmp_path / "x").exists()
