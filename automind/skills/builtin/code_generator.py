"""代码生成技能 — 从规格生成/补全代码，匹配项目风格并做语法校验（§14.4 增强）。

在原有"规格→代码→写文件"基础上增强编程能力：
    - 语言自动检测（依据 output_file 扩展名）
    - LLM 输出的 Markdown 代码围栏自动剥离（避免 ``` 混入源文件）
    - 生成后语法校验（Python/JSON）+ 一次自我修复重试
    - 多模式：generate（生成）/ complete（补全既有代码）/ scaffold（脚手架）
    - 增量与覆盖保护（incremental / overwrite）
    - 无 LLM 时的高质量模板兜底

对外接口（name / execute 签名 / 结果结构）保持不变，向后兼容旧调用方。
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from automind.skills.skill_base import AbstractSkill, SkillResult


class CodeGenInput(BaseModel):
    """代码生成输入（新增字段均为可选，旧调用零改动）。"""

    language: str = "python"
    specification: str = ""
    output_file: str
    style_guide: str = ""
    context: str = ""  # 项目上下文
    # ── 增强字段 ──
    mode: str = "generate"        # generate / complete / scaffold
    existing_code: str = ""        # complete 模式下待补全的既有代码
    framework: str = ""            # scaffold 模式下的框架（fastapi/flask/cli...）
    incremental: bool = False      # 文件已存在时追加而非覆盖
    overwrite: bool = True         # 允许覆盖既有文件
    validate_syntax: bool = True   # 生成后做语法校验（含一次自我修复）


# 扩展名 → 语言
_EXT_LANG: dict[str, str] = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".mjs": "javascript",
    ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript", ".go": "go",
    ".rs": "rust", ".java": "java", ".kt": "kotlin", ".rb": "ruby", ".php": "php",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp", ".cs": "csharp",
    ".sh": "bash", ".html": "html", ".css": "css", ".scss": "css", ".sql": "sql",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".md": "markdown",
}

_FENCE_RE = re.compile(r"```[ \t]*([\w+#.-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)

# 语言别名，用于围栏标签匹配
_LANG_ALIASES: dict[str, set[str]] = {
    "python": {"python", "py"},
    "javascript": {"javascript", "js", "node"},
    "typescript": {"typescript", "ts"},
    "csharp": {"csharp", "cs", "c#"},
    "bash": {"bash", "sh", "shell"},
}


class CodeGeneratorSkill(AbstractSkill):
    """从自然语言规格生成/补全代码，自动匹配项目风格并校验语法。"""

    name = "code_generator"
    description = (
        "Generate or complete code from a specification, match project style, "
        "and validate syntax with an automatic self-repair pass."
    )
    input_schema = CodeGenInput

    async def execute(self, input_data: Any, agent: Any = None) -> SkillResult:
        if isinstance(input_data, dict):
            inp = CodeGenInput(**input_data)
        else:
            inp = input_data

        # 语言：文件扩展名优先（更可靠），否则用显式指定
        language = self._detect_language(inp.output_file, inp.language)

        # 项目风格约定
        style_guide = inp.style_guide
        if agent is not None and getattr(agent, "memory", None) is not None:
            try:
                style_guide = agent.memory.project.get_convention_prompt() or style_guide
            except Exception:
                pass

        llm = getattr(agent, "llm", None) if agent is not None else None

        # 生成代码
        if llm is not None:
            raw = await self._llm_generate(llm, inp, language, style_guide)
            code = self._extract_code(raw, language)
        else:
            code = self._template_generate(inp, language)

        if not code.strip():
            return SkillResult(success=False, error="生成结果为空，未写入文件")

        # 语法校验 + 一次自我修复
        validated: bool | None = None
        syntax_error = ""
        self_repaired = False
        if inp.validate_syntax:
            validated, syntax_error = self._validate(code, language)
            if validated is False and llm is not None:
                repaired = await self._llm_repair(llm, code, syntax_error, language)
                repaired_code = self._extract_code(repaired, language)
                if repaired_code.strip():
                    v2, e2 = self._validate(repaired_code, language)
                    if v2 is not False:  # 修复成功或无法判定，采用修复版本
                        code, validated, syntax_error = repaired_code, v2, e2
                        self_repaired = True

        # 写文件（含增量/覆盖保护）
        try:
            path = Path(inp.output_file)
            existed = path.exists()
            if existed and not inp.overwrite:
                if inp.incremental:
                    prev = path.read_text(encoding="utf-8")
                    code = prev.rstrip() + "\n\n" + code.lstrip()
                else:
                    return SkillResult(
                        success=False,
                        error=f"文件已存在：{path}（设置 overwrite=true 或 incremental=true）",
                    )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(code, encoding="utf-8")
        except Exception as e:
            return SkillResult(success=False, error=str(e))

        return SkillResult(
            success=True,
            output={
                "file": str(path),
                "code": code,
                "lines": code.count("\n") + 1,
                "language": language,
                "validated": validated,
                "syntax_error": syntax_error,
            },
            artifacts=[str(path)],
            metadata={"mode": inp.mode, "language": language, "self_repaired": self_repaired},
        )

    # ── LLM 路径 ──────────────────────────────────────────

    async def _llm_generate(
        self, llm: Any, inp: CodeGenInput, language: str, style_guide: str
    ) -> str:
        system = (
            "You are an expert software engineer. Write clean, correct, production-ready "
            f"{language} code. Follow the project's conventions. Provide COMPLETE, working "
            "implementations — never leave TODO stubs, placeholders, or pseudo-code. "
            "Include necessary imports. Output ONLY the code, no prose."
        )

        if inp.mode == "complete" and inp.existing_code.strip():
            user = (
                f"Complete the following {language} code. Preserve existing structure, "
                "implement all unfinished parts, and return the FULL file.\n\n"
                f"Existing code:\n{inp.existing_code}\n"
            )
            if inp.specification:
                user += f"\nWhat to complete / requirements:\n{inp.specification}\n"
        elif inp.mode == "scaffold":
            fw = inp.framework or language
            user = (
                f"Generate a minimal but runnable {fw} starter for a single file "
                f"({Path(inp.output_file).name}). Requirements:\n{inp.specification}\n"
            )
        else:
            user = f"Generate {language} code based on this specification:\n{inp.specification}\n"

        if style_guide:
            user += f"\nProject style guide:\n{style_guide}\n"
        if inp.context:
            user += f"\nProject context:\n{inp.context}\n"

        response = await llm.generate([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])
        return (response.text or "").strip()

    async def _llm_repair(self, llm: Any, code: str, error: str, language: str) -> str:
        prompt = (
            f"The following {language} code has a syntax error: {error}\n\n"
            f"Fix ONLY the error and return the complete corrected code. Output ONLY code.\n\n"
            f"{code}"
        )
        try:
            response = await llm.generate([{"role": "user", "content": prompt}])
            return (response.text or "").strip()
        except Exception:
            return ""

    # ── 辅助方法 ──────────────────────────────────────────

    @staticmethod
    def _detect_language(output_file: str, explicit: str) -> str:
        ext = Path(output_file).suffix.lower()
        if ext in _EXT_LANG:
            return _EXT_LANG[ext]
        return explicit or "python"

    @staticmethod
    def _extract_code(text: str, language: str) -> str:
        """从可能包含 Markdown 围栏/解释的文本中提取纯代码。"""
        text = (text or "").strip()
        matches = _FENCE_RE.findall(text)
        if matches:
            aliases = _LANG_ALIASES.get(language, {language})
            # 优先取语言标签匹配的代码块
            for tag, body in matches:
                if tag.lower() in aliases:
                    return body.strip("\n")
            # 否则取最长的代码块（通常即主体）
            return max((b for _, b in matches), key=len).strip("\n")
        return text

    @staticmethod
    def _validate(code: str, language: str) -> tuple[bool | None, str]:
        """返回 (是否通过, 错误信息)。None 表示该语言不做校验。"""
        lang = language.lower()
        if lang == "python":
            try:
                ast.parse(code)
                return True, ""
            except SyntaxError as e:
                return False, f"SyntaxError: {e.msg} (line {e.lineno})"
        if lang == "json":
            try:
                json.loads(code)
                return True, ""
            except ValueError as e:
                return False, f"JSONError: {e}"
        return None, ""

    # ── 无 LLM 模板兜底 ───────────────────────────────────

    @staticmethod
    def _template_generate(inp: CodeGenInput, language: str) -> str:
        spec = inp.specification or "generated module"
        if language == "python":
            name = Path(inp.output_file).stem or "module"
            return (
                f'"""{spec}"""\n\n'
                "from __future__ import annotations\n\n\n"
                f"def {name}() -> None:\n"
                f'    """TODO: implement — {spec}"""\n'
                "    raise NotImplementedError\n\n\n"
                'if __name__ == "__main__":\n'
                f"    {name}()\n"
            )
        if language in ("javascript", "typescript"):
            ann = ": void" if language == "typescript" else ""
            return (
                f"// {spec}\n\n"
                f"export function main(){ann} {{\n"
                f"  // TODO: implement — {spec}\n"
                "  throw new Error('Not implemented');\n"
                "}\n"
            )
        if language == "html":
            return (
                "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
                "  <meta charset=\"utf-8\">\n"
                f"  <title>{spec}</title>\n</head>\n<body>\n"
                f"  <!-- {spec} -->\n</body>\n</html>\n"
            )
        return f"# {spec}\n# TODO: implement (no LLM available)\n"
