"""v1.11.19 交付图片预览修复 —— 服务端 + 前端源码级回归。

修复的 Bug：``GET /api/preview/file`` 此前对**所有**文件
``read_text(encoding="utf-8", errors="replace")`` 后以 ``text/plain`` 下发
（仅 .html/.svg 例外）—— 二进制图片先被 UTF-8 解码成替换字符
（PNG 的 ``\\x89`` → ``\\xef\\xbf\\xbd``），前端 ``PreviewModal`` 再用 iframe
打开，用户只能看到空白/乱码。本文件锁住修复后的契约：

1. **核心**：PNG 响应字节与磁盘逐字节相等 + Content-Type=image/png + no-store；
2. jpg/jpeg/gif/webp/bmp/ico 的 MIME 正确（二进制直出，不做文本解码）；
3. svg/html/txt 既有行为不回归；
4. 路径越界 403 / 敏感文件 403 / 不存在 404 / 超限 413；
5. 会话隔离工作副本里的图片也能预览（present 工具允许的范围）；
6. 前端源码级断言：图片分支必须渲染 ``<img>``，不能只有 iframe。

所有图片都在 ``tmp_path`` 里现造，不写入仓库。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import automind.server as srv  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
PREVIEW_MODAL = ROOT / "web" / "src" / "components" / "modals" / "PreviewModal.tsx"

#: 手工最小合法 PNG（1x1 透明像素，67 字节）。
#: 不依赖 pillow —— 它在项目里是可选依赖（office extra），测试不能假设已装。
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a"
    "0000000d49484452"
    "00000001000000010806000000"
    "1f15c489"
    "0000000a49444154"
    "789c6300010000050001"
    "0d0a2db4"
    "0000000049454e44ae426082"
)

IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """临时项目根 + 假 Agent；不启动 lifespan、不碰仓库 ``.automind``。

    不写成 ``with TestClient(...)``：那会跑 lifespan（连 MCP / 起调度器 / 恢复
    插件），本文件测的是端点本身的契约，与那些后台任务无关，也没必要为一条
    预览用例付启动成本。
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    fake_agent = SimpleNamespace(
        config=SimpleNamespace(project_root=str(proj)))
    monkeypatch.setattr(srv, "_agent", fake_agent)
    # 真实配置里可能配了 auth_token；预览端点本身与鉴权无关，这里显式放行
    monkeypatch.setattr(srv, "_auth_token", lambda: "")
    return TestClient(srv.app, client=("127.0.0.1", 51000)), proj


def _preview(client, path) -> object:
    return client.get("/api/preview/file", params={"path": str(path)})


# ═══════════════════════════════════════════════════════════
# 1. 核心断言：图片字节不能被破坏
# ═══════════════════════════════════════════════════════════


def test_png_served_byte_identical(client):
    """本 bug 的核心断言：响应体与磁盘 PNG 逐字节相等、类型是 image/png。

    修复前这一条必然失败：content-type 是 text/plain; charset=utf-8，
    且响应体是 UTF-8 替换字符（\\xef\\xbf\\xbd），与磁盘字节不等。
    """
    c, proj = client
    png = proj / "chart.png"
    png.write_bytes(PNG_1X1)

    r = _preview(c, png)

    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == PNG_1X1, (
        "图片响应被改写（被当成 UTF-8 文本解码过）—— 这正是无法预览的根因")
    # 长度头按真实字节数给（画面/下载都依赖它）
    assert r.headers["content-length"] == str(len(PNG_1X1))
    # 交付文件可能随时被 Agent 改写，不允许缓存住旧图
    assert "no-store" in r.headers.get("cache-control", "")
    # 既有安全头不回归
    assert r.headers.get("x-content-type-options") == "nosniff"


def test_png_with_unicode_name_previewable(client):
    """交付物常是中文文件名（Windows 路径），预览同样要能按字节取回。"""
    c, proj = client
    png = proj / "交付图.png"
    png.write_bytes(PNG_1X1)

    r = _preview(c, png)

    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == PNG_1X1


@pytest.mark.parametrize("suffix,mime", sorted(IMAGE_MIME_TYPES.items()))
def test_image_mime_types(client, suffix, mime):
    """各图片扩展名 → 正确 MIME，且二进制内容原样直出（不做文本解码）。"""
    c, proj = client
    payload = b"\x00\xffbinary:" + suffix.encode("ascii")
    img = proj / f"pic{suffix}"
    img.write_bytes(payload)

    r = _preview(c, img)

    assert r.status_code == 200
    assert r.headers["content-type"] == mime
    assert r.content == payload


def test_uppercase_extension_still_image(client):
    """相机/截图工具常给 .PNG 大写扩展名，MIME 判定必须大小写不敏感。"""
    c, proj = client
    img = proj / "SHOT.PNG"
    img.write_bytes(PNG_1X1)

    r = _preview(c, img)

    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == PNG_1X1


# ═══════════════════════════════════════════════════════════
# 2. 既有文本 / 文档行为不回归
# ═══════════════════════════════════════════════════════════


def test_html_still_rendered_as_document(client):
    c, proj = client
    html = "<!doctype html><html><body><h1>报告</h1></body></html>"
    (proj / "page.html").write_text(html, encoding="utf-8")

    r = _preview(c, proj / "page.html")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.text == html


def test_svg_still_image_svg_xml(client):
    c, proj = client
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
           '<rect width="10" height="10" fill="red"/></svg>')
    (proj / "pic.svg").write_text(svg, encoding="utf-8")

    r = _preview(c, proj / "pic.svg")

    assert r.status_code == 200
    assert r.headers["content-type"] == "image/svg+xml"
    assert r.text == svg


def test_txt_still_plain_text_utf8(client):
    c, proj = client
    (proj / "note.txt").write_text("你好，预览", encoding="utf-8")

    r = _preview(c, proj / "note.txt")

    assert r.status_code == 200
    assert r.headers["content-type"] == "text/plain; charset=utf-8"
    assert r.text == "你好，预览"


def test_markdown_still_plain_text(client):
    """MarkdownDocModal 依赖 .md 走 text/plain 原文，不能改成别的类型。"""
    c, proj = client
    (proj / "README.md").write_text("# 标题\n\n正文\n", encoding="utf-8")

    r = _preview(c, proj / "README.md")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "# 标题" in r.text


# ═══════════════════════════════════════════════════════════
# 3. 安全边界：越界 / 敏感 / 不存在 / 超限
# ═══════════════════════════════════════════════════════════


def test_directory_traversal_blocked(client):
    c, proj = client
    outside = proj.parent / "outside.png"
    outside.write_bytes(PNG_1X1)

    # 相对路径 .. 穿越
    r1 = c.get("/api/preview/file", params={"path": "../outside.png"})
    assert r1.status_code == 403
    assert "error" in r1.json()

    # 绝对路径越界
    r2 = _preview(c, outside)
    assert r2.status_code == 403


def test_sensitive_credential_file_blocked(client):
    """预览与编辑器同一道判定：.env 这类凭据文件仍 403（SCOPE_WEB）。"""
    c, proj = client
    (proj / ".env").write_text("DEEPSEEK_API_KEY=sk-secret\n", encoding="utf-8")

    r = _preview(c, proj / ".env")

    assert r.status_code == 403
    assert "error" in r.json()


def test_platform_config_file_blocked(client):
    c, proj = client
    (proj / ".automind_config.json").write_text("{}", encoding="utf-8")

    r = _preview(c, proj / ".automind_config.json")

    assert r.status_code == 403


def test_missing_file_404(client):
    c, proj = client

    r = _preview(c, proj / "missing.png")

    assert r.status_code == 404
    assert "不存在" in r.json()["error"]


def test_size_limit_returns_413(client, monkeypatch):
    """先 stat 再读：超过上限直接 413，不把整个文件读进内存。

    默认上限 25MB；这里 monkeypatch 成 1KB，避免测试真的写 25MB 磁盘。
    """
    c, proj = client
    monkeypatch.setattr(srv, "_PREVIEW_MAX_BYTES", 1024)
    big = proj / "big.png"
    big.write_bytes(b"\x89PNG" + b"0" * 4096)

    r = _preview(c, big)

    assert r.status_code == 413
    assert "过大" in r.json()["error"]


def test_default_size_limit_is_25mb():
    """上限必须真的存在且是 25MB（不是被随手改成 0 或无穷大）。"""
    assert srv._PREVIEW_MAX_BYTES == 25 * 1024 * 1024


# ═══════════════════════════════════════════════════════════
# 4. 会话隔离工作副本（present 工具允许的范围）
# ═══════════════════════════════════════════════════════════


def test_image_in_isolated_workspace_previewable(client):
    """开启 isolate_workspace 后交付物在 <项目>/.automind/workspaces/<sid>/，
    预览端点必须能读到（此前虽能过路径校验，但被当作文本解码破坏）。"""
    c, proj = client
    ws = proj / ".automind" / "workspaces" / "sess-1"
    ws.mkdir(parents=True)
    img = ws / "deliverable.png"
    img.write_bytes(PNG_1X1)

    # 绝对路径（present 事件里的形态）
    r = _preview(c, img)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == PNG_1X1

    # 相对项目根的路径同样可用
    r2 = c.get("/api/preview/file",
               params={"path": ".automind/workspaces/sess-1/deliverable.png"})
    assert r2.status_code == 200
    assert r2.content == PNG_1X1


def test_workspace_root_is_an_allowed_root(tmp_path):
    """边界 = 项目根 + 会话副本根（不是只认项目根，也不放宽到任意盘符）。"""
    proj = (tmp_path / "proj").resolve()
    proj.mkdir()
    roots = srv._preview_roots(proj)
    assert proj in roots
    assert any(p.name == "workspaces" for p in roots), "会话隔离副本必须也是允许的根"


# ═══════════════════════════════════════════════════════════
# 5. 前端源码级断言（防回归：图片不能再塞给 iframe）
# ═══════════════════════════════════════════════════════════


def test_frontend_preview_modal_renders_img_for_images():
    src = PREVIEW_MODAL.read_text(encoding="utf-8")

    assert "<img" in src, "图片分支必须用 <img> 渲染（iframe + 二进制文本响应正是本 bug）"
    assert "IMAGE_EXT_RE" in src, "需要按扩展名识别图片交付物"
    for ext in ("png", "jpe?g", "gif", "webp"):
        assert ext in src
    assert "isImage ?" in src, "图片分支与 HTML/iframe 分支必须分开"
    assert "objectFit" in src, "图片需要 object-fit: contain 才不会被拉伸"
    assert "alt=" in src, "img 必须带 alt（用文件名）"
    assert "图片加载失败" in src and "onError" in src, "加载失败要有可操作提示"


def test_frontend_titles_distinguish_image_and_html():
    src = PREVIEW_MODAL.read_text(encoding="utf-8")

    assert "🖼 图片预览" in src
    # SVG/HTML 的既有入口不能被改坏
    assert "🔍 HTML 预览" in src
    # HTML/内联截图仍走沙箱 iframe
    assert "iframe" in src
    assert "srcDoc" in src
