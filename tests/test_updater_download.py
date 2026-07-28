"""在线升级的下载可靠性与校验 —— 直连 GitHub 慢且易断是"升级装不上"的主因。

锁住四件事：
    1. 镜像只套在 release 下载地址上（版本决策绝不交给第三方）；
    2. 断点续传接得对（含服务端无视 Range 的退化情形）；
    3. 坏包一定被拦下，且不会被后续续传接在错误文件后面；
    4. 安装脚本在安装失败时也要把应用拉回来。
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from automind.core import updater

REAL_URL = ("https://github.com/yl13571844594-arch/AutoMind/releases/download/"
            "v1.3.0/AutoMind-Setup-1.3.0.exe")


class FakeResp(io.BytesIO):
    """可控的 HTTP 响应桩：支持 206 续传语义与"传到一半断线"。"""

    # 每次 read 只吐一小段（真实网络就是这样），否则 256KB 的读块会一口气
    # 把测试载荷读完，"传到一半断线"根本触发不了。
    CHUNK = 256

    def __init__(self, body: bytes, status: int = 200, cut: int | None = None):
        super().__init__(body)
        self.status = status
        self.headers = {"Content-Length": str(len(body))}
        self._cut = cut
        self._served = 0

    def read(self, n: int = -1) -> bytes:
        if self._cut is not None and self._served >= self._cut:
            raise OSError("connection reset by peer")
        chunk = super().read(self.CHUNK if n is None or n < 0 else min(n, self.CHUNK))
        self._served += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _range_start(req) -> int:
    hdr = req.headers.get("Range") or req.headers.get("range") or "bytes=0-"
    return int(hdr.split("=")[1].split("-")[0])


class TestMirrorSelection:
    def test_direct_is_always_a_candidate(self, monkeypatch):
        monkeypatch.delenv("AUTOMIND_UPDATE_MIRRORS", raising=False)
        cands = updater.mirror_candidates(REAL_URL)
        assert REAL_URL in cands, "直连必须始终是候选（境外/公司网络下它最快）"
        assert len(cands) > 1, "应同时提供镜像候选"

    def test_mirrors_are_prefixes(self, monkeypatch):
        monkeypatch.delenv("AUTOMIND_UPDATE_MIRRORS", raising=False)
        for c in updater.mirror_candidates(REAL_URL):
            assert c.endswith(REAL_URL), f"镜像应为前缀拼接，得到 {c}"

    def test_api_requests_are_never_mirrored(self, monkeypatch):
        # 走镜像会把"该装哪个版本 / 校验基线是什么"的决策交给第三方
        monkeypatch.delenv("AUTOMIND_UPDATE_MIRRORS", raising=False)
        assert updater.mirror_candidates(updater._API_LATEST) == [updater._API_LATEST]

    def test_foreign_urls_untouched(self, monkeypatch):
        monkeypatch.delenv("AUTOMIND_UPDATE_MIRRORS", raising=False)
        other = "https://example.com/x.exe"
        assert updater.mirror_candidates(other) == [other]

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("AUTOMIND_UPDATE_MIRRORS", "https://m1.test,https://m2.test/")
        assert updater.mirrors() == ("https://m1.test/", "https://m2.test/")
        assert updater.mirror_candidates(REAL_URL) == [
            "https://m1.test/" + REAL_URL, "https://m2.test/" + REAL_URL]

    def test_env_direct_only(self, monkeypatch):
        monkeypatch.setenv("AUTOMIND_UPDATE_MIRRORS", "direct")
        assert updater.mirrors() == ("",)
        assert updater.mirror_candidates(REAL_URL) == [REAL_URL]

    def test_probe_picks_a_reachable_candidate(self, monkeypatch):
        """全部候选都挂时不能抛异常 —— 让正式下载去报真实错误。"""
        monkeypatch.setattr(updater, "_open",
                            lambda *_a, **_k: (_ for _ in ()).throw(OSError("no route")))
        urls = ["https://a.test/x", "https://b.test/x"]
        assert updater._probe_fastest(urls, timeout=0.3) in urls


class TestDownloadResume:
    def test_resumes_from_partial_file(self, tmp_path, monkeypatch):
        payload = b"A" * 1000
        dest = tmp_path / "setup.exe"
        dest.write_bytes(payload[:400])       # 上次断在 400 字节
        seen = {}

        def fake_open(req, timeout=None):
            seen["range"] = req.headers.get("Range")
            return FakeResp(payload[_range_start(req):], status=206)

        monkeypatch.setattr(updater, "_probe_fastest", lambda urls, **_k: urls[0])
        monkeypatch.setattr(updater, "_open", fake_open)
        n = updater._download(REAL_URL, dest, len(payload))
        assert seen["range"] == "bytes=400-", "应从已落盘处续传，而非从头再来"
        assert n == 1000 and dest.read_bytes() == payload

    def test_retries_and_completes_after_disconnect(self, tmp_path, monkeypatch):
        payload = b"B" * 2000
        dest = tmp_path / "setup.exe"
        calls = {"n": 0}

        def fake_open(req, timeout=None):
            calls["n"] += 1
            start = _range_start(req)
            # 第一次只吐 700 字节就断线，之后正常
            return FakeResp(payload[start:], status=206 if start else 200,
                            cut=700 if calls["n"] == 1 else None)

        monkeypatch.setattr(updater, "_probe_fastest", lambda urls, **_k: urls[0])
        monkeypatch.setattr(updater, "_open", fake_open)
        monkeypatch.setattr(updater.time, "sleep", lambda *_: None)
        n = updater._download(REAL_URL, dest, len(payload))
        assert calls["n"] >= 2, "断线后应自动重试"
        assert n == 2000 and dest.read_bytes() == payload

    def test_server_ignoring_range_restarts_cleanly(self, tmp_path, monkeypatch):
        """服务端无视 Range 返回 200 时必须从头写，否则两段会拼成坏文件。"""
        payload = b"C" * 500
        dest = tmp_path / "setup.exe"
        dest.write_bytes(b"X" * 200)
        monkeypatch.setattr(updater, "_probe_fastest", lambda urls, **_k: urls[0])
        monkeypatch.setattr(updater, "_open",
                            lambda _req, timeout=None: FakeResp(payload, status=200))  # noqa: ARG005
        updater._download(REAL_URL, dest, len(payload))
        assert dest.read_bytes() == payload, "不能把 200 的整包续写在残包之后"

    def test_short_download_is_retried_not_accepted(self, tmp_path, monkeypatch):
        """服务端提前 EOF（字节数不够）不能当成功返回。"""
        dest = tmp_path / "setup.exe"
        monkeypatch.setattr(updater, "_probe_fastest", lambda urls, **_k: urls[0])
        monkeypatch.setattr(updater, "_open",
                            lambda _req, timeout=None: FakeResp(b"D" * 10, status=200))  # noqa: ARG005
        monkeypatch.setattr(updater.time, "sleep", lambda *_: None)
        with pytest.raises(OSError, match="下载失败"):
            updater._download(REAL_URL, dest, 999)

    def test_gives_up_with_readable_error(self, tmp_path, monkeypatch):
        dest = tmp_path / "setup.exe"
        monkeypatch.setattr(updater, "_probe_fastest", lambda urls, **_k: urls[0])
        monkeypatch.setattr(updater, "_open",
                            lambda *_a, **_k: (_ for _ in ()).throw(OSError("boom")))
        monkeypatch.setattr(updater.time, "sleep", lambda *_: None)
        with pytest.raises(OSError, match="下载失败"):
            updater._download(REAL_URL, dest, 100)

    def test_already_complete_file_is_not_refetched(self, tmp_path, monkeypatch):
        payload = b"E" * 300
        dest = tmp_path / "setup.exe"
        dest.write_bytes(payload)
        monkeypatch.setattr(updater, "_probe_fastest", lambda urls, **_k: urls[0])
        monkeypatch.setattr(updater, "_open",
                            lambda *_a, **_k: pytest.fail("已完整的文件不该再下载"))
        assert updater._download(REAL_URL, dest, len(payload)) == 300


class TestIntegrityVerification:
    def _pkg(self, tmp_path, data: bytes):
        p = tmp_path / "AutoMind-Setup-99.0.0.exe"
        p.write_bytes(data)
        return p

    def test_accepts_matching_size_and_hash(self, tmp_path):
        data = b"good package"
        ok, msg = updater._verify_integrity(self._pkg(tmp_path, data), {
            "asset_size": len(data), "asset_sha256": hashlib.sha256(data).hexdigest()})
        assert ok and "SHA256" in msg

    def test_rejects_truncated_download(self, tmp_path):
        ok, msg = updater._verify_integrity(self._pkg(tmp_path, b"half"),
                                            {"asset_size": 999, "asset_sha256": ""})
        assert not ok and "大小不符" in msg

    def test_rejects_tampered_content(self, tmp_path):
        """镜像换掉了内容 —— 大小对得上也必须被哈希拦下。"""
        data = b"evil payload"
        ok, msg = updater._verify_integrity(self._pkg(tmp_path, data), {
            "asset_size": len(data),
            "asset_sha256": hashlib.sha256(b"good payload").hexdigest()})
        assert not ok and "校验和不符" in msg

    def test_passes_when_no_hash_published(self, tmp_path):
        data = b"pkg"
        ok, _ = updater._verify_integrity(self._pkg(tmp_path, data),
                                          {"asset_size": len(data), "asset_sha256": ""})
        assert ok, "未发布校验和时不阻断（Authenticode 签名仍会把关）"


class TestSha256Sums:
    def test_parses_by_basename(self, monkeypatch):
        digest = "a" * 64
        body = (f"{digest} *windows/AutoMind-Setup-1.3.0.exe\n"
                f"{'b' * 64} *macos/AutoMind-1.3.0.dmg\n").encode()
        monkeypatch.setattr(updater, "_open", lambda *_a, **_k: FakeResp(body))
        assets = [{"name": "SHA256SUMS",
                   "browser_download_url": "https://github.com/x/SHA256SUMS"}]
        assert updater._fetch_sha256(assets, "AutoMind-Setup-1.3.0.exe") == digest
        assert updater._fetch_sha256(assets, "nope.exe") == ""

    def test_missing_asset_is_tolerated(self):
        assert updater._fetch_sha256([], "AutoMind-Setup-1.3.0.exe") == ""

    def test_network_failure_is_tolerated(self, monkeypatch):
        monkeypatch.setattr(updater, "_open",
                            lambda *_a, **_k: (_ for _ in ()).throw(OSError("down")))
        assets = [{"name": "SHA256SUMS", "browser_download_url": "https://github.com/x"}]
        assert updater._fetch_sha256(assets, "AutoMind-Setup-1.3.0.exe") == ""


class TestInstallScript:
    def test_restarts_app_even_when_install_fails(self, tmp_path):
        """装失败也要把应用拉起来 —— 否则"点了升级就再也没回来"。"""
        bat = updater._install_script(tmp_path, tmp_path / "s.exe",
                                      "C:\\app\\AutoMind.exe", "4242")
        text = bat.read_text(encoding=updater.locale.getpreferredencoding(False) or "utf-8")
        assert "4242" in text                       # 等旧进程退出
        assert "/VERYSILENT" in text                # 静默安装
        assert "ERRORLEVEL" in text                 # 记录安装结果，失败可追溯
        assert Path("C:\\app\\AutoMind.exe").name in text
        # 重启必须在退出码之后无条件执行，不能被安装失败跳过
        assert text.index("start ") > text.index("set RC=")


class TestPlatformAssets:
    def test_all_platforms_have_asset_patterns(self):
        assert set(updater._ASSET_PATTERNS) == {"win32", "darwin", "linux"}
        assert updater._ASSET_PATTERNS["darwin"].match("AutoMind-1.3.0.dmg")
        assert updater._ASSET_PATTERNS["linux"].match("automind_1.3.0_amd64.deb")

    def test_auto_install_requires_frozen(self, monkeypatch):
        monkeypatch.setattr(updater, "_is_frozen", lambda: False)
        assert not updater.can_auto_install()
