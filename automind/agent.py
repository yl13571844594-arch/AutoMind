"""AutoMind Agent — 顶层编排器，绑定所有模块。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from automind.context.context_manager import ContextManager
from automind.context.env_detector import EnvironmentDetector
from automind.context.input_parser import InputParser
from automind.context.project_indexer import ProjectIndexer
from automind.core.config import AgentConfig
from automind.core.events import EventBus
from automind.core.hooks import AgentHooks, invoke_hook
from automind.core.llm import LLMBackendFactory
from automind.core.logging import get_logger
from automind.core.plugin import PluginManager

logger = get_logger("automind.agent")
from automind.core.types import (
    AgentResult,
    AgentState,
    ExecutionMode,
    HierarchicalPlan,
    InputMessage,
    InteractionMode,
    Message,
    PlanStatus,
    Role,
    TokenUsage,
)


class _TokenTrackingLLM:
    """LLM 后端包装器 — 透明累计每次调用的 token 用量。"""

    def __init__(self, backend: Any) -> None:
        self._backend = backend
        self.usage = TokenUsage()

    async def generate(self, messages, tools=None, stop=None):
        resp = await self._backend.generate(messages, tools=tools, stop=stop)
        try:
            self.usage.add(resp)
        except Exception:
            pass
        return resp

    async def generate_stream(self, messages, tools=None):
        import json as _json, re as _re
        async for chunk in self._backend.generate_stream(messages, tools=tools):
            # 最后一块可能包含 STREAM_USAGE 元数据标记
            m = _re.search(r'\n<!--STREAM_USAGE:(.*?)-->', chunk if isinstance(chunk, str) else '')
            if m:
                try:
                    usage = _json.loads(m.group(1))
                    self.usage.prompt_tokens += usage.get("prompt_tokens", 0)
                    self.usage.completion_tokens += usage.get("completion_tokens", 0)
                except Exception:
                    pass
                # 移除标记再输出
                yield _re.sub(r'\n<!--STREAM_USAGE:.*?-->', '', chunk if isinstance(chunk, str) else '')
            else:
                yield chunk

    def reset(self) -> None:
        self.usage = TokenUsage()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._backend, name)
from automind.memory.manager import MemoryManager
from automind.planning.hierarchical_planner import HierarchicalPlanner
from automind.planning.plan_executor import PlanExecutor
from automind.planning.react_executor import ReActExecutor
from automind.reflection.consistency_checker import ConsistencyChecker
from automind.reflection.quality_assessor import QualityAssessor
from automind.reflection.reflexion import ReflexionEngine
from automind.skills.skill_registry import SkillRegistry
from automind.state.checkpoint import CheckpointManager
from automind.state.human_loop import (
    ApprovalAction,
    ApprovalRequest,
    ApprovalResponse,
    HumanInTheLoop,
    ProgressDisplay,
)
from automind.state.resource_manager import ResourceManager
from automind.tools.base import ToolRegistry
from automind.tools.file_editor import FileEditTool, FileReadTool, FileWriteTool
from automind.tools.function_calling import FunctionCallHandler
from automind.tools.mcp_registry import MCPRegistry
from automind.tools.permissions import PermissionEngine
from automind.tools.sandbox import PythonSandboxTool
from automind.tools.terminal import TerminalTool


class AutoMindAgent:
    """AutoMind 通用自动化 Agent。

    将所有模块绑定为统一接口，支持:
        - ReAct 模式 (思考-行动循环)
        - Plan-and-Execute 模式 (分层规划 + 符号验证)
        - Multi-Agent 模式 (预留)

    使用示例::

        config = AgentConfig.auto_load()
        agent = AutoMindAgent(config)
        result = await agent.run("Create a FastAPI project with health check")
    """

    def __init__(self, config: AgentConfig | None = None) -> None:
        self.config = config or AgentConfig.auto_load()

        # ── 核心基础设施 ──────────────────────────
        self.event_bus = EventBus()
        # 执行过程事件回调（由 Web 层注入，用于实时展示执行过程）
        self.event_sink = None
        self.llm = self._init_llm()
        self.tool_registry = ToolRegistry()
        self.permissions = PermissionEngine(
            policy=self.config.permissions,
            project_root=self.config.project_root,
            approval_mode=getattr(self.config.execution, "approval_mode", "auto"),
        )
        # 审批回调（由 Web 层注入，用于 ask 模式的人工确认）
        self.approval_callback = None
        self.resources = ResourceManager(
            token_budget=self.config.llm.max_tokens * 10,
        )

        # ── 上下文模块 ───────────────────────────
        self.env = EnvironmentDetector.detect(self.config.project_root)
        self.project_indexer = ProjectIndexer(
            project_root=self.config.project_root,
            cache_file=str(Path(self.config.project_root) / ".automind" / "project_index.json"),
        )
        self.input_parser = InputParser()
        self.context_mgr = ContextManager(
            max_tokens=self.config.memory.short_term_max_tokens,
            summary_threshold=self.config.memory.short_term_summary_threshold,
        )

        # ── 记忆 ──────────────────────────────────
        self.memory = MemoryManager(
            max_tokens=self.config.memory.short_term_max_tokens,
            persist_dir=self.config.memory.chroma_persist_dir,
            project_root=self.config.project_root,
        )

        # ── 工具注册 ─────────────────────────────
        self._register_default_tools()

        # ── 技能 ──────────────────────────────────
        self.skill_registry = SkillRegistry()
        self.skill_registry.register_builtin_skills()

        # ── 规划与推理 ────────────────────────────
        self.hierarchical_planner = HierarchicalPlanner(self.llm)
        self.react_executor: ReActExecutor | None = None
        self.plan_executor = PlanExecutor(
            self.llm, self.tool_registry, self.permissions,
            max_retries=self.config.execution.max_retries,
            parallel=self.config.execution.parallel_execution,
            use_cache=self.config.execution.subtask_cache,
        )
        self.fn_handler = FunctionCallHandler(self.tool_registry)

        # ── 反思 ──────────────────────────────────
        self.quality_assessor = QualityAssessor(self.llm)
        self.consistency_checker = ConsistencyChecker()
        self.reflexion = ReflexionEngine(self.llm, self.memory.long_term)

        # ── MCP ───────────────────────────────────
        self.mcp_registry = MCPRegistry()

        # ── 状态管理 ──────────────────────────────
        self.checkpoint_mgr = CheckpointManager(self.config.execution.checkpoint_dir)
        self.human_loop = HumanInTheLoop(auto_approve_safe=self.config.execution.auto_approve_safe)

        # ── 多智能体协同 ─────────────────────────
        from automind.multiagent.orchestrator import MultiAgentOrchestrator
        self.orchestrator = MultiAgentOrchestrator(self.llm) if self.llm else None

        # ── 当前会话状态 ─────────────────────────
        self._current_plan: HierarchicalPlan | None = None
        self._agent_state = AgentState()
        self._mode: ExecutionMode = ExecutionMode(self.config.execution.mode)
        # 上层交互模式（对话/工作/编程），默认对话
        self._interaction: InteractionMode = InteractionMode.CHAT
        # 对话模式的多轮历史
        self._chat_history: list[dict[str, str]] = []

        # ── 生命周期钩子 + 插件系统（§3.5 / §14.7）──
        self.hooks = AgentHooks()
        self.plugin_manager = PluginManager()

    # 各交互模式的系统提示词（精炼、可执行，提升命中率并节省 token）
    CHAT_SYSTEM_PROMPT = (
        "你是 AutoMind，一个友好、博学的中文 AI 助手。"
        "直接回答用户的问题，简明扼要、重点突出，必要时用 Markdown（标题/列表/代码块/表格）。"
        "不确定时坦诚说明，不编造事实。这是纯对话模式，不调用任何工具。"
        "若用户提供了图片，请结合图片内容作答。"
    )
    CODING_SYSTEM_PROMPT = (
        "你是 AutoMind 编程助手，擅长阅读、编写、调试和重构代码。\n"
        "高效工作准则（务必遵守，以减少无效步骤、节省 token）：\n"
        "1. 动手前先用 file_read 确认相关文件的真实内容，不要臆测。\n"
        "2. 一次只做一件明确的事；工具参数必须完整、准确（用确切的工具名与文件路径）。\n"
        "3. 改动最小化、风格与现有代码一致；不要重写无关部分。\n"
        "4. 执行终端命令前评估安全性，危险命令需说明理由。\n"
        "5. 任务完成即停止并简要总结你做了什么、改了哪些文件。\n"
        "6. 若生成 HTML/前端页面，请将完整代码放入 ```html 代码块，便于用户预览。\n"
        "7. 需要从零生成/补全整段代码时优先用 code_generate 工具"
        "（自带语法校验与自动修复；mode='complete' 可补全既有代码）。\n"
        "8. 每次写入/编辑 .py 文件后，观察结果中会附带 syntax_check 自动验证；"
        "若 FAILED 必须立即修复该语法错误再继续（TDD 内环）。"
    )

    # ═══════════════════════════════════════════════════════════
    # 主入口
    # ═══════════════════════════════════════════════════════════

    async def run(self, user_input: str) -> AgentResult:
        """执行用户指令（对外入口，包裹生命周期钩子）。

        在核心流程外围触发 before_run / after_run / on_error 钩子，
        供插件系统（§14.7）介入；钩子异常不影响主流程。
        """
        await self._invoke_hook("before_run", user_input)
        try:
            result = await self._run_impl(user_input)
        except Exception as e:
            await self._invoke_hook("on_error", e, user_input)
            raise
        await self._invoke_hook("after_run", result)
        return result

    async def _invoke_hook(self, name: str, *args: Any) -> None:
        """安全触发单个生命周期钩子（不存在或报错均忽略）。"""
        await invoke_hook(getattr(self.hooks, name, None), *args)

    def apply_plugin_hooks(self) -> None:
        """将当前已加载插件的 hooks 汇总应用到本 Agent。"""
        self.hooks = self.plugin_manager.assemble_hooks()

    async def _run_impl(self, user_input: str) -> AgentResult:
        """执行用户指令。

        完整流程:
            1. 解析输入
            2. 收集上下文
            3. 生成计划
            4. 执行计划
            5. 验证与反思
            6. 返回结果
        """
        start_time = time.perf_counter()
        backtracks = 0
        errors_corrected = 0

        if self.llm is None:
            raise RuntimeError(
                "LLM 未初始化。请先在「API Keys」面板配置当前提供商的 API Key。"
            )
        self.llm.reset()  # 重置本次任务的 token 计数

        # 1. 解析输入
        parsed = self.input_parser.parse(user_input)
        await self._invoke_hook("after_parse", parsed)
        self.context_mgr.add(Message(role=Role.USER, content=user_input))

        # 2. 收集上下文
        context = self._build_context(parsed)
        relevant_memories = await self.memory.retrieve_relevant(user_input, k=5)
        if relevant_memories:
            context += "\n\n[Relevant Memories]\n" + "\n".join(
                f"- [{m.source}] {m.content[:200]}" for m in relevant_memories
            )

        # 3. 选择模式并执行
        step_results = []
        if self._mode == ExecutionMode.REACT:
            result_text = await self._run_react(user_input, context)
            plan = None
        else:
            plan, step_results = await self._run_plan_execute(user_input, context)
            await self._invoke_hook("after_plan", plan)
            result_text = self._build_result_text(plan, step_results)
            backtracks = sum(1 for s in step_results if s.retries > 0) if step_results else 0
            errors_corrected = sum(1 for s in step_results if s.retries > 0 and s.success) if step_results else 0

        # 3.5 自主任务闭环：TDD 测试 + 多 Agent 审查 + Loop 验收（工作/编程模式）
        if self._interaction in (InteractionMode.WORK, InteractionMode.CODING):
            result_text = await self._autonomy_closure(user_input, result_text, context)

        # 4. 质量评估
        quality = await self.quality_assessor.evaluate(user_input, result_text, context)

        # 5. 反思
        reflection = await self.reflexion.reflect(
            user_input,
            "success" if quality.overall_pass else "partial",
            result_text[:2000],
            quality,
        )

        # 6. 存储交互
        assistant_msg = Message(role=Role.ASSISTANT, content=result_text)
        self.context_mgr.add(assistant_msg)
        await self.memory.store_interaction(
            Message(role=Role.USER, content=user_input),
            assistant_msg,
        )

        # 7. 保存检查点
        checkpoint_id = ""
        if self.config.execution.checkpoint_enabled:
            self._agent_state.plan = self._current_plan
            self._agent_state.messages = self.context_mgr.get_context()
            checkpoint_id = await self.checkpoint_mgr.save(self._agent_state)

        duration = (time.perf_counter() - start_time) * 1000

        # success 判定：计划模式以"是否真正执行完成"为准（质量分仅作辅助信号），
        # 避免任务已完成但因 LLM 评分偏低而误报失败。
        if plan is not None and step_results:
            plan_done = (
                getattr(plan, "status", None)
                and plan.status.value == "completed"
                and not any(not s.success for s in step_results)
            )
            success = bool(plan_done or quality.overall_pass)
        elif self._mode == ExecutionMode.REACT:
            # ReAct/编程模式：只要产出了实质答案（非迭代上限兜底）即视为成功
            produced = bool(result_text and "最大迭代步数" not in result_text)
            success = bool(produced or quality.overall_pass)
        else:
            success = quality.overall_pass

        return AgentResult(
            success=success,
            output=result_text,
            plan=self._current_plan,
            steps_executed=len(step_results) if step_results else 0,
            errors_corrected=errors_corrected,
            backtracks=backtracks,
            token_usage=self.llm.usage,
            duration_ms=duration,
            checkpoints=[checkpoint_id] if checkpoint_id else [],
        )

    async def run_repl(self) -> None:
        """交互式 REPL 循环。"""
        print("AutoMind REPL — Type 'exit' to quit, 'mode' to switch mode")
        print(f"Mode: {self._mode.value.upper()} | Model: {self.config.llm.model}")
        print(f"Project: {self.config.project_root}")

        while True:
            try:
                user_input = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye.")
                break

            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit", "q"):
                print("Goodbye.")
                break
            if user_input.lower().startswith("mode "):
                new_mode = user_input[5:].strip()
                if new_mode in ("react", "plan_and_execute", "multi_agent"):
                    self._mode = ExecutionMode(new_mode)
                    print(f"Mode switched to: {new_mode}")
                continue

            result = await self.run(user_input)
            print(f"\n{result.output}")
            print(f"[{result.steps_executed} steps, {result.errors_corrected} corrected, "
                  f"{result.backtracks} backtracks, {result.duration_ms:.0f}ms]")

    # ═══════════════════════════════════════════════════════════
    # 内部执行方法
    # ═══════════════════════════════════════════════════════════

    async def chat(self, user_input: str, images: list[str] | None = None,
                   history: list[dict] | None = None) -> str:
        """对话模式 — 纯多轮对话，不调用工具、不规划。

        Args:
            user_input: 用户文本。
            images: 可选的图片 data URL 列表（多模态，发送给视觉模型）。
            history: 可选的会话历史列表（多用户隔离时由调用方传入；
                     不传则使用 Agent 内置的共享历史，保持单用户兼容）。
        """
        if self.llm is None:
            raise RuntimeError(
                "LLM 未初始化。请先在「API Keys」面板配置当前提供商的 API Key。"
            )
        self.llm.reset()
        hist = history if history is not None else self._chat_history

        # 多模态：含图片时，构造 OpenAI 视觉消息格式
        if images:
            content: Any = [{"type": "text", "text": user_input}]
            for url in images:
                content.append({"type": "image_url", "image_url": {"url": url}})
            hist.append({"role": "user", "content": content})
        else:
            hist.append({"role": "user", "content": user_input})

        messages = [{"role": "system", "content": self.CHAT_SYSTEM_PROMPT}, *hist[-20:]]
        response = await self.llm.generate(messages)
        reply = response.text or "(无回复)"
        hist.append({"role": "assistant", "content": reply})
        return reply

    async def chat_stream(self, user_input: str, images: list[str] | None = None,
                          history: list[dict] | None = None):
        """对话模式（流式）— 逐字产出，结束后写入历史并估算 token。"""
        if self.llm is None:
            raise RuntimeError(
                "LLM 未初始化。请先在「API Keys」面板配置当前提供商的 API Key。"
            )
        self.llm.reset()
        hist = history if history is not None else self._chat_history

        if images:
            content: Any = [{"type": "text", "text": user_input}]
            for url in images:
                content.append({"type": "image_url", "image_url": {"url": url}})
            hist.append({"role": "user", "content": content})
        else:
            hist.append({"role": "user", "content": user_input})

        messages = [{"role": "system", "content": self.CHAT_SYSTEM_PROMPT}, *hist[-20:]]

        chunks: list[str] = []
        async for delta in self.llm.generate_stream(messages):
            chunks.append(delta)
            yield delta

        reply = "".join(chunks) or "(无回复)"
        hist.append({"role": "assistant", "content": reply})

        # 流式接口通常不返回用量，这里做估算
        try:
            prompt_text = "".join(
                str(m.get("content", "")) for m in messages
                if isinstance(m.get("content"), str)
            )
            self._last_stream_usage = TokenUsage(
                prompt_tokens=self.llm.token_count(prompt_text),
                completion_tokens=self.llm.token_count(reply),
            )
        except Exception:
            self._last_stream_usage = TokenUsage()

    def reset_chat(self) -> None:
        """清空对话历史。"""
        self._chat_history.clear()

    async def run_multi(self, task: str, on_event: Any = None) -> dict:
        """多智能体协同执行。"""
        if self.llm is None:
            raise RuntimeError("LLM 未初始化，请先配置 API Key。")
        self.llm.reset()
        if self.orchestrator is None:
            from automind.multiagent.orchestrator import MultiAgentOrchestrator
            self.orchestrator = MultiAgentOrchestrator(self.llm)
        result = await self.orchestrator.run(task, context="", on_event=on_event)
        result["token_usage"] = self.llm.usage
        return result

    async def run_loop(self, task: str, on_event: Any = None,
                       max_iterations: int | None = None) -> dict:
        """循环工程（Loop Engineering）— 自主"行动-观察-修正"闭环。

        每轮：执行任务 → 观察/校验结果 → 若未达成则带着反馈继续修正，
        直到满足停止条件：任务完成 / 达到最大轮数 / 连续无进展 / 被中断。
        """
        import difflib

        if self.llm is None:
            raise RuntimeError("LLM 未初始化，请先配置 API Key。")
        self.llm.reset()
        max_it = max_iterations or getattr(self.config.execution, "loop_max_iterations", 8)
        feedback = ""
        last_output = ""
        output = ""
        it = 0
        idle_rounds = 0  # 连续"未执行任何工具操作"的轮数
        for it in range(1, max_it + 1):
            if on_event:
                await on_event({"type": "loop_iter_start", "iter": it, "max": max_it})

            # ── 行动 ──
            prompt = task if not feedback else (
                f"{task}\n\n[上一轮观察到的问题，请据此修正]\n{feedback}")
            self.react_executor = ReActExecutor(
                self.llm, self.tool_registry,
                max_iterations=self.config.execution.max_iterations,
                permissions=self.permissions,
                approval_cb=self.approval_callback,
            )
            on_thought, on_action = self._react_callbacks(tag=it)
            output = await self.react_executor.run(
                prompt, self.CODING_SYSTEM_PROMPT,
                on_thought=on_thought, on_action=on_action)
            acted = bool(self.react_executor.actions)
            if on_event:
                await on_event({"type": "loop_action", "iter": it, "output": output})

            # ── 观察 / 校验（三级停止条件）──
            verdict = await self._loop_verify(task, output)
            if on_event:
                await on_event({"type": "loop_observation", "iter": it,
                                "done": verdict["done"], "reason": verdict["reason"]})
            # 1) 语义判断：任务完成
            if verdict["done"]:
                if on_event:
                    await on_event({"type": "loop_done", "iter": it, "success": True})
                return {"output": output, "iterations": it, "success": True,
                        "stop_reason": "completed", "token_usage": self.llm.usage}

            stop_reason = None
            # 2) 无进展：与上一轮输出完全相同 或 高度相似（收敛）
            if output.strip() and output.strip() == last_output.strip():
                stop_reason = "no_progress"
            elif last_output:
                sim = difflib.SequenceMatcher(None, output, last_output).ratio()
                if sim > 0.95:
                    stop_reason = "converged"
            # 3) 空转：连续两轮均未执行任何工具操作（只是在"说"而非"做"）
            if stop_reason is None:
                idle_rounds = idle_rounds + 1 if not acted else 0
                if idle_rounds >= 2:
                    stop_reason = "idle"

            if stop_reason:
                if on_event:
                    await on_event({"type": "loop_done", "iter": it, "success": False,
                                    "stop_reason": stop_reason})
                return {"output": output, "iterations": it, "success": False,
                        "stop_reason": stop_reason, "token_usage": self.llm.usage}

            last_output = output
            feedback = verdict["reason"]

        if on_event:
            await on_event({"type": "loop_done", "iter": it, "success": False,
                            "stop_reason": "max_iterations"})
        return {"output": output, "iterations": it, "success": False,
                "stop_reason": "max_iterations", "token_usage": self.llm.usage}

    async def _loop_verify(self, task: str, output: str) -> dict:
        """观察阶段 — 让模型判断任务是否真正完成，并给出修正方向。"""
        from automind.core.json_utils import extract_json
        prompt = (
            f"你是严格的验收员。判断下面的任务是否已真正完成且正确。\n\n"
            f"任务：{task}\n\n执行结果：\n{output[:2500]}\n\n"
            f'只输出 JSON：{{"done": true 或 false, '
            f'"reason": "若未完成，明确说明还差什么、下一步如何修正"}}'
        )
        try:
            resp = await self.llm.generate([{"role": "user", "content": prompt}])
            data = extract_json(resp.text)
            if isinstance(data, dict):
                return {"done": bool(data.get("done")),
                        "reason": str(data.get("reason", ""))[:600]}
        except Exception:
            pass
        return {"done": False, "reason": "无法判定，继续尝试。"}

    async def _emit(self, event: dict) -> None:
        """向执行过程事件回调推送一条事件（无回调时静默）。"""
        if self.event_sink is not None:
            try:
                await self.event_sink(event)
            except Exception:
                pass

    def _react_callbacks(self, tag: int | None = None):
        """构造 ReAct 的思考/行动回调，转发到 event_sink。"""
        step = {"n": 0}

        async def on_thought(text: str) -> None:
            step["n"] += 1
            await self._emit({"type": "step_thought", "iter": tag,
                              "step": step["n"], "text": (text or "")[:1200]})

        async def on_action(tc, result) -> None:
            out = result.output if result.success else result.error
            await self._emit({"type": "step_action", "iter": tag,
                              "tool": tc.name,
                              "args": {k: str(v)[:200] for k, v in (tc.arguments or {}).items()},
                              "success": result.success,
                              "output": str(out)[:600]})

        return on_thought, on_action

    async def _run_react(self, task: str, context: str) -> str:
        """ReAct 模式执行。"""
        # 每次重建以注入最新的权限/审批回调
        self.react_executor = ReActExecutor(
            self.llm, self.tool_registry,
            max_iterations=self.config.execution.max_iterations,
            permissions=self.permissions,
            approval_cb=self.approval_callback,
            auto_validate=self.config.execution.auto_test,  # TDD 内环开关
        )
        # 编程模式下注入面向编程的引导
        if self._interaction == InteractionMode.CODING:
            context = f"{self.CODING_SYSTEM_PROMPT}\n\n{context}"
        on_thought, on_action = self._react_callbacks()
        return await self.react_executor.run(
            task, context, on_thought=on_thought, on_action=on_action)

    async def _run_plan_execute(self, task: str, context: str) -> tuple[HierarchicalPlan, list[Any]]:
        """Plan-and-Execute 模式执行。"""
        # 传入带参数签名的工具说明，便于规划器生成正确的 tool_params
        tools = []
        for t in self.tool_registry.list_all():
            params = list(t.parameters.get("properties", {}).keys())
            required = t.parameters.get("required", [])
            sig = ", ".join(
                (f"{p}*" if p in required else p) for p in params
            )
            desc = (t.description or "").strip().split("\n")[0][:80]
            tools.append(f"{t.name}({sig}) — {desc}")

        # 生成计划
        plan = await self.hierarchical_planner.plan(task, context, tools)
        self._current_plan = plan

        # 推送计划已生成事件（含叶子步骤），供前端实时展示
        leaves = plan.root_goal.leaf_goals()
        await self._emit({
            "type": "plan_created",
            "task": plan.task_description,
            "steps": [
                {"goal_id": g.id, "description": g.description,
                 "tool": g.assigned_action.tool_name if g.assigned_action else None}
                for g in leaves
            ],
        })

        # 记录计划树（库层走 logger，Web 层已有 plan_created 事件流）
        if self._mode == ExecutionMode.PLAN_AND_EXECUTE:
            logger.info("plan_created", plan="\n" + self._format_plan(plan))

        # 执行计划
        report = await self.plan_executor.execute(
            plan,
            on_step_start=self._on_step_start,
            on_step_end=self._on_step_end,
            on_backtrack=self._on_backtrack,
            on_approval_needed=self._on_approval_needed,
        )

        return plan, report.steps

    # ═══════════════════════════════════════════════════════════
    # 回调
    # ═══════════════════════════════════════════════════════════

    async def _on_step_start(self, goal: Any) -> None:
        await self.event_bus.emit(
            type("EventType", (), {"value": "goal.start"})(),
            {"goal_id": goal.id, "description": goal.description},
        )
        tool = goal.assigned_action.tool_name if goal.assigned_action else None
        await self._emit({"type": "plan_step_start", "goal_id": goal.id,
                          "description": goal.description, "tool": tool})

    async def _on_step_end(self, step_result: Any) -> None:
        if step_result.success:
            logger.info("step_end", goal=step_result.goal_description, status="ok")
        else:
            logger.warning("step_end", goal=step_result.goal_description,
                           status="fail", error=step_result.error or "")
        await self._emit({"type": "plan_step_end",
                          "goal_id": step_result.goal_id,
                          "description": step_result.goal_description,
                          "success": step_result.success,
                          "error": step_result.error or ""})

    async def _on_backtrack(self, goal_id: str, reason: str) -> None:
        logger.warning("backtrack", goal_id=goal_id, reason=str(reason)[:300])
        await self._emit({"type": "plan_backtrack", "goal_id": goal_id,
                          "reason": str(reason)[:300]})

    async def _on_approval_needed(self, goal: Any, action: Any) -> bool:
        tool_name = getattr(action, "tool_name", "unknown")
        params = getattr(action, "parameters", {}) or {}
        # 优先走 Web 注入的审批回调
        if self.approval_callback is not None:
            try:
                return bool(await self.approval_callback(
                    tool_name, params, "sensitive",
                    f"步骤需要批准：{getattr(goal, 'description', '')}"))
            except Exception:
                return True
        request = ApprovalRequest(
            goal=goal, action=action, risk_level=tool_name,
            reason="Manual approval required",
        )
        response = await self.human_loop.request_approval(request)
        return response.action == ApprovalAction.APPROVE

    # ═══════════════════════════════════════════════════════════
    # 辅助方法
    # ═══════════════════════════════════════════════════════════

    def _build_context(self, parsed: InputMessage) -> str:
        """构建执行上下文。"""
        parts = [self.env.to_prompt_context()]

        # 项目索引
        try:
            index = self.project_indexer.build_index()
            parts.append(index.to_summary())
        except Exception:
            pass

        return "\n\n".join(parts)

    def _build_result_text(self, plan: HierarchicalPlan, steps: list[Any]) -> str:
        """构建最终输出文本。"""
        progress = self.hierarchical_planner.get_progress(plan)
        completed = [s for s in steps if s.success]
        failed = [s for s in steps if not s.success]

        lines = [
            f"Task: {plan.task_description}",
            f"Status: {plan.status.value}",
            f"Progress: {progress['completed']}/{progress['total']} ({progress['percent']}%)",
            "",
        ]

        if completed:
            lines.append("Completed steps:")
            for s in completed:
                lines.append(f"  ✓ {s.goal_description}")
        if failed:
            lines.append("Failed steps:")
            for s in failed:
                lines.append(f"  ✗ {s.goal_description}: {s.error}")

        return "\n".join(lines)

    def _format_plan(self, plan: HierarchicalPlan) -> str:
        """格式化计划为可显示文本。"""
        lines = ["\n" + "=" * 60, f"PLAN: {plan.task_description}", "=" * 60]

        def _print_goal(goal: Any, indent: int) -> None:
            status_icon = {
                "pending": "○", "in_progress": "◐", "completed": "✓",
                "failed": "✗", "blocked": "⊘", "reverted": "↺",
            }.get(goal.status.value, "?")
            prefix = "  " * indent
            action_str = ""
            if goal.assigned_action:
                action_str = f" → [{goal.assigned_action.tool_name}]"
            lines.append(f"{prefix}{status_icon} {goal.description}{action_str}")
            for child in goal.children:
                _print_goal(child, indent + 1)

        _print_goal(plan.root_goal, 0)
        lines.append("=" * 60)
        return "\n".join(lines)

    def _init_llm(self) -> Any:
        """初始化 LLM 后端（包装 token 统计）。"""
        try:
            backend = LLMBackendFactory.create(self.config.llm.provider, self.config.llm)
            return _TokenTrackingLLM(backend)
        except Exception as e:
            logger.warning("llm_init_failed",
                           provider=self.config.llm.provider,
                           model=self.config.llm.model,
                           api_base=self.config.llm.api_base or "(default)",
                           error=str(e))
            self._llm_init_error = str(e)
            return None

    def _register_default_tools(self) -> None:
        """注册默认工具。"""
        self.tool_registry.register(TerminalTool(workdir=self.config.project_root))
        # 文件工具开启路径穿越防护：所有读写限定在 project_root 之内
        _root = self.config.project_root
        self.tool_registry.register(FileReadTool(project_root=_root))
        self.tool_registry.register(FileWriteTool(project_root=_root))
        self.tool_registry.register(FileEditTool(project_root=_root))
        self.tool_registry.register(PythonSandboxTool())
        # 浏览器 / 网页能力
        try:
            from automind.tools.browser import BrowserTool, WebFetchTool
            self.tool_registry.register(WebFetchTool())
            self.tool_registry.register(BrowserTool())
        except Exception:
            pass
        # 编程能力增强：把 code_generator 技能（生成/补全/脚手架 + 语法校验 + 自动修复）
        # 以工具形式暴露给 ReAct 循环，编程模式可直接调用
        self.tool_registry.register(_CodeGenerateTool(self))

    async def close(self) -> None:
        """释放全部持有资源 — MCP 连接 / 记忆系统（ChromaDB）/ LLM 连接池。

        幂等：重复调用安全；单项失败不阻断其余清理。
        """
        # 1. 断开所有 MCP 服务器连接
        try:
            await self.mcp_registry.disconnect_all()
        except Exception as e:
            logger.warning("close_mcp_failed", error=str(e))
        # 2. 释放记忆系统（ChromaDB 客户端 + 短期窗口）
        try:
            self.memory.close()
        except Exception as e:
            logger.warning("close_memory_failed", error=str(e))
        # 3. 关闭 LLM 后端网络资源（经 _TokenTrackingLLM 委托）
        try:
            if self.llm is not None:
                await self.llm.close()
        except Exception as e:
            logger.warning("close_llm_failed", error=str(e))
        logger.info("agent_closed")

    # ═══════════════════════════════════════════════════════════
    # 检查点恢复（CLI --restore）
    # ═══════════════════════════════════════════════════════════

    @classmethod
    async def from_checkpoint(
        cls, checkpoint_id: str, config: AgentConfig | None = None
    ) -> "AutoMindAgent":
        """从检查点恢复一个 Agent 实例（上下文消息 / 计划 / 对话历史）。"""
        agent = cls(config or AgentConfig.auto_load())
        state = await agent.checkpoint_mgr.load(checkpoint_id)
        agent._agent_state = state
        agent._current_plan = state.plan
        for msg in state.messages:
            agent.context_mgr.add(msg)
        agent._chat_history = [
            {"role": m.role.value, "content": m.content}
            for m in state.messages
            if m.role.value in ("user", "assistant")
        ]
        logger.info("checkpoint_restored", checkpoint=checkpoint_id,
                    messages=len(state.messages), has_plan=state.plan is not None)
        return agent

    async def resume_from_checkpoint(self, checkpoint_id: str) -> AgentResult:
        """从检查点继续执行未完成的计划；无进行中计划时仅确认已恢复上下文。"""
        state = await self.checkpoint_mgr.load(checkpoint_id)
        plan = state.plan
        if plan is None:
            return AgentResult(
                success=True,
                output="检查点已恢复（上下文与对话历史）；其中无进行中的计划，无需继续执行。",
            )
        status = getattr(plan, "status", None)
        if status is not None and status.value in ("completed", "aborted"):
            return AgentResult(
                success=True,
                output=f"检查点已恢复；计划状态为「{status.value}」，无需继续。",
            )
        task = getattr(plan, "task_description", "") or "继续未完成的任务"
        return await self.run(f"继续执行此前未完成的任务：{task}")

    # ═══════════════════════════════════════════════════════════
    # 自主任务闭环 — 多 Agent 审查 + Loop 验证 + TDD 测试
    # ═══════════════════════════════════════════════════════════

    async def _autonomy_closure(self, task: str, output: str, context: str) -> str:
        """自主任务闭环：TDD 测试 → 多 Agent 审查 → Loop 验收（未过带反馈自动修复）。

        仅作用于 工作/编程 模式；各环节由 ExecutionConfig 开关控制（默认全开）。
        返回可能被补充轮更新过的最终输出（末尾附闭环摘要）。
        """
        ex = self.config.execution
        summary: list[str] = []
        issues: list[str] = []

        # ① TDD：编程模式跑项目级测试（若存在 tests/）
        if ex.auto_test and self._interaction == InteractionMode.CODING:
            t = await self._run_project_tests()
            if t is not None:
                summary.append("测试" + ("通过 ✓" if t["passed"] else "未通过 ✗"))
                if not t["passed"]:
                    issues.append(f"项目测试未通过：{t['detail'][:600]}")
                await self._emit({"type": "autopilot", "stage": "tdd",
                                  "passed": t["passed"], "detail": t["detail"][:300]})

        # ② 多 Agent 审查：工作模式由审阅者角色复核（共享只读工具，含 MCP）
        if ex.auto_review and self._interaction == InteractionMode.WORK \
                and self.llm is not None:
            rv = await self._review_result(task, output)
            summary.append("审查" + ("通过 ✓" if rv["approved"] else "有意见 ⚠"))
            if not rv["approved"] and rv["issues"]:
                issues.append("审阅者意见：" + rv["issues"][:600])
            await self._emit({"type": "autopilot", "stage": "review",
                              "approved": rv["approved"], "issues": rv["issues"][:300]})

        # ③ Loop 验收：语义判定是否真正完成；未过则带反馈补充修复轮
        if ex.auto_verify and self.llm is not None:
            rounds = 0
            while True:
                verdict = await self._loop_verify(task, output)
                done = bool(verdict.get("done")) and not issues
                await self._emit({"type": "autopilot", "stage": "verify", "done": done,
                                  "round": rounds, "reason": str(verdict.get("reason", ""))[:300]})
                if done:
                    summary.append("验收通过 ✓")
                    break
                if rounds >= ex.auto_verify_max_rounds:
                    summary.append(f"验收未过（已修复 {rounds} 轮）✗")
                    break
                rounds += 1
                feedback = "；".join(
                    [str(verdict.get("reason", ""))] + issues)[:1000]
                issues = []  # 意见已并入反馈
                await self._emit({"type": "autopilot", "stage": "fix_round",
                                  "round": rounds, "feedback": feedback[:300]})
                logger.info("autopilot_fix_round", round=rounds)
                output = await self._run_react(
                    f"{task}\n\n[自主闭环 · 修复第 {rounds} 轮] "
                    f"上一轮结果未通过验收，请针对以下反馈修复并完成任务：\n{feedback}",
                    context)

        if summary:
            output = f"{output}\n\n---\n🔄 自主闭环：{' · '.join(summary)}"
        return output

    async def _review_result(self, task: str, output: str) -> dict:
        """多 Agent 审查：审阅者角色复核结果，可调用只读工具核实（MCP 工具共享）。"""
        from automind.core.json_utils import extract_json
        from automind.multiagent.orchestrator import ROLE_PROMPTS

        # 共享只读（SAFE 级）工具给审阅者 —— 同一 registry，MCP 注册的只读工具同样可用
        read_only = [t for t in self.tool_registry.list_all()
                     if t.permission_tier.value == "safe"]
        tool_schemas = [t.to_openai_schema() for t in read_only] or None

        messages = [
            {"role": "system", "content": ROLE_PROMPTS["reviewer"] +
             ' 最终必须输出 JSON：{"approved": true 或 false, "issues": "问题清单，无则空串"}'},
            {"role": "user", "content":
             f"任务：{task}\n\n执行结果：\n{output[:3000]}\n\n"
             f"请复核结果的正确性与完整性。可调用只读工具核实文件真实状态。"},
        ]
        try:
            resp = await self.llm.generate(messages, tools=tool_schemas)
            # 允许审阅者做一轮只读核实
            if getattr(resp, "tool_calls", None):
                results = []
                for tc in resp.tool_calls[:4]:
                    try:
                        args = tc.arguments if isinstance(tc.arguments, dict) else {}
                        results.append(await self.tool_registry.dispatch(tc.name, **args))
                    except Exception as e:
                        from automind.core.types import ToolResult
                        results.append(ToolResult(tool_name=tc.name, success=False, error=str(e)))
                messages.append(
                    {"role": "assistant", "content": resp.text or "(核实中)"})
                for tc, r in zip(resp.tool_calls[:4], results):
                    out = r.output if r.success else r.error
                    messages.append({"role": "user",
                                     "content": f"[工具 {tc.name} 结果] {str(out)[:800]}"})
                resp = await self.llm.generate(messages)
            data = extract_json(resp.text)
            if isinstance(data, dict):
                return {"approved": bool(data.get("approved")),
                        "issues": str(data.get("issues", ""))[:800]}
        except Exception as e:
            logger.warning("autopilot_review_failed", error=str(e))
        return {"approved": True, "issues": ""}  # 审查异常不阻断主流程

    async def _run_project_tests(self) -> dict | None:
        """TDD 收尾：项目存在测试时运行 pytest，返回 {passed, detail}；无测试返回 None。"""
        root = Path(self.config.project_root)
        has_tests = (root / "tests").is_dir() or bool(list(root.glob("test_*.py")))
        if not has_tests:
            return None
        try:
            result = await self.tool_registry.dispatch(
                "terminal",
                command="python -m pytest -q --tb=line -x",
                workdir=str(root), timeout=180,
            )
            out = ""
            if isinstance(result.output, dict):
                out = (result.output.get("stdout") or "") + "\n" + \
                      (result.output.get("stderr") or "")
            passed = bool(result.success)
            # 提取摘要行（"N passed" / "N failed"）
            tail = "\n".join(line for line in out.strip().splitlines()[-5:] if line.strip())
            return {"passed": passed, "detail": tail[:800]}
        except Exception as e:
            logger.warning("autopilot_test_failed", error=str(e))
            return None


class _CodeGenerateTool:
    """工具适配器 — 把 code_generator 技能暴露给 ReAct 循环（编程模式增强）。

    技能自带：语言检测、Markdown 围栏剥离、语法校验 + 一次自我修复、
    generate / complete / scaffold 三种模式、覆盖与增量保护。
    """

    name = "code_generate"
    description = (
        "Generate or complete code from a specification and write it to a file. "
        "Backed by the code_generator skill: language auto-detection, syntax "
        "validation with automatic self-repair. Use mode='complete' with "
        "existing_code to finish partial code (code completion)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "specification": {"type": "string", "description": "What the code should do."},
            "output_file": {"type": "string", "description": "Target file path."},
            "mode": {"type": "string",
                     "description": "generate (default) / complete / scaffold."},
            "existing_code": {"type": "string",
                              "description": "Existing code to complete (mode=complete)."},
            "language": {"type": "string", "description": "Language hint (auto-detected from extension)."},
        },
        "required": ["specification", "output_file"],
    }

    def __init__(self, agent: "AutoMindAgent") -> None:
        from automind.core.types import PermissionTier, ToolSource
        self.permission_tier = PermissionTier.SENSITIVE
        self.risk_score = 45
        self.source = ToolSource.BUILTIN
        self._agent = agent

    def dry_run_possible(self) -> bool:
        return False

    def get_execution_plan(self, **kwargs: Any) -> str:
        return f"[code_generate] → {kwargs.get('output_file', '?')}"

    def to_openai_schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "parameters": self.parameters}

    def to_anthropic_schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "input_schema": self.parameters}

    async def execute(self, **kwargs: Any) -> Any:
        from automind.core.types import ToolResult
        result = await self._agent.skill_registry.invoke(
            "code_generator", kwargs, self._agent)
        return ToolResult(
            tool_name=self.name,
            success=result.success,
            output=result.output,
            error=result.error or None,
        )
