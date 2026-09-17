"""模板渲染测试 —— 严格模式、嵌套取值、以及"绝不求值"。

这个文件里最重要的一组断言是 `TestNotAnExpression`：它们不是在测"某个功能能用"，
而是在测"某条能力**不存在**"。模板一旦能求值，工作流文件就变成了代码，
评审也就失效了（见 automind/workflow/template.py 开头的说明）。
把"不能做什么"写成测试，是为了防止将来有人顺手加上 eval。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from automind.workflow.exceptions import TemplateError
from automind.workflow.template import (
    find_placeholders,
    render,
    render_structure,
    validate_template,
)


@pytest.fixture
def context() -> dict:
    return {
        "inputs": {"ticket": "INC001", "count": 3, "flag": True, "empty": None},
        "steps": {
            "fetch": {"output": {"id": "INC001", "status": "ok",
                                 "items": [{"name": "第一项"}, {"name": "第二项"}]}},
            "boom": {"output": None, "error": "接口 500"},
        },
        "env": {"ITSM_TOKEN": "s3cret", "HOME": r"C:\Users\test"},
    }


class TestBasicRender:
    def test_inputs(self, context: dict) -> None:
        assert render("工单 {{ inputs.ticket }}", context) == "工单 INC001"

    def test_step_output(self, context: dict) -> None:
        assert render("{{ steps.fetch.output }}", context) == render(
            "{{ steps.fetch.output }}", context)          # 幂等且不抛

    def test_step_error(self, context: dict) -> None:
        assert render("失败原因：{{ steps.boom.error }}", context) == "失败原因：接口 500"

    def test_env(self, context: dict) -> None:
        assert render("token={{ env.ITSM_TOKEN }}", context) == "token=s3cret"

    def test_multiple_placeholders(self, context: dict) -> None:
        # 这一条专门防"把多占位符误判成单个占位符"的回归：
        # `PLACEHOLDER.fullmatch` 会从第一个 `{{` 一路匹配到最后一个 `}}`，
        # 于是 "{{a}}/x/{{b}}" 会被当成一个占位符 —— 实测踩过这个坑。
        got = render("{{ inputs.ticket }}@{{ env.HOME }}", context)
        assert got == r"INC001@C:\Users\test"

    def test_no_placeholder_returns_text(self, context: dict) -> None:
        assert render("没有任何变量", context) == "没有任何变量"

    def test_non_string_returned_as_is(self, context: dict) -> None:
        assert render(42, context) == 42                     # type: ignore[arg-type]
        assert render(None, context) is None                 # type: ignore[arg-type]

    def test_empty_none_renders_empty(self, context: dict) -> None:
        # None 是**声明过**的可选入参值，渲染成空串是既定行为（不是"未定义"）
        assert render("值=[{{ inputs.empty }}]", context) == "值=[]"

    def test_escaped_placeholder_is_literal(self, context: dict) -> None:
        # 给 LLM 的提示词里要示范模板语法时，得能写出字面量花括号
        assert render(r"\{{ inputs.ticket }}", context) == "{{ inputs.ticket }}"


class TestTypePreservation:
    """整串就是一个占位符时保留原类型 —— 否则工具会拿到字符串数字。"""

    def test_whole_template_keeps_int(self, context: dict) -> None:
        value = render("{{ inputs.count }}", context)
        assert value == 3 and isinstance(value, int)

    def test_whole_template_keeps_bool(self, context: dict) -> None:
        assert render("{{ inputs.flag }}", context) is True

    def test_whole_template_keeps_dict(self, context: dict) -> None:
        value = render("{{ steps.fetch.output }}", context)
        assert isinstance(value, dict) and value["id"] == "INC001"

    def test_with_prefix_becomes_string(self, context: dict) -> None:
        assert render("n={{ inputs.count }}", context) == "n=3"

    def test_render_structure_keeps_nested_types(self, context: dict) -> None:
        out = render_structure(
            {"n": "{{ inputs.count }}", "list": ["{{ inputs.ticket }}"],
             "deep": {"token": "{{ env.ITSM_TOKEN }}"}}, context)
        assert out == {"n": 3, "list": ["INC001"], "deep": {"token": "s3cret"}}


class TestNestedAccess:
    def test_dict_key(self, context: dict) -> None:
        assert render("{{ steps.fetch.output.id }}", context) == "INC001"

    def test_list_index(self, context: dict) -> None:
        assert render("{{ steps.fetch.output.items[1].name }}", context) == "第二项"

    def test_quoted_key(self, context: dict) -> None:
        ctx = {"inputs": {"a b": "x"}}
        assert render("{{ inputs['a b'] }}", ctx) == "x"

    def test_missing_dict_key_raises_with_available_keys(self, context: dict) -> None:
        with pytest.raises(TemplateError) as err:
            render("{{ steps.fetch.output.nope }}", context)
        message = err.value.format()
        assert "取不到键 'nope'" in message
        assert "id" in message                      # 附上该层可用键
        assert "status" in message

    def test_missing_list_index_raises(self, context: dict) -> None:
        with pytest.raises(TemplateError) as err:
            render("{{ steps.fetch.output.items[9] }}", context)
        assert "下标越界" in err.value.format()

    def test_scalar_continue_raises(self, context: dict) -> None:
        with pytest.raises(TemplateError) as err:
            render("{{ steps.fetch.output.id.deeper }}", context)
        assert "不能在这里继续取值" in err.value.format()

    def test_wrong_root_key_suggests(self, context: dict) -> None:
        with pytest.raises(TemplateError) as err:
            render("{{ step.fetch.output }}", context)
        assert "step" in err.value.format() or "step" in err.value.suggestions


class TestStrictMode:
    """严格模式：宁可当场停，也不要把空串送到远端系统上。"""

    def test_unknown_input_raises(self, context: dict) -> None:
        with pytest.raises(TemplateError) as err:
            render("{{ inputs.nope }}", context)
        assert "nope" in err.value.message
        assert err.value.available                    # 必须给出可用变量清单

    def test_unknown_step_raises(self, context: dict) -> None:
        with pytest.raises(TemplateError):
            render("{{ steps.nosuch.output }}", context)

    def test_unknown_env_raises(self, context: dict) -> None:
        with pytest.raises(TemplateError):
            render("{{ env.NOT_SET_ANYWHERE }}", context)

    def test_never_renders_empty_string_on_missing(self, context: dict) -> None:
        # 这条是"严格模式"的核心：不许悄悄变成空串
        with pytest.raises(TemplateError):
            render("https://itsm/api/tickets/{{ inputs.missing }}", context)

    def test_explicit_non_strict_still_possible_for_probing(self, context: dict) -> None:
        # 非严格模式只用于内部探测场景；默认路径永远严格
        assert render("{{ inputs.nope }}", context, strict=False) == ""

    def test_real_env_var_available(self, context: dict) -> None:
        os.environ["AUTOMIND_WORKFLOW_TEST"] = "yes"
        try:
            ctx = {"env": dict(os.environ)}
            assert render("{{ env.AUTOMIND_WORKFLOW_TEST }}", ctx) == "yes"
        finally:
            os.environ.pop("AUTOMIND_WORKFLOW_TEST", None)


class TestNotAnExpression:
    """这些必须**失败**：模板不是求值引擎，也就不是代码执行通道。"""

    @pytest.mark.parametrize("text", [
        "{{ 1 + 1 }}",
        "{{ inputs.count + 1 }}",
        "{{ inputs.a or inputs.b }}",
        "{{ os.system('echo hi') }}",
        "{{ ''.__class__ }}",
        "{{ __import__('os').getcwd() }}",
        "{{ steps.fetch.output.id.upper() }}",
        "{{ 'a' if inputs.flag else 'b' }}",
        "{{ inputs.count > 1 }}",
    ])
    def test_expression_is_rejected(self, text: str, context: dict) -> None:
        with pytest.raises(TemplateError) as err:
            render(text, context)
        assert "不支持表达式求值" in err.value.format()

    def test_no_side_effect_from_rejected_expression(self, context: dict) -> None:
        """被拒绝的表达式不能留下任何痕迹（证明真的没有执行它）。"""
        marker = Path.cwd() / "should_not_exist_workflow_test.txt"
        with pytest.raises(TemplateError):
            render(f"{{{{ __import__('pathlib').Path(r'{marker}').write_text('x') }}}}",
                   context)
        assert not marker.exists()

    def test_placeholder_count_capped(self, context: dict) -> None:
        text = "{{ inputs.ticket }}" * 300
        with pytest.raises(TemplateError) as err:
            find_placeholders(text)
        assert "占位符过多" in err.value.message


class TestStaticValidation:
    """加载期静态校验：能在跑之前发现的，绝不留给执行期。"""

    def test_ok(self) -> None:
        assert validate_template("{{ inputs.a }} {{ steps.s.output }}",
                                 inputs={"a"}, step_ids={"s"}) == []

    def test_unknown_input(self) -> None:
        problems = validate_template("{{ inputs.ticket }}", inputs={"ticket_id"},
                                     step_ids=set())
        assert [p.kind for p in problems] == ["unknown_input"]
        # 名字写错要给出最像的那个 —— 否则用户得自己去翻 inputs 段
        assert problems[0].suggestions == ["ticket_id"]

    def test_unknown_step(self) -> None:
        problems = validate_template("{{ steps.nope.output }}", inputs=set(),
                                     step_ids={"fetch"})
        assert problems[0].kind == "unknown_step"

    def test_bad_field(self) -> None:
        problems = validate_template("{{ steps.fetch.result }}", inputs=set(),
                                     step_ids={"fetch"})
        assert problems[0].kind == "syntax"
        assert "output" in problems[0].suggestions

    def test_bad_root(self) -> None:
        problems = validate_template("{{ ticket }}", inputs=set(), step_ids=set())
        assert problems[0].kind == "syntax"
        assert "inputs" in problems[0].suggestions

    def test_lowercase_env_rejected(self) -> None:
        problems = validate_template("{{ env.home }}", inputs=set(), step_ids=set())
        assert problems[0].kind == "bad_env"

    def test_incomplete_steps_reference(self) -> None:
        problems = validate_template("{{ steps.fetch }}", inputs=set(), step_ids={"fetch"})
        assert problems[0].kind == "syntax"

    def test_find_placeholders_lists_variables(self) -> None:
        tokens = find_placeholders("{{ inputs.a }} 与 {{ steps.b.output }}")
        assert [t.render_path() for t in tokens] == ["inputs.a", "steps.b.output"]
