"""ReAct 执行器 — Observe → Think → Act → Observe 循环。"""

from __future__ import annotations

from typing import Any

from automind.core.types import (
    LLMResponse,
    ToolCall,
    ToolResult,
)
from automind.tools.base import ToolRegistry
from automind.tools.function_calling import FunctionCallHandler


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
                try:
                    args = tc.arguments if isinstance(tc.arguments, dict) else {}
                    result = await self.tool_registry.dispatch(tc.name, **args)
                    # TDD 内环：代码修改后立即语法验证并注入观察
                    results.append(self._auto_validate_result(tc, result))
                except Exception as e:
                    results.append(ToolResult(
                        tool_name=tc.name, success=False,
                        error=f"工具执行异常：{type(e).__name__}: {e}"))

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
        # ask_user
        if self.approval_cb is None:
            return True, reason  # 无审批通道时不阻塞（自主运行）
        try:
            approved = await self.approval_cb(tc.name, tc.arguments, tier.value, reason)
            return bool(approved), reason if approved else f"用户拒绝：{reason}"
        except Exception:
            return True, reason

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
