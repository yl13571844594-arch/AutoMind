"""Markdown 技能 — 加载 SKILL.md 格式技能（Claude Code / OpenClaw 风格）。

每个技能是一个文件夹，内含 `SKILL.md`：

    ---
    name: my-skill
    description: 简介...
    metadata:
      openclaw:
        emoji: 🌐
    ---

    # 技能正文（指令 / 使用说明 / 流程）...

这类技能是“指令型/提示型”技能：被调用时，把正文作为指导注入到 Agent 上下文，
而非执行 Python 代码。与内置的 AbstractSkill（可执行）互补。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from automind.skills.skill_base import AbstractSkill, SkillResult


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 YAML frontmatter，返回 (元数据, 正文)。"""
    meta: dict = {}
    body = text
    m = re.match(r"^﻿?---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if m:
        front, body = m.group(1), m.group(2)
        try:
            import yaml
            meta = yaml.safe_load(front) or {}
            if not isinstance(meta, dict):
                meta = {}
        except Exception:
            meta = {}
    return meta, body


def _extract_emoji(meta: dict) -> str:
    """从元数据中尽量取出 emoji。"""
    for path in (("metadata", "openclaw", "emoji"), ("emoji",),
                 ("metadata", "emoji")):
        node: Any = meta
        ok = True
        for key in path:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                ok = False
                break
        if ok and isinstance(node, str) and node.strip():
            return node.strip()
    return "📦"


class MarkdownSkill(AbstractSkill):
    """基于 SKILL.md 的指令型技能。"""

    source = "markdown"

    def __init__(self, skill_md: Path) -> None:
        self.path = Path(skill_md)
        raw = self.path.read_text(encoding="utf-8", errors="ignore")
        meta, body = _parse_frontmatter(raw)
        self.meta = meta
        self.body = body.strip()
        folder = self.path.parent.name
        self.name = str(meta.get("name") or folder).strip()
        self.description = str(meta.get("description") or "").strip() or "(无描述)"
        self.author = str(meta.get("author") or "").strip()
        self.emoji = _extract_emoji(meta)
        # 依赖的工具（若声明）
        req = meta.get("metadata", {})
        if isinstance(req, dict):
            req = req.get("openclaw", {}).get("requires", {}) if isinstance(req.get("openclaw"), dict) else {}
        self.required_tools = req.get("tools", []) if isinstance(req, dict) else []

    async def execute(self, input_data: Any, agent: Any = None) -> SkillResult:
        """调用技能 — 返回技能的指令正文（供注入上下文 / 指导执行）。"""
        return SkillResult(
            success=True,
            output=self.body,
            metadata={"name": self.name, "emoji": self.emoji,
                      "source": "markdown", "path": str(self.path)},
        )

    def instructions(self, max_chars: int = 4000) -> str:
        """返回可注入提示词的技能指令。"""
        return f"【技能: {self.name}】{self.description}\n\n{self.body[:max_chars]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "emoji": self.emoji,
            "author": self.author,
            "type": "markdown",
            "required_tools": self.required_tools,
            "path": str(self.path),
            "body_chars": len(self.body),
        }
