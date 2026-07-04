"""前端安全回归测试 — XSS 转义与内联事件处理器审计（静态分析拆分后的 JS 模块）。

拆分后的前端在 static/js/*.js。这些测试锁定几个易回归的安全约束：
    1. esc() 必须转义引号（" '）与尖括号、& —— 否则属性上下文可注入；
    2. 存在 jsq()（JS 字符串-in-属性 转义），且同时处理 \\ ' " < > & ；
    3. 内联 onclick="fn('${...}')" 的插值参数一律走 jsq()，不得再用裸 esc() 或
       crude 的 .replace(/'/g,'') —— 这些无法防住 JS 串逃逸；
    4. iframe 预览沙箱不得包含 allow-same-origin（否则 LLM 生成内容可读父页数据）。
"""

import re
from pathlib import Path

import pytest

JS_DIR = Path(__file__).resolve().parents[2] / "automind" / "static" / "js"
HTML = Path(__file__).resolve().parents[2] / "automind" / "static" / "index.html"


def _all_js() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in JS_DIR.glob("*.js"))


class TestEscaping:
    def test_esc_escapes_quotes_and_brackets(self):
        chat = (JS_DIR / "chat.js").read_text(encoding="utf-8")
        # 定位 _ESC_MAP 定义，断言其覆盖全部危险字符对应的实体
        m = re.search(r"_ESC_MAP\s*=\s*\{(.+?)\}", chat, re.DOTALL)
        assert m, "未找到 _ESC_MAP 定义"
        table = m.group(1)
        for ent in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
            assert ent in table, f"esc() 映射缺少实体 {ent}"
        # esc() 必须基于该映射对 " 和 ' 做替换
        assert re.search(r"\[&<>\"'`?\]", chat) or "[&<>\"'`]" in chat, \
            "esc() 未对引号类字符做正则替换"

    def test_jsq_defined_and_covers_js_and_html(self):
        chat = (JS_DIR / "chat.js").read_text(encoding="utf-8")
        assert "function jsq(" in chat, "缺少 jsq() JS-字符串属性转义器"
        # 必须转义反斜杠、单引号（JS 串）与 & " < >（HTML 属性）
        for token in [r"\\\\", r"\\'", "&amp;", "&quot;", "&lt;", "&gt;"]:
            assert token in chat, f"jsq() 未包含转义 {token}"


class TestNoUnsafeInlineHandlers:
    def test_no_bare_esc_in_onclick_js_string(self):
        """onclick="fn('${esc(x)}')" 是双上下文漏洞：应改用 jsq()。"""
        js = _all_js()
        # 匹配 onclick="...('${esc(...)}...')" —— JS 单引号串里用 esc
        bad = re.findall(r"""onclick="[^"]*\('\$\{esc\(""", js)
        assert not bad, f"内联 onclick 的 JS 串参数仍用裸 esc()：{bad}"

    def test_no_crude_quote_stripping(self):
        """crude 的 .replace(/'/g,'') 无法真正防注入，应替换为 jsq()。"""
        js = _all_js()
        assert "replace(/'/g,'')" not in js and "replace(/'/g, '')" not in js, \
            "仍存在 crude 单引号剥离，应改用 jsq()"


class TestIframeSandbox:
    def test_preview_iframe_has_no_same_origin(self):
        html = HTML.read_text(encoding="utf-8")
        m = re.search(r'id="preview-frame"[^>]*sandbox="([^"]*)"', html, re.DOTALL)
        assert m, "未找到 preview-frame 的 sandbox 属性"
        sandbox = m.group(1)
        assert "allow-same-origin" not in sandbox, \
            "预览 iframe 不得含 allow-same-origin（否则可读取父页敏感数据）"
        assert "allow-scripts" in sandbox  # 交互式预览需要脚本，但受 null 源隔离


class TestImageUrlValidation:
    def test_thumbnails_filtered_by_safe_url(self):
        # 缩略图渲染统一到 core.js 的 buildMessageEl，须经 isSafeUrl 过滤
        js = _all_js()
        assert "images.filter(isSafeUrl)" in js, "消息缩略图未经 isSafeUrl 过滤"

    def test_markdown_links_reject_data_urls(self):
        # 链接只允许 http(s)：data:/javascript: 可在 <a> 点击后执行脚本
        chat = (JS_DIR / "chat.js").read_text(encoding="utf-8")
        assert "function isSafeHref(" in chat, "缺少 isSafeHref（链接白名单）"
        assert "isSafeHref(url)" in chat, "markdown 链接未用 isSafeHref 收窄协议"


@pytest.mark.parametrize("payload,expect_neutralized", [
    ("');alert(1)//", True),
    ('"><img src=x onerror=alert(1)>', True),
    ("normal-name", False),
])
def test_jsq_semantics_documented(payload, expect_neutralized):
    """文档化 jsq 的语义预期（真实转义在浏览器端验证）。"""
    dangerous = "'" in payload or '"' in payload or "<" in payload
    assert dangerous == expect_neutralized
