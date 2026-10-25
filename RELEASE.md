# 发布流程（当前版本 v1.11.18）

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
   `tests/test_version_consistency.py` 会自动校验上面六处版本号一致（含 `pro/` 两处）。

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

> v1.11.13 的 PyPI 发布已完成：`automind_agent-1.11.13-py3-none-any.whl` /
> `automind_agent-1.11.13.tar.gz` 已上传 PyPI（sha256 与本地构建一致）。
> 后续 tag 发布由仓库 Secret `PYPI_API_TOKEN` 自动完成；也可继续按「上传 PyPI」
> 一节改用 Trusted Publisher（更推荐，零令牌）。

> **v1.11.18 已发布（2026-10-25）**：PyPI `automind_agent-1.11.18` 已上传；
> GitHub Release 已包含 Windows 签名安装包 / 便携版、macOS 通用 DMG、Linux deb、
> wheel/sdist、社区源码包与 `SHA256SUMS`。后续 tag 发布由仓库 Secret
> `PYPI_API_TOKEN` 自动完成（工作流已接线）。

**v1.11.18 已构建（2026-10-25，社区版）**：

| 产物 | 大小 | sha256 |
|---|---|---|
| `automind_agent-1.11.18-py3-none-any.whl` | 1802 KB | `8d8c699432cca7c7eaba7dce7934c41756da2b0d735dc0b8600914f5cc005989` |
| `automind_agent-1.11.18.tar.gz` | 1775 KB | `5f63b9062f9317d427c452c2eeff8d2e467aec7e715f456a7f794fb87eb9c317` |
| `automind-community-1.11.18-src.zip` | 5960 KB | `7c6186cd75b428ce86065db7daa45a7dc8176cf10d446d5094f2a30a9b4c65b2` |

三平台与 Release 附件（[v1.11.18](https://github.com/yl13571844594-arch/AutoMind/releases/tag/v1.11.18)）：

| 平台 | 文件 | sha256 |
|---|---|---|
| Windows 安装包（Certum 签名 + RFC3161） | `AutoMind-Setup-1.11.18.exe` | `e8bd147cf4264e51553004e6d01d7a7834d636038fc876d0a87bac2ff03e7770` |
| Windows 便携版 | `AutoMind-1.11.18-win64-portable.zip` | `54c768b0bba029043aa7f51b9f2a2cd1c6669566ce2d6d2dd72f8b674858eeff` |
| macOS 通用 DMG | `AutoMind-1.11.18.dmg` | `1a83fa99d444968089f56ce039e766180541fa170a7bf6a469ade2b7c4313c9a` |
| Linux deb | `automind_1.11.18_amd64.deb` | `a37ea09b33d34b66e3a34a61354f043a94bf70b59af789c5462e8706d1d55758` |

> v1.11.18 核验：Windows `AutoMind.exe` 与 `AutoMind-Setup-1.11.18.exe`
> 均为 `Valid`，发布者 `西安心途启境科技有限公司`，带 RFC3161 时间戳；
> 安装后在正常 Windows 会话运行 `--server-only`，`/api/health` 返回
> version=1.11.18、edition=community；macOS CI 完成 lipo 合并并校验
> `x86_64 arm64` 双架构；Linux deb 构建 + 冒烟测试通过；PyPI 1.11.18 已发布。
> 社区源码包已排除商业扩展与运营手册/配图，审计通过。

**v1.11.13 已构建（2026-10-18，社区版）**：

| 产物 | 大小 | sha256 |
|---|---|---|
| `automind_agent-1.11.13-py3-none-any.whl` | 1625 KB | `c13dd3b2f91a712677d67d8a0dd85fedbe4e1143958ebf94030131c788ef95c2` |
| `automind_agent-1.11.13.tar.gz` | 1563 KB | `380a998302b6f143ea637e381dc679e656db5feae02ed5035bd89cda1c88d4cd` |
| `automind-community-1.11.13-src.zip` | 4602 KB | `14d5d935b3fc4e5d11a927c84b287c1357ce1541322db26363278c04d1ff11c3` |

三平台与 Release 附件（[v1.11.13](https://github.com/yl13571844594-arch/AutoMind/releases/tag/v1.11.13)）：

| 平台 | 文件 | sha256 |
|---|---|---|
| Windows 安装包（Certum 签名 + RFC3161） | `AutoMind-Setup-1.11.13.exe` | `6b1d889c7ec26b710aa94515215158f2c46ba5848ed92ebc940fb60b7a6d66ff` |
| Windows 便携版 | `AutoMind-1.11.13-win64-portable.zip` | `c8a84dbc14874dcbe947be84a4326efbcb15a9a134df700284186ecb8ffbee5e` |
| macOS 通用 DMG | `AutoMind-1.11.13.dmg` | `d5b04641c6a907239845d66b473404655774372d7b77caece0d5c6858bb5a669` |
| Linux deb | `automind_1.11.13_amd64.deb` | `184a90197a2026bdb46694ea2dd562fbddfdbbedf911026ce3f2323d11909cc7` |

> v1.11.13 核验：Windows `AutoMind.exe` 与 `AutoMind-Setup-1.11.13.exe`
> 均为 `Valid`，发布者 `西安心途启境科技有限公司`，带 RFC3161 时间戳；
> `/api/health` 返回 version=1.11.13、edition=community；macOS CI 已做
> universal 合并校验；Linux 包冒烟测试通过；PyPI 1.11.13 已上传并可通过
> `pip install "automind-agent[web]==1.11.13"` 安装。

**v1.11.18 当前状态（2026-10-25，源码级补丁发版）**：

| 项目 | 状态 |
|---|---|
| 版本号（core / pro / web / installer / 手册） | 已统一为 `1.11.18`，`tests/test_version_consistency.py` 全绿 |
| 前端 `automind/static/dist/` | **本版未改前端源码，无需重建**（沿用 v1.11.17 构建产物） |
| 商业扩展包 `automind_pro-1.11.18-py3-none-any.whl` / 一键升级包 `.ampkg` | **均已重建**：`pro/dist/automind_pro-1.11.18-py3-none-any.whl`（119,601 B，sha256 `f8b4fe284cf27ad37c6e806f545101e6c17f487b98e8377e5406997bc43fb04b`），旧 1.11.17 wheel 已移除（避免 `doctor` 误报过期 wheel）；`.ampkg` 两份均为 1.11.18 wheel：`dist/upgrades/AutoMind-PRO-acme-20271231.ampkg`（118,303 B）与 `AutoMind-ENT-bigcorp-00000000.ampkg`（118,313 B），旧包归档到 `dist/upgrades/archive-1.11.17/`；`issue_license.py doctor` **12/12 全绿** |
| 社区 wheel / sdist / PyPI / 三平台安装包 | **本次未构建/未上传**；发布时按本文「构建社区版发布物」「构建桌面三平台安装包」「上传 PyPI」执行 |
| 老客户升级 | 本版未改商业扩展包功能：已按 v1.11.17 换发过 `.ampkg` 的客户可继续使用（面板对不同号会提示）；发行方已重建 v1.11.18 同号 wheel 与 `.ampkg`，需要严格同号时直接换发。公钥未轮换，旧许可证仍有效 |
| 本轮变更 | 评测执行器无人值守审批（`approve_all` 默认 + `--approval-mode` 覆盖 + 报告标注）与任务超时硬收敛；环境探测改在任务工作区执行（修掉"系统提示把进程所在仓库误报成项目"的误导）；`tool_called` 断言路径等价；`file_write` 磁盘回读校验 + 中文写入提示词纪律；`pwsh` 原生后台通道 / `subagent_list` 的 `agent_id` / 团队共享任务工具 `team_task` |
| 本轮验证 | `pytest` 全量 **3019 条用例通过（exit 0）**、`ruff check .` 通过、`tests/test_version_consistency.py` 10 passed；`pro/tests` 全量通过、`issue_license.py doctor` **12/12 全绿**；新增 `tests/eval/test_eval_approval.py`、`tests/tools/test_file_write_verify.py`、`tests/tools/test_background_pwsh.py`、`tests/tools/test_subagent_detail.py`、`tests/tools/test_team_task_tool.py`、`tests/context/test_env_detector_cwd.py` + 团队注册/迁移测试；标准工具评测复跑 **7/7 通过**（251.0s / 186,243 token，原失败两条见 `CHANGELOG.md`） |

**v1.11.17 当前状态（2026-10-23，源码级发版）**：

| 项目 | 状态 |
|---|---|
| 版本号（core / pro / web / installer / 手册） | 已统一为 `1.11.17`，`tests/test_version_consistency.py` 全绿 |
| 商业扩展包 `automind_pro-1.11.17-py3-none-any.whl` | 已重建（`pro/dist/`，119,601 B，sha256 `620e509bea4df7b3b2a74cb7db8de1ba1c5029f03817d2f8dff1d9e113210ef2`）；旧 1.11.16 wheel 归档到 `pro/dist/archive-1.11.16/`（避免 `doctor` 误报过期 wheel） |
| 一键升级包 `.ampkg`（PRO / ENT 各一份，v1.11.17 wheel） | 已重新生成：`dist/upgrades/AutoMind-PRO-acme-20271231.ampkg`（118,298 B，sha256 `829c311322788b5648d070d7394105faa3b02ac279f5554c05015edd1c5fe7b4`）、`AutoMind-ENT-bigcorp-00000000.ampkg`（118,310 B，sha256 `1a4688b91b68efe2a5468fd09215ad93e4f1afc71bcf4ec26c2f9aaac8b9e39b`）；`issue_license.py doctor` **12/12 全绿**（含 2 个升级包可激活校验） |
| 前端 `automind/static/dist/` | 已用 v1.11.17 前端源码重建（tsc + vite build，哈希见 `automind/static/dist/index.html`） |
| 社区 wheel / sdist / PyPI / 三平台安装包 | **本次未构建/未上传**；发布时按本文「构建社区版发布物」「构建桌面三平台安装包」「上传 PyPI」执行 |
| 老客户升级 | **必须换发 v1.11.17 的 `.ampkg`**：本版修复了"已激活专业版 / 企业版但语义缓存、智能路由、成本仪表盘、知识库进阶仍 403"的路由挂载 bug（旧扩展包功能本身未变，但建议随核心一起升级到同号版本；公钥未轮换，旧许可证仍有效） |
| 本轮新增验证 | 新增 `tests/server/test_pro_feature_routes.py` **7 passed**、`pro/tests/test_feature_route_attach.py` **5 passed**、`tests/core/test_edition.py` 新增 **3 passed**；`tests` + `tests/server` 全量 **exit 0**、`pro/tests` 全量 **exit 0**；屏蔽 pro 包的社区场景 **47 passed**；`ruff check .`、`tsc --noEmit` 通过；真机端到端（Playwright + 系统 Edge + 真实服务 / 真实 API）PRO / ENT 两套环境 **50/50 检查通过**（`/api/cache`、`/api/router`、`/api/costs`、`/api/kb/kbs` 状态 + 路由页无降级错误 + 会话管理搜索/筛选/分组/排序/全部工作区/设置持久化） |
| 发行工具修复 | `scripts/build_pro_upgrade.py` 在打包成功后读不到 `issue_license.py bundle` 返回的 `wheel` 字段会 `KeyError` 退出；已让 bundle 结果回传 `wheel` 名（并在脚本侧兜底），一键发行闭环恢复 |

**v1.11.16 当前状态（2026-10-23，源码级发版）**：

| 项目 | 状态 |
|---|---|
| 版本号（core / pro / web / installer / 手册） | 已统一为 `1.11.16`，`tests/test_version_consistency.py` 全绿 |
| 商业扩展包 `automind_pro-1.11.16-py3-none-any.whl` | 已重建（`pro/dist/`，119,586 B，sha256 `62d16f3fc72ececa5ac45db6857cd7850361a0cbc669efaeb2240a0b95cef53e`），内含协同/循环预算续跑、工作流触发器补偿/重放保护等 Pro 侧变更 |
| 一键升级包 `.ampkg`（PRO / ENT 各一份，v1.11.16 wheel） | 已重新生成：`dist/upgrades/AutoMind-PRO-acme-20271231.ampkg`（118,282 B，sha256 `4bff67b29d15a2d7570c5a11216e4c2d71c38a5340ebba5dfab240104083084d`）、`AutoMind-ENT-bigcorp-00000000.ampkg`（118,294 B，sha256 `66691c1adb9078a817e6bb79d6294b979f29a4f09306c057ef74c3494e85cc68`）；`issue_license.py doctor` **12/12 全绿** |
| 前端 `automind/static/dist/` | 已用 v1.11.16 前端源码重建（tsc + vite build：`index-CMqs_gvL.js` / `index-ZfBx0Oh2.css`） |
| 社区 wheel / sdist / PyPI / 三平台安装包 | **本次未构建/未上传**；发布时按本文「构建社区版发布物」「构建桌面三平台安装包」「上传 PyPI」执行 |
| 老客户升级 | **必须换发 v1.11.16 的 `.ampkg`**：协同/循环模式的断点续跑会向 Pro 扩展传 `resume=` 参数，旧 1.11.15 及更早扩展包不支持；公钥未轮换，旧许可证仍有效，旧 `.ampkg` 仍可激活但无新特性 |
| 本轮新增验证 | 预算续跑/断点续跑组合 **51 passed**（含四模式 e2e 19 条）；`tests/planning`+续跑 **231 passed**；执行治理 **47 passed**；工作流 **457 passed**（新增 57）；路由与成本 Playwright 兼容性 **60 passed**；`tsc`/`ruff`/`git diff --check` 通过 |

**v1.11.15 当前状态（2026-10-22，源码级发版）**：

| 项目 | 状态 |
|---|---|
| 版本号（core / pro / web / installer / 手册） | 已统一为 `1.11.15`，`tests/test_version_consistency.py` 全绿 |
| 商业扩展包 `automind_pro-1.11.15-py3-none-any.whl` | 已重建（`pro/dist/`，114,399 B，sha256 `8c9c41daa666c41552958764900ee7c1fb475e69e75e5d4e0b0167b770b42f05`），内含 v3 许可证 / 机器绑定 / CRL / 周期复检（`licensing/guard.py`） |
| 一键升级包 `.ampkg`（PRO / ENT 各一份，v1.11.15 wheel） | 已重新生成：`dist/upgrades/AutoMind-PRO-acme-20271231.ampkg`（sha256 `abef103be83552a9c76a5ac5eba02e308c5c7f6f43d03dd700aa9790067dbf69`）、`AutoMind-ENT-bigcorp-00000000.ampkg`（sha256 `bb9bc22201ba416f41398302cb9655093a3668ba293f8dc9518a1764e4863edb`）；`issue_license.py doctor` **12/12 全绿** |
| 前端 `automind/static/dist/` | 已用 v1.11.15 前端源码重建（tsc + vite build） |
| 社区 wheel / sdist / PyPI / 三平台安装包 | **本次未构建/未上传**；发布时按本文「构建社区版发布物」「构建桌面三平台安装包」「上传 PyPI」执行 |
| 老客户升级 | **必须换发 v1.11.15 的 `.ampkg`**（内含支持 v3 许可证/机器绑定/CRL/周期复检的新扩展包；公钥未轮换，旧许可证仍有效；旧 .ampkg 仍可激活，但新特性缺失）。**给仍在使用旧扩展包的客户发证时不要带 `--serial/--machine/--start`**（工具默认输出 v2 格式，可被旧包解析） |
| 商业运营文档 | `docs/COMMERCIAL_ACTIVATION.md`（§12-20 运营章节）、`docs/LICENSE_OPERATIONS.md`（运营 SOP）、`docs/商业运营升级与发证操作手册_v1.11.15.docx`（Word 图文手册） |

**v1.11.14 当前状态（2026-10-21，源码级发版）**：

| 项目 | 状态 |
|---|---|
| 版本号（core / pro / web / installer / 手册） | 已统一为 `1.11.14`，`tests/test_version_consistency.py` 全绿 |
| 商业扩展包 `automind_pro-1.11.14-py3-none-any.whl` | 已重建（`pro/dist/`），内含已置备的验签公钥 |
| 一键升级包 `.ampkg`（PRO / ENT 各一份） | 已生成于 `dist/upgrades/`，`issue_license.py doctor` 8/8 全绿 |
| 社区 wheel / sdist / PyPI / 三平台安装包 | **本次未构建/未上传**；发布时按本文「构建社区版发布物」「构建桌面三平台安装包」「上传 PyPI」执行 |
| 老客户升级 | v1.11.13 及更早的 Pro wheel 内是占位公钥，**必须换发 v1.11.14 的 `.ampkg`** 才能激活；详见 [docs/COMMERCIAL_ACTIVATION.md](docs/COMMERCIAL_ACTIVATION.md) |

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

## 构建商业版（内部 / 商业渠道分发）

商业包与社区核心**同号发布**：`pro/automind_pro/__init__.py`、`pro/pyproject.toml`
必须与 `automind/__init__.py`（唯一数据源）一致 —— `tests/test_version_consistency.py`
与 `python pro/tools/issue_license.py doctor` 都会拦。

### 商业激活闭环（v1.11.14 起，发行方必读）

> 背景：v1.11.13 及更早，`pro/automind_pro/licensing/keys.py` 的 `PUBLIC_KEY_HEX`
> 还是 `"0" * 64` 占位符 → `parse_license` 一律 fail-closed，**客户把许可证填对了也激活不了**；
> 同时 `pro/dist/` 里只有 1.11.4 的旧 wheel。v1.11.14 补齐：真实公钥置备、wheel 重建、
> `doctor` 发行前自检、一键发行脚本、防回归测试（`pro/tests/test_commercial_activation_pro.py`）。
> 完整说明见 [docs/COMMERCIAL_ACTIVATION.md](docs/COMMERCIAL_ACTIVATION.md)。

**① 一次性：置备真实发行密钥（发行方本机，只做一次）**

```bash
# 生成 Ed25519 密钥对，并把公钥直接写回 keys.py（不再手抄，避免再次退化为占位符）
python pro/tools/issue_license.py keygen --write-public-key

# 确认：provisioned=true、key_id 非空
python pro/tools/issue_license.py pubkey
```

- 私钥只落 `pro/.license-private/private_key.hex`（已 gitignore）。**绝不提交、绝不分发、
  绝不贴进任何文档/日志**；丢失 = 存量许可证仍有效但无法再签新证，泄露 = 立即换钥
  并重发所有客户许可证（旧公钥签的全部作废）。
- 公钥写回 `keys.py` 后**必须重新构建 wheel** 才能在客户端生效（wheel 内置公钥）。

**② 每次发版：重建 wheel → 出升级包（一条命令）**

```bash
# 专业版，到期 2027-12-31
python scripts/build_pro_upgrade.py --tier PRO --expiry 20271231 --customer acme

# 企业版，永久，50 席
python scripts/build_pro_upgrade.py --tier ENT --expiry 00000000 --customer bigcorp --seats 50

# Windows PowerShell 等价写法
.\scripts\build_pro_upgrade.ps1 -Tier ENT -Expiry 00000000 -Customer bigcorp -Seats 50
```

脚本依序执行：构建 `pro/dist/automind_pro-<ver>-py3-none-any.whl` → 三处版本号一致性校验 →
`issue_license.py doctor` 自检 → 签发许可证并打成
`dist/upgrades/AutoMind-<TIER>-<customer>-<expiry>.ampkg` → 打印 sha256 与客户升级步骤。
同一版本已构建过 wheel 时可加 `--no-build` 跳过重复构建。

> ⚠️ 发行目录只保留**当前版本**的 wheel 与 .ampkg：旧 wheel 可能仍带占位公钥，
> 旧 .ampkg 可能由已轮换的密钥签发 —— `doctor` 会把它们点名，清理后再发版。

**③ 发行前自检（任一项失败即非零退出；v1.11.15 起共 12 项）**

```bash
python pro/tools/issue_license.py doctor
```

检查：公钥已置备（含旧钥轮换状态）/ 私钥可用且与公钥配对 / wheel 文件名版本 = 商业包版本 = 核心版本 /
**wheel 内置公钥与 `keys.py` 一致**（置备公钥后忘重建 wheel → 客户装上仍激活不了）/
签约-验签闭环 / CRL 可读（可选）/ 周期复检配置 / 机器绑定就绪 /
`dist/upgrades/*.ampkg` 的清单、许可证、wheel 版本与内置公钥均可激活。

**④ 商业运营（v1.11.15 起：批量发证 / 台账 / CRL / 机器绑定 / 试用）**

```bash
# 单发（传统写法，输出 v2 格式，旧扩展包客户也认）
python pro/tools/issue_license.py issue PRO 20271231 acme
# 试用 30 天 / 指定生效起始日 / 指定序列号（输出 v3，需 v1.11.15+ 扩展包）
python pro/tools/issue_license.py issue PRO acme --days 30 --serial TRIAL0001
python pro/tools/issue_license.py issue PRO acme --days 365 --start 20270101
# 机器绑定（客户在升级面板复制本机指纹；换机按新指纹重签）
python pro/tools/issue_license.py issue ENT acme --days 365 --machine 0123456789abcdef
# 批量发证 + 台账（+可选逐客户 .ampkg）
python pro/tools/issue_license.py issue-batch --csv customers.csv --out dist/licenses \
    --bundles --wheel pro/dist/automind_pro-1.11.15-py3-none-any.whl
# 吊销（写入 CRL；bundle 会随包分发 CRL）
python pro/tools/issue_license.py revoke AMP2-... --reason refund
python pro/tools/issue_license.py crl list
# 换钥（过渡期旧钥仍验签，到期后下线）
python pro/tools/issue_license.py keygen --rotate --retire-after 20271022
```

客户端行为（v1.11.15 扩展包）：`AUTOMIND_LICENSE_RECHECK`（默认 3600 秒）周期复检，到期 /
被吊销 / 机器不符 / 时钟回拨自动降级并通知界面；`AUTOMIND_LICENSE_GRACE_DAYS`（默认 0）
可配置宽限期；CRL 路径 `AUTOMIND_LICENSE_CRL` 或 `~/.automind/license_crl.json`。
完整运营 SOP 见 [docs/LICENSE_OPERATIONS.md](docs/LICENSE_OPERATIONS.md) 与
[docs/COMMERCIAL_ACTIVATION.md](docs/COMMERCIAL_ACTIVATION.md) §12-20；
图文步骤见 `docs/商业运营升级与发证操作手册_v1.11.15.docx`。


### 许可证签发（v1.7.0 起：Ed25519 非对称签名）

付费墙的安全性等价于**"客户拿不到签发私钥"**。签发私钥不进仓库、不进 wheel、
不进 sdist（`.gitignore` + `pro/pyproject.toml` 的 `exclude` + `pro/MANIFEST.in`
三处共同保证，CI 另有两道审计）。

**一次性置备（发行方）**：见上文「商业激活闭环 ①」—— 用
`keygen --write-public-key` 自动写回公钥，不再手工粘贴（手工粘贴漏一次就退化成占位符，
商业版会在所有客户机器上悄悄变成"怎么填许可证都不生效"）。

**给客户签发**

```bash
# 推荐：许可证 + wheel 打成一个 .ampkg，客户拖进升级面板一次完成装包 + 激活
python pro/tools/issue_license.py bundle PRO 20271231 acme
python pro/tools/issue_license.py bundle ENT 00000000 bigcorp --seats 50

# 只签发许可证（运维批量部署 / 手工配置场景）
python pro/tools/issue_license.py issue PRO 20271231 acme
python pro/tools/issue_license.py verify AMP2-xxxx-...      # 签完自己验一遍
```

客户侧读取顺序：环境变量 `AUTOMIND_LICENSE` → 当前目录 `.automind_license` →
`~/.automind/license`（应用内激活写这一份）。排障用
`python -m automind_pro.license status` 或 `GET /api/license`
（升级面板同款数据：卡在哪一步、扩展包与主程序版本是否匹配）。

> 📌 旧格式（`AMP-<TIER>-...`，对称 HMAC 签名）**默认拒绝** ——
> 那套密钥历史上随包分发过，已等同于公开。存量客户请在换证窗口期内换发新证；
> 万不得已的临时过渡可用 `AUTOMIND_LICENSE_ALLOW_LEGACY_HMAC=1`，
> **正式发布不要设置它**。

## 上传 PyPI（社区版）

**v1.11.13（2026-10-18）**：PyPI 已发布。仓库已配置加密 Secret
`PYPI_API_TOKEN`，`.github/workflows/publish.yml` 通过该 Secret 上传；
下次发版推送 tag 即可自动发布。更推荐逐步切换为下面的 Trusted Publisher
（OIDC、无令牌）方式。

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

### 商业激活闭环（内部仓库：含 pro/ + 发行私钥时）

```bash
python pro/tools/issue_license.py pubkey          # provisioned=true，key_id 非空
python pro/tools/issue_license.py doctor          # 12 项全绿、退出码 0（公钥/私钥/wheel/.ampkg/CRL/复检/机器绑定）

python -m pytest pro/tests/test_commercial_activation_pro.py \
    pro/tests/test_license_v3.py pro/tests/test_license_forgery.py \
    pro/tests/test_license_and_activation.py pro/tests/test_in_app_upgrade.py \
    pro/tests/test_issue_bundle.py -q
python -m pytest tests/test_version_consistency.py tests/test_commercial_activation.py -q
python -m ruff check pro automind/core/edition.py tests
```

### v1.11.18 新增验证项（发版前逐条跑）

```bash
# 评测无人值守审批 + 任务超时硬收敛
python -m pytest tests/eval/test_eval_approval.py -q

# file_write 磁盘回读校验 / 中文写入
python -m pytest tests/tools/test_file_write_verify.py -q

# 后台任务 / 子智能体详情 / 团队共享任务工具
python -m pytest tests/tools/test_background_pwsh.py tests/tools/test_subagent_detail.py \
    tests/tools/test_team_task_tool.py -q

# 版本一致性（六处同号 + 手册含本版条目）
python -m pytest tests/test_version_consistency.py -q
```

> ⚠️ v1.11.18 为**补丁发版**：商业扩展包与前端无功能变更（`pro` 版本号随核心到 1.11.18，
> wheel 与两份 `.ampkg` 已重建、`doctor` 12/12 全绿）；评测复跑数据（标准工具赛道
> **7/7 通过、251.0s / 186,243 token**；`smoke_run_command` 18,626 tok / 26.0s，
> `smoke_chinese_content` 56,735 tok / 36.2s）已回填 `CHANGELOG.md`。

### v1.11.17 新增验证项（发版前逐条跑）

```bash
# 商业特性路由挂载（本版核心修复）：社区版安全测试 + PRO/ENT 激活后的真实路由优先级
python -m pytest tests/server/test_pro_feature_routes.py -q
python -m pytest pro/tests/test_feature_route_attach.py \
    pro/tests/test_in_app_upgrade.py tests/core/test_edition.py -q

# 真机探测（临时目录 + 测试密钥 + 生成 PRO/ENT 许可证，TestClient 起真实 app）
python _perfcheck/probe_pro_routes.py        # 期望 /api/cache、/api/router 200
python _perfcheck/probe_pro_routes.py ENT    # 期望再加 /api/costs、POST /api/kb/kbs 200

# 会话管理（分组 / 排序 / 筛选 / 搜索）：前端类型检查 + 构建产物随包
cd web && node node_modules/typescript/bin/tsc --noEmit
node node_modules/vite/bin/vite.js build

# 版本一致性（六处同号 + 手册含本版条目）
python -m pytest tests/test_version_consistency.py -q
```

> ⚠️ v1.11.17 的修复点在**社区核心的挂载清单**（`automind/server.py`）与商业包无关 ——
> 但仍建议客户把 `automind_pro` 与核心**同号升级**：`doctor` 与 `tests/test_version_consistency.py`
> 都要求同版本发布；旧 wheel 会留在 `pro/dist/` 触发 "stale_wheels" 警告时，把它归档到子目录再出包。

### v1.11.16 新增验证项（发版前逐条跑）

```bash
# 预算连续性 + 断点续跑（四模式；fake LLM / 临时目录，不联网）
python -m pytest tests/core/test_budget_continuation.py \
    pro/tests/test_multi_loop_budget_resume.py \
    tests/test_checkpoint_resume_e2e.py tests/core/test_token_budget_chain.py -q

# 执行治理 26 参数逐项生效（行为级断言）
python -m pytest tests/test_execution_governance.py -q

# 工作流自动化强化（条件 / 失败降级 / 整轮超时 / token / 触发器补偿与防重放）
python -m pytest tests/workflow -q

# 前端类型与构建（dist 随包分发）
cd web && node node_modules/typescript/bin/tsc --noEmit
node node_modules/vite/bin/vite.js build

# 路由与成本页兼容性（Playwright + mock 六种异常数据形态时；无浏览器环境可跳过人工确认）
#   期望：不白屏、无未捕获异常、无 NaN 文本，正常数据视觉不回归
```

> ⚠️ v1.11.16 的协同/循环续跑依赖同版本 Pro 扩展（`resume=` 参数）；发版时必须重建
> `automind_pro-1.11.16` wheel 并重新出 `.ampkg`，旧 1.11.15 扩展包与续跑不兼容。

### v1.11.15 新增验证项（发版前逐条跑）

```bash
# 商业运营（CLI 实跑；产物在 /tmp 或 dist 下，不要污染仓库）
python pro/tools/issue_license.py issue PRO smoke-co --days 30 --serial SMOKE0001 --machine auto
python pro/tools/issue_license.py verify <许可证>
python pro/tools/issue_license.py issue-batch --csv <临时 csv> --out <临时目录>
python pro/tools/issue_license.py revoke <许可证或 serial> --crl <临时 crl.json> --reason test
python pro/tools/issue_license.py crl list --crl <临时 crl.json>

# 前端（dist 是随包分发的，改过 web/src 必须重建）
cd web && node node_modules/typescript/bin/tsc --noEmit
node node_modules/vite/bin/vite.js build          # 产物落在 automind/static/dist/

# 新功能测试
python -m pytest tests/test_model_choice.py tests/test_team_collab.py \
    pro/tests/test_model_route_override.py pro/tests/test_cost_dashboard.py -q

# .ampkg 端到端：隔离 HOME/数据目录下 install_upgrade(.ampkg)
#   期望：PRO → edition=pro（19/27 特性）、ENT → edition=enterprise（27/27），version_match=true

# Word 图文手册结构自检（python-docx 可读、图片/表格数量正确）
python -c "from docx import Document; d=Document('docs/商业运营升级与发证操作手册_v1.11.15.docx'); print(len(d.paragraphs), len(d.tables), len(d.inline_shapes))"
```