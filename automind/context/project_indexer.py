"""项目索引器 — 扫描目录树，构建文件索引。"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class FileEntry:
    """单个文件的索引条目。"""

    path: str
    relative_path: str
    name: str
    extension: str
    size_bytes: int
    mtime: float
    content_hash: str = ""
    language: str = ""
    imports: list[str] = field(default_factory=list)
    exports: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "relative_path": self.relative_path,
            "name": self.name,
            "extension": self.extension,
            "size_bytes": self.size_bytes,
            "language": self.language,
        }


@dataclass
class ProjectIndex:
    """项目完整索引。"""

    root: str
    files: list[FileEntry] = field(default_factory=list)
    file_count: int = 0
    total_size_bytes: int = 0
    languages: dict[str, int] = field(default_factory=dict)
    directory_tree: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0

    def to_summary(self) -> str:
        """生成项目概览文本。"""
        lines = [
            f"Project: {self.root}",
            f"Files: {self.file_count} ({self._format_size(self.total_size_bytes)})",
            f"Languages: {', '.join(f'{k}({v})' for k, v in self.languages.items())}",
        ]
        if self.files:
            lines.append("\nKey files:")
            for f in self.files[:20]:
                lines.append(f"  {f.relative_path} ({f.language})")
        return "\n".join(lines)

    @staticmethod
    def _format_size(size: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024:
                return f"{size:.1f}{unit}"
            size /= 1024
        return f"{size:.1f}TB"


class ProjectIndexer:
    """项目目录扫描与索引构建器。"""

    # 已知扩展名 → 语言映射
    EXTENSION_MAP: dict[str, str] = {
        ".py": "Python",
        ".js": "JavaScript",
        ".ts": "TypeScript",
        ".tsx": "TypeScript React",
        ".jsx": "JavaScript React",
        ".go": "Go",
        ".rs": "Rust",
        ".java": "Java",
        ".kt": "Kotlin",
        ".swift": "Swift",
        ".c": "C",
        ".cpp": "C++",
        ".h": "C/C++ Header",
        ".hpp": "C++ Header",
        ".rb": "Ruby",
        ".php": "PHP",
        ".cs": "C#",
        ".fs": "F#",
        ".scala": "Scala",
        ".md": "Markdown",
        ".yaml": "YAML",
        ".yml": "YAML",
        ".json": "JSON",
        ".toml": "TOML",
        ".xml": "XML",
        ".html": "HTML",
        ".css": "CSS",
        ".scss": "SCSS",
        ".sql": "SQL",
        ".sh": "Shell",
        ".bash": "Bash",
        ".zsh": "Zsh",
        ".ps1": "PowerShell",
        ".bat": "Batch",
        ".dockerfile": "Docker",
        ".makefile": "Makefile",
    }

    # 默认忽略模式
    DEFAULT_IGNORE_PATTERNS: list[str] = [
        ".git", "__pycache__", "*.pyc", "*.pyo",
        "node_modules", ".venv", "venv", ".env",
        ".tox", ".eggs", "*.egg-info", "dist", "build",
        ".mypy_cache", ".pytest_cache", ".ruff_cache",
        ".idea", ".vscode", ".DS_Store", "Thumbs.db",
        "*.log", "*.lock", ".automind",
    ]

    def __init__(
        self,
        project_root: str | Path = ".",
        ignore_patterns: list[str] | None = None,
        cache_file: str | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.ignore_patterns = ignore_patterns or self.DEFAULT_IGNORE_PATTERNS
        self.cache_file = cache_file

    def build_index(self, force: bool = False) -> ProjectIndex:
        """构建项目索引。

        Args:
            force: 是否强制重建 (忽略缓存)。

        Returns:
            ProjectIndex 实例。
        """
        if not force and self.cache_file:
            cached = self._load_cache()
            if cached is not None:
                return cached

        index = ProjectIndex(
            root=str(self.project_root),
            created_at=time.time(),
        )
        files: list[FileEntry] = []
        dir_tree: dict[str, Any] = {}

        for entry in self.project_root.rglob("*"):
            if self._should_ignore(entry):
                continue
            if entry.is_file():
                fe = self._index_file(entry)
                files.append(fe)
                index.total_size_bytes += fe.size_bytes
                lang = fe.language
                if lang:
                    index.languages[lang] = index.languages.get(lang, 0) + 1

        index.files = sorted(files, key=lambda f: f.relative_path)
        index.file_count = len(files)

        if self.cache_file:
            self._save_cache(index)

        return index

    def search_files(self, pattern: str, index: ProjectIndex | None = None) -> list[FileEntry]:
        """通配符搜索文件。"""
        if index is None:
            index = self.build_index()
        result = []
        for f in index.files:
            if fnmatch.fnmatch(f.name, pattern) or fnmatch.fnmatch(f.relative_path, pattern):
                result.append(f)
        return result

    def get_files_by_language(self, language: str, index: ProjectIndex | None = None) -> list[FileEntry]:
        """按语言筛选文件。"""
        if index is None:
            index = self.build_index()
        return [f for f in index.files if f.language.lower() == language.lower()]

    def _index_file(self, path: Path) -> FileEntry:
        ext = path.suffix.lower()
        language = self.EXTENSION_MAP.get(ext, "")
        if not language and path.name.lower() in ("dockerfile", "makefile"):
            language = self.EXTENSION_MAP.get(f".{path.name.lower()}", "")

        return FileEntry(
            path=str(path),
            relative_path=str(path.relative_to(self.project_root)),
            name=path.name,
            extension=ext,
            size_bytes=path.stat().st_size,
            mtime=path.stat().st_mtime,
            language=language,
        )

    def _should_ignore(self, path: Path) -> bool:
        # B-13 修复：指向项目根之外的符号链接会让 relative_to 抛 ValueError，
        # 直接视为忽略，避免整个索引构建崩溃。
        try:
            rel = str(path.relative_to(self.project_root))
        except ValueError:
            return True
        for pattern in self.ignore_patterns:
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(path.name, pattern):
                return True
            # 匹配路径各段
            for part in path.parts:
                if fnmatch.fnmatch(part, pattern):
                    return True
        return False

    def _load_cache(self) -> ProjectIndex | None:
        if not self.cache_file:
            return None
        cache_path = Path(self.cache_file)
        if not cache_path.exists():
            return None
        try:
            with open(cache_path, encoding="utf-8") as f:  # B-14 修复：显式 UTF-8
                data = json.load(f)
            # 简单检查：缓存时间超过 1 小时则失效
            if time.time() - data.get("created_at", 0) > 3600:
                return None
            index = ProjectIndex(root=data.get("root", ""), created_at=data.get("created_at", 0))
            index.file_count = data.get("file_count", 0)
            index.total_size_bytes = data.get("total_size_bytes", 0)
            index.languages = data.get("languages", {})
            return index
        except Exception:
            return None

    def _save_cache(self, index: ProjectIndex) -> None:
        if not self.cache_file:
            return
        cache_path = Path(self.cache_file)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:  # B-14 修复：显式 UTF-8
            json.dump({
                "root": index.root,
                "file_count": index.file_count,
                "total_size_bytes": index.total_size_bytes,
                "languages": index.languages,
                "created_at": index.created_at,
            }, f)
