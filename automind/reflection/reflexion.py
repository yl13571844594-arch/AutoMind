"""Reflexion 机制 — 自我批评与经验学习。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Reflection:
    """一次反思/经验。"""

    task: str
    outcome: str  # success / partial / failure
    self_criticism: str
    mistakes: list[str] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    context: str = ""
    timestamp: float = 0.0
    embedding: list[float] | None = None


class ReflexionEngine:
    """Reflexion 引擎 — 生成自我批评并存入长期记忆。

    核心循环:
        1. 评估结果 (通过 QualityAssessor)
        2. 生成自我批评 (self-criticism)
        3. 提取教训 (lessons learned)
        4. 存入长期记忆 (LongTermMemory / ChromaDB)
        5. 后续任务检索相关经验以改进规划
    """

    def __init__(self, llm: Any = None, long_term_memory: Any = None) -> None:
        self.llm = llm
        self.long_term_memory = long_term_memory
        self.reflections: list[Reflection] = []

    async def reflect(
        self,
        task: str,
        outcome: str,
        execution_trace: str,
        quality_report: Any = None,
    ) -> Reflection:
        """对一次执行进行反思。

        Args:
            task: 任务描述。
            outcome: 结果 (success/partial/failure)。
            execution_trace: 执行过程描述。
            quality_report: 质量评估报告 (可选)。

        Returns:
            Reflection 对象。
        """
        if self.llm is None:
            reflection = self._simple_reflection(task, outcome, execution_trace)
        else:
            reflection = await self._llm_reflection(task, outcome, execution_trace, quality_report)

        import time
        reflection.timestamp = time.time()
        self.reflections.append(reflection)

        # 存入长期记忆
        if self.long_term_memory:
            try:
                await self.long_term_memory.add(
                    documents=[reflection.self_criticism],
                    metadatas=[{
                        "type": "reflection",
                        "task": task,
                        "outcome": outcome,
                    }],
                )
            except Exception:
                pass

        return reflection

    async def retrieve_relevant(self, task: str, k: int = 3) -> list[Reflection]:
        """检索与当前任务相关的历史反思。

        Args:
            task: 当前任务描述。
            k: 返回数量。

        Returns:
            相关性排序的反思列表。
        """
        if self.long_term_memory is None:
            # 简单关键词匹配
            return self._keyword_search(task, k)

        try:
            results = await self.long_term_memory.search(
                task, k=k,
                filter_metadata={"type": "reflection"},
            )
            # 匹配 Reflections
            matched = []
            for r in results:
                for ref in self.reflections:
                    if ref.self_criticism[:50] in r.get("document", ""):
                        matched.append(ref)
                        break
            return matched[:k]
        except Exception:
            return self._keyword_search(task, k)

    def get_lessons_prompt(self, task: str) -> str:
        """生成可注入系统提示的经验教训。"""
        relevant = self._keyword_search(task, 3)
        if not relevant:
            return ""

        parts = ["\n[Lessons from previous similar tasks]\n"]
        for ref in relevant:
            parts.append(f"- Task: {ref.task[:100]}")
            for lesson in ref.lessons[:2]:
                parts.append(f"  Lesson: {lesson}")
        return "\n".join(parts)

    # ── 内部方法 ──────────────────────────────────────────

    async def _llm_reflection(
        self,
        task: str,
        outcome: str,
        trace: str,
        quality_report: Any = None,
    ) -> Reflection:
        quality_text = ""
        if quality_report:
            quality_text = (
                f"Completeness: {quality_report.completeness}, "
                f"Correctness: {quality_report.correctness}, "
                f"Issues: {quality_report.issues}"
            )

        prompt = (
            f"Reflect on the following task execution.\n\n"
            f"Task: {task}\n"
            f"Outcome: {outcome}\n"
            f"Execution trace: {trace[:2000]}\n"
            f"{quality_text}\n\n"
            f"Provide:\n"
            f"1. Self-criticism: What went wrong? What could be improved?\n"
            f"2. Mistakes: List specific mistakes made.\n"
            f"3. Lessons: What should be done differently next time?\n\n"
            f'Return as JSON:\n'
            f'{{"self_criticism": "...", "mistakes": [...], "lessons": [...]}}'
        )
        try:
            response = await self.llm.generate([{"role": "user", "content": prompt}])
            import json
            data = json.loads(response.text)
        except Exception:
            return self._simple_reflection(task, outcome, trace)

        return Reflection(
            task=task,
            outcome=outcome,
            self_criticism=data.get("self_criticism", ""),
            mistakes=data.get("mistakes", []),
            lessons=data.get("lessons", []),
            context=trace[:500],
        )

    @staticmethod
    def _simple_reflection(task: str, outcome: str, trace: str) -> Reflection:
        return Reflection(
            task=task,
            outcome=outcome,
            self_criticism=f"Task outcome: {outcome}. Review the execution trace for details.",
            mistakes=[],
            lessons=["Review errors carefully before retrying"],
            context=trace[:500],
        )

    def _keyword_search(self, task: str, k: int) -> list[Reflection]:
        """简单的关键词搜索。"""
        task_words = set(task.lower().split())
        scored = []
        for ref in self.reflections:
            ref_words = set(ref.task.lower().split())
            score = len(task_words & ref_words)
            if score > 0:
                scored.append((score, ref))
        scored.sort(key=lambda x: -x[0])
        return [ref for _, ref in scored[:k]]
