"""评测编排与 CLI —— ``python -m automind.eval run <suite.yml> [--json] [--out report.json]``。

流程：逐个任务 → 独立临时工作区 → 执行器跑 → 逐条断言 → 汇总报告。

三条不可动摇的行为约定：

  1. **无 Key 时必须明确报"LLM 未配置，无法评测"并退出码 2**。
     绝不允许把"跑不了"记成"全部失败"或"全部通过"：前者会让人去查模型退化，
     后者会让一次没跑的评测看起来像绿灯 —— 两种都是评测框架最严重的失职。
  2. **任务之间零污染**：每个任务自己的临时目录 + 隔离的 ``AUTOMIND_DATA_DIR``
     （轨迹/数据库都落不到仓库里）。
  3. **退出码**：0=全部通过；1=有任务未通过；2=配置/用法问题（无 Key、套件写错）；
     3=评测被中断（执行器整体不可用）。
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

from automind.core.logging import get_logger
from automind.eval.assertions import EvalOutcome, check_all
from automind.eval.executors import (
    AutoMindExecutor,
    Executor,
    LLMTarget,
    RunContext,
    detect_llm_target,
    list_artifacts,
    record_tool_calls,
)
from automind.eval.pricing import estimate
from automind.eval.report import (
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_PASSED,
    CaseResult,
    EvalReport,
)
from automind.eval.suite import EvalCase, EvalSuite, SuiteError, find_suites, load_suite

logger = get_logger("automind.eval.runner")

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_CONFIG = 2
EXIT_ABORTED = 3

#: 断言失败信息里输出预览的长度
_OUTPUT_PREVIEW = 2000


# ═══════════════════════════════════════════════════════════════
# 环境隔离
# ═══════════════════════════════════════════════════════════════


@contextlib.contextmanager
def isolated_data_dir(root: Path) -> Iterator[Path]:
    """把 ``AUTOMIND_DATA_DIR`` 指向评测自己的目录，跑完恢复。

    为什么必须做：agent 的轨迹、检查点、会话库都经 ``core/paths.py`` 落到
    ``data_dir()``。不隔离的话，一次评测会在用户的仓库里留下 ``.automind/``
    的一堆中间产物（甚至把真实运行的历史按配额淘汰掉）。
    """
    key = "AUTOMIND_DATA_DIR"
    old = os.environ.get(key)
    target = root / "data"
    target.mkdir(parents=True, exist_ok=True)
    os.environ[key] = str(target)
    try:
        yield target
    finally:
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old


# ═══════════════════════════════════════════════════════════════
# 单任务
# ═══════════════════════════════════════════════════════════════


def _prepare_workspace(workdir: Path, case: EvalCase) -> Path:
    """建任务工作区并预置 ``setup`` 里的文件。"""
    ws = workdir / "work"
    ws.mkdir(parents=True, exist_ok=True)
    for rel, content in (case.setup or {}).items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return ws


def _with_timeout_safeguard(outcome: EvalOutcome, ctx: RunContext) -> EvalOutcome:
    """执行器若自己处理超时（推荐做法，它能真正中止内部循环），这里只兜底。

    为什么 runner 还要兜一层：执行器是**可注入**的，一个忘了实现超时的自定义
    执行器会让整场评测挂死 —— 而挂死的评测比失败的评测更难排查。
    """
    limit = ctx.timeout_seconds
    if not limit or outcome.timed_out or outcome.seconds <= limit:
        return outcome
    return replace(outcome, timed_out=True,
                   error=outcome.error or f"任务超时（{limit:g}s 上限）")


async def run_case(case: EvalCase, executor: Executor, workdir: Path,
                   model: str = "", default_timeout: float = 0.0,
                   keep_workspace: bool = False) -> CaseResult:
    """执行一个任务并判定其断言。异常一律转成 ``error`` 状态，不让整场评测崩掉。"""
    timeout = case.timeout_seconds or default_timeout or 0.0
    ws = _prepare_workspace(workdir, case)
    ctx = RunContext(case=case, workspace=ws, mode=case.mode,
                     timeout_seconds=timeout)
    started = time.monotonic()
    try:
        outcome = await executor.run(ctx)
    except TimeoutError as e:
        outcome = EvalOutcome(success=False, seconds=time.monotonic() - started,
                              error=f"任务超时：{e}", timed_out=True)
    except BaseException as e:                   # 执行器自身故障：记 error，不扩散
        import asyncio

        if isinstance(e, asyncio.CancelledError):
            raise
        outcome = EvalOutcome(success=False, seconds=time.monotonic() - started,
                             error=f"执行器异常：{type(e).__name__}: {e}")
    outcome = _with_timeout_safeguard(outcome, ctx)
    if not outcome.artifacts:
        outcome.artifacts = list_artifacts(ws, ctx.ignore_dirs)
    checks = check_all(case.assertions, outcome, ws)
    failed = [c for c in checks if not c.passed]

    if outcome.timed_out:
        status = STATUS_FAILED                    # 超时属于"没达到要求"，不是框架故障
    elif outcome.error:
        # 出错时再看是谁的错：**执行器自己**（agent 建不起来、入口不对、模型
        # 不可用）属于"框架/环境问题"，而"任务跑完但工具/模型报错"仍按断言
        # 结果判定。把两者混为一谈会同时损害两种排查方向。
        status = STATUS_ERROR if outcome.error.startswith("执行器异常") else STATUS_FAILED
    elif case.expect_fail:
        # 反向断言：这条任务**期望**有断言失败（用它体检判定链路本身是否在工作）。
        # 因此"失败了"才算通过，"竟然全过"才是问题 —— 后者意味着断言根本没被判定。
        status = STATUS_PASSED if failed else STATUS_FAILED
    else:
        status = STATUS_PASSED if not failed else STATUS_FAILED

    cost = estimate(outcome.prompt_tokens, outcome.completion_tokens, model)
    result = CaseResult(
        id=case.id, status=status, mode=case.mode,
        seconds=outcome.seconds or (time.monotonic() - started),
        prompt_tokens=outcome.prompt_tokens,
        completion_tokens=outcome.completion_tokens,
        total_tokens=outcome.total_tokens,
        estimated_cost_usd=cost.usd,
        tool_calls=list(outcome.tool_calls),
        assertions=[c.as_dict() for c in checks],
        output_preview=(outcome.output or "")[:_OUTPUT_PREVIEW],
        error=outcome.error,
        timed_out=outcome.timed_out,
        workspace=str(ws),
        expect_fail=case.expect_fail,
    )
    if not keep_workspace:
        # 默认删掉工作区：一次评测会留下几十个目录，攒起来就是垃圾场。
        # 需要排障时 runner 会把路径写进报告（keep_workspace=True 则保留）。
        with contextlib.suppress(Exception):
            import shutil

            shutil.rmtree(ws, ignore_errors=True)
    return result


# ═══════════════════════════════════════════════════════════════
# 套件
# ═══════════════════════════════════════════════════════════════


def _select(suite: EvalSuite, include: list[str] | None,
            limit: int = 0) -> list[EvalCase]:
    cases = suite.cases
    if include:
        wanted = {x.strip() for x in include if x.strip()}
        missing = wanted - {c.id for c in cases}
        if missing:
            raise SuiteError(f"--include 里的任务 id 不存在：{sorted(missing)}")
        cases = [c for c in cases if c.id in wanted]
    if limit:
        cases = cases[:limit]
    return cases


@contextlib.contextmanager
def _case_root(keep: bool = False) -> Iterator[Path]:
    """给一次套件运行准备一个**一定可写**的根目录。

    为什么不用 ``tempfile.TemporaryDirectory``：它内部用 ``mkdir(mode=0o700)``，
    在 Windows 上等于给目录打上**只读属性**；受限环境（本机沙箱、部分企业
    CI）会因此拒绝往里写任何文件，整场评测直接崩在"建目录"这一步。实测同一
    进程、同一位置：``os.mkdir(p)`` 可写，``os.mkdir(p, 0o700)`` 与
    ``tempfile.mkdtemp()`` 不可写。

    因此这里自己建：默认权限 + 用完删除；系统临时目录不可用时退回数据目录
    下（评测本来就要求可写）。``keep=True``（``--keep-workspace``）时不删，
    否则报告里给的 workspace 路径在运行结束后根本不存在，等于假线索。
    """
    candidates = [Path(tempfile.gettempdir()) / f"automind-eval-{os.urandom(4).hex()}"]
    try:
        from automind.core.paths import data_dir

        candidates.append(Path(data_dir()) / "eval-tmp" / os.urandom(4).hex())
    except Exception:
        pass
    last: Exception | None = None
    for cand in candidates:
        try:
            cand.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            last = e
            continue
        try:
            yield cand
        finally:
            if not keep:
                # 忽略失败：删不掉临时目录不该让整场评测报错
                import shutil

                shutil.rmtree(cand, ignore_errors=True)
        return
    raise RuntimeError(f"无法创建评测工作目录：{last}")


async def run_suite(suite: EvalSuite, executor: Executor, model: str = "",
                    provider: str = "", include: list[str] | None = None,
                    limit: int = 0, default_timeout: float = 0.0,
                    keep_workspace: bool = False,
                    check_credentials: bool = True,
                    target: LLMTarget | None = None) -> EvalReport:
    """跑完一个套件并返回报告。

    ``check_credentials=False`` 用于测试注入的假执行器（离线不应该被 Key 挡住）；
    真实执行器**必须**走检查，否则 CI 上"没 Key"会伪装成"模型退化"。
    """
    report = EvalReport(suite=suite.name, path=suite.path, model=model,
                        provider=provider)
    if check_credentials:
        tgt = target or detect_llm_target(provider, model)
        if not tgt.available:
            report.aborted = True
            report.abort_reason = tgt.reason or "LLM 未配置，无法评测。"
            report.finished_at = time.time()
            return report
        report.provider = report.provider or tgt.provider
        report.model = report.model or tgt.model

    cases = _select(suite, include, limit)
    with _case_root(keep=keep_workspace) as root, isolated_data_dir(root):
        for case in cases:
            workdir = root / case.id
            workdir.mkdir(parents=True, exist_ok=True)
            res = await run_case(case, executor, workdir, model=report.model,
                                 default_timeout=default_timeout,
                                 keep_workspace=keep_workspace)
            report.cases.append(res)
    report.finished_at = time.time()
    if report.total and report.error_cases == report.total:
        report.notes.append(
            "全部任务都以 error 结束：优先怀疑执行器/环境（模型入口、权限、依赖），"
            "而不是模型能力。")
    return report


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════


def _cmd_list() -> int:
    suites = find_suites()
    if not suites:
        print("没有找到内置套件。", file=sys.stderr)
        return EXIT_CONFIG
    for p in suites:
        try:
            s = load_suite(p)
            print(f"{p}  name={s.name}  任务数={len(s.cases)}  "
                  f"断言数={s.total_assertions()}  {s.description}")
        except SuiteError as e:
            print(f"{p}  【套件格式错误】{e}", file=sys.stderr)
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m automind.eval",
        description="AutoMind 评测框架：跑套件、出报告")
    sub = ap.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="运行一个套件")
    run.add_argument("suite", help="套件 YAML 路径")
    run.add_argument("--out", default="", help="报告写到哪里（JSON）")
    run.add_argument("--json", action="store_true", help="把报告打到 stdout（JSON）")
    run.add_argument("--dry-run", action="store_true",
                     help="只检查套件与配置，不调用模型（不需要 API Key）")
    run.add_argument("--model", default="", help="覆盖模型名")
    run.add_argument("--provider", default="", help="覆盖提供商")
    run.add_argument("--include", default="", help="只跑这些任务 id（逗号分隔）")
    run.add_argument("--limit", type=int, default=0, help="只跑前 N 个任务")
    run.add_argument("--timeout", type=float, default=0.0,
                     help="任务级默认超时（秒），任务里写了的以任务为准")
    run.add_argument("--keep-workspace", action="store_true",
                     help="保留任务工作目录（排障用）")
    run.add_argument("--verbose", action="store_true", help="打印全部断言明细")

    sub.add_parser("list", help="列出内置套件")
    return ap


def _plan_text(suite: EvalSuite, cases: list[EvalCase], target: LLMTarget) -> str:
    lines = [f"套件 {suite.name}（{suite.path}）",
             f"模式 {suite.mode}　任务 {len(cases)} 个　"
             f"断言 {sum(len(c.assertions) for c in cases)} 条",
             f"模型 {target.provider}/{target.model}　"
             f"凭据 {target.api_key_source or '（无）'}"
             + ("　→ 可运行" if target.available else "　→ 缺失，无法评测")]
    for c in cases:
        kinds = ", ".join(a.type for a in c.assertions) or "（无断言）"
        # 提示词里常有多行（YAML 块），折叠成一行否则清单会被撑散
        one_line = " ".join((c.prompt or "").split())[:60]
        lines.append(f"  - {c.id} [{c.mode}] {one_line}")
        lines.append(f"      断言：{kinds}")
    return "\n".join(lines)


def run_command(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "list":
        return _cmd_list()

    try:
        suite = load_suite(args.suite)
    except SuiteError as e:
        print(f"套件格式错误：{e}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        cases = _select(suite, [x for x in (args.include or "").split(",") if x],
                        args.limit)
    except SuiteError as e:
        print(f"用法错误：{e}", file=sys.stderr)
        return EXIT_CONFIG

    target = detect_llm_target(args.provider, args.model)
    if args.dry_run:
        print("[dry-run] " + _plan_text(suite, cases, target))
        if not target.available:
            print("[dry-run] 提示：当前没有可用凭据，去掉 --dry-run 前请先配置 API Key。")
        return EXIT_OK

    if not target.available:
        # 关键行为：明确区分"配置问题"与"模型退化"，并给非 0 退出码
        print(f"LLM 未配置，无法评测：{target.reason}", file=sys.stderr)
        print("（评测不会把'跑不了'记成'全部失败'或'全部通过'——请先配置凭据。）",
              file=sys.stderr)
        return EXIT_CONFIG

    executor = AutoMindExecutor(provider=args.provider, model=args.model,
                                keep_workspace=args.keep_workspace)
    import asyncio

    report = asyncio.run(run_suite(
        suite, executor, model=target.model, provider=target.provider,
        include=[x for x in (args.include or "").split(",") if x],
        limit=args.limit, default_timeout=args.timeout,
        keep_workspace=args.keep_workspace, target=target))

    if report.aborted:
        print(f"评测中止：{report.abort_reason}", file=sys.stderr)
        return EXIT_CONFIG
    if args.json:
        print(report.to_json())
    else:
        print(report.render())
        if not args.verbose and report.passed_cases == report.total:
            print("（`--verbose` 可打印通过任务的全部断言明细）")
    if args.out:
        print(f"报告已写入 {report.write(args.out)}")
    return EXIT_OK if report.all_passed else EXIT_FAILURES


def main(argv: list[str] | None = None) -> int:       # pragma: no cover - CLI 入口
    try:
        return run_command(argv)
    except KeyboardInterrupt:
        print("已中断。", file=sys.stderr)
        return EXIT_ABORTED


__all__ = [
    "EXIT_ABORTED", "EXIT_CONFIG", "EXIT_FAILURES", "EXIT_OK",
    "AutoMindExecutor", "CaseResult", "EvalReport", "EvalSuite", "Executor",
    "detect_llm_target", "isolated_data_dir", "load_suite", "record_tool_calls",
    "run_case", "run_command", "run_suite",
]
