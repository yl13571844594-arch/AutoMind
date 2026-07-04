"""示例技能 — 统计文本词数/字符数。

导入方式：Web 工作台「✨ 技能」→「📄 导入 .py」选择本文件。
本地验证：python examples/03-skill-development/word_count_skill.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

# 支持从仓库任意位置直接运行本脚本（未 pip install 时回退到仓库根导入）
try:
    from automind.skills.skill_base import AbstractSkill, SkillResult
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from automind.skills.skill_base import AbstractSkill, SkillResult


class WordCountInput(BaseModel):
    """输入：待统计的文本。"""

    text: str


class WordCountSkill(AbstractSkill):
    """统计文本的词数、字符数与行数。"""

    name = "word_count"
    description = "Count words, characters and lines in a text."
    input_schema = WordCountInput

    async def execute(self, input_data: Any, agent: Any = None) -> SkillResult:  # noqa: ARG002
        inp = WordCountInput(**input_data) if isinstance(input_data, dict) else input_data
        text = inp.text
        return SkillResult(
            success=True,
            output={
                "words": len(text.split()),
                "chars": len(text),
                "lines": text.count("\n") + 1 if text else 0,
            },
        )


if __name__ == "__main__":
    import asyncio

    async def _demo() -> None:
        result = await WordCountSkill().execute({"text": "hello automind skill demo text"})
        print("SkillResult", f"success={result.success}",
              f"words={result.output['words']}", f"chars={result.output['chars']}")

    asyncio.run(_demo())
