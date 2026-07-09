# 贡献指南 / Contributing

感谢关注 AutoMind 社区版！本项目以 MIT 许可开源，欢迎 Issue、PR 与讨论。
Thanks for your interest in AutoMind Community Edition (MIT licensed). Issues, PRs and discussions are welcome.

## 开发环境 / Development Setup

```bash
git clone https://github.com/yl13571844594-arch/automind.git
cd automind
pip install -e ".[web,dev]"      # 或 ".[full,dev]" 安装全部模型后端
pytest -q                        # 运行测试（应全部通过）
```

- Python ≥ 3.11。
- 代码风格由 ruff 约束（配置见 `pyproject.toml`）：`ruff check automind tests`。
- 新功能请附带 `tests/` 下的测试；修 bug 请先写能复现的失败用例。

## 提交 PR / Pull Requests

1. Fork 并从 `main` 拉分支（`feat/xxx` 或 `fix/xxx`）。
2. 保证 `pytest -q` 与 `ruff check` 通过。
3. PR 描述里说明**动机**与**行为变化**；界面改动请附截图。
4. 一个 PR 只做一件事，方便审阅与回滚。

## 版本边界 / Edition Boundary

本仓库只包含**社区版**代码。商业功能（协同模式、循环模式、定时任务、
高级统计、会话池）由闭源扩展包 `automind-pro` 通过
`automind/core/edition.py` 的稳定扩展协议注入：

- **不要**向本仓库提交任何商业功能实现或许可证相关代码；
- 涉及扩展协议（`ExtensionAPI` / `server_ctx` 契约）的改动需保持向后兼容，
  破坏性变更必须提升 `EXTENSION_API_VERSION` 并在 PR 中说明；
- 安全能力（鉴权/限流/脱敏/审计）永远保留在社区版，不接受将其移入付费层的提案。

## 报告安全问题 / Security Issues

请勿公开披露安全漏洞，先通过 GitHub Security Advisories（仓库 Security 标签页）
或维护者邮箱私下报告，我们会尽快响应并在修复后致谢。

## 行为准则 / Code of Conduct

保持友善与专业；尊重不同水平的使用者与贡献者。
