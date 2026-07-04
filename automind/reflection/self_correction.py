"""自我纠错 — 错误分析 → 生成修正 → 重新执行循环。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from automind.core.types import ToolResult


@dataclass
class CorrectionRecord:
    """一次纠错记录。"""

    iteration: int
    error: str
    analysis: str
    fix_description: str
    success: bool
    tool_result: ToolResult | None = None


@dataclass
class CorrectionResult:
    """完整纠错结果。"""

    original_error: str
    fixed: bool
    iterations: int
    records: list[CorrectionRecord] = field(default_factory=list)
    final_result: ToolResult | None = None
    final_analysis: str = ""


class SelfCorrectionLoop:
    """自我纠错循环。

    流程:
        1. 捕获工具执行错误
        2. LLM 分析错误原因
        3. 生成修正方案
        4. 重新执行
        5. 验证结果
        6. 重复直到成功或达到上限

    限制:
        max_iterations: 最大重试次数 (默认 3)
        backoff_seconds: 每次重试的基础等待时间
    """

    MAX_ITERATIONS = 3

    def __init__(self, llm: Any = None, max_iterations: int = 3) -> None:
        self.llm = llm
        self.max_iterations = max_iterations if max_iterations > 0 else self.MAX_ITERATIONS
        self.history: list[CorrectionResult] = []

    async def correct(
        self,
        tool_name: str,
        original_params: dict[str, Any],
        error_message: str,
        tool_executor: Any,  # async callable(**params) → ToolResult
        context: str = "",
    ) -> CorrectionResult:
        """尝试自我纠错。

        Args:
            tool_name: 失败的工具名称。
            original_params: 原始参数。
            error_message: 错误消息。
            tool_executor: 重新执行的函数 (async (**params) → ToolResult)。
            context: 额外上下文。

        Returns:
            CorrectionResult。
        """
        result = CorrectionResult(original_error=error_message)
        current_params = dict(original_params)

        for iteration in range(self.max_iterations):
            # 分析错误
            analysis = await self._analyze_error(
                tool_name, current_params, error_message, context, iteration
            )

            # 生成修正
            fix = await self._generate_fix(
                tool_name, current_params, error_message, analysis, iteration
            )

            record = CorrectionRecord(
                iteration=iteration + 1,
                # B-07 修复：每轮都记录当轮真实错误，便于回溯多次修正过程。
                error=error_message,
                analysis=analysis,
                fix_description=fix.get("description", ""),
                success=False,
            )
            result.records.append(record)

            if not fix.get("actionable", True):
                # LLM 判断不可自动修复
                result.final_analysis = analysis
                break

            # 应用修正并重新执行
            corrected_params = fix.get("params", current_params)
            try:
                tool_result = await tool_executor(**corrected_params)
                record.tool_result = tool_result

                if tool_result.success:
                    record.success = True
                    result.fixed = True
                    result.iterations = iteration + 1
                    result.final_result = tool_result
                    result.final_analysis = analysis
                    break

                # 更新参数用于下一次尝试
                current_params = corrected_params
                error_message = tool_result.error or "Unknown error"

            except Exception as e:
                error_message = str(e)

        self.history.append(result)
        return result

    async def _analyze_error(
        self,
        tool_name: str,
        params: dict[str, Any],
        error: str,
        context: str,
        iteration: int,
    ) -> str:
        """分析错误原因。"""
        if self.llm is None:
            return f"Tool '{tool_name}' failed with: {error}"

        prompt = (
            f"A tool execution failed. Analyze the root cause.\n\n"
            f"Tool: {tool_name}\n"
            f"Parameters: {params}\n"
            f"Error: {error}\n"
            f"Context: {context}\n"
            f"Attempt: {iteration + 1}/{self.max_iterations}\n\n"
            f"What caused this error? Provide a brief root cause analysis."
        )
        try:
            response = await self.llm.generate([{"role": "user", "content": prompt}])
            return response.text.strip()
        except Exception:
            return f"Error: {error}"

    async def _generate_fix(
        self,
        tool_name: str,
        params: dict[str, Any],
        error: str,
        analysis: str,
        iteration: int,
    ) -> dict[str, Any]:
        """生成修正方案。"""
        if self.llm is None:
            return {"actionable": False, "description": "No LLM available for fix generation"}

        prompt = (
            f"Based on the error analysis, propose a fix.\n\n"
            f"Tool: {tool_name}\n"
            f"Original params: {params}\n"
            f"Error: {error}\n"
            f"Analysis: {analysis}\n\n"
            f"Provide corrected parameters as JSON:\n"
            f'{{"actionable": true, "description": "...", "params": {{...}}}}\n'
            f"If the error cannot be automatically fixed, set actionable to false."
        )
        try:
            response = await self.llm.generate([{"role": "user", "content": prompt}])
            import json
            return json.loads(response.text)
        except Exception:
            return {"actionable": False, "description": "Failed to generate fix"}
