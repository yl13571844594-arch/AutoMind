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

## 七、已知事项

- 未签名 exe 首次运行可能触发 SmartScreen「更多信息 → 仍要运行」；
  有收入后购买代码签名证书（OV）即可根治；
- chromadb / reportlab / ldap3 等重型可选依赖被 spec 显式排除，
  桌面版对应功能走内置降级路径（知识库用内置向量存储，审计导出用 HTML）。
