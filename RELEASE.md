# 发布流程（当前版本 v0.8.0）

> 各版本变更明细见 [CHANGELOG.md](CHANGELOG.md)。本文档为**发布操作手册**，
> 与具体版本号解耦 —— 下文 `<ver>` 以 `automind/__init__.py` 的
> `__version__` 为准（唯一数据源）。

## 版本体系速览

- **社区版**（本仓库，MIT 开源）：`automind-agent` 包，发布到 PyPI + GitHub。
- **商业版**（`pro/` 目录，闭源）：`automind-pro` 包，商业渠道分发，
  **严禁**推送公开仓库或上传 PyPI。
- 社区核心零商业代码；商业能力经 `automind/core/edition.py`
  的稳定扩展协议 v1 运行时注入（详见 [docs/EDITIONS.md](docs/EDITIONS.md)）。

## 升版本（发布前）

1. 改 `automind/__init__.py` 的 `__version__`（唯一数据源）；
2. 同步 `pyproject.toml` 的 `version`；
3. 商业包同步 `pro/automind_pro/__init__.py` 与 `pro/pyproject.toml`；
4. 更新 `CHANGELOG.md`、`使用手册.md` 头部适用版本与
   `automind/static/manual.html`（并同步副本 `使用手册.html`）；
5. 全量回归：`pytest -q && ruff check . pro && pytest pro/tests -q`。

## 构建社区版发布物

```bash
python scripts/build_community.py
```

产物（`dist/`）：

```
automind_agent-<ver>-py3-none-any.whl      ← pip 安装包（含 Web 界面静态资源）
automind_agent-<ver>.tar.gz                ← sdist
automind-community-<ver>-src.zip           ← 开源上传源码包（白名单收集）
```

脚本会自动**审计**产物：任何产物中出现 `automind_pro`、许可证、
`.automind_config.json` 等敏感/商业内容即构建失败。

## 构建商业版（内部）

```bash
cd pro && python -m build          # automind_pro-<ver> wheel（商业渠道分发）
python -m automind_pro.license PRO 20271231 acme   # 生成客户许可证
```

## 上传 PyPI（社区版）

PyPI 需 API Token（https://pypi.org/manage/account/ → API tokens；
用户名固定填 `__token__`，令牌切勿贴进任何对话）：

```bash
python -m twine check dist/automind_agent-<ver>*
# 可选：先传 TestPyPI 演练
python -m twine upload --repository testpypi dist/automind_agent-<ver>* -u __token__ -p <TOKEN>
# 正式发布（不可撤销，同版本号无法覆盖）
python -m twine upload dist/automind_agent-<ver>* -u __token__ -p <TOKEN>
```

> ⚠ 切勿上传 `automind_pro` 相关文件到 PyPI；商业包 classifiers 已含
> `Private :: Do Not Upload`（PyPI 会拒收），但请勿依赖这一层兜底。

## 推送 GitHub 并发 Release

```bash
git push origin main
git tag v<ver> && git push origin v<ver>
# GitHub Releases 页基于该 tag 发布，正文粘贴 CHANGELOG 对应段落
```

## 验证

```bash
pip install automind-agent==<ver>
python -c "import automind; print(automind.__version__)"
python -m automind.server --port 8765     # /api/health → edition: community
```
