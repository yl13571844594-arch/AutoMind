#!/usr/bin/env bash
# AutoMind 桌面版 macOS 打包：PyInstaller .app → 通用 DMG（Apple Silicon + Intel）
#
# 前置（在 universal2 Python 上，产出通用二进制的 .app）：
#   pip install -e ".[desktop]"
#   cd desktop && python make_icon.py && pyinstaller automind.spec --noconfirm
#   # → dist/AutoMind.app
#
# 运行：bash packaging/macos/build_dmg.sh
# 产物：desktop/Output/AutoMind-<ver>.dmg
#
# 签名/公证（可选，配了证书才生效）：
#   AUTOMIND_MAC_CODESIGN_IDENTITY="Developer ID Application: NAME (TEAMID)"
#   配了则对 .app 做深度签名；未配则 ad-hoc 签名（用户首次需右键「打开」）。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP="$(cd "$HERE/../.." && pwd)"
ROOT="$(cd "$DESKTOP/.." && pwd)"

VER="$(grep -oE '__version__ *= *"[0-9.]+"' "$ROOT/automind/__init__.py" | grep -oE '[0-9.]+')"
[ -n "$VER" ] || { echo "无法解析版本号"; exit 1; }
echo "== AutoMind DMG 构建 == 版本 v$VER"

APP="$DESKTOP/dist/AutoMind.app"
[ -d "$APP" ] || { echo "缺少 $APP — 请先运行 pyinstaller automind.spec --noconfirm"; exit 1; }

# 确认是否为通用二进制（Apple Silicon + Intel），仅提示不阻断
BIN="$APP/Contents/MacOS/AutoMind"
if [ -f "$BIN" ]; then
  echo "架构：$(lipo -archs "$BIN" 2>/dev/null || echo '未知')"
fi

# 1) 代码签名（深度）——配置了 Developer ID 用之，否则 ad-hoc
IDENTITY="${AUTOMIND_MAC_CODESIGN_IDENTITY:-}"
if [ -n "$IDENTITY" ]; then
  echo "签名：$IDENTITY（深度签名 + hardened runtime）"
  codesign --force --deep --options runtime --timestamp \
           --sign "$IDENTITY" "$APP"
  codesign --verify --deep --strict --verbose=2 "$APP" || true
else
  echo "签名：ad-hoc（未配置 Developer ID；用户首次需右键→打开）"
  codesign --force --deep --sign - "$APP" || true
fi

# 2) 组装 DMG 内容（应用 + /Applications 快捷方式，拖拽安装）
OUT="$DESKTOP/Output"
mkdir -p "$OUT"
DMG="$OUT/AutoMind-$VER.dmg"
rm -f "$DMG"

if command -v create-dmg >/dev/null 2>&1; then
  # create-dmg：带背景/图标布局的漂亮安装窗口
  create-dmg \
    --volname "AutoMind $VER" \
    --window-pos 200 120 --window-size 640 400 \
    --icon-size 128 \
    --icon "AutoMind.app" 160 190 \
    --app-drop-link 480 190 \
    --no-internet-enable \
    "$DMG" "$APP" || {
      echo "create-dmg 失败，回退 hdiutil"; NEED_HDIUTIL=1; }
else
  NEED_HDIUTIL=1
fi

if [ "${NEED_HDIUTIL:-0}" = "1" ]; then
  STAGE="$(mktemp -d)"
  cp -R "$APP" "$STAGE/"
  ln -s /Applications "$STAGE/Applications"
  hdiutil create -volname "AutoMind $VER" -srcfolder "$STAGE" \
    -ov -format UDZO "$DMG"
  rm -rf "$STAGE"
fi

# 3) 签名 DMG 本身（配了证书时）
if [ -n "$IDENTITY" ]; then
  codesign --force --sign "$IDENTITY" "$DMG" || true
fi

echo ""
echo "✅ 构建完成：$DMG"
ls -lh "$DMG"
echo "提示：如需分发到未公证机器，用户右键 AutoMind.app →「打开」一次即可。"
