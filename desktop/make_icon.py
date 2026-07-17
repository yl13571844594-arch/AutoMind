"""生成 desktop/icon.ico — 品牌双色渐变圆点 + 描边（多尺寸）。"""

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
    out = Path(__file__).parent / "icon.ico"
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [draw(s) for s in sizes]
    imgs[-1].save(out, format="ICO", sizes=[(s, s) for s in sizes],
                  append_images=imgs[:-1])
    print(f"已生成 {out}")


if __name__ == "__main__":
    main()
