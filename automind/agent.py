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
        # 下一次触发上下文压缩的用量比例（每压一次往后推 5%）
        self._next_compact_at = self._BUDGET_WARN_AT
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
        # 随包内置的工具名单 —— 界面据此把「内置」与后来接入的 MCP / 插件工具
        # 区分开（用户需要一眼看出哪些是开箱即用、哪些是自己接的）
        self.builtin_tool_names: frozenset[str] = frozenset(self.tool_registry.list_names())

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
        #: 会话标识（Web 层注入；决定轨迹分文件与目录级隔离的工作副本）
        self.session_id: str = ""
        #: 最近一次工作区准备结果（isolated=False 时表示仍在共享目录）
        self._workspace_plan: Any = None
        #: 最近一次闭环的证据摘要（验收来源/产物断言/各环节是否真正执行）
        self._verify_state: dict[str, Any] = {}
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
        # 会话身份 + （可选的）私有工作目录 —— 两者都经 contextvar 下发到
        # 工具边界，使并发会话的文件写入不再互相覆盖，也让轨迹能按会话切分。
        from automind.core import session_ctx as _sctx

        sid = getattr(self, "session_id", "") or "default"
        plan = self._prepare_workspace(sid)
        with _sctx.bind_session(sid, run_id=_sctx.new_run_id("run"),
                                workspace=plan.path if plan.isolated else None):
            return await self._run_impl_bound(user_input, plan)

    def _prepare_workspace(self, sid: str) -> Any:
        """按配置准备会话工作目录（未开启隔离时返回共享 project_root）。"""
        from automind.core import workspace as _ws

        plan = _ws.prepare(sid, self.config.project_root, self.config.execution)
        self._workspace_plan = plan
        if plan.reason and not plan.isolated:
            logger.info("workspace_isolation_skipped", session=sid,
                        reason=plan.reason)
        return plan

    async def _run_impl_bound(self, user_input: str, plan: Any) -> AgentResult:
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
            # ReAct/编程模式：产出了实质答案即视为成功。
            # v1.6.4：优先用执行器给出的**结构化停止原因**，而不是靠匹配
            # 回复文本里有没有"最大迭代步数"几个字 —— 那种判定既脆弱
            # （模型自己复述这几个字就会被误判失败）又无法区分
            # "到达上限"与"被取消/异常中断"。文本匹配仅作为兜底保留。
            ex = getattr(self, "react_executor", None)
            reason = getattr(ex, "stop_reason", "") if ex is not None else ""
            if reason:
                produced = reason == "no_more_tools" and bool(result_text)
            else:
                produced = bool(result_text and "最大迭代步数" not in result_text)
            success = bool(produced or quality.overall_pass)
        else:
            success = quality.overall_pass

        # 收尾事件：把工作区、验收证据与省 token 总账推给观测与轨迹。
        # 这三样此前都只活在各自的模块里，落盘证据里一条都看不到。
        try:
            ev: dict[str, Any] = {"type": "run_summary", "success": success,
                                  "duration_ms": round(duration, 1),
                                  "steps": len(step_results) if step_results else 0}
            wp = getattr(self, "_workspace_plan", None)
            if wp is not None:
                ev["workspace"] = wp.as_dict()
            if self._verify_state:
                ev["verify"] = self._verify_state
            rs = getattr(self, "react_executor", None)
            if rs is not None:
                ev["react"] = rs.token_report()
            await self._emit(ev)
        except Exception as e:                        # pragma: no cover - 纯观测
            logger.warning("run_summary_emit_failed", error=str(e))

        out_text = result_text
        if self._workspace_plan is not None and self._workspace_plan.isolated:
            out_text += (
                f"\n\n---\n📁 本次任务在**会话独立工作目录**中执行："
                f"`{self._workspace_plan.path}`\n"
                f"（为避免并发覆盖，改动没有直接写进你的项目目录。"
                f"需要取回产物请在该目录查看，或调用工作区导出。）")

        return AgentResult(
            success=success,
            output=out_text,
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
        """观察阶段 — 验收是否真正完成。

        v1.6.3 及更早：把 ``output[:2500]`` 交给 LLM 做文本自评，``done?`` 由
        同一个模型说了算；且**任何异常**（网络抖动、JSON 解析失败、模型返回
        废话）都落进 ``except: pass``，然后返回
        ``{"done": False, "reason": "无法判定，继续尝试。"}`` —— 调用方把它当成
        「验收未通过」，于是白烧最多 ``auto_verify_max_rounds`` 轮修复 token，
        最后还给用户报一个「验收未过 ✗」。**验收设施故障被记成了任务失败**。

        现在分两段判定，并显式区分「通过 / 未通过 / 验收不可用」：

          1. **确定性断言（产物级，不花 token）**：从任务与执行结果里抽出
             被声称产生的文件路径，逐个核验是否**真的存在**、是否为**空文件**、
             以及声明的关键词是否真的出现在内容里。任一断言失败 → 直接判未
             完成，LLM 无权推翻（自评"自嗨通过"从此过不去）。
          2. **语义判定**：再由 LLM 判断剩余部分是否完成；LLM 调用/解析失败
             时返回 ``available=False``，调用方据此**不进入修复轮**、也不再
             谎报"验收未过"，而是如实说明"验收不可用"。

        Returns:
            ``{"done": bool, "available": bool, "reason": str,
            "artifacts": {...}, "source": str}``
        """
        from automind.core.json_utils import extract_json

        artifacts = self._verify_artifacts(task, output)
        await self._emit({"type": "verify_evidence", "artifacts": artifacts})

        if artifacts["checked"] and not artifacts["passed"]:
            return {
                "done": False, "available": True, "source": "artifact",
                "artifacts": artifacts,
                "reason": "产物断言未通过：" + "；".join(artifacts["failures"])[:800],
            }

        prompt = (
            f"你是严格的验收员。判断下面的任务是否已真正完成且正确。\n\n"
            f"任务：{task}\n\n执行结果：\n{output[:2500]}\n\n"
        )
        if artifacts["checked"]:
            prompt += (
                "以下**客观事实**已由系统核验，不要推翻，也不要要求重新生成：\n"
                + "\n".join(f"- {c}" for c in artifacts["claims"])
                + "\n\n"
            )
        prompt += (
            '只输出 JSON：{"done": true 或 false, '
            '"reason": "若未完成，明确说明还差什么、下一步如何修正"}'
        )
        try:
            resp = await self.llm.generate([{"role": "user", "content": prompt}])
            data = extract_json(resp.text)
            if isinstance(data, dict) and "done" in data:
                return {
                    "done": bool(data.get("done")), "available": True,
                    "source": "llm", "artifacts": artifacts,
                    "reason": str(data.get("reason", ""))[:600],
                }
            detail = "模型未按 JSON 格式作答"
        except Exception as e:
            logger.warning("loop_verify_failed", error=f"{type(e).__name__}: {e}")
            detail = f"验收调用异常（{type(e).__name__}）"

        # 验收不可用：明确说"判不了"，而不是伪装成"没通过"。
        # 调用方据此跳过修复轮 —— 拿不到反馈的修复轮纯属烧 token。
        return {
            "done": False, "available": False, "source": "unavailable",
            "artifacts": artifacts,
            "reason": f"{detail}，本次未做验收（不会据此判定任务失败）。",
        }

    # ── 产物级确定性验收（不花 token 的客观证据）────────────
    #: 任务文本里出现这些词时，"文件真的存在"才成为硬性验收条件
    _FILE_TASK_HINTS = ("文件", "脚本", "代码", "生成", "创建", "写入", "导出",
                        "报告", "报表", "文档", "保存", "输出到", "保存到",
                        "file", "script", "create", "write", "generate", "export")
    #: 产物体积下限：小于此字节数视为"空壳产物"，不构成交付
    _MIN_ARTIFACT_BYTES = 1
    #: 单次验收最多核验的路径数（防止刷屏与长耗时）
    _MAX_ARTIFACT_CLAIMS = 12

    @classmethod
    def _claimed_paths(cls, *texts: str) -> list[str]:
        """从文本里抽出"被声称产生的文件路径"（去重、保序、限量）。

        只认**带扩展名的相对/绝对路径**：像 ``automind/agent.py``、
        ``report.docx``、``C:\\out\\a.csv``。没有扩展名的裸词（如 ``tests``）
        不当作产物，避免把普通名词误判成"文件没生成"。
        """
        import re

        seen: dict[str, None] = {}
        # 反引号包裹、引号包裹、或裸路径
        pattern = re.compile(
            r"(?:[A-Za-z]:[\\/])?[A-Za-z0-9_\-./\\\u4e00-\u9fff]+"
            r"\.(?:py|js|ts|tsx|jsx|json|ya?ml|toml|md|txt|csv|xlsx?|docx?|pptx?|"
            r"pdf|html?|css|sql|sh|bat|ps1|ini|cfg|log|xml|zip|png|jpg|jpeg|svg)\b",
            re.IGNORECASE,
        )
        for t in texts:
            if not t:
                continue
            for m in pattern.findall(t or ""):
                p = m.strip().strip("`\"'()[]{}，,。;；:：")
                if not p or len(p) > 240:
                    continue
                low = p.lower()
                if low.startswith(("http://", "https://", "data:")):
                    continue
                if any(x in low for x in (".pyc", "__pycache__", ".git/")):
                    continue
                if p not in seen:
                    seen[p] = None
                if len(seen) >= cls._MAX_ARTIFACT_CLAIMS:
                    return list(seen)
        return list(seen)

    def _resolve_artifact(self, raw: str) -> Path:
        """把声称的路径解析到真实位置（绝对路径 / 项目根 / 会话工作区）。"""
        p = Path(raw)
        if p.is_absolute():
            return p
        candidates = []
        ws = None
        try:
            from automind.core.session_ctx import workspace as _ws

            ws = _ws()
        except Exception:
            ws = None
        if ws:
            candidates.append(Path(ws) / raw)
        candidates.append(Path(self.config.project_root) / raw)
        candidates.append(Path.cwd() / raw)
        for c in candidates:
            if c.exists():
                return c
        return candidates[-1]

    def _verify_artifacts(self, task: str, output: str) -> dict[str, Any]:
        """产物级确定性断言 —— 把"模型说做了"变成"磁盘上真有"。

        核验三类事实（全部不花 token）：
          · 声称的文件是否**存在**；
          · 是否**非空**（写了 0 字节不能算交付）；
          · 任务里点名的关键词（``X 里要包含 Y`` 之类）是否**真的出现在内容里**。

        判定口径（保守，避免误报失败）：
          · **只有任务本身在要文件**时才把"文件不存在"当硬失败 ——
            纯问答/分析类任务提到一个路径名不该因此判败；
          · 任务里直接出现的具体路径（``生成 report.md``）比结果里顺带提及的
            路径要求更严：前者必查，后者仅在任务指向文件时才算。
        """
        import re

        task_lower = (task or "").lower()
        task_wants_file = any(h in task_lower for h in self._FILE_TASK_HINTS)
        # 任务里显式点名的路径：这些是"要求"，缺失即失败
        required = self._claimed_paths(task)
        # 结果里声称产出的路径：任务指向文件时才算数
        claimed = self._claimed_paths(output)
        targets: list[tuple[str, bool]] = []
        seen: set[str] = set()
        for p in required:
            if p not in seen:
                targets.append((p, True))
                seen.add(p)
        for p in claimed:
            if p not in seen:
                targets.append((p, task_wants_file))
                seen.add(p)

        # 任务中点名的、要求出现在产物里的关键词（"包含 X" / "must contain X"）
        wanted: list[str] = []
        for m in re.finditer(r"(?:包含|含有|带上|写明|写入|含)\s*[「\"']?([^\s，,。；;、\"'」]{2,40})",
                             task or ""):
            term = m.group(1).strip()
            if term and term not in wanted:
                wanted.append(term)
        for m in re.finditer(r"(?:must\s+(?:contain|include))\s+[`\"']?([^\s,;\`\"']{2,40})",
                             task or "", re.IGNORECASE):
            term = m.group(1).strip()
            if term and term not in wanted:
                wanted.append(term)
        wanted = wanted[:5]

        claims: list[str] = []
        failures: list[str] = []
        checked = 0
        for raw, mandatory in targets:
            path = self._resolve_artifact(raw)
            checked += 1
            if not path.exists():
                note = f"声称的文件不存在：{raw}"
                if mandatory:
                    failures.append(note)
                    claims.append(f"✗ {note}")
                else:
                    claims.append(f"· {note}（任务未明确要求该文件，不计为失败）")
                continue
            try:
                size = path.stat().st_size
            except OSError as e:
                claims.append(f"· 无法读取 {raw} 的大小：{e}")
                continue
            if size <= self._MIN_ARTIFACT_BYTES:
                failures.append(f"文件为空（{size} 字节），不算交付：{raw}")
                claims.append(f"✗ 文件为空：{raw}")
                continue
            note = f"✓ 文件存在且非空：{raw}（{size} 字节）"
            if wanted and path.suffix.lower() in (
                    ".md", ".txt", ".py", ".json", ".csv", ".html", ".yml", ".yaml"):
                try:
                    body = path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    body = ""
                if body:
                    missing = [w for w in wanted if w not in body]
                    if missing:
                        failures.append(
                            f"{raw} 中未找到任务要求的内容：{'、'.join(missing)}")
                        note += f"；✗ 缺少关键词 {'、'.join(missing)}"
                    else:
                        note += f"；✓ 含要求关键词 {'、'.join(wanted)}"
            claims.append(note)

        return {
            "checked": checked,
            "passed": not failures,
            "failures": failures,
            "claims": claims,
            "required": required,
            "task_wants_file": task_wants_file,
        }

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
            elif frac >= self._BUDGET_WARN_AT:
                if not self._budget_warned:
                    self._budget_warned = True
                    await self._emit({
                        "type": "budget_warning",
                        "used": rm.tokens.tokens_used.total,
                        "budget": rm.tokens.budget,
                        "percent": round(frac * 100, 1),
                    })
                    logger.warning("token_budget_high", percent=round(frac * 100, 1))
                # 压缩可以反复做（ReAct 那条路是纯本地折叠，不花钱），
                # 但别每次调用都做 —— 每再涨 5% 额度才压一次。
                if frac >= self._next_compact_at:
                    self._next_compact_at = frac + 0.05
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
        """预算吃紧时压缩**真正会被重发的那份上下文**。

        v1.6.2 之前这里只压 ContextManager —— 而 ContextManager 存的是
        "用户说了什么、Agent 答了什么"的流水账，`get_messages_for_llm()`
        在 ReAct 路径上**从来没有被调用过**。于是预算告警时会去调一次 LLM
        生成摘要（真金白银），而下一轮 ReAct 请求体一个 token 都没少 ——
        **花了摘要的钱，压不到 ReAct 头上**。

        正确的顺序：
          1. 先压正在跑的 ReAct 消息列表（那才是每轮重发的东西），且是
             纯本地的字符串折叠，**不花一分钱**；
          2. 只有在没有 ReAct 在跑（对话 / Plan-and-Execute 路径）时，
             才退回到 ContextManager 的摘要压缩。
        """
        ex = getattr(self, "react_executor", None)
        # 必须是**正在跑**的那个执行器：任务跑完后 messages 还留着，
        # 去折叠一份不会再发出去的旧列表，等于什么都没省。
        if ex is not None and getattr(ex, "running", False) and ex.messages:
            stat = None
            try:
                stat = ex.compact()
                logger.info("react_context_compacted_for_budget", **stat)
            except Exception as e:
                logger.warning("react_compact_failed", error=str(e))
            if stat and stat.get("folded"):
                # 事件推送失败不能把"已经省下来了"这个事实一起吞掉 ——
                # 否则又会退回去花钱做一次无用的摘要。
                try:
                    await self._emit({"type": "context_compacted",
                                      "scope": "react", **stat})
                except Exception as e:
                    logger.warning("compact_event_emit_failed", error=str(e))
                return   # 已经省下来了，不必再花钱做摘要
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
        from automind.tools.output_budget import limits_from_config

        ex = self.config.execution
        # 每次重建以注入最新的权限/审批回调
        self.react_executor = ReActExecutor(
            self.llm, self.tool_registry,
            max_iterations=ex.max_iterations,
            permissions=self.permissions,
            approval_cb=self.approval_callback,
            auto_validate=ex.auto_test,  # TDD 内环开关
            # 工具 schema 每一步都要重发；只发与任务相关的那批（0 = 不限）
            tool_budget=getattr(ex, "react_tool_budget", 14),
            # 工具结果进上下文前的体积上限（默认 12000 字符）——
            # 当前轮新产生的大输出必须在这里先夹住，折叠来不及救
            output_limits=limits_from_config(ex),
            # 历史观察折叠时每条保留的字符数（可配，便于按成本调参；0 = 类默认）
            obs_keep_chars=(getattr(ex, "compact_keep_obs_chars", 0) or None),
        )
        # 编程模式下注入面向编程的引导
        if self._interaction == InteractionMode.CODING:
            context = f"{self.CODING_SYSTEM_PROMPT}\n\n{context}"
        on_thought, on_action = self._react_callbacks()
        text = await self.react_executor.run(
            task, context, on_thought=on_thought, on_action=on_action)
        # 省 token 总账推给观测/前端：让"长任务越跑越省"看得见、可调参
        try:
            rep = self.react_executor.token_report()
            await self._emit({"type": "react_savings", **rep})
        except Exception as e:                    # pragma: no cover - 纯观测
            logger.warning("react_savings_emit_failed", error=str(e))
        return text

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

    async def _on_approval_needed(self, goal: Any, action: Any) -> Any:
        """请求人工批准；**任何异常一律按"拒绝"处理**。

        安全修复（v1.4.4）：此前 `except Exception: return True` —— 回调一出错就
        默认放行。而回调最常见的出错原因恰恰是前端断开（`ws.send_json` 抛异常），
        于是"用户关掉页面"反而变成"后续所有敏感操作自动获批"，「询问」模式在
        最需要它的时候等同于「全批准」。审批是安全控制，只能 fail-closed：
        问不到人，就当作没批准。

        返回 `ApprovalOutcome`：除批准/拒绝外，还能表达「改参数后批准」
        （`ApprovalAction.MODIFY`）—— 调用方据此替换本次执行的参数。
        """
        from automind.state.human_loop import ApprovalOutcome

        tool_name = getattr(action, "tool_name", "unknown")
        params = getattr(action, "parameters", {}) or {}
        # 优先走 Web 注入的审批回调
        if self.approval_callback is not None:
            try:
                outcome = ApprovalOutcome.normalize(await self.approval_callback(
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
                return ApprovalOutcome(approved=False)
            if outcome.approved and outcome.modified:
                await self._emit({
                    "type": "approval_modified", "tool": tool_name,
                    "params": {k: str(v)[:200] for k, v in outcome.arguments.items()},
                })
            return outcome
        # 没有回调：交给 human_loop（CLI 交互）；非交互环境下它会拒绝，
        # 绝不会因为"没人可问"就自动放行。
        request = ApprovalRequest(
            goal=goal, action=action, risk_level="sensitive",
            reason="Manual approval required",
        )
        response = await self.human_loop.request_approval(request)
        if response.action == ApprovalAction.MODIFY:
            return ApprovalOutcome(approved=True,
                                   arguments=dict(response.modifications or {}),
                                   comment=response.comment)
        return ApprovalOutcome(approved=response.action == ApprovalAction.APPROVE,
                               comment=response.comment)

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
        clone._next_compact_at = self._BUDGET_WARN_AT
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
        # 会话标识与工作区结果按会话独享（共享的话并发会话会互相覆盖）
        clone.session_id = ""
        clone._workspace_plan = None
        return clone

    def _register_default_tools(self) -> None:
        """注册默认工具。"""
        ex = self.config.execution
        timeout = float(getattr(ex, "tool_timeout_seconds", 300.0))
        self.tool_registry.register(TerminalTool(
            workdir=self.config.project_root,
            timeout=timeout,
            max_timeout=float(getattr(ex, "tool_timeout_max_seconds", 1800.0)),
            background_enabled=bool(getattr(ex, "terminal_background_enabled", True)),
        ))
        # 后台通道的取回入口 —— 与 terminal(background=True) 成对交付
        from automind.tools.terminal import TerminalBackgroundTool as _TBG
        self.tool_registry.register(_TBG())
        # 文件工具开启路径穿越防护：所有读写限定在 project_root 之内。
        # v1.6.4：会话身份经 contextvar 传给工具（registry 在克隆间共享，
        # 直接往工具实例上挂 session 会被并发会话互相覆盖）。
        _root = self.config.project_root
        for cls in (FileReadTool, FileWriteTool, FileEditTool):
            self.tool_registry.register(cls(project_root=_root))
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

        **失败不得静默化**（v1.6.4 的核心修正）：每个环节都区分三种结局 ——
        通过 / 未通过 / **设施不可用**。设施不可用时如实标注"未执行"，
        既不伪装成通过（伪造结论），也不伪装成未通过（白烧修复轮 token）。
        """
        ex = self.config.execution
        summary: list[str] = []
        issues: list[str] = []
        #: 某一环"跑都没跑成"时置位 —— 决定摘要措辞与最终的诚实度
        degraded: list[str] = []
        #: 留档：每一环的真实结局（落盘证据里要能看出"这环到底跑没跑"）
        evidence: dict[str, Any] = {"review": None, "verify": None, "tdd": None}

        # ① TDD：编程模式跑项目级测试（若存在 tests/）
        if ex.auto_test and self._interaction == InteractionMode.CODING:
            t = await self._run_project_tests()
            if t is not None:
                if t.get("unavailable"):
                    summary.append("测试未能运行 ⚠")
                    degraded.append("项目测试")
                else:
                    summary.append("测试" + ("通过 ✓" if t["passed"] else "未通过 ✗"))
                    if not t["passed"]:
                        issues.append(f"项目测试未通过：{t['detail'][:600]}")
                evidence["tdd"] = {"passed": t.get("passed"),
                                   "unavailable": bool(t.get("unavailable")),
                                   "detail": str(t.get("detail", ""))[:400]}
                await self._emit({"type": "autopilot", "stage": "tdd",
                                  "passed": t.get("passed"),
                                  "unavailable": bool(t.get("unavailable")),
                                  "detail": str(t.get("detail", ""))[:300]})

        # ② 多 Agent 审查：工作模式由审阅者角色复核（共享只读工具，含 MCP）
        if ex.auto_review and self._interaction == InteractionMode.WORK \
                and self.llm is not None:
            rv = await self._review_result(task, output)
            if rv.get("available"):
                summary.append("审查" + ("通过 ✓" if rv["approved"] else "有意见 ⚠"))
                if not rv["approved"] and rv["issues"]:
                    issues.append("审阅者意见：" + rv["issues"][:600])
            else:
                # 审查设施故障：不能记成"审查通过 ✓"（伪造），
                # 也不能记成"审查未通过"（审查压根没发生）
                summary.append("审查未执行（设施不可用）⚠")
                degraded.append("多 Agent 审查")
            evidence["review"] = {"approved": rv.get("approved"),
                                  "available": bool(rv.get("available")),
                                  "error": str(rv.get("error", ""))[:300],
                                  "issues": str(rv.get("issues", ""))[:400]}
            await self._emit({"type": "autopilot", "stage": "review",
                              "approved": rv.get("approved"),
                              "available": bool(rv.get("available")),
                              "error": str(rv.get("error", ""))[:200],
                              "issues": str(rv.get("issues", ""))[:300]})

        # ③ Loop 验收：产物断言 + 语义判定；未过则带反馈补充修复轮
        if ex.auto_verify and self.llm is not None:
            rounds = 0
            while True:
                verdict = await self._loop_verify(task, output)
                available = bool(verdict.get("available", True))
                done = bool(verdict.get("done")) and not issues
                await self._emit({"type": "autopilot", "stage": "verify", "done": done,
                                  "available": available, "round": rounds,
                                  "source": str(verdict.get("source", ""))[:20],
                                  "reason": str(verdict.get("reason", ""))[:300]})
                evidence["verify"] = {
                    "done": done, "available": available, "rounds": rounds,
                    "source": verdict.get("source"),
                    "reason": str(verdict.get("reason", ""))[:400],
                    "artifacts": verdict.get("artifacts"),
                }
                if done:
                    summary.append("验收通过 ✓")
                    break
                if not available:
                    # 验收设施故障：不进修复轮（拿不到反馈的修复轮纯烧 token，
                    # 而且最后还会谎报"验收未过 ✗"）。如实说明并结束。
                    summary.append("验收未执行（验收设施不可用）⚠")
                    degraded.append("Loop 验收")
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
            if degraded:
                output += (
                    f"\n（说明：{'、'.join(degraded)}环节因设施异常未真正执行，"
                    f"以上结论不含该环节的判断，请勿据此认为已通过该项检查。）")
        evidence["degraded"] = degraded
        evidence["summary"] = summary
        self._verify_state = evidence
        return output

    async def _review_result(self, task: str, output: str) -> dict:
        """多 Agent 审查：审阅者角色复核结果，可调用只读工具核实（MCP 工具共享）。

        返回 ``{"approved": bool, "available": bool, "issues": str, "error": str}``。

        ``available=False`` 表示**审查设施本身没跑成**（模型不可用、返回不是
        JSON、只读核实过程中异常）—— 这与"审阅者看过并认为有问题"是两回事。
        v1.6.3 及更早把所有异常都吞成 ``{"approved": True}``，于是模型一挂、
        网络一抖，闭环摘要是照样一句「审查通过 ✓」：**设施故障被伪装成通过**，
        用户看到的是一个从未发生过的审查结论。
        """
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
            if isinstance(data, dict) and "approved" in data:
                return {"approved": bool(data.get("approved")), "available": True,
                        "issues": str(data.get("issues", ""))[:800], "error": ""}
            detail = "审阅者未按 JSON 格式作答"
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
        logger.warning("autopilot_review_unavailable", error=detail)
        # 不阻断主流程，但**如实标注不可用** —— 调用方据此说"未执行审查"，
        # 而不是把它记成"审查通过"。
        return {"approved": False, "available": False, "issues": "", "error": detail}

    async def _run_project_tests(self) -> dict | None:
        """TDD 收尾：项目存在测试时运行 pytest。

        返回 ``{passed, detail}``；无测试返回 None；**测试设施跑不起来**
        （终端工具缺失/超时/被拒）返回 ``{unavailable: True, detail}`` ——
        v1.6.3 及更早这里 ``except`` 后返回 None，"测试跑不起来"与"项目里
        没有测试"因此在摘要里长得一模一样（都不出声）。现在两者可区分。
        """
        root = Path(self.config.project_root)
        has_tests = (root / "tests").is_dir() or bool(list(root.glob("test_*.py")))
        if not has_tests:
            return None
        timeout = float(getattr(self.config.execution, "tool_timeout_seconds", 300.0))
        try:
            result = await self.tool_registry.dispatch(
                "terminal",
                command="python -m pytest -q --tb=line -x",
                workdir=str(root), timeout=max(180.0, timeout),
            )
            if getattr(result, "timed_out", False):
                return {"passed": False, "unavailable": True,
                        "detail": f"测试超时未完成：{str(result.error)[:300]}"}
            out = ""
            if isinstance(result.output, dict):
                out = (result.output.get("stdout") or "") + "\n" + \
                      (result.output.get("stderr") or "")
            passed = bool(result.success)
            # 提取摘要行（"N passed" / "N failed"）
            tail = "\n".join(line for line in out.strip().splitlines()[-5:] if line.strip())
            return {"passed": passed, "detail": tail[:800] or "（无输出）"}
        except Exception as e:
            logger.warning("autopilot_test_unavailable", error=str(e))
            return {"passed": False, "unavailable": True,
                    "detail": f"无法运行项目测试：{type(e).__name__}: {e}"}


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
