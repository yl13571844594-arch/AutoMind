"""ReAct 执行器 — Observe → Think → Act → Observe 循环。"""

from __future__ import annotations

from typing import Any

from automind.core.logging import get_logger
from automind.core.types import (
    LLMResponse,
    ToolCall,
    ToolResult,
)
from automind.tools.base import ToolRegistry
from automind.tools.function_calling import FunctionCallHandler

logger = get_logger("automind.react")


class ReActExecutor:
    """ReAct 模式执行器。

    ReAct 循环:
        1. OBSERVE: 读取当前上下文和工具执行结果
        2. THINK: LLM 推理下一步做什么 (生成思考 + 可能的工具调用)
        3. ACT: 执行工具调用
        4. 重复直到 LLM 输出最终答案或达到迭代上限

    配置:
        max_iterations: 最大思考-行动循环数
        stop_on_no_tools: 当 LLM 不再请求工具时停止
    """

    SYSTEM_PROMPT = (
        "You are an AI agent that can use tools to accomplish tasks. "
        "For each step, think about what you need to do, then use the "
        "appropriate tool. After getting tool results, evaluate if you're "
        "done or need to take more actions.\n\n"
        "Workflow:\n"
        "1. Analyze the current state\n"
        "2. Decide what tool to use (if any)\n"
        "3. Interpret the tool result\n"
        "4. Repeat until the task is complete\n"
        "5. Provide a final summary when done"
    )

    def __init__(
        self,
        llm: Any,
        tool_registry: ToolRegistry,
        max_iterations: int = 50,
        stop_on_no_tools: bool = True,
        permissions: Any = None,
        approval_cb: Any = None,
        auto_validate: bool = True,
    ) -> None:
        self.llm = llm
        self.tool_registry = tool_registry
        self.fn_handler = FunctionCallHandler(tool_registry)
        self.max_iterations = max_iterations
        self.stop_on_no_tools = stop_on_no_tools
        self.permissions = permissions
        self.approval_cb = approval_cb  # async (tool_name, args, tier, reason) -> bool
        # TDD 内环：每次代码写入/编辑后自动做语法验证，结果注入观察反馈
        self.auto_validate = auto_validate
        self.thoughts: list[str] = []
        self.actions: list[tuple[ToolCall, ToolResult]] = []
        self.validations: list[dict] = []  # 自动验证记录
        # 工具名 → {streak: 连续失败次数, last: 最后一次原因}
        self._tool_failures: dict[str, dict] = {}

    _CODE_TOOLS = ("file_write", "file_edit", "file_multi_edit")

    def _auto_validate_result(self, tc: ToolCall, result: ToolResult) -> ToolResult:
        """TDD 内环：代码修改成功后立即做语法校验，结论写回观察结果。

        模型在下一轮 OBSERVE 中即可看到 "syntax_check: ..."，
        有错立即修复 —— 形成 编辑 → 验证 → 修复 的自动闭环。
        覆盖 file_write / file_edit / file_multi_edit 产出的全部 .py/.json 文件；
        优先使用工具输出中已解析的绝对路径（参数里的相对路径可能相对项目根）。
        """
        if not (self.auto_validate and result.success and tc.name in self._CODE_TOOLS):
            return result
        checked: list[tuple[str, str]] = []
        for path in self._touched_paths(tc, result):
            note = self._check_syntax(path)
            if note is None:
                continue
            ok = note.startswith("OK")
            self.validations.append({"tool": tc.name, "path": path, "ok": ok,
                                     **({} if ok else {"error": note})})
            checked.append((path, note))
        if checked:
            # 单文件保持简洁格式（syntax_check: OK），多文件带路径前缀
            summary = (checked[0][1] if len(checked) == 1
                       else "; ".join(f"{p}: {n}" for p, n in checked))
            try:
                if isinstance(result.output, dict):
                    result.output["auto_validation"] = f"syntax_check: {summary}"
            except Exception:
                pass
        return result

    @staticmethod
    def _touched_paths(tc: ToolCall, result: ToolResult) -> list[str]:
        """收集本次调用实际写入的文件路径（优先工具输出的解析后路径）。"""
        out = result.output if isinstance(result.output, dict) else {}
        if tc.name == "file_multi_edit":
            paths = []
            for r in out.get("results", []):
                o = r.get("output") if isinstance(r, dict) else None
                if isinstance(o, dict) and o.get("path") and r.get("success"):
                    paths.append(str(o["path"]))
            return paths
        path = out.get("path") or (tc.arguments or {}).get("path", "")
        return [str(path)] if path else []

    @staticmethod
    def _check_syntax(path: str) -> str | None:
        """校验单个文件；返回 "OK" / 错误说明，非目标类型或读不到返回 None。

        覆盖 Python / JSON / YAML / TOML —— Agent 产出最多的四类结构化文件。
        """
        from pathlib import Path as _P
        try:
            if path.endswith(".py"):
                import ast as _ast
                _ast.parse(_P(path).read_text(encoding="utf-8"))
                return "OK"
            if path.endswith(".json"):
                import json as _json
                _json.loads(_P(path).read_text(encoding="utf-8"))
                return "OK"
            if path.endswith((".yaml", ".yml")):
                import yaml as _yaml
                _yaml.safe_load(_P(path).read_text(encoding="utf-8"))
                return "OK"
            if path.endswith(".toml"):
                import tomllib as _toml
                _toml.loads(_P(path).read_text(encoding="utf-8"))
                return "OK"
        except SyntaxError as e:
            return (f"FAILED — {e.msg} (line {e.lineno}). "
                    f"Fix this syntax error before proceeding.")
        except ValueError as e:  # json.JSONDecodeError / tomllib.TOMLDecodeError
            kind = "JSON" if path.endswith(".json") else "TOML"
            return f"FAILED — invalid {kind}: {e}. Fix this before proceeding."
        except ImportError:
            return None  # 校验器依赖缺失（如无 pyyaml）→ 跳过不阻塞
        except Exception as e:
            # yaml.YAMLError 等解析错误也应反馈给模型
            if type(e).__module__.startswith("yaml"):
                return f"FAILED — invalid YAML: {e}. Fix this before proceeding."
            return None  # 文件读不到等情况不干扰主流程
        return None

    async def run(
        self,
        task: str,
        context: str = "",
        on_thought: Any = None,
        on_action: Any = None,
    ) -> str:
        """执行 ReAct 循环。

        Args:
            task: 用户任务。
            context: 额外上下文。
            on_thought: 思考回调 (可选)。
            on_action: 动作回调 (可选)。

        Returns:
            最终答案文本。
        """
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
        ]
        if context:
            messages.append({"role": "system", "content": f"Environment:\n{context}"})
        messages.append({"role": "user", "content": task})

        tool_schemas = self.tool_registry.get_openai_schemas()

        for iteration in range(self.max_iterations):
            # THINK
            response = await self.llm.generate(messages, tools=tool_schemas)

            if response.text:
                self.thoughts.append(response.text)
                if on_thought:
                    await on_thought(response.text)

            # 没有工具调用 → 任务完成
            if not response.tool_calls:
                if self.stop_on_no_tools:
                    return response.text
                # 否则添加 assistant 消息并继续
                messages.append({"role": "assistant", "content": response.text})
                continue

            # ACT — 先做权限门控，再执行
            messages.append(self._assistant_message(response))
            results = []
            for tc in response.tool_calls:
                allowed, deny_reason = await self._gate(tc)
                if not allowed:
                    results.append(ToolResult(
                        tool_name=tc.name, success=False,
                        error=f"操作被拒绝/未批准：{deny_reason}"))
                    continue
                # B-03 修复：工具不存在 / 参数不合法等异常若逸出会整体崩溃 ReAct 循环，
                # 这里兜底为失败结果，让 LLM 据此继续决策而非丢失全部上下文。
                # 熔断：同一工具连续失败 N 次后不再重试。
                # 此前模型会对着同一个坏工具反复调用直到迭代上限耗尽 ——
                # 既烧 token 又拖时间，而失败原因从头到尾没人看见。
                tripped = self._breaker_reason(tc.name)
                if tripped:
                    results.append(ToolResult(
                        tool_name=tc.name, success=False, error=tripped))
                    continue
                try:
                    args = tc.arguments if isinstance(tc.arguments, dict) else {}
                    result = await self.tool_registry.dispatch(tc.name, **args)
                    # TDD 内环：代码修改后立即语法验证并注入观察
                    result = self._auto_validate_result(tc, result)
                    self._record_tool_outcome(tc.name, result.success,
                                              result.error or "")
                    results.append(result)
                except Exception as e:
                    reason = f"{type(e).__name__}: {e}"
                    self._record_tool_outcome(tc.name, False, reason)
                    results.append(ToolResult(
                        tool_name=tc.name, success=False,
                        error=f"工具执行异常：{reason}"))

            for tc, result in zip(response.tool_calls, results):
                self.actions.append((tc, result))
                if on_action:
                    await on_action(tc, result)

            # OBSERVE
            tool_messages = self.fn_handler.tool_results_to_messages(
                response.tool_calls, results
            )
            messages.extend(tool_messages)

        # 达到迭代上限：返回最后一次有意义的思考，避免空泛提示
        if self.thoughts:
            return (self.thoughts[-1] +
                    "\n\n（提示：已达到最大迭代步数，以上为当前进展。）")
        return "已达到最大迭代步数，任务可能尚未完成。"

    #: 同一工具连续失败达到此次数即熔断，本轮任务内不再调用
    FAILURE_THRESHOLD = 3

    def _record_tool_outcome(self, name: str, success: bool, error: str) -> None:
        """记录一次工具调用结果；连续失败到阈值即熔断并留痕。"""
        st = self._tool_failures.setdefault(name, {"streak": 0, "last": ""})
        if success:
            st["streak"] = 0
            st["last"] = ""
            return
        st["streak"] += 1
        st["last"] = (error or "")[:300]
        logger.warning("tool_call_failed", tool=name,
                       streak=st["streak"], error=st["last"])
        if st["streak"] == self.FAILURE_THRESHOLD:
            logger.error("tool_circuit_open", tool=name,
                         threshold=self.FAILURE_THRESHOLD, last_error=st["last"])

    def _breaker_reason(self, name: str) -> str:
        """熔断已触发时返回给模型看的说明，否则返回空串。"""
        st = self._tool_failures.get(name)
        if not st or st["streak"] < self.FAILURE_THRESHOLD:
            return ""
        return (f"工具「{name}」已连续失败 {st['streak']} 次，已停止调用以免空转。"
                f"最后一次的失败原因：{st['last']}。"
                f"请改用其它方式完成该步骤，或先修复该工具的配置。")

    def tool_failure_report(self) -> dict[str, dict]:
        """本轮各工具的失败情况（供上层推送给前端标红）。"""
        return {k: dict(v) for k, v in self._tool_failures.items() if v["streak"]}

    async def _gate(self, tc: ToolCall) -> tuple[bool, str]:
        """工具调用前的权限门控。返回 (是否允许, 原因)。"""
        if self.permissions is None:
            return True, ""
        try:
            tool = self.tool_registry.get(tc.name)
            tier = tool.permission_tier
        except Exception:
            from automind.core.types import PermissionTier
            tier = PermissionTier.SENSITIVE
        decision, reason = self.permissions.check(tc.name, tier, tc.arguments)
        if decision.value == "allow":
            return True, reason
        if decision.value == "deny":
            return False, reason
        # ask_user —— 走到这里说明**权限策略明确要求人工确认**
        # （"询问"模式下的非只读操作，或"自动"模式下的高危操作）。
        #
        # 安全修复（v1.4.5）：这两条分支此前都 `return True`：
        #   · 没有审批通道就直接放行 —— 注释写的是"自主运行不阻塞"，但真正表达
        #     "我要自主运行"的方式是把 approval_mode 设成 auto/approve_all，
        #     那样 permissions.check() 根本不会返回 ask_user，压根到不了这里。
        #     靠"回调恰好没接"来放行，等于让配置疏漏静默变成放权。
        #   · 回调抛异常就直接放行 —— 而最常见的异常正是前端断开。
        # 审批是安全控制，两种情形一律 fail-closed：问不到人 = 没批准。
        if self.approval_cb is None:
            return False, (f"{reason}；当前没有可用的审批通道，已按拒绝处理"
                           "（若需无人值守运行，请将审批模式设为「自动」或「全批准」）")
        try:
            from automind.state.human_loop import ApprovalOutcome
            outcome = ApprovalOutcome.normalize(
                await self.approval_cb(tc.name, tc.arguments, tier.value, reason))
            if outcome.approved and outcome.modified:
                # 「修改后批准」：就地替换本次调用的参数。必须改 tc.arguments
                # 本身 —— 后续真正执行工具时读的是它。
                logger.info("approval_modified", tool=tc.name,
                            keys=sorted(outcome.arguments or {}))
                tc.arguments = dict(outcome.arguments or {})
                return True, f"{reason}；用户修改参数后批准"
            return outcome.approved, (
                reason if outcome.approved
                else f"用户拒绝：{outcome.comment or reason}")
        except Exception as e:
            logger.warning("react_approval_failed", tool=tc.name,
                           error=str(e), decision="denied")
            return False, f"审批通道异常（{type(e).__name__}），已按拒绝处理"

    def get_trace(self) -> str:
        """获取执行跟踪。"""
        lines = ["ReAct Execution Trace:", "=" * 50]
        for i, thought in enumerate(self.thoughts):
            lines.append(f"\n[Think {i + 1}]")
            lines.append(thought[:500])
            if i < len(self.actions):
                tc, result = self.actions[i]
                status = "OK" if result.success else "FAIL"
                lines.append(f"[Action {i + 1}] {status}: {tc.name}(...)")
                output = str(result.output)[:200] if result.success else str(result.error)[:200]
                lines.append(f"  → {output}")
        return "\n".join(lines)

    @staticmethod
    def _assistant_message(response: LLMResponse) -> dict[str, Any]:
        """构建包含工具调用的 assistant 消息。"""
        msg: dict[str, Any] = {
            "role": "assistant",
            "content": response.text or None,
        }
        if response.tool_calls:
            import json
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        # OpenAI 兼容接口要求 arguments 为合法 JSON 字符串
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in response.tool_calls
            ]
        return msg
