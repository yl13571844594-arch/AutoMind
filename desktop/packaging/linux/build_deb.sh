#!/usr/bin/env bash
# AutoMind 桌面版 Linux 打包：PyInstaller onedir → .deb（Debian/Ubuntu，amd64）
#
# 前置：
#   pip install -e ".[desktop]"
#   # WebKit2GTK 运行/打包依赖（Ubuntu 22.04+/Debian 12+）：
#   sudo apt-get install -y python3-gi gir1.2-webkit2-4.1 \
#        libgirepository1.0-dev gir1.2-gtk-3.0
#   cd desktop && python make_icon.py && pyinstaller automind.spec --noconfirm
#
# 运行：bash packaging/linux/build_deb.sh
# 产物：desktop/Output/automind_<ver>_amd64.deb
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP="$(cd "$HERE/../.." && pwd)"
ROOT="$(cd "$DESKTOP/.." && pwd)"

VER="$(grep -oP '__version__\s*=\s*"\K[\d.]+' "$ROOT/automind/__init__.py")"
[ -n "$VER" ] || { echo "无法解析版本号"; exit 1; }
echo "== AutoMind .deb 构建 == 版本 v$VER"

DIST="$DESKTOP/dist/AutoMind"
[ -d "$DIST" ] || { echo "缺少 $DIST — 请先运行 pyinstaller automind.spec --noconfirm"; exit 1; }

PKG="automind"
ARCH="amd64"
STAGE="$DESKTOP/build/deb/${PKG}_${VER}_${ARCH}"
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" \
         "$STAGE/opt/automind" \
         "$STAGE/usr/bin" \
         "$STAGE/usr/share/applications" \
         "$STAGE/usr/share/icons/hicolor/512x512/apps"

# 1) 程序文件 → /opt/automind
cp -a "$DIST/." "$STAGE/opt/automind/"
chmod 755 "$STAGE/opt/automind/AutoMind"

# 2) /usr/bin 启动器（避免依赖 $PATH 里的具体路径）
cat > "$STAGE/usr/bin/automind" <<'EOF'
#!/bin/sh
exec /opt/automind/AutoMind "$@"
EOF
chmod 755 "$STAGE/usr/bin/automind"

# 3) 图标
if [ -f "$DESKTOP/icon.png" ]; then
  cp "$DESKTOP/icon.png" "$STAGE/usr/share/icons/hicolor/512x512/apps/automind.png"
fi

# 4) .desktop 桌面入口
cat > "$STAGE/usr/share/applications/automind.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=AutoMind
GenericName=Automation Agent
Comment=通用自动化 Agent（分层规划 · 符号推理 · MCP · 多模型）
Exec=/usr/bin/automind %U
Icon=automind
Terminal=false
Categories=Development;Utility;
StartupNotify=true
StartupWMClass=AutoMind
EOF

# 5) 安装体积（KB）
INSTALLED_KB="$(du -sk "$STAGE/opt" | cut -f1)"

# 6) DEBIAN/control
cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PKG
Version: $VER
Section: utils
Priority: optional
Architecture: $ARCH
Maintainer: AutoMind Team <noreply@automind.dev>
Installed-Size: $INSTALLED_KB
Depends: libc6 (>= 2.31), libgtk-3-0, libwebkit2gtk-4.1-0 | libwebkit2gtk-4.0-37
Recommends: gir1.2-webkit2-4.1 | gir1.2-webkit2-4.0
Homepage: https://github.com/yl13571844594-arch/AutoMind
Description: 通用自动化 Agent（社区版）
 AutoMind 是支持分层规划、符号推理、自我纠错、MCP 与多模型后端的
 通用自动化 Agent。桌面版内嵌本地服务并以 WebKit2GTK 窗口呈现工作台，
 数据保存在 ~/.local/share/automind。
EOF

# 7) 维护脚本：刷新桌面/图标缓存
cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database -q /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi
exit 0
EOF
chmod 755 "$STAGE/DEBIAN/postinst"

cat > "$STAGE/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
  fi
fi
exit 0
EOF
chmod 755 "$STAGE/DEBIAN/postrm"

# 8) 构建 .deb
OUT="$DESKTOP/Output"
mkdir -p "$OUT"
DEB="$OUT/${PKG}_${VER}_${ARCH}.deb"
# root:root 属主更规范（fakeroot 存在则用；CI/本机无则退化）
if command -v fakeroot >/dev/null 2>&1; then
  fakeroot dpkg-deb --build --root-owner-group "$STAGE" "$DEB"
else
  dpkg-deb --build --root-owner-group "$STAGE" "$DEB"
fi

echo ""
echo "✅ 构建完成：$DEB"
ls -lh "$DEB"
command -v dpkg-deb >/dev/null 2>&1 && dpkg-deb --info "$DEB" | sed -n '1,20p' || true
