"""extract_json 单元测试 — 覆盖三层容错策略。"""

from automind.core.json_utils import extract_json


class TestExtractJson:
    def test_direct_valid_object(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_direct_valid_array(self):
        assert extract_json('[1, 2, 3]') == [1, 2, 3]

    def test_json_in_code_fence(self):
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_json_in_plain_fence(self):
        assert extract_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_json_amidst_text(self):
        assert extract_json('Here is the plan:\n{"goal": "test"}\nEnd.') == {"goal": "test"}

    def test_empty_string(self):
        assert extract_json("") is None

    def test_none_input(self):
        assert extract_json(None) is None

    def test_balanced_array_in_text(self):
        assert extract_json("text [1,2,3] more") == [1, 2, 3]

    def test_nested_braces(self):
        assert extract_json('{"outer": {"inner": 1}}') == {"outer": {"inner": 1}}

    def test_unicode_chinese(self):
        assert extract_json('{"目标": "测试"}') == {"目标": "测试"}

    def test_escaped_quotes_in_balanced_slice(self):
        assert extract_json('{"key": "val\\"ue"}') == {"key": 'val"ue'}

    def test_brace_inside_string_not_breaking_slice(self):
        assert extract_json('Sure! {"note": "has } brace"} tail') == {"note": "has } brace"}

    def test_non_json_text_returns_none(self):
        assert extract_json("Hello, world!") is None
