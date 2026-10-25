"""v1.11.14 IM Markdown 适配层：各平台语法降级 / 标题规范 / 安全分片（纯函数、不联网）。"""

from __future__ import annotations

import re

import pytest

from automind.bots.md_format import (
    CHANNEL_LIMITS,
    adapt_for_channel,
    normalize_channel,
    plain_text,
    render_title,
    split_message,
)

ALL_CHANNELS = ("feishu", "dingtalk", "wecom", "wecom_bot", "wechat_mp", "webhook")

TABLE = """| 项目 | 状态 |
| --- | --- |
| 构建 | 完成 |
| 部署 | 失败 |
"""

RICH_MD = """# 周报

| 项目 | 状态 |
| --- | --- |
| 构建 | 完成 |

> 注意：这是引用

- 一级
  - 嵌套
1. 有序

**加粗** 与 `inline` 与 [文档](https://example.com/a?x=1&y=2)

```python
def f(**kwargs):
    return 1
```

<img src="x"> HTML 块
"""


def test_normalize_channel():
    assert normalize_channel("Feishu") == "feishu"
    assert normalize_channel(" lark ") == "feishu"
    assert normalize_channel("DingTalk") == "dingtalk"
    assert normalize_channel("wecom_app") == "wecom"
    assert normalize_channel("wechat") == "wechat_mp"
    assert normalize_channel(None) == ""
    assert normalize_channel("mystery") == "mystery"


def test_empty_and_weird_inputs_never_raise():
    for channel in (*ALL_CHANNELS, "mystery"):
        for md in ("", None, "#", "```", "```python", "\r\n\r\n", "| a |", "x" * 5000, "\x00"):
            out = adapt_for_channel(channel, md)
            assert isinstance(out, str)
    for title in ("", None, "**粗**", "\n\n", "很" * 100):
        assert isinstance(render_title("dingtalk", title), str)


@pytest.mark.parametrize("channel", ["feishu", "dingtalk", "wecom", "wecom_bot"])
def test_markdown_channels_degrade_tables(channel):
    out = adapt_for_channel(channel, TABLE)
    assert "| ---" not in out and "| 项目 |" not in out
    assert "**项目**：构建" in out and "**状态**：失败" in out


def test_feishu_adapts_tables_html_and_keeps_code_fence():
    out = adapt_for_channel("feishu", RICH_MD)
    assert "<img" not in out and "<div>" not in out
    assert "HTML 块" in out
    assert "```python" in out and "def f(**kwargs):" in out
    assert "# 周报" in out
    assert "**加粗**" in out and "`inline`" in out
    assert "https://example.com/a?x=1&y=2" in out


def test_dingtalk_keeps_heading_levels_and_images():
    out = adapt_for_channel("dingtalk", "##### 五级标题\n\n![图](https://x/y.png)")
    assert "##### 五级标题" in out
    assert "![图](https://x/y.png)" in out


def test_wecom_fences_become_quotes_and_images_become_links():
    out = adapt_for_channel("wecom", RICH_MD)
    assert "```" not in out
    assert "> 代码块（python）：" in out
    assert "> def f(**kwargs):" in out
    assert ">     return 1" in out
    assert "[图片：图](https://x/y.png)" not in out            # RICH_MD 里没有图片
    img = adapt_for_channel("wecom", "![图](https://x/y.png)")
    assert "[图片：图](https://x/y.png)" in img


def test_wecom_bot_keeps_fence_but_degrades_table():
    out = adapt_for_channel("wecom_bot", RICH_MD)
    assert "```python" in out and "def f(**kwargs):" in out
    assert "| ---" not in out and "**项目**：构建" in out


def test_wechat_mp_plain_text_structure():
    out = adapt_for_channel("wechat_mp", RICH_MD)
    assert out.startswith("【周报】")
    assert "• 项目：构建 · 状态：完成" in out
    assert "│ 注意：这是引用" in out
    assert "• 一级" in out and "  • 嵌套" in out
    assert "1. 有序" in out
    assert "加粗 与 inline 与 文档（https://example.com/a?x=1&y=2）" in out
    assert "```" not in out
    assert "def f(**kwargs):" in out                           # 代码内容原样保留
    assert "HTML 块" in out and "<img" not in out


def test_fenced_code_content_is_never_rewritten():
    md = ('```html\n<div class="x"><a href="https://evil">click</a></div>\n'
          "![图](https://example.com/i.png)\n| a | b |\n| --- | --- |\n```")
    for channel in (*ALL_CHANNELS, "mystery"):
        out = adapt_for_channel(channel, md)
        assert '<div class="x">' in out
        assert '<a href="https://evil">click</a>' in out
        assert "![图](https://example.com/i.png)" in out
        assert "| --- |" in out


def test_plain_text_task_list_and_only_title():
    out = plain_text("- [ ] 待办\n- [x] 已完成\n\n# 仅标题")
    assert "☐ 待办" in out and "☑ 已完成" in out
    assert "【仅标题】" in out
    assert plain_text("# 仅标题") == "【仅标题】"
    assert plain_text("") == ""


def test_plain_text_keeps_code_content_verbatim():
    md = "```\n**not bold** and `not code`\n```"
    out = plain_text(md)
    assert "**not bold** and `not code`" in out


def test_webhook_and_unknown_channel_preserve_raw_markdown():
    md = "# 标题\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n```py\nx\n```\n\n<div>h</div>"
    assert adapt_for_channel("webhook", md) == md
    assert adapt_for_channel("mystery-channel", md) == md
    assert adapt_for_channel("", md) == md


@pytest.mark.parametrize("channel", ["feishu", "dingtalk", "wecom", "wecom_bot"])
def test_markdown_adaptation_is_idempotent(channel):
    once = adapt_for_channel(channel, RICH_MD)
    assert adapt_for_channel(channel, once) == once


@pytest.mark.parametrize("channel", ["feishu", "dingtalk", "wecom", "wecom_bot", "wechat_mp"])
def test_html_links_and_image_src_are_kept(channel):
    md = '<a href="https://example.com/doc">说明</a> 与 <img src="https://example.com/p.png">'
    out = adapt_for_channel(channel, md)
    assert "https://example.com/doc" in out
    assert "https://example.com/p.png" in out
    assert "<a " not in out and "<img" not in out


@pytest.mark.parametrize("channel", ALL_CHANNELS)
def test_urls_are_never_lost(channel):
    md = ("[文档](https://example.com/a?x=1&y=2)\n\n<https://example.com/b>\n\n"
          "![图](https://example.com/c.png)")
    out = adapt_for_channel(channel, md)
    assert "https://example.com/a?x=1&y=2" in out
    assert "https://example.com/b" in out
    assert "https://example.com/c.png" in out


def test_crlf_and_unclosed_fence_are_deterministic():
    out = adapt_for_channel("feishu", "# 标题\r\n\r\n正文\r\n")
    assert "\r" not in out and out.startswith("# 标题")
    feishu = adapt_for_channel("feishu", "前言\n\n```python\nprint(1)")
    assert "前言" in feishu and "print(1)" in feishu
    wecom = adapt_for_channel("wecom", "前言\n\n```python\nprint(1)")
    assert "> print(1)" in wecom and "```" not in wecom
    mp = plain_text("```python\nprint(1)")
    assert "print(1)" in mp and "```" not in mp


def test_render_title_per_channel():
    assert render_title("feishu", "**发布** 报告") == "发布 报告"
    assert render_title("feishu", "`code` [链接](https://x)") == "code 链接"
    assert render_title("feishu", "<b>发布</b> 报告") == "发布 报告"
    assert render_title("feishu", "") == ""
    assert len(render_title("dingtalk", "很长的标题" * 10)) <= 30
    assert render_title("dingtalk", "第一行\n第二行") == "第一行"
    assert render_title("wecom", "周报") == "## 周报"
    assert render_title("wecom_bot", "周报") == "## 周报"
    assert render_title("wechat_mp", "周报") == "【周报】"
    assert render_title("webhook", "**原始**") == "**原始**"


def test_title_in_body_only_for_body_title_channels():
    assert adapt_for_channel("wecom", "正文", title="周报").startswith("## 周报\n")
    assert adapt_for_channel("wecom_bot", "正文", title="周报").startswith("## 周报")
    assert adapt_for_channel("wechat_mp", "**正文**", title="周报").startswith("【周报】\n")
    # 飞书 / 钉钉的标题走平台独立字段，不重复拼进正文
    assert "周报" not in adapt_for_channel("feishu", "正文", title="周报")


# ═══════════════════════════════════════════════════════════════
# split_message
# ═══════════════════════════════════════════════════════════════


def test_split_short_and_empty():
    assert split_message("hello", 100) == ["hello"]
    assert split_message("", 100) == [""]
    assert split_message("abc", 0) == ["abc"]
    assert split_message("abc", -5) == ["abc"]


def test_split_respects_utf8_byte_limit_and_is_lossless():
    text = "中" * 500
    parts = split_message(text, 100)
    assert len(parts) > 1
    assert all(len(p.encode("utf-8")) <= 100 for p in parts)
    stripped = [re.sub(r"\n?（续 \d+/\d+）$", "", p) for p in parts]
    assert "".join(stripped) == text


def test_split_prefers_paragraph_boundaries():
    text = "第一段内容\n\n第二段内容\n\n第三段内容"
    assert split_message(text, 22) == ["第一段内容", "第二段内容", "第三段内容"]


def test_split_prefers_list_item_boundaries():
    text = "- 甲甲甲\n- 乙乙乙\n- 丙丙丙"
    parts = split_message(text, 17)
    assert len(parts) == 3
    assert all(p.startswith("- ") for p in parts)


def test_split_never_cuts_inside_fence_and_rewraps():
    body = "\n".join(f"line{i}: " + "x" * 30 for i in range(10))
    md = "前言\n\n```python\n" + body + "\n```\n\n结尾"
    parts = split_message(md, 200, marker=False)
    assert len(parts) > 1
    assert all(len(p.encode("utf-8")) <= 200 for p in parts)
    recovered: list[str] = []
    for part in parts:
        assert part.count("```") % 2 == 0, part      # 每片围栏都闭合，不会漏出代码
        for inner in re.findall(r"```python\n(.*?)\n```", part, re.DOTALL):
            recovered.extend(inner.split("\n"))
    assert "\n".join(recovered) == body
    assert any("前言" in p for p in parts) and any("结尾" in p for p in parts)


def test_split_single_oversized_line():
    line = "x" * 1000
    parts = split_message(line, 120, marker=False)
    assert len(parts) > 1
    assert all(len(p.encode("utf-8")) <= 120 for p in parts)
    assert "".join(parts) == line


def test_split_adds_continuation_marker():
    text = "\n\n".join("段落" + str(i) + "：" + "甲" * 60 for i in range(1, 4))
    parts = split_message(text, 200)
    assert len(parts) >= 2
    for i, part in enumerate(parts[1:], start=2):
        assert f"（续 {i}/{len(parts)}）" in part
    no_marker = split_message(text, 200, marker=False)
    assert all("续" not in part for part in no_marker)


def test_split_channel_aware_adapts_before_splitting():
    md = TABLE + "\n\n" + "正文" * 300
    parts = split_message(md, 120, channel="wecom")
    assert all(len(p.encode("utf-8")) <= 120 for p in parts)
    assert all("| ---" not in p and "```" not in p for p in parts)
    joined = "".join(parts)
    assert "**项目**：构建" in joined and "**状态**：失败" in joined


def test_split_clamps_to_platform_hard_cap():
    parts = split_message("x" * 3000, 99999, channel="wecom", marker=False)
    assert len(parts) > 1
    assert all(len(p.encode("utf-8")) <= CHANNEL_LIMITS["wecom"] for p in parts)
    assert "".join(parts) == "x" * 3000
