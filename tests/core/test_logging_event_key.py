"""结构化日志的键名不该撞上适配器的位置参数（v1.7.3）。

## 修的是什么

``_StdlibStructAdapter`` 的位置参数原来就叫 ``event``：

    logger.warning("投递失败", event="task_complete")   # TypeError!

Python 会因为"位置参数已占用 event"直接抛
``TypeError: got multiple values for argument 'event'``。而 ``event`` 恰恰是
这套日志接口里**最自然的结构化键名**（本仓到处都是"记一条发生了什么事件"），
所以这个坑迟早会被踩到 —— 它已经在 webhook 投递协程里被踩过一次：
一条告警抛异常 → 整批投递被跳过 → **重试逻辑根本没执行**。

异常出现在"日志调用自身"时特别难查：调用点的业务逻辑被一起带走，而堆栈指向
的地方看起来完全无辜。所以这里既改掉实现，也把这条约束钉成用例。

（structlog 存在时不会触发 —— ``structlog`` 的 ``event`` 是**位置参数名**但
它接受 ``event=`` 作为关键字。也就是说：这个坑只在**没装 structlog** 的
环境下出现，而那是刻意支持的降级路径。）
"""

from __future__ import annotations

import logging

import pytest

from automind.core.logging import _StdlibStructAdapter


@pytest.fixture
def adapter(caplog):
    logger = logging.getLogger("automind.test.event_kw")
    logger.setLevel(logging.DEBUG)
    return _StdlibStructAdapter(logger), caplog


@pytest.mark.parametrize("level", ["debug", "info", "warning", "error", "exception"])
def test_event_keyword_is_allowed_at_every_level(adapter, level):
    """五个级别都必须能吃下 ``event=`` 这个键。"""
    ad, caplog = adapter
    with caplog.at_level(logging.DEBUG, logger="automind.test.event_kw"):
        getattr(ad, level)("投递失败", event="task_complete", target=2)

    assert any("task_complete" in r.getMessage() for r in caplog.records), \
        f"{level} 级别把 event= 键丢了"


def test_the_key_is_rendered_as_a_normal_field(adapter):
    ad, caplog = adapter
    with caplog.at_level(logging.INFO, logger="automind.test.event_kw"):
        ad.info("webhook_delivery_failed", event="approval_request", status=503)

    msg = caplog.records[-1].getMessage()

    assert msg.startswith("webhook_delivery_failed")
    assert "event='approval_request'" in msg and "status=503" in msg


def test_no_kwargs_still_works(adapter):
    ad, caplog = adapter
    with caplog.at_level(logging.INFO, logger="automind.test.event_kw"):
        ad.info("nothing_else")

    assert caplog.records[-1].getMessage() == "nothing_else"


def test_real_logger_from_get_logger_accepts_the_key():
    """经 ``get_logger`` 拿到的对象（两种实现之一）同样要吃下这个键。"""
    from automind.core.logging import get_logger

    log = get_logger("automind.test.event_kw.real")
    log.info("probe", event="task_start")        # 不应抛出
