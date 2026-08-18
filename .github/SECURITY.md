# 安全策略 / Security Policy

## 支持的版本 / Supported Versions

只有最新的次版本会收到安全修复。请先升级到最新版再报告问题。

Only the latest minor release receives security fixes. Please upgrade before reporting.

| 版本 / Version | 支持状态 / Supported |
|---|---|
| 1.6.x | ✅ |
| < 1.6 | ❌ |

## 报告漏洞 / Reporting a Vulnerability

**请不要通过公开 Issue 报告安全漏洞** —— 那会在修复发布前把利用方式公之于众。

**Please do not report security issues through public Issues.**

请使用 GitHub 的私密漏洞报告通道：

1. 打开仓库的 **Security** 标签页；
2. 点击 **Report a vulnerability**（Private vulnerability reporting）；
3. 描述问题、影响范围与复现步骤。

该通道只有维护者可见。我们会在 **7 天内**给出首次回复，并在确认后与你商定
披露时间；修复发布后会在致谢中注明报告者（除非你希望匿名）。

Use GitHub's private vulnerability reporting: repository **Security** tab →
**Report a vulnerability**. We aim to acknowledge within **7 days**.

## 报告时请附上 / Please include

- 受影响的版本与运行方式（`pip` 安装 / 桌面版安装包 / 源码运行）；
- 复现步骤，最好是最小可复现示例；
- 你认为的影响（能读到什么、能执行什么、需要什么前置条件）。

## 本项目的安全边界 / Threat Model

AutoMind 会**在你的机器上执行命令、读写文件、访问网络**，这是它的功能而非缺陷。
以下属于设计内行为，不构成漏洞：

- 在「自动」或「全批准」审批模式下，Agent 执行了敏感操作 —— 这正是该模式的语义；
- 用户主动安装的第三方插件 / 技能 / MCP 服务器执行了任意代码 —— 加载插件即等同
  于运行其代码，请只加载可信来源（见 `automind/core/plugin.py` 模块说明）；
- 在 `--host 0.0.0.0` 且用户显式关闭了访问令牌时，局域网内可访问。
  （默认行为是：绑定非本机地址且未设令牌时**自动生成并强制启用**令牌。）

以下**属于**漏洞，欢迎报告：

- 绕过审批（approval）机制执行本应需要人工确认的操作；
- 绕过权限引擎的路径限制读写项目目录之外的文件（路径穿越）；
- 绕过 Python 沙箱访问文件系统 / 网络 / 子进程；
- 未授权访问已启用令牌的实例，或令牌泄漏到日志 / 响应体；
- API Key、令牌等敏感信息未被脱敏地写入日志、审计记录或前端。
