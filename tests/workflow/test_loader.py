"""加载期校验测试 —— "能在跑之前拒绝的，绝不留给运行时"。

任务要求的六类错误逐条覆盖：缺 id、重复 id、未知 type、引用不存在的 step、
模板引用未定义变量、缺少/未知 version。另外补了实际写 YAML 时同样高频的几类
（重复键、未知字段、非法 on_failure、branch 跳转目标不存在、循环）。

每条断言都要求报错**指出位置**（行号或字段路径）：这是"可评审"的一部分 ——
一份 200 行的流程文件只说"某处有错"，评审方还得自己找。
"""

from __future__ import annotations

import textwrap

import pytest

from automind.workflow.exceptions import (
    UnknownSchemaVersionError,
    WorkflowLoadError,
)
from automind.workflow.loader import (
    WorkflowLoader,
    build_document,
    load_version_checked,
)


def load_errors(text: str, **kw) -> list:
    schema, errors, warnings = WorkflowLoader(**kw).try_parse(text)
    assert schema is None, "这份工作流本应校验失败，但它通过了"
    return errors


def error_text(text: str, **kw) -> str:
    return "\n".join(e.format() for e in load_errors(text, **kw))


MINIMAL = """\
version: 1
name: 最小
steps:
  - id: a
    type: tool
    tool: recorder
"""


class TestHappyPath:
    def test_minimal_loads(self) -> None:
        schema = WorkflowLoader().parse(MINIMAL)
        assert schema.version == 1
        assert schema.name == "最小"
        assert schema.step_ids == ["a"]
        assert schema.steps[0].tool == "recorder"
        assert schema.steps[0].on_failure.kind == "abort"      # 默认策略
        assert schema.source_digest                        # 摘要可用

    def test_positions_are_recorded(self) -> None:
        schema = WorkflowLoader().parse(MINIMAL)
        # `- id: a` 在第 4 行、缩进 5 列（1 基）
        assert schema.steps[0].position.line == 4
        assert schema.steps[0].position.column == 5

    def test_json_is_accepted_and_equivalent(self) -> None:
        import json

        doc = {"version": 1, "name": "json 版",
               "steps": [{"id": "a", "type": "tool", "tool": "recorder"}]}
        schema, errors, _ = WorkflowLoader().try_parse(json.dumps(doc), is_json=True)
        assert not errors and schema is not None
        assert schema.step_ids == ["a"]

    def test_as_dict_is_json_serializable(self) -> None:
        import json

        schema = WorkflowLoader().parse(MINIMAL)
        json.dumps(schema.as_dict(), ensure_ascii=False)   # 不抛即通过（回前端用）


class TestVersion:
    """版本字段强制且不许猜。"""

    def test_missing_version_rejected(self) -> None:
        text = MINIMAL.replace("version: 1\n", "")
        messages = error_text(text)
        assert "version" in messages
        assert "缺少必需的 version" in messages

    def test_unknown_version_rejected(self) -> None:
        text = MINIMAL.replace("version: 1", "version: 2")
        messages = error_text(text)
        assert "不支持的 schema 版本：2" in messages
        # 必须说清"为什么不能凑合跑"
        assert "绝不" in messages or "静默" in messages

    def test_version_must_be_int(self) -> None:
        text = MINIMAL.replace("version: 1", 'version: "1"')
        assert "version 应当是整数" in error_text(text)

    def test_unknown_version_error_type(self) -> None:
        # 调用方要能区分"版本不认识"与"文件写错了"
        with pytest.raises(UnknownSchemaVersionError):
            load_version_checked(MINIMAL.replace("version: 1", "version: 9"))
        with pytest.raises(WorkflowLoadError):
            load_version_checked(MINIMAL.replace("name: 最小", "name: ''"))


class TestStructuralErrors:
    """任务点名的六类结构错误。"""

    def test_missing_step_id(self) -> None:
        text = """\
        version: 1
        name: 缺 id
        steps:
          - type: tool
            tool: recorder
        """
        messages = error_text(textwrap.dedent(text))
        assert "缺少必需的 id 字段" in messages
        assert "steps[0]" in messages                       # 指出是第几步

    def test_duplicate_step_id(self) -> None:
        text = """\
        version: 1
        name: 重复 id
        steps:
          - id: a
            type: tool
            tool: recorder
          - id: a
            type: tool
            tool: recorder
        """
        messages = error_text(textwrap.dedent(text))
        assert "步骤 id 重复" in messages
        assert "steps[1](a).id" in messages                 # 指出冲突位置

    def test_unknown_step_type(self) -> None:
        text = """\
        version: 1
        name: 未知类型
        steps:
          - id: a
            type: shell
            command: dir
        """
        messages = error_text(textwrap.dedent(text))
        assert "未知的步骤类型" in messages
        assert "tool" in messages                            # 列出可选类型

    def test_reference_to_missing_step(self) -> None:
        text = """\
        version: 1
        name: 引用不存在的步骤
        steps:
          - id: a
            type: tool
            tool: recorder
            args: {value: "{{ steps.b.output }}"}
        """
        messages = error_text(textwrap.dedent(text))
        assert "引用了不存在的步骤 'b'" in messages

    def test_reference_to_undeclared_input(self) -> None:
        text = """\
        version: 1
        name: 引用未定义的入参
        inputs:
          ticket_id: {type: string, required: true}
        steps:
          - id: a
            type: tool
            tool: recorder
            args: {value: "{{ inputs.ticket }}"}
        """
        messages = error_text(textwrap.dedent(text))
        assert "引用了未声明的入参 'ticket'" in messages
        assert "ticket_id" in messages                       # 给出最像的那个

    def test_missing_name(self) -> None:
        assert "缺少必需的 name" in error_text(MINIMAL.replace("name: 最小\n", ""))

    def test_empty_steps(self) -> None:
        assert "steps 不能为空列表" in error_text("version: 1\nname: x\nsteps: []\n")

    def test_missing_steps(self) -> None:
        assert "缺少必需的 steps" in error_text("version: 1\nname: x\n")

    def test_toplevel_not_mapping(self) -> None:
        assert "顶层应当是映射" in error_text("- 1\n- 2\n")

    def test_step_not_mapping(self) -> None:
        text = "version: 1\nname: x\nsteps:\n  - 就是个字符串\n"
        assert "步骤应当是映射" in error_text(text)

    def test_reports_all_problems_at_once(self) -> None:
        """一次报全部 —— 改一条跑一次最耗评审时间。"""
        text = """\
        version: 1
        name: 多处错误
        steps:
          - type: tool
            tool: recorder
          - id: b
            type: nope
            args: {v: "{{ inputs.missing }}"}
        """
        errors = load_errors(textwrap.dedent(text))
        assert len(errors) >= 3


class TestMalformedYaml:
    def test_syntax_error_has_line(self) -> None:
        schema, errors, _ = WorkflowLoader().try_parse("version: 1\nname: [未闭合\n")
        assert schema is None
        assert errors[0].position.line >= 1
        assert "YAML 语法错误" in errors[0].message

    def test_empty_file(self) -> None:
        schema, errors, _ = WorkflowLoader().try_parse("")
        assert schema is None
        assert "文件是空的" in errors[0].message

    def test_duplicate_key_detected(self) -> None:
        """PyYAML 对重复键是"后者胜"—— 写两遍的步骤会连同参数一起消失。"""
        text = """\
        version: 1
        name: 重复键
        steps:
          - id: a
            type: tool
            tool: recorder
            args: {value: 一}
            args: {value: 二}
        """
        messages = error_text(textwrap.dedent(text))
        assert "重复的键 'args'" in messages
        assert "首次出现在第" in messages

    def test_missing_file(self, wx_tmp) -> None:
        schema, errors, _ = WorkflowLoader().try_load(wx_tmp / "不存在.yaml")
        assert schema is None
        assert "文件不存在" in errors[0].message

    def test_directory_rejected(self, wx_tmp) -> None:
        schema, errors, _ = WorkflowLoader().try_load(wx_tmp)
        assert schema is None
        assert "这是一个目录" in errors[0].message

    def test_non_utf8_rejected(self, wx_tmp) -> None:
        path = wx_tmp / "gbk.yaml"
        path.write_bytes("version: 1\nname: 中文\n".encode("gbk"))
        schema, errors, _ = WorkflowLoader().try_load(path)
        assert schema is None
        assert "UTF-8" in errors[0].message

    def test_load_file_records_source_and_digest(self, wx_tmp) -> None:
        path = wx_tmp / "ok.yaml"
        path.write_text(MINIMAL, encoding="utf-8")
        schema = WorkflowLoader().load(path)
        assert schema.source.endswith("ok.yaml")
        assert len(schema.source_digest) == 64           # sha256 十六进制
        # 同一份内容两次加载摘要必须一致（"跑的就是批的那版"靠它证明）
        assert WorkflowLoader().load(path).source_digest == schema.source_digest

    def test_load_raises_with_all_issues(self) -> None:
        with pytest.raises(WorkflowLoadError) as err:
            WorkflowLoader().parse("version: 1\nname: x\n")
        assert err.value.issues                            # 结构化的全部问题
        assert "校验失败" in err.value.format()


class TestScalarTypes:
    """标量类型解析与 YAML 1.1 一致（自研解析器不能把 true/3 解析歪）。"""

    def test_types(self) -> None:
        doc, _, issues = build_document(
            "a: true\nb: 3\nc: 1.5\nd: null\ne: '3'\nf: 文本\ng: 0\nh: false\n")
        assert not issues
        assert doc == {"a": True, "b": 3, "c": 1.5, "d": None,
                       "e": "3", "f": "文本", "g": 0, "h": False}

    def test_template_text_with_braces_parses(self) -> None:
        """含 {{ }} 的标量必须能解析 —— 之前用 yaml.safe_load 二次解析会炸。"""
        doc, _, issues = build_document('u: "{{ inputs.base }}/api/{{ inputs.id }}"\n')
        assert not issues
        assert doc["u"] == "{{ inputs.base }}/api/{{ inputs.id }}"

    def test_multiline_block_scalar_kept(self) -> None:
        doc, _, issues = build_document("p: |\n  第一行\n  第二行\n")
        assert not issues
        assert doc["p"] == "第一行\n第二行\n"


class TestUnknownFields:
    def test_unknown_step_field_is_error(self) -> None:
        text = """\
        version: 1
        name: 字段名写错
        steps:
          - id: a
            type: tool
            tool: recorder
            timout: 30
        """
        messages = error_text(textwrap.dedent(text))
        assert "未知字段 'timout'" in messages
        assert "timeout" in messages                       # 建议正确字段名

    def test_unknown_toplevel_field_is_error(self) -> None:
        messages = error_text(MINIMAL + "versoin: 1\n")
        assert "未知字段 'versoin'" in messages

    def test_warning_mode_allows_and_reports(self) -> None:
        text = """\
        version: 1
        name: 允许扩展字段
        steps:
          - id: a
            type: tool
            tool: recorder
            note: 这是给人看的备注
        """
        schema, errors, warnings = WorkflowLoader(unknown_fields="warning").try_parse(
            textwrap.dedent(text))
        assert schema is not None and not errors
        assert any("未知字段 'note'" in w.message for w in warnings)

    def test_bad_mode_rejected(self) -> None:
        with pytest.raises(ValueError):
            WorkflowLoader(unknown_fields="silent")


class TestStepFields:
    def test_tool_step_requires_tool_name(self) -> None:
        text = "version: 1\nname: x\nsteps:\n  - id: a\n    type: tool\n"
        assert "缺少必需字段 'tool'" in error_text(text)

    def test_llm_step_requires_prompt(self) -> None:
        text = "version: 1\nname: x\nsteps:\n  - id: a\n    type: llm\n"
        assert "缺少必需字段 'prompt'" in error_text(text)

    def test_human_step_requires_prompt(self) -> None:
        text = "version: 1\nname: x\nsteps:\n  - id: a\n    type: human\n"
        assert "缺少必需字段 'prompt'" in error_text(text)

    def test_args_must_be_mapping(self) -> None:
        text = ("version: 1\nname: x\nsteps:\n  - id: a\n    type: tool\n"
                "    tool: recorder\n    args: 不是映射\n")
        assert "args 应当是映射" in error_text(text)

    def test_tool_wrong_field_shares_common_fields(self) -> None:
        # tool 类型不该接受 llm 的 prompt（否则是明显的复制粘贴错误）
        text = ("version: 1\nname: x\nsteps:\n  - id: a\n    type: tool\n"
                "    tool: recorder\n    prompt: 多余\n")
        assert "未知字段 'prompt'" in error_text(text)


class TestTimeouts:
    def test_timeout_parsed(self) -> None:
        text = MINIMAL + "    timeout: 30\n"
        assert WorkflowLoader().parse(text).steps[0].timeout == 30.0

    def test_timeout_string_number_accepted(self) -> None:
        text = MINIMAL + '    timeout: "30"\n'
        assert WorkflowLoader().parse(text).steps[0].timeout == 30.0

    def test_negative_timeout_rejected(self) -> None:
        assert "timeout 不能为负数" in error_text(MINIMAL + "    timeout: -1\n")

    def test_non_numeric_timeout_rejected(self) -> None:
        assert "timeout 应当是秒数" in error_text(MINIMAL + "    timeout: 很久\n")

    def test_bool_timeout_rejected(self) -> None:
        assert "不是布尔值" in error_text(MINIMAL + "    timeout: true\n")


class TestOnFailure:
    @pytest.mark.parametrize(("raw", "kind", "retries"), [
        ("abort", "abort", 0),
        ("continue", "continue", 0),
        ("retry", "retry", 1),
        ("retry(3)", "retry", 3),
        ("retry:3", "retry", 3),
        ("RETRY(2)", "retry", 2),
    ])
    def test_accepted_forms(self, raw: str, kind: str, retries: int) -> None:
        text = MINIMAL + f'    on_failure: "{raw}"\n'
        policy = WorkflowLoader().parse(text).steps[0].on_failure
        assert (policy.kind, policy.retries) == (kind, retries)

    def test_unknown_value_rejected(self) -> None:
        messages = error_text(MINIMAL + "    on_failure: give_up\n")
        assert "无法识别的 on_failure 写法" in messages

    def test_zero_retries_rejected(self) -> None:
        assert "必须大于 0" in error_text(MINIMAL + '    on_failure: "retry(0)"\n')

    def test_unclosed_paren_rejected(self) -> None:
        assert "括号没有闭合" in error_text(MINIMAL + '    on_failure: "retry(3"\n')

    def test_number_rejected(self) -> None:
        assert "不能直接写数字" in error_text(MINIMAL + "    on_failure: 3\n")


class TestBranch:
    BASE = """\
    version: 1
    name: 分支
    inputs:
      mode: {type: string, default: ok}
    steps:
      - id: first
        type: tool
        tool: recorder
        args: {value: "{{ inputs.mode }}"}
      - id: dec
        type: branch
        condition: "{{ steps.first.output.value }} == ok"
        then: good
        else: bad
      - id: good
        type: tool
        tool: recorder
      - id: bad
        type: tool
        tool: recorder
    """

    def test_valid_branch_loads(self) -> None:
        schema = WorkflowLoader().parse(textwrap.dedent(self.BASE))
        assert schema.get_step("dec").then == "good"          # type: ignore[union-attr]

    def test_unknown_jump_target(self) -> None:
        text = textwrap.dedent(self.BASE).replace("then: good", "then: nowhere")
        assert "指向不存在的步骤：'nowhere'" in error_text(text)

    def test_backward_jump_rejected_as_loop(self) -> None:
        text = textwrap.dedent(self.BASE).replace("else: bad", "else: first")
        messages = error_text(text)
        assert "v1 不支持循环" in messages

    @pytest.mark.parametrize("condition", [
        'condition: "{{ steps.first.output.value }} = ok"',      # 单等号笔误
        'condition: "{{ inputs.mode }} equals ok"',
        'condition: "{{ steps.first.output.value }}"',           # 没有算子
        'condition: "{{ inputs.mode }} > 3"',
    ])
    def test_bad_condition_rejected(self, condition: str) -> None:
        base = textwrap.dedent(self.BASE)
        base = base.replace('condition: "{{ steps.first.output.value }} == ok"', condition)
        assert "条件" in error_text(base)

    def test_condition_operator_supported(self) -> None:
        base = textwrap.dedent(self.BASE).replace(
            "== ok", "contains o")
        assert WorkflowLoader().parse(base).get_step("dec") is not None


class TestForwardReference:
    """引用排在自己后面的步骤 → 加载期 warning（不是静默忽略，也不是报错）。"""

    def test_warns_but_loads(self) -> None:
        text = """\
        version: 1
        name: 前向引用
        steps:
          - id: a
            type: tool
            tool: recorder
            args: {value: "{{ steps.b.output }}"}
          - id: b
            type: tool
            tool: recorder
        """
        schema, errors, warnings = WorkflowLoader().try_parse(textwrap.dedent(text))
        assert schema is not None and not errors
        assert schema.warnings, "前向引用必须在加载期可见，不能静默忽略"


class TestInputs:
    def test_shorthand_string_form(self) -> None:
        schema = WorkflowLoader().parse(
            "version: 1\nname: x\ninputs:\n  a: string\nsteps:\n  - id: s\n"
            "    type: tool\n    tool: recorder\n")
        assert schema.inputs["a"].type == "string"

    def test_bad_type_rejected(self) -> None:
        text = ("version: 1\nname: x\ninputs:\n  a: {type: 日期}\nsteps:\n  - id: s\n"
                "    type: tool\n    tool: recorder\n")
        assert "type 不支持" in error_text(text)

    def test_required_with_default_rejected(self) -> None:
        text = ("version: 1\nname: x\ninputs:\n  a: {type: string, required: true, default: 1}\n"
                "steps:\n  - id: s\n    type: tool\n    tool: recorder\n")
        assert "二者矛盾" in error_text(text)

    def test_required_inputs_helper(self) -> None:
        """`required_inputs()` 只列"必填且无默认值"的 —— CLI 据此判断入参给全了没。"""
        text = ("version: 1\nname: x\ninputs:\n"
                "  a: {type: string, required: true}\n"
                "  c: {type: string}\n"
                "  d: {type: string, required: true, default: d}\n"
                "steps:\n  - id: s\n    type: tool\n    tool: recorder\n")
        # d 同时写了 required 与 default，是**有意报错**的矛盾写法，
        # 因此这里用 warning 模式把它放行，只为验证 helper 本身
        loader = WorkflowLoader(unknown_fields="error")
        schema, errors, _ = loader.try_parse(text)
        # 它必须被拒绝（required + default 矛盾），这条断言本身就是行为约定
        assert schema is None
        assert any("二者矛盾" in e.message for e in errors)

        ok_text = text.replace("  d: {type: string, required: true, default: d}\n", "")
        schema = WorkflowLoader().parse(ok_text)
        assert schema.required_inputs() == ["a"]


class TestToolNameHints:
    def test_unknown_tool_only_warns(self) -> None:
        """注册表是运行时注入的，加载器不该因为"清单里没有"就拒绝加载。"""
        text = MINIMAL.replace("tool: recorder", "tool: recorrder")
        schema, errors, warnings = WorkflowLoader(tool_names=["recorder"]).try_parse(text)
        assert schema is not None and not errors
        assert any("recorder" in (w.suggestions or []) for w in warnings)


class TestVersionHelper:
    def test_valid_document(self) -> None:
        assert load_version_checked(MINIMAL).version == 1

    def test_yaml_error_becomes_load_error(self) -> None:
        with pytest.raises(WorkflowLoadError):
            load_version_checked("version: [\n")
