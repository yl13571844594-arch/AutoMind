"""repair_json / parse_tool_arguments 测试 — 截断/瑕疵 JSON 的容错。"""

from automind.core.json_utils import parse_tool_arguments, repair_json


class TestParseToolArguments:
    def test_valid_args(self):
        assert parse_tool_arguments('{"path": "a.txt", "content": "hi"}') == \
            {"path": "a.txt", "content": "hi"}

    def test_empty_and_none(self):
        assert parse_tool_arguments("") == {}
        assert parse_tool_arguments(None) == {}

    def test_unterminated_string_truncated(self):
        # 复现 "Unterminated string" —— content 字段被截断
        raw = '{"path": "demo.txt", "content": "这是一段被截断的很长内容'
        out = parse_tool_arguments(raw)
        assert isinstance(out, dict)
        assert out.get("path") == "demo.txt"
        assert "被截断" in out.get("content", "")

    def test_truncated_missing_closing_brace(self):
        raw = '{"command": "ls -la"'
        out = parse_tool_arguments(raw)
        assert out == {"command": "ls -la"}

    def test_trailing_comma(self):
        out = parse_tool_arguments('{"a": 1, "b": 2,')
        assert out == {"a": 1, "b": 2}

    def test_garbage_returns_empty_dict(self):
        assert parse_tool_arguments("not json at all") == {}

    def test_non_object_returns_empty_dict(self):
        # 工具参数必须是对象；数组等一律返回 {}
        assert parse_tool_arguments("[1, 2, 3]") == {}


class TestRepairJson:
    def test_repair_nested_truncation(self):
        out = repair_json('{"outer": {"inner": "val')
        assert out["outer"]["inner"] == "val"

    def test_repair_array_truncation(self):
        out = repair_json('[{"role": "planner", "subtask": "do x"')
        assert isinstance(out, list) and out[0]["role"] == "planner"
