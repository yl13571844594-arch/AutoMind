"""治理审计账本 —— 可持久、可验证、可解释的执行治理证据链（v1.11.19）。

## 为什么需要它

权限审批（ReAct / Plan）此前只在**内存**里留 ``PermissionAuditEntry``：进程一重启
全丢。于是 Agent 在无人值守通道（工作流、技能、直接 dispatch）上到底放行了什么、
拦截了什么、有没有绕过审批的旁路，事后**没有任何证据**。B 端场景里这正是
"出了事说不清"的根源。

本模块把治理决策与执行结果写成**按序哈希链**的 JSONL 账本：

  · 一条记录一行 JSON（JSONL）—— 可 ``tail -f``，单行损坏不影响其余行；
  · 每条 ``hash`` = sha256(去掉 hash 后的**紧凑规范 JSON**：sort_keys=True、
    ensure_ascii=False、separators=(",", ":")），``prev`` = 上一条的 hash；
  · 任何人改动/删除/插入中间记录，``verify_chain()`` 都能指到断点（broken_at）；
  · ``state.json`` 记录 ``{seq, last_hash, file}``，保证**跨文件轮转、跨进程重启**
    的链与序号连续；启动时以磁盘上最后一条记录为准（state.json 落后一条也自愈）。

## 落盘位置与轮转

  ``<AUTOMIND_DATA_DIR>/governance/ledger-YYYYMMDD.jsonl``（开发模式即
  ``./.automind/governance/``）；单文件超过 8MB 或跨天时轮转，同日轮转文件名为
  ``ledger-YYYYMMDD-2.jsonl``、``-3.jsonl`` ……；最多保留 10 个文件，超出删除最旧。

## 两类记录（字段固定，供服务端 / 前端消费）

公共字段::

    seq:int, ts:float(epoch), kind:"decision"|"execution", session:str, tool:str,
    tier:str, risk:int, decision:str, reason:str, source:str, governed:bool,
    params:dict(已脱敏+截断), call_id:str, duration_ms:float,
    success:bool|None, error:str(<=300), prev:str, hash:str

  · ``kind="decision"``：治理决策（allow / deny / approved / denied / modified /
    error / allow_unattended / deny_unattended …）；
  · ``kind="execution"``：实际执行结果（``decision="executed"``，成败看
    ``success`` / ``error``；execution 记录不携带参数）。
  · ``source`` 取值：direct（默认）/ react / plan / workflow / skill。

## 安全与健壮性（硬要求）

  · **脱敏**：params 的敏感键名（api_key/token/password/secret/…）整值打码，
    其余值字符串化后先过 ``core.redact.redact_secrets``（识别内嵌的 sk- 等），
    再截断到 200 字符；最多 12 个键。文件内容 / 密钥明文不落盘。
  · **fail-open**：任何写入故障只记 ``logger.warning("governance_write_failed", …)``，
    返回 None，**绝不向调用方抛异常**影响任务执行；读取故障返回空结果。
  · **并发**：单进程内 ``threading.Lock`` 保护 seq/hash 状态与按行 append。
  · **可关闭**：环境变量 ``AUTOMIND_GOVERNANCE_AUDIT=0``（false/no/off 同义）
    时 ``enabled()`` 为 False，不产生任何写入。其它可调环境变量：
    ``AUTOMIND_GOVERNANCE_DIR``（账本目录覆盖）、
    ``AUTOMIND_GOVERNANCE_MAX_FILE_BYTES``（默认 8MB）、
    ``AUTOMIND_GOVERNANCE_MAX_FILES``（默认 10）。

## 与执行器的分工

  · ReAct / Plan 已经做过**人工审批**，由执行器在审批分支调用本模块记账，
    并把来源绑定为 ``react`` / ``plan``；
  · 分派级闸门（``tools/base.py`` 的 dispatch）只对**未经审批门控**的通道
    （direct / workflow / skill）生效：``source`` 属于 react/plan 时不再二次审批。
    闸门策略见 :func:`make_unattended_governor`。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any

from automind.core.logging import get_logger
from automind.core.paths import data_dir
from automind.core.redact import redact_secrets

logger = get_logger("automind.core.governance")

# ═══════════════════════════════════════════════════════════
# 常量与可调参数
# ═══════════════════════════════════════════════════════════

#: 单文件上限（超过即轮转）。8MB ≈ 2 万条记录，足够一天的高频任务；再大
#: 单次读取（摘要 / 校验）就会变慢，而取证粒度并不需要那么大。
_DEFAULT_MAX_FILE_BYTES = 8 * 1024 * 1024
#: 最多保留的历史文件数（删除最旧）。10 × 8MB = 80MB 封顶，不会吃满磁盘。
_DEFAULT_MAX_FILES = 10
#: 环境变量给的上限不能太小，否则一条记录还没写就"轮转"出无数文件。
_MIN_FILE_BYTES = 1024

_LEDGER_GLOB = "ledger-*.jsonl"
_STATE_FILE = "state.json"

#: 读末尾记录时回看的字节数 —— 一条记录通常 < 1KB，256KB 足够覆盖坏行重试
_TAIL_READ_BYTES = 256 * 1024
#: 读文件首条记录（判断文件在链中的位置）时最多看几行
_FIRST_SCAN_LINES = 5

#: params 里最多保留几个键 / 每个值最多多少字符（硬要求，防止文件内容落盘）
_PARAM_MAX_KEYS = 12
_PARAM_VALUE_MAX = 200
#: 报错信息上限（前端按 300 展示）
_ERROR_MAX = 300
#: 决策原因上限（原因是一句人话，不需要无上限）
_REASON_MAX = 500
#: 短字段（tool/session/call_id…）上限
_FIELD_MAX = 120
_SOURCE_MAX = 40
_TIER_MAX = 20
_DECISION_MAX = 40

#: 键名命中即整值打码 —— 与 ``core/trace.py`` 的敏感字段语义保持一致。
#: 光靠 ``redact_secrets`` 的文本正则不够：``{"password": "hunter2"}`` 里
#: 单独一个值没有 "password=" 前缀，正则是识别不出来的。
_SENSITIVE_KEYS = (
    "api_key", "apikey", "api-key", "authorization", "auth", "token",
    "password", "passwd", "secret", "cookie", "session_key", "private_key",
    "access_key", "credential", "bearer",
)

_REDACTED = "***REDACTED***"

#: 执行记录没有独立的风险评估入口：按工具声明等级折算一个基础分，
#: 让摘要里的 dangerous（tier=dangerous 或 risk>=80）仍有意义。
_TIER_BASE_RISK = {"safe": 10, "sensitive": 50, "dangerous": 90}

_DECISION_KIND = "decision"
_EXECUTION_KIND = "execution"
#: 视为"拒绝"的决策值（摘要 denied 计数）
_DENIED_DECISIONS = frozenset({"deny", "denied", "deny_unattended"})

#: 当前调用来源（ContextVar）：默认 direct（库调用 / 服务端直调）。
#: 执行器在真正执行工具的作用域内 bind 成 react / plan / workflow / skill，
#: dispatch 的分派级闸门据此判断"这次调用有没有经过人工审批门控"。
_source: ContextVar[str] = ContextVar("automind_governance_source", default="direct")


# ═══════════════════════════════════════════════════════════
# 来源上下文（ContextVar）
# ═══════════════════════════════════════════════════════════


def bind_source(source: str) -> Token:
    """把当前作用域的调用来源设为 ``source``，返回 token（用 :func:`reset_source` 恢复）。

    用法::

        token = bind_source("workflow")
        try:
            await registry.dispatch("terminal", ...)   # 账本 record.source == "workflow"
        finally:
            reset_source(token)

    用 ContextVar 而不是全局变量：并发任务（两个会话 / 子智能体）各自一份，
    互不覆盖 —— 与 ``core/session_ctx.py`` 的会话身份同一套机制。
    """
    return _source.set(str(source or "direct"))


def reset_source(token: Token) -> None:
    """恢复 :func:`bind_source` 之前的值（token 失效时静默忽略）。

    重复 reset（执行器 finally 里可能被调两次）在 CPython 里抛的是
    ``RuntimeError``（Token has already been used once），这里一并吞掉 ——
    来源恢复失败的好过抛异常打断真正的工具执行。
    """
    try:
        _source.reset(token)
    except (ValueError, LookupError, RuntimeError):
        pass


def current_source() -> str:
    """当前调用来源；未绑定时返回 ``"direct"``。"""
    try:
        return str(_source.get() or "direct")
    except Exception:                                     # pragma: no cover - 防御性
        return "direct"


# ═══════════════════════════════════════════════════════════
# 配置（目录 / 开关 / 上限）
# ═══════════════════════════════════════════════════════════

#: 进程级单例（懒构造：data_dir() 依赖环境变量，导入时机太早会取错目录）
_ledger: _Ledger | None = None
#: configure / reset_for_tests 指定的 root 与开关（None = 按 env / 默认值走）
_root_override: Path | None = None
_enabled_override: bool | None = None
#: 保护单例构造与配置切换（不与 _Ledger 的写锁混用，避免锁序问题）
_global_lock = threading.Lock()


def _env_text(name: str) -> str:
    try:
        return str(os.environ.get(name, "") or "").strip()
    except Exception:                                     # pragma: no cover - 防御性
        return ""


def _env_int(name: str, default: int, minimum: int) -> int:
    raw = _env_text(name)
    if not raw:
        return default
    try:
        return max(minimum, int(float(raw)))
    except (TypeError, ValueError):
        return default


def _default_root() -> Path:
    override = _env_text("AUTOMIND_GOVERNANCE_DIR")
    if override:
        return Path(override).expanduser()
    return data_dir() / "governance"


def _env_disabled() -> bool:
    """``AUTOMIND_GOVERNANCE_AUDIT`` 是否为假值（0/false/no/off/disabled）。"""
    raw = _env_text("AUTOMIND_GOVERNANCE_AUDIT").lower()
    return raw in ("0", "false", "no", "off", "disabled")


def enabled() -> bool:
    """账本是否启用。

    优先级：``AUTOMIND_GOVERNANCE_AUDIT=0``（硬关闭，谁也不能把它打开）→
    :func:`configure` / :func:`reset_for_tests` 的显式设置 → 默认 **True**。
    默认开启的理由：治理账本是"事后取证"能力，默认可关等于默认没有证据。
    """
    if _env_disabled():
        return False
    if _enabled_override is not None:
        return bool(_enabled_override)
    return True


def configure(root: str | Path | None = None, enabled: bool | None = None) -> None:
    """调整账本目录 / 开关（服务端或运维入口用；测试用 :func:`reset_for_tests`）。

    参数为 None 表示"该项保持现状"；下一次写入时按新配置重建账本实例（会从
    磁盘恢复 seq/hash，因此切换目录不会让链断掉）。
    """
    global _root_override, _enabled_override, _ledger
    with _global_lock:
        if root is not None:
            _root_override = Path(root).expanduser()
        if enabled is not None:
            _enabled_override = bool(enabled)
        _ledger = None


def reset_for_tests(root: str | Path | None = None, enabled: bool = True) -> None:
    """**仅供测试**：把账本重置到指定目录并立即重建实例（默认启用）。

    不传 ``root`` 时沿用当前配置（默认数据目录）；实例重建时会读取磁盘上的
    ``state.json`` / 最后一条记录，所以同一个 root 连续 reset 等价于"进程重启"，
    序号与哈希链保持连续（这正是重启连续性用例所需的模拟方式）。
    """
    global _root_override, _enabled_override, _ledger
    with _global_lock:
        _root_override = Path(root).expanduser() if root is not None else _root_override
        _enabled_override = bool(enabled)
        # 注意：不能用模块级 enabled() —— 上面这个同名参数会把它遮蔽掉
        # （v1.11.19 联调时真实踩到：TypeError: 'bool' object is not callable）。
        # 这里按与 enabled() 相同的优先级自己算一次：env 硬关闭 > 显式参数。
        effective = False if _env_disabled() else bool(enabled)
        _ledger = _Ledger(_root_override if _root_override is not None else _default_root(),
                          effective)


def _get_ledger() -> _Ledger:
    """取进程级账本（首次使用时构造，构造过程本身也 fail-open）。"""
    global _ledger
    if _ledger is None:
        with _global_lock:
            if _ledger is None:
                root = _root_override if _root_override is not None else _default_root()
                _ledger = _Ledger(root, enabled())
    return _ledger


# ═══════════════════════════════════════════════════════════
# 字段清洗（脱敏 / 截断）
# ═══════════════════════════════════════════════════════════


def _clip(text: Any, limit: int) -> str:
    """字符串化并截断到 ``limit`` 字符（超长补省略号，结果**不超过** limit）。"""
    body = text if isinstance(text, str) else str(text or "")
    limit = max(1, int(limit))
    if len(body) <= limit:
        return body
    return body[: limit - 1] + "…"


def _clean_text(text: Any, limit: int) -> str:
    """先脱敏再截断：截断不能早于脱敏，否则密钥被切成两半反而躲过正则。"""
    raw = text if isinstance(text, str) else str(text or "")
    try:
        raw = redact_secrets(raw)
    except Exception:                                     # pragma: no cover - 防御性
        pass
    return _clip(raw, limit)


def _stringify(value: Any) -> str:
    """把任意参数值变成**有界**字符串（不把整份文件内容读进内存再截断）。"""
    if isinstance(value, str):
        return value[:1000]
    if isinstance(value, (bytes, bytearray)):
        return bytes(value[:500]).decode("utf-8", "replace")
    if isinstance(value, (int, float, bool)) or value is None:
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False, default=str)[:1000]
    except Exception:                                     # pragma: no cover - 防御性
        try:
            return repr(value)[:1000]
        except Exception:
            return "<不可序列化>"


def _sanitize_params(params: Any) -> dict[str, str]:
    """脱敏 + 截断工具参数：最多 12 个键、每值 <= 200 字符。

    · 键名命中敏感词（password / token / secret …）→ 整值 ``***REDACTED***``；
    · 其余值字符串化后过 ``redact_secrets``（识别内嵌的 ``sk-…`` 等），再截断。
    · 所有值统一为字符串：账本要的是"参数长什么样"的取证摘要，不是可直接重放的
      原始入参 —— 保留原始类型反而会让大对象重新落盘。
    """
    if not isinstance(params, dict) or not params:
        return {}
    out: dict[str, str] = {}
    for key, value in list(params.items())[:_PARAM_MAX_KEYS]:
        name = _clip(str(key), 64)
        lowered = str(key).lower()
        if any(mark in lowered for mark in _SENSITIVE_KEYS):
            out[name] = _REDACTED
            continue
        out[name] = _clean_text(_stringify(value), _PARAM_VALUE_MAX)
    return out


def _tier_value(tier: Any) -> str:
    """把 PermissionTier 枚举 / 字符串归一化成 tier 字段（safe/sensitive/dangerous）。"""
    raw = getattr(tier, "value", None)
    if raw is None:
        raw = tier
    text = _clip(str(raw or ""), _TIER_MAX).strip().lower()
    return text or "unknown"


def _risk_for_tier(tier: Any) -> int:
    return _TIER_BASE_RISK.get(_tier_value(tier), 0)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _canonical(payload: dict[str, Any]) -> str:
    """哈希输入的规范 JSON —— 键排序、紧凑分隔符、中文不转义（保证可复算）。"""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)


def _hash_of(record: dict[str, Any]) -> str:
    """记录哈希：**不含 hash 字段本身**的规范 JSON 的 sha256（十六进制）。"""
    payload = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _session_id() -> str:
    """当前会话标识（沿用执行上下文；未绑定返回空串）。"""
    try:
        from automind.core import session_ctx

        return str(session_ctx.session_id() or "")
    except Exception:                                     # pragma: no cover - 防御性
        return ""


# ═══════════════════════════════════════════════════════════
# 账本实现
# ═══════════════════════════════════════════════════════════


class _Ledger:
    """一个账本目录的读写实现（进程级单例，线程安全）。

    职责分层：``write`` 负责哈希链与轮转；``tail/summary/verify_chain`` 只读；
    读接口对损坏行**尽量容忍**（跳过），只有 ``verify_chain`` 会把损坏如实报出来。
    """

    def __init__(self, root: str | Path, enabled: bool) -> None:
        self.root = Path(root)
        self.enabled = bool(enabled)
        self.max_file_bytes = _env_int("AUTOMIND_GOVERNANCE_MAX_FILE_BYTES",
                                       _DEFAULT_MAX_FILE_BYTES, _MIN_FILE_BYTES)
        self.max_files = _env_int("AUTOMIND_GOVERNANCE_MAX_FILES",
                                  _DEFAULT_MAX_FILES, 1)
        self._lock = threading.Lock()
        #: 下一条记录的序号 / 上一条的 hash / 当前正在写的文件名
        self.seq = 0
        self.last_hash = ""
        self.current_file = ""
        self._load_state()

    # ── 启动恢复 ────────────────────────────────────────────

    def _load_state(self) -> None:
        """从 state.json + 磁盘最后一条记录恢复 seq/hash/当前文件。

        以**磁盘最后一条记录**为准：它是 append 成功后才存在的，而 state.json
        是随后才写的 —— 进程在两者之间崩溃时，state.json 会比磁盘落后一条。
        """
        state_seq, state_hash = 0, ""
        try:
            data = json.loads((self.root / _STATE_FILE).read_text(encoding="utf-8"))
            if isinstance(data, dict):
                state_seq = max(0, _to_int(data.get("seq"), 0))
                state_hash = str(data.get("last_hash") or "")
        except Exception:                                 # 缺文件/损坏都走磁盘恢复
            pass
        self.seq, self.last_hash = state_seq, state_hash

        files = self._files_by_mtime()
        if files:
            self.current_file = files[-1].name
        last_record, record_file = self._last_record_on_disk(files)
        if last_record is not None:
            disk_seq = _to_int(last_record.get("seq"), 0)
            disk_hash = str(last_record.get("hash") or "")
            if disk_seq > self.seq or (disk_seq == self.seq and disk_hash != self.last_hash):
                self.seq, self.last_hash = disk_seq, disk_hash
                if record_file:
                    self.current_file = record_file
        if self.current_file and not (self.root / self.current_file).is_file():
            self.current_file = ""

    @staticmethod
    def _mtime(path: Path) -> float:
        try:
            return float(path.stat().st_mtime)
        except OSError:
            return 0.0

    def _files_by_mtime(self) -> list[Path]:
        """按修改时间升序返回账本文件（升序 = 链的先后顺序）。

        为什么用 mtime 而不是纯文件名排序：同日轮转会产出 ``-2/-3`` 后缀文件，
        跨天会有新日期文件，两种顺序混在一起用文件名排会错位；轮转只会"新建
        后续文件、不再改旧文件"，因此 mtime 升序天然等于写入顺序。

        v1.11.23 审计：Windows / 快速连续轮转时多个文件的 mtime 可能落在同一
        时间粒度内，仅用 ``(mtime, name)`` 会把 ``ledger-YYYYMMDD-2`` 排到
        ``ledger-YYYYMMDD`` 前面（`-` 的字典序小于 `.`），verify_chain 因此
        把正常轮转误报成断链。同 mtime 时按"日期 + 轮转序号"兜底：base=1，
        ``-2`` → 2，``-10`` → 10。
        """
        try:
            if not self.root.is_dir():
                return []
            files = [p for p in self.root.glob(_LEDGER_GLOB) if p.is_file()]
        except OSError:                                   # pragma: no cover - 防御性
            return []

        def _order(path: Path) -> tuple[float, str, int, str]:
            parts = path.stem.split("-")
            date_token = parts[1] if len(parts) > 1 else ""
            ordinal = 1
            if len(parts) >= 4 and parts[-1].isdigit():
                ordinal = int(parts[-1])
            return (self._mtime(path), date_token, ordinal, path.name)

        return sorted(files, key=_order)

    @staticmethod
    def _read_lines(path: Path) -> list[str]:
        try:
            return path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            logger.warning("governance_read_failed", path=str(path), error=str(exc))
            return []

    @classmethod
    def _parse_line(cls, line: str) -> dict[str, Any] | None:
        line = line.strip()
        if not line:
            return None
        try:
            obj = json.loads(line)
        except ValueError:
            return None
        return obj if isinstance(obj, dict) and "seq" in obj else None

    def _last_record_on_disk(self, files: list[Path]) -> tuple[dict[str, Any] | None, str]:
        """读最近文件的尾部，取最后一条可解析记录（用于重启恢复）。"""
        for path in reversed(files):
            try:
                size = path.stat().st_size
                with open(path, "rb") as fh:
                    if size > _TAIL_READ_BYTES:
                        fh.seek(-_TAIL_READ_BYTES, os.SEEK_END)
                    chunk = fh.read()
            except OSError as exc:
                logger.warning("governance_read_failed", path=str(path), error=str(exc))
                continue
            for line in reversed(chunk.decode("utf-8", "replace").splitlines()):
                record = self._parse_line(line)
                if record is not None:
                    return record, path.name
        return None, ""

    # ── 写入（哈希链 / 轮转 / 状态）────────────────────────

    def write(
        self,
        *,
        kind: str,
        tool: str,
        tier: Any,
        risk: Any,
        decision: str,
        reason: str,
        source: str,
        session_id: str,
        params: dict[str, Any] | None = None,
        call_id: str = "",
        duration_ms: float = 0.0,
        success: bool | None = None,
        error: str = "",
        governed: bool = True,
    ) -> dict[str, Any] | None:
        """追加一条记录并返回它；未启用或任何故障返回 None（fail-open）。"""
        if not self.enabled:
            return None
        try:
            session = _clip(str(session_id or ""), _FIELD_MAX)
            fields: dict[str, Any] = {
                "ts": round(time.time(), 3),
                "kind": str(kind),
                "session": session,
                "tool": _clip(str(tool or ""), _FIELD_MAX),
                "tier": _tier_value(tier),
                "risk": _to_int(risk, 0),
                "decision": _clip(str(decision or ""), _DECISION_MAX),
                "reason": _clean_text(reason or "", _REASON_MAX),
                "source": _clip(str(source or "direct"), _SOURCE_MAX) or "direct",
                "governed": bool(governed),
                "params": _sanitize_params(params),
                "call_id": _clip(str(call_id or ""), _FIELD_MAX),
                "duration_ms": round(_to_float(duration_ms, 0.0), 3),
                "success": None if success is None else bool(success),
                "error": _clean_text(error or "", _ERROR_MAX),
            }
            with self._lock:
                record: dict[str, Any] = {"seq": self.seq + 1, **fields}
                record["prev"] = self.last_hash
                record["hash"] = _hash_of(record)
                line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                path = self._ensure_file_locked()
                self._append_line(path, line)
                # 只有 append 成功后才推进内存态 —— 否则下一条会以不存在的
                # 记录为 prev，链在磁盘上就断了。
                self.seq = int(record["seq"])
                self.last_hash = str(record["hash"])
                self.current_file = path.name
                self._write_state_locked()
            return record
        except Exception as exc:
            logger.warning("governance_write_failed",
                           error=f"{type(exc).__name__}: {exc}",
                           tool=_clip(str(tool or ""), 80), kind=str(kind))
            return None

    def _append_line(self, path: Path, line: str) -> None:
        """按行追加（单独抽出来便于测试 monkeypatch 制造"写入故障"）。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")

    def _ensure_file_locked(self) -> Path:
        """确定本记录要写入的文件，必要时轮转 + 清理旧文件（调用方持锁）。"""
        today = time.strftime("%Y%m%d")
        name = self.current_file
        need_new = True
        if name:
            path = self.root / name
            try:
                same_day = f"ledger-{today}" in name
                size = path.stat().st_size if path.is_file() else -1
                need_new = not (same_day and 0 <= size < self.max_file_bytes)
            except OSError:                               # pragma: no cover - 防御性
                need_new = True
        if need_new:
            name = self._new_file_locked(today)
            self.current_file = name
            self._prune_locked(keep=name)
        path = self.root / self.current_file
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _new_file_locked(self, today: str) -> str:
        """给新文件取名：``ledger-YYYYMMDD.jsonl``，同日重复则加 ``-2/-3…``。"""
        base = f"ledger-{today}.jsonl"
        if not (self.root / base).exists():
            return base
        index = 2
        while True:
            candidate = f"ledger-{today}-{index}.jsonl"
            if not (self.root / candidate).exists():
                return candidate
            index += 1

    def _prune_locked(self, keep: str) -> None:
        """文件数超过上限时删除最旧的（绝不删除"正要写的那个"）。"""
        try:
            files = self._files_by_mtime()
            excess = len(files) + 1 - self.max_files
            for path in files[: max(0, excess)]:
                if path.name == keep:
                    continue
                path.unlink(missing_ok=True)
                logger.info("governance_ledger_pruned", file=path.name)
        except OSError as exc:                            # pragma: no cover - 防御性
            logger.warning("governance_prune_failed", error=str(exc))

    def _write_state_locked(self) -> None:
        """原子更新 state.json（临时文件 + os.replace）。

        state.json 是**加速恢复**而不是唯一真相；它写失败只警告，不改变
        "本条记录已落盘成功"的结论（重启时会从磁盘最后一条记录自愈）。
        """
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({"seq": self.seq, "last_hash": self.last_hash,
                                  "file": self.current_file}, ensure_ascii=False)
            tmp = self.root / f".state-{os.getpid()}-{threading.get_ident()}.tmp"
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self.root / _STATE_FILE)
        except Exception as exc:                          # pragma: no cover - 防御性
            logger.warning("governance_state_write_failed", error=str(exc))

    # ── 读取 ────────────────────────────────────────────────

    def _iter_records(self):
        """按链顺序迭代全部可解析记录（坏行跳过）。"""
        for path in self._files_by_mtime():
            for line in self._read_lines(path):
                record = self._parse_line(line)
                if record is not None:
                    yield record

    def tail(self, limit: int = 200, *, session_id: str = "", tool: str = "",
             decision: str = "", kind: str = "", source: str = "") -> list[dict[str, Any]]:
        """最后 N 条记录（**按 seq 升序**，旧→新），可按会话/工具/决策/类型/来源过滤。"""
        try:
            limit = max(0, _to_int(limit, 200))
        except Exception:                                 # pragma: no cover - 防御性
            limit = 200
        if limit == 0:
            return []
        filters = {
            "session": str(session_id or ""),
            "tool": str(tool or ""),
            "decision": str(decision or ""),
            "kind": str(kind or ""),
            "source": str(source or ""),
        }

        def _match(record: dict[str, Any]) -> bool:
            for field, want in filters.items():
                if want and str(record.get(field) or "") != want:
                    return False
            return True

        out: list[dict[str, Any]] = []
        for path in reversed(self._files_by_mtime()):
            for line in reversed(self._read_lines(path)):
                record = self._parse_line(line)
                if record is None or not _match(record):
                    continue
                out.append(record)
                if len(out) >= limit:
                    out.reverse()
                    return out
        out.reverse()
        return out

    def summary(self, session_id: str = "") -> dict[str, Any]:
        """账本概览（供治理中心首屏）。只读，失败时返回结构完整的零值 + error。"""
        out: dict[str, Any] = {
            "total": 0,
            "by_decision": {},
            "by_tier": {},
            "by_source": {},
            "denied": 0,
            "dangerous": 0,
            "ungoverned": 0,
            "sessions": 0,
            "files": 0,
            "bytes": 0,
            "oldest_ts": None,
            "newest_ts": None,
        }
        try:
            want_session = str(session_id or "")
            sessions: set[str] = set()
            for record in self._iter_records():
                if want_session and str(record.get("session") or "") != want_session:
                    continue
                out["total"] += 1
                decision = str(record.get("decision") or "")
                tier = str(record.get("tier") or "")
                source = str(record.get("source") or "")
                if decision:
                    out["by_decision"][decision] = out["by_decision"].get(decision, 0) + 1
                if tier:
                    out["by_tier"][tier] = out["by_tier"].get(tier, 0) + 1
                if source:
                    out["by_source"][source] = out["by_source"].get(source, 0) + 1
                if decision in _DENIED_DECISIONS:
                    out["denied"] += 1
                if tier == "dangerous" or _to_int(record.get("risk"), 0) >= 80:
                    out["dangerous"] += 1
                if record.get("governed") is False:
                    out["ungoverned"] += 1
                session = str(record.get("session") or "")
                if session:
                    sessions.add(session)
                ts = _to_float(record.get("ts"), 0.0)
                if ts > 0:
                    if out["oldest_ts"] is None or ts < out["oldest_ts"]:
                        out["oldest_ts"] = ts
                    if out["newest_ts"] is None or ts > out["newest_ts"]:
                        out["newest_ts"] = ts
            out["sessions"] = len(sessions)
            info = self.ledger_info()
            out["files"] = info["files"]
            out["bytes"] = info["bytes"]
        except Exception as exc:                          # pragma: no cover - 防御性
            logger.warning("governance_summary_failed", error=str(exc))
            out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    def ledger_info(self) -> dict[str, Any]:
        """账本目录状态（治理中心"文件数/体积/最近序号"用）。"""
        files = self._files_by_mtime()
        total = 0
        for path in files:
            try:
                total += path.stat().st_size
            except OSError:                               # pragma: no cover - 防御性
                continue
        return {
            "root": str(self.root),
            "files": len(files),
            "bytes": total,
            "enabled": enabled(),
            "last_seq": self.seq,
            "last_hash": self.last_hash,
            "current_file": self.current_file,
            "max_file_bytes": self.max_file_bytes,
            "max_files": self.max_files,
        }

    def verify_chain(self, max_records: int = 20000) -> dict[str, Any]:
        """校验哈希链完整性，返回 ``{ok, checked, broken_at, files, expected, actual, …}``。

        校验项（按链顺序）：
          1. 每条记录自身的 ``hash`` 是否等于按内容重算的 sha256；
          2. ``seq`` 是否严格 +1（中间被删除会在这里暴露）；
          3. ``prev`` 是否等于上一条的 ``hash``。

        诚实边界：
          · 只校验**最近** ``max_records`` 条（``truncated=True`` 表示更早的
            历史未纳入）；从更早历史进入校验窗口的第一条无法验证 ``prev``
            （它指向窗口外），属于正常边界，不算断链；
          · 轮转**删除最旧文件**后，最早保留文件的 ``prev`` 指向已删除记录，
            同样属于正常边界；
          · 坏行（崩溃时写了一半）如实报 ``reason="json_invalid"``，不静默跳过。
        """
        try:
            cap = max(1, _to_int(max_records, 20000))
        except Exception:                                 # pragma: no cover - 防御性
            cap = 20000
        result: dict[str, Any] = {
            "ok": True,
            "checked": 0,
            "broken_at": None,
            "files": [],
            "expected": "",
            "actual": "",
            "file": "",
            "reason": "",
            "truncated": False,
        }
        try:
            # 校验必须在一致快照上做：与写入共用同一把锁，读到的最后一行不会
            # 是写了一半的记录（否则会把正常并发写入误报成断链）。
            with self._lock:
                files = self._files_by_mtime()
                result["files"] = [p.name for p in files]
                collected: list[tuple[str, dict[str, Any] | None]] = []
                for path in reversed(files):
                    for line in reversed(self._read_lines(path)):
                        if not line.strip():
                            continue
                        collected.append((path.name, self._parse_line(line)))
                        if len(collected) >= cap:
                            result["truncated"] = True
                            break
                    if len(collected) >= cap:
                        break
                collected.reverse()

                previous_hash: str | None = None
                previous_seq: int | None = None
                for file_name, record in collected:
                    if record is None:
                        result.update(
                            ok=False, broken_at=(previous_seq + 1 if previous_seq is not None else 0),
                            expected="", actual="<invalid json line>",
                            file=file_name, reason="json_invalid")
                        return result
                    stored_hash = str(record.get("hash") or "")
                    actual_hash = _hash_of(record)
                    seq = _to_int(record.get("seq"), 0)
                    result["checked"] += 1
                    if actual_hash != stored_hash:
                        result.update(ok=False, broken_at=seq, expected=stored_hash,
                                      actual=actual_hash, file=file_name,
                                      reason="record_hash_mismatch")
                        return result
                    if previous_hash is not None:
                        # 先判 seq gap：删掉中间一条记录时 prev 与 seq 都会断，
                        # 但"序号缺了一段"是更直接、更可操作的诊断（能点名断了哪条）。
                        if previous_seq is not None and seq != previous_seq + 1:
                            result.update(ok=False, broken_at=seq,
                                          expected=str(previous_seq + 1),
                                          actual=str(seq), file=file_name,
                                          reason="seq_gap")
                            return result
                        if str(record.get("prev") or "") != previous_hash:
                            result.update(
                                ok=False, broken_at=seq, expected=previous_hash,
                                actual=str(record.get("prev") or ""),
                                file=file_name, reason="prev_hash_mismatch")
                            return result
                    previous_hash, previous_seq = stored_hash, seq
        except Exception as exc:                          # pragma: no cover - 防御性
            logger.warning("governance_verify_failed", error=str(exc))
            result.update(ok=False, reason="verify_error", actual=f"{type(exc).__name__}: {exc}")
        return result


# ═══════════════════════════════════════════════════════════
# 公共 API（供执行器 / 服务端 / 测试使用，签名保持稳定）
# ═══════════════════════════════════════════════════════════


def record_decision(
    *,
    tool: str,
    tier: Any,
    risk: Any,
    decision: str,
    reason: str = "",
    source: str = "direct",
    session_id: str = "",
    params: dict[str, Any] | None = None,
    call_id: str = "",
    governed: bool = True,
) -> dict[str, Any] | None:
    """记录一条治理决策（allow/deny/approved/denied/modified/allow_unattended/…）。

    返回落盘的记录（含 seq/hash）；未启用或写失败返回 None，**从不抛异常**。
    ``source`` 为空时取当前 ContextVar 来源，便于执行器只 bind 一次来源。
    """
    try:
        if not enabled():
            return None
        return _get_ledger().write(
            kind=_DECISION_KIND,
            tool=tool,
            tier=tier,
            risk=risk,
            decision=decision,
            reason=reason,
            source=source or current_source(),
            session_id=session_id or _session_id(),
            params=params,
            call_id=call_id,
            governed=governed,
        )
    except Exception as exc:                              # pragma: no cover - 防御性
        logger.warning("governance_write_failed", error=f"{type(exc).__name__}: {exc}")
        return None


def record_execution(
    *,
    tool: str,
    tier: Any,
    success: bool | None,
    duration_ms: float = 0.0,
    source: str = "direct",
    session_id: str = "",
    error: str = "",
    governed: bool = True,
) -> dict[str, Any] | None:
    """记录一次**实际执行**（成功/失败/耗时）；参数不落盘（体积与敏感双考虑）。"""
    try:
        if not enabled():
            return None
        return _get_ledger().write(
            kind=_EXECUTION_KIND,
            tool=tool,
            tier=tier,
            risk=_risk_for_tier(tier),
            decision="executed",
            reason="",
            source=source or current_source(),
            session_id=session_id or _session_id(),
            duration_ms=duration_ms,
            success=success,
            error=error,
            governed=governed,
        )
    except Exception as exc:                              # pragma: no cover - 防御性
        logger.warning("governance_write_failed", error=f"{type(exc).__name__}: {exc}")
        return None


def tail(limit: int = 200, *, session_id: str = "", tool: str = "",
         decision: str = "", kind: str = "", source: str = "") -> list[dict[str, Any]]:
    """读取最后 N 条记录（按 seq 升序，旧→新）；读取故障返回空列表。"""
    try:
        return _get_ledger().tail(limit, session_id=session_id, tool=tool,
                                  decision=decision, kind=kind, source=source)
    except Exception as exc:                              # pragma: no cover - 防御性
        logger.warning("governance_read_failed", error=f"{type(exc).__name__}: {exc}")
        return []


def summary(*, session_id: str = "") -> dict[str, Any]:
    """账本概览：total/by_decision/by_tier/by_source/denied/dangerous/ungoverned/
    sessions/files/bytes/oldest_ts/newest_ts。读取故障返回零值结构。"""
    try:
        return _get_ledger().summary(session_id=session_id)
    except Exception as exc:                              # pragma: no cover - 防御性
        logger.warning("governance_summary_failed", error=f"{type(exc).__name__}: {exc}")
        return {"total": 0, "by_decision": {}, "by_tier": {}, "by_source": {},
                "denied": 0, "dangerous": 0, "ungoverned": 0, "sessions": 0,
                "files": 0, "bytes": 0, "oldest_ts": None, "newest_ts": None,
                "error": f"{type(exc).__name__}: {exc}"}


def verify_chain(max_records: int = 20000) -> dict[str, Any]:
    """校验哈希链完整性（见 ``_Ledger.verify_chain`` 的校验项与诚实边界）。"""
    try:
        return _get_ledger().verify_chain(max_records=max_records)
    except Exception as exc:                              # pragma: no cover - 防御性
        logger.warning("governance_verify_failed", error=f"{type(exc).__name__}: {exc}")
        return {"ok": False, "checked": 0, "broken_at": None, "files": [],
                "expected": "", "actual": "", "file": "",
                "reason": "verify_error", "truncated": False}


def ledger_info() -> dict[str, Any]:
    """账本目录状态：root/files/bytes/enabled/last_seq（另含 last_hash/current_file）。"""
    try:
        return _get_ledger().ledger_info()
    except Exception as exc:                              # pragma: no cover - 防御性
        logger.warning("governance_read_failed", error=f"{type(exc).__name__}: {exc}")
        return {"root": "", "files": 0, "bytes": 0, "enabled": enabled(),
                "last_seq": 0, "last_hash": "", "current_file": "",
                "max_file_bytes": 0, "max_files": 0}


# ═══════════════════════════════════════════════════════════
# 分派级治理闸门（无人值守通道）
# ═══════════════════════════════════════════════════════════


def make_unattended_governor(permission_engine: Any, approval_mode_getter: Any):
    """构造"无人值守通道"治理闸门：``async (tool, kwargs) -> (allowed, reason)``。

    为什么需要它：ReAct / Plan 在**执行器内部**逐次调用 ``permissions.check()``
    并接人工审批；而工作流、技能、直接 ``registry.dispatch`` 这三条通道压根不经过
    执行器 —— 高危工具在 ``approval_mode="ask"`` 下也能被静默执行。闸门装在
    ``ToolRegistry`` 上，dispatch 在执行前统一问它。

    策略（与 ``PermissionEngine`` 同一套判定，不另立标准）：
      · 用 ``permission_engine.assess_risk(tool, tier, params)`` 算风险分，
        ``effective_tier(risk)`` 得到**有效等级**（命令/路径会抬高等级）；
      · 有效等级为 DANGEROUS 且 ``approval_mode != "approve_all"`` → 拒绝
        （记 ``deny_unattended``）；否则放行（记 ``allow_unattended``）。
      · 依据：``ask`` / ``auto`` 模式下 DANGEROUS 本该**弹窗问人**，无人值守
        通道问不到人 —— 按既有的 fail-closed 语义处理（与 ReAct v1.4.5 的
        "没有审批通道 = 拒绝"一致）；只有用户显式选择「全批准」才放行。
      · 风险评估自身异常时退回工具声明等级继续判定（治理不能因自身故障把
        所有任务打死；同时也绝不因为算不出风险就放行 DANGEROUS）。

    ``approval_mode_getter`` 必须读**实时**值（如
    ``lambda: agent.permissions.approval_mode``）而不是构造时快照 —— 审批模式
    可在任务中途修改，且会话克隆共享同一个工具注册表。
    """
    from automind.core.types import PermissionTier

    async def _governor(tool: Any, kwargs: dict[str, Any]) -> tuple[bool, str]:
        tool_name = str(getattr(tool, "name", "") or tool)
        declared = getattr(tool, "permission_tier", None)
        risk = _risk_for_tier(declared)
        effective: Any = declared if isinstance(declared, PermissionTier) else PermissionTier.SENSITIVE
        try:
            params = kwargs if isinstance(kwargs, dict) else {}
            risk = int(permission_engine.assess_risk(tool_name, declared, params))
            effective = permission_engine.effective_tier(risk)
        except Exception as exc:
            logger.warning("governance_risk_assess_failed", tool=tool_name,
                           error=f"{type(exc).__name__}: {exc}")
        try:
            mode = str(approval_mode_getter() or "auto")
        except Exception:                                 # pragma: no cover - 防御性
            mode = "auto"
        effective_value = _tier_value(effective)
        source = current_source()
        common = {
            "tool": tool_name,
            "tier": effective_value,
            "risk": risk,
            "source": source,
            "params": kwargs if isinstance(kwargs, dict) else None,
            "session_id": _session_id(),
        }
        if effective_value == "dangerous" and mode != "approve_all":
            reason = (f"无人值守通道（{source}）不能自动执行高危操作：有效等级 DANGEROUS"
                      f"（risk={risk}，当前审批模式 {mode}）。该操作需要人工批准 —— "
                      f"请经 ReAct/Plan 审批执行，或在设置中把审批模式改为「全批准」。")
            record_decision(decision="deny_unattended", reason=reason, **common)
            return False, reason
        reason = (f"无人值守通道（{source}）放行：有效等级 {effective_value}"
                  f"（risk={risk}，审批模式 {mode}）")
        record_decision(decision="allow_unattended", reason=reason, **common)
        return True, reason

    return _governor
