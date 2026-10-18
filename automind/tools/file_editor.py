"""文件编辑工具 — 多文件协同修改、diff 追踪、原子回滚。"""

from __future__ import annotations

import difflib
import time
from pathlib import Path
from typing import Any

from automind.core.types import PermissionTier, ToolResult
from automind.tools.base import AbstractTool
from automind.tools.write_guard import content_hash as _content_hash
from automind.tools.write_guard import path_lock


def read_text_any(path: Path) -> tuple[str, str]:
    """读文本：UTF-8 优先，失败再按 GBK（中文 Windows 上大量旧文件是 GBK）。返回 (内容, 编码)。

    写回时用同一编码 —— 不能把用户的 GBK 文件悄悄改成 UTF-8（别的程序会读成乱码）。
    """
    for enc in ("utf-8", "gbk"):
        try:
            return path.read_text(encoding=enc), enc
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace"), "utf-8"


def fuzzy_locate(original: str, old: str, new: str) -> tuple[int, int, str, str] | None:
    """old_string 精确匹配失败时的容错定位（v1.11.7）。

    依次尝试：① 忽略行尾空白；② 忽略每行首尾空白（整体缩进不同）。必须**唯一命中**。
    返回 (起, 止, 调整过缩进的 new_string, 说明)；找不到 / 多处命中返回 None。
    """
    target = old.splitlines()
    while target and not target[-1].strip():
        target.pop()
    while target and not target[0].strip():
        target.pop(0)
    if not target:
        return None
    lines = original.splitlines(keepends=True)
    n = len(target)
    for label, norm in (("忽略行尾空白", str.rstrip), ("忽略缩进差异", str.strip)):
        want = [norm(t) for t in target]
        hits = [i for i in range(len(lines) - n + 1)
                if all(norm(lines[i + k].rstrip("\r\n")) == want[k] for k in range(n))]
        if len(hits) != 1:
            continue
        i = hits[0]
        start = sum(len(x) for x in lines[:i])
        end = start + sum(len(x) for x in lines[i:i + n])
        span_nl = lines[i + n - 1][len(lines[i + n - 1].rstrip("\r\n")):]
        new_text = new
        if norm is str.strip:
            # 按"文件里第一行的缩进 vs old 里第一行的缩进"整体平移 new_string 的缩进
            file_first = lines[i].rstrip("\r\n")
            fi = file_first[:len(file_first) - len(file_first.lstrip())]
            first_old = next(t for t in old.splitlines() if t.strip())
            oi = first_old[:len(first_old) - len(first_old.lstrip())]
            shifted = []
            for ln in new.splitlines():
                if ln.startswith(oi):
                    shifted.append(fi + ln[len(oi):])
                elif ln.strip():
                    shifted.append(fi + ln.lstrip())
                else:
                    shifted.append(ln)
            new_text = "\n".join(shifted)
        if span_nl and not new_text.endswith(("\n", "\r\n")):
            new_text += "\n"
        return start, end, new_text, f"old_string 未精确匹配，已按「{label}」唯一定位后替换"
    return None


class FileChangeJournal:
    """全局文件改动日志 — 记录每次写入/编辑的前像，支持撤销回滚。

    进程级单例（模块属性 ``JOURNAL``），跨 Agent 重建仍保留，
    供 Web 层「↩️ 撤销/回滚」功能使用：
        - ``record(path, before, tool)``：工具写入前调用；before=None 表示新建文件。
        - ``entries()``：最近的改动列表（新→旧），前像不外传（可能很大/含敏感内容）。
        - ``rollback(path)``：把该文件恢复到**最早记录**的前像（新建则删除）。
        - ``rollback_all()``：按时间倒序恢复全部记录的改动。
    容量上限 200 条，超限丢弃最旧记录。
    """

    MAX_ENTRIES = 200

    def __init__(self) -> None:
        self._entries: list[dict] = []  # {path, before, tool, time, created}

    def record(self, path: str, before: str | None, tool: str) -> None:
        self._entries.append({
            "path": path, "before": before, "tool": tool,
            "time": time.strftime("%H:%M:%S"), "ts": time.time(),
            "created": before is None,
        })
        del self._entries[:-self.MAX_ENTRIES]

    def entries(self) -> list[dict]:
        return [
            {"path": e["path"], "tool": e["tool"], "time": e["time"],
             "created": e["created"]}
            for e in reversed(self._entries)
        ]

    def _restore(self, entry: dict) -> bool:
        p = Path(entry["path"])
        try:
            if entry["created"]:
                if p.exists():
                    p.unlink()
            else:
                p.write_text(entry["before"], encoding="utf-8")
            return True
        except Exception:
            return False

    def pre_image(self, path: str) -> dict | None:
        """该文件最早的前像（Diff 预览用）：{'before': str|None, 'created': bool}。"""
        for e in self._entries:
            if e["path"] == path:
                return {"before": e["before"], "created": e["created"]}
        return None

    def rollback(self, path: str) -> bool:
        """恢复单个文件到本日志中最早的前像（即撤销全部已记录改动）。"""
        matches = [e for e in self._entries if e["path"] == path]
        if not matches:
            return False
        ok = self._restore(matches[0])  # 最早的前像 = 改动前的原始内容
        if ok:
            self._entries = [e for e in self._entries if e["path"] != path]
        return ok

    def rollback_all(self) -> int:
        """按时间倒序恢复全部改动；返回成功恢复的文件数。"""
        restored: set[str] = set()
        count = 0
        for e in reversed(self._entries):
            if e["path"] in restored:
                continue
            first = next(x for x in self._entries if x["path"] == e["path"])
            if self._restore(first):
                restored.add(e["path"])
                count += 1
        self._entries = [e for e in self._entries if e["path"] not in restored]
        return count

    def clear(self) -> None:
        self._entries.clear()


#: 进程级改动日志单例（Web 层与全部文件工具共享）
JOURNAL = FileChangeJournal()


class _RootGuard:
    """路径穿越防护 — 将文件路径限定在 project_root 之内。

    安全语义：
        - ``project_root=None`` 时不做任何限制（向后兼容：库/测试中默认构造的
          工具行为与升级前完全一致）。
        - 设置 ``project_root`` 后，相对路径基于 root 解析，任何解析到 root 之外
          （含 ``..`` 穿越、符号链接逃逸）的路径都会抛出 ``PermissionError``。

    采用 ``resolved == root or root in resolved.parents`` 的严格包含判断，
    避免 ``str.startswith`` 的前缀碰撞漏洞（如 ``/srv/app`` 与 ``/srv/app-evil``）。

    v1.6.4：**会话级隔离工作目录**。当开启了目录级隔离
    （``ExecutionConfig.isolate_workspace``）时，当前会话通过 contextvar 声明了
    自己的私有工作目录；相对路径优先解析到那里，从而让并发会话不再改同一批
    文件。绝对路径仍按 ``project_root`` 校验（隔离工作区也建在它之内），
    越界照旧拒绝 —— 隔离不放松任何安全边界。
    """

    def __init__(self, project_root: str | Path | None = None) -> None:
        self._root: Path | None = (
            Path(project_root).resolve() if project_root is not None else None
        )

    @property
    def root(self) -> Path | None:
        return self._root

    def _session_root(self) -> Path | None:
        try:
            from automind.core.session_ctx import workspace

            ws = workspace()
        except Exception:
            return None
        if not ws:
            return None
        try:
            p = Path(ws).resolve()
        except Exception:
            return None
        return p

    def resolve(self, path: str | Path) -> Path:
        """解析并校验路径；越界时抛出 PermissionError。

        相对路径的基准在会话隔离开启时是**该会话的私有工作目录**（见
        ``core/workspace.py``），否则是 ``project_root``。
        """
        p = Path(path)
        if self._root is None:
            return p
        session_root = self._session_root()
        if not p.is_absolute():
            # 会话私有工作目录优先：同一相对路径在不同会话里落到不同目录
            p = (session_root or self._root) / p
        resolved = p.resolve()
        if session_root is not None:
            from automind.core.workspace import remap_into_workspace
            resolved = remap_into_workspace(resolved, self._root, session_root)
        # 放行两种合法根：project_root，以及**被显式采纳的**会话隔离目录。
        # 隔离目录由服务端/测试显式传入（workspace.prepare / contextvar），
        # 不是模型可控的输入；这里如实放行，而不是用"必须在 project_root 之内"
        # 去拒绝它 —— 那会让隔离目录里的每一次写入都被自己的防护挡下。
        allowed = [self._root]
        if session_root is not None:
            allowed.append(session_root)
        for root in allowed:
            if resolved == root or root in resolved.parents:
                return self._guard_platform_secrets(resolved, path)
        raise PermissionError(self.out_of_scope_message(path, allowed))

    def out_of_scope_message(self, path: str | Path,
                             roots: list[Path] | None = None) -> str:
        """越界时的统一话术：**说清基准目录**，而不是只说"越界了"。

        v1.7.5：原先只有一句"路径越界：'x' 解析到允许范围之外" —— 模型不知道
        基准在哪，只能猜。而同一个错误在 ``data_tools`` 里又是另一种写法
        （"path 超出项目目录范围"），两处口径不一致。现在共用这一个函数。
        """
        roots = roots if roots is not None else [self._root]
        shown = "、".join(str(r) for r in roots if r is not None) or "(未设置)"
        # 措辞里保留"超出"：既有调用方/用例是照这个词判定的，而这里加的
        # 是**补充信息**（基准目录 + 该怎么写），不该顺带改掉判定依据。
        return (f"路径越界：'{path}' 超出允许的目录范围（不在允许的目录内）。"
                f"允许的根目录是：{shown}。"
                f"请改用**相对项目根的相对路径**（例如 src/app.py），"
                f"或项目内的绝对路径。")

    @staticmethod
    def _guard_platform_secrets(resolved: Path, original: str | Path) -> Path:
        """在项目目录之内**再挡一层平台自身的密钥与数据**（v1.7.3）。

        路径守卫原本只管"在不在 project_root 之内"，平台的密钥与数据恰好就在
        项目里：``.automind_config.json``（全部提供商 API Key）、
        ``.automind/automind.db``（全部会话历史）、``.automind/traces/**``
        （提示词与被读过的文件内容）。模型没有任何正当理由读它们，而提示注入
        最想拿的就是它们 —— 一次 ``file_read(".automind_config.json")``
        就足以把密钥带进发给模型提供商的上下文里。

        注意与 Web 端点的**粒度差异**：这里只挡平台自身的文件，不挡用户项目里的
        ``.env`` / ``.ssh`` —— 读项目文件是 Agent 的本职，"帮我看看 .env 缺哪项"
        是正常任务。理由与边界写在 ``automind/core/sensitive.py`` 的模块文档里。
        """
        from automind.core.sensitive import SCOPE_TOOL
        from automind.core.sensitive import reason as _sensitive_reason

        denied = _sensitive_reason(resolved, SCOPE_TOOL)
        if denied:
            raise PermissionError(f"拒绝访问 '{original}'：{denied}")
        return resolved


def _write_guard() -> Any:
    """构造本次写入的冲突守卫（会话身份 + 策略均来自 contextvar/配置）。

    会话身份走 contextvar 而不是实例属性：工具注册表在会话克隆之间是**共享**
    的，把 session 挂到工具实例上会被并发会话互相覆盖。
    """
    from automind.tools.write_guard import WriteGuard

    try:
        from automind.core.session_ctx import session_id as _sid

        sid = _sid() or ""
    except Exception:
        sid = ""
    policy = "warn"
    try:
        from automind.core.config import ExecutionConfig

        policy = ExecutionConfig().write_conflict_policy
    except Exception:
        pass
    return WriteGuard(session=sid, policy=policy)


def _conflict_note(conflict: dict[str, Any]) -> str:
    """把冲突判定渲染成给模型的明确提示（可照做，而不是含糊警告）。"""
    if not conflict or conflict.get("ok") and not conflict.get("foreign"):
        return ""
    return (
        "\n\n⚠ 并发写冲突提示：" + str(conflict.get("reason") or "") +
        "（本次写入已完成，但请立刻重新读取该文件核对内容；"
        "若发现不是你预期的版本，请基于**当前真实内容**重新修改，"
        "不要凭记忆覆盖。）"
    )


class FileReadTool(AbstractTool):
    """文件读取工具 — 支持按行分段读取，大文件自动截断保护上下文。"""

    name = "file_read"
    description = (
        "Read the contents of a file. For large files, pass offset/limit "
        "(1-based line number + line count) to read a specific range; "
        "oversized reads are truncated with a note."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to read. Relative to the project root (e.g. `src/app.py`); an absolute path inside the project also works."},
            "encoding": {"type": "string", "description": "File encoding (default: utf-8)."},
            "offset": {"type": "integer",
                       "description": "Start line (1-based). Use with limit for large files."},
            "limit": {"type": "integer",
                      "description": "Max number of lines to return from offset."},
        },
        "required": ["path"],
    }
    permission_tier = PermissionTier.SAFE

    #: 无显式范围时的自动截断阈值 —— 整读超大文件会撑爆模型上下文，
    #: 反而降低后续编辑的准确度；截断并提示用 offset/limit 分段读。
    MAX_CHARS = 120_000
    TRUNC_LINES = 1500

    def __init__(self, project_root: str | Path | None = None) -> None:
        self._guard = _RootGuard(project_root)

    @staticmethod
    def _decode_file(path: Path, encoding: str) -> tuple[str, str, str]:
        """读文本：先按请求编码，再按 UTF-8 / GBK 系列回退（v1.11.13）。

        中文 Windows 上大量 .txt/.csv/.md 是 GBK/GB18030；模型默认按 UTF-8
        读会直接 `UnicodeDecodeError`，于是一次本可成功的 file_read 白跑。
        返回 ``(内容, 实际编码, 提示)``；全部失败抛最后一个异常。
        """
        candidates = [encoding] if encoding else []
        for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "latin-1"):
            if enc not in candidates:
                candidates.append(enc)
        last_error: Exception | None = None
        for enc in candidates:
            try:
                text = path.read_text(encoding=enc)
            except (UnicodeDecodeError, LookupError) as e:
                last_error = e
                continue
            except OSError as e:
                raise e
            if enc != encoding:
                return text, enc, (f"文件不是 {encoding} 编码，已按 {enc} 成功读取；"
                                   f"后续写入请沿用 encoding={enc}")
            return text, enc, ""
        if last_error is not None:
            raise last_error
        return path.read_text(encoding=encoding), encoding, ""

    @staticmethod
    def _suggest_similar(path: Path, limit: int = 3) -> list[str]:
        """文件不存在时给几条最像的路径，省掉一次盲目重试。"""
        import difflib

        parent = path.parent
        try:
            names = [p.name for p in parent.iterdir() if p.is_file()][:500]
        except OSError:
            return []
        close = difflib.get_close_matches(path.name, names, n=limit, cutoff=0.55)
        out = [str(parent / name) for name in close]
        # 命中大小写不同但同名的文件也算
        lower = {name.casefold(): name for name in names}
        match = lower.get(path.name.casefold())
        if match and str(parent / match) not in out:
            out.insert(0, str(parent / match))
        return out[:limit]

    async def execute(self, **kwargs: Any) -> ToolResult:
        encoding = kwargs.get("encoding", "utf-8")
        try:
            path = self._guard.resolve(kwargs["path"])
        except PermissionError as e:
            return ToolResult(tool_name=self.name, success=False, error=str(e))
        if not path.exists():
            similar = self._suggest_similar(path)
            hint = ("；同目录下相近的文件：" + "、".join(similar)) if similar else ""
            return ToolResult(
                tool_name=self.name, success=False,
                error=(f"文件不存在：{path}{hint}。"
                       f"请先用 file_search 确认路径，或写相对项目根/会话工作副本的路径。"),
                output={"path": str(path), "missing": True, "similar": similar},
            )
        if path.is_dir():
            return ToolResult(
                tool_name=self.name, success=False,
                error=(f"这是一个目录而不是文件：{path}。"
                       f"读取目录请用 file_search（pattern='*.py'）或 terminal（dir/ls）。"),
                output={"path": str(path), "is_dir": True},
            )
        try:
            content, used_encoding, note = self._decode_file(path, encoding)
        except Exception as e:
            return ToolResult(
                tool_name=self.name, success=False,
                error=(f"读取失败（已尝试 {encoding}/utf-8/gb18030/gbk）："
                       f"{type(e).__name__}: {e}。若文件很大或不是文本，"
                       f"可用 terminal 的 python 脚本按二进制读取。"),
            )

        total_size = len(content)
        lines = content.splitlines(keepends=True)
        total_lines = len(lines)
        out: dict[str, Any] = {"path": str(path), "size": total_size,
                               "total_lines": total_lines,
                               "encoding": used_encoding,
                               # 内容指纹：可原样回传给 file_write/file_edit 的
                               # expected_hash，声明"我只覆盖我读到的那一版"，
                               # 从而确定性拦住并发覆盖（见 tools/write_guard.py）
                               "content_hash": _content_hash(content)}
        if note:
            out["note"] = note
        # 登记"本会话读过"，同名文件后续被别的会话改写才能被检出为冲突
        _write_guard().note_read(str(path))

        offset = kwargs.get("offset")
        limit = kwargs.get("limit")
        if offset or limit:
            start = max(int(offset or 1) - 1, 0)
            count = int(limit) if limit else self.TRUNC_LINES
            picked = lines[start:start + max(count, 1)]
            out["content"] = "".join(picked)
            out["range"] = f"lines {start + 1}-{start + len(picked)} of {total_lines}"
        elif total_size > self.MAX_CHARS:
            picked = lines[:self.TRUNC_LINES]
            out["content"] = "".join(picked)
            out["truncated"] = True
            out["note"] = (
                f"File is large ({total_size} chars, {total_lines} lines); "
                f"showing first {len(picked)} lines. Use offset/limit to read "
                f"the rest in ranges."
            )
        else:
            out["content"] = content
        return ToolResult(tool_name=self.name, success=True, output=out)


class FileWriteTool(AbstractTool):
    """文件写入工具。

    v1.6.4 并发写治理：
        · 同路径写入经 :func:`automind.tools.write_guard.path_lock` 串行化，
          避免"读 A → 被 B 覆盖 → 写回 A"的交错覆盖；
        · 覆盖**别的会话刚改过、本会话之后没读过**的文件时，按
          ``ExecutionConfig.write_conflict_policy`` 给出明确冲突提示（warn）
          或直接拒绝（block），不再静默覆盖对方成果；
        · 可选 ``expected_hash``：声明"我改的是我读到的那一版"（``file_read``
          会返回 ``content_hash``），不匹配即失败并附当前内容指纹，
          让模型基于真实状态重做。
    """

    name = "file_write"
    description = (
        "Write content to a file, creating or overwriting it. "
        "When overwriting a file another session may have changed, pass "
        "expected_hash (from file_read's content_hash) to guarantee you are "
        "not clobbering concurrent edits."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to write. Relative to the project root (e.g. `src/app.py`); an absolute path inside the project also works."},
            "content": {"type": "string", "description": "Content to write."},
            "encoding": {"type": "string", "description": "File encoding (default: utf-8)."},
            "expected_hash": {
                "type": "string",
                "description": ("Content hash you expect the file to currently have "
                                "(from file_read's content_hash). Mismatch aborts "
                                "the write instead of silently overwriting."),
            },
        },
        "required": ["path", "content"],
    }
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 40

    def __init__(self, project_root: str | Path | None = None) -> None:
        self._guard = _RootGuard(project_root)

    async def execute(self, **kwargs: Any) -> ToolResult:
        raw_content = kwargs["content"]
        if raw_content is None:
            raw_content = ""
        if not isinstance(raw_content, str):
            # 模型偶尔把对象/数组直接塞进 content；写文件先序列化成文本，
            # 而不是让 write_text 抛 TypeError 白跑一次
            import json as _json

            if isinstance(raw_content, (dict, list, tuple)):
                raw_content = _json.dumps(raw_content, ensure_ascii=False, indent=2)
            else:
                raw_content = str(raw_content)
        content = raw_content
        encoding = kwargs.get("encoding", "utf-8")
        expected = kwargs.get("expected_hash") or None
        try:
            path = self._guard.resolve(kwargs["path"])
        except PermissionError as e:
            return ToolResult(tool_name=self.name, success=False, error=str(e))
        if path.exists() and path.is_dir():
            return ToolResult(
                tool_name=self.name, success=False,
                error=f"目标是一个目录而不是文件：{path}。请指定文件名后重试。",
                output={"path": str(path), "is_dir": True},
            )

        guard = _write_guard()
        conflict = guard.check(str(path), expected_hash=expected)
        if not conflict["ok"]:
            # block 策略或指纹不符 —— 明确失败并给出当前真实状态
            current = ""
            try:
                current = path.read_text(encoding=encoding)[:1200]
            except Exception:
                current = "（读取失败，文件可能不存在）"
            return ToolResult(
                tool_name=self.name, success=False,
                error=(f"写入被拒绝：{conflict['reason']}\n"
                       f"文件当前内容（供修正参考）：\n{current}"),
                output={"path": str(path), "conflict": conflict,
                        "rejected": True},
            )

        async with path_lock(str(path)):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                existed = path.exists()
                # 撤销支持：写入前记录前像（新建文件记 None，回滚时删除）。
                # 已存在但前像读取失败（二进制/编码问题）时不记录 —— 无法安全恢复。
                if existed:
                    try:
                        JOURNAL.record(str(path), path.read_text(encoding=encoding),
                                       self.name)
                    except Exception:
                        pass
                else:
                    JOURNAL.record(str(path), None, self.name)
                path.write_text(content, encoding=encoding)
                guard.note_write(str(path), self.name)
                out: dict[str, Any] = {
                    "path": str(path),
                    "size": len(content),
                    "created": not existed,
                    "overwritten": existed,
                    "content_hash": _content_hash(content),
                }
                if conflict.get("foreign"):
                    out["conflict"] = conflict
                    out["warning"] = _conflict_note(conflict)
                return ToolResult(tool_name=self.name, success=True, output=out)
            except Exception as e:
                return ToolResult(tool_name=self.name, success=False, error=str(e))


class FileEditTool(AbstractTool):
    """精确字符串替换编辑 — 支持 diff 追踪和回滚。

    使用精确匹配替换 (类似 Claude Code 的 Edit 工具)：
    提供 old_string 和 new_string，工具在文件中查找并替换。
    """

    name = "file_edit"
    description = (
        "Edit a file by replacing a specific string with another string. "
        "The old_string must match exactly once in the file."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to edit. Relative to the project root (e.g. `src/app.py`); an absolute path inside the project also works."},
            "old_string": {"type": "string", "description": "The exact text to replace."},
            "new_string": {"type": "string", "description": "The text to replace it with."},
            "replace_all": {
                "type": "boolean",
                "description": "Replace all occurrences (default: false).",
            },
            "expected_hash": {
                "type": "string",
                "description": ("Optional content hash (from file_read) that the file "
                                "must still have; mismatch aborts the edit instead of "
                                "overwriting concurrent changes."),
            },
        },
        "required": ["path", "old_string", "new_string"],
    }
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 45

    def __init__(self, project_root: str | Path | None = None) -> None:
        self._guard = _RootGuard(project_root)
        self._backups: dict[str, str] = {}
        self._diff_history: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> ToolResult:
        old = kwargs["old_string"]
        new = kwargs["new_string"]
        # 模型偶尔把数字/对象当字符串传；先转成文本，避免 .count() 直接抛类型错
        if not isinstance(old, str):
            old = "" if old is None else str(old)
        if not isinstance(new, str):
            new = "" if new is None else str(new)
        replace_all = kwargs.get("replace_all", False)
        if old == new:
            return ToolResult(
                tool_name=self.name, success=False,
                error="old_string and new_string are identical — no-op edit. "
                      "Provide the changed text as new_string.",
            )
        try:
            path = self._guard.resolve(kwargs["path"])
        except PermissionError as e:
            return ToolResult(tool_name=self.name, success=False, error=str(e))

        guard = _write_guard()
        conflict = guard.check(str(path), expected_hash=kwargs.get("expected_hash") or None)
        if not conflict["ok"]:
            return ToolResult(
                tool_name=self.name, success=False,
                error=(f"编辑被拒绝：{conflict['reason']}"),
                output={"path": str(path), "conflict": conflict, "rejected": True},
            )

        async with path_lock(str(path)):
            return await self._execute_locked(path, old, new, replace_all, conflict)

    async def _execute_locked(self, path: Path, old: str, new: str,
                              replace_all: bool, conflict: dict[str, Any]) -> ToolResult:
        """持锁执行实际的替换（读—改—写必须在同一把锁内才谈得上原子）。"""
        try:
            original, enc = read_text_any(path)
        except Exception as e:
            return ToolResult(tool_name=self.name, success=False, error=str(e))

        count = original.count(old)
        tolerant = ""
        if count == 0:
            # v1.11.7：精确匹配失败时，按"忽略行尾空白 / 忽略整体缩进差异"再找一次。
            # 模型复述代码时最常丢的就是行尾空格与缩进层级 —— 唯一命中才替换，
            # 多处命中或找不到仍按原样报错（宁可让模型重试，也不改错地方）。
            hit = fuzzy_locate(original, old, new)
            if hit is not None:
                start, end, new, tolerant = hit
                old = original[start:end]
                count = 1
                replace_all = False
        if count == 0:
            hint = self._nearest_match_hint(original, old)
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"old_string not found in file: {old[:80]}...{hint}",
            )

        if not replace_all and count > 1:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"old_string found {count} times. Use replace_all=true or provide more context.",
            )

        if tolerant:
            modified = original[:start] + new + original[end:]     # 按定位到的区间拼接，不再二次搜索
        else:
            modified = original.replace(old, new, -1 if replace_all else 1) if replace_all else original.replace(old, new, 1)

        # 备份原始内容（工具级回滚 + 全局撤销日志）
        self._backups[str(path)] = original
        JOURNAL.record(str(path), original, self.name)

        # 计算 diff
        diff = self._compute_diff(original, modified, path.name)

        path.write_text(modified, encoding=enc)
        guard = _write_guard()
        guard.note_write(str(path), self.name)
        self._diff_history.append({
            "path": str(path),
            "diff": diff,
            "replacements": count if replace_all else 1,
        })

        out: dict[str, Any] = {
            "path": str(path),
            "diff": diff,
            "replacements": count if replace_all else 1,
            "content_hash": _content_hash(modified),
        }
        if tolerant:
            out["match_note"] = tolerant
        if enc != "utf-8":
            out["encoding"] = enc
        if conflict.get("foreign"):
            out["conflict"] = conflict
            out["warning"] = _conflict_note(conflict)
        return ToolResult(tool_name=self.name, success=True, output=out)

    def rollback(self, path: str | Path) -> bool:
        """回滚文件到最后一次编辑前的状态。"""
        key = str(path)
        if key in self._backups:
            Path(key).write_text(self._backups[key], encoding="utf-8")
            del self._backups[key]
            return True
        return False

    def get_diff_history(self) -> list[dict[str, Any]]:
        return list(self._diff_history)

    @staticmethod
    def _nearest_match_hint(original: str, old: str, context_lines: int = 2) -> str:
        """定位文件中与 old_string 最接近的片段，附进错误信息。

        精确匹配失败最常见的原因是缩进/空白/引号差异——把文件里真实的
        近似片段回显给模型，下一轮即可用确切文本重试，而非盲目猜测。
        """
        old_first = next((ln.strip() for ln in old.splitlines() if ln.strip()), "")
        if not old_first:
            return ""
        orig_lines = original.splitlines()
        # 先做「去空白后相等」的快速定位，再退化到模糊匹配
        idx = next((i for i, ln in enumerate(orig_lines) if ln.strip() == old_first), -1)
        if idx < 0:
            stripped = [ln.strip() for ln in orig_lines]
            close = difflib.get_close_matches(old_first, stripped, n=1, cutoff=0.6)
            if close:
                idx = stripped.index(close[0])
        if idx < 0:
            return ""
        n_old = max(len(old.splitlines()), 1)
        lo = max(0, idx - context_lines)
        hi = min(len(orig_lines), idx + n_old + context_lines)
        snippet = "\n".join(f"{i + 1}\t{orig_lines[i]}" for i in range(lo, hi))
        return (
            "\nNearest match in the file (line-numbered; check exact "
            f"whitespace/indentation and retry with the exact text):\n{snippet}"
        )

    @staticmethod
    def _compute_diff(original: str, modified: str, filename: str) -> str:
        diff_lines = list(difflib.unified_diff(
            original.splitlines(keepends=True),
            modified.splitlines(keepends=True),
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
        ))
        return "".join(diff_lines)


class FileMultiEditTool(AbstractTool):
    """多文件协同编辑 — 在一次操作中修改多个文件。"""

    name = "file_multi_edit"
    description = "Edit multiple files in one operation. Each edit is a path + old_string + new_string triple."
    parameters = {
        "type": "object",
        "properties": {
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                    },
                    "required": ["path", "old_string", "new_string"],
                },
                "description": "List of file edits to apply.",
            },
        },
        "required": ["edits"],
    }
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 50

    def __init__(self, project_root: str | Path | None = None) -> None:
        self._edit_tool = FileEditTool(project_root)

    async def execute(self, **kwargs: Any) -> ToolResult:
        edits = kwargs["edits"]
        results = []
        all_success = True

        for edit in edits:
            result = await self._edit_tool.execute(**edit)
            results.append({
                "path": edit["path"],
                "success": result.success,
                "error": result.error,
                "output": result.output,
            })
            if not result.success:
                all_success = False

        return ToolResult(
            tool_name=self.name,
            success=all_success,
            output={"results": results, "total": len(edits), "succeeded": sum(1 for r in results if r["success"])},
            error=None if all_success else "Some edits failed",
        )

    def rollback_all(self) -> int:
        """回滚所有已编辑的文件。"""
        count = 0
        for path in list(self._edit_tool._backups.keys()):
            if self._edit_tool.rollback(path):
                count += 1
        return count
