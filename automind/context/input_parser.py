"""输入解析器 — 统一解析 NL 文本、文件路径、图片等输入。"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from automind.core.types import InputMessage


class InputParser:
    """统一输入解析器 — 支持自然语言、文件引用、图片等。"""

    # 文件扩展名映射
    TEXT_EXTENSIONS: set[str] = {
        ".txt", ".py", ".js", ".ts", ".go", ".rs", ".java", ".kt",
        ".md", ".yaml", ".yml", ".json", ".toml", ".xml", ".html",
        ".css", ".sql", ".sh", ".cfg", ".ini", ".env",
    }
    IMAGE_EXTENSIONS: set[str] = {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg",
    }
    # B-12 修复：图片大小上限，防止超大文件读入导致 OOM。
    MAX_IMAGE_BYTES: int = 20 * 1024 * 1024  # 20MB

    def __init__(self) -> None:
        self._parsed_files: dict[str, str] = {}

    def parse(self, raw_input: str) -> InputMessage:
        """解析用户输入。

        自动识别：
        - `@filepath` → 读取文件内容
        - `![description](filepath)` → 读取图片 (base64)
        - 自然语言文本 → 直接保留

        Args:
            raw_input: 用户原始输入。

        Returns:
            结构化的 InputMessage。
        """
        msg = InputMessage(raw_text=raw_input)
        msg.intent = self._classify_intent(raw_input)
        msg.constraints = self._extract_constraints(raw_input)

        # 提取 @file 引用
        import re
        file_refs = re.findall(r'@([\w./\\\-]+)', raw_input)
        for ref in file_refs:
            path = Path(ref)
            if path.is_absolute() and path.exists():
                msg.attached_files.append(str(path))
            elif path.exists():
                msg.attached_files.append(str(path.resolve()))

        # 提取图片
        for ext in self.IMAGE_EXTENSIONS:
            pattern = rf'!\[.*?\]\(([^)]+{ext})\)'
            for match in re.findall(pattern, raw_input):
                img_path = Path(match)
                # B-12 修复：消除 exists()→read_bytes() 之间的 TOCTOU 竞争，
                # 并加入大小上限；直接尝试读取并捕获文件消失/无权限等异常。
                try:
                    if img_path.stat().st_size > self.MAX_IMAGE_BYTES:
                        continue
                    msg.images.append(img_path.read_bytes())
                except (FileNotFoundError, PermissionError, OSError):
                    continue

        # 从文本中提取关键实体
        msg.entities = self._extract_entities(raw_input)

        return msg

    def read_file_content(self, file_path: str | Path) -> str:
        """读取文件内容 (支持文本文件)。"""
        path = Path(file_path)
        if not path.exists():
            return f"[File not found: {file_path}]"

        ext = path.suffix.lower()
        if ext in self.TEXT_EXTENSIONS or ext == "":
            try:
                content = path.read_text(encoding="utf-8")
                self._parsed_files[str(path)] = content
                return content
            except UnicodeDecodeError:
                return f"[Binary file: {path.name}]"
        elif ext in self.IMAGE_EXTENSIONS:
            return self._encode_image(path)
        else:
            return f"[Unknown file type: {ext}]"

    def get_parsed_files(self) -> dict[str, str]:
        """返回所有已解析的文件内容。"""
        return dict(self._parsed_files)

    @staticmethod
    def _encode_image(path: Path) -> str:
        """将图片编码为 base64 data URL。"""
        data = path.read_bytes()
        ext = path.suffix.lower().lstrip(".")
        mime_map = {
            "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
            "svg": "image/svg+xml",
        }
        mime = mime_map.get(ext, "image/png")
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:{mime};base64,{b64}"

    @staticmethod
    def _classify_intent(text: str) -> str:
        """简单的意图分类。"""
        text_lower = text.lower()
        if any(w in text_lower for w in ("创建", "新建", "初始化", "init", "create", "scaffold")):
            return "create_project"
        if any(w in text_lower for w in ("修复", "fix", "debug", "bug", "错误", "报错")):
            return "fix_bug"
        if any(w in text_lower for w in ("重构", "refactor", "优化", "optimize")):
            return "refactor"
        if any(w in text_lower for w in ("测试", "test", "pytest", "unittest")):
            return "run_tests"
        if any(w in text_lower for w in ("部署", "deploy", "发布", "release")):
            return "deploy"
        if any(w in text_lower for w in ("解释", "explain", "说明", "是什么", "怎么样")):
            return "explain"
        if any(w in text_lower for w in ("搜索", "search", "find", "查找", "grep")):
            return "search"
        return "general"

    @staticmethod
    def _extract_constraints(text: str) -> list[str]:
        """从文本中提取约束条件。"""
        constraints = []
        if "必须" in text or "must" in text.lower():
            constraints.append("required")
        if "不要" in text or "禁止" in text or "don't" in text.lower():
            constraints.append("forbidden_actions")
        if "安全" in text or "security" in text.lower():
            constraints.append("security_sensitive")
        return constraints

    @staticmethod
    def _extract_entities(text: str) -> dict[str, Any]:
        """从文本中提取关键实体 (简单的关键词匹配)。"""
        import re
        entities: dict[str, Any] = {}

        # 匹配技术栈
        tech_keywords = [
            "python", "javascript", "typescript", "go", "rust", "java", "kotlin",
            "react", "vue", "angular", "svelte", "next.js", "nuxt",
            "fastapi", "flask", "django", "express", "spring",
            "postgresql", "mysql", "mongodb", "redis", "sqlite",
            "docker", "kubernetes", "aws", "gcp", "azure",
        ]
        found_tech = [t for t in tech_keywords if t.lower() in text.lower()]
        if found_tech:
            entities["technologies"] = found_tech

        # 匹配文件路径
        paths = re.findall(r'(?:[\w./\\-]+\.\w{1,6})', text)
        if paths:
            entities["mentioned_files"] = paths

        return entities
