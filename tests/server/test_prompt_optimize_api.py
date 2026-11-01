"""v1.11.20 问题优化接口：POST /api/prompt/optimize。

直接调用路由函数（``asyncio.run(api_prompt_optimize({...}))``）而不是起 TestClient：
这个接口没有任何中间件/状态依赖，函数级测试更稳也更快 —— 真正要钉住的是
**"模型失败也必须给出规则版本，接口不返回 5xx"** 这条产品契约。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from fastapi.responses import JSONResponse

import automind.server as srv

#: 一个合法的模型改写结果（带 Markdown 围栏也不影响，extract_json 会剥掉）。
_LLM_JSON = json.dumps({
    "optimized": "目标：修复启动报错\n\n请先复述你的理解，再列出必需信息与产出，最后标注假设。",
    "changes": ["补充了目标", "要求先复述理解"],
    "questions": ["是哪个入口启动时报错？"],
    "clear_enough": False,
}, ensure_ascii=False)


class _FakeLLM:
    """最小模型替身：记录收到的提示词，按配置返回文本 / 抛异常。"""

    def __init__(self, payload: str = "", *, raises: Exception | None = None):
        self.payload = payload
        self.raises = raises
        self.calls: list[list[dict]] = []

    async def generate(self, messages, tools=None, stop=None):
        self.calls.append(messages)
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(text=self.payload)


def _agent(llm):
    return SimpleNamespace(llm=llm)


def _body(resp) -> dict:
    """统一读响应体：JSONResponse 要 decode，成功路径直接是 dict。"""
    if isinstance(resp, JSONResponse):
        return json.loads(resp.body.decode("utf-8"))
    return resp


class TestValidation:
    def test_missing_text_is_400(self):
        resp = asyncio.run(srv.api_prompt_optimize({}))
        assert isinstance(resp, JSONResponse)
        assert resp.status_code == 400
        assert _body(resp)["error"] == "text 不能为空"

    def test_blank_text_is_400(self):
        resp = asyncio.run(srv.api_prompt_optimize({"text": "   \n "}))
        assert resp.status_code == 400

    def test_too_long_text_is_400(self):
        resp = asyncio.run(srv.api_prompt_optimize({"text": "改" * 4001}))
        assert resp.status_code == 400
        assert "4000" in _body(resp)["error"]


class TestLLMPath:
    def test_llm_result_is_returned(self, monkeypatch):
        llm = _FakeLLM(_LLM_JSON)
        monkeypatch.setattr(srv, "get_agent", lambda: _agent(llm))
        body = asyncio.run(srv.api_prompt_optimize(
            {"text": "帮我看看这个报错", "interaction": "coding"}))
        assert body["by"] == "llm"
        assert body["changed"] is True
        assert body["clear_enough"] is False
        assert body["notes"] == ["补充了目标", "要求先复述理解"]
        assert body["questions"] == ["是哪个入口启动时报错？"]
        assert set(body) == {
            "original", "optimized", "changed", "clear_enough", "by", "notes", "questions",
        }
        # interaction 只作为改写上下文传给模型，路由原样透传。
        assert "coding" in llm.calls[0][0]["content"]
        assert "帮我看看这个报错" in llm.calls[0][0]["content"]

    def test_llm_exception_falls_back_to_rule(self, monkeypatch):
        llm = _FakeLLM(raises=RuntimeError("provider down"))
        monkeypatch.setattr(srv, "get_agent", lambda: _agent(llm))
        body = asyncio.run(srv.api_prompt_optimize({"text": "帮我看看这个报错"}))
        assert body["by"] == "rule"
        assert body["changed"] is True
        assert body["optimized"].startswith("目标：帮我看看这个报错")

    def test_llm_non_json_falls_back_to_rule(self, monkeypatch):
        monkeypatch.setattr(srv, "get_agent", lambda: _agent(_FakeLLM("我觉得还行")))
        body = asyncio.run(srv.api_prompt_optimize({"text": "帮我看看这个报错"}))
        assert body["by"] == "rule"

    def test_text_is_stripped_before_optimizing(self, monkeypatch):
        monkeypatch.setattr(srv, "get_agent", lambda: _agent(None))
        body = asyncio.run(srv.api_prompt_optimize({"text": "  帮我看看这个报错  "}))
        assert body["original"] == "帮我看看这个报错"
        assert "帮我看看这个报错" in body["optimized"]


class TestNoLLMFallback:
    def test_llm_none_uses_rule(self, monkeypatch):
        monkeypatch.setattr(srv, "get_agent", lambda: _agent(None))
        body = asyncio.run(srv.api_prompt_optimize({"text": "帮我看看这个报错"}))
        assert body["by"] == "rule"
        assert body["changed"] is True

    def test_agent_none_uses_rule(self, monkeypatch):
        monkeypatch.setattr(srv, "get_agent", lambda: None)
        body = asyncio.run(srv.api_prompt_optimize({"text": "帮我看看这个报错"}))
        assert body["by"] == "rule"

    def test_agent_build_failure_uses_rule(self, monkeypatch):
        # Agent 还没建好/建失败不该让优化接口跟着 500 —— 规则版照样可用。
        monkeypatch.setattr(srv, "get_agent",
                            lambda: (_ for _ in ()).throw(RuntimeError("agent down")))
        body = asyncio.run(srv.api_prompt_optimize({"text": "帮我看看这个报错"}))
        assert body["by"] == "rule"
        assert body["changed"] is True

    def test_clear_question_is_returned_unchanged(self, monkeypatch):
        monkeypatch.setattr(srv, "get_agent", lambda: _agent(None))
        text = ("请把 web/src/components/chat/ChatPanel.tsx 里的发送按钮改成蓝色，"
                "并在点击后 200ms 内显示加载动画，最后用 tsc --noEmit 验证类型通过。")
        body = asyncio.run(srv.api_prompt_optimize({"text": text}))
        assert body["clear_enough"] is True
        assert body["changed"] is False
        assert body["optimized"] == text


class TestFailureContract:
    def test_value_error_becomes_400(self, monkeypatch):
        # 路由预校验之外再兜一层：optimize_prompt 抛 ValueError 也走 400。
        async def _raise(*_a, **_k):
            raise ValueError("问题不能为空")

        import automind.core.prompt_optimizer as po
        monkeypatch.setattr(po, "optimize_prompt", _raise)
        monkeypatch.setattr(srv, "get_agent", lambda: _agent(None))
        resp = asyncio.run(srv.api_prompt_optimize({"text": "帮我看看"}))
        assert isinstance(resp, JSONResponse)
        assert resp.status_code == 400
        assert _body(resp)["error"] == "问题不能为空"

    def test_unexpected_error_returns_rule_not_500(self, monkeypatch):
        # 任何非 ValueError 的意外都退回规则版本 —— 接口不返回 5xx（产品契约）。
        async def _boom(*_a, **_k):
            raise RuntimeError("unexpected")

        import automind.core.prompt_optimizer as po
        monkeypatch.setattr(po, "optimize_prompt", _boom)
        monkeypatch.setattr(srv, "get_agent", lambda: _agent(None))
        body = asyncio.run(srv.api_prompt_optimize({"text": "帮我看看这个报错"}))
        assert body["by"] == "rule"
        assert body["optimized"].startswith("目标：")


class TestRouteRegistration:
    def test_post_route_registered(self):
        route = next((r for r in srv.app.routes
                      if getattr(r, "path", "") == "/api/prompt/optimize"), None)
        assert route is not None, "POST /api/prompt/optimize 未注册"
        assert "POST" in (getattr(route, "methods", None) or set())
        assert route.endpoint is srv.api_prompt_optimize

    def test_concurrency_gate_is_two(self):
        # 优化最多 2 个并发：再多就会挤占正在执行的任务的模型额度。
        assert isinstance(srv._prompt_opt_sem, asyncio.Semaphore)
        assert srv._prompt_opt_sem._value == 2
