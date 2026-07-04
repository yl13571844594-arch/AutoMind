"""技能基类 — 可复用的工作流封装模块。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class SkillResult(BaseModel):
    """技能执行结果。"""

    success: bool
    output: Any = None
    error: str = ""
    artifacts: list[str] = []  # 生成的文件路径等
    metadata: dict[str, Any] = {}


class AbstractSkill(ABC):
    """技能抽象基类。

    技能是可复用的工作流封装，类似 Claude Code 的 /skill 机制。
    每个技能包含:
        - name: 唯一标识符 (如 "project_init")
        - description: 描述
        - input_schema: Pydantic 输入模型
        - execute(): 核心执行逻辑
    """

    name: str = ""
    description: str = ""
    input_schema: type[BaseModel] | None = None

    @abstractmethod
    async def execute(self, input_data: Any, agent: Any = None) -> SkillResult:
        """执行技能。

        Args:
            input_data: 输入数据 (应符合 input_schema)。
            agent: AutoMindAgent 实例 (可选，用于访问 LLM/工具)。

        Returns:
            SkillResult。
        """
        ...

    async def dry_run(self, input_data: Any) -> str:
        """干运行 — 预览将执行的操作而不实际执行。"""
        return f"[{self.name}] Would execute: {self.description}\n  Input: {input_data}"

    def to_dict(self) -> dict[str, Any]:
        """转为元数据字典。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema.model_json_schema() if self.input_schema else None,
        }
