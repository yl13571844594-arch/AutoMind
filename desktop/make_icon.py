"""生成品牌图标 — 双色渐变圆点 + 内亮斑（多尺寸）。

产物（跨平台打包共用同一视觉）：
    icon.ico   Windows（PyInstaller/Inno）
    icon.png   Linux（.desktop / .deb，512×512）
    icon.icns  macOS（.app 包，best-effort；失败不阻断）
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


def draw(size: int) -> Image.Image:
    s = size * 4   # 超采样抗锯齿
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = cy = s // 2
    r = int(s * 0.42)
    # 径向渐变：#7b9fff → #b594ff
    for i in range(r, 0, -1):
        t = 1 - i / r
        color = (int(123 + (181 - 123) * t), int(159 + (148 - 159) * t), 255, 255)
        d.ellipse([cx - i, cy - i, cx + i, cy + i], fill=color)
    # 内亮斑（拟光感）
    hr = int(r * 0.45)
    hx, hy = cx - int(r * 0.28), cy - int(r * 0.28)
    for i in range(hr, 0, -1):
        alpha = int(90 * (i / hr))
        d.ellipse([hx - i, hy - i, hx + i, hy + i], fill=(255, 255, 255, alpha))
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    here = Path(__file__).parent

    # Windows .ico（多尺寸）
    ico = here / "icon.ico"
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [draw(s) for s in sizes]
    imgs[-1].save(ico, format="ICO", sizes=[(s, s) for s in sizes],
                  append_images=imgs[:-1])
    print(f"已生成 {ico}")

    # Linux .png（512×512，供 .desktop / hicolor 图标主题）
    png = here / "icon.png"
    draw(512).save(png, format="PNG")
    print(f"已生成 {png}")

    # macOS .icns（Pillow 直写；老版本或缺编码器时跳过，CI 可用 sips 兜底）
    icns = here / "icon.icns"
    try:
        base = draw(1024)
        base.save(icns, format="ICNS")
        print(f"已生成 {icns}")
    except Exception as e:  # noqa: BLE001
        print(f"跳过 icon.icns（{e}）；macOS 构建时可用 sips 从 icon.png 生成")


if __name__ == "__main__":
    main()
