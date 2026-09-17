"""工作流 CLI —— `python -m automind.workflow run <file.yaml>`。

为什么 CLI 要单独存在（Web 已经有了）
-----------------------------------
工作流的价值有一半在**CI 里**：客户把 `change_request.yaml` 提交进仓库，
流水线上每次改动都跑一遍校验（甚至 dry-run），合不进去就说明有人改错了流程。
所以这个入口的退出码是核心接口，不能只是"打印点东西"：

    0    全绿（含 dry-run：试运行本身没有任何失败）
    1    校验通过、也真的跑了，但有步骤失败（CI 据此拦住合并）
    2    文件不存在 / 校验失败 / 入参给错（**还没开始跑**，或根本没法跑）
    130  被取消（Ctrl+C；沿用 shell 对 SIGINT 的惯例）

**只看有没有失败步骤，不看整轮状态叫什么名字** —— `on_failure: continue`
的步骤在报告里是 `failed`，退出码就必须是 1。把部分成功当成功放过去，
CI 门禁就成了摆设，而这正是本模块最容易犯的错。

日志一律走 stderr，stdout 只留结果：`--json` 的输出要能直接喂给
`jq` / 前端 / 另一个程序，掺进日志就没法用了。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from automind.workflow.exceptions import WorkflowLoadError
from automind.workflow.executor import (
    EXIT_BAD_WORKFLOW,
    EXIT_OK,
    WorkflowRun,
    check_inputs,
    jsonable,
    run_workflow,
)
from automind.workflow.loader import WorkflowLoader, load_version_checked
from automind.workflow.schema import WorkflowSchema

#: 退出码语义（与 pyproject/CI 文档保持一致）
EXIT_USAGE = EXIT_BAD_WORKFLOW          # 2：用法/校验错误
EXIT_CANCELLED = 130


# ═══════════════════════════════════════════════════════════════
# 参数解析
# ═══════════════════════════════════════════════════════════════


def _parse_input(raw: str, *, raw_string: bool) -> tuple[str, Any]:
    """把 `k=v` 解析成 `(k, v)`。

    默认把值当 **YAML 标量**解析（`count=3` → int 3，`flag=true` → True）：
    工作流声明的入参是有类型的，若一律按字符串塞进去，`{{ inputs.count }}`
    渲染出来是 "3"，而后端接口收到字符串数字往往直接 400 —— 报错在对端，
    排查成本很高。`--raw` 关掉这个行为（值里有冒号/逗号等 YAML 特殊字符时用）。
    """
    if "=" not in raw:
        raise ValueError(f"入参格式应为 key=value，实际是 {raw!r}")
    key, value = raw.split("=", 1)
    key = key.strip()
    if not key:
        raise ValueError(f"入参名不能为空：{raw!r}")
    if raw_string:
        return key, value
    import yaml

    try:
        parsed = yaml.safe_load(value)
    except yaml.YAMLError:
        return key, value
    # 空字符串经 YAML 解析会变成 None；入参里空串是常见且合法的输入（"可选备注"），
    # 不能悄悄变成"未提供"
    return key, ("" if parsed is None and value.strip() == "" else parsed)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m automind.workflow",
        description="AutoMind 工作流（工作流即代码）：校验、试运行与确定性执行",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m automind.workflow run examples/06-workflow/change_request.yaml "
            "--dry-run\n"
            "  python -m automind.workflow run examples/06-workflow/hello.yaml "
            "--input path=out.txt --input content=hi\n"
            "  python -m automind.workflow validate examples/06-workflow/change_request.yaml\n"
            "\n退出码：0 全绿 / 1 有步骤失败 / 2 文件或校验错误 / 130 被取消\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    run_p = sub.add_parser("run", help="校验并执行一个工作流文件")
    run_p.add_argument("file", help="工作流文件路径（.yaml / .yml / .json）")
    run_p.add_argument("--input", "-i", action="append", default=[], metavar="k=v",
                       help="入参，可重复；值默认按 YAML 标量解析")
    run_p.add_argument("--raw", action="store_true",
                       help="入参值一律按字符串处理（不做 YAML 解析）")
    run_p.add_argument("--dry-run", action="store_true",
                       help="只渲染参数并列出将执行什么，不调用任何工具/模型")
    run_p.add_argument("--json", action="store_true",
                       help="把执行报告以 JSON 输出到 stdout（日志仍在 stderr）")
    run_p.add_argument("--include-outputs", action="store_true",
                       help="JSON 报告里附带各步骤的完整输出（默认只有摘要）")
    run_p.add_argument("--registry", choices=("builtin", "none"), default="builtin",
                       help="注入的工具注册表；none = 不接工具（只验流程结构）")
    run_p.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                       help="额外设置环境变量，供模板 {{ env.NAME }} 使用")
    run_p.add_argument("--quiet", "-q", action="store_true", help="不打印步骤进度")

    val_p = sub.add_parser("validate", help="只校验不执行（CI 用）")
    val_p.add_argument("file", help="工作流文件路径")
    val_p.add_argument("--json", action="store_true", help="以 JSON 输出校验结果")
    val_p.add_argument("--unknown-fields", choices=("error", "warning"), default="error",
                       help="未知字段的处理：error（默认，拒绝加载）/ warning（记警告放行）")

    return parser


# ═══════════════════════════════════════════════════════════════
# 输出
# ═══════════════════════════════════════════════════════════════


def print_issue(issue: Any) -> None:
    """打印一条校验问题到 stderr（stdout 留给 --json 的结果）。"""
    print(issue.format(), file=sys.stderr)


def print_dry_run_plan(schema: WorkflowSchema, run: WorkflowRun) -> None:
    """把 dry-run 的"将执行什么"打印成人能读的清单。

    格式刻意与 `schema.summarize()` 对齐：评审方可以先看文件摘要、
    再看**渲染后**的实际参数，两者对照就能发现"模板取错了值"。
    """
    print(schema.summarize())
    print("")
    print("── 试运行计划（未执行任何工具 / 未调用模型）──")
    for i, step in enumerate(run.steps, 1):
        detail = step.detail
        print(f"{i}. {step.id}（第 {step.line} 行）：", end="")
        if step.status == "failed":
            print(f"渲染失败 —— {step.error}")
            continue
        if step.type == "tool":
            print(f"调用工具 {detail.get('tool')}")
            args = detail.get("args") or {}
            for key, value in args.items():
                print(f"      {key} = {_short(value)}")
            if "warning" in detail:
                print(f"      ⚠ {detail['warning']}")
            plan = detail.get("plan")
            if plan:
                for line in str(plan).splitlines():
                    print(f"      {line}")
        elif step.type == "llm":
            print(f"调用模型生成（提示词 {detail.get('prompt_chars', 0)} 字）")
            print(f"      提示词开头：{_short(detail.get('prompt_preview'))}")
        elif step.type == "human":
            print("等待人工审批（试运行不会真的询问）")
            print(f"      审批内容开头：{_short(detail.get('prompt_preview'))}")
        elif step.type == "branch":
            print(f"判断 {detail.get('condition')}")
            print(f"      左侧渲染为 {_short(detail.get('left_preview'))}，"
                  f"右侧为 {_short(detail.get('right'))}")
            print(f"      成立 → {detail.get('then')}；否则 → {detail.get('else')}")
        else:
            print(step.type)
    print("")
    print(run.summary_line())


def print_run_report(run: WorkflowRun) -> None:
    """真实执行的报告（stderr，避免污染 --json）。"""
    out = sys.stderr
    for step in run.steps:
        mark = {"ok": "✓", "failed": "✗", "aborted": "✗", "skipped": "–",
                "cancelled": "⊘"}.get(step.status, "?")
        line = (f"  {mark} [{step.id}] {step.status}  "
                f"{step.duration_ms:.0f}ms"
                + (f"  重试 {step.retries} 次" if step.retries else ""))
        if step.output_digest:
            line += f"  输出摘要 {step.output_digest}"
        print(line, file=out)
        if step.error:
            print(f"      原因：{step.error}", file=out)
    print(run.summary_line(), file=out)
    if run.error:
        print(f"整轮说明：{run.error}", file=out)
    for warning in run.warnings:
        print(f"警告：{warning}", file=out)


def _short(value: Any, limit: int = 160) -> str:
    """把任意值压成一行短文本（dry-run 计划里展示参数用）。

    先过 `jsonable` 再过 `json.dumps`：直接 `json.dumps(value, default=str)`
    对 dry-run 的占位对象无效（它是 dict 子类，json 会把它当空对象编成 `{}`），
    于是"这里还没有值"会被显示成"这里是个空对象" —— 两种完全不同的含义。
    """
    text = value if isinstance(value, str) else json.dumps(
        jsonable(value), ensure_ascii=False, default=str)
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


# ═══════════════════════════════════════════════════════════════
# 注册表 / 模型注入
# ═══════════════════════════════════════════════════════════════


def build_cli_registry(mode: str, *, project_root: str | None = None) -> Any:
    """按 --registry 造一个工具注册表。

    为什么 CLI 不默认接 LLM：`llm` 步骤需要真实凭据与网络，而 CLI 的主要用途是
    "校验 + 试运行 + 在 CI 里跑纯工具流程"。需要模型参与的流程请从服务端调
    `run_workflow(llm=...)`（见 docs/WORKFLOWS.md 的接线片段），不要让 CLI 去猜配置 ——
    否则很容易出现"命令行能跑、接口跑不通"这种最难查的差异。

    注册来源分两级：
      1. 优先复用 agent 的注册逻辑（`_register_default_tools`），保证 CLI 与 Web
         用的是**同一批**工具（各注册一份迟早会出现"命令行有、界面上没有"的偏差）；
      2. 它依赖完整配置对象，构造失败时退回**最小可用集**（文件读写 + 沙箱），
         并明确打印一条警告 —— 静默少掉一批工具比直接报错更难排查。
    """
    if mode == "none":
        return None
    from automind.tools.base import ToolRegistry

    registry = ToolRegistry()
    try:
        from automind.agent import AutoMindAgent

        agent = AutoMindAgent.__new__(AutoMindAgent)     # 只借方法，不跑完整初始化
        agent.tool_registry = registry
        agent.config = _minimal_config(project_root)     # _register_default_tools 需要
        agent.tool_registration_failures = []
        agent._register_default_tools()
        return registry
    except Exception as exc:
        print(f"警告：复用完整内置工具失败（{type(exc).__name__}: {exc}），"
              "退回最小工具集（file_read / file_write / file_edit / python_sandbox）；"
              "需要其它工具请用服务端接口执行", file=sys.stderr)
    return _minimal_registry(registry, project_root)


def _minimal_config(project_root: str | None = None) -> Any:
    """给 `_register_default_tools` 用的最小配置对象。

    为什么造一个轻量替身而不是 `AgentConfig()`：完整配置会去读用户的配置文件与
    环境变量（可能触发一堆与本命令无关的校验/提示）。这里只需要
    `project_root` 与 `execution` 上的几个字段，用替身最省事也最可预期。
    """
    from types import SimpleNamespace

    root = str(Path(project_root).resolve() if project_root else Path.cwd())

    class _Execution:
        tool_timeout_seconds = 300.0
        tool_timeout_max_seconds = 1800.0
        terminal_background_enabled = True

    return SimpleNamespace(project_root=root, execution=_Execution())


def _minimal_registry(registry: Any, project_root: str | None) -> Any:
    """兜底注册表：只装不依赖可选库、且能约束写入范围的文件工具。"""
    root = str(Path(project_root).resolve() if project_root else Path.cwd())
    try:
        from automind.tools.file_editor import FileEditTool, FileReadTool, FileWriteTool
        from automind.tools.sandbox import PythonSandboxTool

        registry.register(FileReadTool(project_root=root))
        registry.register(FileWriteTool(project_root=root))
        registry.register(FileEditTool(project_root=root))
        registry.register(PythonSandboxTool())
    except Exception as exc:                            # pragma: no cover - 环境残缺
        print(f"警告：最小工具集也注册失败（{type(exc).__name__}: {exc}）", file=sys.stderr)
    return registry


def registry_tool_names(registry: Any) -> list[str] | None:
    if registry is None:
        return None
    try:
        return list(registry.list_names())
    except Exception:                                    # pragma: no cover - 防御性
        return None


# ═══════════════════════════════════════════════════════════════
# 子命令
# ═══════════════════════════════════════════════════════════════


def cmd_validate(args: argparse.Namespace) -> int:
    loader = WorkflowLoader(unknown_fields=args.unknown_fields)
    schema, errors, warnings = loader.try_load(args.file)
    if args.json:
        payload = {
            "file": str(args.file),
            "ok": schema is not None,
            "errors": [e.as_dict() for e in errors],
            "warnings": [w.as_dict() for w in warnings],
            "schema": schema.as_dict() if schema is not None else None,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        for issue in errors:
            print_issue(issue)
        for issue in warnings:
            print_issue(issue)
        if schema is not None:
            print(schema.summarize())
            print(f"\n校验通过：{args.file}"
                  + (f"（{len(warnings)} 条警告）" if warnings else ""))
    return EXIT_OK if schema is not None else EXIT_USAGE


def cmd_run(args: argparse.Namespace) -> int:
    # ── 1. 入参 ──────────────────────────────────────
    provided: dict[str, Any] = {}
    try:
        for item in args.input:
            key, value = _parse_input(item, raw_string=args.raw)
            provided[key] = value
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_USAGE

    for item in args.set:
        if "=" not in item:
            print(f"错误：--set 的格式应为 NAME=VALUE，实际是 {item!r}", file=sys.stderr)
            return EXIT_USAGE
        name, value = item.split("=", 1)
        os.environ[name.strip()] = value

    # ── 2. 校验（先校验再注入工具：连文件都不合法时不该有任何副作用）──
    registry = build_cli_registry(args.registry)
    loader = WorkflowLoader(tool_names=registry_tool_names(registry))
    schema, errors, warnings = loader.try_load(args.file)
    if schema is None:
        for issue in errors:
            print_issue(issue)
        print(f"\n工作流文件不合法，未执行任何步骤：{args.file}", file=sys.stderr)
        if args.json:
            print(json.dumps({"file": str(args.file), "ok": False,
                              "errors": [e.as_dict() for e in errors]},
                             ensure_ascii=False, indent=2, default=str))
        return EXIT_USAGE

    for issue in warnings:
        print_issue(issue)

    # ── 3. 入参校验（与接口共用同一套规则）──────────────
    final_inputs, input_errors, input_warnings = check_inputs(schema, provided)
    for warning in input_warnings:
        print(f"警告：{warning}", file=sys.stderr)
    if input_errors:
        for message in input_errors:
            print(f"错误：{message}", file=sys.stderr)
        print(f"\n入参不完整，未执行任何步骤。该工作流声明了："
              f"{'、'.join(schema.inputs) or '（无）'}", file=sys.stderr)
        return EXIT_USAGE

    # ── 4. 执行 ──────────────────────────────────────
    quiet = args.quiet or args.json
    on_event = None if quiet else _make_progress_printer(schema)

    try:
        run = asyncio.run(run_workflow(
            schema, final_inputs, registry=registry, llm=None, approval=None,
            dry_run=args.dry_run, on_event=on_event))
    except KeyboardInterrupt:                           # pragma: no cover - 交互场景
        print("\n已中断（工作流被取消）", file=sys.stderr)
        return EXIT_CANCELLED
    except WorkflowLoadError as exc:                    # pragma: no cover - 防御性
        print(exc.format(), file=sys.stderr)
        return EXIT_USAGE

    # ── 5. 输出与退出码 ──────────────────────────────
    if args.json:
        print(run.to_json(include_outputs=args.include_outputs))
    elif args.dry_run:
        print_dry_run_plan(schema, run)
    else:
        print_run_report(run)

    return run.exit_code


def _make_progress_printer(schema: WorkflowSchema):
    """进度打印回调：每步开始/结束各一行，走 stderr。

    为什么要有它：一次真实执行可能几分钟（等审批更久）。没有进度输出时，
    用户看到的是"卡住了" —— 而 CLI 最容易被 Ctrl+C 掐断的时机正是在这里。
    """
    total = len(schema.steps)

    def _on_event(event: str, payload: dict[str, Any]) -> None:
        if event == "step_start":
            print(f"[{payload.get('index')}/{total}] 执行 {payload.get('id')}"
                  f"（{payload.get('type')}）…", file=sys.stderr)
        elif event == "step_end" and payload.get("status") not in ("ok",):
            print(f"     {payload.get('id')} → {payload.get('status')}"
                  f"：{payload.get('error') or ''}", file=sys.stderr)

    return _on_event


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口，返回退出码（父 agent 的 `automind workflow` 直接用它）。"""
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    # 兼容 `python -m automind.workflow <file.yaml>`：省略 run 子命令时补上，
    # 让"跑一个文件"这件最常见的事少打一个词
    if argv and not argv[0].startswith("-") and argv[0] not in ("run", "validate"):
        if Path(argv[0]).suffix.lower() in (".yaml", ".yml", ".json") or len(argv) > 1:
            argv = ["run", *argv]

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return EXIT_BAD_WORKFLOW

    # 日志走 stderr 且默认只留 WARNING：stdout 是给 --json 用的
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")

    try:
        if args.command == "validate":
            return cmd_validate(args)
        if args.command == "run":
            return cmd_run(args)
    except KeyboardInterrupt:                           # pragma: no cover - 交互场景
        # Ctrl+C 时给 130，与 `run` 内部的处置保持一致 —— 同一个动作
        # 不该因为"按了两次 Ctrl+C"而变成另一个退出码
        print("\n已中断", file=sys.stderr)
        return EXIT_CANCELLED

    parser.print_help(sys.stderr)                       # pragma: no cover - 防御性
    return EXIT_BAD_WORKFLOW


def sigint_exit_code(exc: BaseException | None = None) -> int:
    """`automind workflow` 被 Ctrl+C 中断时的退出码（130，沿用 shell 惯例）。

    单独导出是为了让父 agent 的接线**不用猜**这个数字：
    `automind/cli/app.py` 的 `main()` 里若有 `except KeyboardInterrupt`，
    直接 `sys.exit(sigint_exit_code())` 即可，与 `python -m automind.workflow`
    的行为完全一致 —— 同一个动作出现两个退出码，CI 里是看不出来的。
    """
    return EXIT_CANCELLED


def run_file(path: str, *, inputs: dict[str, Any] | None = None, dry_run: bool = False) -> WorkflowRun:
    """便捷函数：加载 + 执行，直接返回报告（供脚本/测试调用，不打印不退出）。

    与 CLI 的区别：**不吞异常也不决定退出码**。校验失败抛 `WorkflowLoadError`，
    需要退出码请用 `main()` 或读 `run.exit_code`。
    """
    schema = load_version_checked(Path(path).read_text(encoding="utf-8"), source=str(path))
    return asyncio.run(run_workflow(schema, inputs or {}, dry_run=dry_run))


if __name__ == "__main__":                              # pragma: no cover - 入口
    sys.exit(main())
