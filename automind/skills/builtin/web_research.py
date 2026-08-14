"""网络调研技能 —— 多来源搜索 + 抓取正文，汇总成带引用的调研报告。

编排 ``web_search`` / ``web_fetch`` 两个工具：先搜索、再抓取若干来源正文，
最后拼成一份带来源 URL 的 Markdown 报告。搜索服务需先配好
``AUTOMIND_SEARCH_PROVIDER`` 等环境变量（见 web_search 工具说明）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from automind.skills.skill_base import AbstractSkill, SkillResult


class WebResearchInput(BaseModel):
    query: str
    output: str = ""  # 报告输出路径（.md，留空则只返回文本）
    sources: int = 3  # 抓取正文的来源数量
    lang: str = "zh-CN"


class WebResearchSkill(AbstractSkill):
    """多来源搜索并抓取正文，输出带引用的调研报告。"""

    name = "web_research"
    description = "Search the web across multiple sources and compile a cited research report"

    async def execute(self, input_data: Any, agent: Any = None) -> SkillResult:
        inp = WebResearchInput(**input_data) if isinstance(input_data, dict) else input_data
        if agent is None or getattr(agent, "tool_registry", None) is None:
            return SkillResult(success=False, error="web_research 需要 Agent 上下文以编排 web_search / web_fetch")
        registry = agent.tool_registry

        try:
            # 1) 搜索
            sr = await registry.dispatch("web_search", query=inp.query,
                                         max_results=10, lang=inp.lang)
            results = (sr.output or {}).get("results") if sr.success else []
            if not results:
                return SkillResult(success=False,
                                   error=f"搜索未返回结果：{sr.error or '请检查搜索服务配置'}")

            # 2) 抓取正文（限制来源数，失败跳过）
            sources: list[dict[str, str]] = []
            for item in results[: max(inp.sources, 1)]:
                url = (item or {}).get("url") or ""
                if not url:
                    continue
                fr = await registry.dispatch("web_fetch", url=url, max_chars=4000)
                if fr.success:
                    sources.append({
                        "title": (item or {}).get("title") or (fr.output or {}).get("title") or url,
                        "url": url,
                        "text": ((fr.output or {}).get("text") or "").strip(),
                    })

            report = self._build(inp.query, results, sources)
            artifacts: list[str] = []
            if inp.output:
                out = Path(inp.output).expanduser()
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(report, encoding="utf-8")
                artifacts.append(str(out))
            return SkillResult(success=True, output=report, artifacts=artifacts,
                               metadata={"searched": len(results), "fetched": len(sources)})
        except Exception as e:
            return SkillResult(success=False, error=str(e))

    @staticmethod
    def _build(query: str, results: list[Any], sources: list[dict[str, str]]) -> str:
        lines = [f"# 调研报告：{query}", "", f"抓取来源：{len(sources)} 个", ""]
        for i, s in enumerate(sources, 1):
            lines.append(f"## 来源 {i}：{s['title']}")
            lines.append(f"[{s['url']}]({s['url']})")
            lines.append("")
            lines.append(s["text"][:2000])
            lines.append("")
        lines.append("## 检索结果快照")
        for r in results:
            lines.append(f"- **{(r or {}).get('title', '')}** — {(r or {}).get('url', '')}")
        return "\n".join(lines) + "\n"
