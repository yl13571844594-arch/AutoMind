"""用打包好的桌面版（--server-only）截取 README 界面图。

为什么不用 mock 或手工截图：
  · 手工截图会带开发机的时间、任务历史与个人路径；
  · mock 出来的界面与真机不一致，README 就变成"货不对板"。
这里跑的是**发行版冻结包**本身，看到什么就截什么。

用法：
    python scripts/shot_readme.py <base_url> <out_dir>
"""

from __future__ import annotations

import sys
from pathlib import Path

# README 现用图的尺寸基准（device_scale_factor=2）：保持同一比例，换图不跳动
VIEWPORT = {"width": 1050, "height": 660}
SCALE = 2

#: 「侧边栏文字 → 截图文件名」——README 界面一览用的几张图
NAV_SHOTS = [
    ("工具面板", "tools.png"),
    ("观测中心", "observe.png"),
    ("计划视图", "plan.png"),
    ("知识库", "kb.png"),
]


def _dismiss_onboarding(page) -> None:
    """关掉首次运行的 4 步引导。

    不关掉的话每张图正中都是同一个弹窗 —— 那是"用户第一次打开"的样子，
    不是"这个界面长什么样"，README 需要的是后者。
    """
    for text in ("跳过引导", "跳过"):
        try:
            el = page.get_by_text(text, exact=True).first
            if el.is_visible(timeout=2500):
                el.click()
                page.wait_for_timeout(700)
                return
        except Exception:
            continue
    # 兜底：右上角关闭按钮
    try:
        page.locator(".ant-modal-close, [aria-label='Close']").first.click(timeout=2000)
        page.wait_for_timeout(700)
    except Exception:
        pass


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18770"
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "docs/images")
    out.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge")
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=SCALE)

        page.goto(base, wait_until="load", timeout=60_000)
        try:
            page.wait_for_selector("#sidebar, .sidebar, aside", timeout=30_000)
        except Exception:
            page.wait_for_timeout(3000)
        page.wait_for_timeout(2000)
        _dismiss_onboarding(page)
        page.wait_for_timeout(1500)

        target = out / "chat.png"
        page.screenshot(path=str(target))
        print(f"[shot] {target}  {target.stat().st_size} bytes")

        for label, name in NAV_SHOTS:
            try:
                page.get_by_text(label, exact=True).first.click(timeout=6000)
                page.wait_for_timeout(2600)      # 视图切换 + 数据加载
                target = out / name
                page.screenshot(path=str(target))
                print(f"[shot] {target}  {target.stat().st_size} bytes")
            except Exception as e:
                print(f"[skip] {label}: {type(e).__name__}: {e}")

        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
