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

## 五、已知事项

- 未签名 exe 首次运行可能触发 SmartScreen「更多信息 → 仍要运行」；
  有收入后购买代码签名证书（OV）即可根治；
- chromadb / reportlab / ldap3 等重型可选依赖被 spec 显式排除，
  桌面版对应功能走内置降级路径（知识库用内置向量存储，审计导出用 HTML）。
