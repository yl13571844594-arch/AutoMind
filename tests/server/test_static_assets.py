"""§14.10 前端拆分回归测试 — 静态资源完整性与引用一致性。

保证拆分后的前端在工程上是自洽的：
    1. / 返回的骨架引用全部 css/js 模块；
    2. 每个被引用的静态资源都真实存在且可经 /static 伺服；
    3. HTML/JS 中所有 onclick 引用的函数在某个 JS 模块中有定义
       （防止未来增删模块时悄悄丢失函数导致运行时 ReferenceError）；
    4. JS 模块中没有遗留的 <script> 标签、CSS 模块中没有 <style> 标签。
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

STATIC = Path(__file__).resolve().parents[2] / "automind" / "static"


@pytest.fixture(scope="module")
def client():
    import automind.server as srv
    srv._AUTH_TOKEN = ""
    return TestClient(srv.app)


@pytest.fixture(scope="module")
def index_html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


def _referenced_assets(html: str) -> list[str]:
    """提取 index.html 引用的 /static/ 资源相对路径。"""
    hrefs = re.findall(r'href="/static/([^"]+)"', html)
    srcs = re.findall(r'src="/static/([^"]+)"', html)
    return hrefs + srcs


class TestSkeleton:
    def test_index_served(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "AutoMind" in r.text

    def test_references_all_modules(self, index_html):
        assets = _referenced_assets(index_html)
        css = [a for a in assets if a.endswith(".css")]
        js = [a for a in assets if a.endswith(".js")]
        assert len(css) == 5, css
        assert len(js) == 6, js
        # 加载顺序必须保持（core 最先，misc 最后）
        assert js[0] == "js/core.js" and js[-1] == "js/misc.js"

    def test_no_inline_blocks_left(self, index_html):
        # 骨架中不应再有大段内联样式/脚本块
        assert "<style>" not in index_html
        assert re.search(r"<script>(?!</script>)", index_html) is None


class TestAssetsExistAndServed:
    def test_all_referenced_assets_exist(self, index_html):
        for rel in _referenced_assets(index_html):
            assert (STATIC / rel).exists(), f"missing asset: {rel}"

    def test_assets_served_via_static_mount(self, client, index_html):
        for rel in _referenced_assets(index_html):
            r = client.get(f"/static/{rel}")
            assert r.status_code == 200, rel
            assert len(r.content) > 0, rel

    def test_no_stray_tags_inside_modules(self):
        for f in (STATIC / "js").glob("*.js"):
            text = f.read_text(encoding="utf-8")
            assert "<script" not in text.lower(), f.name
        for f in (STATIC / "css").glob("*.css"):
            text = f.read_text(encoding="utf-8")
            assert "<style" not in text.lower(), f.name


class TestCrossFileFunctionIntegrity:
    """拆分后的强校验：所有 HTML/JS 中 onclick 引用的函数必须有定义。"""

    @staticmethod
    def _defined_functions() -> set[str]:
        defined: set[str] = set()
        for f in (STATIC / "js").glob("*.js"):
            text = f.read_text(encoding="utf-8")
            defined |= set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(", text))
            defined |= set(re.findall(
                r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function|\()", text))
        return defined

    @staticmethod
    def _onclick_refs() -> set[str]:
        refs: set[str] = set()
        sources = [(STATIC / "index.html").read_text(encoding="utf-8")]
        sources += [f.read_text(encoding="utf-8") for f in (STATIC / "js").glob("*.js")]
        for text in sources:
            for m in re.findall(r'onclick="([A-Za-z_$][\w$]*)\s*\(', text):
                refs.add(m)
            for m in re.findall(r"onclick=\\?\"?([A-Za-z_$][\w$]*)\s*\(", text):
                refs.add(m)
        # 浏览器原生 API 与 JS 关键字不算自定义函数
        natives = {"event", "fetch", "alert", "confirm", "prompt", "open"}
        keywords = {"if", "for", "while", "switch", "return", "new", "typeof"}
        return refs - natives - keywords

    def test_all_onclick_targets_defined(self):
        defined = self._defined_functions()
        # document.getElementById(...).click() 一类杂音先剔除
        missing = sorted(
            r for r in self._onclick_refs()
            if r not in defined and not hasattr(str, r)
        )
        assert not missing, f"onclick 引用了未定义函数: {missing}"

    def test_init_entry_defined(self):
        defined = self._defined_functions()
        for essential in ("init", "connectWS", "sendMessage", "loadToolsView",
                          "renderPluginsList", "toast", "esc"):
            assert essential in defined, f"关键函数缺失: {essential}"
