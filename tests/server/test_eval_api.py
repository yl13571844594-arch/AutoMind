"""v1.11.24 Web 评测接口 —— 必须用工作台当前接入的模型/Key。

## 修的是什么

工作台 8765 已经接入 deepseek-flash 并能正常对话，但 ``/api/eval/run`` 的
dry-run 一直报 ``openai/gpt-4o 未配置``：评测接口把请求里可选的 provider/model
传下去，其余落到评测模块自己的默认 ``AgentConfig``，与工作台当前配置无关。

## 这里钉住四件事（全部离线，不调真实模型）

1. 不显式传 provider/model 时，评测目标跟随 ``get_agent().config.llm``；
2. 当前 Agent 上的 Key / 已保存 Key / 环境变量 Key 都能让评测"可运行"；
3. 一处 Key 都没有 → 409（``code=llm_not_configured``），dry-run 仍可看计划；
4. 任何响应 / 报告都不含 Key 明文（只报告来源标签 ``api_key_source``）。

后台任务用 ``_call_and_drain`` 在同一事件循环里跑完，保证"落库的 job"可断言。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse

import automind.server as srv
from automind.core.config import LLMProviderConfig

#: 测试用假 Key —— 任何响应/报告里出现它都算泄露
SECRET = "sk-web-eval-secret-0123456789"
SECRET_OPENAI = "sk-openai-eval-secret-9876543210"


# ═══════════════════════════════════════════════════════════
# 替身 / 工具
# ═══════════════════════════════════════════════════════════


def _llm(provider: str = "deepseek", model: str = "deepseek-flash",
         api_key: str = SECRET,
         api_base: str = "https://api.deepseek.com/v1",
         temperature: float = 0.35, max_tokens: int = 4096) -> LLMProviderConfig:
    """真实的 LLMProviderConfig（服务端辅助函数会调用 model_copy 等真方法）。"""
    return LLMProviderConfig(provider=provider, model=model, api_key=api_key,
                             api_base=api_base, temperature=temperature,
                             max_tokens=max_tokens)


def _agent(llm: LLMProviderConfig | None = None):
    return SimpleNamespace(config=SimpleNamespace(llm=llm or _llm()))


def _body(resp) -> dict:
    """统一读响应体：JSONResponse 要 decode，成功路径直接是 dict。"""
    if isinstance(resp, JSONResponse):
        return json.loads(resp.body.decode("utf-8"))
    return resp


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _clear_provider_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    from automind.core.provider_resolver import ENV_KEY_MAP

    for name in sorted(set(ENV_KEY_MAP.values())):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _isolation(monkeypatch: pytest.MonkeyPatch):
    """每个用例：干净 job 表 + 默认"没有任何 Key"（用例按需覆盖）。"""
    srv._eval_jobs.clear()
    monkeypatch.setattr(srv, "_load_api_keys", lambda: {})
    # 参数名以下划线开头：autouse 桩只关心"没有环境 Key"
    monkeypatch.setattr(srv, "_env_api_key", lambda _provider: "")
    monkeypatch.setattr(srv, "_load_providers", lambda: {})
    yield
    srv._eval_jobs.clear()


async def _call_and_drain(data: dict):
    """调用路由并把它创建的后台任务在本循环里跑完（否则 job 永远 running）。"""
    result = await srv.api_eval_run(data)
    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return result


def _dry_run(monkeypatch: pytest.MonkeyPatch, data: dict) -> dict:
    return _body(asyncio.run(_call_and_drain({"suite": "smoke",
                                              "dry_run": True, **data})))


# ═══════════════════════════════════════════════════════════
# 1. dry-run：跟随工作台当前模型/Key
# ═══════════════════════════════════════════════════════════


class TestDryRunTarget:
    def test_follows_current_agent_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(srv, "get_agent", lambda: _agent())
        body = _dry_run(monkeypatch, {})

        assert body["dry_run"] is True
        assert body["cases"], "dry-run 仍应列出任务清单"
        target = body["target"]
        assert target["provider"] == "deepseek", (
            "评测没有跟随工作台当前的 provider（又退回 openai 了）")
        assert target["model"] == "deepseek-flash"
        assert target["available"] is True
        assert target["api_key_source"] == "web:agent"
        assert SECRET not in _dump(body), "dry-run 响应泄露了 Key"

    def test_saved_key_is_used_when_agent_has_none(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(srv, "get_agent", lambda: _agent(_llm(api_key="")))
        monkeypatch.setattr(srv, "_load_api_keys", lambda: {"deepseek": SECRET})
        body = _dry_run(monkeypatch, {})

        target = body["target"]
        assert target["available"] is True
        assert target["api_key_source"] == "web:saved"
        assert SECRET not in _dump(body)

    def test_env_key_is_used_and_reported_as_source(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_provider_envs(monkeypatch)
        monkeypatch.setattr(srv, "get_agent", lambda: _agent(_llm(api_key="")))
        monkeypatch.setattr(
            srv, "_env_api_key",
            lambda provider: SECRET if provider == "deepseek" else "")
        body = _dry_run(monkeypatch, {})
        target = body["target"]
        assert target["available"] is True
        assert target["api_key_source"] == "env:DEEPSEEK_API_KEY"
        assert SECRET not in _dump(body)


# ═══════════════════════════════════════════════════════════
# 2. 没有 Key：dry-run 仍可用；真跑 409（不是"模型退化"）
# ═══════════════════════════════════════════════════════════


class TestNoKey:
    def test_dry_run_without_key_still_returns_plan(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_provider_envs(monkeypatch)
        monkeypatch.setattr(srv, "get_agent", lambda: _agent(_llm(api_key="")))
        body = _dry_run(monkeypatch, {})
        target = body["target"]
        assert target["available"] is False
        assert target["reason"], "不可用时必须给出可照做的原因"
        assert body["cases"]

    def test_run_without_key_is_409(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_provider_envs(monkeypatch)
        monkeypatch.setattr(srv, "get_agent", lambda: _agent(_llm(api_key="")))
        resp = asyncio.run(_call_and_drain({"suite": "smoke"}))

        assert isinstance(resp, JSONResponse)
        assert resp.status_code == 409
        body = _body(resp)
        assert body["code"] == "llm_not_configured"
        assert "deepseek" in body["error"], body["error"]
        assert SECRET not in _dump(body)


# ═══════════════════════════════════════════════════════════
# 3. 显式覆盖 provider/model
# ═══════════════════════════════════════════════════════════


class TestExplicitOverride:
    def test_explicit_provider_and_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(srv, "get_agent", lambda: _agent())
        monkeypatch.setattr(srv, "_load_api_keys", lambda: {"openai": SECRET_OPENAI})
        monkeypatch.setattr(srv, "_load_providers", lambda: {
            "openai": {"model": "gpt-4o-mini",
                       "api_base": "https://relay.example.com/v1"}})
        body = _dry_run(monkeypatch, {"provider": "openai", "model": "gpt-4o-mini"})

        target = body["target"]
        assert target["provider"] == "openai"
        assert target["model"] == "gpt-4o-mini"
        assert target["available"] is True
        assert target["api_key_source"] == "web:saved"
        dumped = _dump(body)
        assert SECRET not in dumped and SECRET_OPENAI not in dumped

    def test_explicit_provider_resolves_saved_provider_model(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        """只给 provider 时，模型按已保存的提供商配置/内置默认补齐。"""
        monkeypatch.setattr(srv, "get_agent", lambda: _agent())
        monkeypatch.setattr(srv, "_load_api_keys", lambda: {"openai": SECRET_OPENAI})
        monkeypatch.setattr(srv, "_load_providers",
                            lambda: {"openai": {"model": "gpt-4o-mini"}})
        body = _dry_run(monkeypatch, {"provider": "openai"})
        target = body["target"]
        assert target["provider"] == "openai"
        assert target["model"] == "gpt-4o-mini"
        assert target["available"] is True

    def test_explicit_same_provider_keeps_current_model(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        """只重申当前 provider 时不该把工作台的 deepseek-flash 换成该家默认模型。"""
        monkeypatch.setattr(srv, "get_agent", lambda: _agent())
        body = _dry_run(monkeypatch, {"provider": "deepseek"})
        target = body["target"]
        assert target["provider"] == "deepseek"
        assert target["model"] == "deepseek-flash"
        assert target["available"] is True


# ═══════════════════════════════════════════════════════════
# 4. 真跑路径：凭据传给执行器，报告/响应不含 Key
# ═══════════════════════════════════════════════════════════


def _patch_run_suite(monkeypatch: pytest.MonkeyPatch, captured: dict,
                     payload: dict | None = None,
                     error: Exception | None = None) -> None:
    """替换 ``automind.eval.runner.run_suite``（接口内部是运行时 import）。"""
    import automind.eval.runner as runner

    async def fake_run_suite(suite, executor, **kwargs):
        captured["executor"] = executor
        captured["kwargs"] = kwargs
        if error is not None:
            raise error

        class _Report:
            def as_dict(self):
                return dict(payload or {})

        return _Report()

    monkeypatch.setattr(runner, "run_suite", fake_run_suite)


class TestRunPath:
    def test_run_forwards_agent_credentials_and_scrubs_report(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(srv, "get_agent", lambda: _agent())
        captured: dict = {}
        # 故意在报告里塞 Key：验证"落库前统一脱敏"真的生效
        payload = {"suite": "smoke", "passed": True, "total": 1,
                   "model": "deepseek-flash", "provider": "deepseek",
                   "cases": [],
                   "notes": [f"Authorization: Bearer {SECRET}"],
                   "abort_reason": f"last_error={SECRET}"}
        _patch_run_suite(monkeypatch, captured, payload=payload)

        resp = asyncio.run(_call_and_drain({"suite": "smoke"}))

        assert SECRET not in _dump(resp), "启动响应泄露了 Key"
        assert resp["target"]["provider"] == "deepseek"
        assert resp["target"]["api_key_source"] == "web:agent"

        ex = captured["executor"]
        assert ex.provider == "deepseek" and ex.model == "deepseek-flash"
        assert ex.api_key == SECRET, "执行器没拿到工作台当前 Key"
        assert ex.api_base == "https://api.deepseek.com/v1"
        assert ex.temperature == pytest.approx(0.35)
        assert ex.max_tokens == 4096
        # v1.11.24：评测默认走 ReAct（否则 AgentResult 只有步骤摘要，
        # contains 类断言会系统性误判）
        assert ex.execution_mode == "react"
        kwargs = captured["kwargs"]
        assert kwargs["model"] == "deepseek-flash"
        assert kwargs["provider"] == "deepseek"
        assert kwargs["target"].available is True

        job = srv._eval_jobs[resp["job_id"]]
        assert job["status"] == "done"
        assert job["target"]["provider"] == "deepseek"
        assert SECRET not in _dump(job), "job/报告泄露了 Key"
        assert "***" in _dump(job["report"]["notes"]), "报告里的 Key 没有被脱敏"

        # GET /api/eval/report 返回的就是这个 job：同样不能有 Key
        report_resp = asyncio.run(srv.api_eval_report(resp["job_id"]))
        assert SECRET not in _dump(report_resp)

    def test_background_error_is_scrubbed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(srv, "get_agent", lambda: _agent())
        captured: dict = {}
        _patch_run_suite(monkeypatch, captured,
                         error=RuntimeError(f"auth failed: {SECRET}"))

        resp = asyncio.run(_call_and_drain({"suite": "smoke"}))
        assert SECRET not in _dump(resp)

        job = srv._eval_jobs[resp["job_id"]]
        assert job["status"] == "error"
        assert SECRET not in _dump(job), "后台错误信息泄露了 Key"
        assert "***" in job["error"]

    def test_eval_run_does_not_change_server_agent_mode(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        """评测执行器默认 ReAct，**只影响评测 Agent**；服务端当前 Agent 的运行
        模式一个字节都不能动（否则用户的工作台会在测评后被切走模式）。"""
        from automind.core.types import ExecutionMode

        agent = _agent()
        agent.config.execution = SimpleNamespace(mode="plan_and_execute")
        agent._mode = ExecutionMode.PLAN_AND_EXECUTE
        monkeypatch.setattr(srv, "get_agent", lambda: agent)
        captured: dict = {}
        _patch_run_suite(monkeypatch, captured, payload={"passed": True})

        asyncio.run(_call_and_drain({"suite": "smoke"}))

        assert captured["executor"].execution_mode == "react"
        assert agent.config.execution.mode == "plan_and_execute", (
            "评测请求改了服务端 Agent 的配置模式")
        assert agent._mode is ExecutionMode.PLAN_AND_EXECUTE, (
            "评测请求改了服务端 Agent 的运行模式")
