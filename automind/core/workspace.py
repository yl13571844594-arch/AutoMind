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

import os
import shutil
from pathlib import Path

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


def _ignored_parts(name: str) -> bool:
    return name in SKIP_DIRS or name.endswith(SKIP_SUFFIX)


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


def _prune_old(keep: int, project_root: str | Path) -> None:
    """淘汰旧的会话目录（保留最近 keep 个）。"""
    try:
        base = root_dir(project_root)
        if not base.is_dir():
            return
        dirs = [d for d in base.iterdir() if d.is_dir()]
        if len(dirs) <= keep:
            return
        dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
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


def prepare(session_id: str, project_root: str | Path,
            ex: object | None = None) -> WorkspacePlan:
    """为该会话准备隔离工作目录。

    返回的 ``isolated=False`` 表示**没有**隔离（未开启 / 复制失败 / 超限），
    调用方必须按"共享目录"继续工作 —— 而不是以为已经隔离了。
    """
    root = Path(project_root).resolve()
    if not enabled(ex):
        return WorkspacePlan(path=str(root), isolated=False,
                             created=False, reason="目录级隔离未开启")

    base = root_dir(root)
    target = base / _safe(session_id)
    if target.is_dir():
        return WorkspacePlan(path=str(target), isolated=True, created=False,
                             files=sum(1 for _ in target.rglob("*") if _.is_file()))

    try:
        target.mkdir(parents=True, exist_ok=True)
        copied = 0
        for src in root.rglob("*"):
            rel = src.relative_to(root)
            if any(_ignored_parts(part) for part in rel.parts):
                continue
            # 工作副本的根目录**可能落在项目目录之内**（例如被配置成了
            # <project>/.cache/workspaces）。这时若不排除，复制会把自己
            # 一层层拷进自己里面：wss/sess/wss/sess/... 直到
            # Windows 报 WinError 206（文件名或扩展名太长）。此前只检查
            # 名字，抓不到这种"路径级的自包含"，于是静默失败并降级。
            if _under(src, target) or _under(src, base):
                continue
            copied += 1
            if copied > MAX_FILES:
                shutil.rmtree(target, ignore_errors=True)
                logger.warning("session_workspace_too_large", root=str(root),
                               limit=MAX_FILES)
                return WorkspacePlan(
                    path=str(root), isolated=False, created=False,
                    reason=f"项目文件数超过 {MAX_FILES}，已跳过目录级隔离"
                           f"（请改用 write_conflict_policy=block 或手动分工作区）")
            dst = target / rel
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        _prune_old(int(getattr(ex, "workspace_keep", 8) or 8), root)
        logger.info("session_workspace_ready", session=session_id,
                    path=str(target), files=copied)
        return WorkspacePlan(path=str(target), isolated=True, created=True,
                             files=copied)
    except Exception as e:
        shutil.rmtree(target, ignore_errors=True)
        logger.warning("session_workspace_failed", session=session_id, error=str(e))
        return WorkspacePlan(path=str(root), isolated=False, created=False,
                             reason=f"工作副本创建失败：{type(e).__name__}: {e}")


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
            if not f.is_file() or EXPORT_DIRNAME in f.parts:
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
