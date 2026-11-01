#!/usr/bin/env python3
"""AutoMind 回归测试验收门禁（v1.11.19）—— 发布前最后一道门。

一条命令、可重复、可机器判读、**不会误伤**的本地验收：

    python scripts/acceptance.py --profile static          # 30 秒级：版本/ lint / 前端产物 / 卫生
    python scripts/acceptance.py --profile fast            # 治理与可靠性冒烟集
    python scripts/acceptance.py --profile full            # tests/ 全量 + pro/tests
    python scripts/acceptance.py --profile release --json acceptance.json --report acceptance.md

门禁（gate）：version / lint / tests / pro / web / hygiene。每个门禁返回
``{name, status: pass|fail|skip|warn, detail, duration_ms}``；有 fail 时退出码 1，
否则 0（报告里其余状态只是提示）。

几个刻意的设计选择：

* **商业许可证不影响「社区版结论」**：tests 门禁注入 ``AUTOMIND_FORCE_COMMUNITY=1``
  （见 ``automind/core/edition.py``）、临时 ``AUTOMIND_DATA_DIR`` 与临时 ``HOME``，
  本机即使装了有效商业许可证也稳定跑社区版；绝不读写 ``~/.automind/license``。
  唯一例外是 ``tests/test_commercial_activation.py``（它要验证真实激活链路），
  用「隔离 HOME + 不开强制社区版」的第二趟子进程单独跑。
* **所有子进程带超时**（默认 1800s，``--timeout`` 可调），超时判 fail 并给出复现命令。
* **残留清理默认只报告不删除**；``--clean-dry-run`` 打印将删除清单，
  ``--clean`` 才真正删除，且只删「命中白名单 + 未被 git 跟踪」的临时产物 ——
  已跟踪文件、``.git``、``node_modules`` 一律不动，任何可疑命中都会拒绝删除并判 fail。
  白名单集中在 :data:`RESIDUE_PATTERNS`，便于维护与评审。
* 报告：``--json`` 机器可读，``--report`` Markdown（表格 + 失败详情 + 复现命令）。

边界：本脚本是「本地与 CI 共用的发布前最后一道门」，**不替代** CI 的跨平台矩阵
（ubuntu / windows / macOS、干净环境、打包安装验证）—— 那些仍由
``.github/workflows/ci.yml`` 负责。详见 ``docs/ACCEPTANCE.md``。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: 仓库根目录（本文件位于 ``scripts/`` 下一层）
ROOT = Path(__file__).resolve().parents[1]

PROFILES: tuple[str, ...] = ("static", "fast", "full", "release")

#: profile → 需要执行的门禁（顺序即执行顺序）
PROFILE_GATES: dict[str, tuple[str, ...]] = {
    "static": ("version", "lint", "web", "hygiene"),
    "fast": ("version", "lint", "tests", "web", "hygiene"),
    "full": ("version", "lint", "tests", "pro", "web", "hygiene"),
    "release": ("version", "lint", "tests", "pro", "web", "hygiene"),
}

GATE_DESCRIPTIONS: dict[str, str] = {
    "version": "版本号四处一致（等价 tests/test_version_consistency.py）",
    "lint": "python -m ruff check .",
    "tests": "pytest 子集（注入强制社区版 + 临时数据目录）",
    "pro": "pytest pro/tests（专业版代码，社区源码包自动 skip）",
    "web": "tsc --noEmit（有 node_modules 时）+ dist 产物完整性",
    "hygiene": "git diff --check / 私钥泄漏 / 残留临时产物扫描与安全清理",
}

GATE_NAMES: tuple[str, ...] = tuple(GATE_DESCRIPTIONS)

STATUSES: tuple[str, ...] = ("pass", "fail", "skip", "warn")

_STATUS_ICONS = {"pass": "✅ PASS", "fail": "❌ FAIL", "skip": "⏭ SKIP", "warn": "⚠️ WARN"}

#: 控制台使用的纯 ASCII 状态标记（Windows GBK 控制台不能编码 emoji，会直接崩）。
_CONSOLE_STATUS = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP", "warn": "WARN"}


def _configure_console() -> None:
    """让控制台输出对任何字符都不抛 UnicodeEncodeError（Windows GBK 控制台）。

    编码保持不变（中文照常显示），只把不可编码字符降级为转义文本 —— 门禁脚本
    绝不应该因为一个状态图标而崩掉。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):        # 非文本流 / 老解释器
            pass

#: fast profile 的「治理 / 可靠性」冒烟集：**具体路径**写死（可评审、可复现）。
#: 新增治理相关用例时在这里补一行即可；不存在的路径会被跳过并记录（兼容社区源码包）。
FAST_TEST_PATHS: tuple[str, ...] = (
    # 版本与社区版门控
    "tests/test_version_consistency.py",
    "tests/core/test_edition.py",
    "tests/core/test_edition_force_community.py",
    # 治理 / 权限 / 脱敏
    "tests/core/test_v164_governance.py",
    "tests/core/test_workspace_governance.py",
    "tests/core/test_governance.py",
    "tests/core/test_redact.py",
    "tests/core/test_sensitive_paths.py",
    "tests/tools/test_permissions.py",
    "tests/tools/test_dispatch_contract.py",
    "tests/tools/test_dispatch_governance.py",
    "tests/tools/test_security_hardening.py",
    "tests/tools/test_sandbox_escape.py",
    "tests/test_execution_governance.py",
    "tests/test_approval_modify.py",
    # 可靠性 / 长任务边界
    "tests/core/test_v1117_reliability.py",
    "tests/tools/test_tool_reliability_v1113.py",
    "tests/planning/test_tool_reliability.py",
    "tests/planning/test_react_no_progress.py",
    "tests/planning/test_long_task_guards.py",
    # v1.11.20：静默子进程 / 断线保活 / 工具可见 / 防跑飞 / 问题优化
    "tests/core/test_silent_subprocess.py",
    "tests/server/test_ws_run_reattach.py",
    "tests/planning/test_tool_liveness.py",
    "tests/planning/test_read_target_guard.py",
    "tests/core/test_prompt_optimizer.py",
    "tests/server/test_prompt_optimize_api.py",
    # v1.11.21：修复轮预算治理 / 结构化任务状态 / 两个长任务回归
    "tests/planning/test_autonomy_budget.py",
    "tests/core/test_agent_result_fields.py",
    "tests/planning/test_long_task_regressions.py",
    # v1.11.22：工程协同执行接口 / 协同质量评测指标
    "tests/core/test_multi_agent_engineering_wiring.py",
    "tests/planning/test_collab_benchmark_metrics.py",
    # v1.11.23：审计/压力（团队看板并发、子智能体委派、WS/在线）
    "tests/audit/test_team_flow.py",
    "tests/planning/test_subagent_stress_audit.py",
    "tests/server/test_ws_stress_audit.py",
    # v1.11.24：真实能力测评（离线：套件/聚合/Web 评测取模型；--live 不进门禁）
    "tests/eval/test_comprehensive_suite.py",
    "tests/eval/test_eval_key_target.py",
    "tests/server/test_eval_api.py",
    "tests/planning/test_edition_benchmark.py",
    "tests/planning/test_auto_closure_output.py",
    # v1.11.25：多智能体产物断言/交付后误提示续跑（离线）
    "tests/planning/test_artifact_scope_and_post_budget.py",
    # 门禁自身的回归
    "tests/test_acceptance_gate.py",
)

#: 需要「真实激活链路」的测试文件：它们验证商业版激活 / 降级 / 恢复，
#: 与 AUTOMIND_FORCE_COMMUNITY 的语义冲突，改在第二趟子进程里跑
#: （隔离 HOME、不注入强制社区版，因此读不到本机真实许可证）。
PRO_INTEGRATION_TEST_FILES: tuple[str, ...] = ("tests/test_commercial_activation.py",)

#: release profile 下「跳过用例意外暴增」的提示阈值（只 warn、不 fail）。
RELEASE_SKIP_WARN_THRESHOLD = 400

#: 单次子进程输出保留的尾部行数（写入报告，避免日志吞掉关键信息）。
OUTPUT_TAIL_LINES = 30

#: git 命令的超时上限（秒）—— 只读命令，正常毫秒级返回。
GIT_TIMEOUT_SECONDS = 120

#: 许可证明文标记：仓库根出现即 fail（许可证文件绝不能进版本库）。
ROOT_LICENSE_MARKER = ".automind_license"

#: 私钥文件形态：出现在**已跟踪**文件里即 fail；未跟踪（发行方本机）只提示。
PRIVATE_KEY_NAME_MARKERS = ("private_key.hex", ".license-private")
PRIVATE_KEY_SUFFIXES = (".pem", ".key")

# ── 残留 / 临时产物白名单（清理策略的唯一事实来源）────────────────────
#
# 每项：规则名 → (根目录相对 glob, 类型, 说明)。只匹配**仓库根目录**下的
# 已知临时产物；子目录里同名但正当的文件不在扫描范围（避免误伤）。
# 是否可删不只看白名单 —— 还必须「未被 git 跟踪」，两道条件同时满足才会删。
RESIDUE_PATTERNS: tuple[tuple[str, str, str, str], ...] = (
    ("perfcheck", "_perfcheck", "dir", "性能排查临时工作台"),
    ("probe-file", "probe_*.py", "file", "根目录探针脚本"),
    ("probe-image", "probe_*.png", "file", "根目录探针截图"),
    ("probe-text", "probe_*.txt", "file", "根目录探针输出"),
    ("probe-json", "probe_*.json", "file", "根目录探针输出"),
    ("scratch-a-txt", "a.txt", "file", "根目录随手文本"),
    ("scratch-output", "output.txt", "file", "根目录命令输出"),
    ("scratch-out", "out.txt", "file", "根目录命令输出"),
    ("scratch-err", "err.txt", "file", "根目录命令错误输出"),
    ("pytest-out", "pytest_out.txt", "file", "本地回归临时输出"),
    ("pytest-log", "pytest_*.log", "file", "本地回归临时日志"),
    ("workflow-tmp", ".workflow_test_tmp", "dir", "工作流测试临时目录"),
    ("connector-verify", "connector-verify-*", "dir", "connector 验证脚本残留目录"),
    ("pytest-cache", ".pytest_cache", "dir", "pytest 缓存"),
    ("ruff-cache", ".ruff_cache", "dir", "ruff 缓存"),
    ("pip-cache", "pip", "dir", "pip 缓存 / --target 临时目录"),
    ("dsh-scratch", ".dsh_*", "file", "DSH 会话临时文件"),
    ("dsh-scratch-dir", ".dsh_*", "dir", "DSH 会话临时目录"),
)

#: 残留扫描/清理时绝不进入的目录名（保护源码、构建缓存与第三方依赖）。
_PROTECTED_DIR_NAMES = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".automind",
})

#: 源码树里不算「可清理临时物」的重量级/构建产物目录（私钥与残留扫描时剪枝）。
_SOURCE_SCAN_SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", ".venv", "venv", "env", "build", "dist", "Output",
    ".automind", ".reasonix", "egg-info",
})


# ═══════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════


@dataclass
class CommandResult:
    """一条子进程命令的执行结果（统一处理超时 / 启动失败）。"""

    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str = ""
    duration_s: float = 0.0

    @property
    def output(self) -> str:
        return (self.stdout or "") + (self.stderr or "")

    def tail(self, lines: int = OUTPUT_TAIL_LINES) -> str:
        text = self.output.strip()
        if not text:
            return ""
        rows = text.splitlines()
        return "\n".join(rows[-lines:])

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.error


@dataclass
class GateResult:
    """单个门禁的结论（JSON 里只序列化 name/status/detail/duration_ms）。"""

    name: str
    status: str
    detail: str = ""
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "duration_ms": round(self.duration_ms, 1),
        }


@dataclass
class RunContext:
    """一次验收运行的上下文（root/临时目录/开关都集中在这里，便于测试注入）。"""

    root: Path
    profile: str
    timeout: float
    skip_web: bool
    skip_pro: bool
    clean: bool
    clean_dry_run: bool
    data_dir: Path
    home_dir: Path
    cache: dict[str, Any] = field(default_factory=dict)

    @property
    def clean_mode(self) -> str:
        if self.clean:
            return "clean"
        if self.clean_dry_run:
            return "dry-run"
        return "report"


@dataclass
class ResidueMatch:
    """白名单命中的文件系统项（尚未判断能否删除）。"""

    rel_path: str
    pattern: str
    kind: str                      # "file" | "dir"
    description: str
    files: list[str] = field(default_factory=list)   # 目录展开后的文件（相对 posix）


@dataclass
class ResidueEntry:
    """带上「能否安全删除」结论的残留项。"""

    rel_path: str
    pattern: str
    kind: str
    description: str
    files: list[str]
    deletable: bool
    reason: str = ""

    @property
    def total_files(self) -> int:
        return len(self.files)


@dataclass
class CleanupResult:
    deleted: list[str] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
# 通用工具
# ═══════════════════════════════════════════════════════════════


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _read_text(path: Path) -> str:
    """读文本并容忍 UTF-8 BOM（desktop/installer.iss 是 UTF-8-BOM）。"""
    raw = path.read_bytes()
    return raw.decode("utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8")


def _rel(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:          # 落在仓库根之外 —— 一律按"不安全"处理
        return path.as_posix()


def _is_within(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def run_command(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float,
) -> CommandResult:
    """执行子进程并**强制超时**；任何启动失败都收敛为可判读的结果。"""
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):          # pragma: no cover - 防御旧解释器
            out = out.decode("utf-8", errors="replace")
        if isinstance(err, bytes):          # pragma: no cover
            err = err.decode("utf-8", errors="replace")
        return CommandResult(returncode=None, stdout=out, stderr=err, timed_out=True,
                             duration_s=time.monotonic() - started)
    except OSError as exc:
        return CommandResult(returncode=None, error=str(exc),
                             duration_s=time.monotonic() - started)
    return CommandResult(returncode=proc.returncode, stdout=proc.stdout,
                         stderr=proc.stderr, duration_s=time.monotonic() - started)


def _repro(cmd: list[str], cwd: Path, root: Path) -> str:
    """把命令渲染成可直接照抄的复现命令。"""
    joined = " ".join(cmd)
    if cwd.resolve() != root.resolve():
        return f"cd {_display_path(cwd)} && {joined}"
    return joined


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix() or "."
    except (ValueError, OSError):
        return str(path)


def _tail_block(text: str, indent: str = "    ") -> str:
    rows = [row for row in text.strip().splitlines() if row.strip()]
    if not rows:
        return ""
    return "\n".join(indent + row for row in rows[-OUTPUT_TAIL_LINES:])


def _base_env(ctx: RunContext) -> dict[str, str]:
    """验收子进程的统一环境：强制社区版 + 临时数据目录 + 隔离 HOME。

    关键点（见任务书与 docs/ACCEPTANCE.md）：
      * ``AUTOMIND_FORCE_COMMUNITY=1``：本机有商业许可证也能跑出社区版结论；
      * ``AUTOMIND_DATA_DIR`` 指向本次运行的临时目录：不污染仓库 ``.automind``；
      * ``HOME`` / ``USERPROFILE`` 指向临时目录：不读写用户 home 的许可证与状态；
      * 移走 ``AUTOMIND_LICENSE``：结论只由被测代码决定，不由本机环境决定。
    """
    env = dict(os.environ)
    env["AUTOMIND_FORCE_COMMUNITY"] = "1"
    env["AUTOMIND_DATA_DIR"] = str(ctx.data_dir)
    env["HOME"] = str(ctx.home_dir)
    env["USERPROFILE"] = str(ctx.home_dir)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("AUTOMIND_BOTS_AUTOSTART", "0")
    env.pop("AUTOMIND_LICENSE", None)
    return env


def _pro_env(ctx: RunContext) -> dict[str, str]:
    """pro/tests 的环境：保留真实扩展加载（与强制社区版互斥），其余隔离同 base。"""
    env = _base_env(ctx)
    env.pop("AUTOMIND_FORCE_COMMUNITY", None)
    pro_dir = str((ctx.root / "pro").resolve())
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = pro_dir + (os.pathsep + existing if existing else "")
    return env


def _pytest_cmd(ctx: RunContext) -> list[str]:
    """pytest 基础命令；pytest-timeout 可用时附上逐用例超时（挂住能点名）。

    刻意**不再传** ``-q``：仓库 ``pyproject.toml`` 的 ``addopts`` 已经带了 ``-q``，
    再传一次会变成 ``-qq``，pytest 那时会连「N passed, M failed in …s」汇总行都不打印，
    报告里的计数就全空了。安静程度交给项目配置决定。
    """
    cmd = [sys.executable, "-m", "pytest"]
    if _module_available(ctx, "pytest_timeout"):
        cmd += ["--timeout=180", "--timeout-method=thread"]
    return cmd


def _module_available(ctx: RunContext, module: str) -> bool:
    key = f"module:{module}"
    if key not in ctx.cache:
        probe = run_command(
            [sys.executable, "-c",
             f"import importlib.util, sys; sys.exit(0 if importlib.util.find_spec({module!r}) "
             "else 1)"],
            cwd=ctx.root, env=os.environ.copy(), timeout=60,
        )
        ctx.cache[key] = probe.returncode == 0
    return bool(ctx.cache[key])


_PYTEST_COUNT_RE = re.compile(
    r"(\d+)\s+(passed|failed|skipped|errors?|xfailed|xpassed|warnings?)")


def parse_pytest_counts(text: str) -> dict[str, int]:
    """从 pytest 输出里抽取汇总计数（取每个键最后一次出现的值）。"""
    counts: dict[str, int] = {}
    for match in _PYTEST_COUNT_RE.finditer(text or ""):
        number = int(match.group(1))
        key = match.group(2)
        if key == "errors":
            key = "error"
        elif key == "warnings":
            key = "warning"
        elif key.endswith("s") and key not in ("passed", "failed", "skipped", "xfailed",
                                               "xpassed"):
            key = key.rstrip("s")
        counts[key] = number
    return counts


def format_pytest_counts(counts: dict[str, int]) -> str:
    rows = (("通过", "passed"), ("失败", "failed"), ("错误", "error"),
            ("跳过", "skipped"), ("xfail", "xfailed"), ("xpass", "xpassed"))
    return " / ".join(f"{label} {counts[key]}" for label, key in rows if counts.get(key))


# ═══════════════════════════════════════════════════════════════
# gate: version —— 版本一致性（等价 tests/test_version_consistency.py）
# ═══════════════════════════════════════════════════════════════

_VERSION_RE = re.compile(r'__version__\s*=\s*"([^"]+)"')
_APP_VERSION_RE = re.compile(r'#define\s+AppVersion\s+"([\d.]+)"')
_PRO_VERSION_RE = re.compile(r'__version__\s*=\s*"([\d.]+)"')


def _core_version(root: Path) -> str:
    init = root / "automind" / "__init__.py"
    if not init.is_file():
        return ""
    match = _VERSION_RE.search(_read_text(init))
    return match.group(1) if match else ""


def check_version(root: Path) -> tuple[list[str], list[str]]:
    """检查版本一致性；返回 (问题列表, 跳过说明列表)。

    每条问题都点名「哪个文件、实际是什么、期望是什么」，便于直接定位。
    """
    problems: list[str] = []
    notes: list[str] = []
    version = _core_version(root)
    if not version:
        return ["automind/__init__.py 里找不到 __version__（唯一的版本数据源）"], notes
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        problems.append(f"automind/__init__.py: 版本号格式不合法 {version!r}（应为 x.y.z）")
    expected = f"（应为 {version}）"

    def _check_scalar(label: str, path: Path, actual: str | None) -> None:
        if actual is None:
            notes.append(f"{label}: 文件不存在，跳过")
            return
        if actual != version:
            problems.append(f"{label}: {actual} ≠ {version}{expected}")

    # 1) pyproject.toml
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        data = tomllib.loads(_read_text(pyproject))
        _check_scalar("pyproject.toml", pyproject, str(data.get("project", {}).get("version", "")))
    else:
        notes.append("pyproject.toml: 文件不存在，跳过")

    # 2) web/package.json
    package_json = root / "web" / "package.json"
    if package_json.is_file():
        data = json.loads(_read_text(package_json))
        _check_scalar("web/package.json", package_json, str(data.get("version", "")))
    else:
        notes.append("web/package.json: 文件不存在，跳过")

    # 3) desktop/installer.iss（Windows 安装包版本；社区源码包可能不含 desktop/）
    installer = root / "desktop" / "installer.iss"
    if installer.is_file():
        match = _APP_VERSION_RE.search(_read_text(installer))
        _check_scalar("desktop/installer.iss", installer,
                      match.group(1) if match else None)
    else:
        notes.append("desktop/installer.iss: 文件不存在，跳过")

    # 4) CHANGELOG.md
    changelog = root / "CHANGELOG.md"
    if changelog.is_file():
        if f"## [{version}]" not in _read_text(changelog):
            problems.append(f"CHANGELOG.md: 缺少 [{version}] 小节（发版必须写更新日志）")
    else:
        notes.append("CHANGELOG.md: 文件不存在，跳过")

    # 5) 使用手册（构建产物；未构建时跳过）
    manual = root / "automind" / "static" / "manual.html"
    if manual.is_file():
        if f"v{version}" not in _read_text(manual):
            problems.append(f"automind/static/manual.html: 没有 v{version} 的更新日志条目")
    else:
        notes.append("automind/static/manual.html: 未构建手册，跳过")

    # 6) 商业包（社区源码包不含 pro/）
    pro_init = root / "pro" / "automind_pro" / "__init__.py"
    pro_pyproject = root / "pro" / "pyproject.toml"
    if pro_init.is_file():
        match = _PRO_VERSION_RE.search(_read_text(pro_init))
        _check_scalar("pro/automind_pro/__init__.py（商业包）", pro_init,
                      match.group(1) if match else None)
    else:
        notes.append("pro/automind_pro/__init__.py: 不含商业包，跳过")
    if pro_pyproject.is_file():
        data = tomllib.loads(_read_text(pro_pyproject))
        _check_scalar("pro/pyproject.toml（商业包）", pro_pyproject,
                      str(data.get("project", {}).get("version", "")))
    else:
        notes.append("pro/pyproject.toml: 不含商业包，跳过")

    # 7) 私钥忽略规则 / 社区包不得包含商业路径（与版本测试同源）
    gitignore = root / ".gitignore"
    if gitignore.is_file():
        ignore_text = _read_text(gitignore)
        for marker in (".license-private", "private_key.hex"):
            if marker not in ignore_text:
                problems.append(f".gitignore: 没有忽略 {marker}（签发私钥可能进版本库）")
    else:
        notes.append(".gitignore: 文件不存在，跳过")
    if pyproject.is_file():
        data = tomllib.loads(_read_text(pyproject))
        include = (data.get("tool", {}).get("setuptools", {})
                   .get("packages", {}).get("find", {}).get("include", []))
        bad = [pattern for pattern in include if "pro" in pattern]
        if bad:
            problems.append(f"pyproject.toml: 社区包竟包含商业代码路径 {bad}")
    return problems, notes


def gate_version(ctx: RunContext) -> GateResult:
    problems, notes = check_version(ctx.root)
    version = _core_version(ctx.root) or "?"
    lines = [f"核心版本 v{version}（automind/__init__.py 为唯一数据源）"]
    if problems:
        detail = "版本一致性失败：\n" + "\n".join(f"- {p}" for p in problems)
        detail += f"\n复现：cd {_display_path(ctx.root)} && python -m pytest tests/test_version_consistency.py -q"
        return GateResult("version", "fail", _with_notes(detail, notes))
    detail = "、".join(("automind/__init__.py", "pyproject.toml", "web/package.json",
                       "desktop/installer.iss", "CHANGELOG.md", "manual.html",
                       "pro/*", ".gitignore")) + " 全部与核心版本一致"
    return GateResult("version", "pass", _with_notes(detail, notes))


def _with_notes(detail: str, notes: list[str]) -> str:
    if not notes:
        return detail
    return detail + "\n（跳过：" + "；".join(notes) + "）"


# ═══════════════════════════════════════════════════════════════
# gate: lint
# ═══════════════════════════════════════════════════════════════


def gate_lint(ctx: RunContext) -> GateResult:
    probe = run_command([sys.executable, "-m", "ruff", "--version"],
                        cwd=ctx.root, env=os.environ.copy(), timeout=120)
    if not probe.ok:
        return GateResult("lint", "skip",
                          "未安装 ruff（python -m ruff --version 失败），跳过 lint")
    cmd = [sys.executable, "-m", "ruff", "check", "."]
    result = run_command(cmd, cwd=ctx.root, env=os.environ.copy(), timeout=ctx.timeout)
    repro = f"复现：cd {_display_path(ctx.root)} && " + " ".join(cmd)
    if result.timed_out:
        return GateResult("lint", "fail",
                          f"ruff 超过 {ctx.timeout:g}s 未结束\n{repro}")
    if result.error:
        return GateResult("lint", "fail", f"ruff 启动失败：{result.error}\n{repro}")
    if result.returncode == 0:
        version_line = probe.stdout.strip().splitlines()[0] if probe.stdout.strip() else "ruff"
        return GateResult("lint", "pass", f"{version_line}：全部通过")
    return GateResult("lint", "fail",
                      "ruff 发现问题：\n" + _tail_block(result.output) + "\n" + repro)


# ═══════════════════════════════════════════════════════════════
# gate: tests —— 社区版测试（强制社区版 + 临时数据目录 + 隔离 HOME）
# ═══════════════════════════════════════════════════════════════


def _tests_command_and_env(ctx: RunContext) -> tuple[list[str], dict[str, str], list[str], list[str]]:
    """构造 tests 门禁第一趟（社区版）的命令、环境与说明。

    返回 (命令, 环境, 包含的路径, 不存在而跳过的路径)。
    """
    env = _base_env(ctx)
    if ctx.profile == "fast":
        existing = [path for path in FAST_TEST_PATHS if (ctx.root / path).is_file()]
        missing = [path for path in FAST_TEST_PATHS if not (ctx.root / path).is_file()]
        cmd = _pytest_cmd(ctx) + existing
        return cmd, env, existing, missing
    # full / release：tests/ 全量，但把「需要真实激活链路」的文件留给第二趟
    cmd = _pytest_cmd(ctx) + ["tests/"]
    for path in PRO_INTEGRATION_TEST_FILES:
        if (ctx.root / path).is_file():
            cmd.append(f"--ignore={path}")
    return cmd, env, ["tests/"], []


def _run_tests_pass(
    ctx: RunContext,
    label: str,
    cmd: list[str],
    env: dict[str, str],
) -> tuple[GateResult, dict[str, int]]:
    """跑一趟 pytest，返回（该趟的迷你结论, 计数）。"""
    result = run_command(cmd, cwd=ctx.root, env=env, timeout=ctx.timeout)
    counts = parse_pytest_counts(result.output)
    repro = (f"复现：cd {_display_path(ctx.root)} && "
             + " ".join(cmd)
             + "（需设置 AUTOMIND_FORCE_COMMUNITY=1 与临时 AUTOMIND_DATA_DIR）")
    if result.timed_out:
        return GateResult("tests", "fail",
                          f"{label}：超过 {ctx.timeout:g}s 未结束\n{repro}"), counts
    if result.error:
        return GateResult("tests", "fail",
                          f"{label}：pytest 启动失败：{result.error}\n{repro}"), counts
    summary = format_pytest_counts(counts) or "无汇总输出"
    if result.returncode != 0:
        detail = f"{label}：{summary}\n" + _tail_block(result.output) + "\n" + repro
        return GateResult("tests", "fail", detail), counts
    return GateResult("tests", "pass", f"{label}：{summary}"), counts


def gate_tests(ctx: RunContext) -> GateResult:
    if ctx.profile == "static":                     # 防御：static 不选 tests，万一被调也 skip
        return GateResult("tests", "skip", "static profile 不运行 pytest")
    cmd, env, included, missing = _tests_command_and_env(ctx)
    if not included:
        return GateResult("tests", "fail",
                          "fast 冒烟集里没有任何存在的测试路径（检查 FAST_TEST_PATHS）")

    started = time.monotonic()
    lines: list[str] = []
    failed = False
    mini, counts = _run_tests_pass(ctx, "社区版测试", cmd, env)
    lines.append(mini.detail)
    failed = failed or mini.status == "fail"

    total_skipped = counts.get("skipped", 0)
    detail_note = "环境：AUTOMIND_FORCE_COMMUNITY=1、临时 AUTOMIND_DATA_DIR、隔离 HOME"

    # 第二趟：需要真实激活链路的商业集成用例（隔离 HOME，因此读不到本机许可证）
    integration: list[str] = [path for path in PRO_INTEGRATION_TEST_FILES
                              if (ctx.root / path).is_file()]
    if integration and ctx.skip_pro:
        lines.append("商业激活链路：按 --skip-pro 跳过")
    elif integration and ctx.profile in ("full", "release"):
        integration_cmd = _pytest_cmd(ctx) + integration
        integration_env = _base_env(ctx)
        integration_env.pop("AUTOMIND_FORCE_COMMUNITY", None)
        mini2, counts2 = _run_tests_pass(
            ctx, "商业激活链路（隔离 HOME、不强制社区版）", integration_cmd, integration_env)
        lines.append(mini2.detail)
        failed = failed or mini2.status == "fail"
        total_skipped += counts2.get("skipped", 0)
    elif integration:
        lines.append("商业激活链路：fast profile 不纳入（fast 只跑治理/可靠性冒烟集）")

    if missing:
        lines.append("未找到而跳过：" + "、".join(missing))

    if failed:
        status = "fail"
    else:
        status = "pass"
        if ctx.profile == "release" and total_skipped > RELEASE_SKIP_WARN_THRESHOLD:
            status = "warn"
            lines.append(f"跳过用例 {total_skipped} 个，超过提示阈值 "
                         f"{RELEASE_SKIP_WARN_THRESHOLD}（请确认没有意外 skip 暴增）")
    duration_ms = (time.monotonic() - started) * 1000
    detail = "\n".join(lines) + "\n" + detail_note
    return GateResult("tests", status, detail, duration_ms)


# ═══════════════════════════════════════════════════════════════
# gate: pro —— 专业版测试（社区源码包自动 skip）
# ═══════════════════════════════════════════════════════════════


def gate_pro(ctx: RunContext) -> GateResult:
    if ctx.profile not in ("full", "release"):
        return GateResult("pro", "skip", "profile 不含专业版测试（仅 full / release）")
    if ctx.skip_pro:
        return GateResult("pro", "skip", "按 --skip-pro 跳过")
    pro_tests = ctx.root / "pro" / "tests"
    if not pro_tests.is_dir():
        return GateResult("pro", "skip", "社区版源码包不含 pro/，跳过专业版测试")
    cmd = _pytest_cmd(ctx) + ["pro/tests"]
    result = run_command(cmd, cwd=ctx.root, env=_pro_env(ctx), timeout=ctx.timeout)
    counts = parse_pytest_counts(result.output)
    summary = format_pytest_counts(counts)
    repro = f"复现：cd {_display_path(ctx.root)} && " + " ".join(cmd)
    if result.timed_out:
        return GateResult("pro", "fail", f"超过 {ctx.timeout:g}s 未结束\n{repro}")
    if result.error:
        return GateResult("pro", "fail", f"pytest 启动失败：{result.error}\n{repro}")
    if result.returncode != 0:
        return GateResult("pro", "fail",
                          f"{summary}\n" + _tail_block(result.output) + "\n" + repro)
    return GateResult("pro", "pass",
                      f"{summary}（PYTHONPATH 含 pro/；隔离 HOME 与临时数据目录）")


# ═══════════════════════════════════════════════════════════════
# gate: web —— tsc 类型检查 + dist 产物完整性（release 追加热度校验）
# ═══════════════════════════════════════════════════════════════

_DIST_REF_RE = re.compile(r"/static/(dist/assets/[\w.-]+)")


def find_node() -> str | None:
    """定位 node 可执行文件（AUTOMIND_NODE 覆盖 → PATH）。"""
    override = os.environ.get("AUTOMIND_NODE", "").strip()
    if override and Path(override).is_file():
        return override
    found = shutil.which("node") or shutil.which("node.exe")
    return found


def check_dist(static_dir: Path) -> tuple[list[str], list[str]]:
    """校验 ``dist/index.html`` 引用的每个构建产物都真实存在。"""
    problems: list[str] = []
    checks: list[str] = []
    index = static_dir / "dist" / "index.html"
    if not index.is_file():
        return ["缺少前端构建产物 automind/static/dist/index.html"], checks
    html = _read_text(index)
    refs = _DIST_REF_RE.findall(html)
    if not refs:
        return ["automind/static/dist/index.html 未引用任何 dist/assets 构建产物"], checks
    missing = [ref for ref in refs
               if not (static_dir / ref).is_file()]
    if missing:
        problems.append("index.html 引用了不存在的产物：" + "、".join(missing))
    else:
        checks.append(f"dist 产物完整：{len(refs)} 个引用全部命中")
    return problems, checks


def check_dist_freshness(root: Path, static_dir: Path) -> tuple[list[str], list[str]]:
    """release：index.html 不得早于 web/src 的最新源文件（提示重建前端产物）。"""
    index = static_dir / "dist" / "index.html"
    source_root = root / "web" / "src"
    if not index.is_file() or not source_root.is_dir():
        return [], []
    newest = max((path.stat().st_mtime for path in source_root.rglob("*") if path.is_file()),
                 default=0.0)
    if not newest:
        return [], []
    index_mtime = index.stat().st_mtime
    if index_mtime + 1.0 < newest:
        newest_path = max((path for path in source_root.rglob("*") if path.is_file()),
                          key=lambda path: path.stat().st_mtime)
        return ["automind/static/dist/index.html 早于最新前端源码 "
                f"（{_rel(root, newest_path)}），请重建前端产物（cd web && pnpm build）"], []
    return [], ["dist 产物不早于最新前端源码"]


def gate_web(ctx: RunContext) -> GateResult:
    if ctx.skip_web:
        return GateResult("web", "skip", "按 --skip-web 跳过")
    problems: list[str] = []
    checks: list[str] = []
    static_dir = ctx.root / "automind" / "static"

    dist_problems, dist_checks = check_dist(static_dir)
    problems += dist_problems
    checks += dist_checks
    if ctx.profile == "release":
        fresh_problems, fresh_checks = check_dist_freshness(ctx.root, static_dir)
        problems += fresh_problems
        checks += fresh_checks

    node_modules = ctx.root / "web" / "node_modules"
    tsc = node_modules / "typescript" / "bin" / "tsc"
    if not node_modules.is_dir():
        checks.append("tsc：skip（web/node_modules 不存在，CI 无 pnpm 时属正常）")
    elif not tsc.is_file():
        checks.append("tsc：skip（web/node_modules/typescript 未安装）")
    else:
        node = find_node()
        if not node:
            checks.append("tsc：skip（未找到 node 可执行文件；可用 AUTOMIND_NODE 指定）")
        else:
            cmd = [node, "node_modules/typescript/bin/tsc", "--noEmit"]
            result = run_command(cmd, cwd=ctx.root / "web", env=os.environ.copy(),
                                 timeout=ctx.timeout)
            repro = f"复现：cd {_display_path(ctx.root / 'web')} && " + " ".join(cmd)
            if result.timed_out:
                problems.append(f"tsc 超过 {ctx.timeout:g}s 未结束\n{repro}")
            elif result.error:
                problems.append(f"tsc 启动失败：{result.error}\n{repro}")
            elif result.returncode != 0:
                problems.append("tsc --noEmit 类型检查失败：\n"
                                + _tail_block(result.output) + "\n" + repro)
            else:
                checks.append("tsc --noEmit 通过")

    if problems:
        return GateResult("web", "fail", "\n".join(problems + checks))
    return GateResult("web", "pass", "；".join(checks))


# ═══════════════════════════════════════════════════════════════
# gate: hygiene —— Git 卫生 / 私钥泄漏 / 残留临时产物（安全清理）
# ═══════════════════════════════════════════════════════════════


def _git(ctx: RunContext, args: list[str]) -> CommandResult:
    return run_command(["git", *args], cwd=ctx.root, env=os.environ.copy(),
                       timeout=GIT_TIMEOUT_SECONDS)


def git_available(ctx: RunContext) -> bool:
    if "git_available" not in ctx.cache:
        probe = _git(ctx, ["rev-parse", "--is-inside-work-tree"])
        ctx.cache["git_available"] = probe.ok and probe.stdout.strip() == "true"
    return bool(ctx.cache["git_available"])


def collect_tracked(ctx: RunContext) -> set[str]:
    """仓库已跟踪文件集合（posix 相对路径）。git 不可用时返回空集 + 记录原因。"""
    if "tracked" in ctx.cache:
        return ctx.cache["tracked"]
    tracked: set[str] = set()
    if git_available(ctx):
        result = _git(ctx, ["ls-files", "-z"])
        if result.ok:
            tracked = {name for name in result.stdout.split("\0") if name}
        else:
            ctx.cache["tracked_error"] = result.output.strip()[:300]
    ctx.cache["tracked"] = tracked
    return tracked


def git_whitespace_problems(ctx: RunContext) -> list[str]:
    """``git diff --check``（工作区 + 暂存区）：空白错误会让补丁评审变噪音。"""
    problems: list[str] = []
    if not git_available(ctx):
        return problems
    for label, args in (("git diff --check", ["diff", "--check"]),
                        ("git diff --cached --check", ["diff", "--cached", "--check"])):
        result = _git(ctx, args)
        if result.returncode == 0:
            continue
        if result.error or result.timed_out:
            problems.append(f"{label} 无法执行：{result.error or '超时'}")
            continue
        problems.append(f"{label} 发现空白错误：\n" + _tail_block(result.output))
    return problems


def git_status_summary(ctx: RunContext) -> tuple[str, dict[str, int]]:
    """``git status --porcelain`` 摘要（修改 / 新增 / 删除 / 未跟踪计数）。"""
    counts = {"modified": 0, "added": 0, "deleted": 0, "untracked": 0, "other": 0}
    if not git_available(ctx):
        return "git 不可用：未执行 git status", counts
    result = _git(ctx, ["status", "--porcelain"])
    if not result.ok:
        return "git status 执行失败", counts
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        code, _, path = line[:2], line[2:3], line[3:]
        path = path.strip()
        if line.startswith("??"):
            counts["untracked"] += 1
        elif code.strip() == "A":
            counts["added"] += 1
        elif code.strip() == "D":
            counts["deleted"] += 1
        elif code.strip() in ("M", "R", "C", "T"):
            counts["modified"] += 1
        elif "A" in code:
            counts["added"] += 1
        elif "D" in code:
            counts["deleted"] += 1
        elif "M" in code:
            counts["modified"] += 1
        else:
            counts["other"] += 1
    summary = (f"git status：{counts['modified']} 修改 / {counts['added']} 新增 / "
               f"{counts['deleted']} 删除 / {counts['untracked']} 未跟踪")
    return summary, counts


def _iter_source_files(root: Path) -> list[Path]:
    """遍历源码树（剪掉 .git / node_modules / 构建缓存等重量级目录）。"""
    files: list[Path] = []
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames
                       if name not in _SOURCE_SCAN_SKIP_DIRS
                       and not name.endswith(".egg-info")]
        base = Path(current)
        files.extend(base / name for name in filenames)
    return files


def scan_private_key_candidates(root: Path) -> list[str]:
    """扫描「私钥文件形态」的候选路径（相对 posix）。

    命中形态：文件名 ``private_key.hex`` / 路径含 ``.license-private`` /
    后缀 ``.pem`` / ``.key``。是否能删、是否算泄漏由调用方结合 git 跟踪状态判断。
    """
    candidates: list[str] = []
    for path in _iter_source_files(root):
        name = path.name
        rel = _rel(root, path)
        if (name in PRIVATE_KEY_NAME_MARKERS
                or any(marker in rel for marker in PRIVATE_KEY_NAME_MARKERS)
                or path.suffix.lower() in PRIVATE_KEY_SUFFIXES):
            candidates.append(rel)
    return sorted(candidates)


def _git_tracked_status(ctx: RunContext, rel_paths: list[str]) -> tuple[list[str], list[str]]:
    """把文件路径分成「已跟踪」与「未跟踪/忽略」两组。"""
    tracked = collect_tracked(ctx)
    tracked_hits = [rel for rel in rel_paths if rel in tracked]
    untracked_hits = [rel for rel in rel_paths if rel not in tracked]
    return tracked_hits, untracked_hits


def scan_private_key_constants(root: Path) -> list[str]:
    """pro/ 下「64 位 hex + 密钥语义同现」即视为把签发私钥写进了代码。"""
    hex64 = re.compile(r"[0-9a-fA-F]{64}")
    semantic = re.compile(r"seed|private|secret|signing[_ ]?key|priv[_ ]?key", re.I)
    hits: list[str] = []
    pro = root / "pro"
    if not pro.is_dir():
        return hits
    for path in sorted(pro.rglob("*.py")):
        parts = set(path.parts)
        if parts & {"__pycache__", "build", ".venv"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if hex64.search(line) and semantic.search(line):
                hits.append(f"{_rel(root, path)}:{number}: {line.strip()[:120]}")
    return hits


# ── 残留扫描 / 安全清理 ──────────────────────────────────────


def match_residue(root: Path) -> list[ResidueMatch]:
    """白名单匹配（只看仓库根目录，避免误伤子目录同名文件）。"""
    matches: list[ResidueMatch] = []
    seen: set[str] = set()
    for pattern_name, glob, kind, description in RESIDUE_PATTERNS:
        for path in sorted(root.glob(glob)):
            if path.is_dir() != (kind == "dir"):
                continue
            rel = _rel(root, path)
            if rel in seen:
                continue
            seen.add(rel)
            matches.append(ResidueMatch(rel_path=rel, pattern=pattern_name, kind=kind,
                                        description=description))
    return matches


def expand_residue_files(root: Path, match: ResidueMatch) -> list[str]:
    """目录命中 → 递归展开成文件清单（跳过受保护目录名）。"""
    if match.kind == "file":
        return [match.rel_path]
    base = root / match.rel_path
    files: list[str] = []
    for current, dirnames, filenames in os.walk(base):
        dirnames[:] = [name for name in dirnames if name not in _PROTECTED_DIR_NAMES]
        current_path = Path(current)
        files.extend(_rel(root, current_path / name) for name in filenames)
    return sorted(files)


def classify_residue(
    root: Path,
    matches: list[ResidueMatch],
    tracked: set[str],
    *,
    git_ok: bool = True,
) -> list[ResidueEntry]:
    """判断每个命中的残留项能否安全删除。

    删除条件（缺一不可）：命中白名单 + 路径在仓库根内 + **未被 git 跟踪** +
    不含受保护目录；任何一条不满足都标记 ``deletable=False`` 并写明原因。
    """
    entries: list[ResidueEntry] = []
    for match in matches:
        files = expand_residue_files(root, match)
        entry = ResidueEntry(rel_path=match.rel_path, pattern=match.pattern,
                             kind=match.kind, description=match.description,
                             files=files, deletable=True)
        target = root / match.rel_path
        if not _is_within(root, target):
            entry.deletable, entry.reason = False, "路径落在仓库根之外"
        elif any(part in _PROTECTED_DIR_NAMES for part in Path(match.rel_path).parts):
            entry.deletable, entry.reason = False, "命中受保护目录（.git / node_modules 等）"
        elif not git_ok:
            entry.deletable, entry.reason = False, "git 不可用，无法确认是否被跟踪"
        else:
            tracked_in_entry = [name for name in files if name in tracked]
            if tracked_in_entry:
                entry.deletable = False
                shown = "、".join(tracked_in_entry[:3])
                more = f" 等 {len(tracked_in_entry)} 个" if len(tracked_in_entry) > 3 else ""
                entry.reason = f"包含已跟踪文件（{shown}{more}），绝不删除"
        entries.append(entry)
    return entries


def scan_residue(ctx: RunContext) -> list[ResidueEntry]:
    return classify_residue(ctx.root, match_residue(ctx.root), collect_tracked(ctx),
                            git_ok=git_available(ctx))


def apply_cleanup(root: Path, entries: list[ResidueEntry], *, dry_run: bool,
                  print_limit: int = 200) -> CleanupResult:
    """执行/预演清理：只删 ``deletable`` 条目里的文件，目录删空后自底向上移除。"""
    result = CleanupResult()
    printed = 0
    for entry in entries:
        if not entry.deletable:
            result.protected.append(f"{entry.rel_path}（{entry.reason}）")
            continue
        for rel in entry.files:
            target = root / rel
            if not _is_within(root, target) or target.is_symlink():
                result.failed.append(f"{rel}（路径越界或为符号链接，已拒绝删除）")
                continue
            if target.is_dir():                     # 防御：只删文件
                result.failed.append(f"{rel}（是目录，已拒绝删除）")
                continue
            if printed < print_limit:
                print(f"  {'将删除' if dry_run else '删除'}：{rel}", flush=True)
                printed += 1
            elif printed == print_limit:
                print(f"  …（仅显示前 {print_limit} 个路径，完整清单见 JSON/Markdown 报告）",
                      flush=True)
                printed += 1
            if not dry_run:
                try:
                    target.unlink()
                except OSError as exc:
                    result.failed.append(f"{rel}（{exc}）")
                    continue
            result.deleted.append(rel)
        if not dry_run and entry.kind == "dir":
            _prune_empty_dirs(root / entry.rel_path)
    return result


def _prune_empty_dirs(top: Path) -> None:
    """自底向上删除清理后留下的空目录（只删空目录，绝不递归删内容）。"""
    if not top.is_dir():
        return
    for current, _dirnames, _filenames in os.walk(top, topdown=False):
        current_path = Path(current)
        try:
            if current_path != top and not any(current_path.iterdir()):
                current_path.rmdir()
        except OSError:                             # pragma: no cover - 竞态/权限
            pass
    try:
        if top.is_dir() and not any(top.iterdir()):
            top.rmdir()
    except OSError:                                 # pragma: no cover
        pass


def format_residue_entries(entries: list[ResidueEntry], max_items: int = 200) -> str:
    """渲染残留清单（默认报告 / dry-run / clean 共用）。"""
    if not entries:
        return ""
    lines: list[str] = []
    for shown, entry in enumerate(entries):
        if shown >= max_items:
            lines.append(f"…还有 {len(entries) - shown} 项未列出（规则见 RESIDUE_PATTERNS）")
            break
        suffix = f"（{entry.total_files} 个文件）" if entry.kind == "dir" else ""
        flag = "" if entry.deletable else f" [排除：{entry.reason}]"
        lines.append(f"· {entry.rel_path}{suffix} [{entry.pattern}] {entry.description}{flag}")
    return "\n".join(lines)


def run_residue_hygiene(ctx: RunContext) -> tuple[list[str], list[str], list[str]]:
    """残留子流程；返回 (问题（fail 级）, 提示（warn 级）, 通过说明)。"""
    problems: list[str] = []
    warnings: list[str] = []
    checks: list[str] = []
    entries = scan_residue(ctx)
    if not entries:
        checks.append("残留扫描：未发现已知临时产物")
        return problems, warnings, checks

    dry_run = ctx.clean_mode == "dry-run"
    clean = ctx.clean_mode == "clean"
    listing = format_residue_entries(entries)

    if clean or dry_run:
        cleanup = apply_cleanup(ctx.root, entries, dry_run=dry_run)
        if cleanup.failed:
            problems.extend(cleanup.failed)
        if cleanup.protected:
            # 白名单命中了不应清理的内容（最典型：已跟踪文件）——这是安全闸触发，
            # 不是普通提示：清理模式下一律 fail，让人先修正规则/人工核验，绝不误删。
            problems.append(
                "清理安全闸触发：以下命中项被拒绝删除（绝不误伤源码/已跟踪文件）。"
                "请人工核验后修正 scripts/acceptance.py 的 RESIDUE_PATTERNS：\n"
                + "\n".join(f"- {item}" for item in cleanup.protected))
        title = "清理预演（--clean-dry-run，未删除任何文件）" if dry_run else "已清理"
        warnings.append(f"{title}：{len(cleanup.deleted)} 个文件（均为未跟踪临时产物）\n"
                        + listing)
    else:
        warnings.append(f"残留扫描：发现 {len(entries)} 项已知临时产物"
                        f"（默认只报告不删除；--clean-dry-run 预览、--clean 清理）\n"
                        + listing)
    return problems, warnings, checks


def gate_hygiene(ctx: RunContext) -> GateResult:
    problems: list[str] = []
    warnings: list[str] = []
    checks: list[str] = []

    # 1) git 空白/状态
    if git_available(ctx):
        problems += git_whitespace_problems(ctx)
        if not problems:
            checks.append("git diff --check：无空白错误")
        status_line, _counts = git_status_summary(ctx)
        checks.append(status_line)
        tracked = collect_tracked(ctx)
        if not tracked and "tracked_error" in ctx.cache:
            warnings.append("无法读取 git 跟踪清单：" + str(ctx.cache["tracked_error"]))
    else:
        checks.append("git：不可用（跳过 diff/跟踪校验；--clean 将被禁止）")

    # 2) 许可证明文 / 私钥文件
    license_marker = ctx.root / ROOT_LICENSE_MARKER
    if license_marker.exists():
        problems.append(f"仓库根出现许可证文件 {ROOT_LICENSE_MARKER}（绝不能进版本库）")
    else:
        checks.append(f"仓库根无 {ROOT_LICENSE_MARKER}")

    key_candidates = scan_private_key_candidates(ctx.root)
    tracked_keys, untracked_keys = _git_tracked_status(ctx, key_candidates)
    if tracked_keys:
        problems.append("私钥文件出现在**已跟踪**文件里（付费墙即刻失效）：\n"
                        + "\n".join(f"- {rel}" for rel in tracked_keys))
    if untracked_keys:
        warnings.append("发现未跟踪的私钥文件（发行方本机签发材料；未提交、不随包分发，"
                        "但请不要 `git add -f`）：\n"
                        + "\n".join(f"- {rel}" for rel in untracked_keys))
    if not tracked_keys:
        if key_candidates:
            checks.append(f"私钥文件扫描：{len(key_candidates)} 个命中均未被 git 跟踪"
                          "（见上方提示；清理策略不会动它们）")
        else:
            checks.append("私钥文件扫描：未发现 private_key.hex / .license-private / "
                          "*.pem / *.key")

    # 3) pro/ 源码里的私钥常量
    if (ctx.root / "pro").is_dir():
        constant_hits = scan_private_key_constants(ctx.root)
        if constant_hits:
            problems.append("pro/ 源码疑似硬编码签发私钥（64 位 hex + 密钥语义同现）：\n"
                            + "\n".join(f"- {hit}" for hit in constant_hits))
        else:
            checks.append("pro/ 源码：无硬编码私钥常量")
    else:
        checks.append("pro/：不存在（社区源码包），跳过私钥常量扫描")

    # 4) 残留 / 临时产物（含安全清理）
    residue_problems, residue_warnings, residue_checks = run_residue_hygiene(ctx)
    problems += residue_problems
    warnings += residue_warnings
    checks += residue_checks

    # 5) 汇总状态：problems > warnings > pass
    if problems:
        return GateResult("hygiene", "fail",
                          "\n".join(problems + warnings + checks))
    if warnings:
        return GateResult("hygiene", "warn", "\n".join(warnings + checks))
    return GateResult("hygiene", "pass", "\n".join(checks))


# ═══════════════════════════════════════════════════════════════
# 门禁注册 / profile 选择
# ═══════════════════════════════════════════════════════════════

GATE_RUNNERS: dict[str, Callable[[RunContext], GateResult]] = {
    "version": gate_version,
    "lint": gate_lint,
    "tests": gate_tests,
    "pro": gate_pro,
    "web": gate_web,
    "hygiene": gate_hygiene,
}


def select_gates(
    profile: str,
    *,
    skip_web: bool = False,
    skip_pro: bool = False,
) -> list[str]:
    """按 profile 选择要跑的门禁（--skip-web / --skip-pro 只减不增）。"""
    if profile not in PROFILE_GATES:
        raise ValueError(f"未知 profile：{profile}（可选：{', '.join(PROFILES)}）")
    gates = list(PROFILE_GATES[profile])
    if skip_web:
        gates = [name for name in gates if name != "web"]
    if skip_pro:
        gates = [name for name in gates if name != "pro"]
    return gates


def execute_gate(name: str, ctx: RunContext) -> GateResult:
    """执行单个门禁；内部异常收敛为一条 fail（门禁自身不能把验收流程炸掉）。"""
    runner = GATE_RUNNERS.get(name)
    if runner is None:
        return GateResult(name, "fail", f"未注册的门禁：{name}")
    started = time.monotonic()
    try:
        result = runner(ctx)
    except Exception as exc:                        # noqa: BLE001 - 门禁必须 fail 而非崩溃
        result = GateResult(name, "fail", f"门禁内部错误：{type(exc).__name__}: {exc}")
    if result.name != name:
        result.name = name
    result.duration_ms = max(result.duration_ms, (time.monotonic() - started) * 1000)
    return result


# ═══════════════════════════════════════════════════════════════
# 报告：JSON + Markdown + 控制台摘要
# ═══════════════════════════════════════════════════════════════


def build_payload(
    profile: str,
    started_at: str,
    finished_at: str,
    results: list[GateResult],
) -> dict[str, Any]:
    """机器可读报告：结构与 docs/ACCEPTANCE.md 一致（下游可稳定消费）。"""
    summary = {status: sum(1 for result in results if result.status == status)
               for status in STATUSES}
    return {
        "profile": profile,
        "started_at": started_at,
        "finished_at": finished_at,
        "gates": [result.to_dict() for result in results],
        "summary": summary,
        "ok": summary["fail"] == 0,
    }


def payload_exit_code(payload: dict[str, Any]) -> int:
    return 0 if payload.get("ok") else 1


def _md_cell(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", "<br>")


def render_markdown(payload: dict[str, Any], results: list[GateResult],
                    root: Path = ROOT) -> str:
    """Markdown 报告：结论 + 摘要 + 门禁明细表 + 失败详情（含复现命令）。"""
    summary = payload["summary"]
    lines = [
        "# AutoMind 验收报告",
        "",
        f"- profile：`{payload['profile']}`",
        f"- 开始：{payload['started_at']}",
        f"- 结束：{payload['finished_at']}",
        f"- 结论：{'✅ 通过' if payload['ok'] else '❌ 未通过'}",
        f"- 摘要：pass {summary['pass']} / fail {summary['fail']} / "
        f"skip {summary['skip']} / warn {summary['warn']}",
        "",
        "## 门禁明细",
        "",
        "| 门禁 | 状态 | 耗时 | 详情 |",
        "| --- | --- | ---: | --- |",
    ]
    for result in results:
        icon = {"pass": "✅", "fail": "❌", "skip": "⏭", "warn": "⚠️"}.get(result.status,
                                                                           result.status)
        lines.append(f"| `{result.name}` | {icon} {result.status} | "
                     f"{result.duration_ms / 1000:.1f}s | {_md_cell(result.detail)} |")
    failed = [result for result in results if result.status == "fail"]
    if failed:
        lines += ["", "## 失败详情", ""]
        for result in failed:
            lines += [f"### `{result.name}` —— {_STATUS_ICONS['fail']}", "",
                      "```text", result.detail.strip(), "```", ""]
    lines += [
        "",
        "## 复现",
        "",
        "```bash",
        f"cd {_display_path(root)}",
        f"python scripts/acceptance.py --profile {payload['profile']} --json acceptance.json "
        f"--report acceptance.md",
        "```",
        "",
        "> 本报告由 `scripts/acceptance.py` 生成；边界与清理策略见 `docs/ACCEPTANCE.md`。",
    ]
    return "\n".join(lines) + "\n"


def print_console(payload: dict[str, Any], results: list[GateResult]) -> None:
    """控制台摘要（人看的部分；完整信息在 JSON / Markdown 报告里）。"""
    print("=" * 72, flush=True)
    for result in results:
        seconds = result.duration_ms / 1000
        first_line = result.detail.strip().splitlines()[0] if result.detail.strip() else ""
        print(f"[{_CONSOLE_STATUS.get(result.status, result.status):<4}] {result.name:<8} "
              f"{seconds:6.1f}s  {first_line}", flush=True)
    summary = payload["summary"]
    print("-" * 72, flush=True)
    print(f"profile={payload['profile']}  pass={summary['pass']} fail={summary['fail']} "
          f"skip={summary['skip']} warn={summary['warn']}  => "
          f"{'OK' if payload['ok'] else 'FAILED'}", flush=True)
    print("=" * 72, flush=True)


def write_report(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/acceptance.py",
        description="AutoMind 回归测试验收门禁（可重复、可机器判读、残留清理不误伤）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--profile", choices=PROFILES, default="fast",
                        help="验收档位（默认 fast）：static=秒级静态门禁；fast=治理冒烟集；"
                             "full=全量社区测试+专业版测试；release=full 加前端产物新鲜度校验")
    parser.add_argument("--json", metavar="PATH", help="输出机器可读 JSON 报告")
    parser.add_argument("--report", metavar="PATH", help="输出 Markdown 报告")
    parser.add_argument("--skip-web", action="store_true", help="跳过 web（tsc/产物）门禁")
    parser.add_argument("--skip-pro", action="store_true", help="跳过 pro（专业版测试）门禁")
    parser.add_argument("--keep-going", dest="keep_going", action="store_true", default=True,
                        help="失败后继续跑完剩余门禁（默认开启）")
    parser.add_argument("--no-keep-going", dest="keep_going", action="store_false",
                        help="遇到第一个 fail 立即停止")
    parser.add_argument("--clean-dry-run", action="store_true", dest="clean_dry_run",
                        help="预演残留清理：打印将删除的未跟踪临时产物，但不删除任何文件")
    parser.add_argument("--clean", action="store_true",
                        help="清理残留（只删命中白名单且未被 git 跟踪的临时产物）")
    parser.add_argument("--timeout", type=float, default=1800.0, metavar="SECONDS",
                        help="单个子进程的超时秒数（默认 1800）")
    parser.add_argument("--list", action="store_true",
                        help="只打印 profile/门禁矩阵与 fast 冒烟集，不执行任何门禁")
    return parser


def print_matrix() -> None:
    """打印 profile × 门禁矩阵，便于人工核对与 CI 文档引用。"""
    width = max(len(name) for name in GATE_NAMES) + 2
    header = "门禁".ljust(width) + "".join(profile.ljust(10) for profile in PROFILES)
    print(header)
    print("-" * len(header))
    for gate in GATE_NAMES:
        row = gate.ljust(width)
        row += "".join(("Y" if gate in PROFILE_GATES[profile] else "-").ljust(10)
                       for profile in PROFILES)
        print(row + GATE_DESCRIPTIONS[gate])
    print("\nfast profile 的治理/可靠性冒烟集：")
    for path in FAST_TEST_PATHS:
        print(f"  - {path}")


def _make_context(root: Path, args: argparse.Namespace) -> tuple[RunContext, Path]:
    """创建上下文与本次运行专用的临时数据目录（调用方负责清理）。"""
    temp_root = Path(tempfile.mkdtemp(prefix="automind-acceptance-"))
    data_dir = temp_root / "data"
    home_dir = temp_root / "home"
    data_dir.mkdir(parents=True, exist_ok=True)
    home_dir.mkdir(parents=True, exist_ok=True)
    ctx = RunContext(
        root=root,
        profile=args.profile,
        timeout=args.timeout,
        skip_web=args.skip_web,
        skip_pro=args.skip_pro,
        clean=args.clean,
        clean_dry_run=args.clean_dry_run,
        data_dir=data_dir,
        home_dir=home_dir,
    )
    return ctx, temp_root


def run(
    args: argparse.Namespace,
    *,
    root: Path = ROOT,
    context: RunContext | None = None,
) -> dict[str, Any]:
    """执行一次验收，返回 payload（同时按 --json / --report 落盘）。

    ``context`` 参数供测试注入（复用临时目录/自定义开关），CLI 路径保持零副作用。
    """
    temp_root: Path | None = None
    if context is None:
        context, temp_root = _make_context(root, args)
    gates = select_gates(context.profile, skip_web=context.skip_web,
                         skip_pro=context.skip_pro)
    started_at = _now_iso()
    results: list[GateResult] = []
    try:
        total = len(gates)
        for index, name in enumerate(gates, 1):
            print(f"==> [{index}/{total}] {name} …", flush=True)
            result = execute_gate(name, context)
            results.append(result)
            print(f"    {_CONSOLE_STATUS.get(result.status, result.status)} "
                  f"{result.duration_ms / 1000:.1f}s", flush=True)
            if result.status == "fail" and not args.keep_going:
                print("    首个 fail 即停止（--no-keep-going）", flush=True)
                break
    finally:
        if temp_root is not None:
            shutil.rmtree(temp_root, ignore_errors=True)
    finished_at = _now_iso()
    payload = build_payload(context.profile, started_at, finished_at, results)
    print_console(payload, results)
    if args.json:
        write_report(Path(args.json), json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        print(f"JSON 报告：{args.json}", flush=True)
    if args.report:
        write_report(Path(args.report),
                     render_markdown(payload, results, root=context.root))
        print(f"Markdown 报告：{args.report}", flush=True)
    return payload


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list:
        print_matrix()
        return 0
    if args.clean and args.clean_dry_run:
        parser.error("--clean 与 --clean-dry-run 不能同时使用")
    try:
        payload = run(args)
    except ValueError as exc:                        # 例如 --profile 非法（argparse 已兜）
        parser.error(str(exc))
    return payload_exit_code(payload)


if __name__ == "__main__":
    raise SystemExit(main())
