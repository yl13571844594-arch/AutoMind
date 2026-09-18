# 发布流程（当前版本 v1.7.4）

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

> **v1.7.3 提示（本机实测）**：`python -m build` 可能**卡住不返回** ——
> 进程阻塞、CPU 0.2s、十分钟以上无任何输出（不是慢，是卡在子进程/后端调用上）。
> 绕开办法是在**进程内**直接调用 PEP 517 后端，产物完全一致：
>
> ```bash
> python -u -c "import setuptools.build_meta as b; print(b.build_sdist('dist')); print(b.build_wheel('dist'))"
> ```
>
> 再补"源码包 + 审计"两步即可（`scripts/build_community.py` 内部仍会调一次
> `python -m build`，v1.7.4 实测仍会卡住，故那两步单跑：`build_source_zip()`
> 与 `audit()`）。
> 另外 `[tool.setuptools.packages.find]` 已加 `exclude`（`web/`、`desktop/`、
> `promo/`…）：`find_packages(where=".")` 会递归遍历整个工作区去找
> `__init__.py`，包括 `web/node_modules` 与 `desktop/dist` 里 playwright 的
> 90MB 二进制 —— **打包器看的是文件系统，不是 .gitignore**。

**v1.7.4 已构建并审计通过**（2026-09-18，`twine check` PASSED）：

| 产物 | 大小 | sha256 |
|---|---|---|
| `automind_agent-1.7.4-py3-none-any.whl` | 1200 KB | `9a9f5e2ec6091117277567e6f6bc53fdece4c578cbba31474aadce31d6fa74cf` |
| `automind_agent-1.7.4.tar.gz` | 1156 KB | `33ed9297f99971f4c4e0f179b5b83f14a5a187e1b874b717ed919d9c444b7aea` |
| `automind-community-1.7.4-src.zip` | 2846 KB | `5be9899447a86c1b9eb0e67b4b564490912e4c0f43cc9fe553e11d8c478f93f5` |

> v1.7.4 核验项：wheel 内 `automind/core/http_guard.py` 在包内且 `server.py`
> 已接线（`_host_allowed` + `host_denied` 日志）、`updater._SUMS_ASSETS` 认两个
> 校验和名字、`env_detector` 捕获 `OSError`、Web 静态资源为本次前端构建产物；
> 三份产物内均无绝对路径泄漏（`Administrator`/`Desktop` 零命中）。

> 上传 PyPI 仍需先按「上传 PyPI（社区版）」一节配好 Trusted Publisher，
> 或用本机 twine（`dist/automind_agent-1.7.4-*` 已就绪）。

## 构建桌面三平台安装包

| 平台 | 怎么来 | 产物 | 签名 |
|---|---|---|---|
| Windows | **本机** `desktop\build_release.ps1 -SkipWeb` | `desktop\Output\AutoMind-Setup-<ver>.exe` | Certum 代码签名证书（本机证书存储）+ RFC3161 时间戳 |
| macOS | CI `desktop-build.yml`（`workflow_dispatch`） | `AutoMind-<ver>.dmg`（通用二进制） | 配了 `MAC_CERT_P12_BASE64` 等 secrets 才签名；未配则 ad-hoc，DMG 仍可安装 |
| Linux | 同上 | `automind_<ver>_amd64.deb` | 无（deb 不做签名） |

Windows 包**必须本机构建**：CI 产的既未签名、也不含内嵌 WebView2 引导器，
`desktop-build.yml` 因此刻意不 attach 它（历史上出过"CI 未签名版覆盖已签名版"的事故）。

```powershell
cd desktop
$env:AUTOMIND_CERT_THUMBPRINT = "<证书指纹>"
.\build_release.ps1 -SkipWeb      # 前端没改时跳过重建
```

> 证书指纹查看：`Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert`。
> 构建末尾会**自动验签**（主程序 + 安装包都必须 `Valid`），不通过直接失败。

**装完必须冒烟**（冻结包里少一个 datas 是"能装能开、一用就错"的典型来源）：

```powershell
$p = Start-Process -PassThru .\dist\AutoMind\AutoMind.exe -ArgumentList "--server-only","--port","18766"
Invoke-WebRequest http://127.0.0.1:18766/api/health -UseBasicParsing | Select-Object -Expand Content
Stop-Process -Id $p.Id -Force
```

`/api/health` 里 `version` 必须等于本次版本号、`edition` 必须是 `community`。

## 本地归档与上传 Release

**先在本地凑齐三平台包，再一次性建 Release**（避免"发布页上缺一个平台"的中间态）：

```powershell
# 1) Windows 包进 dist\desktop\
Copy-Item desktop\Output\AutoMind-Setup-<ver>.exe dist\desktop\ -Force
# 2) 下载 CI 的 macOS / Linux 产物
gh run download <run-id> --dir $env:TEMP\automind-release-<ver>
Copy-Item <dmg> dist\desktop\ ; Copy-Item <deb> dist\desktop\
# 3) 校验和（文件名必须是 SHA256SUMS，见下节）
# 4) 建 Release：tag 随之创建
gh release create v<ver> --title "AutoMind v<ver>" --notes-file dist\release_notes_v<ver>.md `
  dist\desktop\AutoMind-Setup-<ver>.exe <dmg> <deb> `
  dist\desktop\SHA256SUMS dist\desktop\RELEASE-INFO.txt `
  dist\automind_agent-<ver>-py3-none-any.whl dist\automind_agent-<ver>.tar.gz `
  dist\automind-community-<ver>-src.zip
```

**本机留档**：三平台包与校验和同时复制一份到 `dist\releases\v<ver>\`
（按版本分目录，便于日后回溯"这一版到底发了什么"）。
`dist/` 已在 `.gitignore` 内 —— 留档是本地行为，不进版本库。

> ⚠️ **建 Release = 建 tag**，而 tag 会同时触发 `publish.yml`（PyPI）与
> `desktop-build.yml`。PyPI 未配 Trusted Publisher 时 `publish` 作业会红 ——
> 那是"没配"，不是"发坏了"；桌面包本身由上面的手工上传保证齐全。
> 若本次不想动 PyPI，别用 `git push --tags`，让 tag 只随 Release 创建。

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

> **v1.7.2 已发布（2026-09-16）**：tag 推送触发的工作流在 publish 这一步报
> `invalid-publisher`（PyPI 侧当时还没配置 Trusted Publisher），随后**由发行方
> 在本机用方式 B 上传成功**。PyPI 上 `automind-agent 1.7.2` 的 sha256 与本地
> 构建产物逐字节一致：
>
> | 产物 | sha256 |
> |---|---|
> | `automind_agent-1.7.2-py3-none-any.whl` | `265aa21972735eaaa7c66533693cef7be14e867686ceaf8b1eb7580be788dde3` |
> | `automind_agent-1.7.2.tar.gz` | `329d1a6b940bd667406ace71774ecf8d71c91a901abc3cac1d6402fc33940fa6` |
>
> **下次发版前建议把那一次性的第 1-2 步配好**（配完就不必再手工 twine 上传，
> 且没有任何令牌可泄露）：配置好之后对已有 tag 也可以补发，命令是
>
> ```bash
> gh workflow run publish.yml --ref v1.7.2
> gh run watch                 # 跟一下这次运行，确认 publish 步骤变绿
> ```
>
> 注意：PyPI 上一版是 1.6.3，1.7.2 是从 1.6.3 直接跳上来的（1.6.4/1.7.0/1.7.1
> 未单独发布，变更合并记录在 [CHANGELOG.md](CHANGELOG.md)）。

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

### ⚠️ 校验和资产必须叫 `SHA256SUMS`（v1.7.4 起）

桌面包的自动更新有三重校验：字节数 / SHA256 / Authenticode 签名。其中 SHA256
的基线来自 Release 上的校验和资产，而 `automind/core/updater.py` 找的是
**`SHA256SUMS`**（不带扩展名）。

v1.7.3 及更早的 Release 里这个资产叫 `SHA256SUMS.txt` —— 两侧名字差一个后缀，
于是 `asset_sha256` 恒为空、`_verify_integrity()` 每次都走
**"未提供校验和，跳过"**：不加日志、不报错，界面上照旧显示"三重校验"。
隐蔽点在于**它不会坏**，只会让最该防篡改的那一层长期缺席。

- 发布侧：`scripts/release_github.ps1` 现在产出 `SHA256SUMS`，且**文件里只放
  校验和行**（注释行会让 `sha256sum -c` 报格式错误 —— 用户照着验证反而得到
  "校验失败"）；说明文字移入 `RELEASE-INFO.txt`，两个文件都上传；
- 更新侧：`_SUMS_ASSETS` 同时认 `SHA256SUMS` 与 `SHA256SUMS.txt`（官方名优先），
  免得已经发布的版本为了一个文件名变成校验盲区；
- 有测试钉住这条接线：`tests/test_updater.py::TestChecksumAssetWiring`。

**手工建 Release 时同理**：上传的校验和文件必须命名为 `SHA256SUMS`，
否则桌面版的自动更新会静默跳过哈希校验。

## 验证

```bash
pip install automind-agent==<ver>
python -c "import automind; print(automind.__version__)"
python -m automind.server --port 8765     # /api/health → edition: community
```
