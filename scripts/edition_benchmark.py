#!/usr/bin/env python3
"""版本隔离测评编排（v1.11.24）—— community / pro / enterprise 三版本同套件对比。

## 这个脚本做什么

同一套「综合评测（comprehensive，6 个计分任务）+ 协同基准」，分别在三个**互相
隔离**的环境里跑一遍，把原始报告落盘，交给 ``scripts/benchmark_report.py``
校验 + 聚合：

============  ======  =====================================================
版本           端口     隔离要点
============  ======  =====================================================
community     18765   ``AUTOMIND_FORCE_COMMUNITY=1``、不装扩展包、
                      独立 ``AUTOMIND_DATA_DIR`` / ``HOME`` / project
pro           18766   社区版全部 + 解包 staging Pro wheel 到
                      ``<data>/extensions``（应用内升级同一机制）+ 临时 PRO 许可证
enterprise    18767   同上 + 临时 ENT 许可证（打开会话池/RBAC 等企业特性）
============  ======  =====================================================

每个版本：``/api/status`` 就绪 → 校验版本符合预期 → ``/api/config`` 配置
``deepseek/deepseek-flash``（Key 只经 HTTP 传递，**不打印、不落盘**）→
``/api/eval/run`` 跑 comprehensive **3 轮** → 子进程跑
``scripts/collab_benchmark.py --live``（community 仅 single；
pro/enterprise single+multi+subagent）→ 关服务、清临时许可证。

## 默认 dry-run

不带 ``--live`` 时**只**输出计划矩阵（版本/端口/隔离环境/依赖检查/期望 cell 数），
不启动服务、不联网、不需要 Key。``--live`` 才真实执行（消耗 Token、可能几十分钟），
且会先做依赖预检：缺 API Key / staging wheel / 套件时**直接失败**（退出码 2），
绝不在缺少依赖时跑出一份"看起来像结果"的数据。

## 诚实边界

* 报告数字全部来自真实运行后的原始报告；``--live`` 中途失败会在 manifest 里
  记下错误，缺少的 cell 由报告侧判失败（不补 0、不猜）。
* staging Pro wheel 与核心版本不一致时只**警告**并记录（扩展协议以
  ``EXTENSION_API_VERSION`` 为兼容边界），不静默假装版本一致。
* 临时许可证用一次性 Ed25519 密钥对在内存里签发，通过环境变量交给子进程；
  私钥/许可证不落盘、不打印，结束即失效（不触碰真实 ``~/.automind/license``）。
* 本脚本不内置任何基准数字；仓库里也没有提交过测评结果 —— 要结论就自己跑。

用法::

    python scripts/edition_benchmark.py                 # dry-run（默认）
    python scripts/edition_benchmark.py --live          # 真实跑（需 Key/wheel）
    python scripts/edition_benchmark.py --report-only   # 只聚合已有原始报告
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: 仓库根目录（本文件位于 ``scripts/`` 下一层）
ROOT = Path(__file__).resolve().parents[1]

#: 编排数据格式版本（结构变化时 +1）
SCHEMA = "automind.edition_benchmark/1"

#: 三个隔离版本服务的基础端口：community/pro/enterprise = base, +1, +2
PORT_BASE = 18765

#: 默认输出目录（``.automind/`` 已被 .gitignore 忽略）
DEFAULT_OUT = ROOT / ".automind" / "edition_benchmark"

#: 默认模型（任务书指定）
DEFAULT_PROVIDER = "deepseek"
DEFAULT_MODEL = "deepseek-flash"

#: 综合评测套件与重复次数
EVAL_SUITE = "comprehensive"
EVAL_REPEATS = 3

#: 评测/协同/启动的默认超时（秒）
EVAL_TIMEOUT = 1800.0
COLLAB_TIMEOUT = 5400.0
START_TIMEOUT = 120.0
POLL_INTERVAL = 2.0

#: 退出码：0=成功（dry-run 或报告校验通过）；2=用法/依赖/数据问题；3=被中断
EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_ABORTED = 3

# ── 与报告模块共用同一份"验收契约"（避免两处数字漂移）──────────────
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:                 # scripts/ 不是 Python 包
    sys.path.insert(0, _SCRIPTS_DIR)
import benchmark_report as report_mod  # noqa: E402

SCORING_TASKS = report_mod.SCORING_TASKS
PROBE_TASK = report_mod.PROBE_TASK
COLLAB_TASKS = report_mod.COLLAB_TASKS
COLLAB_MODES = report_mod.COLLAB_MODES
EDITIONS = report_mod.EDITIONS

#: 各提供商的 Key 环境变量（provider_resolver 不可用时退回最小映射）
_FALLBACK_ENV_KEYS = {"deepseek": "DEEPSEEK_API_KEY", "openai": "OPENAI_API_KEY",
                      "anthropic": "ANTHROPIC_API_KEY", "zhipu": "ZHIPU_API_KEY",
                      "kimi": "MOONSHOT_API_KEY", "qwen": "DASHSCOPE_API_KEY",
                      "doubao": "ARK_API_KEY"}


class EditionBenchmarkError(RuntimeError):
    """编排层错误（依赖缺失 / 服务起不来 / 评测失败）—— 消息可直接给用户看。"""


# ═══════════════════════════════════════════════════════════════
# 版本定义 / 计划矩阵（纯函数，dry-run 与测试都依赖）
# ═══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class EditionSpec:
    """一个被测版本的全部静态配置。"""

    name: str
    port: int
    expected_edition: str
    force_community: bool
    license_tier: str                    # "" = 社区版（无许可证）
    collab_modes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "port": self.port,
            "expected_edition": self.expected_edition,
            "force_community": self.force_community,
            "license_tier": self.license_tier or None,
            "collab_modes": list(self.collab_modes),
            "eval": {"suite": EVAL_SUITE, "repeats": EVAL_REPEATS,
                     "scoring_tasks": list(SCORING_TASKS)},
        }


def build_editions(port_base: int = PORT_BASE) -> tuple[EditionSpec, ...]:
    """构造三个版本的静态配置（端口固定为 base / base+1 / base+2）。"""
    base = int(port_base)
    return (
        EditionSpec("community", base, "community", True, "",
                    COLLAB_MODES["community"]),
        EditionSpec("pro", base + 1, "pro", False, "PRO",
                    COLLAB_MODES["pro"]),
        EditionSpec("enterprise", base + 2, "enterprise", False, "ENT",
                    COLLAB_MODES["enterprise"]),
    )


def expected_eval_cells(repeats: int = EVAL_REPEATS) -> list[dict[str, str]]:
    """6 任务 × 3 版本 × repeats 的完整 cell 清单（缺失即失败的对账基准）。"""
    return [{"edition": edition, "repeat": repeat, "task_id": task}
            for edition in EDITIONS
            for repeat in range(1, max(1, int(repeats)) + 1)
            for task in SCORING_TASKS]


def expected_collab_cells(editions: tuple[EditionSpec, ...] | None = None
                          ) -> list[dict[str, str]]:
    """协作矩阵 cell 清单（版本 × 任务 × 该版本允许的模式）。"""
    specs = editions or build_editions()
    return [{"edition": spec.name, "task_id": task, "mode": mode}
            for spec in specs
            for task in COLLAB_TASKS
            for mode in spec.collab_modes]


def _read_core_version() -> str:
    """读核心版本号（只读文件，避免 import 触发任何初始化）。"""
    try:
        text = (ROOT / "automind" / "__init__.py").read_text(encoding="utf-8")
        match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
        return match.group(1) if match else ""
    except OSError:
        return ""


def _provider_env_key(provider: str) -> str:
    """该提供商对应的 Key 环境变量名（尽量走单一事实来源）。"""
    try:
        from automind.core.provider_resolver import ENV_KEY_MAP

        mapped = str(ENV_KEY_MAP.get(provider, "") or "")
        if mapped:
            return mapped
    except Exception:                            # 核心不可用/旧版本
        pass
    return _FALLBACK_ENV_KEYS.get(provider, "")


def _port_conflict(port: int) -> str:
    """探测端口是否已被占用；返回空串 = 可用。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            return f"127.0.0.1:{port} 已被占用（{exc}）"
    return ""


# ═══════════════════════════════════════════════════════════════
# 依赖预检（不联网、不启动服务）
# ═══════════════════════════════════════════════════════════════


@dataclass
class DependencyState:
    """``--live`` 的前置依赖状态；missing 里的每一项都会阻止真实运行。"""

    suite: dict[str, Any] = field(default_factory=dict)
    pro_wheel: dict[str, Any] = field(default_factory=dict)
    credentials: dict[str, Any] = field(default_factory=dict)
    port_conflicts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def critical_errors(self) -> list[str]:
        """真正阻止 ``--live`` 的问题（warnings 不拦）。"""
        return list(self.errors)

    def as_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "pro_wheel": self.pro_wheel,
            "credentials": {key: value for key, value in self.credentials.items()
                            if key != "api_key"},
            "port_conflicts": list(self.port_conflicts),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "ready_for_live": not self.errors,
        }


def _check_eval_suite(suite: str = EVAL_SUITE) -> dict[str, Any]:
    """确认综合套件存在且可加载：6 个计分任务 + expect_fail 反向断言探针。"""
    path = ROOT / "automind" / "eval" / "suites" / f"{suite}.yml"
    info: dict[str, Any] = {"name": suite, "path": f"<REPO>/automind/eval/suites/{suite}.yml",
                            "exists": path.is_file(), "cases": [],
                            "scoring_tasks": [], "probe_cases": [], "error": ""}
    if not path.is_file():
        info["error"] = (f"综合套件不存在：<REPO>/automind/eval/suites/{suite}.yml"
                         "（依赖 v1.11.24 测评套件任务；dry-run 可先看计划）")
        return info
    try:
        from automind.eval.suite import load_suite

        loaded = load_suite(path)
        info["cases"] = [case.id for case in loaded.cases]
        info["timeout_seconds"] = loaded.timeout_seconds
        info["scoring_tasks"] = [case.id for case in loaded.cases
                                 if not case.expect_fail]
        info["probe_cases"] = [case.id for case in loaded.cases if case.expect_fail]
        missing = [task for task in SCORING_TASKS
                   if task not in info["scoring_tasks"]]
        if missing:
            info["error"] = f"套件缺少计分任务：{missing}（套件版本不对）"
        elif not info["probe_cases"]:
            info["error"] = ("套件里没有 expect_fail 反向断言探针；"
                             "没有它就无法体检断言链路")
        overlap = sorted(set(info["probe_cases"]) & set(SCORING_TASKS))
        if overlap:
            info["error"] = f"expect_fail 探针与计分任务 id 重叠：{overlap}"
    except Exception as exc:                     # 套件写错/依赖未就绪
        info["error"] = f"综合套件无法加载：{type(exc).__name__}: {exc}"
    return info


def _wheel_version(path: Path) -> str:
    """从 wheel 文件名解析 ``automind_pro-<version>-py3-...whl`` 的版本。"""
    match = re.match(r"^automind_pro-([^-]+)-", path.name)
    return match.group(1) if match else ""


def resolve_pro_wheel(explicit: str = "") -> dict[str, Any]:
    """定位 staging Pro wheel：显式路径 > pro/dist 与核心同版本 > 最新一份。"""
    core = _read_core_version()
    info: dict[str, Any] = {"path": "", "version": "", "core_version": core,
                            "version_match": False, "error": ""}
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            info["error"] = f"指定的 staging Pro wheel 不存在：{explicit}"
            return info
        info.update(path=str(path), version=_wheel_version(path),
                    version_match=_wheel_version(path) == core)
        if info["version"] and not info["version_match"]:
            info["warning"] = (f"staging wheel 版本 {info['version']} 与核心 "
                              f"{core} 不一致；扩展协议兼容才可跑，结果需标注")
        return info
    dist = ROOT / "pro" / "dist"
    candidates = sorted(dist.glob("automind_pro-*.whl"),
                        key=lambda item: item.stat().st_mtime, reverse=True) \
        if dist.is_dir() else []
    if not candidates:
        info["error"] = (
            "找不到 staging Pro wheel：先在 pro/ 下构建（如 "
            "`cd pro && python -m build --wheel --outdir dist`），"
            "或用 --pro-wheel 指定 .whl")
        return info
    exact = [path for path in candidates if _wheel_version(path) == core]
    chosen = (exact or candidates)[0]
    info.update(path=str(chosen), version=_wheel_version(chosen),
                version_match=_wheel_version(chosen) == core)
    if exact:
        info["note"] = "使用与核心同版本的 staging wheel"
    else:
        info["warning"] = (f"pro/dist 里没有与核心 {core} 同版本的 wheel，"
                          f"回退到最新 {info['version']}；结果需标注版本差异")
    return info


def resolve_credentials(provider: str = DEFAULT_PROVIDER, api_key: str = "",
                        api_key_source: str = "") -> dict[str, Any]:
    """解析凭据来源（**只报告来源，绝不返回/打印 Key 本身**）。"""
    source = str(api_key_source or "").strip()
    if provider == "ollama":
        return {"present": True, "source": "local (ollama)", "env_var": ""}
    if str(api_key or "").strip():
        return {"present": True, "source": source or "explicit",
                "env_var": _provider_env_key(provider)}
    env_var = _provider_env_key(provider)
    if env_var and os.environ.get(env_var, "").strip():
        return {"present": True, "source": f"env:{env_var}", "env_var": env_var}
    return {
        "present": False, "source": "", "env_var": env_var,
        "error": (f"缺少 API Key：provider='{provider}' 需要环境变量 "
                  f"{env_var or '（未知）'}，或用 --api-key / AUTOMIND_BENCH_API_KEY 提供；"
                  "dry-run 不需要 Key"),
    }


def collect_dependencies(*, provider: str = DEFAULT_PROVIDER,
                         model: str = DEFAULT_MODEL,
                         api_key: str = "", suite: str = EVAL_SUITE,
                         pro_wheel: str = "", port_base: int = PORT_BASE,
                         editions: tuple[EditionSpec, ...] | None = None
                         ) -> DependencyState:
    """``--live`` 前的全部依赖检查（不联网、不启动进程）。"""
    state = DependencyState()
    state.suite = _check_eval_suite(suite)
    if state.suite.get("error"):
        state.errors.append(str(state.suite["error"]))

    wheel = resolve_pro_wheel(pro_wheel)
    state.pro_wheel = wheel
    if wheel.get("error"):
        state.errors.append(str(wheel["error"]))
    elif wheel.get("warning"):
        state.warnings.append(str(wheel["warning"]))

    creds = resolve_credentials(provider, api_key)
    state.credentials = creds
    if not creds.get("present"):
        state.errors.append(str(creds.get("error") or "缺少 API Key"))

    for spec in (editions or build_editions(port_base)):
        conflict = _port_conflict(spec.port)
        if conflict:
            state.port_conflicts.append(conflict)
            state.errors.append(
                f"{spec.name} 的端口不可用：{conflict}；"
                f"请释放端口或用 --port-base 换一组（如 {port_base + 100}）")
    return state


def _iso_paths(spec: EditionSpec) -> dict[str, str]:
    """隔离环境的占位路径（计划里不暴露真实绝对路径）。"""
    return {"home": f"<RUN>/{spec.name}/home",
            "data_dir": f"<RUN>/{spec.name}/data",
            "project": f"<RUN>/{spec.name}/project",
            "logs": f"<RUN>/{spec.name}/logs"}


def build_plan(*, editions: tuple[EditionSpec, ...] | None = None,
               repeats: int = EVAL_REPEATS, provider: str = DEFAULT_PROVIDER,
               model: str = DEFAULT_MODEL, out_dir: str | Path = DEFAULT_OUT,
               dependencies: DependencyState | None = None) -> dict[str, Any]:
    """dry-run 计划（纯数据；不创建目录、不启动任何东西）。"""
    specs = editions or build_editions()
    deps = dependencies or collect_dependencies(
        provider=provider, model=model, editions=specs)
    deps_dict = deps.as_dict()
    editions_plan: list[dict[str, Any]] = []
    for spec in specs:
        entry = spec.as_dict()
        entry.update({
            "isolation": _iso_paths(spec),
            "extensions": (f"staging Pro wheel "
                           f"{deps.pro_wheel.get('version') or '（未就绪）'}"
                           if spec.license_tier else "none（社区版不装扩展）"),
            "license": (f"临时 {spec.license_tier} 许可证（一次性密钥对，仅环境变量）"
                        if spec.license_tier else "none"),
        })
        editions_plan.append(entry)
    eval_cells = expected_eval_cells(repeats)
    collab_cells = expected_collab_cells(specs)
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dry_run": True,
        "core_version": _read_core_version(),
        "provider": provider,
        "model": model,
        "api_key_source": deps.credentials.get("source") or "（未配置）",
        "out_dir": str(out_dir),
        "editions": editions_plan,
        "matrix": {
            "eval": {"suite": EVAL_SUITE, "repeats": repeats,
                     "scoring_tasks": list(SCORING_TASKS),
                     "probe_cases": list(deps.suite.get("probe_cases") or []),
                     "probe_rule": "按 expect_fail 过滤，不计分、不参与通过率",
                     "cells": len(eval_cells),
                     "expected": eval_cells},
            "collab": {"tasks": list(COLLAB_TASKS),
                       "cells": len(collab_cells),
                       "expected": collab_cells},
        },
        "dependencies": deps_dict,
        "steps": [
            "1) 预检：套件 / staging Pro wheel / API Key / 三个端口",
            "2) 逐版本：准备隔离 HOME+DATA_DIR+project → 装 wheel（pro/ent）"
            "→ 生成临时许可证 → 启动 server → /api/status 校验版本",
            "3) /api/config 配置模型（Key 只经 HTTP，不打印）",
            "4) /api/eval/run 跑 comprehensive 3 轮（保存原始报告；"
            "6 个计分任务，expect_fail 探针不计分）",
            "5) scripts/collab_benchmark.py --live 跑协作矩阵（保存原始报告）",
            "6) 关服务、清临时许可证 → 写 manifest → 聚合报告",
        ],
        "notes": [
            "dry-run 不启动服务、不联网、不需要 API Key；不代表任何实测结论。",
            "计分口径：6 个计分任务 × 3 版本 × 3 重复 = 54 个 cell；"
            "assertion_probe 等 expect_fail 探针只做断言链路体检，不计分。",
            "真实运行请加 --live；结果数字只来自真实运行，仓库不内置基准数据。",
            "报告会脱敏 API Key / 许可证 / 绝对路径。",
        ],
    }


# ═══════════════════════════════════════════════════════════════
# 隔离环境
# ═══════════════════════════════════════════════════════════════

#: 必须从子进程环境里剔除的变量（防止继承宿主机的许可证/版本/Key 造成串味）
_STRIP_ENV_PREFIXES = ("AUTOMIND_LICENSE", "AUTOMIND_FORCE_COMMUNITY",
                       "AUTOMIND_DATA_DIR", "AUTOMIND_MACHINE_ID",
                       "AUTOMIND_AUTH_TOKEN")


@dataclass
class EditionRuntime:
    """一个版本的隔离运行目录与环境。"""

    spec: EditionSpec
    root: Path
    home: Path
    data_dir: Path
    project: Path
    logs: Path
    env: dict[str, str] = field(default_factory=dict)
    wheel: dict[str, Any] = field(default_factory=dict)
    license: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """manifest 用：只给相对/占位路径与许可证类型，不含 Key/许可证全文。"""
        data: dict[str, Any] = {
            "home": self.home.name and f"<RUN>/{self.spec.name}/home",
            "data_dir": f"<RUN>/{self.spec.name}/data",
            "project": f"<RUN>/{self.spec.name}/project",
            "logs": f"<RUN>/{self.spec.name}/logs",
            "force_community": self.spec.force_community,
        }
        if self.wheel:
            data["pro_wheel"] = {
                "file": Path(self.wheel.get("path") or "").name,
                "version": self.wheel.get("version", ""),
                "core_version": self.wheel.get("core_version", ""),
                "version_match": bool(self.wheel.get("version_match")),
            }
        if self.license:
            data["license"] = {
                "kind": "temporary",
                "tier": self.license.get("tier", ""),
                "expiry": self.license.get("expiry", ""),
                "storage": "env-only（不落盘、不打印）",
            }
        return data


def _base_env(home: Path, data_dir: Path) -> dict[str, str]:
    """基础隔离环境：剔除会串味的变量，隔离 HOME/DATA_DIR，保证可 import 仓库代码。"""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(_STRIP_ENV_PREFIXES)}
    for key in list(env):
        if key in _FALLBACK_ENV_KEYS.values() or key.endswith("_API_KEY"):
            env.pop(key, None)          # Key 不进 server 环境：只走 /api/config
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    drive, rest = os.path.splitdrive(str(home))
    env["HOMEDRIVE"] = drive
    env["HOMEPATH"] = rest or str(home)
    env["AUTOMIND_DATA_DIR"] = str(data_dir)
    python_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (str(ROOT) + (os.pathsep + python_path if python_path else ""))
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def prepare_runtime(spec: EditionSpec, run_root: str | Path, *,
                    wheel: dict[str, Any] | None = None) -> EditionRuntime:
    """创建工作目录并构造隔离环境（不启动服务）。"""
    edition_root = Path(run_root) / spec.name
    runtime = EditionRuntime(
        spec=spec, root=edition_root,
        home=edition_root / "home", data_dir=edition_root / "data",
        project=edition_root / "project", logs=edition_root / "logs")
    for directory in (runtime.home, runtime.data_dir, runtime.project,
                      runtime.logs):
        directory.mkdir(parents=True, exist_ok=True)
    runtime.env = _base_env(runtime.home, runtime.data_dir)
    if spec.force_community:
        runtime.env["AUTOMIND_FORCE_COMMUNITY"] = "1"
    if wheel:
        runtime.wheel = dict(wheel)
    return runtime


def install_pro_wheel(wheel_path: str | Path, extensions_dir: str | Path) -> dict[str, Any]:
    """把 wheel 解包进 ``<data>/extensions``（应用内升级同一机制，无需 pip）。

    安全约束：只解包 ``automind_pro/`` 与 ``*.dist-info/`` 之内的普通文件；
    拒绝绝对路径 / ``..`` 穿越 / 符号链接（zip 无法表达符号链接，但仍校验名字）。
    """
    path = Path(wheel_path)
    if not path.is_file():
        raise EditionBenchmarkError(f"staging Pro wheel 不存在：{path}")
    target = Path(extensions_dir)
    target.mkdir(parents=True, exist_ok=True)
    extracted = 0
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            name = member.filename.replace("\\", "/")
            if member.is_dir():
                continue
            parts = Path(name).parts
            if (not name or name.startswith("/") or ".." in parts
                    or Path(name).is_absolute()):
                raise EditionBenchmarkError(
                    f"wheel 内含不安全路径：{member.filename!r}（拒绝解包）")
            if not (name.startswith("automind_pro/")
                    or name.startswith("automind_pro-")):
                continue
            destination = (target / name).resolve()
            if not destination.is_relative_to(target.resolve()):
                raise EditionBenchmarkError(
                    f"wheel 成员越出安装目录：{member.filename!r}（拒绝解包）")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, destination.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            extracted += 1
    package_dir = target / "automind_pro"
    if not (package_dir / "__init__.py").is_file():
        raise EditionBenchmarkError(
            f"wheel 解包后缺少 automind_pro/__init__.py（{path.name} 可能不是扩展包）")
    return {"files": extracted, "package": str(package_dir)}


def generate_temp_license(tier: str, *, days: int = 7,
                          customer: str = "stagingbenchmark") -> dict[str, Any]:
    """为 staging 生成临时许可证（一次性 Ed25519 密钥对，内存中签发）。

    私钥种子不落盘、不打印；许可证只通过环境变量交给隔离子进程。返回的
    ``public_key`` 需要同时经 ``AUTOMIND_LICENSE_PUBLIC_KEY`` 下发，让扩展包
    只信任这把一次性公钥（不触碰发行方真实私钥，也不污染本机许可证目录）。
    """
    tier = str(tier or "").upper()
    if tier not in ("PRO", "ENT"):
        raise EditionBenchmarkError(f"临时许可证仅支持 PRO/ENT，收到 {tier!r}")
    pro_root = ROOT / "pro"
    if not (pro_root / "automind_pro" / "licensing").is_dir():
        raise EditionBenchmarkError(
            "缺少 Pro 扩展包源码（pro/automind_pro/licensing），无法生成临时许可证；"
            "请改用 --license-file + --license-public-key 提供已有许可证")
    added = str(pro_root) not in sys.path
    if added:
        sys.path.insert(0, str(pro_root))
    try:
        from automind_pro.licensing import build_license
        from automind_pro.licensing.ed25519 import public_key_from_seed

        seed = secrets.token_bytes(32)
        expiry = time.strftime("%Y%m%d",
                               time.gmtime(time.time() + max(1, int(days)) * 86400))
        raw = build_license(seed, tier, expiry, customer)
        public_hex = public_key_from_seed(seed).hex()
    except Exception as exc:
        raise EditionBenchmarkError(
            f"生成临时许可证失败：{type(exc).__name__}: {exc}") from exc
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(pro_root))
    return {"tier": tier, "license": raw, "public_key": public_hex,
            "expiry": expiry, "days": int(days),
            "source": "temporary ephemeral keypair (env-only)"}


def load_license_files(license_file: str, public_key: str) -> dict[str, Any]:
    """读取调用方提供的真实 staging 许可证（与临时许可证二选一）。"""
    path = Path(license_file)
    if not path.is_file():
        raise EditionBenchmarkError(f"许可证文件不存在：{license_file}")
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        raise EditionBenchmarkError(f"许可证文件为空：{license_file}")
    public = str(public_key or "").strip()
    if not public:
        raise EditionBenchmarkError(
            "使用 --license-file 时必须同时给 --license-public-key（验签公钥 hex）")
    return {"tier": "custom", "license": raw, "public_key": public,
            "expiry": "", "source": f"license-file:{path.name}"}


def _apply_license_env(runtime: EditionRuntime, license_info: dict[str, Any]) -> None:
    """把许可证放进子进程环境（服务端与协同子进程共用同一份 env）。"""
    runtime.license = license_info
    runtime.env["AUTOMIND_LICENSE"] = str(license_info["license"])
    runtime.env["AUTOMIND_LICENSE_PUBLIC_KEY"] = str(license_info["public_key"])


# ═══════════════════════════════════════════════════════════════
# HTTP 客户端 / 服务生命周期（只有 --live 会走到）
# ═══════════════════════════════════════════════════════════════


class HttpClient:
    """极简 JSON HTTP 客户端（标准库，无第三方依赖）。"""

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)

    def _request(self, method: str, path: str,
                 payload: dict[str, Any] | None = None,
                 timeout: float | None = None) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
        try:
            with urllib.request.urlopen(
                    request, timeout=timeout or self.timeout) as response:
                body = response.read().decode("utf-8", "replace")
                return response.status, _parse_json(body)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            return exc.code, _parse_json(body)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise EditionBenchmarkError(
                f"无法连接隔离服务 {url}：{type(exc).__name__}: {exc}") from exc

    def get(self, path: str, timeout: float | None = None) -> tuple[int, Any]:
        return self._request("GET", path, None, timeout)

    def post(self, path: str, payload: dict[str, Any],
             timeout: float | None = None) -> tuple[int, Any]:
        return self._request("POST", path, payload, timeout)


def _parse_json(body: str) -> Any:
    try:
        return json.loads(body) if body.strip() else {}
    except ValueError:
        return {"_raw": body[:2000]}


def _scrub(text: Any, *, secrets: tuple[str, ...] = (),
           extra_paths: tuple[str, ...] = ()) -> str:
    """错误/日志文本脱敏：先替换调用方给的敏感值，再走统一脱敏规则。"""
    out = str(text or "")
    for secret in secrets:
        if secret:
            out = out.replace(secret, "[REDACTED]")
    return report_mod.redact_text(out, extra_paths=extra_paths)


def _tail(path: Path, limit: int = 2000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:]


@dataclass
class ServerHandle:
    """隔离服务进程句柄。"""

    process: subprocess.Popen
    log_path: Path
    runtime: EditionRuntime
    ready_seconds: float = 0.0
    status: dict[str, Any] = field(default_factory=dict)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.runtime.spec.port}"


def start_server(runtime: EditionRuntime) -> ServerHandle:
    """启动隔离 uvicorn 服务（stdout/stderr 落到 run 目录日志）。"""
    port = runtime.spec.port
    conflict = _port_conflict(port)
    if conflict:
        raise EditionBenchmarkError(
            f"{runtime.spec.name} 启动前端口冲突：{conflict}")
    log_path = runtime.logs / "server.log"
    command = [sys.executable, "-m", "automind.server",
               "--host", "127.0.0.1", "--port", str(port)]
    log_handle = log_path.open("w", encoding="utf-8", errors="replace")
    try:
        process = subprocess.Popen(  # noqa: S603 - 本地可信命令
            command, cwd=str(runtime.project), env=runtime.env,
            stdout=log_handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    except OSError as exc:
        log_handle.close()
        raise EditionBenchmarkError(f"启动隔离服务失败：{exc}") from exc
    return ServerHandle(process=process, log_path=log_path, runtime=runtime)


def wait_ready(handle: ServerHandle, *, timeout: float = START_TIMEOUT,
               poll: float = POLL_INTERVAL) -> dict[str, Any]:
    """轮询 ``/api/status`` 直到就绪；服务提前退出/超时给出可读错误。"""
    client = HttpClient(handle.base_url, timeout=10.0)
    started = time.monotonic()
    deadline = started + max(1.0, float(timeout))
    while time.monotonic() < deadline:
        code = handle.process.poll()
        if code is not None:
            tail = _tail(handle.log_path)
            raise EditionBenchmarkError(
                f"{handle.runtime.spec.name} 服务提前退出（returncode={code}）："
                f"{_scrub(tail, extra_paths=(str(handle.runtime.root),))}")
        try:
            status_code, data = client.get("/api/status")
        except EditionBenchmarkError:
            status_code, data = 0, {}
        if status_code == 200 and isinstance(data, dict) \
                and data.get("status") == "running":
            handle.ready_seconds = round(time.monotonic() - started, 2)
            handle.status = data
            return data
        time.sleep(poll)
    tail = _tail(handle.log_path)
    raise EditionBenchmarkError(
        f"{handle.runtime.spec.name} 服务在 {timeout:g}s 内没有就绪："
        f"{_scrub(tail, extra_paths=(str(handle.runtime.root),))}")


def stop_server(handle: ServerHandle | None, *, grace: float = 10.0) -> None:
    """先礼后兵地关掉隔离服务（绝不留孤儿进程）。"""
    if handle is None or handle.process.poll() is not None:
        return
    try:
        handle.process.terminate()
        handle.process.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    except Exception as exc:                     # pragma: no cover - 防御
        print(f"[live] 关闭 {handle.runtime.spec.name} 服务时告警：{exc}",
              file=sys.stderr)
    try:
        handle.process.kill()
        handle.process.wait(timeout=grace)
    except Exception as exc:                     # pragma: no cover - 防御
        print(f"[live] 强杀 {handle.runtime.spec.name} 服务失败：{exc}",
              file=sys.stderr)


def _license_diagnosis(client: HttpClient) -> dict[str, Any]:
    """取 ``/api/license`` 的升级状态摘要（用于版本不符时给出原因）。"""
    try:
        code, data = client.get("/api/license", timeout=10.0)
    except EditionBenchmarkError:
        return {}
    if code != 200 or not isinstance(data, dict):
        return {}
    keep = ("edition", "step", "reason_text", "version_match", "version_reason",
            "package")
    return {key: data.get(key) for key in keep if key in data}


def _require_edition(spec: EditionSpec, observed: str,
                     diagnosis: dict[str, Any], *, secrets: tuple[str, ...] = (),
                     extra_paths: tuple[str, ...] = ()) -> None:
    """校验隔离出来的版本与预期一致（串味 = 污染结论，必须失败）。"""
    if observed == spec.expected_edition:
        return
    reason = str((diagnosis or {}).get("reason_text")
                 or (diagnosis or {}).get("version_reason") or "")
    raise EditionBenchmarkError(_scrub(
        f"{spec.name} 版本不符合预期：期望 {spec.expected_edition}，实际 "
        f"{observed or '（未知）'}。{('原因：' + reason) if reason else ''}"
        f"（检查 staging wheel / 临时许可证 / 环境变量是否串味）",
        secrets=secrets, extra_paths=extra_paths))


def _configure_model(client: HttpClient, runtime: EditionRuntime, *,
                     provider: str, model: str, api_key: str,
                     api_base: str = "") -> dict[str, Any]:
    """通过 ``/api/config`` 设置模型与 Key（Key 只在 HTTP body 里，不打印）。"""
    payload: dict[str, Any] = {"provider": provider, "model": model}
    if api_key:
        payload["api_key"] = api_key
    if api_base:
        payload["api_base"] = api_base
    code, data = client.post("/api/config", payload, timeout=60.0)
    secrets = (api_key,)
    paths = (str(runtime.root),)
    if code != 200 or not isinstance(data, dict) or data.get("error"):
        raise EditionBenchmarkError(_scrub(
            f"{runtime.spec.name} /api/config 配置失败（HTTP {code}）："
            f"{data if isinstance(data, dict) else data}",
            secrets=secrets, extra_paths=paths))
    if str(data.get("model") or "") != model:
        raise EditionBenchmarkError(_scrub(
            f"{runtime.spec.name} 配置后模型不符：期望 {model}，"
            f"实际 {data.get('model')}", secrets=secrets, extra_paths=paths))
    code, status = client.get("/api/status", timeout=30.0)
    if code != 200 or not isinstance(status, dict):
        raise EditionBenchmarkError(_scrub(
            f"{runtime.spec.name} 配置后 /api/status 不可用（HTTP {code}）",
            secrets=secrets, extra_paths=paths))
    if not status.get("has_api_key"):
        raise EditionBenchmarkError(_scrub(
            f"{runtime.spec.name} 配置后仍没有可用 API Key"
            f"（来源：{status.get('llm_notice') or '未知'}）",
            secrets=secrets, extra_paths=paths))
    if status.get("llm_ready") is False:
        raise EditionBenchmarkError(_scrub(
            f"{runtime.spec.name} LLM 后端未就绪：{status.get('llm_error') or ''}",
            secrets=secrets, extra_paths=paths))
    return status


def _eval_preflight(client: HttpClient, runtime: EditionRuntime, *,
                    raw_dir: Path) -> dict[str, Any]:
    """套件预检：``/api/eval/run`` dry_run=true，确认 6 个计分任务都在。

    注意 server 的 dry-run 响应不含 ``expect_fail`` 字段（只有 id/mode/断言类型），
    因此这里按常量校验 6 个计分任务 + 至少多出一个 case（探针）；探针的
    "不计分"过滤在报告侧以原始报告的 ``expect_fail`` 标志为准。
    """
    code, data = client.post("/api/eval/run",
                             {"suite": EVAL_SUITE, "dry_run": True}, timeout=60.0)
    paths = (str(runtime.root),)
    if code != 200 or not isinstance(data, dict):
        raise EditionBenchmarkError(_scrub(
            f"{runtime.spec.name} 评测预检失败（HTTP {code}）：{data}",
            extra_paths=paths))
    case_ids = [str(case.get("id") or "")
                for case in (data.get("cases") or []) if isinstance(case, dict)]
    missing = [task for task in SCORING_TASKS if task not in case_ids]
    if missing:
        raise EditionBenchmarkError(
            f"{runtime.spec.name} 的 comprehensive 套件缺少计分任务：{missing}"
            "（依赖 v1.11.24 测评套件 / 服务器版本；请确认 server 加载了同一份套件）")
    if len(case_ids) <= len(SCORING_TASKS):
        raise EditionBenchmarkError(
            f"{runtime.spec.name} 的 comprehensive 套件没有额外的 expect_fail "
            "反向断言探针（应共 7 个 case：6 计分 + 1 探针）")
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "eval_dryrun.json").write_text(
        json.dumps(report_mod.redact_obj(data, extra_paths=paths),
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return {"cases": case_ids, "scoring_present": not missing,
            "target": data.get("target"),
            "extra_cases": sorted(set(case_ids) - set(SCORING_TASKS))}


def _run_eval_repeats(client: HttpClient, runtime: EditionRuntime, *,
                      raw_dir: Path, repeats: int, timeout: float,
                      poll: float) -> list[dict[str, Any]]:
    """通过 ``/api/eval/run`` + 轮询 ``/api/eval/report/{job}`` 跑 repeats 轮。"""
    raw_dir.mkdir(parents=True, exist_ok=True)
    paths = (str(runtime.root),)
    jobs: list[dict[str, Any]] = []
    for repeat in range(1, max(1, int(repeats)) + 1):
        started = time.monotonic()
        code, data = client.post("/api/eval/run", {"suite": EVAL_SUITE},
                                 timeout=120.0)
        if code == 409:
            raise EditionBenchmarkError(_scrub(
                f"{runtime.spec.name} 评测被拒（409，LLM 未配置）：{data}；"
                "这是 v1.11.24 Web 评测取当前模型/Key 的依赖项",
                extra_paths=paths))
        if code != 200 or not isinstance(data, dict) or not data.get("job_id"):
            raise EditionBenchmarkError(_scrub(
                f"{runtime.spec.name} 启动评测失败（HTTP {code}）：{data}",
                extra_paths=paths))
        job_id = str(data["job_id"])
        deadline = started + max(1.0, float(timeout))
        job: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            status_code, polled = client.get(
                f"/api/eval/report/{job_id}", timeout=60.0)
            if status_code != 200 or not isinstance(polled, dict):
                raise EditionBenchmarkError(_scrub(
                    f"{runtime.spec.name} 轮询评测结果失败（HTTP {status_code}）",
                    extra_paths=paths))
            if polled.get("status") in ("done", "error"):
                job = polled
                break
            time.sleep(max(0.2, float(poll)))
        if job is None:
            raise EditionBenchmarkError(
                f"{runtime.spec.name} 第 {repeat} 轮评测超时（>{timeout:g}s）")
        seconds = round(time.monotonic() - started, 2)
        if job.get("status") == "error":
            payload = {"status": "error",
                       "error": _scrub(job.get("error"), extra_paths=paths)}
        else:
            payload = job.get("report") or {}
        recorded = report_mod.redact_obj(
            {"repeat": repeat, "job_id": job_id, "seconds": seconds, **payload}
            if not isinstance(payload, dict) else payload,
            extra_paths=paths)
        (raw_dir / f"eval_rep{repeat}.json").write_text(
            json.dumps(recorded, ensure_ascii=False, indent=2), encoding="utf-8")
        jobs.append({"repeat": repeat, "job_id": job_id,
                     "status": job.get("status"), "seconds": seconds})
        if job.get("status") == "error":
            raise EditionBenchmarkError(_scrub(
                f"{runtime.spec.name} 第 {repeat} 轮评测失败：{job.get('error')}",
                extra_paths=paths))
        print(f"[live] {runtime.spec.name} 评测 {repeat}/{repeats} 完成"
              f"（{seconds:.1f}s）", flush=True)
    return jobs


def _run_collab(runtime: EditionRuntime, *, raw_dir: Path, provider: str,
                model: str, api_key: str, timeout: float) -> dict[str, Any]:
    """子进程跑协同基准（复用其 --live 矩阵与指标口径）。"""
    raw_dir.mkdir(parents=True, exist_ok=True)
    script = ROOT / "scripts" / "collab_benchmark.py"
    modes = ",".join(runtime.spec.collab_modes)
    command = [sys.executable, str(script), "--live", "--out", str(raw_dir),
               "--mode", modes, "--provider", provider, "--model", model]
    env = dict(runtime.env)
    env_key = _provider_env_key(provider)
    secrets_list = [api_key]
    if api_key and env_key:
        env[env_key] = api_key
    started = time.monotonic()
    log_path = runtime.logs / "collab.log"
    try:
        completed = subprocess.run(  # noqa: S603 - 本地可信脚本
            command, cwd=str(ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=max(30.0, float(timeout)))
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        returncode = completed.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = str(exc.stdout or "")[-4000:]
        stderr = str(exc.stderr or "")[-4000:]
        returncode = -1
    except OSError as exc:
        raise EditionBenchmarkError(f"启动协同基准子进程失败：{exc}") from exc
    seconds = round(time.monotonic() - started, 2)
    log_text = _scrub(f"{stdout}\n{stderr}", secrets=tuple(secrets_list),
                      extra_paths=(str(runtime.root), str(ROOT)))
    log_path.write_text(log_text, encoding="utf-8")
    if returncode != 0:
        raise EditionBenchmarkError(_scrub(
            f"{runtime.spec.name} 协同基准退出码 {returncode}："
            f"{log_text[-800:]}", secrets=tuple(secrets_list),
            extra_paths=(str(runtime.root),)))
    return {"returncode": returncode, "seconds": seconds,
            "modes": list(runtime.spec.collab_modes),
            "log": "run/<edition>/logs/collab.log（默认运行结束清理）",
            "saved": f"raw/{runtime.spec.name}/collab_benchmark.json"}


def _remove_license_artifacts(runtime: EditionRuntime) -> None:
    """清理临时许可证痕迹（环境变量随进程结束；这里兜底清隔离 HOME 里的文件）。"""
    for path in (runtime.home / ".automind" / "license",
                 runtime.project / ".automind_license"):
        try:
            if path.is_file():
                path.unlink()
        except OSError as exc:                   # pragma: no cover - 防御
            print(f"[live] 清理许可证文件失败（{path.name}）：{exc}",
                  file=sys.stderr)
    runtime.env.pop("AUTOMIND_LICENSE", None)
    runtime.env.pop("AUTOMIND_LICENSE_PUBLIC_KEY", None)
    runtime.license = {}


def run_edition(runtime: EditionRuntime, *,
                provider: str, model: str, api_key: str, api_base: str,
                repeats: int, eval_timeout: float, collab_timeout: float,
                start_timeout: float, poll: float, raw_root: Path) -> dict[str, Any]:
    """跑完一个版本的全部流程；返回 manifest 里的一条记录（失败不抛给上层）。"""
    spec = runtime.spec
    raw_dir = raw_root / spec.name
    result: dict[str, Any] = {
        "edition": spec.name, "status": "error", "expected_edition":
        spec.expected_edition, "errors": [], "timings": {},
        "eval": {"jobs": []}, "collab": {},
    }
    handle: ServerHandle | None = None
    started = time.monotonic()
    try:
        # ① 隔离 + 扩展包 + 许可证
        if spec.license_tier:
            if not runtime.wheel.get("path"):
                raise EditionBenchmarkError(
                    f"{spec.name} 缺少 staging Pro wheel，无法挂载商业特性")
            install = install_pro_wheel(runtime.wheel["path"],
                                        runtime.data_dir / "extensions")
            print(f"[live] {spec.name} 扩展包装载 {install['files']} 个文件",
                  flush=True)
            if spec.license_tier == "PRO" or spec.license_tier == "ENT":
                if runtime.license.get("license"):
                    license_info = runtime.license
                else:
                    license_info = generate_temp_license(spec.license_tier)
                _apply_license_env(runtime, license_info)
                print(f"[live] {spec.name} 临时 {spec.license_tier} 许可证已就绪"
                      f"（expiry={license_info.get('expiry') or 'n/a'}；不落盘）",
                      flush=True)

        # ② 启动 + 版本校验
        # v1.11.24 实测修复：上一版本（尤其 community）跑完评测/协同后，进程级
        # 环境或复用的 runtime 对象可能残留隔离变量；启动前把“这个版本必须是
        # 哪个数据目录 / 是否强制社区”再钉死一次，避免 pro/enterprise 被上一轮
        # 的 community env 串味而误判为 community（已在真实编排中复现）。
        runtime.env["AUTOMIND_DATA_DIR"] = str(runtime.data_dir)
        if spec.force_community:
            runtime.env["AUTOMIND_FORCE_COMMUNITY"] = "1"
        else:
            runtime.env["AUTOMIND_FORCE_COMMUNITY"] = "0"
            if runtime.license.get("license"):
                runtime.env["AUTOMIND_LICENSE"] = str(runtime.license["license"])
                runtime.env["AUTOMIND_LICENSE_PUBLIC_KEY"] = str(
                    runtime.license.get("public_key") or "")
        print(f"[live] {spec.name} 启动隔离服务 http://127.0.0.1:{spec.port}",
              flush=True)
        handle = start_server(runtime)
        status = wait_ready(handle, timeout=start_timeout, poll=poll)
        result["timings"]["ready_seconds"] = handle.ready_seconds
        observed = str(status.get("edition") or "")
        diagnosis = _license_diagnosis(HttpClient(handle.base_url))
        result["license_diagnosis"] = diagnosis
        _require_edition(spec, observed, diagnosis,
                         secrets=(str(runtime.license.get("license") or ""),),
                         extra_paths=(str(runtime.root),))
        result["observed_edition"] = observed
        print(f"[live] {spec.name} 就绪：edition={observed}", flush=True)

        # ③ 配置模型（Key 只在 HTTP body 里）
        client = HttpClient(handle.base_url)
        configured = _configure_model(client, runtime, provider=provider,
                                      model=model, api_key=api_key,
                                      api_base=api_base)
        result["model_status"] = {
            "provider": configured.get("provider"),
            "model": configured.get("model"),
            "has_api_key": bool(configured.get("has_api_key")),
            "llm_ready": configured.get("llm_ready"),
        }
        print(f"[live] {spec.name} 模型已配置：{provider}/{model}"
              f"（Key 来源：{_key_source_label(api_key)}，不打印）", flush=True)

        # ④ 评测 3 轮
        result["eval"]["preflight"] = _eval_preflight(
            client, runtime, raw_dir=raw_dir)
        result["eval"]["jobs"] = _run_eval_repeats(
            client, runtime, raw_dir=raw_dir, repeats=repeats,
            timeout=eval_timeout, poll=poll)

        # ⑤ 协同矩阵
        if spec.collab_modes:
            print(f"[live] {spec.name} 协同基准（{','.join(spec.collab_modes)}）…",
                  flush=True)
            result["collab"] = _run_collab(
                runtime, raw_dir=raw_dir, provider=provider, model=model,
                api_key=api_key, timeout=collab_timeout)
        result["status"] = "ok"
    except EditionBenchmarkError as exc:
        result["errors"].append(_scrub(exc, secrets=(api_key,),
                                       extra_paths=(str(runtime.root),)))
    except Exception as exc:                     # 非预期错误也不能拖垮其它版本
        result["errors"].append(_scrub(
            f"{type(exc).__name__}: {exc}", secrets=(api_key,),
            extra_paths=(str(runtime.root),)))
    finally:
        result["timings"]["total_seconds"] = round(time.monotonic() - started, 2)
        stop_server(handle)
        _remove_license_artifacts(runtime)
    return result


def _key_source_label(api_key: str) -> str:
    return "显式参数" if str(api_key or "").strip() else "环境变量"


# ═══════════════════════════════════════════════════════════════
# 编排入口（dry-run / live / report-only）
# ═══════════════════════════════════════════════════════════════


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def _print_plan(plan: dict[str, Any]) -> None:
    print("[dry-run] 版本隔离测评计划（不启动服务、不联网、不需要 Key）")
    print(f"  模型：{plan['provider']}/{plan['model']}"
          f"　Key 来源：{plan['api_key_source']}")
    for edition in plan["editions"]:
        print(f"  - {edition['name']:<11} port={edition['port']}  "
              f"isolation={edition['isolation']['data_dir']}  "
              f"license={edition['license']}  "
              f"collab={','.join(edition['collab_modes'])}")
    matrix = plan["matrix"]
    print(f"  评测矩阵：{matrix['eval']['cells']} 个 cell"
          f"（{len(SCORING_TASKS)} 任务 × {len(EDITIONS)} 版本 × "
          f"{matrix['eval']['repeats']} 重复）")
    print(f"  协作矩阵：{matrix['collab']['cells']} 个 cell（按版本允许的模式）")
    deps = plan["dependencies"]
    if deps.get("errors"):
        print("  依赖问题（--live 前必须解决）：")
        for item in deps["errors"]:
            print(f"    ✗ {item}")
    if deps.get("warnings"):
        print("  依赖警告：")
        for item in deps["warnings"]:
            print(f"    ⚠ {item}")
    print("  真实运行请加 --live（会消耗 Token、耗时较长）。")


def _prepare_out_dir(out_dir: Path, *, overwrite: bool) -> None:
    """准备输出目录；已有 raw/ 时默认拒绝（避免旧数据混进新报告）。"""
    raw = out_dir / "raw"
    if raw.exists() and any(raw.iterdir()) and not overwrite:
        raise EditionBenchmarkError(
            f"输出目录里已有原始数据：{raw}；请先删除/换目录，"
            "或显式加 --overwrite（会清掉旧的 raw/ 与 report/）")
    if overwrite:
        for name in ("raw", "report", "run"):
            shutil.rmtree(out_dir / name, ignore_errors=True)
    (out_dir / "raw").mkdir(parents=True, exist_ok=True)


def run_live(args: argparse.Namespace, editions: tuple[EditionSpec, ...],
             dependencies: DependencyState) -> int:
    """真实执行三个隔离版本并把原始报告 + manifest 落盘。返回退出码。"""
    errors = dependencies.critical_errors
    if errors:
        print("无法开始 --live：依赖预检未通过", file=sys.stderr)
        for item in errors:
            print(f"  ✗ {item}", file=sys.stderr)
        return EXIT_CONFIG
    out_dir = Path(args.out)
    try:
        _prepare_out_dir(out_dir, overwrite=bool(args.overwrite))
    except EditionBenchmarkError as exc:
        print(f"无法开始 --live：{exc}", file=sys.stderr)
        return EXIT_CONFIG
    raw_root = out_dir / "raw"
    run_root = out_dir / "run"
    if run_root.exists():
        shutil.rmtree(run_root, ignore_errors=True)
    run_root.mkdir(parents=True, exist_ok=True)
    api_key = str(args.api_key or os.environ.get(
        _provider_env_key(args.provider), "")).strip()

    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dry_run": False,
        "core_version": _read_core_version(),
        "provider": args.provider,
        "model": args.model,
        "api_key_source": dependencies.credentials.get("source") or "",
        "eval_suite": {"name": EVAL_SUITE, "repeats": int(args.repeats),
                       "scoring_tasks": list(SCORING_TASKS),
                       "probe_cases": list(
                           dependencies.suite.get("probe_cases") or []),
                       "probe_rule": "按 expect_fail 过滤，不计分、不参与通过率"},
        "collab": {"tasks": list(COLLAB_TASKS),
                   "modes_by_edition": {name: list(modes)
                                        for name, modes in COLLAB_MODES.items()}},
        "editions": {},
        "warnings": list(dependencies.warnings),
        "notes": [
            "数字只来自本次真实运行的原始报告；失败/缺失由报告侧判失败。",
            "API Key 只经 HTTP /api/config 传递，不打印、不落盘。",
            "临时许可证仅存在于子进程环境变量，结束即清理。",
        ],
    }
    manifest_path = out_dir / "manifest.json"
    _write_json(manifest_path, manifest)

    exit_code = EXIT_OK
    try:
        for spec in editions:
            runtime = prepare_runtime(spec, run_root, wheel=dependencies.pro_wheel)
            if spec.license_tier and not args.license_file:
                # 许可证在 run_edition 内生成（需要 pro 源码；出错按版本失败记录）
                pass
            elif spec.license_tier:
                runtime.license = load_license_files(
                    args.license_file, args.license_public_key)
                _apply_license_env(runtime, runtime.license)
            print(f"[live] ===== {spec.name}（port {spec.port}）=====", flush=True)
            record = run_edition(
                runtime, provider=args.provider, model=args.model,
                api_key=api_key, api_base=args.api_base,
                repeats=int(args.repeats), eval_timeout=float(args.eval_timeout),
                collab_timeout=float(args.collab_timeout),
                start_timeout=float(args.start_timeout),
                poll=float(args.poll_interval), raw_root=raw_root)
            record["isolation"] = runtime.as_dict()
            manifest["editions"][spec.name] = record
            _write_json(manifest_path, manifest)
            if record.get("status") != "ok":
                exit_code = EXIT_CONFIG
                print(f"[live] {spec.name} 失败：{record.get('errors')}",
                      file=sys.stderr)
    finally:
        if not args.keep_run_dirs:
            shutil.rmtree(run_root, ignore_errors=True)
        manifest["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        _write_json(manifest_path, manifest)

    if not args.no_report:
        report_code, _ = report_mod.run_report(out_dir, out_dir / "report")
        if report_code != report_mod.EXIT_OK:
            print("报告校验未通过：真实数据不完整，未产出汇总"
                  f"（详见 {out_dir / 'report' / 'validation.json'}）。",
                  file=sys.stderr)
            exit_code = EXIT_CONFIG
        else:
            print(f"[live] 汇总报告：{out_dir / 'report'}")
    return exit_code


def _report_only(args: argparse.Namespace) -> int:
    """不跑编排，只对已有原始报告做校验 + 聚合。"""
    run_dir = Path(args.run or args.out)
    code, result = report_mod.run_report(run_dir, Path(args.out) / "report")
    if code == report_mod.EXIT_OK:
        print("报告已生成：")
        for key, path in (result.get("outputs") or {}).items():
            print(f"  {key}: {path}")
        return EXIT_OK
    print(f"原始数据不完整/非法：{result.get('errors', 0)} 项错误；"
          f"未产出汇总（详见 {result.get('validation', '')}）。", file=sys.stderr)
    return EXIT_CONFIG


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/edition_benchmark.py",
        description="版本隔离测评：community/pro/enterprise 同套件对比"
                    "（默认 dry-run，不启动服务、不联网）")
    parser.add_argument("--live", action="store_true",
                        help="真实执行（启动三个隔离 server、跑评测与协同基准；"
                             "消耗 Token）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只输出计划矩阵（默认行为；与 --live 互斥）")
    parser.add_argument("--report-only", action="store_true",
                        help="不跑编排，只聚合 --run/--out 下已有的原始报告")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="产出根目录（默认 .automind/edition_benchmark）")
    parser.add_argument("--run", default="",
                        help="--report-only 的原始数据根目录（默认同 --out）")
    parser.add_argument("--port-base", type=int, default=PORT_BASE,
                        help=f"community 端口（pro/enterprise = +1/+2；默认 {PORT_BASE}）")
    parser.add_argument("--provider", default=DEFAULT_PROVIDER)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-key", default="",
                        help="API Key（也可用对应环境变量；**不会打印**）")
    parser.add_argument("--api-base", default="", help="可选 api_base 覆盖")
    parser.add_argument("--pro-wheel", default="",
                        help="staging Pro wheel 路径（默认取 pro/dist 最新）")
    parser.add_argument("--license-file", default="",
                        help="改用已有许可证文件（需同时给 --license-public-key）")
    parser.add_argument("--license-public-key", default="")
    parser.add_argument("--repeats", type=int, default=EVAL_REPEATS,
                        help=f"每次版本的评测重复次数（任务书要求 {EVAL_REPEATS}）")
    parser.add_argument("--eval-timeout", type=float, default=EVAL_TIMEOUT,
                        help="单轮评测墙钟上限（秒）")
    parser.add_argument("--collab-timeout", type=float, default=COLLAB_TIMEOUT,
                        help="协同基准子进程墙钟上限（秒）")
    parser.add_argument("--start-timeout", type=float, default=START_TIMEOUT,
                        help="单个隔离服务就绪等待上限（秒）")
    parser.add_argument("--poll-interval", type=float, default=POLL_INTERVAL,
                        help="轮询间隔（秒）")
    parser.add_argument("--keep-run-dirs", action="store_true",
                        help="保留隔离运行目录（排障用；默认清理）")
    parser.add_argument("--overwrite", action="store_true",
                        help="清掉输出目录里旧的 raw/run/report（默认拒绝混用）")
    parser.add_argument("--no-report", action="store_true",
                        help="--live 后不自动聚合报告（仍写 manifest）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.live and args.dry_run:
        print("--live 与 --dry-run 互斥：默认就是 dry-run。", file=sys.stderr)
        return EXIT_CONFIG
    if args.report_only and args.live:
        print("--report-only 与 --live 互斥。", file=sys.stderr)
        return EXIT_CONFIG
    if args.report_only:
        return _report_only(args)

    editions = build_editions(args.port_base)
    suite = EVAL_SUITE
    api_key = str(args.api_key or "").strip()
    dependencies = collect_dependencies(
        provider=args.provider, model=args.model, api_key=api_key,
        suite=suite, pro_wheel=args.pro_wheel, port_base=args.port_base,
        editions=editions)
    plan = build_plan(editions=editions, repeats=args.repeats,
                      provider=args.provider, model=args.model,
                      out_dir=args.out, dependencies=dependencies)
    plan["dry_run"] = not args.live
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not args.live:
        path = _write_json(out_dir / "plan.json", plan)
        _print_plan(plan)
        print(f"[dry-run] 计划已写入 {path}")
        return EXIT_OK

    plan_path = _write_json(out_dir / "plan.json", plan)
    print(f"[live] 计划已写入 {plan_path}")
    if args.license_file and not args.license_public_key:
        print("--license-file 需要同时给 --license-public-key", file=sys.stderr)
        return EXIT_CONFIG
    try:
        return run_live(args, editions, dependencies)
    except KeyboardInterrupt:
        print("已中断。", file=sys.stderr)
        return EXIT_ABORTED
    except EditionBenchmarkError as exc:
        print(f"无法完成测评：{exc}", file=sys.stderr)
        return EXIT_CONFIG


if __name__ == "__main__":                       # pragma: no cover - CLI 入口
    raise SystemExit(main())
