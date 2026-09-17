"""敏感路径判定 —— 哪些文件不该被 Web 端点或工具读到、写到。

## 为什么需要它

平台的密钥与数据都在工作目录里，而且**是明文**：

    .automind_config.json     所有模型提供商的 API Key
    .automind/automind.db     全部会话历史（含客户数据）、知识库
    .automind/traces/**       执行轨迹（含提示词与被读过的文件内容）
    .automind_license         许可证
    pro/.license-private/**   商业版签发私钥

而 Web 侧的文件端点是**只按"在不在 project_root 里"校验**的（见
``server.py`` 的 ``_editor_target`` / ``_preview_file_sync``）。默认
project_root 就是启动目录 —— 于是 ``GET /api/files/read?path=.automind_config.json``
一次请求就能把全部 API Key 读走，``/api/files/write`` 还能把它改成
攻击者自己的 ``api_base``（把后续每一次对话的提示词都导走）。

## 两级粒度（这是刻意的，不是漏了）

* ``SCOPE_WEB`` —— **编辑器 / 预览 / diff** 这类"让人看项目文件"的端点：
  平台密钥 + 用户自己的凭据文件（``.env``、``.ssh``、``.aws``…）一律不给读。
  这些端点没有任何"必须读凭据"的正当用途。
* ``SCOPE_TOOL`` —— **Agent 的文件工具**：只挡**平台自身**的密钥与数据目录。
  用户项目里的 ``.env`` 不挡 —— 读项目文件本来就是 Agent 的职责，而且它读出
  来的内容本来就要发给模型；在这里挡掉只会让"帮我检查一下 .env 缺哪一项"
  这类正常任务失败，却挡不住真正的问题（数据外发是由用户自己选的模型决定的）。
  平台自己的密钥则不同：Agent 没有任何正当理由去读它。

## 覆盖开关

``AUTOMIND_ALLOW_SENSITIVE_FILE_READ=1`` 可整体放开本模块的拦截 —— 给"我就想
用编辑器改 .automind_config.json"这种明确知道自己要干什么的场景留一条路。
拒绝时返回的原因里会写明这个变量，用户不会卡在一个没有出路的 403 上。
"""

from __future__ import annotations

import os
from pathlib import Path

SCOPE_WEB = "web"
SCOPE_TOOL = "tool"

#: 平台自身的密钥与数据 —— 两个 scope 都挡
_PLATFORM_NAMES = {
    ".automind_config.json",
    ".automind_license",
    "private_key.hex",
    "license.key",
    "public_key.hex",
}
_PLATFORM_PARTS = {
    ".automind",           # 数据目录：会话库 / 轨迹 / 检查点 / 配额
    ".license-private",    # 商业版签发私钥目录
    ".reasonix",
}

#: 数据目录里**属于用户工作面**的子目录：会话隔离的工作副本就在
#: ``<project>/.automind/workspaces/<session>/``（见 ``core/workspace.py``）。
#: 它是"被隔离的一份项目副本" —— 一起挡掉会让隔离模式下的每一次文件写入都
#: 失败（v1.7.3 的全量回归当场抓到这个回归：
#: tests/core/test_v164_governance.py::test_copy_is_usable_by_file_tools）。
#:
#: **只列 workspaces**：``checkpoints`` 是平台管理的恢复状态（任务断点、
#: 步骤快照），``traces`` 是取证数据，工具都没有理由去写它们 —— 挡住不影响
#: 任何正常任务，而放开等于给"篡改取证/恢复状态"留一条路。这条边界同样有
#: 用例钉住（tests/core/test_sensitive_paths.py）。
_WORKSPACE_PARTS = {"workspaces"}

#: 用户自己的凭据文件 —— 只在 SCOPE_WEB 挡（编辑器没有读它的正当理由）
_CREDENTIAL_NAMES = {
    ".git-credentials", ".netrc", "_netrc", ".pgpass", ".my.cnf",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    ".htpasswd", "credentials.json", "service-account.json",
}
_CREDENTIAL_PARTS = {".ssh", ".aws", ".kube", ".gnupg", ".docker"}
_CREDENTIAL_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".ppk"}


def _is_env_file(name: str) -> bool:
    """``.env``、``.env.local``、``.env.production``…… 都算凭据文件。"""
    low = name.lower()
    return low == ".env" or low.startswith(".env.")


def override_enabled() -> bool:
    """用户是否显式放开了拦截。"""
    return os.environ.get("AUTOMIND_ALLOW_SENSITIVE_FILE_READ", "").strip().lower() \
        in ("1", "true", "yes", "on")


def reason(target: Path | str, scope: str = SCOPE_WEB) -> str | None:
    """返回拒绝原因；允许访问时返回 None。

    判定按**路径分量**做，不做子串匹配：``my.automind/notes.md`` 不该因为
    名字里带 ``.automind`` 就被拦（子串匹配会把它误伤成数据目录）。
    """
    if override_enabled():
        return None
    p = Path(target)
    name = p.name.lower()
    parts = {part.lower() for part in p.parts}

    platform_data = bool(parts & _PLATFORM_PARTS)
    if platform_data and (parts & _WORKSPACE_PARTS):
        # 数据目录里的**工作副本**（会话隔离目录 / 检查点）是用户的工作面，
        # 不是平台密钥。见 _WORKSPACE_PARTS 的说明。
        platform_data = False

    if name in _PLATFORM_NAMES or platform_data:
        return ("这是平台的密钥/数据文件（含 API Key 与会话历史），"
                "不允许通过 Web 端点或文件工具读写。"
                "确需手工编辑请直接改磁盘上的文件；"
                "确需放开请设置 AUTOMIND_ALLOW_SENSITIVE_FILE_READ=1。")

    if scope != SCOPE_WEB:
        return None

    if _is_env_file(name) or name in _CREDENTIAL_NAMES:
        return ("这看起来是凭据文件（环境变量/密钥/令牌）。"
                "编辑器与预览不提供读取；确需放开请设置 "
                "AUTOMIND_ALLOW_SENSITIVE_FILE_READ=1。")
    if parts & _CREDENTIAL_PARTS or p.suffix.lower() in _CREDENTIAL_SUFFIXES:
        return ("这看起来是凭据目录/密钥文件（.ssh/.aws/.kube/*.pem/*.key 等）。"
                "编辑器与预览不提供读取；确需放开请设置 "
                "AUTOMIND_ALLOW_SENSITIVE_FILE_READ=1。")
    return None


def is_denied(target: Path | str, scope: str = SCOPE_WEB) -> bool:
    return reason(target, scope) is not None
