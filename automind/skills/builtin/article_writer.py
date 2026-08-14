"""文章写作技能 —— 面向公众号 / 小红书等平台的风格化写作。

直接调用 Agent 的 LLM 后端（``agent.llm.generate``）生成成文，按平台风格
（标题党 / 干货 / 种草笔记等）套用不同提示词，并做一次自我审校：让模型
再读一遍给出可改进点，附在文末供人参考。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from automind.skills.skill_base import AbstractSkill, SkillResult


class ArticleWriterInput(BaseModel):
    topic: str
    style: str = "通用"  # 公众号 | 小红书 | 新闻稿 | 通用
    words: int = 600
    audience: str = ""


class ArticleWriterSkill(AbstractSkill):
    """按平台风格撰写文章。"""

    name = "article_writer"
    description = "Write a styled article (公众号/小红书/news) via the LLM backend"

    async def execute(self, input_data: Any, agent: Any = None) -> SkillResult:
        inp = ArticleWriterInput(**input_data) if isinstance(input_data, dict) else input_data
        llm = getattr(agent, "llm", None) if agent is not None else None
        if llm is None or not hasattr(llm, "generate"):
            return SkillResult(success=False, error="article_writer 需要已配置的 LLM 后端")

        style_guide = {
            "公众号": "公众号深度文：观点鲜明、分点论述、段落简短、金句收尾，可用小标题。",
            "小红书": "小红书种草/干货笔记：口语化、emoji 点缀、短句分行、结尾加 3-5 个话题标签。",
            "新闻稿": "新闻通稿：倒金字塔结构，导语一句话说清 5W1H，正文客观中立。",
            "通用": "结构清晰的中文文章：开头点题、分点展开、结尾总结。",
        }.get(inp.style, "结构清晰的中文文章")

        audience = f"目标读者：{inp.audience}。" if inp.audience else ""
        try:
            messages = [
                {"role": "system",
                 "content": "你是资深中文内容创作者，输出直接、可发布的成文，不解释创作过程。"},
                {"role": "user",
                 "content": f"主题：{inp.topic}\n风格要求：{style_guide}\n"
                            f"{audience}字数约 {inp.words} 字，用 Markdown 排版。"},
            ]
            resp = await llm.generate(messages)
            article = (resp.text or "").strip()
            if not article:
                return SkillResult(success=False, error="LLM 未返回内容")

            # 自我审校：让模型给一句改进建议（失败不影响正文交付）
            review = ""
            try:
                r2 = await llm.generate([
                    {"role": "system", "content": "你是严格的编辑，只给一条最关键的改进建议，不超过 40 字。"},
                    {"role": "user", "content": f"审校下面文章：\n{article[:2000]}"},
                ])
                review = (r2.text or "").strip()
            except Exception:
                review = ""

            output = article + (f"\n\n---\n_编辑建议：{review}_" if review else "")
            return SkillResult(success=True, output=output,
                               metadata={"style": inp.style, "words": len(article)})
        except Exception as e:
            return SkillResult(success=False, error=str(e))
