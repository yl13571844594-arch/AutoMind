"""Web 层持久化存储 — 从 server.py 抽出的配置 / API Key / 对话历史 / 会话隔离。

以 `Store` 类内聚原先分散的模块级全局（`_config_file`/`_CHAT_FILE`/`_CHATS_DIR`/
`_session_histories` 与配置 TTL 缓存）与其读写函数。server.py 实例化单例并保留
同名委托别名，故 40+ 路由调用点无需改动；测试改为覆写 `store.config_file` 等属性。

`config_file` 用属性封装：赋新值时自动失效缓存，比旧的裸全局更安全。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path


class Store:
    """Web 层持久化状态容器（配置 / API Key / 提供商 / 对话与会话历史）。"""

    CONFIG_CACHE_TTL = 2.0  # 秒

    def __init__(self, env_key_map: dict[str, str] | None = None) -> None:
        self._config_file = Path(".automind_config.json")
        self.chat_file = Path(".automind") / "chat_history.json"
        self.chats_dir = Path(".automind") / "chats"
        self.session_histories: dict[str, list] = {}
        self._env_key_map = env_key_map or {}
        self._config_cache: dict | None = None
        self._config_cache_time: float = 0.0

    # ── config_file：赋值即失效缓存 ──
    @property
    def config_file(self) -> Path:
        return self._config_file

    @config_file.setter
    def config_file(self, value) -> None:
        self._config_file = Path(value)
        self._config_cache = None
        self._config_cache_time = 0.0

    # ── 配置读写（TTL 缓存）──
    def read_config(self) -> dict:
        now = time.time()
        if self._config_cache is not None and (now - self._config_cache_time) < self.CONFIG_CACHE_TTL:
            return self._config_cache
        if self._config_file.exists():
            try:
                self._config_cache = json.loads(self._config_file.read_text(encoding="utf-8"))
            except Exception:
                self._config_cache = {}
        else:
            self._config_cache = {}
        self._config_cache_time = now
        return self._config_cache

    def write_config(self, data: dict) -> None:
        self._config_file.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        self._config_cache = data
        self._config_cache_time = time.time()

    # ── API Key / 提供商 ──
    def load_api_keys(self) -> dict[str, str]:
        return self.read_config().get("api_keys", {})

    def save_api_keys(self, keys: dict[str, str]) -> None:
        data = self.read_config()
        data["api_keys"] = keys
        self.write_config(data)

    def load_providers(self) -> dict[str, dict]:
        return self.read_config().get("providers", {})

    @staticmethod
    def valid_api_base(val: str) -> bool:
        return bool(val) and (val.startswith("http://") or val.startswith("https://"))

    def save_provider_cfg(self, provider: str, api_base: str | None = None,
                          model: str | None = None) -> None:
        data = self.read_config()
        entry = data.setdefault("providers", {}).setdefault(provider, {})
        if api_base is not None:
            entry["api_base"] = api_base if self.valid_api_base(api_base) else ""
        if model is not None:
            entry["model"] = model
        self.write_config(data)

    def load_active(self) -> dict:
        return self.read_config().get("active", {})

    def save_active(self, **kwargs) -> None:
        data = self.read_config()
        active = data.setdefault("active", {})
        active.update({k: v for k, v in kwargs.items() if v is not None})
        self.write_config(data)

    def load_mode_models(self) -> dict:
        return self.read_config().get("mode_models", {})

    def mode_model(self, interaction: str) -> dict | None:
        mm = self.load_mode_models().get(interaction)
        if isinstance(mm, dict) and mm.get("provider") and mm.get("model"):
            return {"provider": mm["provider"], "model": mm["model"]}
        return None

    def env_api_key(self, provider: str) -> str:
        import os
        return os.environ.get(self._env_key_map.get(provider, ""), "")

    def custom_models(self, provider: str) -> list[str]:
        return self.load_providers().get(provider, {}).get("custom_models", [])

    def add_custom_model(self, provider: str, model: str) -> None:
        data = self.read_config()
        entry = data.setdefault("providers", {}).setdefault(provider, {})
        models = entry.setdefault("custom_models", [])
        if model and model not in models:
            models.append(model)
        self.write_config(data)

    def remove_custom_model(self, provider: str, model: str) -> None:
        data = self.read_config()
        entry = data.get("providers", {}).get(provider, {})
        models = entry.get("custom_models", [])
        if model in models:
            models.remove(model)
            self.write_config(data)

    # ── 对话历史（default 会话单文件，向后兼容）──
    def load_chat_history(self) -> list[dict]:
        if self.chat_file.exists():
            try:
                return json.loads(self.chat_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return []

    def save_chat_history(self, history: list[dict]) -> None:
        try:
            self.chat_file.parent.mkdir(parents=True, exist_ok=True)
            self.chat_file.write_text(
                json.dumps(history[-200:], ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ── 多用户会话隔离 ──
    def session_file(self, sid: str) -> Path:
        if not sid or sid == "default":
            return self.chat_file
        safe = re.sub(r"[^A-Za-z0-9_-]", "", sid)[:48] or "default"
        return self.chats_dir / f"{safe}.json"

    def get_session_history(self, sid: str) -> list:
        sid = sid or "default"
        if sid not in self.session_histories:
            f = self.session_file(sid)
            try:
                self.session_histories[sid] = (
                    json.loads(f.read_text(encoding="utf-8")) if f.exists() else [])
            except Exception:
                self.session_histories[sid] = []
        return self.session_histories[sid]

    def save_session_history(self, sid: str) -> None:
        sid = sid or "default"
        hist = self.session_histories.get(sid, [])[-200:]
        self.session_histories[sid] = hist
        f = self.session_file(sid)
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
