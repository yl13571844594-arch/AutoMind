"""可重放轨迹（Replay Trace）—— 把「模型当时收到的确切输入」原样存下来，并且能再发一次。

为什么需要它（与 ``core/trace.py`` 的分工）：

    ``trace.py`` 是**取证**用的：它证明"发生了什么"——调了哪个工具、成功率多少、
    花了多少 token。为此它必须做两件对重放致命的事：

      1. 把 ``content`` / ``output`` / ``prompt`` 这类字段**截断到 4000 字符**，
         一条长系统提示词/一份大文件正文进去，出来就只剩开头一段；
      2. 把所有可能的敏感字段替换成 ``***``。

    于是它**重建不了"模型当时收到的确切输入"**。后果很具体：改了提示词不知道
    有没有退化，用户报了 badcase 复现不出来，卖给客户时"效果好不好"只能靠演示。

    本模块只解决这一件事：**一次 LLM 调用 = 一行可完整重建请求的 JSON**，
    并且提供一个把这份请求重新发出去的 CLI。工具执行、计划、审批这些仍然看
    ``trace.py``，两者互不替代。

设计取向（明确取舍）：

  · **默认关闭**。完整提示词里含客户数据（代码、文档、业务数据），落盘必须是
    用户的显式选择：环境变量 ``AUTOMIND_REPLAY=1`` 或配置
    ``getattr(ExecutionConfig(), "replay_capture", False)``。
    关闭时 :func:`record_call` 只有**一次布尔判断**就返回 —— 这是它能被放进
    热路径（每次 LLM 调用）的前提。
  · **不截断正文，但要挡住密钥**。记录前**主动过滤**：key 名命中敏感词的整条
    丢弃，字符串值再过一遍 ``redact.py`` 的密钥正则（防止密钥被塞进普通 content）。
    单条记录仍有 4MB 的硬上限（Ctrl-C 级别的兜底），但断了正文时会**显式写一条
    ``replay_truncated``**，绝不静默丢。
  · **单文件上限 + 轮转**。超过 ``max_file_bytes`` 时先写一条明确的
    ``replay_truncated`` 事件（记清丢了几次调用），随后是否继续用**新文件**接着
    记由 ``rotate`` 决定（默认开启，因为"评测要的是完整轨迹"）。
  · **不阻断主流程**：任何写入异常只记一行日志，绝不让重放记录影响模型调用。
  · **就地分层**：``ReplayRecorder`` 可注入根目录/开关/上限，测试可完全离线构造；
    进程级默认实例懒构造，并随 ``AUTOMIND_DATA_DIR`` 变化自动重建（轨迹默认落在
    ``<data_dir>/traces/<session>/<run_id>.replay.jsonl``，与取证轨迹同目录便于一起归档）。

CLI::

    python -m automind.core.replay <file.replay.jsonl> [--model X] [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from automind.core.logging import get_logger
from automind.core.paths import traces_dir

logger = get_logger("automind.core.replay")

#: 单条记录的字节上限（保护性兜底，不是常规截断手段）。
#: 定得比 trace.py 的单文件上限还宽松：重放要的就是完整正文。
_MAX_RECORD_BYTES = 4 * 1024 * 1024

#: 工具 schema 里 description 的体积上限。schema 每次调用都要重发一遍，
#: 一个 20k 字符的 description 会把轨迹文件整个撑大，而它极少是重放的关键；
#: 参数定义（properties/required）**一个字都不截**，否则重放出来的请求不等价。
_TOOL_DESC_MAX = 2000

#: 判定"未配置 Key"的退出码 —— 与"跑完了但有失败"（1）区分开，
#: 否则 CI 上"没有 Key"会被误当成"模型退化了"。
EXIT_NO_CREDENTIALS = 2
EXIT_HAS_FAILURES = 1
EXIT_OK = 0

#: 敏感字段名（小写）。与 ``trace.py`` 的语义保持一致：命中就整条丢弃 ——
#: 密钥不该有机会进磁盘。
_SENSITIVE_KEYS = (
    "api_key", "apikey", "api-key", "authorization", "auth", "token",
    "password", "passwd", "secret", "cookie", "session_key", "private_key",
    "access_key", "credential",
)

#: 白名单：名字里含敏感词、但**不是**凭据的字段。
#: 为什么必须有：``token``/``auth`` 这类词根会误伤正常字段，而重放恰恰需要
#: 它们 —— ``max_tokens`` 被抹掉就重建不出请求，``usage.prompt_tokens``
#: 被抹掉就无法比较用量差异。误伤的具体表现是"记录里有这个键但值是 ***"，
#: 排查起来极其费时（看起来像脱敏过度，实际是名字匹配太宽）。
_NOT_SENSITIVE = frozenset({
    "max_tokens", "max_output_tokens", "max_completion_tokens", "min_tokens",
    "usage", "token_usage", "total_tokens", "prompt_tokens",
    "completion_tokens", "input_tokens", "output_tokens", "cached_tokens",
    "tokens", "n_tokens", "token_count", "tokenizer", "author", "authors",
})


def _is_sensitive_key(key: Any) -> bool:
    """字段名是否指向凭据（白名单优先，避免误伤 ``max_tokens`` 这类正常字段）。"""
    k = str(key).lower()
    if k in _NOT_SENSITIVE:
        return False
    return any(s in k for s in _SENSITIVE_KEYS)


# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════


def _cfg() -> dict[str, Any]:
    """取重放记录开关（环境变量优先于配置字段）。

    配置项一律走 ``getattr``：``core/config.py`` 里没有 ``replay_*`` 字段时
    本模块照样工作（约束：不得修改 config.py，字段由后续版本补）。
    """
    out: dict[str, Any] = {
        "enabled": False,             # 默认关闭：完整提示词含客户数据
        "dir": str(traces_dir()),
        "max_file_bytes": 64 * 1024 * 1024,
        "rotate": True,
    }
    try:
        from automind.core.config import ExecutionConfig

        ex = ExecutionConfig()
        out.update({
            "enabled": bool(getattr(ex, "replay_capture", False)),
            "dir": str(getattr(ex, "replay_dir", "") or out["dir"]),
            "max_file_bytes": int(getattr(ex, "replay_max_file_bytes",
                                         out["max_file_bytes"])),
            # config 里若显式给了 replay_rotate=False，则"超限就停"（不要新文件）
            "rotate": bool(getattr(ex, "replay_rotate", True)),
        })
    except Exception:
        pass
    env_on = os.environ.get("AUTOMIND_REPLAY")
    if env_on is not None:
        out["enabled"] = env_on.strip().lower() not in ("0", "false", "off", "no", "")
    env_dir = os.environ.get("AUTOMIND_REPLAY_DIR")
    if env_dir:
        out["dir"] = env_dir
    env_max = os.environ.get("AUTOMIND_REPLAY_MAX_BYTES")
    if env_max and env_max.strip().isdigit():
        out["max_file_bytes"] = int(env_max.strip())
    env_rot = os.environ.get("AUTOMIND_REPLAY_ROTATE")
    if env_rot is not None:
        out["rotate"] = env_rot.strip().lower() not in ("0", "false", "off", "no")
    return out


# ═══════════════════════════════════════════════════════════════
# 脱敏（记录前过滤，而不是读取时才打码）
# ═══════════════════════════════════════════════════════════════


def _sanitize(value: Any, key: str = "", depth: int = 0) -> Any:
    """递归过滤敏感字段 + 密钥字符串，**不截断正文**。

    与 ``trace.py._sanitize`` 的关键差异：这里没有 4000 字符截断 —— 截断会让
    "重建请求"这件事直接失效。体积控制交给单条记录上限与文件轮转。
    """
    if depth > 24:                      # 环形引用/病态嵌套的兜底
        return "<嵌套过深，已省略>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if _is_sensitive_key(k):
                out[str(k)] = "***"
            else:
                out[str(k)] = _sanitize(v, str(k), depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize(v, key, depth + 1) for v in value]
    if isinstance(value, bytes):
        return _sanitize(value.decode("utf-8", "replace"), key, depth + 1)
    if isinstance(value, str):
        # 第二道防线：密钥可能被塞进普通 content（用户粘贴、工具回显）
        from automind.core.redact import redact_secrets

        return redact_secrets(value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    try:
        return _sanitize(str(value), key, depth + 1)
    except Exception:
        return "<不可序列化>"


def sanitize(value: Any) -> Any:
    """对外暴露的脱敏入口（读取轨迹时也用它复核一遍）。"""
    return _sanitize(value)


# ═══════════════════════════════════════════════════════════════
# 记录器
# ═══════════════════════════════════════════════════════════════


def _as_dict(obj: Any) -> dict[str, Any]:
    """把对象/命名空间转成 dict（支持 dict / pydantic / dataclass / 普通对象）。"""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    for attr in ("model_dump", "_asdict", "__dict__"):
        fn = getattr(obj, attr, None)
        if fn is None:
            continue
        try:
            data = fn() if callable(fn) else dict(fn)
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return {}


def _tool_calls_of(value: Any) -> list[dict[str, Any]]:
    """把响应的 tool_calls 规范化成 ``[{"id","name","arguments"}]``。"""
    out: list[dict[str, Any]] = []
    for tc in (value or []):
        d = _as_dict(tc)
        name = d.get("name") or d.get("tool_name") or ""
        args = d.get("arguments", d.get("args", {}))
        out.append({
            "id": str(d.get("id") or ""),
            "name": str(name),
            "arguments": args if isinstance(args, dict) else {"_raw": str(args)},
        })
    return out


def _tool_schema_tools(tools: Any) -> list[dict[str, Any]]:
    """工具 schema 规范化 —— 只压 description，参数定义原样保留。"""
    out: list[dict[str, Any]] = []
    for t in (tools or []):
        if not isinstance(t, dict):
            d = _as_dict(t)
        else:
            d = t
        # 兼容 OpenAI 外层包装 {"type":"function","function":{...}}
        if "function" in d and isinstance(d.get("function"), dict):
            d = d["function"]
        name = str(d.get("name") or "")
        if not name:
            continue
        desc = str(d.get("description") or "")
        if len(desc) > _TOOL_DESC_MAX:
            desc = desc[:_TOOL_DESC_MAX] + f"…[schema 描述截断，原 {len(desc)} 字符]"
        out.append({
            "name": name,
            "description": desc,
            "parameters": d.get("parameters", d.get("input_schema", {})) or {},
        })
    return out


def split_generate_args(args: tuple[Any, ...],
                        kwargs: dict[str, Any]) -> tuple[Any, Any]:
    """从 ``generate(*args, **kwargs)`` 的调用现场取出 (messages, tools)。

    为什么要这个函数：接线点位于 ``llm.py`` 的通用包装器里，那里只有
    ``*a, **k``（不得为记录而重写各 provider 的签名）。因此这里按位置/关键字
    两种形态各试一次，取不到就返回空 —— 记录不到内容也好过记错内容或抛异常。
    """
    messages = kwargs.get("messages")
    if messages is None and args and isinstance(args[0], (list, tuple)):
        messages = args[0]
    tools = kwargs.get("tools")
    if tools is None and len(args) > 1 and isinstance(args[1], (list, tuple)):
        tools = args[1]
    return messages or [], tools


class ReplayRecorder:
    """把每次 LLM 调用写成一行可重放的 JSON。

    线程安全：写入与计数用 ``threading.Lock`` 串行化（服务端事件循环 + 后台
    线程都可能调用；临界区只是几次字典操作，成本可忽略）。

    与 ``TraceRecorder`` 一样**不持有文件句柄**（追加即开即关）：Windows 上
    长期持有句柄会让目录无法删除/重命名，而重放轨迹需要能被随时打包带走。
    """

    def __init__(self, root: str | Path | None = None, enabled: bool | None = None,
                 max_file_bytes: int | None = None, rotate: bool | None = None) -> None:
        cfg = _cfg()
        self.root = Path(root or cfg["dir"])
        #: 开关是热路径上唯一会被读到的字段（关闭时零成本返回）
        self.enabled = cfg["enabled"] if enabled is None else bool(enabled)
        self.max_file_bytes = int(max_file_bytes or cfg["max_file_bytes"])
        self.rotate = bool(cfg["rotate"] if rotate is None else rotate)
        self._lock = threading.Lock()
        self._written: dict[str, int] = {}
        self._skipped: dict[str, int] = {}
        #: ``session/run`` → 当前正在写的轮转序号（"1" = 未轮转）
        self._base: dict[str, str] = {}
        #: 便于自检与报告：最近一次落盘的文件路径
        self.last_path: str = ""
        self.write_errors = 0

    # ── 路径 ────────────────────────────────────────────────

    @staticmethod
    def _safe(part: str) -> str:
        keep = [c for c in str(part) if c.isalnum() or c in "-_.@"]
        return ("".join(keep) or "default")[:64]

    def path_for(self, session_id: str, run_id: str, seq: int = 1) -> Path:
        """``<root>/<session>/<run>.replay.jsonl``（轮转后带 ``.N`` 后缀）。

        注意轮转文件名是 ``<run>.replay.2.jsonl`` —— 匹配它们必须用
        ``*.replay*.jsonl`` 而不是 ``*.replay.jsonl``：glob 的 ``*`` **不跨**
        ``.``，后者永远只能看到第一个文件（这个坑会让"轮转后剩下 11 个文件"、
        "统计只算 1 个文件"，而且看起来像轮转没生效）。
        """
        name = f"{self._safe(run_id)}.replay.jsonl"
        if seq > 1:
            name = f"{self._safe(run_id)}.replay.{seq}.jsonl"
        return self.root / self._safe(session_id) / name

    #: 匹配本模块产出的**全部**轨迹文件（含轮转分片）
    GLOB = "*.replay*.jsonl"

    # ── 记录 ────────────────────────────────────────────────

    def record(self, session_id: str, run_id: str, call: dict[str, Any]) -> str:
        """追加一条调用记录；返回落盘路径（禁用/失败时返回空串）。"""
        if not self.enabled:
            return ""
        sid, rid = session_id or "default", run_id or "default"
        base = f"{self._safe(sid)}/{self._safe(rid)}"
        with self._lock:
            seq = int(self._base.get(f"{base}#seq", "1") or 1)
            path = self.path_for(sid, rid, seq)
            key = str(path)

            body = {
                "ts": round(time.time(), 3),
                "session_id": sid,
                "run_id": rid,
                "type": "llm_call",
                # 调用序号：本文件内第几条（轮转后从 1 重新开始）——
                # 重放报告要能指回"轨迹里的第几条"，序号必须落在文件里而不是靠行号猜
                "seq": self._count_in(path) + 1,
                **_sanitize(call),
            }
            line = self._dumps(body)
            size = len(line.encode("utf-8")) + 1
            if size > self.max_file_bytes:
                # 单条就超上限（病态大 payload）：按比例截断并**显式标注**，
                # 因为被截断的记录已经不能再"完整重建请求"了。
                line = self._dumps(self._truncate_record(body, self.max_file_bytes))
                size = len(line.encode("utf-8")) + 1
            # 基准字节数：本进程没写过这个文件时以**磁盘上的真实大小**起步
            # （可能是上一次进程留下的），否则用累计值 —— 两者混淆会让
            # "文件多大"算错，轮转就会在该发生的时候不发生。
            used = self._written.get(key)
            if used is None:
                used = self._size_of(path)
                self._written[key] = used

            if used and used + size > self.max_file_bytes:
                # 超上限：先**写明**"从这里开始不再记录"，而不是默默丢数据
                self._skipped[key] = self._skipped.get(key, 0) + 1
                if self._skipped[key] == 1:
                    self._append(path, self._dumps({
                        "ts": round(time.time(), 3),
                        "session_id": sid, "run_id": rid,
                        "type": "replay_truncated",
                        "reason": "max_file_bytes",
                        "limit_bytes": self.max_file_bytes,
                        "message": (f"单文件已达上限 {self.max_file_bytes} 字节，"
                                    f"后续调用只计数不再写入正文。"),
                    }))
                if not self.rotate:
                    return str(path)
                # 轮转：换一个新文件继续记全 —— 评测要的是完整轨迹，
                # "截断后继续丢"会让一次长任务的后半程全部无法重放。
                prev = path
                seq += 1
                self._base[f"{base}#seq"] = str(seq)
                path = self.path_for(sid, rid, seq)
                key = str(path)
                self._written[key] = self._size_of(path)
                self._append(path, self._dumps({
                    "ts": round(time.time(), 3),
                    "session_id": sid, "run_id": rid,
                    "type": "replay_rotated",
                    "to": path.name,
                    "skipped_in_previous": self._skipped.get(str(prev), 0),
                    "message": (f"上一文件 {prev.name} 超过 {self.max_file_bytes} 字节，"
                                f"已轮转到 {path.name} 继续记录完整轨迹。"),
                }))
                used = self._written[key]

            ok = self._append(path, line)
            if not ok:
                return ""
            self._written[key] = used + size
        self.last_path = key
        return key

    @staticmethod
    def _dumps(obj: dict[str, Any]) -> str:
        try:
            return json.dumps(obj, ensure_ascii=False, default=str)
        except Exception as e:                   # 序列化失败不该丢事件
            return json.dumps({"ts": round(time.time(), 3),
                               "type": "replay_encode_error", "error": str(e)},
                              ensure_ascii=False)

    @staticmethod
    def _truncate_record(body: dict[str, Any], limit: int) -> dict[str, Any]:
        """单条记录超限时按比例夹正文，并**显式标注**已截断。"""
        req = body.get("request") or {}
        msgs = req.get("messages") or []
        kept_bytes = sum(len(str(m.get("content") or "").encode("utf-8")) for m in msgs)
        body["truncated"] = True
        body["truncate_reason"] = (
            f"单条记录超过 {limit} 字节（消息正文约 {kept_bytes} 字节），"
            f"已按比例截断；本条**不能**完整重建请求。")
        if msgs:
            # 每 4 字节约 1 字符（中文占 3 字节）；留出 1/2 的预算给正文
            budget = max(1000, limit // 8)
            for m in msgs:
                c = str(m.get("content") or "")
                if len(c.encode("utf-8")) > budget:
                    m["content"] = c[:budget] + f"…[replay 截断，原 {len(c)} 字符]"
        return body

    def _append(self, path: Path, line: str) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            return True
        except Exception as e:
            self.write_errors += 1
            if self.write_errors <= 3:           # 磁盘故障时别刷屏
                logger.warning("replay_write_failed", path=str(path), error=str(e))
            return False

    @staticmethod
    def _size_of(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    @staticmethod
    def _count_in(path: Path) -> int:
        """本文件已有多少条记录（轮转后 seq 重新计数）。"""
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return sum(1 for line in f if line.strip())
        except OSError:
            return 0

    # ── 自检 ────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        """重放轨迹占用概览（与 ``trace.py`` 同名方法语义一致）。

        ``replay_*`` 三个字段是本模块新增的：开启了完整提示词落盘之后，
        "占了多大磁盘"必须能被一眼看到 —— 否则用户会直到磁盘写满才发现
        ``AUTOMIND_REPLAY=1`` 一直开着。
        """
        files, total = 0, 0
        if self.root.exists():
            for p in self.root.rglob(self.GLOB):
                files += 1
                total += self._size_of(p)
        return {"enabled": self.enabled, "dir": str(self.root), "files": files,
                "bytes": total, "write_errors": self.write_errors,
                "skipped_calls": sum(self._skipped.values()),
                "replay_enabled": self.enabled, "replay_files": files,
                "replay_bytes": total}


#: 进程级默认记录器（懒构造；``AUTOMIND_DATA_DIR`` 变化时自动重建）
_default: ReplayRecorder | None = None
_default_identity: str = ""


def _identity() -> str:
    """当前数据目录对应的身份指纹 —— 变了说明换数据目录了，得重建记录器。"""
    return str(traces_dir())


def get_recorder() -> ReplayRecorder:
    """返回进程级重放记录器。

    生命周期：默认实例是**懒构造**的，并在 ``AUTOMIND_DATA_DIR`` 变化时重建
    ——评测 runner 会为每次运行设置隔离的数据目录，缓存住旧目录的记录器会把
    轨迹写到用户仓库里（甚至写到上一次评测的临时目录里）。

    身份指纹只跟**数据目录**走，不跟开关走：``configure_recorder()`` 显式
    指定过开关与目录（测试、服务端启动参数）时必须尊重那个决定，否则
    "注入的临时目录 + 小上限 + enabled=True" 会被环境变量悄悄顶掉，
    表现为"开了开关却什么都没写 / 上限不生效"这类最难查的问题。
    """
    global _default, _default_identity
    want = _identity()
    if _default is None or _default_identity != want:
        _default = ReplayRecorder()
        _default_identity = want
    return _default


def configure_recorder(root: str | Path | None = None,
                       enabled: bool | None = None,
                       max_file_bytes: int | None = None,
                       rotate: bool | None = None) -> ReplayRecorder:
    """重建进程级记录器（测试/服务端启动时用：把轨迹写到指定目录）。

    ``root`` 只决定**写到哪**；身份指纹仍取当前数据目录，见
    :func:`get_recorder` 的说明。
    """
    global _default, _default_identity
    _default = ReplayRecorder(root=root, enabled=enabled,
                              max_file_bytes=max_file_bytes, rotate=rotate)
    _default_identity = _identity()
    return _default


def record_call(backend: Any = None, response: Any = None, elapsed: float = 0.0,
                messages: Any = None, tools: Any = None,
                error: Any = None) -> str:
    """记录一次已完成的 LLM 调用；返回落盘路径（未开启时返回空串）。

    **热路径**：关闭时这里只有一次属性读取 + 一次布尔判断。接线点见
    ``core/llm.py`` 的 ``generate`` 包装器（唯一一处）。

    ``messages`` / ``tools`` 由调用方（``llm.py``）从调用现场取出后传入；
    取不到时本函数会退而读后端自带的 ``_tools``（provider 的真实行为是
    ``tools or self._tools``，这样记录的仍是**实际发出**的 schema）。
    """
    rec = get_recorder()
    if not rec.enabled:                      # ← 关闭时到此为止，零成本
        return ""
    try:
        from automind.core import session_ctx

        sid = session_ctx.session_id() or "default"
        rid = session_ctx.run_id() or "default"
    except Exception:
        sid, rid = "default", "default"

    cfg = getattr(backend, "config", None)
    eff_tools = tools if tools else getattr(backend, "_tools", None)
    try:
        payload = {
            "request": {
                "provider": str(getattr(cfg, "provider", "") or ""),
                "model": str(getattr(backend, "_model", "")
                             or getattr(cfg, "model", "") or ""),
                "api_base": str(getattr(backend, "api_base", "") or ""),
                "temperature": getattr(cfg, "temperature", None),
                "max_tokens": getattr(cfg, "max_tokens", None),
                "top_p": getattr(cfg, "top_p", None),
                "stop": None,
                "extra_body": getattr(cfg, "extra_body", None) or {},
                "messages": list(messages or []),
                "tools": _tool_schema_tools(eff_tools),
            },
            "response": {
                "text": str(getattr(response, "text", "") or ""),
                "tool_calls": _tool_calls_of(getattr(response, "tool_calls", None)),
                "finish_reason": str(getattr(response, "finish_reason", "") or ""),
                "prompt_tokens": int(getattr(response, "prompt_tokens", 0) or 0),
                "completion_tokens": int(getattr(response, "completion_tokens", 0) or 0),
                "model": str(getattr(response, "model", "") or ""),
                "provider": str(getattr(response, "provider", "") or ""),
            },
            # usage 与 response.*_tokens 是同一份数据的两种位置：重放报告读
            # usage，人读文件看 response。少了 usage，重放时"usage 差异"
            # 会永远显示成"旧值 0"，看起来像成本暴涨。
            "usage": {
                "prompt_tokens": int(getattr(response, "prompt_tokens", 0) or 0),
                "completion_tokens": int(getattr(response, "completion_tokens", 0) or 0),
                "total_tokens": int(getattr(response, "prompt_tokens", 0) or 0)
                + int(getattr(response, "completion_tokens", 0) or 0),
                "model": str(getattr(response, "model", "") or ""),
                "provider": str(getattr(response, "provider", "") or ""),
            },
            "elapsed_seconds": round(float(elapsed or 0.0), 4),
            "error": str(error)[:2000] if error else "",
        }
    except Exception as e:
        logger.warning("replay_build_failed", error=str(e))
        return ""
    return rec.record(sid, rid, payload)


# ═══════════════════════════════════════════════════════════════
# 读取
# ═══════════════════════════════════════════════════════════════


def read_records(path: str | Path) -> list[dict[str, Any]]:
    """读取一个 ``.replay.jsonl``（跳过损坏行，保留所有事件类型）。

    读取时**再过一遍脱敏**：轨迹文件可能是别人给的/被手工改过的，
    "落盘时已过滤"不能作为重新发请求时的唯一保证。
    """
    p = Path(path)
    out: list[dict[str, Any]] = []
    with open(p, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                logger.warning("replay_bad_line", path=str(p), line=i)
                continue
            if not isinstance(obj, dict):
                continue
            obj.setdefault("_line", i)
            out.append(sanitize(obj))
    return out


def calls_of(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从记录里筛出可重放的 LLM 调用（按原顺序）。"""
    return [r for r in records if r.get("type") == "llm_call" and r.get("request")]


def skipped_of(records: list[dict[str, Any]]) -> int:
    """统计轨迹里"因超限被丢弃"的调用次数（用于在报告里如实声明不完整）。"""
    n = 0
    for r in records:
        if r.get("type") == "replay_rotated":
            n += int(r.get("skipped_in_previous") or 0)
    return n


# ═══════════════════════════════════════════════════════════════
# 重放
# ═══════════════════════════════════════════════════════════════


def similarity(a: str, b: str) -> float:
    """两条回复的文本相似度 0~1（字符级，空对空视为 1.0）。

    为什么不要求"完全一致"：LLM 采样本身不确定，逐字比对只会永远红灯。
    这里要回答的是"提示词/模型改动后回答是否**还是原来的意思范围**"，
    因此给一个可设阈值的连续分，而不是真假二值。
    """
    a, b = a or "", b or ""
    if not a and not b:
        return 1.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def compare_calls(expected: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    """逐条响应对齐情况：文本相似度 / tool_calls 名称集合 / usage 差异。

    ``expected`` 传整条记录：响应用量与顶层 ``usage`` 两处都写过同一份数据
    （见 :func:`record_call`），这里取顶层那份作准 —— 它是重放报告与外部
    工具读取的稳定位置。
    """
    exp_resp = expected.get("response") or expected
    exp_usage = expected.get("usage") or exp_resp.get("usage") or {}
    act_usage = actual.get("usage") or {}
    exp_names = [tc.get("name", "") for tc in (exp_resp.get("tool_calls") or [])]
    act_names = [tc.get("name", "") for tc in (actual.get("tool_calls") or [])]
    return {
        "text_similarity": round(similarity(str(exp_resp.get("text") or ""),
                                             str(actual.get("text") or "")), 4),
        "text_equal": str(exp_resp.get("text") or "") == str(actual.get("text") or ""),
        "expected_tools": exp_names,
        "actual_tools": act_names,
        "tool_names_match": sorted(set(exp_names)) == sorted(set(act_names)),
        "tool_order_match": exp_names == act_names,
        "prompt_tokens_delta": int(act_usage.get("prompt_tokens", 0) or 0)
        - int(exp_usage.get("prompt_tokens", 0) or 0),
        "completion_tokens_delta": int(act_usage.get("completion_tokens", 0) or 0)
        - int(exp_usage.get("completion_tokens", 0) or 0),
        "expected_finish_reason": str(exp_resp.get("finish_reason") or ""),
        "actual_finish_reason": str(actual.get("finish_reason") or ""),
    }


def _summary_of(rec: dict[str, Any]) -> dict[str, Any]:
    """一次调用的摘要（dry-run 与报告共用）。"""
    req = rec.get("request") or {}
    msgs = req.get("messages") or []
    tools = req.get("tools") or []
    last_user = ""
    for m in reversed(msgs):
        if str(m.get("role")) == "user":
            last_user = str(m.get("content") or "")
            break
    return {
        "line": rec.get("_line"),
        "seq": rec.get("seq"),
        "provider": req.get("provider", ""),
        "model": req.get("model", ""),
        "messages": len(msgs),
        "tools": len(tools),
        "request_chars": len(json.dumps(req, ensure_ascii=False, default=str)),
        "temperature": req.get("temperature"),
        "max_tokens": req.get("max_tokens"),
        "last_user_preview": last_user[:120].replace("\n", " "),
    }


def dry_run_report(records: list[dict[str, Any]], limit: int | None = None) -> dict[str, Any]:
    """构造 dry-run 报告（不联网、不需要 Key）。"""
    calls = calls_of(records)
    if limit:
        calls = calls[:limit]
    skipped = skipped_of(records)
    return {
        "mode": "dry-run",
        "planned_calls": len(calls),
        "skipped_in_trace": skipped,
        "complete": skipped == 0,
        "calls": [_summary_of(r) for r in calls],
    }


def build_backend(request: dict[str, Any], override_model: str = "",
                  provider_override: str = "") -> tuple[Any, str]:
    """按记录里的 provider/model 建一个后端；返回 (backend, 说明)。

    缺 Key / provider 不可用时**抛异常**，由调用方如实报错退出 ——
    绝不"静默跳过"或假装成功（那会让 CI 上的一次退化看起来像一次通过）。
    """
    from automind.core.config import LLMProviderConfig
    from automind.core.exceptions import LLMAuthenticationError
    from automind.core.llm import LLMBackendFactory
    from automind.core.provider_resolver import ENV_KEY_MAP

    provider = (provider_override or str(request.get("provider") or "")).strip()
    model = (override_model or str(request.get("model") or "")).strip()
    if not provider:
        provider = "openai"
    if not model:
        raise LLMAuthenticationError(
            "轨迹里没有模型名，且未指定 --model。请用 --model 指定要重放的模型。")

    env_var = ENV_KEY_MAP.get(provider, "")
    api_key = os.environ.get(env_var, "") if env_var else ""
    if not api_key:
        # 退回当前配置里的 Key（同机重放时用户可能只在配置里填过）
        try:
            from automind.core.config import AgentConfig

            api_key = AgentConfig().llm.api_key or ""
        except Exception:
            api_key = ""
    if not api_key and provider != "ollama":
        # Ollama 是本地模型，没有也不需要 Key（OllamaProvider 自己就这么认为），
        # 一刀切要求 Key 会让"离线重放"这条最有价值的路被误判成缺配置。
        raise LLMAuthenticationError(
            f"未配置 API Key（provider='{provider}'"
            + (f"，期望环境变量 {env_var}" if env_var else "")
            + "）。重放需要真实调用模型，请先配置 Key 或改用 --dry-run。")

    cfg = LLMProviderConfig(
        provider=provider, model=model, api_key=api_key,
        max_tokens=int(request.get("max_tokens") or 8192),
        temperature=float(request.get("temperature") if request.get("temperature")
                          is not None else 0.7),
    )
    return LLMBackendFactory.create(provider, cfg), f"{provider}/{model}"


async def replay_file(path: str | Path, model: str = "", limit: int | None = None,
                      provider: str = "", backend_factory: Any = None) -> dict[str, Any]:
    """真跑：按记录重新请求，逐条比对新旧响应。返回汇总（不打印、不退出）。"""
    records = read_records(path)
    calls = calls_of(records)
    if limit:
        calls = calls[:limit]
    if not calls:
        raise ValueError(f"{path} 里没有可重放的 llm_call 记录。")

    factory = backend_factory or build_backend
    results: list[dict[str, Any]] = []
    target = ""
    for rec in calls:
        req = rec.get("request") or {}
        summary = _summary_of(rec)
        try:
            backend, target = factory(req, model, provider)
            started = time.monotonic()
            resp = await backend.generate(req.get("messages") or [],
                                          tools=req.get("tools") or None)
            elapsed = time.monotonic() - started
            actual = {
                "text": getattr(resp, "text", "") or "",
                "tool_calls": _tool_calls_of(getattr(resp, "tool_calls", None)),
                "finish_reason": getattr(resp, "finish_reason", "") or "",
                "usage": {
                    "prompt_tokens": int(getattr(resp, "prompt_tokens", 0) or 0),
                    "completion_tokens": int(getattr(resp, "completion_tokens", 0) or 0),
                },
            }
            close = getattr(backend, "close", None)
            if callable(close):
                try:
                    await close()
                except Exception:
                    pass
            results.append({
                "ok": True, "summary": summary, "elapsed_seconds": round(elapsed, 3),
                **compare_calls(rec, actual),
            })
        except Exception as e:
            results.append({"ok": False, "summary": summary, "error": str(e)[:1000],
                            "text_similarity": 0.0, "tool_names_match": False})

    ok = [r for r in results if r["ok"]]
    return {
        "mode": "replay",
        "file": str(path),
        "target": target,
        "total": len(results),
        "failed_calls": len(results) - len(ok),
        "skipped_in_trace": skipped_of(records),
        "avg_similarity": round(
            sum(r.get("text_similarity", 0.0) for r in ok) / len(ok), 4) if ok else 0.0,
        "tool_names_match_rate": round(
            sum(1 for r in ok if r.get("tool_names_match")) / len(ok), 4) if ok else 0.0,
        "results": results,
    }


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════


def _iter_trace_files(target: Path) -> Iterator[Path]:
    """按文件名顺序展开轮转出来的同一次运行的轨迹。"""
    if target.is_file():
        yield target
        return
    if target.is_dir():
        yield from sorted(target.glob(ReplayRecorder.GLOB))


def _print_dry_run(report: dict[str, Any]) -> None:
    print(f"[dry-run] 将重放 {report['planned_calls']} 次调用（不联网、不需要 API Key）")
    if report["skipped_in_trace"]:
        print(f"[dry-run] 注意：轨迹里有 {report['skipped_in_trace']} 次调用因超限未落盘，"
              f"本次重放**不完整**。")
    for i, c in enumerate(report["calls"], 1):
        print(f"  #{i:<3} {c['provider']}/{c['model']}  "
              f"messages={c['messages']} tools={c['tools']} "
              f"request≈{c['request_chars']}字符 "
              f"temp={c['temperature']} max_tokens={c['max_tokens']}")
        if c["last_user_preview"]:
            print(f"        ↳ 末条用户消息: {c['last_user_preview']}")


def _print_replay(report: dict[str, Any]) -> None:
    print(f"[replay] 目标模型: {report['target']}   调用数: {report['total']}")
    for i, r in enumerate(report["results"], 1):
        s = r["summary"]
        if not r["ok"]:
            print(f"  #{i:<3} ✗ 调用失败: {r['error']}")
            continue
        flag = "✓" if r.get("text_equal") and r.get("tool_names_match") else "≈"
        print(f"  #{i:<3} {flag} 相似度={r['text_similarity']:.3f} "
              f"tools 期望={r['expected_tools']} 实际={r['actual_tools']} "
              f"名称一致={r['tool_names_match']} "
              f"prompt Δ={r['prompt_tokens_delta']:+d} "
              f"completion Δ={r['completion_tokens_delta']:+d} "
              f"({r['elapsed_seconds']}s)  ← {s['model']} / {s['last_user_preview'][:40]}")
    print(f"[replay] 平均文本相似度 {report['avg_similarity']:.3f}　"
          f"tool_calls 名称一致率 {report['tool_names_match_rate']:.3f}　"
          f"调用失败 {report['failed_calls']} 次")
    if report["skipped_in_trace"]:
        print(f"[replay] 注意：轨迹缺 {report['skipped_in_trace']} 次调用（超限未落盘），"
              f"结论仅覆盖已记录部分。")


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：退出码 0=成功，1=有调用失败，2=没有可用凭据/输入。"""
    ap = argparse.ArgumentParser(
        prog="python -m automind.core.replay",
        description="重放一份可重放轨迹（.replay.jsonl）")
    ap.add_argument("file", help="轨迹文件路径（或含轨迹文件的目录）")
    ap.add_argument("--model", default="", help="覆盖记录里的模型名")
    ap.add_argument("--provider", default="", help="覆盖记录里的提供商名")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印将重放多少次调用与摘要，不联网")
    ap.add_argument("--limit", type=int, default=0, help="只重放前 N 次调用")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    args = ap.parse_args(argv)

    target = Path(args.file)
    if not target.exists():
        print(f"错误：找不到轨迹文件 {target}", file=sys.stderr)
        return EXIT_NO_CREDENTIALS

    files = list(_iter_trace_files(target))
    if not files:
        print(f"错误：{target} 下没有 *.replay.jsonl", file=sys.stderr)
        return EXIT_NO_CREDENTIALS

    all_records: list[dict[str, Any]] = []
    for f in files:
        all_records.extend(read_records(f))
    limit = args.limit or None

    if args.dry_run:
        report = dry_run_report(all_records, limit)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            _print_dry_run(report)
        return EXIT_OK

    # 真跑：先确认凭据，避免跑一半才失败（也避免把"没 Key"记成"模型退化"）
    calls = calls_of(all_records)
    if limit:
        calls = calls[:limit]
    if not calls:
        print("错误：轨迹里没有可重放的 llm_call 记录。", file=sys.stderr)
        return EXIT_NO_CREDENTIALS
    try:
        build_backend(calls[0].get("request") or {}, args.model, args.provider)
    except Exception as e:
        print(f"错误：{e}", file=sys.stderr)
        return EXIT_NO_CREDENTIALS

    try:
        report = asyncio.run(replay_file(files[0], model=args.model, limit=limit,
                                         provider=args.provider))
    except Exception as e:
        print(f"错误：重放失败：{e}", file=sys.stderr)
        return EXIT_HAS_FAILURES
    # 多文件（轮转）时逐文件重放并合并结果
    if len(files) > 1:
        extra: list[dict[str, Any]] = []
        for f in files[1:]:
            try:
                extra.extend(asyncio.run(replay_file(
                    f, model=args.model, limit=limit, provider=args.provider))["results"])
            except Exception as e:
                extra.append({"ok": False, "summary": {"model": "", "last_user_preview": ""},
                              "error": str(e)[:500], "text_similarity": 0.0,
                              "tool_names_match": False})
        report["results"].extend(extra)
        report["total"] = len(report["results"])
        report["failed_calls"] = sum(1 for r in report["results"] if not r["ok"])
        report["file"] = ", ".join(str(f) for f in files)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_replay(report)
    return EXIT_HAS_FAILURES if report["failed_calls"] else EXIT_OK


if __name__ == "__main__":       # pragma: no cover - CLI 入口
    raise SystemExit(main())
