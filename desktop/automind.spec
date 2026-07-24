# -*- mode: python ; coding: utf-8 -*-
# AutoMind 桌面版 PyInstaller 配置（跨平台 onedir：启动快、杀软误报低）
#
#   Windows：pyinstaller automind.spec --noconfirm → dist/AutoMind/AutoMind.exe
#   macOS  ：pyinstaller automind.spec --noconfirm → dist/AutoMind.app（通用二进制）
#   Linux  ：pyinstaller automind.spec --noconfirm → dist/AutoMind/AutoMind
#
# 平台差异集中在本文件尾部（图标格式、macOS .app BUNDLE、通用二进制架构），
# 其余收集逻辑三平台共用。

import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

# ── 平台图标 ──────────────────────────────────────────
#   Windows .ico / macOS .icns / Linux 无（.desktop 引用 icon.png，另行安装）
_icon_name = "icon.icns" if IS_MAC else "icon.ico"
icon_path = os.path.join(SPECPATH, _icon_name)
icon_arg = icon_path if os.path.exists(icon_path) else None

hiddenimports = (
    collect_submodules("automind")
    + collect_submodules("uvicorn")
    + ["PIL.Image", "PIL.ImageDraw"]
)
# 托盘后端按平台收集（win32 / AppIndicator / Cocoa）
if IS_WIN:
    hiddenimports += ["pystray._win32"]
elif IS_MAC:
    hiddenimports += ["pystray._darwin"]
else:
    hiddenimports += ["pystray._xorg", "pystray._appindicator"]

# 静态资源显式声明（collect_data_files 对非安装式包不可靠）：
# React 构建产物 + 经典界面 + 内置手册 全部随包
datas = [(os.path.join(ROOT, "automind", "static"), "automind/static")]
datas += collect_data_files("automind")
# 托盘图标随包（Windows 用 .ico；mac/linux 用 .png）
for _ico in ("icon.ico", "icon.png"):
    _p = os.path.join(SPECPATH, _ico)
    if os.path.exists(_p):
        datas.append((_p, "."))

a = Analysis(
    [os.path.join(SPECPATH, "main.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # 明确排除重型可选依赖（桌面版按需增强，不默认捆绑）
        "chromadb", "torch", "tensorflow", "matplotlib", "pandas",
        "reportlab", "ldap3", "tkinter", "pytest",
    ],
    noarchive=False,
    # macOS 通用二进制（Apple Silicon + Intel 同一 .app）；
    # 依赖需提供 universal2/两架构 wheel，CI 在 universal2 Python 上构建。
    target_arch="universal2" if IS_MAC else None,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AutoMind",
    debug=False,
    strip=False,
    upx=False,
    console=False,               # GUI 应用：无黑窗
    disable_windowed_traceback=False,
    icon=icon_arg,
    # macOS 代码签名在 CI 打包后统一处理（此处占位，未配置则 ad-hoc）
    codesign_identity=os.environ.get("AUTOMIND_MAC_CODESIGN_IDENTITY") or None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="AutoMind",
)

# ── macOS：打成 .app 应用包 ────────────────────────────
if IS_MAC:
    app = BUNDLE(
        coll,
        name="AutoMind.app",
        icon=icon_arg,
        bundle_identifier="com.automind.desktop",
        version=os.environ.get("AUTOMIND_VERSION", "1.2.0"),
        info_plist={
            "CFBundleName": "AutoMind",
            "CFBundleDisplayName": "AutoMind",
            "NSHighResolutionCapable": True,
            # 内嵌 WKWebView 加载 http://127.0.0.1 需允许本机明文回环
            "NSAppTransportSecurity": {
                "NSAllowsLocalNetworking": True,
            },
            "LSMinimumSystemVersion": "11.0",
            "LSApplicationCategoryType": "public.app-category.productivity",
        },
    )
