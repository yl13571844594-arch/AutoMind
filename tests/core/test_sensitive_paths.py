"""平台密钥与数据不许被 Web 端点/工具读走（v1.7.3）。

## 修的是什么

Web 侧的文件端点只校验"路径在不在 project_root 之内"，而默认 project_root
**就是启动目录** —— 平台的密钥与数据恰好躺在那里，且是明文：

    GET /api/files/read?path=.automind_config.json   → 全部提供商 API Key
    GET /api/files/read?path=.automind/automind.db   → 全部会话历史
    POST /api/files/write {path: ".automind_config.json", content: ...}
                                                     → 改成攻击者的 api_base，
                                                       之后每一次对话的提示词都被导走

实测确认（审计阶段）：``_editor_target`` 只做 root 归属判断，没有任何拒绝清单。

## 两级粒度是刻意的

* **Web 端点（编辑器/预览/diff）**：平台密钥 + 用户凭据文件（``.env``/``.ssh``/
  ``.aws``/``*.pem``…）一律不给读 —— 这些端点没有读凭据的正当用途。
* **Agent 的文件工具**：只挡**平台自身**的密钥与数据目录。用户项目里的
  ``.env`` 不挡 —— 读项目文件是 Agent 的本职，"帮我看看 .env 缺哪项"是正常
  任务；挡掉它只会让正常任务失败，却挡不住数据外发（那由用户选的模型决定）。

还有一个显式逃生门：``AUTOMIND_ALLOW_SENSITIVE_FILE_READ=1``。
"""

from __future__ import annotations

import pytest

from automind.core.sensitive import SCOPE_TOOL, SCOPE_WEB, is_denied, reason


@pytest.fixture(autouse=True)
def _no_override(monkeypatch):
    monkeypatch.delenv("AUTOMIND_ALLOW_SENSITIVE_FILE_READ", raising=False)


# ═══════════════════════════════════════════════════════════
# 1. 平台自身的密钥与数据：两个 scope 都挡
# ═══════════════════════════════════════════════════════════


@pytest.mark.parametrize("path", [
    ".automind_config.json",
    "C:/proj/.automind_config.json",
    ".automind/automind.db",
    "C:/proj/.automind/traces/default/run1.jsonl",
    ".automind/checkpoints/x.json",
    ".automind_license",
    "pro/.license-private/private_key.hex",
    "private_key.hex",
])
def test_platform_secrets_are_denied_for_both_scopes(path):
    assert is_denied(path, SCOPE_WEB), f"{path} 竟然可被 Web 端点读取"
    assert is_denied(path, SCOPE_TOOL), f"{path} 竟然可被文件工具读取"


def test_denial_reason_tells_the_user_how_to_opt_out():
    msg = reason(".automind_config.json", SCOPE_WEB) or ""

    assert "API Key" in msg, "要说清为什么被拒（否则用户只会以为坏了）"
    assert "AUTOMIND_ALLOW_SENSITIVE_FILE_READ" in msg, "要留一条明确的出路"


# ═══════════════════════════════════════════════════════════
# 2. 用户自己的凭据：只在 Web 端点挡
# ═══════════════════════════════════════════════════════════


@pytest.mark.parametrize("path", [
    ".env", ".env.local", ".env.production",
    ".ssh/id_rsa", "home/.aws/credentials", ".kube/config",
    "certs/server.pem", "keys/app.key", ".git-credentials", ".netrc",
])
def test_web_endpoints_do_not_serve_user_credentials(path):
    assert is_denied(path, SCOPE_WEB), f"{path} 竟然可被编辑器读取"


@pytest.mark.parametrize("path", [".env", "config/.env.local"])
def test_file_tools_may_still_read_project_dotfiles(path):
    """Agent 读项目里的 .env 是正常任务（"帮我看看缺哪项"），不能一刀切挡掉。"""
    assert not is_denied(path, SCOPE_TOOL)


# ═══════════════════════════════════════════════════════════
# 3. 不许误伤正常项目文件
# ═══════════════════════════════════════════════════════════


@pytest.mark.parametrize("path", [
    "automind/agent.py",
    "README.md",
    "src/keyboard.py",          # 名字里带 key
    "docs/env.md",              # 名字里带 env
    "my.automind/notes.md",     # 名字里带 .automind，但不是数据目录
    "config/environment.yaml",
    "app/main.py",
])
def test_normal_project_files_are_untouched(path):
    assert not is_denied(path, SCOPE_WEB), f"{path} 被误拦了"
    assert not is_denied(path, SCOPE_TOOL)


def test_path_components_are_matched_not_substrings():
    """``my.automind/notes.md`` 不是数据目录 —— 判定必须按路径分量做。"""
    assert not is_denied("my.automind/notes.md")
    assert is_denied(".automind/notes.md")


@pytest.mark.parametrize("path", [
    "proj/.automind/workspaces/sess-w1/out.txt",
    "proj/.automind/workspaces/sess-w1/src/main.py",
])
def test_session_workspace_copies_are_not_platform_secrets(path):
    """会话隔离的工作副本就放在 ``.automind/workspaces/`` 下（见 core/workspace.py）。

    它是"被隔离的一份项目副本"，是用户的工作面 —— 一起挡掉会让隔离模式下的
    每一次写入都失败（v1.7.3 全量回归实测抓到这个回归）。
    """
    assert not is_denied(path, SCOPE_TOOL), f"{path} 被误判成平台密钥"
    assert not is_denied(path, SCOPE_WEB)


@pytest.mark.parametrize("path", [
    "proj/.automind/checkpoints/step-3.json",   # 平台管理的恢复状态
    "proj/.automind/traces/s/run.jsonl",        # 取证数据
    "proj/.automind/automind.db",               # 会话库
    "proj/.automind/quota.json",
])
def test_platform_state_stays_denied_even_inside_the_data_dir(path):
    """豁免只给 ``workspaces``：检查点/轨迹/会话库都是平台状态，不许工具写。"""
    assert is_denied(path, SCOPE_TOOL), f"{path} 不该被放开"


def test_session_workspace_exemption_does_not_open_the_data_dir():
    """豁免要精确到 workspaces/checkpoints 子目录，不能顺手把整个数据目录放开。"""
    assert is_denied(".automind/automind.db", SCOPE_TOOL)
    assert is_denied(".automind/traces/default/run.jsonl", SCOPE_TOOL)
    assert is_denied(".automind/quota.json", SCOPE_TOOL)


# ═══════════════════════════════════════════════════════════
# 4. 逃生门
# ═══════════════════════════════════════════════════════════


def test_explicit_override_releases_the_guard(monkeypatch):
    monkeypatch.setenv("AUTOMIND_ALLOW_SENSITIVE_FILE_READ", "1")

    assert not is_denied(".automind_config.json", SCOPE_WEB)
    assert not is_denied(".env", SCOPE_WEB)


@pytest.mark.parametrize("value", ["0", "false", "no", ""])
def test_other_values_do_not_release_the_guard(monkeypatch, value):
    monkeypatch.setenv("AUTOMIND_ALLOW_SENSITIVE_FILE_READ", value)

    assert is_denied(".automind_config.json", SCOPE_WEB)


# ═══════════════════════════════════════════════════════════
# 5. 接线：端点与文件工具都要真的走这道判定
# ═══════════════════════════════════════════════════════════


def test_editor_endpoints_are_wired(tmp_path, monkeypatch):
    """``_editor_target`` 必须真的调用判定 —— 定义了不接线等于没修。"""
    import automind.server as srv

    monkeypatch.setattr(srv, "_editor_root", lambda: tmp_path)
    secret = tmp_path / ".automind_config.json"
    secret.write_text('{"api_keys": {"deepseek": "sk-xxx"}}', encoding="utf-8")
    ok = tmp_path / "app.py"
    ok.write_text("print(1)", encoding="utf-8")

    target, err = srv._editor_target(".automind_config.json")

    assert target is None and err, "密钥文件竟然通过了编辑器端点"
    assert srv._editor_target("app.py")[0] == ok


def test_preview_endpoint_is_wired(tmp_path, monkeypatch):
    import automind.server as srv

    (tmp_path / ".automind_config.json").write_text("{}", encoding="utf-8")

    r = srv._preview_file_sync(tmp_path, ".automind_config.json")

    assert r.get("_status") == 403, "预览端点竟然能读密钥文件"


def test_file_tools_are_wired(tmp_path):
    """文件工具的路径守卫也要挡平台密钥（否则提示注入可直读 Key）。"""
    from automind.tools.file_editor import _RootGuard

    guard = _RootGuard(project_root=str(tmp_path))
    (tmp_path / ".automind_config.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".automind").mkdir(exist_ok=True)
    (tmp_path / ".automind" / "automind.db").write_bytes(b"SQLite")

    with pytest.raises(PermissionError):
        guard.resolve(".automind_config.json")
    with pytest.raises(PermissionError):
        guard.resolve(".automind/automind.db")
    # 正常文件照旧
    assert guard.resolve("app.py").name == "app.py"
