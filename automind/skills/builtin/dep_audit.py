"""依赖审计技能（§14.8）— 解析 requirements.txt / pyproject.toml 依赖并体检。

纯标准库实现（tomllib，Python ≥ 3.11），无网络请求，结果确定可测。
报告：依赖总数、未固定版本（unpinned）、重复声明、已安装版本对照。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from automind.skills.skill_base import AbstractSkill, SkillResult

_NAME_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)")
_SPEC_RE = re.compile(r"[=<>~!]")


class DepAuditInput(BaseModel):
    """依赖审计输入。"""

    path: str = "."          # 项目根目录，或具体的 requirements.txt / pyproject.toml
    report_file: str = ""    # 可选：Markdown 报告输出路径


class DependencyAuditSkill(AbstractSkill):
    """审计项目依赖：统计、未固定版本、重复项、已安装版本对照。"""

    name = "dep_audit"
    description = "Audit project dependencies: counts, unpinned versions, duplicates, installed versions."
    input_schema = DepAuditInput

    async def execute(self, input_data: Any, agent: Any = None) -> SkillResult:  # noqa: ARG002
        inp = DepAuditInput(**input_data) if isinstance(input_data, dict) else input_data

        target = Path(inp.path)
        if not target.exists():
            return SkillResult(success=False, error=f"路径不存在：{target}")

        deps: list[str] = []
        sources: list[str] = []
        if target.is_file():
            deps, ok = self._parse_file(target)
            if not ok:
                return SkillResult(success=False, error=f"无法解析依赖文件：{target}")
            sources.append(str(target))
        else:
            for req in sorted(target.glob("requirements*.txt")):
                parsed, ok = self._parse_file(req)
                if ok:
                    deps += parsed
                    sources.append(str(req))
            pyproject = target / "pyproject.toml"
            if pyproject.exists():
                parsed, ok = self._parse_file(pyproject)
                if ok:
                    deps += parsed
                    sources.append(str(pyproject))

        if not sources:
            return SkillResult(success=False, error="未找到 requirements*.txt 或 pyproject.toml")

        summary = self._audit(deps)
        summary["sources"] = sources

        artifacts: list[str] = []
        if inp.report_file:
            try:
                path = Path(inp.report_file)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(self._render(summary), encoding="utf-8")
                artifacts.append(str(path))
            except OSError as e:
                return SkillResult(success=False, error=f"写入报告失败：{e}", output=summary)

        return SkillResult(success=True, output=summary, artifacts=artifacts)

    # ── 解析 ──────────────────────────────────────────────

    def _parse_file(self, path: Path) -> tuple[list[str], bool]:
        try:
            if path.name == "pyproject.toml":
                return self._parse_pyproject(path), True
            return self._parse_requirements(path), True
        except (OSError, ValueError):
            return [], False

    @staticmethod
    def _parse_requirements(path: Path) -> list[str]:
        out = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", "-")):
                continue
            out.append(line.split(";")[0].strip())
        return out

    @staticmethod
    def _parse_pyproject(path: Path) -> list[str]:
        import tomllib
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        project = data.get("project", {})
        deps = list(project.get("dependencies", []) or [])
        for group in (project.get("optional-dependencies", {}) or {}).values():
            deps += list(group or [])
        return [d.split(";")[0].strip() for d in deps]

    # ── 审计逻辑 ──────────────────────────────────────────

    def _audit(self, deps: list[str]) -> dict:
        seen: dict[str, int] = {}
        unpinned: list[str] = []
        entries: list[dict] = []
        for dep in deps:
            name_m = _NAME_RE.match(dep)
            if not name_m:
                continue
            name = name_m.group(1)
            key = name.lower()
            seen[key] = seen.get(key, 0) + 1
            pinned = bool(_SPEC_RE.search(dep))
            if not pinned:
                unpinned.append(name)
            entries.append({
                "name": name,
                "spec": dep,
                "pinned": pinned,
                "installed": self._installed_version(name),
            })
        duplicates = sorted(k for k, v in seen.items() if v > 1)
        return {
            "total": len(entries),
            "unique": len(seen),
            "unpinned": sorted(set(unpinned)),
            "unpinned_count": len(set(unpinned)),
            "duplicates": duplicates,
            "dependencies": entries,
        }

    @staticmethod
    def _installed_version(name: str) -> str | None:
        try:
            from importlib.metadata import PackageNotFoundError, version
            try:
                return version(name)
            except PackageNotFoundError:
                return None
        except Exception:
            return None

    @staticmethod
    def _render(summary: dict) -> str:
        lines = ["# 依赖审计报告", "",
                 f"- 依赖总数：{summary['total']}（唯一 {summary['unique']}）",
                 f"- 未固定版本：{summary['unpinned_count']}",
                 f"- 重复声明：{len(summary['duplicates'])}"]
        if summary["unpinned"]:
            lines += ["", "## 未固定版本", *[f"- {n}" for n in summary["unpinned"]]]
        if summary["duplicates"]:
            lines += ["", "## 重复声明", *[f"- {n}" for n in summary["duplicates"]]]
        return "\n".join(lines) + "\n"
