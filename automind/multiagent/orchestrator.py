"""多智能体协同编排器 — 协调者拆解任务，角色化子智能体协作完成，再综合。

设计：
    1. Coordinator（协调者）将任务拆解为带角色的子任务清单。
    2. 各角色子智能体（规划/研究/编程/审阅）依次协作，共享一块"白板"上下文。
    3. Synthesizer（综合者）汇总所有子结果，给出最终答案。

所有 LLM 调用复用传入的 backend（含 token 统计包装），因此 token 会被正确累计。
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from automind.core.json_utils import extract_json


ROLE_PROMPTS: dict[str, str] = {
    "planner": "你是规划专家。把目标拆成清晰、可执行的步骤，指出关键风险与依赖。",
    "researcher": "你是研究与分析专家。基于已知信息进行梳理、推理与事实组织，给出要点。",
    "coder": "你是资深工程师。给出正确、简洁、可运行的代码与必要说明；必要时用 ```代码块。",
    "writer": "你是写作专家。把内容组织成结构清晰、表达流畅的中文成稿。",
    "reviewer": "你是审阅专家。检查正确性、完整性与一致性，明确指出问题并给出改进建议。",
}

ROLE_LABELS = {
    "planner": "🧭 规划", "researcher": "🔎 研究", "coder": "💻 编程",
    "writer": "✍️ 写作", "reviewer": "🧐 审阅",
}


class MultiAgentOrchestrator:
    """多智能体协同编排器。"""

    def __init__(self, llm: Any, max_steps: int = 6) -> None:
        self.llm = llm
        self.max_steps = max_steps

    async def run(
        self,
        task: str,
        context: str = "",
        on_event: Callable[[dict], Awaitable[None]] | None = None,
    ) -> dict:
        """执行多智能体协同。

        Returns:
            {"output": str, "plan": [...], "steps": [...]}。
        """
        async def emit(ev: dict) -> None:
            if on_event:
                try:
                    await on_event(ev)
                except Exception:
                    pass

        # 1. 协调者拆解
        plan = await self._decompose(task, context)
        await emit({"type": "ma_plan", "plan": plan})

        # 2. 角色协作（共享白板）
        scratch = f"原始任务：{task}\n"
        steps: list[dict] = []
        for i, step in enumerate(plan):
            role = step.get("role", "researcher")
            subtask = step.get("subtask", "")
            await emit({"type": "ma_step_start", "index": i, "role": role,
                        "label": ROLE_LABELS.get(role, role), "subtask": subtask})
            output = await self._run_role(role, subtask, task, scratch)
            scratch += f"\n【{ROLE_LABELS.get(role, role)}】{subtask}\n{output}\n"
            rec = {"role": role, "label": ROLE_LABELS.get(role, role),
                   "subtask": subtask, "output": output}
            steps.append(rec)
            await emit({"type": "ma_step_end", "index": i, **rec})

        # 3. 综合
        final = await self._synthesize(task, steps)
        await emit({"type": "ma_done", "output": final})
        return {"output": final, "plan": plan, "steps": steps}

    async def _decompose(self, task: str, context: str) -> list[dict]:
        roles = ", ".join(ROLE_PROMPTS.keys())
        prompt = (
            f"你是多智能体团队的协调者。把下面的任务拆解为 2-{self.max_steps} 个有序子任务，"
            f"每个子任务分配给最合适的角色。可用角色：{roles}。\n\n"
            f"任务：{task}\n"
            + (f"背景：{context[:1500]}\n" if context else "")
            + '\n只输出 JSON 数组，形如：\n'
            '[{"role":"planner","subtask":"..."},{"role":"coder","subtask":"..."}]'
        )
        try:
            resp = await self.llm.generate([{"role": "user", "content": prompt}])
            data = extract_json(resp.text)
            if isinstance(data, list) and data:
                out = []
                for d in data[: self.max_steps]:
                    if isinstance(d, dict) and d.get("subtask"):
                        role = d.get("role", "researcher")
                        if role not in ROLE_PROMPTS:
                            role = "researcher"
                        out.append({"role": role, "subtask": str(d["subtask"])})
                if out:
                    return out
        except Exception:
            pass
        # 降级：通用三角色流水线
        return [
            {"role": "planner", "subtask": f"为「{task}」制定执行方案"},
            {"role": "researcher", "subtask": f"完成「{task}」的核心内容"},
            {"role": "reviewer", "subtask": "审阅上述结果并改进"},
        ]

    async def _run_role(self, role: str, subtask: str, task: str, scratch: str) -> str:
        system = ROLE_PROMPTS.get(role, ROLE_PROMPTS["researcher"])
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": (
                f"团队总目标：{task}\n\n"
                f"截至目前的团队进展（白板）：\n{scratch[-4000:]}\n\n"
                f"你的子任务：{subtask}\n"
                f"请只完成你的子任务，简洁高效，不要重复他人已完成的工作。"
            )},
        ]
        try:
            resp = await self.llm.generate(messages)
            return resp.text or "(无输出)"
        except Exception as e:
            return f"(执行失败: {e})"

    async def _synthesize(self, task: str, steps: list[dict]) -> str:
        joined = "\n\n".join(
            f"【{s['label']}】{s['subtask']}\n{s['output']}" for s in steps
        )
        messages = [
            {"role": "system", "content": "你是团队综合者，把各角色的成果整合成面向用户的最终交付物。"},
            {"role": "user", "content": (
                f"总目标：{task}\n\n各角色成果：\n{joined[:6000]}\n\n"
                f"请综合成一份清晰、完整、可直接交付的中文结果（Markdown）。"
            )},
        ]
        try:
            resp = await self.llm.generate(messages)
            return resp.text or joined
        except Exception:
            return joined
