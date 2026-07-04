"""质量评估器 — LLM-as-judge 检查完整性、幻觉、正确性。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class QualityReport:
    """质量评估报告。"""

    completeness: float  # 0-1: 任务是否完全完成
    correctness: float  # 0-1: 结果是否正确
    consistency: float  # 0-1: 与上下文是否一致
    hallucination_score: float  # 0-1: 幻觉风险 (低 → 好)
    issues: list[str]
    suggestions: list[str]
    overall_pass: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "completeness": self.completeness,
            "correctness": self.correctness,
            "consistency": self.consistency,
            "hallucination_score": self.hallucination_score,
            "issues": self.issues,
            "suggestions": self.suggestions,
            "overall_pass": self.overall_pass,
        }


class QualityAssessor:
    """质量评估器 — 使用 LLM 评估输出质量。

    评估维度:
        1. 完整性 — 任务是否完全完成
        2. 正确性 — 结果是否正确
        3. 一致性 — 与上下文和历史是否一致
        4. 幻觉检测 — 是否有编造的内容
    """

    PASS_THRESHOLD = 0.7

    def __init__(self, llm: Any = None) -> None:
        self.llm = llm
        self.history: list[QualityReport] = []

    async def evaluate(
        self,
        task: str,
        result: str,
        context: str = "",
        expected_output: str = "",
    ) -> QualityReport:
        """评估执行结果质量。

        Args:
            task: 原始任务。
            result: Agent 输出。
            context: 上下文信息。
            expected_output: 期望输出 (可选，用于精确匹配检查)。

        Returns:
            QualityReport。
        """
        if self.llm is None:
            return self._simple_evaluate(task, result, context, expected_output)

        prompt = (
            f"Evaluate the quality of an AI agent's output.\n\n"
            f"Task: {task}\n\n"
            f"Agent Output:\n{result[:3000]}\n\n"
            f"{'Expected Output (for reference): ' + expected_output if expected_output else ''}"
            f"{'Context: ' + context if context else ''}\n\n"
            f"Rate each dimension from 0.0 to 1.0:\n"
            f"1. Completeness — Did it fully complete the task?\n"
            f"2. Correctness — Is the result accurate and correct?\n"
            f"3. Consistency — Is it consistent with provided context?\n"
            f"4. Hallucination — Did it fabricate any information? (0=no hallucination)\n\n"
            f"List any issues found and suggestions for improvement.\n\n"
            f'Return as JSON:\n'
            f'{{"completeness": 0.0, "correctness": 0.0, "consistency": 0.0, '
            f'"hallucination_score": 0.0, "issues": [], "suggestions": []}}'
        )
        try:
            response = await self.llm.generate([{"role": "user", "content": prompt}])
            from automind.core.json_utils import extract_json
            data = extract_json(response.text)
            if not isinstance(data, dict):
                return self._simple_evaluate(task, result, context, expected_output)
        except Exception:
            return self._simple_evaluate(task, result, context, expected_output)

        report = QualityReport(
            completeness=data.get("completeness", 0.5),
            correctness=data.get("correctness", 0.5),
            consistency=data.get("consistency", 0.5),
            hallucination_score=data.get("hallucination_score", 0.5),
            issues=data.get("issues", []),
            suggestions=data.get("suggestions", []),
            overall_pass=(
                data.get("completeness", 0) > self.PASS_THRESHOLD
                and data.get("correctness", 0) > self.PASS_THRESHOLD
            ),
        )
        self.history.append(report)
        return report

    @staticmethod
    def _simple_evaluate(
        task: str, result: str, context: str = "", expected_output: str = ""
    ) -> QualityReport:
        """简单规则评估 (无 LLM 降级方案)。"""
        issues = []
        completeness = 1.0 if result else 0.0
        if len(result) < 10:
            issues.append("Output is very short — may be incomplete")
            completeness = 0.3

        # 检查常见错误标记
        error_markers = ["Error:", "Traceback", "Failed:", "Exception"]
        correctness = 1.0
        for marker in error_markers:
            if marker in result:
                issues.append(f"Found error marker: {marker}")
                correctness -= 0.2

        correctness = max(0.0, correctness)

        return QualityReport(
            completeness=completeness,
            correctness=correctness,
            consistency=0.8,
            hallucination_score=0.3,
            issues=issues,
            suggestions=["Consider using LLM-based evaluation for better accuracy"],
            overall_pass=completeness > 0.5 and correctness > 0.5,
        )
