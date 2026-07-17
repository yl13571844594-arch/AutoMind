# -*- mode: python ; coding: utf-8 -*-
# AutoMind 桌面版 PyInstaller 配置（onedir：启动快、杀软误报低）
#   构建：cd desktop && pyinstaller automind.spec --noconfirm
#   产物：desktop/dist/AutoMind/AutoMind.exe

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

hiddenimports = (
    collect_submodules("automind")
    + collect_submodules("uvicorn")
    + ["pystray._win32", "PIL.Image", "PIL.ImageDraw"]
)

# 静态资源显式声明（collect_data_files 对非安装式包不可靠）：
# React 构建产物 + 经典界面 + 内置手册 全部随包
datas = [(os.path.join(ROOT, "automind", "static"), "automind/static")]
datas += collect_data_files("automind")
icon_path = os.path.join(SPECPATH, "icon.ico")
if os.path.exists(icon_path):
    datas.append((icon_path, "."))

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
    icon=icon_path if os.path.exists(icon_path) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="AutoMind",
)
