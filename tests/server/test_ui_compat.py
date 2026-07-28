"""界面加载兼容性回归 — 锁住"升级后部分电脑界面未能加载"的修复。

线上根因（已复现确认）：入口页引用带内容哈希的 ``index-<hash>.js``；旧版本
对 HTML 只下发 ``no-cache``，HTML 仍可落盘缓存。升级到新版本后 WebView2/Edge
可能仍吐出**上一版的 HTML**，而它引用的旧哈希 JS 已随新版本删除 → 404 →
用户看到"⚠ 界面未能加载"。

三道防线，本文件逐条锁住：
    1. HTML 文档 ``no-store``       —— 不落盘就不可能读到旧 HTML（根因修复）；
    2. dist 产物完整性守卫          —— 引用的哈希文件缺失时回退经典界面；
    3. ``/legacy`` 兼容界面 + 前端预检/自愈 —— 老内核与已污染机器的兜底。
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automind.server_web import cache_control_for, dist_asset_refs

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "automind" / "static"
DIST = STATIC / "dist"


@pytest.fixture(scope="module")
def client():
    import automind.server as srv
    srv._AUTH_TOKEN = ""
    return TestClient(srv.app)


class TestCachePolicy:
    """缓存策略（纯函数层）。"""

    @pytest.mark.parametrize("path", ["/", "", "/legacy", "/manual"])
    def test_html_documents_are_never_stored(self, path):
        # no-store 是根因修复：HTML 一旦落盘，升级后就可能读到旧版本
        assert "no-store" in cache_control_for(path)

    def test_hashed_assets_immutable(self):
        cc = cache_control_for("/static/dist/assets/index-Y3944XEH.js")
        assert "immutable" in cc and "max-age=31536000" in cc

    def test_unhashed_static_revalidates(self):
        # 经典界面的 css/js 无内容哈希 → 必须每次协商，不能长缓存
        cc = cache_control_for("/static/css/base.css")
        assert cc == "no-cache"
        assert "immutable" not in cc

    def test_api_paths_untouched(self):
        assert cache_control_for("/api/health") is None


class TestCacheHeadersOverHTTP:
    """缓存策略（实际响应头）。"""

    def test_index_no_store(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "no-store" in r.headers["cache-control"]
        assert r.headers["pragma"] == "no-cache"

    def test_legacy_no_store(self, client):
        r = client.get("/legacy")
        assert r.status_code == 200
        assert "no-store" in r.headers["cache-control"]

    @pytest.mark.skipif(not DIST.exists(), reason="前端未构建")
    def test_hashed_asset_immutable(self, client):
        asset = next(iter((DIST / "assets").glob("*.js")), None)
        assert asset is not None, "dist/assets 下应有构建产物"
        r = client.get(f"/static/dist/assets/{asset.name}")
        assert r.status_code == 200
        assert "immutable" in r.headers["cache-control"]


@pytest.mark.skipif(not (DIST / "index.html").exists(), reason="前端未构建")
class TestDistIntegrity:
    """产物完整性 —— index.html 与 assets 必须来自同一次构建。"""

    def test_referenced_assets_all_exist(self):
        html = (DIST / "index.html").read_text(encoding="utf-8")
        refs = dist_asset_refs(html)
        assert refs, "dist/index.html 应引用构建产物"
        missing = [r for r in refs if not (STATIC / r[len("/static/"):]).exists()]
        assert not missing, f"index.html 引用了不存在的产物（发布即 404）：{missing}"

    def test_served_index_references_live_assets(self, client):
        """/ 返回的页面里每个产物 URL 都能真的取到 —— 直接复现线上故障场景。"""
        html = client.get("/").text
        for ref in dist_asset_refs(html):
            assert client.get(ref).status_code == 200, f"{ref} 取不到"

    def test_index_falls_back_when_assets_missing(self, client, monkeypatch):
        """产物缺失时回退经典界面，而不是把 404 页面丢给用户。"""
        import automind.server as srv
        monkeypatch.setattr(srv, "_missing_dist_assets",
                            lambda _html: ["/static/dist/assets/gone.js"])
        html = client.get("/").text
        assert "gone.js" not in html
        assert 'id="sidebar"' in html   # 经典界面骨架


class TestLegacyRoute:
    """兼容版界面 —— 老内核 / 自愈链的最终落点。"""

    def test_legacy_serves_classic_ui(self, client):
        html = client.get("/legacy").text
        assert 'id="sidebar"' in html and 'id="main"' in html
        # 经典界面不依赖 ES 模块产物
        assert "/static/dist/" not in html

    def test_legacy_has_no_module_scripts(self, client):
        # 老内核跑到这里必须能用：出现 type="module" 就说明兜底无效
        assert 'type="module"' not in client.get("/legacy").text


@pytest.mark.skipif(not (DIST / "index.html").exists(), reason="前端未构建")
class TestBootFallback:
    """入口页自带的预检与自愈逻辑（构建后仍须保留在产物里）。"""

    @pytest.fixture(scope="class")
    @classmethod
    def html(cls) -> str:
        return (DIST / "index.html").read_text(encoding="utf-8")

    def test_syntax_probe_isolated(self, html):
        # 语法探针必须独立成块：与主逻辑同块时老内核会一起解析失败，兜底全废
        assert "window.__AM_SYNTAX_OK__ = false;" in html
        head, _, tail = html.partition("window.__AM_SYNTAX_OK__ = false;")
        assert "</script>" in tail.split("window.__AM_SYNTAX_OK__ = (")[0]

    def test_preflight_redirects_old_kernels(self, html):
        assert "'noModule' in document.createElement('script')" in html
        assert "/legacy" in html

    def test_self_heal_reload_is_guarded(self, html):
        # 一次性：无 sessionStorage 守卫会导致 404 时无限刷新
        assert "sessionStorage" in html
        assert "_cb=" in html

    def test_no_eval_in_boot_script(self, html):
        # 页面 CSP 未开 unsafe-eval，用 eval/new Function 探测会误判现代浏览器
        assert "new Function(" not in html
        assert "eval(" not in html
