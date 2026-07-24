#!/usr/bin/env bash
# 把两个原生架构的 .app（arm64 + x86_64）lipo 合并成一个通用二进制 .app。
#
# 用法：merge_universal.sh <arm64.app> <x86_64.app> <out.app>
# 原理：以 arm64 版为骨架整体复制为输出；遍历其中每个 Mach-O 文件，与
#       x86_64 版对应文件 lipo -create 合并成 fat。非 Mach-O 文件（.pyc、
#       静态资源等）与架构无关，直接沿用 arm64 版即可。
set -euo pipefail

ARM="$1"; X86="$2"; OUT="$3"
[ -d "$ARM" ] || { echo "缺少 arm64 .app：$ARM"; exit 1; }
[ -d "$X86" ] || { echo "缺少 x86_64 .app：$X86"; exit 1; }

rm -rf "$OUT"
cp -R "$ARM" "$OUT"

merged=0; skipped=0
# 用 NUL 分隔遍历，兼容含空格路径
while IFS= read -r -d '' f; do
  # 仅处理 Mach-O（可执行/dylib/bundle）
  if file "$f" | grep -q "Mach-O"; then
    rel="${f#"$OUT"/}"
    other="$X86/$rel"
    if [ -f "$other" ] && file "$other" | grep -q "Mach-O"; then
      # 已是 fat 的跳过；否则合并两架构
      if lipo -info "$f" 2>/dev/null | grep -q "Architectures in the fat"; then
        skipped=$((skipped+1))
      else
        lipo -create "$f" "$other" -output "$f" 2>/dev/null \
          && merged=$((merged+1)) || skipped=$((skipped+1))
      fi
    else
      skipped=$((skipped+1))   # x86 侧无对应（架构专属），保留 arm64 版
    fi
  fi
done < <(find "$OUT" -type f -print0)

echo "lipo 合并完成：merged=$merged skipped=$skipped"

# 校验主可执行文件确为双架构
MAIN="$OUT/Contents/MacOS/AutoMind"
if [ -f "$MAIN" ]; then
  ARCHS="$(lipo -archs "$MAIN" 2>/dev/null || echo '?')"
  echo "主程序架构：$ARCHS"
  case "$ARCHS" in
    *x86_64*arm64*|*arm64*x86_64*) echo "✓ 通用二进制校验通过";;
    *) echo "⚠ 主程序非双架构（$ARCHS）—— 请检查两侧构建产物";;
  esac
fi
