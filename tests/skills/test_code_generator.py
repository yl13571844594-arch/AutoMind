"""增强版代码生成技能测试（§14.4）。"""

import ast

from automind.skills.builtin.code_generator import CodeGeneratorSkill


class _FakeLLM:
    """按序返回预设文本的 mock LLM。"""

    def __init__(self, texts):
        self._texts = texts
        self._i = 0

    async def generate(self, messages, **kwargs):
        class R:
            pass

        r = R()
        r.text = self._texts[self._i]
        self._i = min(self._i + 1, len(self._texts) - 1)
        return r


class _Agent:
    def __init__(self, llm=None):
        self.llm = llm
        self.memory = None


class TestOfflineTemplates:
    def test_python_template_is_valid(self, tmp_path):
        import asyncio
        skill = CodeGeneratorSkill()
        f = tmp_path / "m.py"
        r = asyncio.run(skill.execute({"specification": "helper", "output_file": str(f)}))
        assert r.success
        ast.parse(f.read_text(encoding="utf-8"))  # 生成的模板必须可解析

    def test_empty_output_fails(self, tmp_path):
        import asyncio
        skill = CodeGeneratorSkill()
        # complete 模式 + 无 LLM + 空规格 → 仍走模板，不为空
        r = asyncio.run(skill.execute({"output_file": str(tmp_path / "x.py")}))
        assert r.success


class TestFenceExtraction:
    def test_markdown_fences_stripped(self, tmp_path):
        import asyncio
        skill = CodeGeneratorSkill()
        f = tmp_path / "a.py"
        llm = _FakeLLM(["```python\ndef add(a, b):\n    return a + b\n```"])
        r = asyncio.run(skill.execute(
            {"specification": "add", "output_file": str(f)}, _Agent(llm)))
        content = f.read_text(encoding="utf-8")
        assert "```" not in content
        assert r.output["validated"] is True


class TestValidationAndRepair:
    def test_self_repair_on_syntax_error(self, tmp_path):
        import asyncio
        skill = CodeGeneratorSkill()
        f = tmp_path / "fix.py"
        # 第一次输出语法错误，修复后合法
        llm = _FakeLLM(["def broken(:\n  pass", "```python\ndef broken():\n    pass\n```"])
        r = asyncio.run(skill.execute(
            {"specification": "x", "output_file": str(f)}, _Agent(llm)))
        assert r.metadata["self_repaired"] is True
        assert r.output["validated"] is True

    def test_json_validation(self, tmp_path):
        import asyncio
        skill = CodeGeneratorSkill()
        f = tmp_path / "d.json"
        llm = _FakeLLM(['{"a": 1, "b": [1,2,3]}'])
        r = asyncio.run(skill.execute(
            {"specification": "config", "output_file": str(f)}, _Agent(llm)))
        assert r.output["validated"] is True


class TestLanguageDetection:
    def test_extension_overrides_explicit(self, tmp_path):
        import asyncio
        skill = CodeGeneratorSkill()
        f = tmp_path / "a.ts"
        llm = _FakeLLM(["```ts\nexport const x = 1;\n```"])
        r = asyncio.run(skill.execute(
            {"specification": "c", "output_file": str(f), "language": "python"}, _Agent(llm)))
        assert r.output["language"] == "typescript"


class TestCompleteMode:
    def test_complete_fills_stub(self, tmp_path):
        import asyncio
        skill = CodeGeneratorSkill()
        f = tmp_path / "c.py"
        llm = _FakeLLM(["```python\ndef area(r):\n    return 3.14159 * r * r\n```"])
        r = asyncio.run(skill.execute({
            "mode": "complete", "existing_code": "def area(r):\n    ...",
            "specification": "circle area", "output_file": str(f),
        }, _Agent(llm)))
        assert r.output["validated"] is True
        assert "3.14159" in r.output["code"]


class TestWriteGuards:
    def test_overwrite_guard_blocks(self, tmp_path):
        import asyncio
        skill = CodeGeneratorSkill()
        f = tmp_path / "g.py"
        f.write_text("existing\n", encoding="utf-8")
        r = asyncio.run(skill.execute(
            {"specification": "x", "output_file": str(f), "overwrite": False}))
        assert not r.success

    def test_incremental_appends(self, tmp_path):
        import asyncio
        skill = CodeGeneratorSkill()
        f = tmp_path / "h.py"
        f.write_text("def one():\n    pass\n", encoding="utf-8")
        r = asyncio.run(skill.execute({
            "specification": "two", "output_file": str(f),
            "overwrite": False, "incremental": True,
        }))
        assert r.success
        assert f.read_text(encoding="utf-8").count("def ") >= 2

    def test_default_overwrite_preserved(self, tmp_path):
        # 默认 overwrite=True：保持旧行为，直接覆盖
        import asyncio
        skill = CodeGeneratorSkill()
        f = tmp_path / "i.py"
        f.write_text("old\n", encoding="utf-8")
        r = asyncio.run(skill.execute({"specification": "new", "output_file": str(f)}))
        assert r.success
        assert "old" not in f.read_text(encoding="utf-8")
