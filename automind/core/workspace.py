"""会话级工作区准备 —— 把"目录隔离"做成可选的一档，而不是默认行为。

背景（v1.6.3 的遗留缺口）：会话隔离已经解决了**执行态**串扰（每个会话一个
轻量克隆：交互模式、上下文、token 计数互不污染），但所有克隆仍共享同一个
项目目录。两个会话并发改同一个工作区时，文件写入仍会竞态覆盖 ——
而回滚记录是**按文件**记的，A 的回滚会把 B 的成果一起"恢复"掉，全程静默。

本模块提供可选的**目录级隔离**：每个会话在自己的工作副本里干活，从根上
消除跨会话覆盖。做成可选而非默认，是因为它有三个真实代价：

  · 磁盘：每个会话一份工作副本（大仓库很贵）；
  · 初始化：首次任务要为该会话拷一次文件（大仓库很慢）；
  · 语义：会话产物落在副本里，用户若要的是"改我本地的项目"，就得把改动
    合并回去 —— 这不是所有场景都想要的。

所以默认仍是"共享目录 + 按路径加锁 + 冲突检出"（`tools/write_guard.py`），
需要强隔离时再打开。三档强度由弱到强：

    1. 共享目录 + 按路径串行化 + 冲突提示（默认）
    2. 共享目录 + 冲突即拒绝（`write_conflict_policy = "block"`）
    3. 会话独立工作副本（`isolate_workspace = true`）
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from automind.core.logging import get_logger

logger = get_logger("automind.core.workspace")

#: 复制时跳过的目录/后缀（版本库、虚拟环境、缓存、构建产物 —— 大且可重建）
SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
    ".mypy_cache", ".ruff_cache", ".pytest_cache", "dist", "build", ".next",
    ".automind", ".idea", ".vscode", "target", ".tox", ".eggs", ".cache",
}
SKIP_SUFFIX = (".pyc", ".pyo", ".pyd", ".so", ".dll", ".exe", ".zip", ".7z",
               ".tar", ".gz", ".whl")

#: 单次复制文件数上限 —— 超过就放弃隔离并如实降级（宁可不隔离，不要卡住）
MAX_FILES = 4000

#: 会话工作副本中用户改动被收进这里的子目录名，避免与项目文件混淆
EXPORT_DIRNAME = "_session_output"

#: 副本里的基线清单（v1.7.7）：复制那一刻每个文件的大小与修改时间。
#: "Agent 改了什么"以它为准，而不是去和项目目录比 —— 项目目录在任务期间
#: 可能被用户自己改了，拿它比会把用户的改动算到 Agent 头上。
MANIFEST_NAME = ".automind_ws.json"

#: 应用回项目前，被覆盖的项目文件先备份到副本里的这个目录（v1.11.6，二进制也能还原）
BACKUP_DIRNAME = "_apply_backup"

#: 计算"产物"时跳过的目录（v1.11.6）。比复制时的 SKIP_DIRS 窄：dist / build / target
#: 这类构建产物恰恰常是要交付的东西，复制时不带它们是为了省空间，但 Agent 在副本里
#: 新生成的必须能应用回去；依赖目录、缓存、版本库仍然跳过（又大又可重建）。
PRODUCT_SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
    ".mypy_cache", ".ruff_cache", ".pytest_cache", ".automind", ".tox", ".eggs",
    ".cache", ".idea", EXPORT_DIRNAME, BACKUP_DIRNAME,
}
PRODUCT_SKIP_SUFFIX = (".pyc", ".pyo")

#: 默认上限（可由 ExecutionConfig 覆盖）
DEFAULT_MAX_MB = 500
DEFAULT_LARGE_FILE_MB = 20
DEFAULT_TTL_HOURS = 72.0


def _ignored_parts(name: str) -> bool:
    return name in SKIP_DIRS or name.endswith(SKIP_SUFFIX)


def _product_ignored(rel: str) -> bool:
    parts = Path(rel).parts
    return (any(p in PRODUCT_SKIP_DIRS for p in parts[:-1])
            or rel.endswith(PRODUCT_SKIP_SUFFIX))


def _under(child: Path, parent: Path) -> bool:
    """child 是否在 parent 之内（严格包含判断，避免前缀碰撞）。

    Windows 上 ``Path`` 的比较是大小写不敏感的（``PureWindowsPath``），
    因此这里直接用 ``==`` / ``in parents``，不做字符串前缀比较。
    """
    try:
        c = child.resolve()
        p = parent.resolve()
    except Exception:
        return False
    return c == p or p in c.parents


def remap_into_workspace(path: Path, project_root: Path | None, ws: Path) -> Path:
    """隔离模式下，把指向**项目根**的绝对路径改写到会话副本里的同一相对位置（v1.11.6）。

    此前只有相对路径会落进副本；模型写 ``C:/proj/src/a.py`` 这种绝对路径时直接改了
    项目本身 —— 既绕过了隔离，这个文件也不会出现在副本快照里，「应用到项目」时就少了它。

    规则：项目根之内、但不在副本 / ``.automind`` 之内的路径，若副本里有对应项、或者
    项目里也还没有它（即将新建），就改写到 ``<副本>/<相对路径>``。项目里有、副本里没有的
    （复制时跳过的大文件、依赖目录）保持原样，读得到原文件。
    """
    if project_root is None:
        return path
    if path == ws or ws in path.parents:
        return path
    if not (path == project_root or project_root in path.parents):
        return path
    rel = path.relative_to(project_root)
    if rel.parts and rel.parts[0] == ".automind":
        return path
    mapped = ws / rel
    if mapped.exists() or not path.exists():
        return mapped
    return path


def root_dir(project_root: str | Path | None = None) -> Path:
    """会话工作副本的根目录 —— **始终落在项目目录之内**。

    放在 ``<project>/.automind/workspaces/`` 里是有意为之：

      · ``.automind/`` 在 ``.gitignore`` 内，不会污染你的仓库，也不会被下一次
        复制带上（复制时跳过 ``.automind``）；
      · 文件工具的路径防护（``_RootGuard``）是"限定在 project_root 之内"，
        副本落在项目内才能与那条安全边界一致 —— 否则会出现"隔离目录里的写入
        被自己的越界防护拒绝"这种自相矛盾的状态（曾真实发生）；
      · 用户要取产物时就在项目目录里，不必去翻系统数据目录。

    环境变量 ``AUTOMIND_WORKSPACE_DIR`` 可覆盖（用于把副本放到另一块盘），
    但**若它落在项目之外则忽略并回退** —— 见上面的安全边界理由，并在日志里
    说明原因，不静默改变行为。
    """
    root = Path(project_root or ".").resolve()
    inner = root / ".automind" / "workspaces"
    env = os.environ.get("AUTOMIND_WORKSPACE_DIR", "").strip()
    if env:
        cand = Path(env).expanduser()
        try:
            cand_r = cand.resolve()
        except Exception:
            cand_r = None
        if cand_r is not None and (cand_r == root or root in cand_r.parents):
            return cand_r
        logger.warning(
            "workspace_dir_outside_project_ignored", configured=str(cand),
            project=str(root),
            note="隔离目录必须在项目目录之内（否则写入会被路径防护拒绝）；已回退到 <project>/.automind/workspaces")
    return inner


def enabled(ex: object | None = None) -> bool:
    """是否开启目录级隔离（环境变量 ``AUTOMIND_ISOLATE_WORKSPACE`` 优先）。"""
    env = os.environ.get("AUTOMIND_ISOLATE_WORKSPACE")
    if env is not None:
        return env.strip().lower() not in ("0", "false", "off", "no")
    return bool(getattr(ex, "isolate_workspace", False))


def _last_used(d: Path) -> float:
    """副本最近一次被使用的时间（清单里记的；没有清单退回目录 mtime）。"""
    try:
        m = json.loads((d / MANIFEST_NAME).read_text(encoding="utf-8"))
        return float(m.get("last_used") or m.get("created") or d.stat().st_mtime)
    except Exception:
        try:
            return d.stat().st_mtime
        except OSError:
            return 0.0


def _prune_old(keep: int, project_root: str | Path,
               busy: set[str] | frozenset[str] = frozenset()) -> None:
    """淘汰旧的会话目录（保留最近使用的 keep 个；正在跑任务的不动）。"""
    try:
        base = root_dir(project_root)
        if not base.is_dir():
            return
        dirs = [d for d in base.iterdir() if d.is_dir() and d.name not in busy]
        if len(dirs) <= keep:
            return
        dirs.sort(key=_last_used, reverse=True)
        for d in dirs[keep:]:
            shutil.rmtree(d, ignore_errors=True)
            logger.info("session_workspace_evicted", path=str(d))
    except Exception as e:                       # pragma: no cover - 防御性
        logger.warning("session_workspace_prune_failed", error=str(e))


class WorkspacePlan:
    """一次工作区准备的结果（含降级原因，绝不假装隔离成功）。"""

    def __init__(self, path: str, isolated: bool, created: bool,
                 reason: str = "", files: int = 0) -> None:
        self.path = path
        self.isolated = isolated
        self.created = created
        self.reason = reason
        self.files = files

    def as_dict(self) -> dict[str, object]:
        return {"path": self.path, "isolated": self.isolated,
                "created": self.created, "reason": self.reason,
                "files": self.files}


def _scan(root: Path, target: Path, base: Path, large_bytes: int
          ) -> tuple[list[Path], list[str], int]:
    """列出要复制的文件（及跳过的大文件），不动磁盘。"""
    files: list[Path] = []
    skipped: list[str] = []
    total = 0
    for src in root.rglob("*"):
        rel = src.relative_to(root)
        if any(_ignored_parts(part) for part in rel.parts):
            continue
        # 工作副本的根目录**可能落在项目目录之内**（例如被配置成了
        # <project>/.cache/workspaces）。这时若不排除，复制会把自己
        # 一层层拷进自己里面，直到 Windows 报 WinError 206。
        if _under(src, target) or _under(src, base):
            continue
        if not src.is_file():
            continue
        try:
            size = src.stat().st_size
        except OSError:
            continue
        if large_bytes and size > large_bytes:
            skipped.append(str(rel).replace("\\", "/"))
            continue
        files.append(src)
        total += size
        if len(files) > MAX_FILES:
            break
    return files, skipped, total


def _write_manifest(target: Path, data: dict[str, Any]) -> None:
    try:
        (target / MANIFEST_NAME).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:                        # pragma: no cover - 旁路
        logger.warning("workspace_manifest_write_failed", error=str(e))


def _read_manifest(target: Path) -> dict[str, Any]:
    try:
        return json.loads((target / MANIFEST_NAME).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _touch(target: Path) -> None:
    m = _read_manifest(target)
    if m:
        m["last_used"] = time.time()
        _write_manifest(target, m)


def prepare(session_id: str, project_root: str | Path,
            ex: object | None = None) -> WorkspacePlan:
    """为该会话准备隔离工作目录。

    返回的 ``isolated=False`` 表示**没有**隔离（未开启 / 复制失败 / 超限），
    调用方必须按"共享目录"继续工作 —— 而不是以为已经隔离了。

    v1.7.7：复制前先扫描。总体积超过 ``workspace_max_mb`` 直接降级（此前只
    限文件数，4000 个大文件照样能把磁盘按会话翻倍 —— 实测最坏 1.16GB）；
    单个超过 ``workspace_large_file_mb`` 的文件不复制（视频、数据集、模型权重
    这类几乎不会被 Agent 改动），并记在清单里如实说明。
    """
    root = Path(project_root).resolve()
    if not enabled(ex):
        return WorkspacePlan(path=str(root), isolated=False,
                             created=False, reason="目录级隔离未开启")

    base = root_dir(root)
    target = base / _safe(session_id)
    if target.is_dir():
        _touch(target)
        return WorkspacePlan(path=str(target), isolated=True, created=False,
                             files=sum(1 for _ in target.rglob("*") if _.is_file()))

    max_bytes = int(float(getattr(ex, "workspace_max_mb", DEFAULT_MAX_MB) or 0) * 1024 * 1024)
    large_bytes = int(float(getattr(ex, "workspace_large_file_mb",
                                    DEFAULT_LARGE_FILE_MB) or 0) * 1024 * 1024)
    try:
        files, skipped, total = _scan(root, target, base, large_bytes)
        if len(files) > MAX_FILES:
            logger.warning("session_workspace_too_large", root=str(root), limit=MAX_FILES)
            return WorkspacePlan(
                path=str(root), isolated=False, created=False,
                reason=f"项目文件数超过 {MAX_FILES}，已跳过目录级隔离"
                       f"（请改用 write_conflict_policy=block 或手动分工作区）")
        if max_bytes and total > max_bytes:
            logger.warning("session_workspace_too_big", root=str(root),
                           mb=round(total / 1048576, 1), limit_mb=max_bytes // 1048576)
            return WorkspacePlan(
                path=str(root), isolated=False, created=False,
                reason=(f"项目体积约 {total / 1048576:.0f}MB，超过隔离副本上限 "
                        f"{max_bytes // 1048576}MB，已跳过目录级隔离（避免磁盘按会话成倍增长；"
                        f"可在设置里调大 workspace_max_mb）"))
        target.mkdir(parents=True, exist_ok=True)
        baseline: dict[str, list[int]] = {}
        for src in files:
            rel = src.relative_to(root)
            dst = target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            st = dst.stat()
            baseline[str(rel).replace("\\", "/")] = [st.st_size, st.st_mtime_ns]
        now = time.time()
        _write_manifest(target, {"session": session_id, "source": str(root),
                                 "created": now, "last_used": now,
                                 "baseline": baseline, "skipped_large": skipped[:200],
                                 "bytes": total})
        _prune_old(int(getattr(ex, "workspace_keep", 8) or 8), root,
                   busy=frozenset({target.name}))
        logger.info("session_workspace_ready", session=session_id,
                    path=str(target), files=len(files), mb=round(total / 1048576, 1),
                    skipped_large=len(skipped))
        reason = (f"{len(skipped)} 个超过 {large_bytes // 1048576}MB 的大文件未复制进副本"
                  if skipped else "")
        return WorkspacePlan(path=str(target), isolated=True, created=True,
                             files=len(files), reason=reason)
    except Exception as e:
        shutil.rmtree(target, ignore_errors=True)
        logger.warning("session_workspace_failed", session=session_id, error=str(e))
        return WorkspacePlan(path=str(root), isolated=False, created=False,
                             reason=f"工作副本创建失败：{type(e).__name__}: {e}")


# ── v1.7.7：产物可见 + 取回 + 主动清理 ───────────────────────────


def _dir_size(d: Path) -> tuple[int, int]:
    n = total = 0
    for f in d.rglob("*"):
        if f.is_file():
            n += 1
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return n, total


def changes(session_id: str, project_root: str | Path) -> dict[str, Any]:
    """这个会话的副本里 Agent 改了什么：新增 / 修改 / 删除（相对复制时的基线）。"""
    target = root_dir(project_root) / _safe(session_id)
    if not target.is_dir():
        return {"ok": False, "error": "该会话没有隔离工作副本"}
    m = _read_manifest(target)
    base: dict[str, list[int]] = m.get("baseline") or {}
    added: list[str] = []
    modified: list[str] = []
    seen: set[str] = set()
    for f in target.rglob("*"):
        if not f.is_file():
            continue
        rel = str(f.relative_to(target)).replace("\\", "/")
        if rel == MANIFEST_NAME or _product_ignored(rel):
            continue
        seen.add(rel)
        st = f.stat()
        if rel not in base:
            added.append(rel)
        elif [st.st_size, st.st_mtime_ns] != list(base[rel]):
            modified.append(rel)
    deleted = sorted(r for r in base if r not in seen)
    return {"ok": True, "workspace": str(target), "source": m.get("source", ""),
            "added": sorted(added), "modified": sorted(modified), "deleted": deleted,
            "skipped_large": m.get("skipped_large", []),
            "count": len(added) + len(modified) + len(deleted)}


def source_root(session_id: str, project_root: str | Path) -> Path:
    """副本是从哪个项目根复制出来的 —— 应用时必须回到**同一个根**（v1.11.6）。

    此前一律用"当前项目目录"：任务跑完后切换过项目目录，产物就会被按相对路径写进
    另一个项目。清单里记着复制来源，来源目录还在就以它为准。
    """
    root = Path(project_root).resolve()
    src = _read_manifest(root_dir(root) / _safe(session_id)).get("source")
    if src:
        try:
            p = Path(src).resolve()
            if p.is_dir():
                return p
        except Exception:
            pass
    return root


def apply_plan(session_id: str, project_root: str | Path) -> dict[str, Any]:
    """应用前的预览：每个产物文件会写到项目根的哪里、是新建还是覆盖。"""
    ch = changes(session_id, project_root)
    if not ch.get("ok"):
        return ch
    root = source_root(session_id, project_root)
    base = (_read_manifest(Path(ch["workspace"])).get("baseline") or {})
    files: list[dict[str, Any]] = []
    for rel in ch["added"] + ch["modified"]:
        dst = root / rel
        exists = dst.is_file()
        files.append({"path": rel, "target": str(dst),
                      "action": "overwrite" if exists else "create",
                      # 任务期间项目里的这份也被改过（覆盖前已备份，仍可还原）
                      "changed_in_project": bool(exists and _changed_since(dst, rel, base))})
    return {"ok": True, "project": str(root), "workspace": ch["workspace"],
            "files": files, "not_deleted": ch["deleted"]}


def _changed_since(dst: Path, rel: str, base: dict[str, list[int]]) -> bool:
    """项目里的 dst 与复制时的基线是否不同（基线里没有 = 项目里后来才有的同名文件）。"""
    if rel not in base:
        return True
    try:
        st = dst.stat()
    except OSError:
        return False
    return st.st_size != base[rel][0] or st.st_mtime_ns != base[rel][1]


def apply_to_project(session_id: str, project_root: str | Path,
                     files: list[str] | None = None, force: bool = True,
                     journal: Any = None, skip_conflicts: bool = False) -> dict[str, Any]:
    """把副本快照里的新增/修改**按相对路径覆盖回项目根**（用户点「应用到项目」后调用）。

    v1.11.6 起的语义（"同目录、同文件覆盖"）：
    * 目标根 = 副本的复制来源（见 ``source_root``），``rel`` 在副本里是什么，
      在项目里就写到 ``<项目根>/<rel>`` —— 新建缺失的目录，同名文件直接覆盖；
    * 覆盖前把项目里的原文件备份到 ``<副本>/_apply_backup/<时间戳>/<rel>``（二进制也备份），
      文本文件同时写进改动日志，「↩️ 撤销」一键还原；
    * 任务期间项目里也被改过的文件照样覆盖，但在 ``overwritten_changed`` 里点名；
      只有显式 ``skip_conflicts=True``（或旧参数 ``force=False``）才跳过它们；
    * 副本里被删除的文件**不会**在项目里删除（只在结果里列出，由用户决定）。
    """
    ch = changes(session_id, project_root)
    if not ch.get("ok"):
        return ch
    root = source_root(session_id, project_root)
    target = Path(ch["workspace"])
    base = (_read_manifest(target).get("baseline") or {})
    pick = set(files) if files else None
    skip = skip_conflicts or not force
    applied: list[str] = []
    conflicts: list[str] = []
    overwritten_changed: list[str] = []
    failed: list[dict[str, str]] = []
    backup_dir = target / BACKUP_DIRNAME / time.strftime("%Y%m%d-%H%M%S")
    backed_up = 0
    for rel in ch["added"] + ch["modified"]:
        if pick is not None and rel not in pick:
            continue
        src = target / rel
        dst = root / rel
        if not _under(dst, root):
            failed.append({"path": rel, "error": "路径越出项目根，已跳过"})
            continue
        changed = dst.exists() and _changed_since(dst, rel, base)
        if changed and skip:
            conflicts.append(rel)
            continue
        before = None
        try:
            if dst.is_file():
                bk = backup_dir / rel
                bk.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, bk)
                backed_up += 1
                try:
                    before = dst.read_text(encoding="utf-8")
                except Exception:
                    before = None          # 二进制：靠上面的备份还原
            elif dst.exists():
                failed.append({"path": rel, "error": "项目里同名路径是目录，无法用文件覆盖"})
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        except OSError as e:
            failed.append({"path": rel, "error": f"{type(e).__name__}: {e}"})
            continue
        if journal is not None:
            try:
                journal.record(str(dst), before, "workspace_apply")
            except Exception:           # pragma: no cover - 旁路
                pass
        if changed:
            overwritten_changed.append(rel)
        applied.append(rel)
    if applied:
        # 已应用的文件并入基线：之后它们不再算"待应用的改动"（界面上的
        # "N 处改动"指的是还留在副本里、没回到项目的那部分）
        m = _read_manifest(target)
        bl = m.setdefault("baseline", {})
        for rel in applied:
            st = (target / rel).stat()
            bl[rel] = [st.st_size, st.st_mtime_ns]
        _write_manifest(target, m)
    logger.info("session_workspace_applied", session=session_id, project=str(root),
                applied=len(applied), conflicts=len(conflicts),
                overwritten_changed=len(overwritten_changed), failed=len(failed))
    return {"ok": True, "applied": applied, "conflicts": conflicts,
            "overwritten_changed": overwritten_changed, "failed": failed,
            "backup_dir": str(backup_dir) if backed_up else "",
            "not_deleted": ch["deleted"], "project": str(root)}


def remove(session_id: str, project_root: str | Path) -> bool:
    """删除某个会话的工作副本。"""
    target = root_dir(project_root) / _safe(session_id)
    if not target.is_dir():
        return False
    shutil.rmtree(target, ignore_errors=True)
    logger.info("session_workspace_removed", session=session_id)
    return not target.exists()


def list_copies(project_root: str | Path) -> list[dict[str, Any]]:
    """当前项目下所有会话副本：大小、文件数、最近使用、改动数。"""
    base = root_dir(project_root)
    out: list[dict[str, Any]] = []
    if not base.is_dir():
        return out
    for d in base.iterdir():
        if not d.is_dir():
            continue
        m = _read_manifest(d)
        n, size = _dir_size(d)
        sid = str(m.get("session") or d.name)
        try:
            changed = changes(sid, project_root).get("count", 0) if m else 0
        except Exception:
            changed = 0
        out.append({"session": sid, "dir": d.name, "path": str(d), "files": n,
                    "bytes": size, "last_used": _last_used(d), "changed": changed})
    out.sort(key=lambda x: -x["last_used"])
    return out


def cleanup(project_root: str | Path, ttl_hours: float = DEFAULT_TTL_HOURS,
            keep: int = 8, busy: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """主动清理：删掉超过 TTL 未使用的副本，并把数量压到 keep 以内。

    v1.7.7：此前淘汰只发生在"下一次跑任务创建新副本"时 —— 不再跑任务，
    副本就永远留在磁盘上。现在服务端定时调用（以及会话被删除时）。
    ``busy`` 里的会话（正在跑任务）一律不动。
    """
    base = root_dir(project_root)
    removed: list[str] = []
    if not base.is_dir():
        return removed
    busy_dirs = {_safe(b) for b in busy}
    now = time.time()
    for d in list(base.iterdir()):
        if not d.is_dir() or d.name in busy_dirs:
            continue
        if ttl_hours and now - _last_used(d) > ttl_hours * 3600:
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d.name)
            logger.info("session_workspace_expired", path=str(d))
    _prune_old(keep, project_root, busy=busy_dirs)
    return removed


def export_results(session_id: str, project_root: str | Path) -> dict[str, object]:
    """把会话工作副本的改动收集到一个明确目录，便于用户取走/合并。

    隔离工作区的最大使用风险是"活干完了，东西在副本里，用户找不到"。
    这个函数把副本内容镜像到副本下的 ``_session_output/``，并在返回值里
    给出路径与文件清单。不做自动合并 —— 自动覆盖用户的项目才是真正危险的。
    """
    base = root_dir(project_root)
    src = base / _safe(session_id)
    if not src.is_dir():
        return {"ok": False, "error": f"会话工作区不存在：{src}"}
    dst = src / EXPORT_DIRNAME
    files: list[str] = []
    try:
        for f in src.rglob("*"):
            if (not f.is_file() or EXPORT_DIRNAME in f.parts or BACKUP_DIRNAME in f.parts
                    or f.name == MANIFEST_NAME):
                continue
            rel = f.relative_to(src)
            out = dst / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, out)
            files.append(str(rel))
        logger.info("session_workspace_exported", session=session_id,
                    files=len(files), dst=str(dst))
        return {"ok": True, "dir": str(dst), "workspace": str(src),
                "files": files[:500], "count": len(files),
                "note": ("已把该会话的全部产物镜像到该目录，可直接查看或复制回项目；"
                         "未自动覆盖你的项目目录（避免不经确认的覆盖）。")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _safe(part: str) -> str:
    keep = [c for c in str(part) if c.isalnum() or c in "-_.@"]
    return ("".join(keep) or "default")[:64]
