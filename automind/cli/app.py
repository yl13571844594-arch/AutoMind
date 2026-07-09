"""CLI 应用主程序。"""

from __future__ import annotations

import argparse
import asyncio
import sys

from automind.core.config import AgentConfig


def create_parser() -> argparse.ArgumentParser:
    """创建 CLI 参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="automind",
        description="AutoMind — 通用自动化 Agent 框架",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  automind "Create a FastAPI project with health check"
  automind --mode react "Fix the bug in main.py"
  automind --config project.yaml "Refactor the database module"
  automind --list-tools
  automind --list-skills
  automind --restore ckpt_session_1234
        """,
    )

    from automind import __version__
    parser.add_argument(
        "--version", action="version", version=f"automind {__version__}",
    )
    parser.add_argument(
        "task", nargs="?", type=str,
        help="Task to execute (natural language)",
    )
    parser.add_argument(
        "--config", "-c", type=str, default=None,
        help="Path to YAML/JSON config file",
    )
    parser.add_argument(
        "--mode", "-m", type=str, choices=["react", "plan_and_execute"],
        default="plan_and_execute", help="Execution mode (default: plan_and_execute)",
    )
    parser.add_argument(
        "--model", type=str, default=None,
        help="LLM model to use",
    )
    parser.add_argument(
        "--provider", type=str, default=None,
        help="LLM provider (openai, anthropic, deepseek, ollama, etc.)",
    )
    parser.add_argument(
        "--project", "-p", type=str, default=".",
        help="Project root directory",
    )
    parser.add_argument(
        "--list-tools", action="store_true",
        help="List available tools and exit",
    )
    parser.add_argument(
        "--list-skills", action="store_true",
        help="List available skills and exit",
    )
    parser.add_argument(
        "--list-providers", action="store_true",
        help="List available LLM providers and exit",
    )
    parser.add_argument(
        "--restore", type=str, default=None,
        help="Restore from a checkpoint and continue",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Verbose output",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Debug mode",
    )

    return parser


async def run_cli(args: argparse.Namespace) -> int:
    """运行 CLI (异步)。"""
    # 加载配置
    if args.config:
        config = AgentConfig.from_yaml(args.config)
    else:
        config = AgentConfig.auto_load(args.project)

    # 命令行覆盖
    if args.provider:
        config.llm.provider = args.provider
    if args.model:
        config.llm.model = args.model
    if args.project:
        config.project_root = args.project
    config.execution.mode = args.mode
    if args.verbose:
        config.log_level = "DEBUG"

    # 列出信息
    if args.list_providers:
        from automind.core.llm import LLMBackendFactory
        print("Available LLM providers:")
        for p in LLMBackendFactory.available_providers():
            print(f"  - {p}")
        return 0

    if args.list_tools:
        from automind.agent import AutoMindAgent
        agent = AutoMindAgent(config)
        print("Available tools:")
        for name in agent.tool_registry.list_names():
            tool = agent.tool_registry.get(name)
            print(f"  - {name}: {tool.description}")
        return 0

    if args.list_skills:
        from automind.skills.skill_registry import SkillRegistry
        registry = SkillRegistry()
        registry.register_builtin_skills()
        print("Available skills:")
        for s in registry.list_all():
            print(f"  - {s['name']}: {s['description']}")
        return 0

    # 恢复检查点 — 载入状态并继续未完成的任务
    if args.restore:
        from automind.agent import AutoMindAgent
        try:
            agent = await AutoMindAgent.from_checkpoint(args.restore, config)
        except Exception as e:
            print(f"恢复失败：{e}")
            return 1
        st = agent._agent_state
        print(f"已从检查点恢复: {args.restore}")
        print(f"  会话 {st.session_id} · 消息 {len(st.messages)} 条 · "
              f"计划 {'有' if st.plan else '无'}")
        try:
            result = await agent.resume_from_checkpoint(args.restore)
            print(f"\n{result.output}")
            return 0 if result.success else 1
        finally:
            await agent.close()

    # 需要任务
    if not args.task:
        print("Error: No task provided. Use 'automind <task>' or see --help")
        return 1

    # 初始化并运行
    from automind import __version__
    print(f"AutoMind v{__version__} | Mode: {args.mode} | Model: {config.llm.provider}/{config.llm.model}")
    print(f"Project: {config.project_root}")
    print()

    from automind.agent import AutoMindAgent
    agent = AutoMindAgent(config)
    try:
        result = await agent.run(args.task)
        print(f"\n{result.output}")
        print(f"\n[{result.steps_executed} steps, {result.errors_corrected} corrected, "
              f"{result.backtracks} backtracks, {result.duration_ms:.0f}ms, "
              f"{result.token_usage.total} tokens]")
        return 0 if result.success else 1
    except Exception as e:
        print(f"Error: {e}")
        if args.debug:
            import traceback
            traceback.print_exc()
        return 1


def main() -> None:
    """CLI 入口点。"""
    parser = create_parser()
    args = parser.parse_args()

    # 没有参数时进入 REPL
    if not args.task and not any([
        args.list_tools, args.list_skills, args.list_providers, args.restore,
    ]):
        print("AutoMind REPL — Entering interactive mode")
        print("Type 'exit' to quit, --help for options")
        asyncio.run(_run_repl(args))
        return

    sys.exit(asyncio.run(run_cli(args)))


async def _run_repl(args: argparse.Namespace) -> None:
    """运行 REPL 模式。"""
    if args.config:
        config = AgentConfig.from_yaml(args.config)
    else:
        config = AgentConfig.auto_load(args.project)

    if args.provider:
        config.llm.provider = args.provider
    if args.model:
        config.llm.model = args.model

    config.execution.mode = args.mode

    from automind.agent import AutoMindAgent
    from automind.cli.tui import run_rich_repl
    agent = AutoMindAgent(config)
    await run_rich_repl(agent)  # §14.3：Rich 增强 REPL（rich 缺失自动降级）


if __name__ == "__main__":
    main()
