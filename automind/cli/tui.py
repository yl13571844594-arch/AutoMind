"""TUI 组件 — 基于 Textual 的终端交互界面。"""

from __future__ import annotations

from typing import Any


class ConsoleUI:
    """控制台 UI — Rich 增强的终端输出。

    提供:
        - 美观的进度展示
        - 彩色日志输出
        - 计划树渲染
        - 步骤执行动画
    """

    def __init__(self) -> None:
        self._console = None

    def _get_console(self) -> Any:
        if self._console is None:
            try:
                from rich.console import Console
                self._console = Console()
            except ImportError:
                self._console = None
        return self._console

    def print_header(self, text: str) -> None:
        """打印页眉。"""
        console = self._get_console()
        if console:
            from rich.panel import Panel
            console.print(Panel(text, style="bold cyan"))
        else:
            print(f"\n{'=' * 60}\n{text}\n{'=' * 60}")

    def print_plan(self, plan: Any) -> None:
        """渲染计划树。"""
        console = self._get_console()
        if not console:
            self._print_plan_text(plan)
            return

        from rich.tree import Tree
        tree = Tree(f"Plan: {plan.task_description}")

        def add_goal(parent_tree: Tree, goal: Any) -> None:
            status_icon = {
                "pending": "○", "in_progress": "◐", "completed": "✓",
                "failed": "✗", "blocked": "⊘",
            }.get(goal.status.value, "?")
            action = f" [{goal.assigned_action.tool_name}]" if goal.assigned_action else ""
            node = parent_tree.add(f"{status_icon} {goal.description}{action}")
            for child in goal.children:
                add_goal(node, child)

        add_goal(tree, plan.root_goal)
        console.print(tree)

    def print_step_result(self, description: str, success: bool, error: str = "") -> None:
        """打印步骤执行结果。"""
        console = self._get_console()
        if console:
            if success:
                console.print(f"  [green]✓[/] {description}")
            else:
                console.print(f"  [red]✗[/] {description}")
                if error:
                    console.print(f"    [red]{error}[/]")
        else:
            status = "OK" if success else "FAIL"
            print(f"  [{status}] {description}")

    def print_progress(self, completed: int, total: int, label: str = "") -> None:
        """打印进度条。"""
        console = self._get_console()
        pct = int(completed / max(1, total) * 100)
        if console:
            from rich.progress import Progress
            with Progress() as progress:
                task = progress.add_task(label or "Progress", total=total)
                progress.update(task, completed=completed)
        else:
            bar = "█" * (pct // 2) + "░" * (50 - pct // 2)
            print(f"\r{label}: [{bar}] {pct}%")

    def print_token_usage(self, used: int, budget: int) -> None:
        """打印 Token 使用情况。"""
        pct = used / max(1, budget) * 100
        console = self._get_console()
        if console:
            color = "green" if pct < 50 else "yellow" if pct < 80 else "red"
            console.print(f"Tokens: [{color}]{used}/{budget} ({pct:.1f}%)[/]")
        else:
            print(f"Tokens: {used}/{budget} ({pct:.1f}%)")

    def _print_plan_text(self, plan: Any, indent: int = 0) -> None:
        """降级纯文本计划渲染。"""
        prefix = "  " * indent
        print(f"{prefix}○ {plan.root_goal.description}")
        for child in plan.root_goal.children:
            self._print_goal_text(child, indent + 1)

    def _print_goal_text(self, goal: Any, indent: int) -> None:
        prefix = "  " * indent
        print(f"{prefix}○ {goal.description}")
        for child in goal.children:
            self._print_goal_text(child, indent + 1)


# ═══════════════════════════════════════════════════════════════
# Rich REPL（§14.3）— 彩色面板 / slash 命令 / Token 摘要
# ═══════════════════════════════════════════════════════════════

_REPL_HELP = """\
[bold]可用命令[/]
  /help              显示本帮助
  /mode <m>          切换执行模式 (react / plan_and_execute)
  /tools             列出可用工具
  /skills            列出可用技能
  /stats             显示本次会话统计
  /clear             清屏
  /exit  /quit  q    退出（显示 Token 摘要）
其余输入将作为任务交给 Agent 执行。"""


async def run_rich_repl(agent: Any) -> None:
    """Rich 增强 REPL（§14.3）。rich 缺失时自动降级到 agent.run_repl()。

    特性：欢迎面板 / 彩色提示符 / slash 命令 / Markdown 结果渲染 /
    每任务统计行 / 退出时 Token 用量摘要。
    """
    try:
        from rich.console import Console
        from rich.markdown import Markdown
        from rich.panel import Panel
        from rich.table import Table
    except ImportError:
        await agent.run_repl()  # 优雅降级：无 rich 时用基础 REPL
        return

    from automind import __version__

    console = Console()
    total_tokens = 0
    total_tasks = 0

    console.print(Panel.fit(
        f"[bold cyan]AutoMind[/] [dim]v{__version__}[/]\n"
        f"模式: [yellow]{agent._mode.value}[/] · "
        f"模型: [green]{agent.config.llm.provider}/{agent.config.llm.model}[/]\n"
        f"项目: [dim]{agent.config.project_root}[/]\n"
        f"输入任务开始，[bold]/help[/] 查看命令",
        border_style="blue", title="REPL",
    ))

    while True:
        try:
            user_input = console.input("[bold cyan]automind>[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user_input:
            continue

        # ── slash 命令 ──
        low = user_input.lower()
        if low in ("/exit", "/quit", "exit", "quit", "q"):
            break
        if low == "/help":
            console.print(Panel(_REPL_HELP, border_style="dim"))
            continue
        if low == "/clear":
            console.clear()
            continue
        if low.startswith("/mode"):
            new_mode = user_input[5:].strip()
            if new_mode in ("react", "plan_and_execute"):
                from automind.core.types import ExecutionMode
                agent._mode = ExecutionMode(new_mode)
                console.print(f"[green][OK][/] 模式已切换: [yellow]{new_mode}[/]")
            else:
                console.print("[red]用法: /mode react | plan_and_execute[/]")
            continue
        if low == "/tools":
            table = Table(title="可用工具", border_style="dim")
            table.add_column("名称", style="cyan")
            table.add_column("等级")
            table.add_column("描述", overflow="fold")
            for name in agent.tool_registry.list_names():
                t = agent.tool_registry.get(name)
                table.add_row(name, t.permission_tier.value, t.description[:80])
            console.print(table)
            continue
        if low == "/skills":
            table = Table(title="可用技能", border_style="dim")
            table.add_column("名称", style="magenta")
            table.add_column("描述", overflow="fold")
            for s in agent.skill_registry.list_all():
                table.add_row(s["name"], (s.get("description") or "")[:80])
            console.print(table)
            continue
        if low == "/stats":
            console.print(Panel(
                f"任务数: [bold]{total_tasks}[/] · 累计 Token: [bold]{total_tokens}[/]",
                title="会话统计", border_style="green"))
            continue

        # ── 执行任务 ──
        try:
            with console.status("[cyan]思考与执行中...[/]", spinner="dots"):
                result = await agent.run(user_input)
            total_tasks += 1
            total_tokens += result.token_usage.total
            style = "green" if result.success else "red"
            console.print(Panel(
                Markdown(result.output or "(无输出)"),
                border_style=style,
                title="完成" if result.success else "失败",
            ))
            console.print(
                f"[dim]{result.steps_executed} 步 · "
                f"{result.errors_corrected} 次纠错 · {result.backtracks} 次回溯 · "
                f"{result.duration_ms:.0f}ms · {result.token_usage.total} tokens[/]")
        except KeyboardInterrupt:
            console.print("[yellow]任务已中断[/]")
        except Exception as e:
            console.print(f"[red]错误: {e}[/]")

    console.print(Panel.fit(
        f"本次会话: [bold]{total_tasks}[/] 个任务 · [bold]{total_tokens}[/] tokens\n再见",
        border_style="cyan"))
    try:
        await agent.close()
    except Exception:
        pass
