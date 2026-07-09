"""推理引擎 — CoT (思维链)、ToT (思维树) 结构化推理。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ThoughtNode:
    """思维树节点。"""

    id: str
    content: str
    score: float = 0.0
    parent_id: str | None = None
    children: list[ThoughtNode] = field(default_factory=list)
    is_complete: bool = False
    evaluation: str = ""


class ReasoningEngine:
    """结构化推理引擎。

    提供:
        - Chain of Thought (CoT): 逐步推理
        - Tree of Thoughts (ToT): 广度优先 / 束搜索思维扩展
        - 思维评估与剪枝
    """

    def __init__(self, llm: Any = None) -> None:
        self.llm = llm
        # B-10 修复：id → 节点 映射，供 _get_path 沿 parent_id 回溯完整推理链。
        self._node_registry: dict[str, ThoughtNode] = {}

    # ── Chain of Thought ──────────────────────────────────

    async def cot(
        self,
        problem: str,
        context: str = "",
        steps_hint: int | None = None,
    ) -> str:
        """Chain of Thought — 生成逐步推理链。

        Args:
            problem: 问题描述。
            context: 额外上下文。
            steps_hint: 期望的步骤数量 (可选)。

        Returns:
            完整的推理过程文本。
        """
        if self.llm is None:
            return self._cot_template(problem, context)

        prompt = self._build_cot_prompt(problem, context, steps_hint)
        response = await self.llm.generate([
            {"role": "user", "content": prompt}
        ])
        return response.text

    # ── Tree of Thoughts ──────────────────────────────────

    async def tot(
        self,
        problem: str,
        beam_width: int = 3,
        max_depth: int = 5,
        evaluation_criteria: str = "",
    ) -> ThoughtNode:
        """Tree of Thoughts — 束搜索思维扩展。

        Args:
            problem: 问题描述。
            beam_width: 每层保留的最佳思维数量。
            max_depth: 最大搜索深度。
            evaluation_criteria: 评估标准文本。

        Returns:
            最佳思维路径的根节点 (通过 parent_id 可追溯完整链)。
        """
        root = ThoughtNode(id="root", content=problem)
        # 重置并登记根节点，便于后续按 parent_id 追溯路径
        self._node_registry = {root.id: root}

        if self.llm is None:
            return self._tot_template(problem, beam_width, max_depth)

        current_level = [root]

        for depth in range(max_depth):
            next_level = []

            for node in current_level:
                if node.is_complete:
                    next_level.append(node)
                    continue

                # 生成可能的下一步思维
                branches = await self._generate_thoughts(node, problem, beam_width)

                # 评估每个分支
                eval_tasks = [
                    self.evaluate_thought(branch, evaluation_criteria)
                    for branch in branches
                ]
                scores = await asyncio.gather(*eval_tasks)

                for branch, score in zip(branches, scores):
                    branch.score = score
                    node.children.append(branch)
                    self._node_registry[branch.id] = branch
                    next_level.append(branch)

            # 束搜索：只保留最佳 beam_width 个
            next_level.sort(key=lambda n: n.score, reverse=True)
            current_level = next_level[:beam_width]

            # 如果所有节点都完成，提前结束
            if all(n.is_complete for n in current_level):
                break

        # 返回最佳路径的根
        best = max(current_level, key=lambda n: n.score) if current_level else root
        return best

    async def evaluate_thought(self, thought: ThoughtNode, criteria: str = "") -> float:
        """评估一个思维节点的质量 (0-1)。"""
        if not criteria:
            criteria = "relevance, feasibility, specificity"

        if self.llm is None:
            return 0.5

        prompt = (
            f"Evaluate this thought on a scale of 0.0 to 1.0 based on: {criteria}\n\n"
            f"Thought: {thought.content}\n\n"
            f"Return ONLY a number between 0.0 and 1.0."
        )
        try:
            response = await self.llm.generate([{"role": "user", "content": prompt}])
            return float(response.text.strip())
        except (ValueError, Exception):
            return 0.5

    # ── 辅助方法 ──────────────────────────────────────────

    async def _generate_thoughts(
        self, node: ThoughtNode, problem: str, n: int
    ) -> list[ThoughtNode]:
        """为一个思维节点生成 n 个可能的下一步思维。"""
        prompt = (
            f"Problem: {problem}\n\n"
            f"Current thinking path:\n{self._get_path(node)}\n\n"
            f"Generate {n} possible next thinking steps. Each should be a distinct "
            f"and meaningful progression. Format as a numbered list."
        )
        response = await self.llm.generate([{"role": "user", "content": prompt}])
        thoughts = []
        import uuid
        for line in response.text.strip().split("\n"):
            line = line.strip()
            if line and (line[0].isdigit() or line.startswith("-")):
                # 移除编号
                content = line.lstrip("0123456789. -)").strip()
                if content:
                    thoughts.append(ThoughtNode(
                        id=uuid.uuid4().hex[:8],
                        content=content,
                        parent_id=node.id,
                    ))
        # 确保至少有 n 个
        while len(thoughts) < n:
            thoughts.append(ThoughtNode(
                id=uuid.uuid4().hex[:8],
                content="Alternative approach: consider the problem from a different angle",
                parent_id=node.id,
            ))
        return thoughts[:n]

    def _get_path(self, node: ThoughtNode) -> str:
        """B-10 修复：沿 parent_id 从当前节点回溯至根，返回完整推理链。"""
        parts: list[str] = []
        current: ThoughtNode | None = node
        seen: set[str] = set()
        while current is not None and current.id not in seen:
            parts.append(current.content)
            seen.add(current.id)
            if current.parent_id is None:
                break
            current = self._node_registry.get(current.parent_id)
        return " → ".join(reversed(parts))

    @staticmethod
    def _build_cot_prompt(problem: str, context: str, steps_hint: int | None) -> str:
        parts = [
            "Please think step by step to solve the following problem.",
            f"Problem: {problem}",
        ]
        if context:
            parts.append(f"Context: {context}")
        if steps_hint:
            parts.append(f"Provide your reasoning in {steps_hint} clear steps.")
        else:
            parts.append("Provide your reasoning in clear, logical steps.")
        return "\n\n".join(parts)

    @staticmethod
    def _cot_template(problem: str, context: str) -> str:
        """无 LLM 时的 CoT 模板 (降级)。"""
        return (
            f"Problem: {problem}\n"
            f"Context: {context}\n\n"
            f"Step 1: Analyze the requirements\n"
            f"Step 2: Identify dependencies and constraints\n"
            f"Step 3: Design the solution approach\n"
            f"Step 4: Break down into executable tasks\n"
            f"Step 5: Define verification criteria"
        )

    @staticmethod
    def _tot_template(problem: str, beam_width: int, max_depth: int) -> ThoughtNode:
        """无 LLM 时的 ToT 模板 (降级)。"""
        root = ThoughtNode(id="root", content=problem)
        current = root
        for i in range(min(max_depth, 3)):
            child = ThoughtNode(
                id=f"thought_{i}",
                content=f"Step {i+1}: Consider the {'requirements' if i==0 else 'approach' if i==1 else 'verification'}",
                parent_id=current.id,
                score=0.5,
            )
            current.children.append(child)
            current = child
        return root
