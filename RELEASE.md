# 发布流程（当前版本 v1.7.2）

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
5. 前端重新构建（改过 `web/src/**` 时必须）：
   `cd web && pnpm exec tsc --noEmit && pnpm build` —— 构建产物落在
   `automind/static/dist/`，它**是随包分发的**，不重建等于界面还是旧版；
6. 全量回归：`pytest -q && ruff check . && pytest pro/tests -q`，
   另跑 `analytics-service`：`cd analytics-service && pip install -e ".[test]" && pytest -q`
   （v1.7.0 起 CI 已覆盖这两处 —— 本地也照同样口径跑一遍，别让 CI 当第一道）。
   两处需要外网/浏览器，离线环境会红，与本版代码无关，判读时注意：
   `tests/tools/test_browser_fallback.py::TestFallbackBehaviour::test_falls_back_to_system_browser`。
   `tests/test_version_consistency.py` 会自动校验上面五处版本号一致。

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
```

### 许可证签发（v1.7.0 起：非对称签名）

付费墙的安全性等价于**"客户拿不到签发私钥"**。签发私钥不进仓库、不进 wheel、
不进 sdist（`.gitignore` + `pro/pyproject.toml` 的 `exclude` + `pro/MANIFEST.in`
三处共同保证，CI 另有两道审计）。

**一次性置备（发行方）**

```bash
# 1) 生成签名密钥对；私钥默认写到 pro/.license-private/（已 gitignore）
python pro/tools/issue_license.py keygen

# 2) 把打印出来的 public_key_hex 粘进
#    pro/automind_pro/licensing/keys.py 的 PUBLIC_KEY_HEX
#    （不置备 = 商业版一律激活不了，这是刻意的 fail-closed）

# 3) 确认
python pro/tools/issue_license.py pubkey
```

> ⚠️ 私钥丢失 = 已发出的许可证无法再签新的（存量许可证仍然有效）；
> 私钥泄露 = 换 `keygen` 重新置备公钥，**旧公钥签发的许可证全部作废**。

**给客户签发**

```bash
python pro/tools/issue_license.py issue PRO 20271231 acme
python pro/tools/issue_license.py issue ENT 00000000 bigcorp --seats 50
python pro/tools/issue_license.py verify AMP2-xxxx-...      # 签完自己验一遍
```

客户侧配置方式不变（环境变量 `AUTOMIND_LICENSE` 或
`.automind_license` / `~/.automind/license` 文件）。
排障用 `python -m automind_pro.license status` 查看"为什么没激活"。

> 📌 旧格式（`AMP-<TIER>-...`，对称 HMAC 签名）**默认拒绝** ——
> 那套密钥历史上随包分发过，已等同于公开。存量客户请在换证窗口期内换发新证；
> 万不得已的临时过渡可用 `AUTOMIND_LICENSE_ALLOW_LEGACY_HMAC=1`，
> **正式发布不要设置它**。

## 上传 PyPI（社区版）

**方式 A · GitHub Actions 可信发布（推荐，零令牌）**

仓库已内置 `.github/workflows/publish.yml`：推送版本 tag（`vX.Y.Z`）自动
构建 + 审计 + 发布；也可在 Actions 页手动 Run workflow 对已有 tag 补发。
首次使用需一次性配置（之后永久生效，无需任何令牌）：

1. 打开 https://pypi.org/manage/project/automind-agent/settings/publishing/
   （项目尚未存在时用 https://pypi.org/manage/account/publishing/ 的
   "Add a pending publisher"）；
2. 添加 Trusted Publisher：Owner `yl13571844594-arch`、Repository `AutoMind`、
   Workflow `publish.yml`、Environment `pypi`；
3. GitHub 仓库 Settings → Environments 新建名为 `pypi` 的 environment
   （可加保护规则，仅允许 tag 触发）。

**方式 B · 本机 twine（需 API Token）**

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
