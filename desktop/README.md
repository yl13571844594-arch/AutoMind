# AutoMind 桌面版构建

桌面版 = **pywebview（WebView2）窗口壳 + 系统托盘 + 内嵌 uvicorn 服务**。
后端与 React 前端零改动；数据目录自动收敛到 `%APPDATA%\AutoMind`
（由 `automind/core/paths.py` 统一解析，开发/pip 模式仍是 cwd `.automind`）。

## 一、环境准备

```bash
pip install -e "..[web]"                       # 项目本体（仓库根目录执行则为 .[web]）
pip install pyinstaller pywebview pystray pillow
```

## 二、构建步骤

```bash
cd desktop
python make_icon.py                  # 生成 icon.ico（品牌渐变图标）
pyinstaller automind.spec --noconfirm
```

产物：`desktop/dist/AutoMind/AutoMind.exe`（onedir，约 60-90MB）。

冒烟测试（无 GUI 也可）：

```bash
dist\AutoMind\AutoMind.exe --server-only --port 18765
curl http://127.0.0.1:18765/api/health     # → {"status":"ok", ... "frozen": true}
```

## 三、安装器（Inno Setup 6）

```bash
winget install JRSoftware.InnoSetup        # 一次性安装编译器
# （可选但推荐）官方 WebView2 引导器，内嵌进安装包给精简系统兜底：
curl -L -o MicrosoftEdgeWebview2Setup.exe "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" installer.iss
```

产物：`Output/AutoMind-Setup-<ver>.exe`（约 32MB，lzma2/max 压缩）。

- 中文/英文安装向导（简体中文语言文件 `ChineseSimplified.isl` 随仓库分发，
  取自官方翻译页收录版本）；
- 默认按用户安装（免管理员，落 `%LOCALAPPDATA%\Programs\AutoMind`），
  管理员运行可选装到 Program Files；
- 自动检测并静默补装 WebView2 运行时（注册表已有则跳过）；
- 卸载时询问是否保留用户数据（静默卸载默认保留）。

已通过的 QA 闭环：静默安装 → 快捷方式/卸载项验证 → 已安装程序启动
（窗口/托盘/服务就绪）→ 静默卸载（程序清除、数据保留）。

## 四、运行行为

| 场景 | 行为 |
|------|------|
| 正常启动 | WebView2 窗口 + 托盘图标；关窗隐藏到托盘，任务/定时继续跑 |
| 无 WebView2 | 自动降级系统默认浏览器（安装器通常已补装，属兜底） |
| 无 pystray/Pillow | 无托盘，关窗即退出 |
| `--server-only` | 仅起服务（CI 冒烟 / 服务器场景） |
| `--browser` | 跳过 WebView 直接开浏览器 |

数据全部在 `%APPDATA%\AutoMind`（配置 config.json、automind.db、
sessions.db、kb/、skills/）；托盘菜单「打开数据目录」可直达。

## 五、一键发布与代码签名

```powershell
# 完整流水线：前端构建 → PyInstaller → 签名 exe → 安装器 → 签名 Setup → 校验
.\build_release.ps1              # 未配置证书时自动产出未签名构建
.\build_release.ps1 -SkipWeb     # web 未改动时提速
```

**OV 证书到手后（一次性配置，之后每次构建自动签名）：**

1. 双击 PFX 导入「当前用户 → 个人」证书库；
2. 取证书指纹：`Get-ChildItem Cert:\CurrentUser\My`（40 位 Thumbprint）；
3. `setx AUTOMIND_CERT_THUMBPRINT <指纹>`（或临时 `$env:AUTOMIND_CERT_THUMBPRINT=...`）；
4. 重跑 `.\build_release.ps1` → AutoMind.exe 与 Setup.exe 均带
   SHA256 签名 + RFC3161 时间戳（sectigo/digicert/globalsign 自动回退），
   SmartScreen 蓝色弹窗随信誉累积消失。

不想导入证书库也可用 PFX 直签：设 `AUTOMIND_CERT_PFX` + `AUTOMIND_CERT_PWD`
（密码只放环境变量）。手动签名/验签：`.\sign.ps1 -Path <文件>` /
`.\sign.ps1 -Path <文件> -VerifyOnly`。

## 六、自动更新

应用内置更新通道（左下角「⚙ 设置 → 🔄 检查更新」，启动 3 秒后也会静默检查并
弹可点通知）：

- **检查**：查询 GitHub Releases 最新版（结果缓存 6 小时）；
- **升级（桌面版）**：下载 `AutoMind-Setup-<ver>.exe` → **Authenticode 签名
  校验**（当前程序已签名时，强制要求新包签名有效且同一发布者 —— 防投毒/降级）
  → 静默安装 → 自动重启，配置与数据全保留；
- **pip/源码模式**：给出 `pip install -U "automind-agent[web]"` 一键复制。

发布新版本 = 打 tag 推 GitHub Release 并上传 `AutoMind-Setup-<ver>.exe`
资产，存量用户即可收到更新提示。

## 七、macOS / Linux 打包

三平台共用同一 `main.py` 与 `automind.spec`（spec 内按平台自动切换图标/
产物形态）。**推荐用 CI 一键出三平台安装包**（本地跨平台构建不可行 ——
PyInstaller 不交叉编译）：

- **GitHub Actions**：`.github/workflows/desktop-build.yml`
  （手动 Run 或推 `v*.*.*` tag 触发）产出（三平台单次跑通，已验证）：
  - Windows → `AutoMind-Setup-<ver>.exe`（≈26 MB）
  - macOS → `AutoMind-<ver>.dmg`（≈56 MB，**通用二进制** x86_64+arm64）
  - Linux → `automind_<ver>_amd64.deb`（≈70 MB，Debian/Ubuntu）

  macOS 通用 DMG 的做法：全程在 `macos-14`(arm64) 单机完成 —— arm64 片原生
  PyInstaller 构建，x86_64 片经 **Rosetta 2**（`arch -x86_64` + 同一 universal2
  Python）构建，二者 `lipo` 合并成通用二进制（见 `packaging/macos/
  merge_universal.sh`），规避 Intel(`macos-13`) runner 池排队。

本机各自构建（在对应系统上；PyInstaller 不交叉编译）：

```bash
# 通用准备
pip install -e "..[desktop]"
cd desktop && python make_icon.py          # 生成 icon.ico/.png/.icns

# macOS —— 默认按本机架构原生构建（单架构 DMG）
pyinstaller automind.spec --noconfirm      # → dist/AutoMind.app（当前架构）
brew install create-dmg                     # 可选，更美观的 DMG 窗口
bash packaging/macos/build_dmg.sh          # → Output/AutoMind-<ver>.dmg
# 需通用二进制：分别在 arm64/x86_64（或 Rosetta）各构建一次 .app，再合并：
#   bash packaging/macos/merge_universal.sh <arm64.app> <x86_64.app> dist/AutoMind.app

# Linux（Debian/Ubuntu，需 WebKit2GTK）
sudo apt-get install -y python3-dev libgirepository1.0-dev libcairo2-dev \
     gir1.2-gtk-3.0 gir1.2-webkit2-4.1 libwebkit2gtk-4.1-0 fakeroot
pip install "pygobject<3.50" pycairo
pyinstaller automind.spec --noconfirm      # → dist/AutoMind/
bash packaging/linux/build_deb.sh          # → Output/automind_<ver>_amd64.deb
```

macOS 数据目录 `~/Library/Application Support/AutoMind`，Linux
`~/.local/share/automind`（`automind/core/paths.py` 统一解析）。

**签名**：CI 默认产未签名/ad-hoc 包（可正常安装运行）。要正式签名：
- Windows → 本机 `build_release.ps1`（Certum 云证书，见 `SIGNING.md`）；
- macOS → 仓库 Secrets 配 `MAC_CERT_P12_BASE64`/`MAC_CERT_PASSWORD`/
  `MAC_CODESIGN_IDENTITY` 即深度签名，再配 `MAC_NOTARY_APPLE_ID`/
  `MAC_NOTARY_PASSWORD`/`MAC_NOTARY_TEAM_ID` 即公证装订（去除首启右键）；
- Linux → `.deb` 一般无需签名，直接 `dpkg -i` 安装。

## 八、空白界面（白屏）兼容

针对"部分电脑安装后界面空白"，桌面版内置多层兜底（`main.py`）：

- **GPU 合成型白屏**（老/虚拟显卡、远程桌面、虚拟机）：RDP 会话或历史白屏
  标记时启动即禁用 WebView2 GPU 加速；用户也可托盘「以兼容模式重启（修复
  空白界面）」一键自救；可用 `AUTOMIND_WEBVIEW_DISABLE_GPU=1/0` 强制开关。
- **加载/JS 执行型白屏**（导航失败、内核过旧）：加载后 JS 复核 React 是否
  真挂载，未挂载则自动降级系统浏览器并落自愈标记（下次禁 GPU）；前端
  `index.html` 自带白屏兜底，把纯空白替换成可读提示 + 刷新按钮。
- 前端构建基线锁定 `es2020`（`web/vite.config.ts`），规避个别旧内核不支持
  新语法直接白屏。

## 九、已知事项

- 未签名 exe 首次运行可能触发 SmartScreen「更多信息 → 仍要运行」；
  有收入后购买代码签名证书（OV）即可根治；
- chromadb / reportlab / ldap3 等重型可选依赖被 spec 显式排除，
  桌面版对应功能走内置降级路径（知识库用内置向量存储，审计导出用 HTML）。
