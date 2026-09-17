"""出站事件（webhook）+ 外部审批回执测试。

覆盖面与"为什么这么测"：

  · **未配置 = 零动作**：默认关闭是本模块的第一承诺 —— 没配 URL 时不许有
    任何网络行为。用"传输函数被调用即失败"来证明"一个字节都没发"。
  · **载荷结构 + schema 版本**：接收方按 schema 做兼容判断，字段名一旦漂移
    就是客户侧解析炸掉，所以结构本身要被钉住。
  · **签名可被独立验证**：用标准库 hmac 从**原始 body** 重算 —— 测试里不复用
    被测代码的签名函数，否则"签名错了"会和"验签也错了"一起骗过测试。
  · **重试与最终失败**：4xx 不重试（永久失败）、5xx 重试到耗尽、成功即刻计数。
  · **队列满丢弃计数**：不静默丢事件是本模块的硬要求。
  · **URL 校验**：``file:`` 之类协议会让 urllib 去读本地文件，必须拒绝。
  · **回执往返一致**：``build_approval_receipt`` → ``ApprovalOutcome.normalize``
    → approved/arguments 必须与外部系统的意图一致（含"拒绝时不许带参数"）。
  · **本地真收包**：起一个 127.0.0.1 随机端口的 http.server 收一条真事件。
    沙箱禁止监听时 **skip 而不是 fail** —— 那是环境限制，不是代码缺陷。

全测试**默认不联网**：唯一的真网络用例只打 127.0.0.1。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from automind.core import webhooks
from automind.core.webhooks import (
    SCHEMA_VERSION,
    SIGNATURE_HEADER,
    WebhookDispatcher,
    WebhookEvent,
    WebhookSettings,
    WebhookTarget,
    approval_callback_url,
    approval_receipt_state,
    build_approval_receipt,
    build_payload,
    parse_targets,
    validate_url,
)
from automind.state.human_loop import ApprovalOutcome

# ═══════════════════════════════════════════════════════════════
# 夹具与工具
# ═══════════════════════════════════════════════════════════════


class _Capture:
    """假传输的记录器：按脚本返回状态码，并记录每次调用。"""

    def __init__(self, script: list[int] | None = None) -> None:
        self.calls: list[dict] = []
        self.script = list(script or [])

    def __call__(self, url, body, headers, timeout):  # noqa: ANN001 - 传输协议签名
        self.calls.append({"url": url, "body": body, "headers": dict(headers),
                           "timeout": timeout})
        status = self.script.pop(0) if self.script else 200
        return status, "ok" if 200 <= status < 300 else "err"


@pytest.fixture(autouse=True)
def _offline_config(monkeypatch):
    """把"配置来源"钉死，保证测试绝不依赖运行环境。

    ``_env`` 被换成受控字典 —— CI 上若有人设了 ``AUTOMIND_WEBHOOKS``，
    测试不会因为"真的去发事件"而变成不可重复的。

    **刻意不打桩 ``asyncio.sleep``**：退避等待靠 ``backoff=0`` 消除即可
    （见 :func:`_dispatcher`），而打桩睡眠会把"后台协程能否被调度"一起打坏
    —— 投递协程的等待/唤醒建立在事件循环的让出语义上，睡眠变成空实现后
    flush 会退化成忙等，后台协程一次都排不上队。打了这个桩，测的就不是
    真实时序了（这一点已在本地实测确认）。
    """
    env: dict[str, str] = {}
    monkeypatch.setattr(webhooks, "_env", lambda name, default="": env.get(name, default))
    monkeypatch.setattr(webhooks, "_cfg_attr", lambda _name, default=None: default)
    return env


def _dispatcher(capture: _Capture, script: list[int] | None = None, **kwargs):
    """构造一个钉死配置的投递器（默认单目标、有签名、退避 0）。

    退避固定 0：重试用例因此**不需要**为了跑得快去打桩 ``asyncio.sleep``。
    """
    if script is not None:
        capture.script = list(script)
    settings = WebhookSettings(
        targets=kwargs.pop("targets", [WebhookTarget(url="https://hook.example/x",
                                                     secret="topsecret")]),
        timeout=kwargs.pop("timeout", 1.0),
        max_retries=kwargs.pop("max_retries", 3),
        backoff=kwargs.pop("backoff", 0.0),
        queue_size=kwargs.pop("queue_size", 64),
        **kwargs,
    )
    return WebhookDispatcher(settings=settings, transport=capture)


# ═══════════════════════════════════════════════════════════════
# 1. 未配置 = 零动作
# ═══════════════════════════════════════════════════════════════


class TestDisabledByDefault:
    def test_no_targets_means_no_settings(self, _offline_config):
        settings = webhooks.load_settings()
        assert settings.targets == []
        assert settings.enabled is False

    def test_emit_is_inert_without_config(self, _offline_config):
        """没配 URL 时：不入队、不发请求、不抛异常、零成本。"""

        def _must_not_be_called(*a, **kw):  # pragma: no cover - 被调用即失败
            raise AssertionError("未配置目标时不应有任何投递调用")

        dispatcher = WebhookDispatcher(settings=webhooks.load_settings(),
                                       transport=_must_not_be_called)
        assert dispatcher.emit(WebhookEvent.TASK_COMPLETE,
                               {"event": "task_complete"}) == 0
        assert dispatcher.queue_depth == 0
        assert dispatcher.counters()["skipped"] == 1
        assert dispatcher.counters()["queued"] == 0

    def test_explicit_off_switch(self, _offline_config):
        """``AUTOMIND_WEBHOOKS_ENABLED=0`` 保留配置但停发。"""
        _offline_config["AUTOMIND_WEBHOOKS"] = "https://hook.example/x|s"
        _offline_config["AUTOMIND_WEBHOOKS_ENABLED"] = "0"
        settings = webhooks.load_settings()
        assert settings.targets            # 配置还在
        assert settings.enabled is False   # 但不发

    async def test_flush_is_noop_when_idle(self):
        dispatcher = WebhookDispatcher(settings=WebhookSettings(),
                                       transport=_Capture())
        assert await dispatcher.flush(timeout=0.2) is True


# ═══════════════════════════════════════════════════════════════
# 2. 配置解析
# ═══════════════════════════════════════════════════════════════


class TestConfigParsing:
    def test_json_array_form(self, _offline_config):
        _offline_config["AUTOMIND_WEBHOOKS"] = json.dumps([
            {"url": "https://a.example/hook", "secret": "s1",
             "events": ["task_complete", "approval_request"]},
            {"url": "http://127.0.0.1:9000/hook"},
        ])
        settings = webhooks.load_settings()
        assert [t.url for t in settings.targets] == [
            "https://a.example/hook", "http://127.0.0.1:9000/hook"]
        assert settings.targets[0].secret == "s1"
        assert settings.targets[0].accepts("task_complete") is True
        assert settings.targets[0].accepts("task_start") is False
        # 未声明 events 的目标订阅全部
        assert settings.targets[1].accepts("task_start") is True
        assert settings.enabled is True

    def test_compact_form(self, _offline_config):
        _offline_config["AUTOMIND_WEBHOOKS"] = (
            "https://a.example/hook|s1, https://b.example/hook")
        targets = webhooks.parse_targets(_offline_config["AUTOMIND_WEBHOOKS"])
        assert [(t.url, t.secret) for t in targets] == [
            ("https://a.example/hook", "s1"), ("https://b.example/hook", "")]

    def test_numeric_overrides(self, _offline_config):
        _offline_config.update({
            "AUTOMIND_WEBHOOKS": "https://a.example/hook",
            "AUTOMIND_WEBHOOK_TIMEOUT": "2.5",
            "AUTOMIND_WEBHOOK_RETRIES": "5",
            "AUTOMIND_WEBHOOK_BACKOFF": "0.25",
            "AUTOMIND_WEBHOOK_QUEUE_SIZE": "8",
            "AUTOMIND_WEBHOOK_BASE_URL": "https://automind.example.com/",
        })
        settings = webhooks.load_settings()
        assert (settings.timeout, settings.max_retries, settings.backoff,
                settings.queue_size) == (2.5, 5, 0.25, 8)
        assert settings.base_url == "https://automind.example.com/"

    def test_bad_numeric_falls_back(self, _offline_config):
        """配置写错不该让进程起不来：非法值回落默认。"""
        _offline_config.update({"AUTOMIND_WEBHOOKS": "https://a.example/hook",
                                "AUTOMIND_WEBHOOK_TIMEOUT": "十秒"})
        assert webhooks.load_settings().timeout == 10.0

    def test_negative_values_clamped(self, _offline_config):
        """负的重试次数/退避会让重试循环行为诡异，必须夹到 0。"""
        _offline_config.update({"AUTOMIND_WEBHOOKS": "https://a.example/hook",
                                "AUTOMIND_WEBHOOK_RETRIES": "-3",
                                "AUTOMIND_WEBHOOK_BACKOFF": "-1"})
        settings = webhooks.load_settings()
        assert settings.max_retries == 0
        assert settings.backoff == 0.0


# ═══════════════════════════════════════════════════════════════
# 3. URL 校验
# ═══════════════════════════════════════════════════════════════


class TestURLValidation:
    @pytest.mark.parametrize("bad", [
        "file:///etc/passwd",
        "ftp://host/x",
        "gopher://host/x",
        "javascript:alert(1)",
        "//host/x",              # 无协议
        "https://",              # 无主机名
        "",
        "   ",
    ])
    def test_rejects_non_http(self, bad):
        with pytest.raises(webhooks.InvalidWebhookURL):
            validate_url(bad)

    @pytest.mark.parametrize("good", [
        "https://hook.example/x",
        "http://127.0.0.1:8000/hook",
        "HTTPS://Hook.Example/X",
    ])
    def test_accepts_http_and_https(self, good):
        assert validate_url(good) == good.strip()

    def test_illegal_target_dropped_but_others_kept(self, _offline_config):
        """一个目标写错不该让另外几个也失效（否则一处笔误等于全停）。"""
        targets = parse_targets("file:///etc/passwd,https://ok.example/hook")
        assert [t.url for t in targets] == ["https://ok.example/hook"]


# ═══════════════════════════════════════════════════════════════
# 4. 载荷结构
# ═══════════════════════════════════════════════════════════════


class TestPayload:
    def test_task_complete_structure(self):
        payload = build_payload(
            WebhookEvent.TASK_COMPLETE, session_id="sess-1", task="整理季度报表",
            status="ok", elapsed=3.2, tokens={"prompt": 100, "completion": 50,
                                              "total": 150})
        assert payload["schema"] == SCHEMA_VERSION
        assert payload["event"] == "task_complete"
        assert payload["session_id"] == "sess-1"
        assert payload["task"] == "整理季度报表"
        assert payload["status"] == "ok"
        assert payload["elapsed_ms"] == 3200          # 3.2 秒 → 毫秒
        assert payload["tokens"] == {"prompt": 100, "completion": 50, "total": 150}
        assert payload["event_id"] and payload["delivery_id"]
        assert payload["timestamp"] > 0 and payload["timestamp_iso"].endswith("Z")
        # 无审批语境的事件不该凭空长出 approval 子对象
        assert "approval" not in payload
        # 必须可 JSON 序列化（否则整条事件发不出去）
        json.dumps(payload, ensure_ascii=False)

    def test_elapsed_ms_passthrough(self):
        payload = build_payload(WebhookEvent.TASK_COMPLETE, elapsed=2500)
        assert payload["elapsed_ms"] == 2500          # ≥1000 视为已是毫秒

    @pytest.mark.parametrize("event", list(WebhookEvent))
    def test_all_event_types_supported(self, event):
        payload = build_payload(event, session_id="s", task="t")
        assert payload["event"] == event.value

    def test_token_usage_accepts_object(self):
        """真实用量对象（pydantic 模型 / 轻量对象）都要能读到。"""

        class _Usage:                                  # 字段在实例上
            def __init__(self) -> None:
                self.prompt_tokens = 7
                self.completion_tokens = 3
                self.total_tokens = 10

        class _UsageOnClass:                           # 字段在类上
            total_tokens = 42

        assert build_payload(WebhookEvent.TASK_COMPLETE,
                             tokens=_Usage())["tokens"] == {
            "prompt": 7, "completion": 3, "total": 10}
        assert build_payload(WebhookEvent.TASK_COMPLETE,
                             tokens=_UsageOnClass())["tokens"] == {"total": 42}

    def test_no_tokens_key_when_unknown(self):
        """给不出数字就不发该字段 —— 发个空对象只会让接收方误以为"用量为零"。"""
        assert "tokens" not in build_payload(WebhookEvent.TASK_START)

    def test_approval_payload_carries_receipt_instructions(self, _offline_config):
        # 配置必须走夹具给的受控字典：autouse 夹具已经把 ``webhooks._env``
        # 换成了那个 dict，``monkeypatch.setenv`` 对它无效（第一版就是这么写的，
        # 表现为 callback_url 恒为空 —— 产品没错，是用例没走对入口）。
        _offline_config["AUTOMIND_WEBHOOK_BASE_URL"] = "https://automind.example.com"
        payload = build_payload(
            WebhookEvent.APPROVAL_REQUEST, session_id="sess-1", task="删除日志",
            tool="terminal", tier="danger", reason="将执行 rm -rf",
            arguments={"command": "rm -rf /var/log/*.gz", "timeout": 30},
            approval_id="abc123", timeout_s=300, on_timeout="reject",
            callback_url=approval_callback_url("abc123"))
        approval = payload["approval"]
        assert approval["approval_id"] == "abc123"
        assert approval["tool"] == "terminal"
        assert approval["tier"] == "danger"
        assert approval["arguments_summary"]["command"] == "rm -rf /var/log/*.gz"
        assert approval["timeout_s"] == 300.0
        assert approval["on_timeout"] == "reject"
        # 回执方式必须写在载荷里（收到告警的人手里只有这条载荷）
        assert approval["callback_url"] == \
            "https://automind.example.com/api/approvals/abc123"
        assert approval["callback_method"] == "POST"
        assert "X-Admin-Token" in approval["callback_auth"]
        assert "未决" in approval["callback_note"]

    def test_arguments_are_truncated_and_redacted(self):
        """载荷要发给外部系统，密钥与超长值都不能原样出去。"""
        payload = build_payload(
            WebhookEvent.APPROVAL_REQUEST, approval_id="a1",
            arguments={"cmd": "x" * 500, "api_key": "sk-" + "a" * 40})
        summary = payload["approval"]["arguments_summary"]
        assert "已截断" in summary["cmd"]
        assert "sk-" + "a" * 40 not in summary["api_key"]
        assert "REDACTED" in summary["api_key"]

    def test_arguments_key_cap(self):
        summary = webhooks.summarize_arguments({f"k{i}": i for i in range(50)})
        assert len(summary) == 21                      # 20 个键 + 1 条省略说明
        assert "另有 30 个参数未展示" in summary["…"]

    def test_task_text_truncated(self):
        payload = build_payload(WebhookEvent.TASK_START, task="很长的任务" * 500)
        assert len(payload["task"]) < 600
        assert "已截断" in payload["task"]

    def test_payload_is_json_serializable_with_odd_values(self):
        class _Weird:
            def __str__(self):
                return "weird"

        payload = build_payload(WebhookEvent.TASK_ERROR, task="t", error="boom",
                                arguments={"obj": _Weird()}, extra={"n": 1})
        json.dumps(payload, ensure_ascii=False)        # 不抛即通过
        assert payload["error"] == "boom"

    def test_error_event_carries_error_text(self):
        payload = build_payload(WebhookEvent.TASK_ERROR, status="error",
                                error="工具 terminal 连续失败 3 次")
        assert payload["status"] == "error"
        assert "连续失败" in payload["error"]


# ═══════════════════════════════════════════════════════════════
# 5. 投递、签名与重试
# ═══════════════════════════════════════════════════════════════


class TestDelivery:
    async def test_delivery_success_and_signature(self):
        capture = _Capture()
        dispatcher = _dispatcher(capture)
        payload = build_payload(WebhookEvent.TASK_COMPLETE, session_id="s1",
                                task="跑个任务", status="ok")
        assert dispatcher.emit(WebhookEvent.TASK_COMPLETE, payload) == 1
        assert await dispatcher.flush(timeout=5.0) is True
        assert len(capture.calls) == 1
        call = capture.calls[0]
        assert call["url"] == "https://hook.example/x"
        assert call["headers"]["X-AutoMind-Event"] == "task_complete"
        assert call["headers"]["X-AutoMind-Schema"] == SCHEMA_VERSION
        delivery_id = call["headers"]["X-AutoMind-Delivery"]
        assert delivery_id

        # 用标准库 hmac 从**原始 body** 独立重算（不复用被测签名函数）
        expected = hmac.new(b"topsecret", call["body"], hashlib.sha256).hexdigest()
        assert call["headers"][SIGNATURE_HEADER] == f"sha256={expected}"
        # 而且模块自己的验签函数也认（文档里给接收方抄的就是它）
        assert webhooks.verify_signature(call["body"],
                                         call["headers"][SIGNATURE_HEADER],
                                         "topsecret") is True
        # 换个密钥必须验不过（否则签名等于没做）
        assert webhooks.verify_signature(call["body"],
                                         call["headers"][SIGNATURE_HEADER],
                                         "wrong") is False

        sent = json.loads(call["body"].decode("utf-8"))
        assert sent["schema"] == SCHEMA_VERSION
        assert sent["event"] == "task_complete"
        assert sent["session_id"] == "s1"
        assert sent["delivery_id"] == delivery_id
        assert dispatcher.counters()["delivered"] == 1
        assert dispatcher.counters()["failed"] == 0
        await dispatcher.aclose()

    async def test_no_signature_header_without_secret(self):
        """没配密钥就不该发一个假的签名头（接收方会以为验签通过了）。"""
        capture = _Capture()
        dispatcher = _dispatcher(capture,
                                 targets=[WebhookTarget(url="https://h.example/x")])
        dispatcher.emit(WebhookEvent.TASK_START,
                        build_payload(WebhookEvent.TASK_START))
        await dispatcher.flush(timeout=5.0)
        assert SIGNATURE_HEADER not in capture.calls[0]["headers"]
        await dispatcher.aclose()

    async def test_retry_then_success(self):
        capture = _Capture()
        capture.script = [500, 500, 200]               # 前两次失败，第三次成功
        dispatcher = _dispatcher(capture, max_retries=3)
        dispatcher.emit(WebhookEvent.TASK_COMPLETE,
                        build_payload(WebhookEvent.TASK_COMPLETE))
        assert await dispatcher.flush(timeout=5.0) is True
        assert len(capture.calls) == 3
        counters = dispatcher.counters()
        assert counters["delivered"] == 1
        assert counters["retried"] == 2
        assert counters["failed"] == 0
        await dispatcher.aclose()

    async def test_retry_exhausted_counts_failure(self):
        capture = _Capture()
        capture.script = [503, 503, 503, 503]          # 1 次 + 3 次重试全失败
        dispatcher = _dispatcher(capture, max_retries=3)
        dispatcher.emit(WebhookEvent.TASK_ERROR,
                        build_payload(WebhookEvent.TASK_ERROR))
        assert await dispatcher.flush(timeout=5.0) is True
        assert len(capture.calls) == 4
        counters = dispatcher.counters()
        assert counters["failed"] == 1
        assert counters["delivered"] == 0
        assert counters["retried"] == 3
        assert "HTTP 503" in dispatcher.last_error
        await dispatcher.aclose()

    async def test_client_error_is_permanent(self):
        """4xx 明确拒绝 —— 重试只是浪费任务时间（403 重试 3 次还是 403）。"""
        capture = _Capture()
        capture.script = [400, 400, 400, 400]
        dispatcher = _dispatcher(capture, max_retries=3)
        dispatcher.emit(WebhookEvent.TASK_ERROR,
                        build_payload(WebhookEvent.TASK_ERROR))
        assert await dispatcher.flush(timeout=5.0) is True
        assert len(capture.calls) == 1
        assert dispatcher.counters()["failed"] == 1
        assert dispatcher.counters()["retried"] == 0
        await dispatcher.aclose()

    async def test_429_is_retryable(self):
        capture = _Capture()
        capture.script = [429, 200]
        dispatcher = _dispatcher(capture, max_retries=2)
        dispatcher.emit(WebhookEvent.TASK_START,
                        build_payload(WebhookEvent.TASK_START))
        await dispatcher.flush(timeout=5.0)
        assert len(capture.calls) == 2
        assert dispatcher.counters()["delivered"] == 1
        await dispatcher.aclose()

    async def test_network_exception_is_retried(self):
        calls = {"n": 0}

        def _boom(url, body, headers, timeout):
            calls["n"] += 1
            if calls["n"] < 2:
                raise OSError("connection refused")
            return 200, "ok"

        dispatcher = _dispatcher(_Capture())
        dispatcher._transport = _boom
        dispatcher.emit(WebhookEvent.TASK_COMPLETE,
                        build_payload(WebhookEvent.TASK_COMPLETE))
        assert await dispatcher.flush(timeout=5.0) is True
        assert calls["n"] == 2
        assert dispatcher.counters()["delivered"] == 1
        await dispatcher.aclose()

    async def test_delivery_failure_never_raises_to_caller(self):
        """投递失败不能影响任务主链路：emit 返回、flush 正常、不抛异常。"""

        def _boom(url, body, headers, timeout):
            raise RuntimeError("对端挂了")

        dispatcher = _dispatcher(_Capture(), max_retries=1)
        dispatcher._transport = _boom
        assert dispatcher.emit(WebhookEvent.TASK_COMPLETE,
                               build_payload(WebhookEvent.TASK_COMPLETE)) == 1
        assert await dispatcher.flush(timeout=5.0) is True
        assert dispatcher.counters()["failed"] == 1
        assert "对端挂了" in dispatcher.last_error
        await dispatcher.aclose()

    async def test_multiple_targets_and_event_filter(self):
        """多目标：各自独立投递；未订阅该事件的目标**在入队前就被筛掉**。

        筛选放在入队前（而不是投递时）是刻意的：不订阅的事件不该占用队列
        名额、更不该在队列满时把别的目标的额度挤掉。
        """
        capture = _Capture()
        targets = [
            WebhookTarget(url="https://a.example/hook", secret="sa"),
            WebhookTarget(url="https://b.example/hook",
                          events=frozenset({"task_error"})),
        ]
        dispatcher = _dispatcher(capture, targets=targets)
        dispatcher.emit(WebhookEvent.TASK_COMPLETE,
                        build_payload(WebhookEvent.TASK_COMPLETE))
        assert await dispatcher.flush(timeout=5.0) is True
        assert [c["url"] for c in capture.calls] == ["https://a.example/hook"]
        assert dispatcher.counters()["delivered"] == 1
        # 被筛掉的订阅关系不产生投递，也不计入 rejected（那个计数是异常信号）
        assert dispatcher.counters()["rejected"] == 0
        assert dispatcher.counters()["queued"] == 1

        dispatcher.emit(WebhookEvent.TASK_ERROR,
                        build_payload(WebhookEvent.TASK_ERROR))
        assert await dispatcher.flush(timeout=5.0) is True
        assert sorted(c["url"] for c in capture.calls) == [
            "https://a.example/hook", "https://a.example/hook",
            "https://b.example/hook"]
        await dispatcher.aclose()

    def test_target_for_filters_before_queueing(self):
        """target_for 就是"谁订阅了"的唯一判据（入队前的筛选依据）。"""
        dispatcher = _dispatcher(
            _Capture(),
            targets=[WebhookTarget(url="https://a.example/hook"),
                     WebhookTarget(url="https://b.example/hook",
                                   events=frozenset({"task_error"}))])
        assert [t.url for t in dispatcher.target_for("task_complete")] == \
            ["https://a.example/hook"]
        assert len(dispatcher.target_for(WebhookEvent.TASK_ERROR)) == 2

    async def test_unserializable_payload_fails_without_retry(self):
        """载荷序列化不了属于事件本身的问题，重试无意义。"""
        capture = _Capture()

        class _Boom:
            def __repr__(self):
                raise ValueError("不可序列化")

        dispatcher = _dispatcher(capture, max_retries=3)
        dispatcher.emit("task_complete", {"event": "task_complete", "bad": _Boom()})
        assert await dispatcher.flush(timeout=5.0) is True
        assert capture.calls == []
        assert dispatcher.counters()["failed"] == 1
        await dispatcher.aclose()


# ═══════════════════════════════════════════════════════════════
# 6. 有界队列与丢弃计数
# ═══════════════════════════════════════════════════════════════


class TestBoundedQueue:
    def test_queue_full_drops_and_counts(self, caplog):
        """队列满必须**丢弃 + 计数 + 记 warning**（不许静默、不许无限增长）。

        走同步路径：在没有事件循环的上下文里 emit，事件只进不出，队列必然填满。
        """
        dispatcher = WebhookDispatcher(
            settings=WebhookSettings(
                targets=[WebhookTarget(url="https://h.example/x")],
                queue_size=2, backoff=0.0),
            transport=_Capture())
        with caplog.at_level("WARNING"):
            accepted = [dispatcher.emit(WebhookEvent.TASK_START,
                                        build_payload(WebhookEvent.TASK_START))
                        for _ in range(6)]
        assert accepted == [1, 1, 0, 0, 0, 0]          # 满即拒，且**不抛异常**
        counters = dispatcher.counters()
        assert counters["queued"] == 2
        assert counters["dropped"] == 4
        assert dispatcher.queue_depth == 2             # 有界，不增长
        assert "webhook_queue_full" in caplog.text     # 不许静默

    async def test_queue_size_never_exceeded_under_load(self):
        capture = _Capture()
        dispatcher = _dispatcher(capture, queue_size=4)
        for _ in range(50):
            dispatcher.emit(WebhookEvent.TASK_START,
                            build_payload(WebhookEvent.TASK_START))
        assert dispatcher.queue_depth <= 4
        await dispatcher.flush(timeout=10.0)
        counters = dispatcher.counters()
        # 会计恒等式：每一条要么被投递、要么被丢弃，没有第三条路
        assert counters["queued"] + counters["dropped"] == 50
        assert dispatcher.queue_depth == 0
        await dispatcher.aclose()


# ═══════════════════════════════════════════════════════════════
# 7. 外部审批回执
# ═══════════════════════════════════════════════════════════════


class TestApprovalReceipt:
    def test_approve_roundtrip(self):
        receipt = build_approval_receipt("abc123", True, comment="工单 #42 通过")
        outcome = ApprovalOutcome.normalize(receipt)
        assert outcome.approved is True
        assert outcome.arguments is None               # 未改参数
        assert outcome.modified is False
        assert outcome.comment == "工单 #42 通过"
        assert bool(outcome) is True

    def test_approve_with_modified_arguments_roundtrip(self):
        """「改参数后批准」：arguments 必须原样传到执行器。"""
        args = {"command": "rm -rf /tmp/x", "timeout": 30}
        receipt = build_approval_receipt("abc123", True, comment="缩小范围",
                                         arguments=args)
        outcome = ApprovalOutcome.normalize(receipt)
        assert outcome.approved is True
        assert outcome.arguments == args
        assert outcome.modified is True
        assert outcome.arguments is not args          # 必须是副本，不能被外部改

    def test_deny_roundtrip(self):
        receipt = build_approval_receipt("abc123", False, comment="工单驳回")
        outcome = ApprovalOutcome.normalize(receipt)
        assert outcome.approved is False
        assert bool(outcome) is False                  # fail-closed
        assert outcome.arguments is None

    def test_deny_drops_arguments(self):
        """「拒绝 + 参数」必须退化成纯拒绝 —— 否则只读 modified 的调用点会
        把这次拒绝当成"改完参数批准了"。"""
        receipt = build_approval_receipt("abc123", False, arguments={"a": 1})
        assert receipt["arguments"] is None
        outcome = ApprovalOutcome.normalize(receipt)
        assert outcome.approved is False
        assert outcome.modified is False

    def test_empty_arguments_is_not_a_modification(self):
        assert build_approval_receipt("a", True, arguments={})["arguments"] is None
        assert ApprovalOutcome.normalize(
            build_approval_receipt("a", True, arguments={})).modified is False

    @pytest.mark.parametrize("approved,expected", [
        (True, True), (False, False),
        ("true", True), ("false", False),             # 表单字符串
        ("1", True), ("0", False),
        (1, True), (0, False),
        ("yes", True), ("no", False),
        (None, False),                                # 缺字段 → fail-closed
        ("", False),
    ])
    def test_string_booleans_are_coerced(self, approved, expected):
        """``bool("false") is True`` —— 不显式归一化就会把拒绝读成批准。"""
        receipt = build_approval_receipt("a", approved)
        assert receipt["approved"] is expected
        assert ApprovalOutcome.normalize(receipt).approved is expected

    def test_receipt_shape_is_normalize_compatible(self):
        receipt = build_approval_receipt("a", True, comment="c", arguments={"x": 1})
        assert set(receipt) == {"approval_id", "approved", "arguments", "comment"}
        assert isinstance(receipt["approved"], bool)
        assert isinstance(receipt["comment"], str)
        # normalize 忽略未知键 —— approval_id 可以安全地放在同一个 dict 里
        assert ApprovalOutcome.normalize(receipt).approved is True

    def test_receipt_survives_json_roundtrip(self):
        """回执走的是 HTTP/JSON —— 过一遍序列化后仍须归一化正确。"""
        receipt = build_approval_receipt("a", True, comment="中文备注",
                                         arguments={"n": 1, "s": "值"})
        wire = json.loads(json.dumps(receipt, ensure_ascii=False))
        outcome = ApprovalOutcome.normalize(wire)
        assert outcome.approved is True
        assert outcome.arguments == {"n": 1, "s": "值"}
        assert outcome.comment == "中文备注"


class TestApprovalCallbackURL:
    def test_explicit_base_url(self):
        assert approval_callback_url("abc123", "https://automind.example.com") == \
            "https://automind.example.com/api/approvals/abc123"

    def test_trailing_slash_normalized(self):
        assert approval_callback_url("abc", "https://x.example/") == \
            "https://x.example/api/approvals/abc"

    def test_env_base_url(self, _offline_config):
        _offline_config["AUTOMIND_WEBHOOK_BASE_URL"] = "https://env.example"
        assert approval_callback_url("abc") == "https://env.example/api/approvals/abc"

    def test_empty_without_base_url(self, _offline_config):
        """拿不到基址就返回空串 —— 返回错的地址比返回空更糟。"""
        assert approval_callback_url("abc") == ""

    def test_illegal_base_url_rejected(self, _offline_config):
        assert approval_callback_url("abc", "file:///etc") == ""

    def test_approval_id_sanitized(self):
        """id 会被拼进 URL 路径，必须清掉能改变路径语义的字符。

        断言的是"清掉了 ``/`` 与 ``..`` 这类路径分隔语义"：``..`` 这种输入会被
        过滤成 ``....``（点号本身无害，因为没有任何分隔符可用），所以这里检查
        的是**路径分隔符不存在**，而不是"点号不存在"。
        """
        url = approval_callback_url("../../etc/passwd", "https://x.example")
        assert url.startswith("https://x.example/api/approvals/")
        tail = url[len("https://x.example/api/approvals/"):]
        assert "/" not in tail and "\\" not in tail
        assert tail == "....etcpasswd"

    def test_blank_approval_id(self):
        assert approval_callback_url("", "https://x.example") == ""


class TestApprovalReceiptState:
    def test_pending_is_resolvable(self):
        state = approval_receipt_state("abc", {"abc": {"tool": "terminal"}})
        assert state["resolvable"] is True
        assert state["status"] == "pending"

    def test_stale_is_explicitly_rejected(self):
        """迟到回执要被**明确拒绝**（端点返回 409 + approval_stale），
        不许静默丢弃 —— 静默丢弃会让工单里点过批准的人以为批过了。"""
        state = approval_receipt_state("gone", {})
        assert state["resolvable"] is False
        assert state["status"] == "stale"
        assert "approval_stale" in state["reason"]
        assert "不再生效" in state["reason"]

    def test_known_override_without_mapping(self):
        """父模块若已自行查过待决表，可以直接传 known=。"""
        assert approval_receipt_state("a", known=True)["resolvable"] is True
        assert approval_receipt_state("a", known=False)["resolvable"] is False

    def test_blank_id_is_stale(self):
        assert approval_receipt_state("", {"": 1})["resolvable"] is False


class TestAdminToken:
    def test_token_must_match(self):
        assert webhooks.admin_token_ok("s3cret", "s3cret") is True
        assert webhooks.admin_token_ok("s3cret ", "s3cret") is False
        assert webhooks.admin_token_ok("", "s3cret") is False

    def test_unconfigured_token_never_passes(self):
        """管理员没配令牌时**不放行**（fail-closed）：这个端点能批准任意
        高风险工具调用，不能因为"忘了配"就变成公网上的开放审批后门。"""
        assert webhooks.admin_token_ok("anything", "") is False
        assert webhooks.admin_token_ok("", "") is False


# ═══════════════════════════════════════════════════════════════
# 8. 进程级便捷入口
# ═══════════════════════════════════════════════════════════════


class TestProcessLevelEntry:
    async def test_emit_and_stats(self, _offline_config):
        capture = _Capture()
        webhooks.reset_for_tests(
            settings=WebhookSettings(
                targets=[WebhookTarget(url="https://p.example/hook")], backoff=0.0),
            transport=capture)
        try:
            assert webhooks.emit(WebhookEvent.TASK_COMPLETE,
                                 build_payload(WebhookEvent.TASK_COMPLETE)) == 1
            assert await webhooks.flush(timeout=5.0) is True
            stats = webhooks.stats()
            assert stats["enabled"] is True
            assert stats["delivered"] == 1
            assert stats["queue_depth"] == 0
            assert len(capture.calls) == 1
        finally:
            webhooks.reset_for_tests(settings=WebhookSettings())

    def test_stats_shape_before_first_use(self):
        """端点不需要处理 None：没用过时也给一份"未配置"的形状。"""
        webhooks.reset_for_tests(settings=WebhookSettings())
        stats = webhooks.stats()
        assert stats["enabled"] is False
        for key in ("delivered", "failed", "dropped", "retried", "queued"):
            assert stats[key] == 0

    def test_set_transport_roundtrip(self):
        """注入传输是"测试与自定义通道"的挂钩，必须能装能卸。"""
        original = webhooks.get_transport()

        def _fake(url, body, headers, timeout):  # pragma: no cover - 不真调
            return 200, "ok"

        try:
            webhooks.set_transport(_fake)
            assert webhooks.get_transport() is _fake
            assert webhooks.is_custom_transport() is True
        finally:
            webhooks.set_transport(None)
        assert webhooks.get_transport() is original
        assert webhooks.is_custom_transport() is False


# ═══════════════════════════════════════════════════════════════
# 9. 真发一条：本地 http.server 收包
# ═══════════════════════════════════════════════════════════════


class _Receiver(BaseHTTPRequestHandler):
    """收集收到的请求（不打印访问日志，免得污染 pytest 输出）。"""

    received: list[dict] = []

    def do_POST(self):                                # noqa: N802 - 基类协议
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        type(self).received.append({
            "path": self.path,
            "headers": {k: v for k, v in self.headers.items()},
            "body": raw,
        })
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):                     # pragma: no cover - 静音
        return


@pytest.fixture
def local_server():
    """起一个 127.0.0.1 随机端口的小服务；**监听不了就 skip**（沙箱限制）。"""
    _Receiver.received = []
    try:
        server = HTTPServer(("127.0.0.1", 0), _Receiver)
    except (OSError, PermissionError) as e:            # WinError 5 / EPERM
        pytest.skip(f"沙箱不允许本地监听端口，跳过真收包用例：{e!r}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}/hook"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _header(headers: dict, name: str) -> str:
    """大小写不敏感地取头值。

    为什么必须这么写：头名大小写不敏感是 HTTP 的硬规定（RFC 9110），且
    CPython 的 ``http.client`` 在发送时会把 ``X-AutoMind-Signature`` 规范成
    ``X-Automind-Signature``。用裸 dict 精确匹配就等于把测试绑死在某个
    实现的拼写上 —— 那样的用例红/绿都不代表协议对不对。
    """
    want = name.lower()
    for key, value in headers.items():
        if str(key).lower() == want:
            return str(value)
    return ""


class TestRealLocalDelivery:
    async def test_real_http_post_reaches_local_server(self, local_server):
        """真发一条：证明默认传输（urllib）能把事件送到，且签名可独立验证。"""
        server, url = local_server
        port = int(server.server_address[1])
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1.0).close()
        except OSError as e:                           # pragma: no cover - 环境限制
            pytest.skip(f"本地回环连接被拒绝，跳过：{e!r}")

        webhooks.set_transport(None)                   # 用真实 urllib 传输
        dispatcher = WebhookDispatcher(settings=WebhookSettings(
            targets=[WebhookTarget(url=url, secret="sec-real")],
            timeout=5.0, max_retries=1, backoff=0.0))
        try:
            payload = build_payload(WebhookEvent.TASK_COMPLETE, session_id="real-1",
                                    task="本地真收包", status="ok")
            assert dispatcher.emit(WebhookEvent.TASK_COMPLETE, payload) == 1
            ok = await dispatcher.flush(timeout=15.0)
        finally:
            webhooks.set_transport(None)
            webhooks.reset_for_tests(settings=WebhookSettings())

        assert ok is True, f"投递未在超时内完成：{dispatcher.last_error}"
        assert len(_Receiver.received) == 1, \
            f"本地服务未收到请求：{dispatcher.last_error}"
        got = _Receiver.received[0]
        assert got["path"] == "/hook"
        expected = hmac.new(b"sec-real", got["body"], hashlib.sha256).hexdigest()
        assert _header(got["headers"], SIGNATURE_HEADER) == f"sha256={expected}"
        assert _header(got["headers"], "X-AutoMind-Event") == "task_complete"
        assert _header(got["headers"], "X-AutoMind-Schema") == SCHEMA_VERSION
        sent = json.loads(got["body"].decode("utf-8"))
        assert sent["schema"] == SCHEMA_VERSION
        assert sent["session_id"] == "real-1"
        # 投递 id 头与载荷里的 delivery_id 必须一致，接收方才能幂等去重
        assert _header(got["headers"], "X-AutoMind-Delivery") == sent["delivery_id"]
        assert dispatcher.counters()["delivered"] == 1
        await dispatcher.aclose()
