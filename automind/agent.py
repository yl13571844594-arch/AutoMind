"""AutoMind Agent — 顶层编排器，绑定所有模块。"""

from __future__ import annotations

import os
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
        except Exception as e:
            # 记账失败 = token 统计与成本估算全错，而界面照常显示一个数字，
            # 用户没法察觉。宁可刷日志也不能让它无声无息。
            logger.warning("token_usage_track_failed", error=str(e))
        return resp

    async def generate_stream(self, messages, tools=None):
        import json as _json
        import re as _re
        async for chunk in self._backend.generate_stream(messages, tools=tools):
            # 最后一块可能包含 STREAM_USAGE 元数据标记
            m = _re.search(r'\n<!--STREAM_USAGE:(.*?)-->', chunk if isinstance(chunk, str) else '')
            if m:
                try:
                    usage = _json.loads(m.group(1))
                    self.usage.prompt_tokens += usage.get("prompt_tokens", 0)
                    self.usage.completion_tokens += usage.get("completion_tokens", 0)
                except Exception as e:
                    logger.warning("stream_usage_parse_failed", error=str(e))
                # 移除标记再输出
                yield _re.sub(r'\n<!--STREAM_USAGE:.*?-->', '', chunk if isinstance(chunk, str) else '')
            else:
                yield chunk

    def reset(self) -> None:
        self.usage = TokenUsage()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._backend, name)

    #: 必须转交给真实后端的属性 —— 它们由后端内部读取
    _FORWARD_TO_BACKEND = ("usage_sink", "pre_call_hook",
                           "heartbeat_hook", "call_timeout",
                           "heartbeat_interval")

    def __setattr__(self, name: str, value: Any) -> None:
        """把回调类属性写到真实后端上。

        本类只代理了 __getattr__（读），没代理写。若不特殊处理，
        `agent.llm.usage_sink = fn` 只会在**包装器**上挂一个属性，后端里的
        `self.usage_sink` 永远是 None —— 用量事件一条也发不出来，
        预算钩子同理形同虚设。这类"设了但不生效"的问题极难从现象反推，
        故在此显式转交。
        """
        if name in self._FORWARD_TO_BACKEND and "_backend" in self.__dict__:
            setattr(self._backend, name, value)
            return
        object.__setattr__(self, name, value)
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
    HumanInTheLoop,
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
        # 是否为 clone_for_session 派生的会话实例（决定 close() 的释放范围）
        self._is_session_clone = False

        # ── 核心基础设施 ──────────────────────────
        self.event_bus = EventBus()
        # 执行过程事件回调（由 Web 层注入，用于实时展示执行过程）
        self.event_sink = None
        self._active_goal_id: str | None = None
        self.llm = self._init_llm()
        self._usage_total: dict[str, int] = {
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
        self._budget_warned = False
        self._attach_usage_sink()
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

        # ── 多智能体协同（专业版特性，运行时按需创建）──
        self.orchestrator = None

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
        # 搜索目录由 PluginManager 自己决定（内置目录优先、用户目录其次），
        # 不在这里另写一份 —— 两处各写一遍必然会漂移
        self.plugin_manager = PluginManager()
        # 内置插件开箱即用：默认全部加载（用户插件仍需在界面手动启用）
        self._load_builtin_plugins()
        self.apply_plugin_hooks()

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
        "1. 动手前先用 file_read 确认相关文件的真实内容，不要臆测；"
        "大文件（结果带 truncated 提示）用 offset/limit 按行分段读取需要的部分。\n"
        "2. 一次只做一件明确的事；工具参数必须完整、准确（用确切的工具名与文件路径）。\n"
        "3. 改动最小化、风格与现有代码一致；不要重写无关部分。\n"
        "4. 执行终端命令前评估安全性，危险命令需说明理由。\n"
        "5. 任务完成即停止并简要总结你做了什么、改了哪些文件。\n"
        "6. 若生成 HTML/前端页面，请将完整代码放入 ```html 代码块，便于用户预览。\n"
        "7. 需要从零生成/补全整段代码时优先用 code_generate 工具"
        "（自带语法校验与自动修复；mode='complete' 可补全既有代码）。\n"
        "8. 每次写入/编辑 .py/.json 文件后，观察结果中会附带 syntax_check 自动验证；"
        "若 FAILED 必须立即修复该语法错误再继续（TDD 内环）。\n"
        "9. file_edit 的 old_string 必须与文件内容逐字符一致（含缩进与空白）；"
        "若匹配失败，错误信息会附带文件中最接近的片段（带行号），"
        "请以该片段的原文为准重试，不要凭记忆猜测。"
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

    def _load_builtin_plugins(self) -> None:
        """默认加载随包分发的内置插件（用户插件仍需手动启用）。

        失败不阻断启动：单个插件加载异常只是不生效，绝不会让 Agent 起不来。
        """
        for meta in self.plugin_manager.discover():
            try:
                if self.plugin_manager.is_builtin(meta):
                    self.plugin_manager.load(meta.name)
            except Exception as e:                        # pragma: no cover - 防御性
                logger.warning("builtin_plugin_load_failed",
                               plugin=meta.name, error=str(e))

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
        """多智能体协同执行（专业版特性 multi_agent，未授权时抛 FeatureNotAvailable）。"""
        from automind.core.edition import require_feature

        feature = require_feature("multi_agent")
        if self.llm is None:
            raise RuntimeError("LLM 未初始化，请先配置 API Key。")
        self.llm.reset()
        if self.orchestrator is None:
            self.orchestrator = feature.create(self.llm)
        result = await self.orchestrator.run(task, context="", on_event=on_event)
        result["token_usage"] = self.llm.usage
        return result

    async def run_loop(self, task: str, on_event: Any = None,
                       max_iterations: int | None = None) -> dict:
        """循环工程（Loop Engineering）— 自主"行动-观察-修正"闭环。

        专业版特性 loop_engine：每轮执行任务 → 观察/校验结果 → 未达成则带
        反馈继续修正，直到停止条件（完成/最大轮数/无进展/被中断）。
        未授权时抛 FeatureNotAvailable。
        """
        from automind.core.edition import require_feature

        engine = require_feature("loop_engine")
        return await engine.run(self, task, on_event=on_event,
                                max_iterations=max_iterations)

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
            except Exception as e:
                # 事件推送失败不该影响任务执行，但也不能一声不吭 ——
                # 前端"数字不动/面板空白"的投诉基本都出在这里。
                logger.warning("event_emit_failed",
                               event_type=event.get("type"), error=str(e))

    def _attach_usage_sink(self) -> None:
        """把 LLM 的用量回调接到事件流上，每次调用结束推 usage_update。

        此前流式回答的 token 数只在整段生成完、解析 `<!--STREAM_USAGE:-->`
        标记时才更新一次；长回答期间界面上一直显示 0，看起来像没在计费。
        现在每次 LLM 调用（含流式）结束都推一条，前端累加即可实时显示。
        """
        if self.llm is None:
            return
        self._usage_total = {"prompt_tokens": 0, "completion_tokens": 0,
                             "total_tokens": 0, "calls": 0}

        async def _sink(usage: dict) -> None:
            t = self._usage_total
            t["prompt_tokens"] += usage.get("prompt_tokens", 0)
            t["completion_tokens"] += usage.get("completion_tokens", 0)
            t["total_tokens"] += usage.get("total_tokens", 0)
            t["calls"] += 1
            # 同步记进 ResourceManager —— 不记账，预算检查就永远看到 0
            rm = getattr(self, "resources", None)
            if rm is not None:
                try:
                    rm.tokens.tokens_used.prompt += usage.get("prompt_tokens", 0)
                    rm.tokens.tokens_used.completion += usage.get("completion_tokens", 0)
                except Exception as e:
                    logger.warning("token_accounting_failed", error=str(e))
            await self._emit({"type": "usage_update", "delta": usage,
                              "cumulative": dict(t)})

        self.llm.usage_sink = _sink

        async def _pre_call() -> None:
            """调用前的预算准入 —— ResourceManager 此前实例化后从未被调用。

            分级处置（要求：超预算触发压缩/降级/终止）：
              · ≥ WARN 阈值 —— 推 budget_warning 事件，并尝试压缩上下文（降低
                后续每轮的 prompt 体量），任务继续；
              · ≥ 100%    —— 推 budget_exceeded 并**拒绝本次调用**，避免
                预算保护形同虚设、账单无上限地涨下去。
            """
            rm = getattr(self, "resources", None)
            if rm is None:
                return
            frac = rm.tokens.usage_fraction()
            if frac >= 1.0:
                await self._emit({
                    "type": "budget_exceeded",
                    "used": rm.tokens.tokens_used.total,
                    "budget": rm.tokens.budget,
                })
                logger.error("token_budget_exhausted",
                             used=rm.tokens.tokens_used.total, budget=rm.tokens.budget)
            elif frac >= self._BUDGET_WARN_AT and not self._budget_warned:
                self._budget_warned = True
                await self._emit({
                    "type": "budget_warning",
                    "used": rm.tokens.tokens_used.total,
                    "budget": rm.tokens.budget,
                    "percent": round(frac * 100, 1),
                })
                logger.warning("token_budget_high", percent=round(frac * 100, 1))
                await self._compress_context()
            # 速率限制 + 硬性预算（超了会抛 RuntimeError，由此拒绝本次调用）
            await rm.before_llm_call()

        self.llm.pre_call_hook = _pre_call

        async def _heartbeat(elapsed: float, phase: str) -> None:
            """LLM 调用在飞行中时每几秒发一条 —— 让界面能证明"还活着"。

            长任务此前在对话区完全无反馈，用户分不清"在想"和"挂了"。
            """
            await self._emit({"type": "heartbeat", "phase": phase,
                              "elapsed": round(elapsed, 1)})

        self.llm.heartbeat_hook = _heartbeat
        self.llm.call_timeout = float(
            getattr(self.config.execution, "llm_call_timeout_seconds", 300.0))

    #: 用量到达该比例即预警并尝试压缩上下文
    _BUDGET_WARN_AT = 0.8

    async def _compress_context(self) -> None:
        """预算吃紧时压缩上下文（能压则压，压不了不影响主流程）。

        ContextManager.compress 是**协程** —— 早先这里同步调用它，既没真的
        压缩，还留下一个未 await 的协程（RuntimeWarning）。必须 await。
        """
        try:
            mgr = getattr(self, "context_mgr", None)
            fn = getattr(mgr, "compress", None)
            if not callable(fn):
                return
            import inspect
            r = fn(self.llm)
            if inspect.isawaitable(r):
                await r
            logger.info("context_compressed_for_budget")
        except Exception as e:
            logger.warning("context_compress_failed", error=str(e))

    def _react_callbacks(self, tag: int | None = None):
        """构造 ReAct 的思考/行动回调，转发到 event_sink。"""
        step = {"n": 0}

        async def on_thought(text: str) -> None:
            step["n"] += 1
            await self._emit({"type": "step_thought", "iter": tag,
                              "step": step["n"], "text": (text or "")[:1200]})

        async def on_action(tc, result) -> None:
            out = result.output if result.success else result.error
            # 浏览器/截图工具：把 base64 截图单独推给前端渲染 —— 让"网页交互效果"
            # 直接可视化在对话框里（step_action 的 output 是 600 字文本，塞不下图片）。
            preview = self._extract_screenshot(result)
            if preview and len(preview) <= 1_500_000:
                await self._emit({"type": "browser_preview", "tool": tc.name,
                                  "screenshot_base64": preview})
            # output 里若含 base64，摘要掉，避免 600 字全是乱码
            if isinstance(out, dict) and any(k in out for k in ("screenshot_base64", "base64")):
                out = {k: ("<base64 截图>" if k in ("screenshot_base64", "base64") else v)
                       for k, v in out.items()}
            await self._emit({"type": "step_action", "iter": tag,
                              "goal_id": getattr(self, "_active_goal_id", None),
                              "tool": tc.name,
                              "args": {k: str(v)[:200] for k, v in (tc.arguments or {}).items()},
                              "success": result.success,
                              "output": str(out)[:600]})
            # 工具失败单独发一条：step_action 在界面上和成功步骤长得一样，
            # 失败原因被淹没在流水里。前端据此标红并给出原因。
            if not result.success:
                ex = getattr(self, "react_executor", None)
                streak = 0
                if ex is not None:
                    streak = (getattr(ex, "_tool_failures", {})
                              .get(tc.name, {}).get("streak", 0))
                await self._emit({
                    "type": "tool_error", "tool": tc.name,
                    "error": str(result.error or "")[:600],
                    "streak": streak,
                    "circuit_open": streak >= getattr(
                        type(ex), "FAILURE_THRESHOLD", 3) if ex else False,
                })

        return on_thought, on_action

    @staticmethod
    def _extract_screenshot(result: Any) -> str | None:
        """从工具结果里取出截图 base64（供前端渲染网页交互效果）。

        识别浏览器/截图工具返回的 ``screenshot_base64`` 或 ``base64`` 字段；
        兼容 ``data:image/...;base64,`` 前缀。非截图结果返回 None。
        """
        if not getattr(result, "success", False):
            return None
        out = getattr(result, "output", None)
        if not isinstance(out, dict):
            return None
        b64 = out.get("screenshot_base64") or out.get("base64")
        if not isinstance(b64, str) or not b64:
            return None
        if b64.startswith("data:") and "," in b64:
            b64 = b64.split(",", 1)[1]
        return b64

    async def preflight_check(self) -> dict:
        """任务开始前的配置自检 —— 早报错好过跑到一半才失败。

        只做"能立刻判定"的检查，不联网、不消耗 token。
        """
        problems: list[str] = []
        if self.llm is None:
            problems.append(
                f"LLM 未初始化：{getattr(self, '_llm_init_error', '未配置 API Key')}")
        try:
            n_tools = len(self.tool_registry._tools)
            if n_tools == 0:
                problems.append("没有任何可用工具，任务将无法执行实际操作")
        except Exception as e:
            problems.append(f"工具注册表不可读：{e}")
        try:
            root = Path(self.config.project_root)
            if not root.is_dir():
                problems.append(f"项目目录不存在：{root}")
            elif not os.access(root, os.W_OK):
                problems.append(f"项目目录不可写：{root}")
        except Exception as e:
            problems.append(f"项目目录检查失败：{e}")

        report = {"ok": not problems, "problems": problems}
        if problems:
            logger.warning("preflight_problems", problems=problems)
            await self._emit({"type": "preflight_warning", **report})
        return report

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
        # Goal 只存 children，父指针需现推 —— 观测中心用它还原真实的计划层级
        # （叶子之间并非顺序依赖，串成链会显示出并不存在的依赖关系）
        parent_of: dict[str, str | None] = {}

        def _map_parents(node, parent_id: str | None) -> None:
            parent_of[node.id] = parent_id
            for child in node.children:
                _map_parents(child, node.id)

        _map_parents(plan.root_goal, None)
        root_id = plan.root_goal.id
        await self._emit({
            "type": "plan_created",
            "task": plan.task_description,
            "steps": [
                {"goal_id": g.id, "description": g.description,
                 "tool": g.assigned_action.tool_name if g.assigned_action else None,
                 # parent_id 为 None 表示直挂根；根自身的 id 也一并告知，
                 # 便于消费方把「根目标」映射到自己的 root 节点
                 "parent_id": parent_of.get(g.id)}
                for g in leaves
            ],
            "root_goal_id": root_id,
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
        # 记录当前步骤：ReAct 的工具调用事件据此归属到所属计划步骤
        self._active_goal_id = goal.id
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
        if getattr(self, "_active_goal_id", None) == step_result.goal_id:
            self._active_goal_id = None
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
        """请求人工批准；**任何异常一律按"拒绝"处理**。

        安全修复（v1.4.4）：此前 `except Exception: return True` —— 回调一出错就
        默认放行。而回调最常见的出错原因恰恰是前端断开（`ws.send_json` 抛异常），
        于是"用户关掉页面"反而变成"后续所有敏感操作自动获批"，「询问」模式在
        最需要它的时候等同于「全批准」。审批是安全控制，只能 fail-closed：
        问不到人，就当作没批准。
        """
        tool_name = getattr(action, "tool_name", "unknown")
        params = getattr(action, "parameters", {}) or {}
        # 优先走 Web 注入的审批回调
        if self.approval_callback is not None:
            try:
                return bool(await self.approval_callback(
                    tool_name, params, "sensitive",
                    f"步骤需要批准：{getattr(goal, 'description', '')}"))
            except Exception as e:
                logger.warning("approval_callback_failed", tool=tool_name,
                               error=str(e), decision="denied")
                # 让用户在界面上看到"为什么这一步没做"，而不是默默跳过
                await self._emit({
                    "type": "approval_failed", "tool": tool_name,
                    "reason": f"审批通道异常（{type(e).__name__}），按拒绝处理",
                })
                return False
        # 没有回调：交给 human_loop（CLI 交互）；非交互环境下它会拒绝，
        # 绝不会因为"没人可问"就自动放行。
        request = ApprovalRequest(
            goal=goal, action=action, risk_level="sensitive",
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
        except Exception as e:
            # 项目索引进不了上下文，模型就"看不见"代码结构，回答会明显变差
            # —— 但表现只是"答得不好"，极难归因，必须留痕。
            logger.warning("project_index_unavailable", error=str(e))

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

    def _rebind_llm(self) -> None:
        """按当前 ``config.llm`` 重建 LLM 后端，并把持有它的模块重新指过去。

        规划器 / 执行器 / 反思模块在构造时各存了一份 ``self.llm`` 引用，
        只换 ``agent.llm`` 而不同步它们，会出现"界面显示已切到 B 模型、
        实际规划仍在用 A 模型"的鬼故事。
        """
        self._llm_init_error = ""
        self.llm = self._init_llm()
        self._attach_usage_sink()
        self.hierarchical_planner.llm = self.llm
        self.plan_executor.llm = self.llm
        self.quality_assessor.llm = self.llm
        self.reflexion.llm = self.llm
        # ReAct 执行器每次任务按需新建，置空即可让它下次取到新 llm
        self.react_executor = None

    def switch_llm(self, llm_config: Any) -> bool:
        """只替换 LLM（不重建工具/技能/记忆/项目索引）。

        切换交互模式时此前走的是"整个 Agent 重建"：``AgentConfig.auto_load``
        重新扫盘、重建 ChromaDB、重新注册全部工具与技能、重扫项目索引 ——
        用户在 Web 上点一下模式切换要卡 2~3 秒，而真正变的只有一个模型名。

        Args:
            llm_config: 新的 ``LLMProviderConfig``（调用方负责解析 Key/api_base）。

        Returns:
            LLM 是否初始化成功（False 表示 Key/地址有问题，``self.llm is None``）。
        """
        self.config.llm = llm_config
        self._rebind_llm()
        logger.info("llm_switched", provider=llm_config.provider,
                    model=llm_config.model, ready=self.llm is not None)
        return self.llm is not None

    #: 会话克隆共享的重资源 —— 建一次几秒钟，且对并发任务是只读的
    _SHARED_ON_CLONE = (
        "env", "project_indexer", "input_parser", "memory",
        "tool_registry", "skill_registry", "mcp_registry",
        "checkpoint_mgr", "hooks", "plugin_manager",
    )

    def clone_for_session(self) -> AutoMindAgent:
        """派生一个执行态独立的会话 Agent（轻量：不重扫项目、不重建记忆库）。

        为什么必须隔离：并发任务此前共用同一个全局 Agent 实例，
        ``_interaction`` / ``_mode`` / ``context_mgr`` / ``_current_plan`` /
        ``llm.usage`` 全是共享可变状态。两个标签页同时跑，会出现
        A 的"对话"模式被 B 的"循环编程"覆盖、两边上下文互相串、
        token 计数被对方 ``reset()`` 清零 —— 且没有任何报错提示。

        共享的是重且只读的部分（工具/技能注册表、记忆库、项目索引、
        环境探测结果），独享的是每次任务都会被改写的部分。
        """
        from automind.core.events import EventBus
        from automind.planning.plan_executor import PlanExecutor
        from automind.reflection.consistency_checker import ConsistencyChecker
        from automind.reflection.quality_assessor import QualityAssessor
        from automind.reflection.reflexion import ReflexionEngine
        from automind.state.resource_manager import ResourceManager
        from automind.tools.function_calling import FunctionCallHandler
        from automind.tools.permissions import PermissionEngine

        clone = object.__new__(type(self))
        for name in self._SHARED_ON_CLONE:
            setattr(clone, name, getattr(self, name))

        clone.config = self.config.model_copy(deep=True)
        clone._is_session_clone = True
        clone._llm_init_error = ""

        # ── 独享：LLM 包装器 + 用量记账 ──
        # 后端客户端本身只是个 httpx 会话，构造是毫秒级；但 usage_sink 是挂在
        # 后端上的，若共享后端，两个会话的用量事件会串到最后一个注册者身上。
        clone.event_bus = EventBus()
        clone.event_sink = None
        clone.approval_callback = None
        clone._active_goal_id = None
        clone._usage_total = {"prompt_tokens": 0, "completion_tokens": 0,
                              "total_tokens": 0, "calls": 0}
        clone._budget_warned = False
        clone.llm = clone._init_llm()
        clone.resources = ResourceManager(token_budget=clone.config.llm.max_tokens * 10)
        clone._attach_usage_sink()

        # ── 独享：权限 / 上下文 / 规划 / 反思 ──
        clone.permissions = PermissionEngine(
            policy=clone.config.permissions,
            project_root=clone.config.project_root,
            approval_mode=getattr(clone.config.execution, "approval_mode", "auto"),
        )
        clone.context_mgr = ContextManager(
            max_tokens=clone.config.memory.short_term_max_tokens,
            summary_threshold=clone.config.memory.short_term_summary_threshold,
        )
        clone.hierarchical_planner = HierarchicalPlanner(clone.llm)
        clone.react_executor = None
        clone.plan_executor = PlanExecutor(
            clone.llm, clone.tool_registry, clone.permissions,
            max_retries=clone.config.execution.max_retries,
            parallel=clone.config.execution.parallel_execution,
            use_cache=clone.config.execution.subtask_cache,
        )
        clone.fn_handler = FunctionCallHandler(clone.tool_registry)
        clone.quality_assessor = QualityAssessor(clone.llm)
        clone.consistency_checker = ConsistencyChecker()
        clone.reflexion = ReflexionEngine(clone.llm, clone.memory.long_term)
        clone.human_loop = HumanInTheLoop(
            auto_approve_safe=clone.config.execution.auto_approve_safe)
        clone.orchestrator = None

        # ── 独享：会话状态 ──
        clone._current_plan = None
        clone._agent_state = AgentState()
        clone._mode = self._mode
        clone._interaction = self._interaction
        clone._chat_history = []
        return clone

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

        # 办公自动化与集成工具（v1.5.0）
        #
        # 这些工具的第三方依赖是**可选**的（openpyxl / python-docx / pypdf /
        # icalendar / pywin32），故一律注册、按需导入：模型始终能看到这些能力并
        # 规划到它们，真正调用时若缺库，返回的是一句可照抄的 pip 命令，
        # 而不是让整个工具凭空消失、模型只能干瞪眼。
        # 逐组 try：某一组导入失败（比如残缺安装）不该连累其余工具。
        for _loader in (self._register_office_tools,
                        self._register_net_tools,
                        self._register_data_tools,
                        self._register_collab_tools,
                        self._register_media_tools,
                        self._register_system_tools):
            try:
                _loader()
            except Exception as e:                        # pragma: no cover - 防御性
                logger.warning("optional_tools_register_failed",
                               group=_loader.__name__, error=str(e))

    def _register_office_tools(self) -> None:
        from automind.tools.office import EmailTool, ExcelTool, PdfTool, PptTool, WordTool
        for tool in (ExcelTool(), WordTool(), PdfTool(), PptTool(), EmailTool()):
            self.tool_registry.register(tool)

    def _register_media_tools(self) -> None:
        """多媒体工具（v1.6.0）：截屏 / OCR / 图像 / 图表 / 音频 / 视频。"""
        from automind.tools.media_tools import (
            AudioTool,
            ChartTool,
            ImageTool,
            OcrTool,
            ScreenshotTool,
            VideoTool,
        )
        for tool in (ScreenshotTool(), OcrTool(), ImageTool(),
                     ChartTool(), AudioTool(), VideoTool()):
            self.tool_registry.register(tool)

    def _register_system_tools(self) -> None:
        """系统工具（v1.6.0）：git / 进程 / 剪贴板 / CSV。"""
        from automind.tools.csv_tool import CsvTool
        from automind.tools.system_tools import ClipboardTool, GitTool, ProcessTool
        self.tool_registry.register(GitTool(project_root=self.config.project_root))
        self.tool_registry.register(ProcessTool())
        self.tool_registry.register(ClipboardTool())
        self.tool_registry.register(CsvTool())

    def _register_net_tools(self) -> None:
        from automind.tools.net_tools import HttpRequestTool, WebSearchTool
        self.tool_registry.register(HttpRequestTool())
        self.tool_registry.register(WebSearchTool())

    def _register_data_tools(self) -> None:
        from automind.tools.data_tools import ArchiveTool, DbQueryTool, FileSearchTool
        self.tool_registry.register(DbQueryTool())
        self.tool_registry.register(FileSearchTool(project_root=self.config.project_root))
        self.tool_registry.register(ArchiveTool())

    def _register_collab_tools(self) -> None:
        from automind.tools.collab_tools import CalendarTool, ImIntegrationTool, NotifyTool
        for tool in (NotifyTool(), CalendarTool(), ImIntegrationTool()):
            self.tool_registry.register(tool)

    async def close(self) -> None:
        """释放全部持有资源 — MCP 连接 / 记忆系统（ChromaDB）/ LLM 连接池。

        幂等：重复调用安全；单项失败不阻断其余清理。

        会话克隆（``clone_for_session``）只释放自己独享的 LLM 连接 —— MCP 与
        记忆库是与主 Agent 共享的，克隆去关会把还在跑的其它会话一并弄挂。
        """
        if not getattr(self, "_is_session_clone", False):
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
    ) -> AutoMindAgent:
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
        from automind.core.prompts import ROLE_PROMPTS

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

    def __init__(self, agent: AutoMindAgent) -> None:
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
