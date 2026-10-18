"""v1.7.7 P0-2：隔离工作区 —— 产物可见并可取回、磁盘有上限、闲置副本主动清理。"""

from __future__ import annotations

import os
import time
from pathlib import Path

from automind.core import workspace


class Ex:
    isolate_workspace = True
    workspace_keep = 8
    workspace_max_mb = 500
    workspace_large_file_mb = 20
    workspace_ttl_hours = 72.0


class _Journal:
    def __init__(self):
        self.rec = []

    def record(self, path, before, tool):
        self.rec.append((Path(path).name, before, tool))


def _project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    (p / "a.py").write_text("print('a')\n", encoding="utf-8")
    (p / "b.py").write_text("print('b')\n", encoding="utf-8")
    return p


def test_changes_are_relative_to_baseline(tmp_path):
    proj = _project(tmp_path)
    plan = workspace.prepare("s1", proj, Ex())
    wd = Path(plan.path)
    (wd / "a.py").write_text("print('A!')\n", encoding="utf-8")
    (wd / "new.txt").write_text("hi", encoding="utf-8")
    (wd / "b.py").unlink()
    ch = workspace.changes("s1", proj)
    assert ch["added"] == ["new.txt"] and ch["modified"] == ["a.py"]
    assert ch["deleted"] == ["b.py"] and ch["count"] == 3


def test_apply_copies_back_records_journal_and_keeps_deletes(tmp_path):
    proj = _project(tmp_path)
    wd = Path(workspace.prepare("s1", proj, Ex()).path)
    (wd / "a.py").write_text("print('A!')\n", encoding="utf-8")
    (wd / "new.txt").write_text("hi", encoding="utf-8")
    (wd / "b.py").unlink()
    j = _Journal()
    res = workspace.apply_to_project("s1", proj, journal=j)
    assert sorted(res["applied"]) == ["a.py", "new.txt"] and res["conflicts"] == []
    assert (proj / "a.py").read_text(encoding="utf-8") == "print('A!')\n"
    assert (proj / "b.py").exists()                     # 删除不外溢到项目
    assert res["not_deleted"] == ["b.py"]
    assert ("a.py", "print('a')\n", "workspace_apply") in j.rec   # 可撤销
    assert ("new.txt", None, "workspace_apply") in j.rec


def test_apply_overwrites_same_path_and_backs_up(tmp_path):
    """v1.11.6：应用 = 按相对路径覆盖回项目根；任务期间项目里也改过的照样覆盖，但先备份。"""
    proj = _project(tmp_path)
    wd = Path(workspace.prepare("s1", proj, Ex()).path)
    (wd / "a.py").write_text("agent\n", encoding="utf-8")
    time.sleep(0.02)
    (proj / "a.py").write_text("user edited meanwhile\n", encoding="utf-8")
    plan = workspace.apply_plan("s1", proj)
    assert plan["project"] == str(proj.resolve())
    assert plan["files"] == [{"path": "a.py", "target": str(proj.resolve() / "a.py"),
                              "action": "overwrite", "changed_in_project": True}]
    res = workspace.apply_to_project("s1", proj)
    assert res["applied"] == ["a.py"] and res["overwritten_changed"] == ["a.py"]
    assert (proj / "a.py").read_text(encoding="utf-8") == "agent\n"
    backup = Path(res["backup_dir"]) / "a.py"
    assert backup.read_text(encoding="utf-8") == "user edited meanwhile\n"


def test_apply_can_still_skip_conflicts(tmp_path):
    proj = _project(tmp_path)
    wd = Path(workspace.prepare("s1", proj, Ex()).path)
    (wd / "a.py").write_text("agent\n", encoding="utf-8")
    time.sleep(0.02)
    (proj / "a.py").write_text("user edited meanwhile\n", encoding="utf-8")
    res = workspace.apply_to_project("s1", proj, skip_conflicts=True)
    assert res["conflicts"] == ["a.py"] and res["applied"] == []
    assert (proj / "a.py").read_text(encoding="utf-8") == "user edited meanwhile\n"


def test_apply_nested_paths_build_outputs_and_binaries(tmp_path):
    """同目录结构写回：新子目录自动建；dist/ 产物与 .zip 也算交付物；二进制覆盖前有备份。"""
    proj = _project(tmp_path)
    (proj / "assets").mkdir()
    (proj / "assets" / "logo.png").write_bytes(b"PNG-old")
    wd = Path(workspace.prepare("s1", proj, Ex()).path)
    (wd / "src" / "pkg").mkdir(parents=True)
    (wd / "src" / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (wd / "dist").mkdir()
    (wd / "dist" / "app.zip").write_bytes(b"PK")
    (wd / "assets" / "logo.png").write_bytes(b"PNG-new")
    (wd / "node_modules").mkdir()
    (wd / "node_modules" / "junk.js").write_text("x", encoding="utf-8")
    res = workspace.apply_to_project("s1", proj)
    assert sorted(res["applied"]) == ["assets/logo.png", "dist/app.zip", "src/pkg/mod.py"]
    assert (proj / "src" / "pkg" / "mod.py").read_text(encoding="utf-8") == "x = 1\n"
    assert (proj / "dist" / "app.zip").read_bytes() == b"PK"
    assert (proj / "assets" / "logo.png").read_bytes() == b"PNG-new"
    assert (Path(res["backup_dir"]) / "assets" / "logo.png").read_bytes() == b"PNG-old"
    assert not (proj / "node_modules").exists()


def test_apply_goes_back_to_source_root(tmp_path):
    """副本从哪个项目复制出来就回到哪个项目，与之后"当前项目"指向哪里无关。"""
    proj = _project(tmp_path)
    wd = Path(workspace.prepare("s1", proj, Ex()).path)
    (wd / "new.txt").write_text("hi", encoding="utf-8")
    m = workspace._read_manifest(wd)
    assert Path(m["source"]) == proj.resolve()
    assert workspace.source_root("s1", proj) == proj.resolve()


def test_absolute_project_paths_are_remapped_into_copy(tmp_path):
    proj = _project(tmp_path).resolve()
    ws = Path(workspace.prepare("s1", proj, Ex()).path).resolve()
    remap = workspace.remap_into_workspace
    assert remap(proj / "a.py", proj, ws) == ws / "a.py"            # 副本里有 → 改写
    assert remap(proj / "new" / "x.py", proj, ws) == ws / "new" / "x.py"  # 将新建 → 改写
    assert remap(ws / "a.py", proj, ws) == ws / "a.py"               # 已在副本里
    assert remap(proj / ".automind" / "x", proj, ws) == proj / ".automind" / "x"
    (proj / "big.bin").write_bytes(b"0")                             # 项目有、副本没有 → 原样
    assert remap(proj / "big.bin", proj, ws) == proj / "big.bin"
    outside = tmp_path / "elsewhere.txt"
    assert remap(outside, proj, ws) == outside


def test_root_guard_writes_absolute_path_into_copy(tmp_path):
    from automind.core import session_ctx
    from automind.tools.file_editor import _RootGuard
    proj = _project(tmp_path).resolve()
    ws = Path(workspace.prepare("s1", proj, Ex()).path).resolve()
    guard = _RootGuard(proj)
    with session_ctx.bind_session("s1", run_id="r", workspace=str(ws)):
        assert guard.resolve(str(proj / "a.py")) == ws / "a.py"
        assert guard.resolve("src/b.py") == ws / "src" / "b.py"
    assert guard.resolve(str(proj / "a.py")) == proj / "a.py"


def test_byte_cap_degrades_honestly(tmp_path):
    proj = _project(tmp_path)
    (proj / "data.bin").write_bytes(os.urandom(2 * 1024 * 1024))

    class Small(Ex):
        workspace_max_mb = 1
        workspace_large_file_mb = 0
    plan = workspace.prepare("s1", proj, Small())
    assert not plan.isolated and "上限" in plan.reason
    assert not (workspace.root_dir(proj) / "s1").exists()


def test_large_files_are_not_copied(tmp_path):
    proj = _project(tmp_path)
    (proj / "big.bin").write_bytes(os.urandom(2 * 1024 * 1024))

    class Tight(Ex):
        workspace_large_file_mb = 1
    plan = workspace.prepare("s1", proj, Tight())
    assert plan.isolated and "大文件" in plan.reason
    assert not (Path(plan.path) / "big.bin").exists()
    # 没复制的大文件不能被当成"Agent 删掉了"
    assert workspace.changes("s1", proj)["deleted"] == []


def test_cleanup_ttl_and_busy(tmp_path):
    proj = _project(tmp_path)
    workspace.prepare("old", proj, Ex())
    workspace.prepare("busy", proj, Ex())
    workspace.prepare("fresh", proj, Ex())
    base = workspace.root_dir(proj)
    for name in ("old", "busy"):
        m = workspace._read_manifest(base / name)
        m["last_used"] = time.time() - 100 * 3600
        workspace._write_manifest(base / name, m)
    removed = workspace.cleanup(proj, ttl_hours=72, keep=8, busy={"busy"})
    assert removed == ["old"]
    assert (base / "busy").exists() and (base / "fresh").exists()


def test_list_and_remove(tmp_path):
    proj = _project(tmp_path)
    workspace.prepare("s1", proj, Ex())
    items = workspace.list_copies(proj)
    assert items[0]["session"] == "s1" and items[0]["bytes"] > 0
    assert workspace.remove("s1", proj) and workspace.list_copies(proj) == []


def test_applied_files_no_longer_pending(tmp_path):
    proj = _project(tmp_path)
    wd = Path(workspace.prepare("s1", proj, Ex()).path)
    (wd / "a.py").write_text("changed\n", encoding="utf-8")
    workspace.apply_to_project("s1", proj)
    assert workspace.changes("s1", proj)["count"] == 0
