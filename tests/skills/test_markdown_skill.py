"""MarkdownSkill / SKILL.md 加载测试。"""

from automind.skills.markdown_skill import MarkdownSkill, _extract_emoji, _parse_frontmatter
from automind.skills.skill_registry import SkillRegistry

SKILL_MD = """---
name: demo-skill
description: 一个演示技能
author: tester
metadata:
  openclaw:
    emoji: 🚀
    requires:
      tools: ["browser", "terminal"]
---

# Demo Skill

正文内容，使用说明。
"""


def test_parse_frontmatter():
    meta, body = _parse_frontmatter(SKILL_MD)
    assert meta["name"] == "demo-skill"
    assert "正文内容" in body


def test_extract_emoji():
    meta, _ = _parse_frontmatter(SKILL_MD)
    assert _extract_emoji(meta) == "🚀"
    assert _extract_emoji({}) == "📦"


def test_markdown_skill_loads(tmp_path):
    folder = tmp_path / "demo-skill"
    folder.mkdir()
    (folder / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    skill = MarkdownSkill(folder / "SKILL.md")
    assert skill.name == "demo-skill"
    assert skill.emoji == "🚀"
    assert skill.required_tools == ["browser", "terminal"]
    d = skill.to_dict()
    assert d["type"] == "markdown"
    assert d["emoji"] == "🚀"


def test_registry_discovers_skill_md(tmp_path):
    for nm in ("alpha", "beta"):
        folder = tmp_path / nm
        folder.mkdir()
        (folder / "SKILL.md").write_text(
            f"---\nname: {nm}\ndescription: d\n---\n# body", encoding="utf-8")
    # node_modules 应被跳过
    nm_dir = tmp_path / "node_modules" / "junk"
    nm_dir.mkdir(parents=True)
    (nm_dir / "SKILL.md").write_text("---\nname: junk\n---\n", encoding="utf-8")

    reg = SkillRegistry()
    n = reg.discover_skill_md(tmp_path)
    assert n == 2
    names = reg.list_names()
    assert "alpha" in names and "beta" in names and "junk" not in names
