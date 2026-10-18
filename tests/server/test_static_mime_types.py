"""v1.11.4：前端资源的 Content-Type 不能依赖系统 MIME 表。

Windows 上 Python 的 ``mimetypes`` 会读注册表 ``HKCR\\.js\\Content Type``，不少软件把它
改成了 ``text/plain``；叠加我们的 ``X-Content-Type-Options: nosniff``，浏览器会拒绝执行
界面脚本（启动页报"页面脚本未能正常运行（HTTP 200）"），经典界面也一样打不开。
这里把系统表故意改坏，确认下发的类型仍然正确。
"""

from __future__ import annotations

import mimetypes
import re

import pytest
from fastapi.testclient import TestClient

import automind.server as srv
from automind.server_web import INDEX_CSP, WEB_MIME_TYPES, fix_static_content_type


@pytest.fixture
def poisoned(monkeypatch):
    """模拟注册表污染：.js / .css 都被映射成 text/plain。"""
    saved = {ext: mimetypes.types_map.get(ext) for ext in (".js", ".css")}
    mimetypes.add_type("text/plain", ".js")
    mimetypes.add_type("text/plain", ".css")
    assert mimetypes.guess_type("x.js")[0] == "text/plain"
    yield
    for ext, val in saved.items():
        if val:
            mimetypes.add_type(val, ext)


def _client():
    return TestClient(srv.app, client=("127.0.0.1", 51000))


def test_dist_assets_served_with_correct_types(poisoned):
    with _client() as c:
        html = c.get("/").text
        refs = re.findall(r'(?:src|href)="(/static/dist/assets/[^"?]+)', html)
        if not refs:
            pytest.skip("前端产物未构建")
        for ref in refs:
            r = c.get(ref)
            assert r.status_code == 200
            want = WEB_MIME_TYPES[ref[ref.rfind("."):]]
            assert r.headers["content-type"] == want, ref
            assert r.headers.get("x-content-type-options") == "nosniff"


def test_legacy_ui_scripts_also_fixed(poisoned):
    with _client() as c:
        refs = re.findall(r'src="(/static/[^"?]+\.js)', c.get("/legacy").text)
        if not refs:
            pytest.skip("经典界面没有外链脚本")
        for ref in refs[:5]:
            assert c.get(ref).headers["content-type"].startswith("text/javascript"), ref


def test_missing_asset_keeps_error_type():
    with _client() as c:
        r = c.get("/static/dist/assets/definitely-missing.js")
    assert r.status_code == 404
    assert not r.headers["content-type"].startswith("text/javascript")


def test_fix_only_touches_static_paths():
    class R:
        status_code = 200

        def __init__(self):
            self.headers = {"Content-Type": "application/json"}

    r = R()
    fix_static_content_type(r, "/api/files/read.js")
    assert r.headers["Content-Type"] == "application/json"
    fix_static_content_type(r, "/static/js/app.js")
    assert r.headers["Content-Type"].startswith("text/javascript")
    r2 = R()
    fix_static_content_type(r2, "/static/dir.v2/noext")
    assert r2.headers["Content-Type"] == "application/json"


def test_csp_allows_blob_self_heal():
    """启动页自愈要从 Blob URL 加载脚本。"""
    script_src = next(p for p in INDEX_CSP.split(";") if p.strip().startswith("script-src"))
    assert "blob:" in script_src
