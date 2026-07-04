"""server.py 拆分回归测试 — server_store.Store 与 server_web 纯函数。"""

import tempfile
from pathlib import Path

from automind.server_store import Store
from automind.server_web import apply_security_headers, versioned_html


class TestStore:
    def _store(self):
        s = Store(env_key_map={"openai": "OPENAI_API_KEY"})
        s.config_file = Path(tempfile.mkdtemp()) / "cfg.json"
        s.chat_file = Path(tempfile.mkdtemp()) / "chat.json"
        s.chats_dir = Path(tempfile.mkdtemp()) / "chats"
        return s

    def test_config_roundtrip(self):
        s = self._store()
        s.save_api_keys({"deepseek": "sk-1"})
        assert s.load_api_keys() == {"deepseek": "sk-1"}

    def test_config_file_setter_clears_cache(self):
        s = self._store()
        s.save_api_keys({"a": "1"})
        assert s.load_api_keys() == {"a": "1"}
        s.config_file = Path(tempfile.mkdtemp()) / "other.json"  # 换文件 → 缓存失效
        assert s.load_api_keys() == {}

    def test_provider_and_custom_models(self):
        s = self._store()
        s.save_provider_cfg("openai", api_base="https://x/v1", model="gpt-4o")
        assert s.load_providers()["openai"]["api_base"] == "https://x/v1"
        s.save_provider_cfg("openai", api_base="not-a-url")  # 非法 URL → 置空
        assert s.load_providers()["openai"]["api_base"] == ""
        s.add_custom_model("openai", "my-model")
        assert "my-model" in s.custom_models("openai")
        s.remove_custom_model("openai", "my-model")
        assert "my-model" not in s.custom_models("openai")

    def test_valid_api_base(self):
        assert Store.valid_api_base("https://x") is True
        assert Store.valid_api_base("ftp://x") is False
        assert Store.valid_api_base("") is False

    def test_env_api_key(self, monkeypatch):
        s = self._store()
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")
        assert s.env_api_key("openai") == "env-key"
        assert s.env_api_key("unknown") == ""

    def test_session_isolation(self):
        s = self._store()
        s.get_session_history("alice").append({"role": "user", "content": "hi"})
        s.save_session_history("alice")
        assert s.get_session_history("bob") == []
        # default 会话沿用单文件（向后兼容）
        assert s.session_file("default") == s.chat_file
        assert s.session_file("alice") != s.chat_file

    def test_session_id_sanitized(self):
        s = self._store()
        # 恶意 sid 不得穿越目录
        f = s.session_file("../../../etc/passwd")
        assert ".." not in str(f.name)


class _Resp:
    def __init__(self):
        self.headers = {}
        # 模拟 setdefault 语义
    class _H(dict):
        def setdefault(self, k, v):
            return super().setdefault(k, v)


class TestSecurityWeb:
    def test_security_headers_index(self):
        class R:
            headers = {}
            def __init__(self):
                self.headers = _DictWithSetdefault()
        r = R()
        apply_security_headers(r, "/")
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["X-Frame-Options"] == "SAMEORIGIN"
        assert "Content-Security-Policy" in r.headers

    def test_security_headers_non_index_no_csp(self):
        class R:
            def __init__(self):
                self.headers = _DictWithSetdefault()
        r = R()
        apply_security_headers(r, "/api/health")
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert "Content-Security-Policy" not in r.headers

    def test_versioned_html(self):
        html = '<link href="/static/css/base.css"><script src="/static/js/core.js"></script>'
        out = versioned_html(html, "1.2.3")
        assert '/static/css/base.css?v=1.2.3"' in out
        assert '/static/js/core.js?v=1.2.3"' in out


class _DictWithSetdefault(dict):
    pass
