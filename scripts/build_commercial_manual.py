#!/usr/bin/env python3
"""生成《商业运营升级与发证操作手册 v1.11.17》（Word + 配图）与全部图表资产。

产物：
    docs/商业运营升级与发证操作手册_v1.11.17.docx
    docs/manual_assets/fig01_*.png ... （宽 1500px，微软雅黑，深色标题 + 浅色底）

用法（用带 python-docx / Pillow 的 Python 运行）::

    python scripts/build_commercial_manual.py            # 图表 + Word 全量重新生成
    python scripts/build_commercial_manual.py --charts   # 只重新生成图表
    python scripts/build_commercial_manual.py --docx     # 只重新生成 Word（图表已存在时）

脚本可重复运行：同名文件覆盖；图片先写临时文件再原子替换，避免 --charts 时
写到一半的 PNG 被 Word 引用。

版本常量集中在文件顶部（VERSION / RELEASE_DATE / PRODUCT_NAME）。商业运营 CLI
自 v1.11.15 起未变化（v1.11.17 的 `issue_license.py --help` 与之一致），
本手册中的命令示例仍以实际 `--help` 为准。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# ── 生成物常量（收尾更新版本时只改这里）────────────────────────────────
VERSION = "1.11.17"
RELEASE_DATE = "2026-10-23"
PRODUCT_NAME = "AutoMind"
MANUAL_TITLE = "商业运营升级与发证操作手册"
ASSET_DIR_NAME = "manual_assets"
DOCX_NAME = f"商业运营升级与发证操作手册_v{VERSION}.docx"

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs" / ASSET_DIR_NAME
OUT_DOCX = ROOT / "docs" / DOCX_NAME

# ── 作图基础设施 ───────────────────────────────────────────────────
W = 1500                       # 所有图表统一宽度（要求 1400-1600px）
FONT_REG = Path("C:/Windows/Fonts/msyh.ttc")      # 微软雅黑
FONT_BOLD = Path("C:/Windows/Fonts/msyhbd.ttc")   # 微软雅黑 Bold
FONT_FALLBACK = Path("C:/Windows/Fonts/simhei.ttf")

#: 深色标题 + 浅色底 的一套配色
INK = (31, 42, 68)             # #1F2A44 深色标题/正文
INK2 = (94, 108, 134)          # 次级文字
MUTED = (132, 145, 168)
BLUE = (47, 111, 235)
BLUE_L = (233, 240, 254)
GREEN = (22, 143, 92)
GREEN_L = (228, 246, 237)
AMBER = (186, 112, 8)
AMBER_L = (255, 246, 225)
RED = (196, 54, 54)
RED_L = (252, 235, 235)
PURPLE = (108, 76, 214)
PURPLE_L = (241, 237, 253)
TEAL = (13, 128, 138)
TEAL_L = (228, 246, 247)
BG = (246, 248, 252)
WHITE = (255, 255, 255)
BORDER = (209, 218, 235)
GRID = (226, 232, 242)

_FONT_CACHE: dict[tuple[int, bool], object] = {}


def font(size: int, bold: bool = False):
    """取微软雅黑字体（找不到时退黑体），大小按像素。"""
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    from PIL import ImageFont

    path = (FONT_BOLD if bold else FONT_REG)
    if not path.is_file():
        path = FONT_FALLBACK
    f = ImageFont.truetype(str(path), size=size, index=0)
    _FONT_CACHE[key] = f
    return f


def text_w(draw, text: str, f) -> int:
    box = draw.textbbox((0, 0), text, font=f)
    return box[2] - box[0]


def wrap(draw, text: str, f, max_w: int) -> list[str]:
    """中英混排折行：优先按空格断词，超长 token / 行按字符硬断。"""
    lines: list[str] = []
    for raw_line in str(text).split("\n"):
        if not raw_line:
            lines.append("")
            continue
        cur = ""
        for token in _tokens(raw_line):
            if not cur and text_w(draw, token, f) > max_w:
                # 单个 token 本身就超宽（长文件名 / URL）：按字符硬切
                chunk = ""
                for ch in token:
                    if chunk and text_w(draw, chunk + ch, f) > max_w:
                        lines.append(chunk)
                        chunk = ch
                    else:
                        chunk += ch
                cur = chunk
                continue
            cand = cur + token
            if text_w(draw, cand, f) <= max_w or not cur:
                cur = cand
            else:
                lines.append(cur.rstrip())
                cur = token.lstrip()
        lines.append(cur.rstrip())
    # 最后再过一遍，保证没有一行超宽（中文没有空格时上面可能仍留下超宽行）
    out: list[str] = []
    for line in lines:
        while len(line) > 1 and text_w(draw, line, f) > max_w:
            cut = len(line)
            while cut > 1 and text_w(draw, line[:cut], f) > max_w:
                cut -= 1
            out.append(line[:cut])
            line = line[cut:]
        out.append(line)
    return out


def _tokens(line: str) -> list[str]:
    """把一行拆成「词 + 尾随空格」的 token，中文按单字。"""
    out: list[str] = []
    buf = ""
    for ch in line:
        if ch == " ":
            buf += ch
            out.append(buf)
            buf = ""
        elif ord(ch) < 128:
            buf += ch
        else:
            if buf:
                out.append(buf)
                buf = ""
            out.append(ch)
    if buf:
        out.append(buf)
    return out


def canvas(title: str, subtitle: str = "", height: int = 800):
    """新建一张浅底图 + 顶部标题，返回 (image, draw)。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (W, height), BG)
    draw = ImageDraw.Draw(img)
    # 顶部一条浅色装饰带
    draw.rectangle([0, 0, W, 10], fill=BLUE)
    draw.text((60, 42), title, font=font(40, True), fill=INK)
    if subtitle:
        draw.text((62, 98), subtitle, font=font(23), fill=INK2)
    draw.text((W - 60 - text_w(draw, f"AutoMind v{VERSION}", font(20)), height - 40),
              f"AutoMind v{VERSION}", font=font(20), fill=MUTED)
    draw.text((60, height - 40), f"{MANUAL_TITLE} · 内部资料", font=font(20), fill=MUTED)
    return img, draw


def rounded(draw, box, fill=WHITE, outline=BORDER, radius=18, width=2):
    draw.rounded_rectangle(list(box), radius=radius, fill=fill, outline=outline, width=width)


def pill(draw, x, y, text, *, fill, fg, f, padx=16, pady=8, outline=None):
    w = text_w(draw, text, f) + padx * 2
    h = f.size + pady * 2
    box = (x, y, x + w, y + h)
    draw.rounded_rectangle(list(box), radius=h // 2, fill=fill,
                           outline=outline or fill, width=2)
    draw.text((x + padx, y + pady - 2), text, font=f, fill=fg)
    return box


def badge(draw, cx, cy, text, fill, fg=WHITE, r=20, f=None):
    f = f or font(20, True)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill)
    tw = text_w(draw, text, f)
    draw.text((cx - tw / 2, cy - r * 0.62), text, font=f, fill=fg)


def arrow(draw, x1, y1, x2, y2, color=INK2, width=3, head=13):
    import math

    draw.line([(x1, y1), (x2, y2)], fill=color, width=width)
    ang = math.atan2(y2 - y1, x2 - x1)
    for s in (-0.5, 0.5):
        draw.line([(x2, y2),
                   (x2 - head * math.cos(ang + s), y2 - head * math.sin(ang + s))],
                  fill=color, width=width)


def dashed_line(draw, xy, color=MUTED, width=3, dash=14, gap=10):
    import math

    x1, y1, x2, y2 = xy
    total = math.hypot(x2 - x1, y2 - y1)
    if total == 0:
        return
    ux, uy = (x2 - x1) / total, (y2 - y1) / total
    pos = 0.0
    while pos < total:
        end = min(pos + dash, total)
        draw.line([(x1 + ux * pos, y1 + uy * pos),
                   (x1 + ux * end, y1 + uy * end)], fill=color, width=width)
        pos = end + gap


def lines_centered(draw, cx, y, lines, f, fill, gap=8):
    for i, line in enumerate(lines):
        tw = text_w(draw, line, f)
        draw.text((cx - tw / 2, y + i * (f.size + gap)), line, font=f, fill=fill)


def save_png(img, name: str) -> Path:
    """先写临时文件再原子替换，避免被 Word 半截读取。"""
    ASSETS.mkdir(parents=True, exist_ok=True)
    out = ASSETS / name
    tmp = out.with_suffix(".png.tmp")
    img.save(tmp, format="PNG", optimize=True)
    tmp.replace(out)
    return out


# ── 图 1：商业激活总览流程 ─────────────────────────────────────────
def fig01_activation_overview() -> Path:
    img, d = canvas("商业激活总览：从发行方出包到客户可用",
                    "4 步闭环 · 全程离线验签 · 客户侧 1 分钟完成 · 私钥永不离开发行方", 780)
    cards = [
        ("1", "发行方准备", BLUE,
         ["一次性：keygen --write-public-key", "公钥写入 wheel（内置验签）", "私钥离线保管 / 备份", "doctor 自检全绿"]),
        ("2", "签发与打包", PURPLE,
         ["issue / issue-batch 签发", "bundle 打成 .ampkg", "wheel + 许可证 + 清单", "打印 sha256 并登记台账"]),
        ("3", "分发交付", TEAL,
         ["邮件 / IM 发送 .ampkg", "附激活步骤与 sha256", "按客户登记到期日", "提醒续费窗口"]),
        ("4", "客户激活", GREEN,
         ["设置 → ★ 升级专业版/企业版", "拖入 .ampkg 自动装包", "离线验签 + 就地激活", "提示重启则重启一次"]),
    ]
    x0, gap, cw, ch, y = 50, 40, 320, 418, 168
    for i, (num, title, color, items) in enumerate(cards):
        x = x0 + i * (cw + gap)
        rounded(d, (x, y, x + cw, y + ch), fill=WHITE, outline=BORDER, radius=20)
        d.rectangle([x, y, x + cw, y + 78], fill=color)
        d.rounded_rectangle([x, y, x + cw, y + 96], radius=20, fill=color)
        d.rectangle([x, y + 70, x + cw, y + 96], fill=color)
        badge(d, x + 44, y + 40, num, WHITE, fg=color, r=24, f=font(24, True))
        d.text((x + 80, y + 22), title, font=font(26, True), fill=WHITE)
        yy = y + 126
        for item in items:
            d.ellipse([x + 26, yy + 9, x + 36, yy + 19], fill=color)
            for line in wrap(d, item, font(21), cw - 90):
                d.text((x + 52, yy), line, font=font(21), fill=INK)
                yy += 31
            yy += 16
        if i < len(cards) - 1:
            arrow(d, x + cw + 4, y + ch // 2, x + cw + gap - 6, y + ch // 2, BLUE, 4, 14)
    rounded(d, (50, 632, 1450, 732), fill=BLUE_L, outline=(190, 210, 248), radius=16)
    d.text((90, 662), "关键纪律", font=font(24, True), fill=BLUE)
    d.text((250, 662), "私钥只在发行方离线介质；客户包内只有公钥（能验不能签）；"
                       "许可证与 .ampkg 均可离线校验，不依赖外网。",
           font=font(22), fill=INK)
    return save_png(img, "fig01_activation_overview.png")


# ── 图 2：角色与物料清单 / 密钥边界 ────────────────────────────────
def fig02_roles_materials() -> Path:
    img, d = canvas("角色与物料清单：谁拿什么、边界在哪",
                    "发行方掌握私钥；客户侧只有公钥与许可证 —— 这条边界就是付费墙本身", 900)
    # 中间边界
    dashed_line(d, (750, 150, 750, 780), color=RED, width=4, dash=16, gap=10)
    pill(d, 618, 140, "● 密钥边界：私钥永不跨越", fill=RED_L, fg=RED, f=font(21, True))
    # 左：发行方
    rounded(d, (50, 210, 700, 780), fill=WHITE, outline=(198, 212, 240), radius=20)
    d.rectangle([50, 210, 700, 286], fill=BLUE)
    d.rounded_rectangle([50, 210, 700, 306], radius=20, fill=BLUE)
    d.rectangle([50, 276, 700, 306], fill=BLUE)
    d.text((82, 236), "发行方（商业运营 / 交付实施）", font=font(26, True), fill=WHITE)
    left = [
        ("钥", "签发私钥 private_key.hex", "绝密 · 只存离线介质 / 密码库", RED),
        ("公", "验签公钥 keys.py::PUBLIC_KEY_HEX", "公开 · 随 wheel 分发", GREEN),
        ("包", "扩展包 wheel（内置公钥）", "与主程序同版本构建", PURPLE),
        ("证", "许可证 / .ampkg 升级包", "按客户签发并登记台账", TEAL),
        ("账", "商业运营台账 + sha256", "续费提醒与审计依据", AMBER),
        ("禁", "CRL 吊销清单", "随包或单独下发到客户端", RED),
    ]
    y = 322
    for icon, name, desc, color in left:
        rounded(d, (80, y, 670, y + 68), fill=BG, outline=BORDER, radius=12)
        d.text((96, y + 16), icon, font=font(26), fill=color)
        d.text((142, y + 10), name, font=font(22, True), fill=INK)
        d.text((142, y + 40), desc, font=font(19), fill=INK2)
        y += 76
    # 右：客户
    rounded(d, (800, 210, 1450, 780), fill=WHITE, outline=(198, 212, 240), radius=20)
    d.rectangle([800, 210, 1450, 286], fill=GREEN)
    d.rounded_rectangle([800, 210, 1450, 306], radius=20, fill=GREEN)
    d.rectangle([800, 276, 1450, 306], fill=GREEN)
    d.text((832, 236), "客户（终端用户 / 客户 IT）", font=font(26, True), fill=WHITE)
    right = [
        ("包", "扩展包 wheel", "只含公钥与验签代码，无签发路径", PURPLE),
        ("证", "许可证串（AMP2-…）", "档位 / 到期 / 席位 / 绑定机器", TEAL),
        ("包", ".ampkg 一键升级包", "拖进升级面板即装包 + 激活", GREEN),
        ("证", "~/.automind/license", "激活后许可证落盘位置", BLUE),
        ("禁", "license_crl.json", "吊销清单（本地或随包）", RED),
        ("时", "license_state.json", "时钟 / 复检状态（自动维护）", AMBER),
    ]
    y = 322
    for icon, name, desc, color in right:
        rounded(d, (830, y, 1420, y + 68), fill=BG, outline=BORDER, radius=12)
        d.text((846, y + 16), icon, font=font(26), fill=color)
        d.text((892, y + 10), name, font=font(22, True), fill=INK)
        d.text((892, y + 40), desc, font=font(19), fill=INK2)
        y += 76
    d.text((80, 812), "√ 客户机没有任何可执行签发代码：拿到 wheel 也无法伪造、改档位、改到期日或改席位。",
           font=font(21), fill=INK2)
    return save_png(img, "fig02_roles_materials.png")


# ── 图 3：许可证生命周期时间轴 ───────────────────────────────────
def fig03_license_lifecycle() -> Path:
    img, d = canvas("许可证生命周期：签发 → 生效 → 到期 → 宽限 → 续期",
                    "时间以客户本地日期为准；到期后自动降级社区版（数据不丢），续期包拖入即恢复", 820)
    axis_y = 330
    x1, x2 = 90, 1410
    arrow(d, x1, axis_y, x2, axis_y, INK2, 4, 16)

    marks = [
        (150, "签发日", "issued", "台账登记", BLUE),
        (380, "生效日", "--start", "可选，未来生效", TEAL),
        (710, "正常使用", "周期复检", "默认 3600 秒", GREEN),
        (1000, "到期日", "expiry", "YYYYMMDD", AMBER),
        (1190, "宽限期", "GRACE_DAYS", "默认 0 天", RED),
        (1370, "续期", "bundle 新证", "拖入即恢复", PURPLE),
    ]
    for x, title, code, note, color in marks:
        d.ellipse([x - 13, axis_y - 13, x + 13, axis_y + 13], fill=color, outline=WHITE, width=3)
        d.line([(x, axis_y - 20), (x, axis_y - 44)], fill=color, width=3)
        tw = text_w(d, title, font(24, True))
        d.text((x - tw / 2, axis_y - 116), title, font=font(24, True), fill=INK)
        cw = text_w(d, code, font(18, True))
        d.text((x - cw / 2, axis_y - 78), code, font=font(18, True), fill=color)
        nw = text_w(d, note, font(17))
        d.text((x - nw / 2, axis_y + 34), note, font=font(17), fill=INK2)
    # 有效 / 宽限 / 过期 区段
    seg_y = axis_y + 76
    d.rounded_rectangle([150, seg_y, 980, seg_y + 54], radius=12, fill=GREEN_L)
    d.text((470, seg_y + 14), "有效期内：全部商业特性可用", font=font(23, True), fill=GREEN)
    d.rounded_rectangle([990, seg_y, 1290, seg_y + 54], radius=12, fill=AMBER_L)
    d.text((1004, seg_y + 14), "宽限期（可选）", font=font(23, True), fill=AMBER)
    d.rounded_rectangle([1190, seg_y + 72, 1410, seg_y + 126], radius=12, fill=RED_L)
    d.text((1204, seg_y + 86), "过期 → 社区版", font=font(23, True), fill=RED)
    arrow(d, 1290, seg_y + 54, 1420, seg_y + 92, RED, 3, 12)
    # 永久许可证
    rounded(d, (90, 590, 1410, 690), fill=BLUE_L, outline=(190, 210, 248), radius=14)
    d.text((120, 612), "永久许可证", font=font(25, True), fill=BLUE)
    d.text((330, 614), "expiry = 00000000：无到期日、无需续期；换档 / 换绑机器时重新签发并替换。",
           font=font(22), fill=INK)
    d.text((90, 714), "提示：宽限期默认 0 天（过期即降级），建议年度客户设置 7~15 天；宽限只对「到期」生效，"
                      "吊销 / 机器不符 / 时钟回拨不享受宽限。not-before 未到前旧证继续有效。",
           font=font(19), fill=INK2)
    return save_png(img, "fig03_license_lifecycle.png")


# ── 图 4：版本升级决策树 ─────────────────────────────────────────
def fig04_upgrade_decision_tree() -> Path:
    img, d = canvas("版本升级决策树：客户该拖包、重启，还是让发行方重出包？",
                    "先看「扩展包版本 = 主程序版本」，再看「当前进程是否已加载旧扩展」", 940)

    def node(x, y, w, h, text_lines, fill, outline, title=True, f=None):
        rounded(d, (x, y, x + w, y + h), fill=fill, outline=outline, radius=14)
        f = f or font(21, bool(title))
        yy = y + h / 2 - (len(text_lines) * (f.size + 8)) / 2
        for line in text_lines:
            lw = text_w(d, line, f)
            d.text((x + w / 2 - lw / 2, yy), line, font=f, fill=INK)
            yy += f.size + 8

    node(600, 158, 300, 62, ["客户要激活 / 升级商业版"], BLUE, BLUE, f=font(23, True))
    node(600, 262, 300, 66, ["已安装商业扩展包？"], PURPLE_L, PURPLE)
    node(600, 384, 300, 66, ["扩展包版本 = 主程序？"], AMBER_L, AMBER)
    node(600, 506, 300, 66, ["许可证有效且机器匹配？"], TEAL_L, TEAL)
    node(600, 628, 300, 66, ["进程已加载旧扩展？"], BLUE_L, BLUE)

    node(1010, 158, 400, 62, ["首次激活：拖入 .ampkg → 第 6 章"], GREEN_L, GREEN, f=font(21))
    node(1010, 262, 400, 62, ["全新安装：先装 wheel 再激活"], GREEN_L, GREEN, f=font(21))
    node(1010, 384, 400, 86, ["让发行方出同号 .ampkg", "（wheel 与主程序必须同版本）"], RED_L, RED, f=font(21))
    node(1010, 506, 400, 86, ["续期 / 换机 / 解除吊销", "→ 第 8 章"], AMBER_L, AMBER, f=font(21))
    node(1010, 628, 400, 62, ["提示重启：重启一次即自动激活"], GREEN_L, GREEN, f=font(21))
    node(1010, 726, 400, 62, ["热激活完成：无需重启"], GREEN_L, GREEN, f=font(21))

    arrow(d, 750, 220, 750, 260)
    arrow(d, 750, 328, 750, 382)
    arrow(d, 750, 450, 750, 504)
    arrow(d, 750, 572, 750, 626)
    arrow(d, 900, 195, 1006, 189)
    arrow(d, 900, 295, 1006, 293)
    arrow(d, 900, 417, 1006, 427)
    arrow(d, 900, 539, 1006, 549)
    arrow(d, 900, 659, 1006, 659)
    arrow(d, 900, 690, 1006, 757)

    for y in (236, 351, 473, 595):
        d.text((762, y), "是", font=font(20, True), fill=GREEN)
    for y in (172, 269, 390, 512):
        d.text((944, y), "否", font=font(20, True), fill=RED)
    d.text((938, 626), "是", font=font(20, True), fill=GREEN)
    d.text((930, 712), "否", font=font(20, True), fill=RED)

    rounded(d, (90, 168, 540, 360), fill=WHITE, outline=BORDER, radius=14)
    d.text((118, 188), "三条硬规则", font=font(24, True), fill=INK)
    for i, line in enumerate([
        "① 扩展包版本必须等于主程序版本；",
        "② 老客户旧 wheel 可能内置占位公钥，必须换发；",
        "③ 已加载旧扩展的进程不能热替换，重启一次即可。",
    ]):
        for j, seg in enumerate(wrap(d, line, font(20), 390)):
            d.text((118, 236 + i * 76 + j * 28), seg, font=font(20), fill=INK2)

    rounded(d, (90, 470, 540, 700), fill=BLUE_L, outline=(190, 210, 248), radius=14)
    d.text((118, 490), "升级顺序（同一窗口内）", font=font(24, True), fill=BLUE)
    for i, line in enumerate([
        "1. 先升级社区主程序（pip / 安装包）",
        "2. 再用同版本 .ampkg 升级扩展包并激活",
        "3. 最后验证 /api/health 的 edition 与 version",
        "4. 提示重启时重启一次，再跑激活验证清单",
    ]):
        d.text((118, 538 + i * 38), line, font=font(20), fill=INK)
    return save_png(img, "fig04_upgrade_decision_tree.png")


# ── 图 5：客户激活界面示意 ───────────────────────────────────────
def fig05_customer_ui_mockup() -> Path:
    img, d = canvas("客户激活界面示意：三种方式任选其一",
                    "设置 →「★ 升级专业版 / 企业版」；一键升级包方式最简（示意图，非实际截图）", 900)
    # 窗口
    rounded(d, (110, 160, 1390, 830), fill=WHITE, outline=(160, 176, 202), radius=16)
    d.rectangle([110, 160, 1390, 210], fill=(36, 46, 66))
    d.rounded_rectangle([110, 160, 1390, 232], radius=16, fill=(36, 46, 66))
    d.rectangle([110, 204, 1390, 232], fill=(36, 46, 66))
    for i, color in enumerate([(244, 106, 94), (245, 191, 79), (98, 197, 84)]):
        d.ellipse([134 + i * 26, 176, 148 + i * 26, 190], fill=color)
    d.text((222, 174), "AutoMind — 设置 / 升级专业版 · 企业版", font=font(21), fill=(235, 240, 250))
    # 侧栏
    d.rectangle([110, 232, 380, 830], fill=(244, 247, 252))
    d.line([(380, 232), (380, 830)], fill=GRID, width=2)
    d.text((136, 262), "设置", font=font(22, True), fill=INK)
    menu = ["通用设置", "模型配置", "API Keys", "机器人", "连接器", "关于"]
    for i, item in enumerate(menu):
        d.text((136, 308 + i * 46), item, font=font(19), fill=INK2)
    d.rounded_rectangle([126, 600, 366, 650], radius=10, fill=BLUE)
    d.text((146, 614), "★ 升级专业版 / 企业版", font=font(20, True), fill=WHITE)
    # 主区
    d.text((420, 258), "★ 升级专业版 / 企业版", font=font(30, True), fill=INK)
    pill(d, 420, 306, f"当前版本：社区版 v{VERSION}", fill=BG, fg=INK2, f=font(19), outline=BORDER)
    # 三个卡片
    cards = [
        ("① 一键升级（推荐）", BLUE, ["把 .ampkg 拖到这里", "自动装包 + 离线验签 + 激活", "无需 pip / 无需外网"]),
        ("② 粘贴许可证", GREEN, ["粘贴 AMP2-… 一整行", "或「从文件导入」", "当场校验并说明原因"]),
        ("③ 扩展包 + 许可证", PURPLE, ["上传 automind_pro-*.whl", "再粘贴许可证", "适合分两步交付"]),
    ]
    cw, gap, y = 296, 26, 356
    for i, (title, color, items) in enumerate(cards):
        x = 420 + i * (cw + gap)
        rounded(d, (x, y, x + cw, y + 320), fill=WHITE, outline=color, radius=14, width=3)
        rounded(d, (x, y, x + cw, y + 60), fill=color, outline=color, radius=14)
        d.rectangle([x, y + 44, x + cw, y + 60], fill=color)
        d.text((x + 18, y + 16), title, font=font(21, True), fill=WHITE)
        yy = y + 86
        for item in items:
            for seg in wrap(d, "· " + item, font(19), cw - 40):
                d.text((x + 18, yy), seg, font=font(19), fill=INK2)
                yy += 30
            yy += 12
        if i == 0:
            d.rounded_rectangle([x + 18, y + 236, x + cw - 18, y + 296], radius=10,
                                fill=BLUE_L, outline=BLUE, width=2)
            tw = text_w(d, "把 .ampkg 拖到这里", font(20, True))
            d.text((x + cw / 2 - tw / 2, y + 256), "把 .ampkg 拖到这里", font=font(20, True), fill=BLUE)
    # 底部提示
    rounded(d, (420, 696, 1330, 782), fill=GREEN_L, outline=(180, 226, 200), radius=12)
    d.text((446, 708), "激活成功：左上角徽标变为「专业版 / 企业版」；"
                       "GET /api/health 的 edition 可查当前档位。", font=font(19), fill=INK)
    d.text((446, 740), "许可证面板可看序列号 / 绑定机器 / 宽限 / 复检状态；"
                       "失败时会给出具体原因码而不是笼统「无效」。", font=font(19), fill=INK2)
    # 示意图水印
    d.text((1075, 790), "示意图（非实际截图）", font=font(20, True), fill=(196, 202, 214))
    return save_png(img, "fig05_customer_ui_mockup.png")


# ── 图 6：.ampkg 结构解剖 ────────────────────────────────────────
def fig06_ampkg_anatomy() -> Path:
    img, d = canvas(".ampkg 一键升级包结构解剖",
                    "本质是一个 zip：清单 + 许可证 + 扩展包 wheel（+ 可选 CRL）；客户端解压后离线验签并激活", 980)
    # 左侧 zip 文件
    rounded(d, (70, 250, 440, 620), fill=BLUE_L, outline=BLUE, radius=18, width=3)
    d.ellipse([96, 272, 156, 332], fill=BLUE)
    d.text((114, 288), "包", font=font(30, True), fill=WHITE)
    for i, line in enumerate(wrap(d, "AutoMind-PRO-acme-20271231.ampkg",
                                 font(21, True), 310)):
        d.text((96, 356 + i * 28), line, font=font(21, True), fill=INK)
    d.text((96, 466), "一个文件 = 装包 + 许可证 + 清单", font=font(18), fill=INK2)
    d.text((96, 500), "客户只需拖入升级面板", font=font(18), fill=INK2)
    d.text((96, 534), "可附带 license_crl.json", font=font(17), fill=MUTED)
    d.text((96, 572), "zip 压缩，通常 < 200 KB", font=font(17), fill=MUTED)
    # 右侧子项
    kids = [
        ("automind-upgrade.json", "清单：format / tier / customer / expiry / seats / "
                                  "wheel / package_version / core_version / key_id / crl", PURPLE),
        ("license.txt", "一行许可证（AMP2-…）：签名覆盖档位、到期、席位、"
                        "序列号、机器指纹、生效日", GREEN),
        ("automind_pro-<ver>-py3-none-any.whl", "商业扩展包：版本必须等于主程序；"
                                                "内置验签公钥（客户侧验签用）", BLUE),
        ("license_crl.json（可选）", "吊销清单：随包下发的已吊销序列号，激活时一并落盘", RED),
    ]
    x0, y0, gap = 590, 225, 128
    for i, (name, desc, color) in enumerate(kids):
        y = y0 + i * gap
        rounded(d, (x0, y, 1430, y + 112), fill=WHITE, outline=BORDER, radius=14)
        d.rectangle([x0, y, x0 + 10, y + 112], fill=color)
        d.text((x0 + 28, y + 12), name, font=font(21, True), fill=INK)
        yy = y + 48
        for seg in wrap(d, desc, font(17), 800):
            d.text((x0 + 28, yy), seg, font=font(17), fill=INK2)
            yy += 24
        # 连线
        d.line([(440, 435), (490, 435), (490, y + 56), (x0 - 2, y + 56)], fill=MUTED, width=2)
    rounded(d, (70, 760, 1430, 890), fill=AMBER_L, outline=(238, 214, 160), radius=14)
    d.text((96, 780), "激活流程（客户侧，全程离线）", font=font(23, True), fill=AMBER)
    for i, seg in enumerate(wrap(d, "解压 → 校验清单 → 用 wheel 内置公钥验签许可证 → "
                                    "写 ~/.automind/license → 安装 wheel → 就地激活"
                                    "（必要时重启一次）", font(20), 1290)):
        d.text((96, 822 + i * 28), seg, font=font(20), fill=INK)
    return save_png(img, "fig06_ampkg_anatomy.png")


# ── 图 7：批量发证与台账流程 ──────────────────────────────────────
def fig07_batch_issuance() -> Path:
    img, d = canvas("批量发证与台账流程：一份 CSV 发出 N 个客户",
                    "issue-batch 逐行签发；失败行不中断并汇总原因；台账与续费提醒同日登记", 1000)
    # CSV
    rounded(d, (50, 170, 560, 520), fill=WHITE, outline=(198, 212, 240), radius=16)
    d.text((74, 188), "customers.csv（示例）", font=font(23, True), fill=INK)
    rows = [("customer", "tier", "days", "serial"),
            ("acme", "PRO", "365", "ACME20260001"),
            ("bigcorp", "ENT", "永久", "BIGCORP0001"),
            ("trial-co", "PRO", "30", "TRIAL202601"),
            ("edu-lab", "ENT", "365", "EDU20260001")]
    y = 232
    for i, row in enumerate(rows):
        if i == 0:
            d.rectangle([70, y - 6, 540, y + 30], fill=BLUE)
            color, fnt = WHITE, font(18, True)
        else:
            if i % 2 == 0:
                d.rectangle([70, y - 6, 540, y + 30], fill=BG)
            color, fnt = INK, font(18)
        d.text((82, y), row[0], font=fnt, fill=color)
        d.text((232, y), row[1], font=fnt, fill=color)
        d.text((312, y), row[2], font=fnt, fill=color)
        d.text((412, y), row[3], font=fnt, fill=color)
        y += 50
    d.text((70, 470), "完整列：…,expiry,seats,machine,serial,start,notes,days", font=font(15), fill=MUTED)
    # 中：命令
    rounded(d, (600, 170, 960, 520), fill=PURPLE_L, outline=PURPLE, radius=16, width=2)
    d.text((624, 188), "一条命令", font=font(23, True), fill=PURPLE)
    cmd_lines = ["python pro/tools/", "  issue_license.py", "  issue-batch",
                 "  --csv customers.csv", "  --out dist/licenses", "  --bundles",
                 "  --wheel pro/dist/", f"    automind_pro-{VERSION}-", "    py3-none-any.whl"]
    for i, line in enumerate(cmd_lines):
        d.text((624, 238 + i * 30), line, font=font(16), fill=INK)
    # 右：产物
    rounded(d, (1000, 170, 1450, 700), fill=WHITE, outline=(198, 212, 240), radius=16)
    d.text((1024, 188), "产物", font=font(23, True), fill=INK)
    outs = [
        ("<out>/*.license.txt", "每客户一份许可证"),
        ("<out>/*.ampkg", "加 --bundles 时同时出包"),
        ("<out>/license_ledger.csv", "12 列台账（追加写）"),
        ("stdout JSON 摘要", "ok / failed 计数与逐行失败原因"),
        ("dist/crl/license_crl.json", "吊销清单（--crl 可指定）"),
    ]
    y = 240
    for name, desc in outs:
        d.ellipse([1028, y + 6, 1040, y + 18], fill=GREEN)
        d.text((1054, y), name, font=font(18, True), fill=INK)
        yy = y + 26
        for seg in wrap(d, desc, font(16), 360):
            d.text((1054, yy), seg, font=font(16), fill=INK2)
            yy += 21
        y = yy + 18
    # 下方：三块衔接
    steps = [("① CSV 准备", "客户 / 档位 / 到期 / 席位 / 机器指纹", BLUE),
             ("② 逐行签发", "成功行写证与台账；失败行不中断", PURPLE),
             ("③ 分发与续费", "按台账到期日 T-90/60/30 提醒续费", GREEN)]
    x = 50
    for title, desc, color in steps:
        rounded(d, (x, 740, x + 440, 860), fill=color, outline=color, radius=14)
        d.text((x + 22, 760), title, font=font(23, True), fill=WHITE)
        for i, seg in enumerate(wrap(d, desc, font(19), 396)):
            d.text((x + 22, 802 + i * 27), seg, font=font(19), fill=WHITE)
        x += 480
    for i in range(2):
        arrow(d, 500 + i * 480, 800, 520 + i * 480, 800, INK2, 3, 10)
    d.text((50, 900), "注意：批量签发同样需要 doctor 全绿；失败行不中断，JSON 摘要逐行给出原因；"
                      "加 --strict 时任一行失败即非零退出。", font=font(19), fill=INK2)
    return save_png(img, "fig07_batch_issuance.png")


# ── 图 8：机器绑定 / 换机流程 ────────────────────────────────────
def fig08_machine_binding() -> Path:
    img, d = canvas("机器绑定与换机：绑定证只认一台机器",
                    "客户在许可证面板复制「本机机器指纹」发给发行方；换机需要重新签发（可选吊销旧证）", 1040)
    # 第一行：首次绑定（4 列）
    d.text((60, 150), "首次绑定（购买绑定版）", font=font(26, True), fill=BLUE)
    col_w, col_gap = 320, 30
    boxes = [
        ("客户机：许可证面板", ["「本机信息」卡片", "显示 16 位指纹 + 复制按钮", "发给发行方"], GREEN),
        ("发行方：签发绑定证", ["bundle … --machine <16hex>", "或 --machine auto（仅测试）", "--serial 便于台账/吊销"], PURPLE),
        ("客户机：拖入激活", ["拖入 .ampkg 或粘贴许可证", "校验机器指纹匹配", "激活成功"], BLUE),
        ("台账登记", ["序列号 / 指纹", "到期日 / 换机史"], AMBER),
    ]
    x = 60
    for title, items, color in boxes:
        rounded(d, (x, 200, x + col_w, 380), fill=WHITE, outline=color, radius=14, width=2)
        rounded(d, (x, 200, x + col_w, 248), fill=color, outline=color, radius=14)
        d.rectangle([x, 232, x + col_w, 248], fill=color)
        d.text((x + 16, 212), title, font=font(18, True), fill=WHITE)
        yy = 266
        for item in items:
            for seg in wrap(d, "· " + item, font(16), col_w - 30):
                d.text((x + 16, yy), seg, font=font(16), fill=INK2)
                yy += 24
            yy += 8
        x += col_w + col_gap
    for i in range(3):
        arrow(d, 380 + i * (col_w + col_gap), 290, 416 + i * (col_w + col_gap), 290, INK2, 3, 11)
    # 第二行：换机
    d.text((60, 440), "换机流程（旧机 → 新机）", font=font(26, True), fill=AMBER)
    steps = [
        ("① 新机查看指纹", "新机升级面板「本机信息」复制 machine_id"),
        ("② 发行方重签", "bundle … --machine <新指纹> --serial <新序列号>"),
        ("③ 旧证处置", "旧机报废/转卖时：revoke <旧 serial> --reason 换机"),
        ("④ 交付与回访", "发新 .ampkg；客户拖入激活；台账记录换机日期"),
    ]
    x = 60
    for title, desc in steps:
        rounded(d, (x, 490, x + 320, 650), fill=AMBER_L, outline=(238, 214, 160), radius=13)
        d.text((x + 18, 508), title, font=font(20, True), fill=AMBER)
        for j, seg in enumerate(wrap(d, desc, font(17), 282)):
            d.text((x + 18, 548 + j * 26), seg, font=font(17), fill=INK)
        x += 350
    rounded(d, (60, 690, 1440, 860), fill=RED_L, outline=(240, 196, 196), radius=14)
    d.text((86, 708), "▲ 常见失败", font=font(22, True), fill=RED)
    for i, line in enumerate([
        "· 换机后直接复用旧证 → machine_mismatch；需要发行方重签（换机不是「复制许可证」）。",
        "· 绑定证与未绑定证混发 → 台账必须记录每张证是否绑定、绑定到哪台机器。",
        "· 旧证未吊销就继续给新机使用 → 两台机器都能用，席位/合规风险；重要客户建议吊销旧证。",
    ]):
        d.text((86, 746 + i * 30), line, font=font(18), fill=INK)
    d.text((60, 900), "指纹 = hostname + MAC/UUID + 平台 + 安装盐（~/.automind/machine_salt）的 sha256 前 16 位；",
           font=font(18), fill=INK2)
    d.text((60, 928), "重装系统 / 清空用户目录会变（换机前先报备）；容器等易变场景可用 AUTOMIND_MACHINE_ID 固定。",
           font=font(18), fill=INK2)
    return save_png(img, "fig08_machine_binding.png")


# ── 图 9：v1.11.16 新功能一览 ────────────────────────────────────
def fig09_v1116_features() -> Path:
    img, d = canvas("v1.11.16 新功能一览：任务执行连续性",
                    "预算自动续窗 / 四模式断点续跑 / 工作流自动化强化 / 治理参数生效 / 路由成本页修复", 820)
    cards = [
        ("续", "Token 预算连续性", ["预算耗尽自动续窗", "协同 / 循环 / 子智能体", "携带进度续跑不重做"],
         "社区版可用", BLUE),
        ("断", "断点续跑四模式", ["计划 / ReAct / 协同 / 循环", "重启后可发现并接续", "检查点 id 唯一化"],
         "社区 + 专业版", PURPLE),
        ("流", "工作流自动化强化", ["if / unless 步骤条件", "fallback 失败降级 · 整轮超时", "Token 统计 · 触发器补偿/去重"],
         "社区 + 专业版", TEAL),
        ("治", "执行治理真正生效", ["26 项参数逐项生效", "界面标注生效时机", "诚实标注不追溯范围"],
         "社区版可用", AMBER),
        ("修", "路由与成本页修复", ["toFixed 崩溃修复", "旧版 API 数据兼容", "页面级错误边界"],
         "社区版可用", GREEN),
    ]
    cw, gap = 270, 24
    x = 50
    for icon, title, items, tag, color in cards:
        rounded(d, (x, 180, x + cw, 640), fill=WHITE, outline=BORDER, radius=16)
        d.ellipse([x + cw / 2 - 38, 210, x + cw / 2 + 38, 286], fill=color)
        tw = text_w(d, icon, font(30))
        d.text((x + cw / 2 - tw / 2, 228), icon, font=font(30), fill=WHITE)
        for i, seg in enumerate(wrap(d, title, font(22, True), cw - 30)):
            tw = text_w(d, seg, font(22, True))
            d.text((x + cw / 2 - tw / 2, 306 + i * 30), seg, font=font(22, True), fill=INK)
        yy = 384
        for item in items:
            d.ellipse([x + 22, yy + 8, x + 32, yy + 18], fill=color)
            for seg in wrap(d, item, font(17), cw - 60):
                d.text((x + 42, yy), seg, font=font(17), fill=INK2)
                yy += 25
            yy += 14
        pill(d, x + 20, 572, tag, fill=BG, fg=color, f=font(16, True), outline=BORDER)
        x += cw + gap
    d.text((50, 700), "说明：Token 预算续窗与四模式断点续跑面向所有版本（协同 / 循环需专业版）；"
                      "工作流触发器补偿 / 重放保护属专业版，", font=font(18), fill=INK2)
    d.text((50, 728), "其余工作流字段与执行治理为社区版；商业发证 / 激活 / CRL 流程与 v1.11.15 完全一致"
                      "（CLI 未变化）。", font=font(18), fill=INK2)
    return save_png(img, "fig09_v1116_features.png")


# ── 图 10：错误原因码处置流程 ────────────────────────────────────
def fig10_error_triage() -> Path:
    img, d = canvas("激活失败原因码速查：先看原因码，再做处置",
                    "客户端在许可证面板 / /api/license 给出具体原因码与中文说明，不再笼统「许可证无效」", 1160)
    cols = ["客户看到的现象", "原因码（reason）", "谁处理 · 第一步动作"]
    rows = [
        ("一直是社区版，无报错", "empty / public_key_not_provisioned",
         "客户：拖入 .ampkg 或放置许可证；发行方：pubkey + doctor 复核"),
        ("「许可证内容格式不合法」", "payload_invalid",
         "重新复制完整一行（AMP2- 开头）；仍失败用 verify 复核"),
        ("「签名校验未通过」", "signature_invalid",
         "许可证被改过 / 跨钥签发：verify 复核、换正确钥匙、重发"),
        ("「需要升级扩展包」（轮换过渡）", "key_id_unknown",
         f"用含新公钥的 v{VERSION} 扩展包重出 .ampkg"),
        ("「许可证已过期」", "expired", "发行方签发续期证；客户拖入新 .ampkg"),
        ("「已过期且超出宽限期」", "grace_expired", "续费后拖入新包；宽限只对到期生效"),
        ("仍可用，但提示宽限剩余 N 天", "grace", "正常状态：尽快完成续费（宽限默认 0 天）"),
        ("「尚未到生效日」", "not_yet_active", "核对 --start 生效日与客户本地日期 / 时区"),
        ("「绑定机器与本机不符」", "machine_mismatch",
         "换机：客户提供新指纹 → 发行方 --machine 重签（第 8 章）"),
        ("「许可证已被吊销」", "revoked", "查 CRL 原因；恢复需重签新序列号"),
        ("「检测到系统时钟回拨」", "clock_rollback", "校时（NTP）→ 等复检或重启；不要改时间续命"),
        ("旧 AMP- 格式许可证", "legacy_hmac_disabled", "旧 HMAC 证已停用：换发 AMP2 新证"),
    ]
    x0, y0, w = 50, 168, 1400
    col_w = [400, 420, 580]
    d.rounded_rectangle([x0, y0, x0 + w, y0 + 52], radius=10, fill=INK)
    x = x0
    for i, name in enumerate(cols):
        d.text((x + 18, y0 + 15), name, font=font(21, True), fill=WHITE)
        x += col_w[i]
    y = y0 + 60
    for idx, (sym, code, action) in enumerate(rows):
        h = 70
        fill = WHITE if idx % 2 == 0 else BG
        d.rectangle([x0, y, x0 + w, y + h], fill=fill, outline=GRID)
        d.text((x0 + 18, y + 14), sym, font=font(18), fill=INK)
        code_lines = code.split("\n")
        chip_h = 32 + (len(code_lines) - 1) * 24
        d.rounded_rectangle([x0 + 410, y + 10, x0 + 870, y + 10 + chip_h],
                            radius=8, fill=RED_L)
        for j, seg in enumerate(code_lines):
            d.text((x0 + 426, y + 15 + j * 24), seg, font=font(16), fill=RED)
        for j, seg in enumerate(wrap(d, action, font(16), col_w[2] - 40)):
            d.text((x0 + 892, y + 12 + j * 22), seg, font=font(16), fill=INK2)
        y += h
    d.text((50, 1082), "排查顺序：客户现象 → 原因码 → 发行侧 verify/doctor → 客户侧文件与优先级 → "
                       f"再决定重签还是重装。原因码枚举以 v{VERSION} 最终代码为准。", font=font(19), fill=INK2)
    return save_png(img, "fig10_error_triage.png")


# ── 图 11：密钥轮换与泄露应急 ────────────────────────────────────
def fig11_key_rotation() -> Path:
    img, d = canvas("密钥轮换与泄露应急：两条完全不同的路径",
                    "计划内换钥要平滑（保旧证过渡）；私钥泄露要果断（全部作废、全部重发）", 860)
    # 计划内
    rounded(d, (50, 160, 730, 700), fill=GREEN_L, outline=(180, 226, 200), radius=18)
    d.text((78, 180), "换 · 计划内换钥（平滑过渡）", font=font(25, True), fill=GREEN)
    steps = [
        ("1. keygen --rotate", "新钥成主钥，旧主钥移入 LEGACY 列表（--retire-after 设下线日）"),
        ("2. 重建扩展包 wheel", "wheel 内置新主钥 + 旧钥列表；旧证按 KID 继续可验"),
        ("3. 过渡期换证", "新证用新钥签；存量客户在 retire_after 前换发"),
        ("4. doctor 检查", "key_rotation_state 显示过渡期与预计下线日"),
        ("5. 到期收口", "清空 LEGACY 列表 / retire_after 到期后 doctor 强制失败 → 删旧钥重建"),
    ]
    y = 240
    for title, desc in steps:
        rounded(d, (78, y, 702, y + 76), fill=WHITE, outline=(180, 226, 200), radius=12)
        d.text((96, y + 8), title, font=font(20, True), fill=GREEN)
        for j, seg in enumerate(wrap(d, desc, font(17), 580)):
            d.text((96, y + 38 + j * 22), seg, font=font(17), fill=INK2)
        y += 88
    # 泄露应急
    rounded(d, (770, 160, 1450, 700), fill=RED_L, outline=(240, 196, 196), radius=18)
    d.text((798, 180), "警 · 私钥泄露应急（全部作废）", font=font(25, True), fill=RED)
    steps = [
        ("1. 立即隔离发行机", "断开网络、保留现场；通知安全负责人与商业负责人"),
        ("2. 紧急换钥（不保留旧钥）", "keygen --force --write-public-key：直接替换主钥、清空旧钥列表"),
        ("3. 重建 wheel + 重出包", "wheel 换新公钥；所有客户重新出 .ampkg"),
        ("4. CRL 吊销旧序列号", "台账中旧序列号全部 revoke 并随包 / 单独下发"),
        ("5. 客户通知与留痕", "说明影响面、给新包、更新台账与安全事件记录"),
    ]
    y = 240
    for title, desc in steps:
        rounded(d, (798, y, 1422, y + 76), fill=WHITE, outline=(240, 196, 196), radius=12)
        d.text((816, y + 8), title, font=font(20, True), fill=RED)
        for j, seg in enumerate(wrap(d, desc, font(17), 580)):
            d.text((816, y + 38 + j * 22), seg, font=font(17), fill=INK2)
        y += 88
    d.text((50, 740), "底线：私钥只存在于离线介质/密码库；轮换与泄露处置都必须留痕（时间、操作人、影响范围、"
                      "下发清单）。retire_after 到期后 doctor 会强制失败，必须完成删旧钥 + 重建 wheel。",
           font=font(19), fill=INK2)
    return save_png(img, "fig11_key_rotation.png")


CHARTS = {
    "fig01_activation_overview": fig01_activation_overview,
    "fig02_roles_materials": fig02_roles_materials,
    "fig03_license_lifecycle": fig03_license_lifecycle,
    "fig04_upgrade_decision_tree": fig04_upgrade_decision_tree,
    "fig05_customer_ui_mockup": fig05_customer_ui_mockup,
    "fig06_ampkg_anatomy": fig06_ampkg_anatomy,
    "fig07_batch_issuance": fig07_batch_issuance,
    "fig08_machine_binding": fig08_machine_binding,
    "fig09_v1116_features": fig09_v1116_features,
    "fig10_error_triage": fig10_error_triage,
    "fig11_key_rotation": fig11_key_rotation,
}


def build_charts() -> list[Path]:
    ASSETS.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for name, fn in CHARTS.items():
        path = fn()
        out.append(path)
        print(f"[chart] {path.name} ({path.stat().st_size // 1024} KB)", flush=True)
    return out


# ══════════════════════════════════════════════════════════════════
#  Word 生成（python-docx）
# ══════════════════════════════════════════════════════════════════
FONT_CN = "微软雅黑"
FONT_CODE = "Consolas"
BODY_PT = 10.5
PAGE_W_CM = 16.0          # 可用正文宽度（A4 21cm - 左右边距 2.5*2）
COLOR_INK = (0x1F, 0x2A, 0x44)
COLOR_BLUE = (0x2F, 0x6F, 0xEB)
COLOR_GRAY = (0x5E, 0x6C, 0x86)
COLOR_MUTED = (0x84, 0x91, 0xA8)
COLOR_RED = (0xC0, 0x36, 0x36)
COLOR_GREEN = (0x16, 0x8F, 0x5C)
COLOR_AMBER = (0x9A, 0x6A, 0x08)


def _rgb(t: tuple[int, int, int]):
    from docx.shared import RGBColor

    return RGBColor(*t)


def _set_run_font(run, *, name: str = FONT_CN, size: float = BODY_PT,
                  bold: bool = False, color: tuple[int, int, int] | None = None):
    from docx.oxml.ns import qn
    from docx.shared import Pt

    run.font.name = name
    run.font.size = Pt(size)
    run.font.bold = bold
    if color is not None:
        run.font.color.rgb = _rgb(color)
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.insert(0, rfonts)
    rfonts.set(qn("w:ascii"), name)
    rfonts.set(qn("w:hAnsi"), name)
    rfonts.set(qn("w:eastAsia"), name)


def _set_style_font(style, *, name: str = FONT_CN, size: float = BODY_PT,
                    bold: bool = False, color: tuple[int, int, int] | None = None):
    from docx.oxml.ns import qn
    from docx.shared import Pt

    style.font.name = name
    style.font.size = Pt(size)
    style.font.bold = bold
    if color is not None:
        style.font.color.rgb = _rgb(color)
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.insert(0, rfonts)
    rfonts.set(qn("w:ascii"), name)
    rfonts.set(qn("w:hAnsi"), name)
    rfonts.set(qn("w:eastAsia"), name)


def _shade(cell, hex_color: str):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tc_pr.append(shd)


def _cell_margins(cell, top: int = 60, bottom: int = 60, left: int = 110, right: int = 110):
    """单元格内边距（单位 twip，1/20 pt）。"""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    tc_pr = cell._tc.get_or_add_tcPr()
    mar = OxmlElement("w:tcMar")
    for tag, val in (("top", top), ("bottom", bottom), ("start", left), ("end", right)):
        el = OxmlElement(f"w:{tag}")
        el.set(qn("w:w"), str(val))
        el.set(qn("w:type"), "dxa")
        mar.append(el)
    tc_pr.append(mar)


def _table_borders(table, color: str = "D1DAEB", size: int = 4):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    tbl_pr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), str(size))
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), color)
        borders.append(el)
    tbl_pr.append(borders)


def _cell_border(cell, edge: str, color: str, size: int = 24):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    tc_pr = cell._tc.get_or_add_tcPr()
    borders = tc_pr.find(qn("w:tcBorders"))
    if borders is None:
        borders = OxmlElement("w:tcBorders")
        tc_pr.append(borders)
    el = OxmlElement(f"w:{edge}")
    el.set(qn("w:val"), "single")
    el.set(qn("w:sz"), str(size))
    el.set(qn("w:space"), "0")
    el.set(qn("w:color"), color)
    borders.append(el)


def _repeat_header(row):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    tr_pr = row._tr.get_or_add_trPr()
    el = OxmlElement("w:tblHeader")
    el.set(qn("w:val"), "true")
    tr_pr.append(el)


def _cant_split(row):
    """单行区块（代码块 / 提示框）不允许跨页拆开。"""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    tr_pr = row._tr.get_or_add_trPr()
    el = OxmlElement("w:cantSplit")
    el.set(qn("w:val"), "true")
    tr_pr.append(el)


def _keep_with_next(paragraph, val: bool = True):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    p_pr = paragraph._p.get_or_add_pPr()
    el = OxmlElement("w:keepNext")
    el.set(qn("w:val"), "true" if val else "false")
    p_pr.append(el)


def _para_bottom_border(paragraph, color: str = "2F6FEB", size: int = 8):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    p_pr = paragraph._p.get_or_add_pPr()
    p_bdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), str(size))
    bottom.set(qn("w:space"), "4")
    bottom.set(qn("w:color"), color)
    p_bdr.append(bottom)
    p_pr.append(p_bdr)


def _add_page_field(paragraph):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), "PAGE")
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = "1"
    r.append(t)
    fld.append(r)
    paragraph._p.append(fld)


def _add_pages_field(paragraph):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), "NUMPAGES")
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = "1"
    r.append(t)
    fld.append(r)
    paragraph._p.append(fld)


class ManualBuilder:
    """封装文档样式与常用区块（标题 / 段落 / 表格 / 代码 / 提示框 / 图片）。"""

    def __init__(self):
        from docx import Document

        self.doc = Document()
        self.fig_no = 0
        self.tbl_no = 0
        self._setup_page()
        self._setup_styles()
        self._setup_header_footer()

    # ── 基础设置 ──
    def _setup_page(self):
        from docx.shared import Cm

        sec = self.doc.sections[0]
        sec.page_width = Cm(21.0)
        sec.page_height = Cm(29.7)
        sec.top_margin = Cm(2.3)
        sec.bottom_margin = Cm(2.1)
        sec.left_margin = Cm(2.5)
        sec.right_margin = Cm(2.5)
        sec.header_distance = Cm(1.1)
        sec.footer_distance = Cm(1.1)
        sec.different_first_page_header_footer = True

    def _setup_styles(self):
        from docx.enum.text import WD_LINE_SPACING
        from docx.shared import Pt

        st = self.doc.styles
        normal = st["Normal"]
        _set_style_font(normal, size=BODY_PT, color=COLOR_INK)
        pf = normal.paragraph_format
        pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
        pf.line_spacing = 1.3
        pf.space_after = Pt(5)
        pf.space_before = Pt(0)

        specs = {
            "Heading 1": (18.0, True, COLOR_INK, 20, 10),
            "Heading 2": (13.5, True, COLOR_BLUE, 14, 6),
            "Heading 3": (11.5, True, COLOR_INK, 10, 4),
            "Heading 4": (10.5, True, COLOR_GRAY, 8, 3),
        }
        for name, (size, bold, color, before, after) in specs.items():
            style = st[name]
            _set_style_font(style, size=size, bold=bold, color=color)
            style.paragraph_format.space_before = Pt(before)
            style.paragraph_format.space_after = Pt(after)
            style.paragraph_format.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
            style.paragraph_format.line_spacing = 1.15
            style.paragraph_format.keep_with_next = True

        cap = st["Caption"]
        _set_style_font(cap, size=9, color=COLOR_GRAY)
        cap.paragraph_format.space_before = Pt(2)
        cap.paragraph_format.space_after = Pt(10)
        cap.paragraph_format.alignment = 1  # center

    def _setup_header_footer(self):
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        sec = self.doc.sections[0]
        # 页眉：右侧标题 + 下边框
        hp = sec.header.paragraphs[0]
        hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        run = hp.add_run(f"AutoMind 商业运营操作手册 v{VERSION}")
        _set_run_font(run, size=8.5, color=COLOR_MUTED)
        _para_bottom_border(hp, color="D1DAEB", size=6)
        # 页脚：居中页码域
        fp = sec.footer.paragraphs[0]
        fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = fp.add_run("第 ")
        _set_run_font(run, size=9, color=COLOR_GRAY)
        _add_page_field(fp)
        run = fp.add_run(" 页 / 共 ")
        _set_run_font(run, size=9, color=COLOR_GRAY)
        _add_pages_field(fp)
        run = fp.add_run(" 页")
        _set_run_font(run, size=9, color=COLOR_GRAY)
        for r in fp.runs:
            _set_run_font(r, size=9, color=COLOR_GRAY)

    # ── 内容区块 ──
    def blank(self, n: int = 1, size: float = BODY_PT):
        from docx.shared import Pt

        for _ in range(n):
            p = self.doc.add_paragraph()
            p.paragraph_format.space_after = Pt(0)
            run = p.add_run("")
            _set_run_font(run, size=size)

    def page_break(self):
        """兼容旧调用：分页由下一个 h1 的 page_break_before 完成，避免空白页。"""
        return

    def h1(self, text: str, *, new_page: bool = True):
        from docx.shared import Pt

        p = self.doc.add_paragraph(style="Heading 1")
        if new_page:
            p.paragraph_format.page_break_before = True
        run = p.add_run(text)
        _set_run_font(run, size=18, bold=True, color=COLOR_INK)
        _para_bottom_border(p, color="2F6FEB", size=10)
        p.paragraph_format.space_before = Pt(6)
        p.paragraph_format.space_after = Pt(12)
        return p

    def h2(self, text: str):
        p = self.doc.add_paragraph(style="Heading 2")
        run = p.add_run(text)
        _set_run_font(run, size=13.5, bold=True, color=COLOR_BLUE)
        return p

    def h3(self, text: str):
        p = self.doc.add_paragraph(style="Heading 3")
        run = p.add_run(text)
        _set_run_font(run, size=11.5, bold=True, color=COLOR_INK)
        return p

    def p(self, text: str = "", *, bold_prefix: str = "", color=None, size=BODY_PT,
          italic: bool = False, align=None, space_after: float = 5):
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Pt

        para = self.doc.add_paragraph()
        if align == "center":
            para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        elif align == "right":
            para.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        else:
            para.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        para.paragraph_format.space_after = Pt(space_after)
        if bold_prefix:
            r = para.add_run(bold_prefix)
            _set_run_font(r, size=size, bold=True, color=color or COLOR_INK)
        r = para.add_run(text)
        _set_run_font(r, size=size, color=color or COLOR_INK)
        r.font.italic = italic
        return para

    def bullets(self, items: list[str], *, size: float = BODY_PT, marker: str = "•"):
        from docx.shared import Cm, Pt

        for item in items:
            para = self.doc.add_paragraph()
            para.paragraph_format.left_indent = Cm(0.55)
            para.paragraph_format.first_line_indent = Cm(-0.42)
            para.paragraph_format.space_after = Pt(2.5)
            para.paragraph_format.line_spacing = 1.28
            r = para.add_run(f"{marker} ")
            _set_run_font(r, size=size, color=COLOR_BLUE)
            r = para.add_run(item)
            _set_run_font(r, size=size, color=COLOR_INK)

    def steps(self, items: list[str], *, size: float = BODY_PT):
        from docx.shared import Cm, Pt

        for i, item in enumerate(items, 1):
            para = self.doc.add_paragraph()
            para.paragraph_format.left_indent = Cm(0.7)
            para.paragraph_format.first_line_indent = Cm(-0.7)
            para.paragraph_format.space_after = Pt(3)
            para.paragraph_format.line_spacing = 1.28
            r = para.add_run(f"{i}. ")
            _set_run_font(r, size=size, bold=True, color=COLOR_BLUE)
            r = para.add_run(item)
            _set_run_font(r, size=size, color=COLOR_INK)

    def code(self, text: str, *, caption: str = ""):
        """代码 / 命令块：单格浅灰底纹表，Consolas。"""
        from docx.enum.table import WD_TABLE_ALIGNMENT
        from docx.shared import Cm, Pt

        table = self.doc.add_table(rows=1, cols=1)
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        table.autofit = False
        _cant_split(table.rows[0])
        cell = table.rows[0].cells[0]
        cell.width = Cm(PAGE_W_CM)
        _shade(cell, "F4F6F9")
        _cell_margins(cell, top=90, bottom=90, left=140, right=120)
        _table_borders(table, color="DDE4F0", size=4)
        _cell_border(cell, "left", "7B9FFF", 18)
        lines = text.strip("\n").split("\n")
        para = cell.paragraphs[0]
        para.paragraph_format.space_after = Pt(0)
        para.paragraph_format.line_spacing = 1.0
        for i, line in enumerate(lines):
            if i == 0:
                run = para.add_run(line if line else " ")
            else:
                run = para.add_run()
                run.add_break()
                run.add_text(line if line else " ")
            _set_run_font(run, name=FONT_CODE, size=8.5, color=(0x24, 0x2B, 0x3A))
        if caption:
            self.caption(caption, kind="代码")
        self.doc.add_paragraph().paragraph_format.space_after = Pt(2)

    def caption(self, text: str, *, kind: str = "图"):
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Pt

        para = self.doc.add_paragraph(style="Caption")
        para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        para.paragraph_format.space_after = Pt(10)
        prefix = f"{kind} {self.fig_no}：" if kind == "图" else (f"{text}" if kind == "代码" else f"{kind}：")
        run = para.add_run(prefix if kind != "代码" else text)
        _set_run_font(run, size=9, color=COLOR_GRAY)
        _keep_with_next(para, False)

    def figure(self, filename: str, caption: str, *, note: str = ""):
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Cm

        path = ASSETS / filename
        if not path.is_file():
            raise SystemExit(f"缺少配图：{path}（先运行 --charts）")
        self.fig_no += 1
        para = self.doc.add_paragraph()
        para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        para.paragraph_format.keep_with_next = True
        run = para.add_run()
        run.add_picture(str(path), width=Cm(15.6))
        cap = self.doc.add_paragraph(style="Caption")
        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
        text = f"图 {self.fig_no}：{caption}"
        if note:
            text += f"（{note}）"
        r = cap.add_run(text)
        _set_run_font(r, size=9, color=COLOR_GRAY)
        _keep_with_next(cap, False)

    def callout(self, kind: str, title: str, text: str):
        """提示框：单格底纹表 + 左侧粗边。kind: info / tip / warn / danger / ok"""
        from docx.enum.table import WD_TABLE_ALIGNMENT
        from docx.shared import Cm, Pt

        styles = {
            "info": ("E9F0FE", "2F6FEB", COLOR_BLUE, "提示"),
            "tip": ("E9F0FE", "2F6FEB", COLOR_BLUE, "经验"),
            "warn": ("FFF6E1", "BA7008", COLOR_AMBER, "注意"),
            "danger": ("FCEBEB", "C43636", COLOR_RED, "危险"),
            "ok": ("E4F6ED", "168F5C", COLOR_GREEN, "要点"),
        }
        bg, border, fg, default_label = styles.get(kind, styles["info"])
        table = self.doc.add_table(rows=1, cols=1)
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        table.autofit = False
        _cant_split(table.rows[0])
        cell = table.rows[0].cells[0]
        cell.width = Cm(PAGE_W_CM)
        _shade(cell, bg)
        _table_borders(table, color=border, size=4)
        _cell_border(cell, "left", border, 24)
        _cell_margins(cell, top=100, bottom=100, left=160, right=140)
        para = cell.paragraphs[0]
        para.paragraph_format.space_after = Pt(0)
        para.paragraph_format.line_spacing = 1.25
        run = para.add_run(f"{title or default_label}　")
        _set_run_font(run, size=10, bold=True, color=fg)
        run = para.add_run(text)
        _set_run_font(run, size=9.8, color=COLOR_INK)
        self.doc.add_paragraph().paragraph_format.space_after = Pt(2)

    def table(self, headers: list[str], rows: list[list[str]], *,
              widths: list[float] | None = None, size: float = 9.0,
              header_fill: str = "1F2A44", zebra: bool = True, caption: str = ""):
        from docx.enum.table import WD_TABLE_ALIGNMENT
        from docx.shared import Cm, Pt

        n_cols = len(headers)
        if caption:
            self.tbl_no += 1
            cap = self.doc.add_paragraph(style="Caption")
            r = cap.add_run(f"表 {self.tbl_no}：{caption}")
            _set_run_font(r, size=9, color=COLOR_GRAY)
            _keep_with_next(cap, True)
            from docx.enum.text import WD_ALIGN_PARAGRAPH

            cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
        table = self.doc.add_table(rows=1, cols=n_cols)
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        table.autofit = False
        _table_borders(table, color="D1DAEB", size=4)
        widths = widths or [PAGE_W_CM / n_cols] * n_cols
        # 表头
        hdr = table.rows[0]
        _repeat_header(hdr)
        _cant_split(hdr)
        for i, text in enumerate(headers):
            cell = hdr.cells[i]
            cell.width = Cm(widths[i])
            _shade(cell, header_fill)
            _cell_margins(cell, top=70, bottom=70)
            para = cell.paragraphs[0]
            para.paragraph_format.space_after = Pt(0)
            para.paragraph_format.line_spacing = 1.0
            run = para.add_run(text)
            _set_run_font(run, size=size, bold=True, color=(0xFF, 0xFF, 0xFF))
        # 数据行
        for ridx, row in enumerate(rows):
            tr = table.add_row()
            _cant_split(tr)
            for i, text in enumerate(row):
                cell = tr.cells[i]
                cell.width = Cm(widths[i])
                if zebra and ridx % 2 == 1:
                    _shade(cell, "F4F7FC")
                _cell_margins(cell, top=60, bottom=60)
                para = cell.paragraphs[0]
                para.paragraph_format.space_after = Pt(0)
                para.paragraph_format.line_spacing = 1.0
                run = para.add_run(str(text))
                _set_run_font(run, size=size, color=COLOR_INK)
        # 很短的表格整体不跨页（长表允许跨页并重复表头）
        if len(rows) <= 5:
            for tr_ in table.rows[:-1]:
                for cell in tr_.cells:
                    for para in cell.paragraphs:
                        _keep_with_next(para, True)
        self.doc.add_paragraph().paragraph_format.space_after = Pt(2)
        return table

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".docx.tmp")
        self.doc.save(tmp)
        tmp.replace(path)
        return path


# ══════════════════════════════════════════════════════════════════
#  文档内容
# ══════════════════════════════════════════════════════════════════
def _cover(b: ManualBuilder):
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt

    b.blank(2)
    p = b.doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(f"{PRODUCT_NAME} · 商业运营")
    _set_run_font(run, size=13, color=COLOR_BLUE, bold=True)
    b.blank(1)
    p = b.doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(MANUAL_TITLE)
    _set_run_font(run, size=30, bold=True, color=COLOR_INK)
    b.blank(1)
    p = b.doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("发证 · 打包 · 分发 · 激活 · 升级 · 运营 全流程操作指南")
    _set_run_font(run, size=14, color=COLOR_GRAY)
    b.blank(1)
    # 分隔线
    p = b.doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _para_bottom_border(p, color="2F6FEB", size=14)
    b.blank(2)
    for label, value in (("适用产品版本", f"v{VERSION}"), ("文档版本", f"v{VERSION}"),
                         ("发布日期", RELEASE_DATE)):
        p = b.doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r1 = p.add_run(f"{label}：")
        _set_run_font(r1, size=12, color=COLOR_GRAY)
        r2 = p.add_run(value)
        _set_run_font(r2, size=12, bold=True, color=COLOR_INK)
        p.paragraph_format.space_after = Pt(6)
    b.blank(2)
    # 内部资料标识（1x1 表格）
    table = b.doc.add_table(rows=1, cols=1)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    cell = table.rows[0].cells[0]
    cell.width = Cm(10)
    _shade(cell, "FCEBEB")
    _table_borders(table, color="C43636", size=6)
    _cell_margins(cell, top=120, bottom=120)
    para = cell.paragraphs[0]
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = para.add_run("内部资料 · 仅供发行方 / 商业运营 / 客服使用 · 请勿外传")
    _set_run_font(run, size=11, bold=True, color=COLOR_RED)
    b.blank(2)
    p = b.doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("AutoMind 技术团队 · 商业运营组")
    _set_run_font(run, size=10.5, color=COLOR_MUTED)
    b.page_break()


def _doc_info(b: ManualBuilder):
    b.h1("文档信息")
    b.table(
        ["项目", "内容"],
        [
            ["文档名称", f"《{MANUAL_TITLE}》"],
            ["文档版本", f"v{VERSION}"],
            ["适用产品版本", f"AutoMind v{VERSION}（社区版 / 专业版 / 企业版）"],
            ["适用读者", "发行方（密钥与发证）、商业运营（发证 / 台账 / 续费）、客服与交付实施、客户 IT"],
            ["文档密级", "内部资料（含商业运营流程与命令行示例；不含私钥）"],
            ["维护责任人", "AutoMind 商业运营组"],
            ["更新日期", RELEASE_DATE],
            ["关联文档", "docs/COMMERCIAL_ACTIVATION.md、docs/LICENSE_OPERATIONS.md、"
                         "RELEASE.md、CHANGELOG.md、docs/UPGRADE_GUIDE.md、docs/OFFLINE_OPS.md"],
            ["适用范围", "离线 Ed25519 许可证 v2/v3；.ampkg 一键升级包；商业运营工具链（v1.11.15 引入，"
                         "v1.11.17 沿用且 CLI 未变化）"],
        ],
        widths=[3.6, 12.4], size=9.0,
    )
    b.callout("warn", "阅读前提",
              f"本手册描述的是 v{VERSION} 的发行工具链。商业运营 CLI（issue / bundle / issue-batch / "
              "revoke / crl / machine / doctor）自 v1.11.15 起未变化，命令示例以仓库内 "
              "`python pro/tools/issue_license.py --help` 的最终输出为准；"
              "若文档与代码不一致，以代码为准并反馈商业运营组更新文档。")
    b.h2("版本记录")
    b.table(
        ["版本", "日期", "变更说明", "维护人"],
        [
            [f"v{VERSION}", RELEASE_DATE,
             "版本升级：任务执行连续性（预算自动续窗 / 四模式断点续跑 / 工作流自动化强化 / "
             "执行治理参数生效 / 路由与成本页修复）；商业发证与激活流程与 v1.11.15 一致", "商业运营组"],
            ["v1.11.15", "2026-10-22",
             "商业运营工具链：许可证 v3（序列号 / 机器绑定 / 生效日）、批量发证与 12 列台账、"
             "CRL 吊销、周期复检、宽限期、密钥轮换", "商业运营组"],
            ["v1.11.14", "2026-10-21", "商业激活闭环首个可用版本（真实公钥 / 同版本 wheel / .ampkg）",
             "商业运营组"],
        ],
        widths=[1.8, 2.2, 10.0, 2.0], size=8.8,
    )
    b.page_break()


def _toc(b: ManualBuilder):
    from docx.shared import Cm, Pt

    b.h1("目录")
    b.p("本手册为静态目录（Word 打开后无需更新域）。按角色建议阅读顺序：",
        space_after=4)
    b.bullets([
        "发行方 / 技术负责人：第 3 章 → 第 4 章 → 第 5 章 → 第 11 章",
        "商业运营 / 客服：第 2 章 → 第 4 章 → 第 6 章 → 第 8 章 → 第 9 章 → 第 10 章",
        "客户 IT / 交付实施：第 6 章 → 第 7 章 → 第 10 章 → 附录",
    ])
    b.blank(1)
    toc = [
        ("第 1 章  手册说明与术语", ["1.1 本手册解决什么问题", "1.2 适用版本与前置条件",
                                     "1.3 术语表", "1.4 按角色的阅读路径", "1.5 相关文档与命令入口"]),
        ("第 2 章  商业版体系总览", ["2.1 三个版本与能力差异", "2.2 许可证类型",
                                    "2.3 激活架构", "2.4 商业包边界", "2.5 版本能力一览（v1.11.16 / v1.11.17）"]),
        ("第 3 章  发行方环境准备", ["3.1 环境要求", "3.2 一次性密钥仪式", "3.3 私钥保管",
                                    "3.4 公钥与 wheel 一致性", "3.5 doctor 自检逐项解读",
                                    "3.6 密钥轮换", "3.7 私钥泄露应急", "3.8 禁止事项"]),
        ("第 4 章  签发许可证", ["4.1 签发前检查", "4.2 单份签发", "4.3 机器绑定签发",
                                "4.4 批量签发 issue-batch", "4.5 verify 核对", "4.6 台账字段与命名",
                                "4.7 常见参数错误", "4.8 试用、宽限与到期策略"]),
        ("第 5 章  制作与分发 .ampkg", ["5.1 .ampkg 是什么", "5.2 单客户出包 bundle",
                                      "5.3 一键发行脚本", "5.4 包结构与 manifest",
                                      "5.5 完整性校验", "5.6 分发渠道与话术",
                                      "5.7 批量出包与 CRL 附带", "5.8 归档与保留"]),
        ("第 6 章  客户侧激活", ["6.1 三种激活方式", "6.2 界面操作步骤", "6.3 命令行 / 无界面激活",
                                "6.4 离线内网激活", "6.5 机器绑定与换机", "6.6 激活成功验证清单",
                                "6.7 激活失败原因与处置"]),
        ("第 7 章  版本升级", ["7.1 版本一致性纪律", "7.2 升级前检查", "7.3 升级步骤",
                              "7.4 热激活与重启", "7.5 升级决策树", "7.6 回滚", "7.7 失败处置"]),
        ("第 8 章  续期、升档、换机与吊销", ["8.1 续期", "8.2 升档", "8.3 换机",
                                           "8.4 吊销 CRL", "8.5 CRL 分发与生效",
                                           "8.6 到期 / 宽限 / 降级行为"]),
        ("第 9 章  商业运营台账与续费提醒", ["9.1 台账字段规范", "9.2 台账维护流程",
                                           "9.3 月度巡检清单", "9.4 续费提醒节奏与话术",
                                           "9.5 客服话术模板", "9.6 数据保护"]),
        ("第 10 章  排障手册", ["10.1 排障总原则", "10.2 原因码速查", "10.3 doctor 输出解读",
                              "10.4 客户侧诊断", "10.5 批量失败处置", "10.6 文件位置与优先级",
                              "10.7 时钟 / 时区 / 宽限", "10.8 升级与升级包问题",
                              "10.9 工单升级路径"]),
        ("第 11 章  安全与合规", ["11.1 密钥安全", "11.2 数据安全", "11.3 离线合规",
                                "11.4 审计与留痕", "11.5 泄露 / 事故响应", "11.6 合规检查表"]),
        ("第 12 章  附录", ["12.1 CLI 速查", "12.2 发行前检查清单", "12.3 客户交付检查清单",
                           "12.4 CSV 模板", "12.5 manifest 示例", "12.6 FAQ", "12.7 版本历史"]),
    ]
    for title, subs in toc:
        p = b.doc.add_paragraph()
        p.paragraph_format.space_before = Pt(5)
        p.paragraph_format.space_after = Pt(1)
        r = p.add_run(title)
        _set_run_font(r, size=11, bold=True, color=COLOR_INK)
        for sub in subs:
            sp = b.doc.add_paragraph()
            sp.paragraph_format.left_indent = Cm(0.9)
            sp.paragraph_format.space_after = Pt(0)
            sp.paragraph_format.line_spacing = 1.05
            r = sp.add_run(sub)
            _set_run_font(r, size=8.8, color=COLOR_GRAY)
    b.page_break()


# ── 第 1 章 ──────────────────────────────────────────────────────
def _ch1(b: ManualBuilder):
    b.h1("第 1 章  手册说明与术语")

    b.h2("1.1 本手册解决什么问题")
    b.p("AutoMind 的商业能力由闭源扩展包 automind-pro 提供，通过离线 Ed25519 许可证授权。"
        "从 v1.11.14 起，这条链路已经闭环；v1.11.15 补齐了商业运营所需的批量发证、台账、CRL 吊销、"
        "机器绑定、周期复检、宽限期与密钥轮换能力；v1.11.16 聚焦任务执行连续性（预算自动续窗、"
        "四模式断点续跑、工作流自动化强化、治理参数生效），v1.11.17 修复商业特性 REST 路由未挂载"
        "（已激活却提示语义缓存 / 智能路由 / 成本仪表盘未开通）并增强工作区会话管理；商业发证与激活流程保持不变。"
        "本手册把「发行方怎么准备、怎么给客户发证、客户怎么激活、版本怎么升级、"
        "运营怎么续费与排障」写成可以直接照做的步骤。")
    b.table(
        ["角色", "你关心的问题", "建议章节"],
        [
            ["发行方 / 技术负责人", "私钥怎么放？公钥怎么进 wheel？怎么出 .ampkg？怎么换钥？", "第 3 / 4 / 5 / 11 章"],
            ["商业运营", "给客户发什么？到期怎么续？批量客户怎么办？台账怎么记？", "第 2 / 4 / 5 / 8 / 9 章"],
            ["客服 / 售后", "客户激活失败是什么原因？怎么回复？什么时候升级工单？", "第 6 / 10 章"],
            ["客户 IT / 交付实施", "怎么激活？怎么验证？升级要注意什么？离线环境怎么办？", "第 6 / 7 / 10 章"],
        ],
        widths=[3.4, 9.0, 3.6], size=9.2, caption="本手册按角色组织",
    )

    b.h2("1.2 适用版本与前置条件")
    b.bullets([
        f"适用产品：AutoMind v{VERSION}（社区版 / 专业版 / 企业版），商业扩展包 automind_pro v{VERSION}。",
        "许可证协议：离线 Ed25519 签名，许可证串形如 AMP2-<KID>-<PAYLOAD>-<SIG>；"
        "v3 payload 增加 id（序列号，8~16 位字母数字）、m（机器指纹 sha256 前 16 位）、n（生效日）三个可选字段；"
        "三个都不给时仍编码 v2，v2 许可证继续可解析。",
        "运行环境：Python 3.11+（推荐 3.12）；发行方需要仓库完整源码（含 pro/ 与 pro/.license-private/）。",
        "发行方网络：签发与打包全部离线完成；客户机激活同样离线，不需要外网。",
        "权限：安装/升级与激活属于管理动作；远程操作需要管理员令牌 AUTOMIND_ADMIN_TOKEN。",
    ])
    b.callout("warn", "命令版本以 --help 为准",
              f"本手册中的命令均来自 v{VERSION} 发行工具链（商业运营 CLI 自 v1.11.15 起未变化）。"
              "实际执行前先跑 `python pro/tools/issue_license.py --help` 与子命令 `--help` 核对参数；"
              "若文档与代码不一致，以代码为准。")

    b.h2("1.3 术语表")
    b.table(
        ["术语", "含义", "出现位置"],
        [
            ["许可证（License）", "一行字符串（AMP2-…），由发行方私钥签名，声明档位、到期日、席位、"
                                  "可选序列号 / 机器指纹 / 生效日", "第 4 / 5 / 6 章"],
            ["验签公钥（PUBLIC_KEY_HEX）", "内置在 automind_pro 包里的 Ed25519 公钥，只能验签、不能签发",
             "第 3 章"],
            ["签发私钥（private_key.hex）", "发行方离线介质上的 32 字节种子，能签发任何许可证；"
                                            "泄露 = 付费墙失效", "第 3 / 11 章"],
            ["key_id / KID", "验签公钥指纹（sha256 前 16 位 hex），用于识别“哪把钥匙签的”", "第 3 / 10 章"],
            [".ampkg", "一键升级包：zip 内含清单 + 许可证 + 商业 wheel（+ 可选 CRL）", "第 5 / 6 章"],
            ["wheel", "Python 安装包 automind_pro-<ver>-py3-none-any.whl；必须与主程序同版本", "第 3 / 5 / 7 章"],
            ["edition", "当前档位：community / pro / enterprise；可由 GET /api/health 查询", "第 6 章"],
            ["feature flags", "商业特性开关集合（如 model_router、cost_dashboard）；由档位决定", "第 2 / 6 章"],
            ["序列号（serial）", "v3 的业务编号（8~16 位字母数字），台账、CRL 吊销按序列号定位；"
                                 "v2 无序列号时用 sha256(许可证)[:12] 派生", "第 4 / 8 章"],
            ["机器指纹（machine_id）", "客户机稳定指纹（16 位 hex）= hostname + MAC/UUID + 平台 + 安装盐的 sha256 前 16 位；"
                                       "绑定版许可证校验它与 payload 的 m 字段", "第 4 / 6 / 8 章"],
            ["not-before（生效日）", "许可证生效日（v3 字段 n）；早于该日期得到“尚未生效”，旧证继续有效", "第 4 / 8 章"],
            ["CRL", "吊销清单 license_crl.json，列出已吊销序列号；可随 .ampkg 下发或本地放置", "第 8 / 10 章"],
            ["宽限期（grace）", "到期后仍可使用的天数，客户机环境变量 AUTOMIND_LICENSE_GRACE_DAYS 控制，默认 0；"
                                "只对到期生效", "第 8 章"],
            ["周期复检（recheck）", "客户端后台复检许可证状态（默认 3600 秒），过期/吊销/机器不符/时钟回拨自动降级，"
                                    "恢复后自动重新激活", "第 6 / 8 章"],
            ["时钟回拨", "系统时间比上次记录回拨超过 1 天（86400 秒）；检测后降级并记录 clock_rollback", "第 10 章"],
            ["原因码（reason）", "许可证无效/降级的具体原因（expired / not_yet_active / machine_mismatch / revoked 等）",
             "第 6 / 10 章"],
            ["台账", "商业运营维护的客户/证书/到期/交付/续费记录表（12 列 CSV，由 issue-batch 生成）", "第 9 章"],
        ],
        widths=[3.3, 9.4, 3.3], size=8.8, caption="常用术语（完整 FAQ 见 12.6）",
    )

    b.h2("1.4 按角色的阅读路径")
    b.bullets([
        "第一次接手商业运营：先读第 2 章建立体系认知，再按第 9 章建立台账，"
        "最后用第 4 / 5 章的流程发第一张证。",
        "客户催“为什么还是社区版”：直接跳到第 10.2 原因码速查与第 6.7 失败处置。",
        "发版日：按第 7 章升级纪律与第 5.3 节一键发行脚本执行，出包后跑 5.5 完整性校验。",
        "年检 / 审计：第 11 章安全合规 + 第 9.3 月度巡检清单。",
    ])

    b.h2("1.5 相关文档与命令入口")
    b.table(
        ["文档 / 入口", "用途"],
        [
            ["docs/COMMERCIAL_ACTIVATION.md", "商业激活闭环总览、密钥纪律、doctor 检查项、排障"],
            ["docs/LICENSE_OPERATIONS.md", "商业运营工具链（批量发证 / CRL / 机器绑定 / 复检 / 换钥）细则"],
            ["RELEASE.md", "发布流程、版本一致性、构建产物与 PyPI / 安装包发布"],
            ["CHANGELOG.md", "每个版本的变更明细与测试数字"],
            ["docs/UPGRADE_GUIDE.md", "专业版 / 企业版升级方案与客户升级步骤"],
            ["docs/OFFLINE_OPS.md", "内网 / 离线部署运维手册（与发证配合使用）"],
            ["python pro/tools/issue_license.py --help", "签发工具全部子命令与参数（唯一权威）"],
            ["scripts/build_pro_upgrade.py --help", "一键发行脚本（构建 wheel → doctor → 出 .ampkg）"],
        ],
        widths=[6.2, 9.8], size=9.2, caption="相关文档与命令",
    )
    b.page_break()


# ── 第 2 章 ──────────────────────────────────────────────────────
def _ch2(b: ManualBuilder):
    b.h1("第 2 章  商业版体系总览")

    b.h2("2.1 三个版本与能力差异")
    b.p("社区版是 MIT 开源的完整 Agent 框架；专业版 / 企业版通过商业扩展包解锁进阶能力。"
        "三者的边界如下（以 v1.11.17 的 edition 特性为准，完整清单见 docs/EDITIONS.md）：")
    b.table(
        ["能力", "社区版", "专业版", "企业版"],
        [
            ["对话 / 工作 / 编程 · 工具 / 技能 / MCP / 插件", "✅", "✅", "✅"],
            ["Web 工作台 + CLI + 自主任务闭环", "✅", "✅", "✅"],
            ["安全能力（审批 / 审计 / 鉴权 / 限流 / 脱敏）", "✅", "✅", "✅"],
            ["RAG 知识库", "5 文档 / 10MB", "无限 / 200MB + 多库 / Reranker", "混合检索 / 审计 / 批量导入"],
            ["🤝 协同模式 / 🔁 循环模式 / ⏰ 定时任务", "—", "✅", "✅"],
            ["📊 高级统计 / 📈 看板 / 审计报告 PDF", "—", "✅", "✅（更多历史与维度）"],
            ["⚡ 语义缓存 / 🧭 模型智能路由", "—", "✅ 基础 / 2 级", "✅ 高级 / N 级"],
            ["💰 成本仪表盘", "—", "—", "✅"],
            ["🤖 子智能体委派", "—", "✅", "✅"],
            ["👥 多用户会话池（执行态隔离）", "—", "—", "✅"],
            ["🔐 SSO / LDAP · 🧩 RBAC · 🚪 私有模型网关", "—", "—", "✅"],
            ["每日任务 / 工作区 / 工作流限额", "100 次 / 3 个 / 20 个", "不限 / 30 个 / 200 个", "全部不限"],
        ],
        widths=[7.6, 3.2, 3.0, 2.2], size=8.8, caption="版本能力差异（v1.11.17）",
    )
    b.callout("ok", "一句话",
              "客户买的是「档位 + 有效期 +（可选）席位 +（可选）机器绑定」；"
              "所有这些都写在签名许可证里，客户改不了、发行方离线可签。")

    b.h2("2.2 许可证类型")
    b.table(
        ["维度", "取值", "说明"],
        [
            ["档位 tier", "PRO / ENT", "专业版 / 企业版；决定解锁哪些商业特性"],
            ["有效期 expiry", "YYYYMMDD / 00000000", "到期日；00000000 表示永久。与 --days 二选一"],
            ["按天到期 days", "正整数（CSV 列 days / CLI --days）", "今天 + N 天到期；适合试用 / 短期 POC，自动算到期日"],
            ["席位 seats", "0 / 正整数", "0 = 不限席位；>0 = 合同席位（台账登记）"],
            ["序列号 serial", "8~16 位字母数字 / 不填", "v3 字段；台账与 CRL 吊销的主键；不填则发 v2（无序列号）"],
            ["机器绑定 machine", "16 位 hex / 不绑定", "v3 字段；绑定后只在指定机器激活；auto = 发行方本机（仅测试）"],
            ["生效日 start", "YYYYMMDD / 立即生效", "v3 字段（not-before）；未到日期前旧证继续有效"],
            ["协议版本", "v2 / v3", "serial / machine / start 三个都不给时输出 v2（兼容旧客户端）"],
        ],
        widths=[3.2, 5.0, 7.8], size=9, caption="许可证的业务维度（v1.11.17）",
    )

    b.h2("2.3 激活架构")
    b.p("激活链路只有四层：发行方私钥签名 → 客户包内置公钥验签 → 档位/特性生效 → 界面与接口反映状态。"
        "私钥不参与客户端任何计算，因此客户机即使拿到完整 wheel 也无法伪造许可证。")
    b.figure("fig01_activation_overview.png",
             "商业激活总览：发行方准备 → 签发打包 → 分发 → 客户激活", note="示意图")
    b.figure("fig02_roles_materials.png",
             "角色与物料清单：私钥只在发行方，客户侧只有公钥与许可证", note="示意图")

    b.h2("2.4 商业包边界")
    b.table(
        ["内容", "是否进 wheel", "是否进 git", "拿到它的人能做什么"],
        [
            ["验签公钥 keys.py", "✅", "✅（公开）", "验签；不能签发"],
            ["Ed25519 验签 / 许可证解析代码", "✅", "✅", "解析与校验"],
            ["签发私钥 pro/.license-private/private_key.hex", "❌", "❌（gitignore + ACL）", "——（客户拿不到）"],
            ["签发工具 pro/tools/issue_license.py", "❌（打包 exclude + prune + CI 审计）", "✅ 内部仓库", "签发（仅发行方机器）"],
            ["客户端 guard / CRL / 复检代码", "✅", "✅", "校验、降级、状态展示"],
            ["dist/upgrades/*.ampkg", "❌", "❌（dist/ 被忽略）", "安装 + 激活（需有效许可证）"],
        ],
        widths=[5.6, 4.4, 3.2, 2.8], size=8.8, caption="商业包与密钥边界",
    )

    b.h2("2.5 版本能力一览（v1.11.16 / v1.11.17）")
    b.p("v1.11.16 是一次“任务执行连续性”版本：Token 预算耗尽不再让任务停住（自动续窗 + 多模式带进度续跑），"
        "断点续跑在计划 / ReAct / 协同 / 循环四种模式下真正可用，工作流自动化与执行治理参数同步强化，"
        "并修复「路由与成本」页的偶发崩溃。商业发证、激活、台账、CRL 等 licensing 流程与 v1.11.15 完全一致，"
        "CLI 未变化，本手册第 3–10 章继续适用。")
    b.p("v1.11.17 是一次修复 + 体验版本：修复社区核心「可挂载特性」清单漏项导致的商业特性 REST 路由未注册"
        "（语义缓存 / 智能路由 / 成本仪表盘 / 知识库进阶因此返回 403，已激活也被提示「未开通或被版本门控」），"
        "并给工作区会话管理补上分组方式 / 排序方式 / 筛选与搜索。商业发证、激活、台账、CRL 等 licensing 流程不变。")
    b.figure("fig09_v1116_features.png",
             "v1.11.16 新功能一览（预算续窗 / 四模式断点续跑 / 工作流强化 / 治理生效 / 路由成本页修复）",
             note="示意图")
    b.table(
        ["能力", "面向", "版本门槛", "说明位置"],
        [
            ["Token 预算连续性与多模式续跑（协同 / 循环 / 子智能体）", "终端用户", "社区版；协同/循环续跑需专业版",
             "本节；细节见 CHANGELOG 1.11.16 §一"],
            ["断点续跑四模式（计划 / ReAct / 协同 / 循环）", "终端用户", "社区版 + 专业版",
             "本节；操作见使用手册 / Web 断点横幅"],
            ["工作流自动化强化（if/unless、fallback、整轮超时 timeout_seconds、Token 统计、触发器补偿/重放保护）",
             "工作流用户", "社区版；触发器补偿/去重为专业版", "本节；语法见 docs/WORKFLOWS.md"],
            ["执行治理 26 项参数真正生效（含生效时机提示）", "管理员 / 运维", "社区版",
             "本节；详见 docs/GOVERNANCE.md"],
            ["「路由与成本」页崩溃修复与旧版 API 数据兼容", "专业 / 企业用户", "商业扩展包",
             "本节；细节见 CHANGELOG 1.11.16 §五"],
            ["商业特性 REST 路由挂载修复（语义缓存 / 智能路由 / 成本仪表盘 / 知识库进阶）", "专业 / 企业用户",
             "社区核心 v1.11.17 + 商业扩展包", "本节；细节见 CHANGELOG 1.11.17 §一"],
            ["商业运营工具链（v3 许可证 / 批量发证 / CRL / 机器绑定 / 复检 / 换钥）", "发行方 / 商业运营",
             "沿用 v1.11.15", "第 3–10 章（流程与命令不变）"],
        ],
        widths=[7.0, 2.6, 3.0, 3.4], size=8.2, caption="v1.11.17 能力与手册章节映射",
    )
    b.callout("warn", "本轮升级建议换发 Pro 扩展包（同号发布纪律）",
              "v1.11.17 的修复在社区核心（挂载清单改为派生），商业包按发行纪律同号重建："
              "核心与商业包必须同为 1.11.17，需重新构建 pro wheel 与 .ampkg。"
              "旧 1.11.15 扩展包可继续激活，但不支持 v1.11.16 起的续跑参数，也没有 v1.11.17 的路由挂载修复。"
              "详见第 7 章。")
    b.page_break()


# ── 第 3 章 ──────────────────────────────────────────────────────
def _ch3(b: ManualBuilder):
    b.h1("第 3 章  发行方环境准备")

    b.h2("3.1 环境要求")
    b.table(
        ["项目", "要求", "检查命令"],
        [
            ["操作系统", "Windows 10/11（推荐）/ macOS / Linux", "—"],
            ["Python", "3.11+（推荐 3.12）", "python --version"],
            ["仓库", "完整源码（含 pro/、scripts/、pro/.license-private/）", "dir pro\\tools\\issue_license.py"],
            ["私钥目录", "pro/.license-private/（gitignored、权限收紧）", "dir pro\\.license-private"],
            ["网络", "发行方全程可离线；只有构建依赖 wheel 时需要内网源", "—"],
            ["前置自检", "doctor 全绿、pro/dist 有当前版本 wheel", "python pro/tools/issue_license.py doctor"],
        ],
        widths=[2.8, 8.2, 5.0], size=9, caption="发行方环境清单",
    )

    b.h2("3.2 一次性密钥仪式")
    b.p("密钥只生成一次；换机器或换钥时才重做。推荐用 --write-public-key 让工具直接写回 keys.py，"
        "避免“忘了粘公钥 → 发出去的包永远激活不了”。")
    b.code(
        "# 1) 生成 Ed25519 密钥对；--write-public-key 自动写回 keys.py 的 PUBLIC_KEY_HEX\n"
        "python pro/tools/issue_license.py keygen --write-public-key\n"
        "\n"
        "# 输出 JSON（公开信息，可留档）：key_file / keys_file / public_key_hex / key_id / next\n"
        "\n"
        "# 2) 确认置备生效：provisioned=true 且 key_id 非空\n"
        "python pro/tools/issue_license.py pubkey\n"
        "\n"
        "# 3) 公钥变了必须重建商业 wheel（wheel 内置验签公钥）\n"
        "cd pro && python -m pip wheel . --no-deps --no-build-isolation -w dist && cd ..\n"
        "\n"
        "# 4) 发行前自检\n"
        "python pro/tools/issue_license.py doctor")
    b.callout("danger", "私钥纪律（红线）",
              "私钥只存在于发行方离线介质 / 密码库；绝不进 git、wheel、sdist、文档、聊天、日志或截图。"
              "一旦泄露，付费墙立即失效，必须按第 3.7 节走泄露应急。")
    b.table(
        ["keygen 用法", "行为"],
        [
            ["keygen", "只生成私钥并打印公钥（需手工粘贴到 keys.py，不推荐）"],
            ["keygen --write-public-key", "生成私钥 + 自动改写 keys.py 的 PUBLIC_KEY_HEX + 打印 key_id（推荐）"],
            ["keygen --force --write-public-key", "覆盖已有私钥并直接替换主钥（旧钥不留 LEGACY；旧证全部作废，慎用）"],
            ["keygen --rotate", "平滑换钥：新私钥写默认文件、新公钥成主钥、旧主钥移入 LEGACY 列表"],
            ["keygen --rotate --retire-after 20271231", "同上，并记录旧钥预计下线日（doctor 到期后强制失败）"],
            ["keygen --out <目录> --keys-file <keys.py>", "自定义落点（测试 / 多钥匙场景）"],
        ],
        widths=[5.8, 10.2], size=9, caption="keygen 的几种用法",
    )
    b.figure("fig11_key_rotation.png",
             "密钥轮换与私钥泄露应急：两条完全不同的路径", note="示意图")

    b.h2("3.3 私钥保管")
    b.bullets([
        "落点：pro/.license-private/private_key.hex；权限 0600（Windows 上通过 ACL 收紧到当前用户 + SYSTEM）。",
        "备份：离线介质 2 份 + 密码库 1 份（3-2-1 原则）；备份介质入保险柜 / 加密盘，登记领用记录。",
        "双人管控：私钥使用需两人在场或审批单；不建议把私钥拷进个人电脑日常目录。",
        "禁止：不要提交、不要上传网盘、不要通过 IM / 邮件发送、不要贴进工单或日志。",
        "丢失：存量许可证继续有效，但无法再签新证；只能重新 keygen → 存量客户全部换发。",
    ])
    b.callout("warn", "Windows 权限检查",
              "`icacls pro\\.license-private` 应只显示当前用户与 SYSTEM；若发现 Everyone 或 Users 有读权限，"
              "立即 `icacls ... /inheritance:r /grant:r \"%USERNAME%:F\" /grant:r \"SYSTEM:F\"` 收紧并复查。")

    b.h2("3.4 公钥与 wheel 一致性")
    b.p("客户装上 wheel 后，验签用的是 wheel 里内置的公钥 —— 不是发行方仓库里的 keys.py。"
        "所以每次公钥变化（首次置备、换钥）都必须重建 wheel，否则会出现“能装不能激活”。"
        "doctor 的 wheel_public_key_matches / wheel_version_match 两项就是守这个底线。")
    b.code(
        "# 重建商业 wheel（在 pro/ 目录执行）\n"
        "python -m pip wheel . --no-deps --no-build-isolation -w dist\n"
        "\n"
        "# doctor 应同时满足：wheel 版本 = 主程序版本，wheel 内置公钥 = keys.py 公钥\n"
        "python pro/tools/issue_license.py doctor")
    b.callout("tip", "发版纪律",
              "pro/dist 只留当前版本 wheel；旧 wheel 可能内置占位公钥或已轮换的旧钥，"
              "会被 doctor 点名，清理后再发版。")

    b.h2("3.5 doctor 自检逐项解读")
    b.p("doctor 输出 JSON：ok + checks[]（name / ok / detail）+ warnings[]；任一项失败即退出码 1。"
        "下表按 v1.11.17 检查项列出含义与修复方式（本轮商业运营工具链未变化，12 项与 v1.11.15 相同）：")
    b.table(
        ["检查项", "失败意味着什么", "修复动作"],
        [
            ["public_key_provisioned", "keys.py 公钥仍是占位符 / 非法 → 商业版无法激活", "keygen --write-public-key"],
            ["private_key_available", "找不到签发私钥", "keygen / --key-file / AUTOMIND_LICENSE_PRIVATE_KEY"],
            ["keypair_matches", "私钥与内置公钥不是一对 → 签出的证全部验不过", "用配对的私钥，或重做 keygen + 重建 wheel"],
            ["core_package_version_match", "商业包与主程序版本不同号", "同步两处 __version__ 与 pyproject"],
            ["wheel_version_match", "pro/dist 里没有当前版本 wheel", "在 pro/ 目录 pip wheel 重建"],
            ["wheel_public_key_matches", "wheel 内置公钥 ≠ keys.py（置备/换钥后忘重建 wheel）", "重建 wheel 后重新 bundle"],
            ["sign_verify_roundtrip", "本地私钥签出的探测许可证验不过（实现/密钥异常）", "检查 licensing 代码与密钥"],
            ["bundles_activatable", "已有 .ampkg 的清单 / 许可证 / wheel / 随包 CRL 有问题", "删旧包，重新 bundle"],
            ["key_rotation_state", "换钥过渡期状态异常 / 旧钥已过 retire_after 未清理", "按 3.6 完成换证并清理 LEGACY 列表"],
            ["crl_readable", "CRL 文件无法解析 / 序列号格式不对", "检查 dist/crl/license_crl.json 的 revoked 数组"],
            ["license_recheck_config", "复检被关闭（0s）或宽限配置非法", "恢复 AUTOMIND_LICENSE_RECHECK / GRACE_DAYS"],
            ["machine_binding_ready", "机器指纹 / 机器绑定相关能力不可用（环境异常）", "检查 machine_salt 可写与系统信息可读"],
        ],
        widths=[4.6, 6.6, 4.8], size=8.6, caption="doctor 12 项检查解读（v1.11.17，沿用 v1.11.15）",
    )
    b.callout("ok", "全绿标准",
              "`python pro/tools/issue_license.py doctor` 退出码 0、ok=true、checks 全部 ok，"
              "且 warnings 中没有 stale_wheels / 旧钥到期提示；此时才允许出包。")

    b.h2("3.6 密钥轮换")
    b.p("v1.11.15 起 keys.py 支持“主公钥 + LEGACY_PUBLIC_KEYS_HEX 列表”：按许可证里的 KID 选择对应公钥验签。"
        "计划内换钥时，新钥成主钥、旧主钥进 LEGACY 列表，旧许可证在过渡期内仍可验签；"
        "过渡期结束把旧公钥移出列表，旧证随即失效。正常轮换一年一次，过渡期建议 6~12 个月。")
    b.steps([
        "前置：doctor 全绿；旧私钥先备份到离线密码库（工具不会替你复制私钥）。",
        "执行 python pro/tools/issue_license.py keygen --rotate（可加 --retire-after 20271231 指定旧钥下线日，"
        "默认今天 + 365 天）；输出 key_id / old_key_id / legacy_key_ids / retire_after。",
        "重建商业 wheel（内置新主钥 + 旧钥列表），跑 doctor：key_rotation_state 应显示“过渡期：N 把旧钥仍被认可”。",
        "过渡期内新证用新私钥签发（工具默认新钥）；存量客户在 retire_after 前完成换发。",
        "到期收口：清空 LEGACY_PUBLIC_KEYS_HEX（与 LEGACY_KEYS_RETIRE_AFTER 同步）→ 重建 wheel → doctor。"
        "retire_after 到期后 doctor 会失败，强制完成这一步。",
    ])
    b.code(
        "# 正常轮换（一年一次）\n"
        "python pro/tools/issue_license.py doctor\n"
        "python pro/tools/issue_license.py keygen --rotate --retire-after 20271231\n"
        "cd pro && python -m pip wheel . --no-deps --no-build-isolation -w dist && cd ..\n"
        "python pro/tools/issue_license.py doctor\n"
        "\n"
        "# 之后新证自动用新私钥；旧证在过渡期内继续有效（客户端按 KID 选钥）\n"
        "python pro/tools/issue_license.py bundle PRO 20281231 acme")
    b.callout("warn", "换钥不是换证",
              "换钥后旧许可证在 LEGACY 有效期内继续可用；但 retire_after 到期（或旧公钥被移出）后，"
              "旧证立即全部失效。请与客户约定过渡窗口，避免业务中断。")

    b.h2("3.7 私钥泄露应急")
    b.p("泄露 = 存量全部作废。此时不能把旧钥留在 LEGACY（那等于继续认可泄露钥签的证），"
        "应直接替换主钥、清空旧钥列表，然后全部客户换证。")
    b.table(
        ["步骤", "动作", "时限要求"],
        [
            ["1", "隔离发行机（断网）、保留现场；通知安全负责人与商业负责人", "立即"],
            ["2", "紧急换钥：keygen --force --write-public-key（直接替换主钥、清空旧钥列表）", "1 小时内"],
            ["3", "重建 wheel，跑 doctor（key_rotation_state 应为“未轮换”），重新出包", "24 小时内"],
            ["4", "台账中旧序列号全部 revoke --reason ...，CRL 随新包 / 单独下发", "24 小时内"],
            ["5", "客户通知（影响面 / 新包 / 操作步骤）+ 安全事件留痕", "24~48 小时"],
        ],
        widths=[1.2, 10.6, 4.2], size=9, caption="私钥泄露应急流程",
    )
    b.callout("danger", "不要试图“悄悄换钥”",
              "泄露事件必须留痕并通知客户；不换钥或只换 wheel 不换钥，都能让旧私钥继续签发有效证，"
              "等于付费墙一直开着。")

    b.h2("3.8 禁止事项")
    b.bullets([
        "禁止把私钥提交到 git / 上传 PyPI / 打进 wheel / sdist（CI 有审计，构建会失败）。",
        "禁止在客户机器上执行 keygen 或保存私钥（客户侧只有验签能力）。",
        "禁止把同一个私钥文件发给多人或多台机器长期保存（应走密码库 / 受控介质）。",
        "禁止跳过 doctor 直接出包；禁止 pro/dist 与 dist/upgrades 里留旧 wheel / 旧 .ampkg。",
        "禁止在文档、工单、聊天、截图中出现 private_key.hex 内容或 64 位 hex 种子。",
        "禁止对未经授权客户签发 / 延长 / 升档；所有变更以合同与台账为准。",
        "禁止修改已签发许可证的到期日 / 档位（签名覆盖这些字段，改了必然验签失败）。",
    ])
    b.page_break()


# ── 第 4 章 ──────────────────────────────────────────────────────
def _ch4(b: ManualBuilder):
    b.h1("第 4 章  签发许可证")

    b.h2("4.1 签发前检查")
    b.bullets([
        "doctor 全绿（退出码 0），私钥可用且与内置公钥配对。",
        "确认客户合同信息：档位（PRO / ENT）、到期日、席位、是否绑定机器、是否试用。",
        "确认台账里已预留序列号区间（同一批证书不重复）。",
        "确认客户主程序版本与将交付的商业 wheel 同版本（否则 .ampkg 会在客户侧提示版本不一致）。",
    ])

    b.h2("4.2 单份签发")
    b.p("单份签发适用于少量客户或单独续期 / 换机。签发只输出许可证串，不改变任何文件；"
        "需要给客户发文件时走第 5 章 bundle。")
    b.code(
        "# 传统写法：到期日 + 客户标识（输出 v2 许可证，兼容旧客户端）\n"
        "python pro/tools/issue_license.py issue PRO 20271231 acme\n"
        "\n"
        "# 企业版，永久，50 席，指定签发日\n"
        "python pro/tools/issue_license.py issue ENT 00000000 bigcorp --seats 50 --issued 20261022\n"
        "\n"
        "# 按天到期（expiry 省略；第二个位置参数是客户名）\n"
        "python pro/tools/issue_license.py issue PRO acme --days 365\n"
        "\n"
        "# v3：序列号（台账 / 吊销主键）\n"
        "python pro/tools/issue_license.py issue PRO acme --days 365 --serial ACME20260001\n"
        "\n"
        "# v3：指定生效日（预约开通，旧证继续有效到生效日前）\n"
        "python pro/tools/issue_license.py issue PRO acme --days 365 --start 20270101\n"
        "\n"
        "# v3：绑定机器指纹（客户从升级面板复制 16 位指纹）\n"
        "python pro/tools/issue_license.py issue ENT acme --days 365 \\\n"
        "    --serial ACME20260002 --machine 0123456789abcdef\n"
        "\n"
        "# 错误示例：expiry 与 --days 同时给会报错退出\n"
        "# python pro/tools/issue_license.py issue PRO 20271231 acme --days 365\n"
        "\n"
        "# JSON 输出（便于脚本 / 台账解析）\n"
        "python pro/tools/issue_license.py issue PRO 20271231 acme --json")
    b.callout("tip", "v2 还是 v3？",
              "不给 --serial / --machine / --start 时输出 v2（历史行为不变，兼容旧扩展包）；"
              "给了任一字段即编码 v3。给还在用旧扩展包（≤v1.11.14）的客户发证时请保持 v2，"
              "或直接用 .ampkg 一并升级扩展包（推荐，包内 wheel 已是新版）。")
    b.table(
        ["参数", "是否必填", "说明", "示例"],
        [
            ["tier", "是", "PRO / ENT", "PRO"],
            ["expiry", "二选一", "到期日 YYYYMMDD；00000000 = 永久；与 --days 互斥", "20271231"],
            ["customer", "是", "客户标识（字母数字 / ._@- 归一，最长 64）；用 --days 时放第二个位置", "acme"],
            ["--days", "二选一", "今天 + N 天到期；与 expiry 互斥（同时给会报错）", "--days 365"],
            ["--seats", "否", "席位数；0 = 不限", "--seats 50"],
            ["--issued", "否", "签发日 YYYYMMDD；默认今天", "--issued 20261022"],
            ["--serial", "否", "v3 序列号（8~16 位字母数字）；台账 / 吊销主键", "--serial ACME20260001"],
            ["--machine", "否", "v3 机器绑定：16 位 hex 指纹或 auto（= 本机指纹，仅测试）", "--machine 0123456789abcdef"],
            ["--start", "否", "v3 生效日 YYYYMMDD（not-before）；未到日期前旧证继续有效", "--start 20270101"],
            ["--key-file", "否", "指定私钥文件；默认按优先级查找", "--key-file D:\\keys\\private_key.hex"],
            ["--json", "否", "以 JSON 输出", "--json"],
        ],
        widths=[2.2, 1.8, 8.2, 3.8], size=8.4, caption="issue 子命令参数（v1.11.17，CLI 与 v1.11.15 一致）",
    )
    b.figure("fig03_license_lifecycle.png",
             "许可证生命周期：签发 → 生效 → 到期 → 宽限 → 续期", note="示意图")

    b.h2("4.3 机器绑定签发")
    b.p("绑定版许可证在 payload 的 m 字段记录机器指纹（sha256 前 16 位）；客户端启动 / 周期复检时校验本机指纹，"
        "不匹配即报 machine_mismatch 并降级社区版。发行方不需要知道客户机器的其他信息，只要 16 位指纹。")
    b.steps([
        "客户打开升级面板「本机信息」卡片，复制「本机指纹」（16 位 hex）。",
        "发行方执行 issue / bundle 时加 --machine <指纹>；--machine auto 表示“签发这台机器”，仅测试 / 演示用。",
        "签发后在台账记录序列号、指纹、绑定的客户与设备描述。",
        "客户拖入 .ampkg 激活；换机需重新签发（见 8.3）。",
    ])
    b.code(
        "# 发行方侧：打印本机机器指纹（JSON；不打印任何密钥）\n"
        "python pro/tools/issue_license.py machine\n"
        "# {\"machine_id\": \"0123456789abcdef\", \"source\": \"hardware\", ...}\n"
        "\n"
        "# 客户机（只装了扩展包、没有 pro/tools）可用面板「本机信息」复制；\n"
        "# 或运行下面这行读取指纹：\n"
        "python -c \"from automind_pro.licensing.guard import machine_id; print(machine_id())\"\n"
        "\n"
        "# 签发绑定到该指纹的企业版\n"
        "python pro/tools/issue_license.py issue ENT acme --days 365 --seats 10 \\\n"
        "    --serial ACME20260002 --machine 0123456789abcdef\n"
        "\n"
        "# 或直接出带绑定证的升级包\n"
        "python pro/tools/issue_license.py bundle ENT acme --days 365 --seats 10 \\\n"
        "    --serial ACME20260002 --machine 0123456789abcdef --out dist/upgrades")
    b.callout("info", "指纹构成与覆盖",
              "指纹 = hostname + MAC/UUID + 平台 + 安装盐（~/.automind/machine_salt）的 sha256 前 16 位；"
              "重装系统 / 清空用户目录会改变指纹，换机前先报备。容器 / 克隆机等易变场景可用 "
              "AUTOMIND_MACHINE_ID 固定指纹（与许可证 m 字段比较）。")

    b.h2("4.4 批量签发 issue-batch")
    b.p("客户量大时用 CSV 批量签发：一行一个客户，失败行不中断（默认），最终输出 JSON 摘要与台账 CSV。"
        "CSV 支持 # 注释行与空行；列顺序为 customer,tier,expiry,seats,machine,serial,start,notes[,days]。"
        "expiry 与 days 二选一。")
    b.code(
        "# customers.csv（UTF-8；# 开头是注释、空行忽略）\n"
        "# 2026-10 批次：华东区续费\n"
        "customer,tier,expiry,seats,machine,serial,start,notes,days\n"
        "acme,PRO,,5,,ACME20260001,,续费一年,365\n"
        "bigcorp,ENT,20271231,20,0123456789abcdef,ACME20260002,,绑定服务器,\n"
        "huanqiu,PRO,,2,,ACME20260003,20261201,12 月起生效,30\n"
        "\n"
        "# 批量签发 + 台账（默认 <out>/license_ledger.csv）\n"
        "python pro/tools/issue_license.py issue-batch --csv customers.csv \\\n"
        "    --out dist/licenses --ledger dist/licenses/license_ledger.csv\n"
        "\n"
        "# 同时出 .ampkg（每客户一个升级包；CRL 默认自动附带）\n"
        "python pro/tools/issue_license.py issue-batch --csv customers.csv \\\n"
        "    --out dist/licenses --bundles \\\n"
        f"    --wheel pro/dist/automind_pro-{VERSION}-py3-none-any.whl\n"
        "\n"
        "# 严格模式：任一行失败即非零退出（适合 CI / 小批量）\n"
        "python pro/tools/issue_license.py issue-batch --csv customers.csv \\\n"
        "    --out dist/licenses --strict")
    b.table(
        ["CSV 列", "必填", "说明", "示例"],
        [
            ["customer", "✅", "客户标识（字母数字 / ._@- 归一）", "acme"],
            ["tier", "✅", "PRO / ENT", "ENT"],
            ["expiry", "二选一", "到期日 YYYYMMDD；00000000 = 永久；与 days 互斥", "20271231"],
            ["days", "二选一", "今天 + N 天到期；与 expiry 同时给会报该行失败", "365"],
            ["seats", "否", "席位数，0 / 空 = 不限", "50"],
            ["machine", "否", "绑定的机器指纹（16 hex）或 auto；不绑定留空", "0123456789abcdef"],
            ["serial", "否", "8~16 位字母数字；留空则发 v2（吊销用派生序列号）", "ACME20260001"],
            ["start", "否", "生效日 YYYYMMDD；立即生效留空", "20261201"],
            ["notes", "否", "备注（只进台账，不进许可证）", "续费 3 年"],
            ["issued", "否", "签发日（默认今天，仅补录 / 排障用）", "20261022"],
        ],
        widths=[2.2, 1.6, 8.2, 4.0], size=8.4, caption="issue-batch CSV 列定义",
    )
    b.callout("warn", "批量失败处理",
              "默认“失败行汇总后继续”：JSON 摘要含 counts.ok / counts.failed 与逐行失败原因，成功行照常写证与台账；"
              "加 --strict 时任一行失败即退出码 1。修正 CSV 后只补发失败行即可（已签发客户请勿重复签发，"
              "否则台账会出现重复序列号）。")
    b.figure("fig07_batch_issuance.png",
             "批量发证与台账流程：CSV → issue-batch → 许可证 / 升级包 / 台账", note="示意图")

    b.h2("4.5 verify 核对")
    b.p("签发后（尤其批量后）必须抽验，避免“签出来装不上”。verify 只用内置公钥，不会接触私钥。")
    b.code(
        "# 校验单份许可证（退出码 0 = 有效）\n"
        "python pro/tools/issue_license.py verify \"AMP2-xxxx-...\"\n"
        "\n"
        "# 校验台账里所有许可证（示例：逐行调 verify）\n"
        "# Windows PowerShell\n"
        "Get-Content dist/licenses/license_ledger.csv | ForEach-Object { ... }")
    b.table(
        ["verify 输出字段", "含义"],
        [
            ["tier", "pro / enterprise"],
            ["customer", "客户标识"],
            ["expiry", "到期日；永久为 null"],
            ["issued", "签发日"],
            ["seats", "席位（0 = 不限）"],
            ["key_id", "签发钥匙指纹（换钥后用于区分新旧钥）"],
            ["license_version", "2 / 3（v3 起才含序列号 / 机器 / 生效日）"],
            ["serial / machine / not_before", "v3 字段：序列号 / 绑定指纹 / 生效日；v2 证该三项为空"],
        ],
        widths=[5.0, 11.0], size=9, caption="verify 输出字段",
    )
    b.callout("info", "客服排障用法",
              "客户发来许可证尾部 4 位即可先 verify：有效返回字段、无效退出码 1 并给出原因（签名不符 / 过期 / "
              "未生效 / 机器不符 / 被吊销）。完整原因码见第 10.2 节。")

    b.h2("4.6 台账字段与命名")
    b.bullets([
        "客户标识：小写字母 + 数字为主（如 acme、bigcorp）；同一客户跨版本保持一致。",
        "序列号：8~16 位字母数字，建议 <客户缩写><年份><序号>（如 ACME20260001）；签发即登记，不重复。"
        "不给序列号时发 v2 证，吊销用派生序列号 sha256(许可证)[:12]。",
        "到期日：YYYYMMDD；永久许可证写 00000000，台账另设“下次复核日”。",
        "机器绑定：登记完整指纹（16 hex）+ 设备描述，换机时逐位比对。",
        "许可证原文：可公开验证，但按商业机密管理；台账外发时建议脱敏中间段。",
        "台账完整 12 列字段见第 9.1 节（由 issue-batch 自动生成，追加写）。",
    ])

    b.h2("4.7 常见参数错误")
    b.table(
        ["报错 / 现象", "原因", "修正"],
        [
            ["tier 必须是 PRO 或 ENT", "档位大小写 / 拼写错误", "用 PRO / ENT（小写也可）"],
            ["expiry 必须是 YYYYMMDD", "传了 2027-12-31 或非法值", "改 20271231 / 00000000，或改用 --days"],
            ["expiry 与 --days 不能同时给", "参数冲突", "二选一：按天用 --days，否则给 expiry"],
            ["customer 含连字符导致解析失败", "客户标识里带 -", "改用下划线或去掉连字符"],
            ["--machine 格式不对", "不是 16 位 hex", "用客户升级面板「本机信息」复制的指纹"],
            ["--serial 长度/字符不合法", "不是 8~16 位字母数字", "如 ACME20260001（字母数字，8~16 位）"],
            ["找不到可用的签发私钥", "私钥不在默认位置且未指定", "keygen 或 --key-file / AUTOMIND_LICENSE_PRIVATE_KEY"],
            ["签出来的许可证验不过", "私钥与 keys.py 公钥不是一对", "doctor 检查 keypair_matches，重建 wheel"],
            ["旧客户端激活不了 v3 证", "旧扩展包不认识 v3 payload", "发 v2（不带 serial/machine/start）或随包升级扩展"],
            [".ampkg 提示 wheel 版本不一致", "wheel 与主程序不同版本", "重建同版本 wheel 后重出包"],
        ],
        widths=[5.0, 5.6, 5.4], size=8.4, caption="签发参数常见错误",
    )

    b.h2("4.8 试用、宽限与到期策略")
    b.bullets([
        "试用：--days N（或 CSV 的 days 列）自动把到期日设为签发日 + N 天，适合 POC；到期前 7 天客服提醒转正式。",
        "宽限期：客户机环境变量 AUTOMIND_LICENSE_GRACE_DAYS 控制到期后仍可用的天数（默认 0，即到期即降级；"
        "建议年度客户设 7~15 天）。宽限期内界面标红提示剩余天数，宽限结束降级社区版；宽限只对“到期”生效，"
        "不豁免吊销 / 机器不符 / 时钟回拨。",
        "周期复检：客户端默认每 3600 秒复检一次（AUTOMIND_LICENSE_RECHECK，0 = 关闭）；过期 / 吊销 / 机器不符 / "
        "时钟回拨会在一小时内自动降级，续期后自动恢复，无需重启。",
        "到期降级：服务照常启动、数据不丢，商业入口显示 🔒；拖入续期包立即恢复。",
        "提前续费：建议到期前 30 天完成续期；可用 --start 指定新证生效日，让旧证无缝接续。",
        "永久证：expiry=00000000，无到期日；换档 / 换绑机器时重新签发并替换。",
    ])
    b.page_break()


# ── 第 5 章 ──────────────────────────────────────────────────────
def _ch5(b: ManualBuilder):
    b.h1("第 5 章  制作与分发 .ampkg")

    b.h2("5.1 .ampkg 是什么")
    b.p(".ampkg 是发给客户的唯一交付物：一个 zip 文件，内含 automind-upgrade.json（清单）、"
        "license.txt（许可证）、automind_pro-<ver>-py3-none-any.whl（商业扩展包），"
        "v1.11.15 起可选携带 license_crl.json（吊销清单）。客户拖进升级面板即完成"
        "“解压装包 + 验签 + 写许可证 + 就地激活”。")
    b.figure("fig06_ampkg_anatomy.png",
             ".ampkg 一键升级包结构解剖：清单 + 许可证 + wheel（+ 可选 CRL）", note="示意图")

    b.h2("5.2 单客户出包 bundle")
    b.code(
        "# 专业版：签发 + 打包（wheel 默认取 pro/dist 里当前版本）\n"
        "python pro/tools/issue_license.py bundle PRO 20271231 acme\n"
        "\n"
        "# 企业版：永久 + 50 席 + 指定输出目录\n"
        "python pro/tools/issue_license.py bundle ENT 00000000 bigcorp \\\n"
        "    --seats 50 --out dist/upgrades\n"
        "\n"
        "# 按天到期 + 序列号 + 绑定机器（触发 v3）\n"
        "python pro/tools/issue_license.py bundle ENT acme --days 365 --seats 20 \\\n"
        "    --serial ACME20260002 --machine 0123456789abcdef --out dist/upgrades\n"
        "\n"
        "# 显式指定 wheel 与 CRL（CRL 默认 dist/crl/license_crl.json，存在才自动附带）\n"
        "python pro/tools/issue_license.py bundle PRO 20271231 acme \\\n"
        f"    --wheel pro/dist/automind_pro-{VERSION}-py3-none-any.whl")
    b.p("bundle 输出 JSON：bundle（路径）/ license / wheel / tier / customer / expiry / "
        "package_version / key_id 等。把 sha256 与许可证原文登记进台账（第 9 章）。"
        "默认会自动把存在的 CRL 打进包，manifest 记录 crl 摘要。")
    b.callout("warn", "已吊销的证打不进包",
              "bundle 会拒绝把已吊销的许可证打进 .ampkg；请先签新证或从 CRL 移除误吊销条目。")
    b.code(
        "# 计算 sha256（Windows PowerShell）\n"
        "Get-FileHash dist/upgrades/AutoMind-PRO-acme-20271231.ampkg -Algorithm SHA256\n"
        "\n"
        "# 校验客户发回的 sha256 是否一致（Linux / macOS）\n"
        "sha256sum AutoMind-PRO-acme-20271231.ampkg")

    b.h2("5.3 一键发行脚本")
    b.p("scripts/build_pro_upgrade.py 把“构建 wheel → 三处版本校验 → doctor → 签发 → 打 .ampkg → "
        "打印 sha256 与客户升级步骤”收成一条命令，任一步失败非零退出。")
    b.code(
        "# 专业版（到期 2027-12-31）\n"
        "python scripts/build_pro_upgrade.py --tier PRO --expiry 20271231 --customer acme\n"
        "\n"
        "# 企业版（永久、50 席）\n"
        "python scripts/build_pro_upgrade.py --tier ENT --expiry 00000000 --customer bigcorp --seats 50\n"
        "\n"
        "# 跳过重复构建（pro/dist 已有当前版本 wheel 时）\n"
        "python scripts/build_pro_upgrade.py --tier PRO --expiry 20271231 --customer acme --no-build\n"
        "\n"
        "# Windows PowerShell 等价写法\n"
        ".\\scripts\\build_pro_upgrade.ps1 -Tier ENT -Expiry 00000000 -Customer bigcorp -Seats 50")
    b.table(
        ["退出码", "含义", "处置"],
        [
            ["0", "成功（已打印 bundle 路径、sha256、许可证、客户步骤）", "登记台账、分发"],
            ["2", "版本不一致（核心 / 商业包 / pyproject）", "同步版本号后重跑"],
            ["3", "wheel 缺失 / 构建失败", "在 pro/ 目录重建 wheel"],
            ["4", "doctor 自检失败", "按 doctor 输出逐项修复"],
            ["5", "出包失败（bundle 报错）", "看 stderr / 重跑 bundle"],
        ],
        widths=[1.8, 8.0, 6.2], size=9, caption="build_pro_upgrade.py 退出码",
    )

    b.h2("5.4 包结构与 manifest")
    b.table(
        ["文件", "内容", "客户侧用途"],
        [
            ["automind-upgrade.json", "清单：format=1、tier、customer、expiry、seats、wheel、"
                                     "package_version、core_version、key_id、summary、crl（摘要）", "完整性校验与激活前置检查"],
            ["license.txt", "一行许可证（AMP2-…，v2 或 v3）", "验签后写入客户机许可证文件"],
            ["automind_pro-<ver>-py3-none-any.whl", "商业扩展包（内置公钥，必须与主程序同版本）", "安装扩展包"],
            ["license_crl.json（可选）", "吊销清单 {\"revoked\":[{serial,reason,time}]}", "激活 / 复检时校验吊销状态"],
        ],
        widths=[5.4, 7.6, 3.0], size=8.6, caption=".ampkg 内部结构",
    )
    b.code(
        "# manifest 示例（v1.11.17；实际字段以 bundle 输出为准）\n"
        "{\n"
        "  \"format\": 1,\n"
        "  \"tier\": \"ENT\",\n"
        "  \"customer\": \"bigcorp\",\n"
        "  \"expiry\": \"20271231\",\n"
        "  \"seats\": 50,\n"
        f"  \"wheel\": \"automind_pro-{VERSION}-py3-none-any.whl\",\n"
        "  \"summary\": {\"tier\": \"enterprise\", \"customer\": \"bigcorp\",\n"
        "              \"expiry\": \"20271231\", \"seats\": 50,\n"
        "              \"serial\": \"BIGCORP0001\", \"machine\": \"0123456789abcdef\",\n"
        "              \"not_before\": null, \"key_id\": \"e9fffafc0f6f19d4\",\n"
        "              \"license_version\": 3},\n"
        f"  \"package_version\": \"{VERSION}\",\n"
        f"  \"core_version\": \"{VERSION}\",\n"
        "  \"key_id\": \"e9fffafc0f6f19d4\",\n"
        "  \"crl\": {\"file\": \"license_crl.json\", \"sha256\": \"<摘要>\", \"count\": 3}\n"
        "}")

    b.h2("5.5 完整性校验")
    b.steps([
        "出包后本地跑 doctor：`--upgrades-dir` 指向输出目录、`--crl` 指向 CRL，确认 bundles_activatable 通过。",
        "计算 sha256 并登记台账（与客户收到的文件比对）。",
        "抽一个包在隔离环境用 install_upgrade 验证（发行方 CI / 测试环境执行）。",
        "分发前再次确认包内 wheel 版本 = 客户主程序版本（manifest.package_version）。",
    ])
    b.code(
        "# doctor 校验指定目录里的 .ampkg 与 CRL\n"
        "python pro/tools/issue_license.py doctor \\\n"
        "    --upgrades-dir dist/upgrades --crl dist/crl/license_crl.json\n"
        "\n"
        "# 校验 manifest 与 wheel 版本（人工抽查）\n"
        "python -c \"import zipfile,json; z=zipfile.ZipFile('dist/upgrades/AutoMind-PRO-acme-20271231.ampkg'); "
        "m=json.loads(z.read('automind-upgrade.json')); print(m['package_version'], m['wheel'])\"")

    b.h2("5.6 分发渠道与话术")
    b.table(
        ["渠道", "适用", "注意事项"],
        [
            ["企业邮箱（加密附件 / 网盘链接）", "正式客户", "附 sha256；说明来源可信；不要群发他客户的包"],
            ["飞书 / 企业微信 / 钉钉", "日常沟通", "文件 + 激活步骤；避免在群里发含许可证原串的截图"],
            ["客户工单系统 / 交付平台", "大型客户", "走客户 IT 审批；附安全说明（离线验签 / 无私钥）"],
            ["U 盘 / 内网文件服务器", "离线内网客户", "介质登记；与客户 IT 当面交接 sha256"],
        ],
        widths=[4.6, 4.2, 7.2], size=9, caption="分发渠道",
    )
    b.callout("info", "标准分发话术",
              "“附件是 AutoMind 商业版一键升级包（wheel + 许可证，离线校验，不需要联网、不需要 pip）。"
              "请在 AutoMind 左下角「设置 → ⭐ 升级专业版 / 企业版」把 .ampkg 拖进「⚡ 一键升级」，"
              "看到「已升级为专业版 / 企业版」即完成；如提示重启，重启一次后自动激活。"
              "文件 sha256：<填写>，下载后可自行校验。”")

    b.h2("5.7 批量出包与 CRL 附带")
    b.bullets([
        "批量出包：issue-batch --bundles，产物在 --out 目录下按客户命名；适合渠道商 / 多客户同批交付。",
        "CRL 附带：bundle / issue-batch 默认读取 dist/crl/license_crl.json（存在才附带），也可 --crl 指定；"
        "manifest 记录 crl 文件名 / sha256 / 条数，客户激活时先把 CRL 落盘再验签。",
        "已吊销的证不会被打进包（bundle 直接拒绝）；请先签新证或从 CRL 移除误吊销条目。",
        "续期 / 换机 / 升档：一律重新出包，不要手工修改已发出的 .ampkg（签名与清单都会失效）。",
        "同一版本 wheel 只需构建一次；批量出包时用 --wheel 复用，避免重复构建耗时。",
    ])

    b.h2("5.8 归档与保留")
    b.bullets([
        "dist/upgrades 只保留当前批次；历史批次归档到商业运营的受控目录（按客户 / 日期分层）。",
        "归档内容：.ampkg、sha256、许可证原文、manifest、签发命令回放记录、客户签收记录。",
        "保留期：合同期 + 2 年；到期客户的包仍需保留以便审计追溯。",
        "敏感度：.ampkg 与许可证虽可公开验证，但包含客户标识与档位，按商业机密管理，不入公开仓库。",
    ])
    b.page_break()


# ── 第 6 章 ──────────────────────────────────────────────────────
def _ch6(b: ManualBuilder):
    b.h1("第 6 章  客户侧激活")

    b.h2("6.1 三种激活方式")
    b.p("客户侧有三种等价方式，按客户环境选择；推荐「一键升级包」方式（最省步骤、离线、不需要 pip）。")
    b.figure("fig05_customer_ui_mockup.png",
             "客户激活界面示意：拖入 .ampkg / 粘贴许可证 / 上传 wheel + 许可证", note="示意图")
    b.table(
        ["方式", "适用", "步骤", "是否需要重启"],
        [
            ["A. 拖入 .ampkg（推荐）", "所有客户，尤其离线内网", "设置 → 升级面板 → 拖入 .ampkg → 完成", "通常不需要；加载过旧扩展时需一次"],
            ["B. 粘贴许可证", "已安装同版本商业 wheel", "粘贴 AMP2-… → 校验 → 激活", "通常不需要"],
            ["C. wheel + 许可证（分两步）", "运维批量 / 无界面服务器", "上传或 pip 安装 wheel → 写许可证 → 重启", "需要（命令行场景）"],
        ],
        widths=[4.4, 3.6, 5.6, 2.4], size=8.8, caption="三种激活方式对比",
    )

    b.h2("6.2 界面操作步骤")
    b.steps([
        "打开 AutoMind，进入左下角「设置」→「⭐ 升级专业版 / 企业版」（桌面版同样路径）。",
        "确认当前版本与许可证面板状态（序列号 / 绑定机器 / 复检状态等）。",
        "方式 A：把 .ampkg 拖进「⚡ 一键升级」框；方式 B：粘贴许可证；方式 C：上传 wheel 后再粘贴许可证。",
        "等待界面提示「已升级为专业版 / 企业版」；若提示需重启，重启一次（许可证已提前保存）。",
        "确认左上角版本徽标变化，并按 6.6 节验证清单逐项确认。",
    ])
    b.table(
        ["平台 / 形态", "数据目录", "激活方式差异"],
        [
            ["Windows 桌面版", "%APPDATA%\\AutoMind", "界面操作一致；许可证落盘在数据目录"],
            ["macOS 桌面版", "~/Library/Application Support/AutoMind", "同上"],
            ["Linux / 服务端", "~/.automind 或 AUTOMIND_DATA_DIR", "可用命令行 / API 激活；Web 界面同样可用"],
            ["源码运行（pip install -e）", "项目目录 / 家目录", "与源码环境一致；升级后需重启服务"],
        ],
        widths=[4.2, 5.8, 6.0], size=8.8, caption="不同平台 / 形态的激活位置",
    )

    b.h2("6.3 命令行 / 无界面激活")
    b.p("无界面服务器、运维批量部署时，可以不用 Web 界面：安装同版本商业 wheel，把许可证写入约定位置，重启服务。")
    b.code(
        "# 1) 安装商业扩展包（与主程序同版本）\n"
        f"python -m pip install automind_pro-{VERSION}-py3-none-any.whl\n"
        "\n"
        "# 2) 写入许可证（三选一，优先级见 10.6）\n"
        "#    方式 a：用户级文件（推荐）\n"
        "#    Windows:  %USERPROFILE%\\.automind\\license\n"
        "#    Linux/macOS: ~/.automind/license\n"
        "#    方式 b：当前目录 .automind_license\n"
        "#    方式 c：环境变量 AUTOMIND_LICENSE（适合容器 / 密钥管理）\n"
        "\n"
        "# 3) 重启服务并确认档位\n"
        "python -m automind.server --host 127.0.0.1 --port 8765\n"
        "# GET /api/health 的 edition 应为 pro / enterprise")
    b.code(
        "# 无界面批量：POST /api/license/package（管理动作，需管理员令牌）\n"
        "curl -X POST http://127.0.0.1:8765/api/license/package \\\n"
        "  -H \"Authorization: Bearer <AUTOMIND_ADMIN_TOKEN>\" \\\n"
        "  -H \"Content-Type: application/octet-stream\" \\\n"
        "  --data-binary @AutoMind-PRO-acme-20271231.ampkg")
    b.callout("warn", "许可证优先级",
              "AUTOMIND_LICENSE（环境变量）> ./.automind_license > ~/.automind/license。"
              "若客户界面激活成功但仍显示社区版，先检查是否存在更高优先级的旧许可证（见 10.6）。")

    b.h2("6.4 离线内网激活")
    b.bullets([
        "许可证与 .ampkg 均离线校验（Ed25519 验签，纯本地），不需要外网、不需要访问许可证服务器。",
        "内网客户可直接用 U 盘把 .ampkg 拷入；激活不需要管理员权限，不需要改环境变量。",
        "离线部署的其他步骤（安装、出网策略、备份回滚）见 docs/OFFLINE_OPS.md。",
        "若客户使用内网镜像源安装 wheel，注意 wheel 必须与主程序同版本。",
    ])

    b.h2("6.5 机器绑定与换机")
    b.p("绑定版许可证只认一台机器；客户换机时不能复制许可证，必须由发行方重新签发。"
        "指纹 = hostname + MAC/UUID + 平台 + 安装盐（~/.automind/machine_salt）的 sha256 前 16 位。")
    b.steps([
        "客户在新机器上打开升级面板「本机信息」卡片，复制「本机指纹」（16 位 hex）。",
        "把指纹 + 客户标识发给发行方；发行方按 4.3 节用 --machine <新指纹> --serial <新序列号> 重签。",
        "旧机报废 / 转卖时，发行方 revoke 旧序列号（写 CRL），防止旧机继续使用。",
        "客户在新机器上拖入新的 .ampkg 完成激活；旧机器上的旧证降级 / 失效。",
        "发行方在台账登记换机日期、新旧序列号与新指纹。",
    ])
    b.figure("fig08_machine_binding.png",
             "机器绑定与换机流程：指纹 → 重签 → 激活；旧证可选吊销", note="示意图")
    b.callout("info", "重装系统也会换指纹",
              "安装盐在 ~/.automind/machine_salt；重装系统 / 清空用户目录会改变指纹，"
              "绑定客户换系统盘前应先报备，否则需要重签。")

    b.h2("6.6 激活成功验证清单")
    b.table(
        ["检查项", "怎么查", "期望结果"],
        [
            ["界面徽标", "左上角版本徽标", "「专业版 / 企业版」"],
            ["档位接口", "GET /api/health", "edition = pro / enterprise"],
            ["许可证状态", "GET /api/license 或界面许可证面板", "active；显示序列号 / 绑定 / 宽限 / 复检"],
            ["版本匹配", "GET /api/license 的 version_match", "true"],
            ["解锁功能", "点协同 / 循环 / 定时任务入口", "无需升级提示，可正常打开"],
            ["周期复检", "等待一个复检周期或重启", "状态不降级，日志无 license 错误"],
            ["离线校验", "断网后重启服务", "仍为专业版 / 企业版（离线验签）"],
        ],
        widths=[3.6, 7.4, 5.0], size=9, caption="激活成功验证清单",
    )

    b.h2("6.7 激活失败原因与处置")
    b.p("v1.11.15 起客户端会给出具体原因码（reason），不再笼统“许可证无效”。"
        "下表按客户常见现象给出处置；完整原因码表见第 10.2 节与图 10。")
    b.table(
        ["现象", "原因码", "客户侧处理", "发行方处理"],
        [
            ["一直是社区版、无报错", "empty / public_key_not_provisioned",
             f"拖入 .ampkg 或放置许可证；确认装的是 v{VERSION} 同版本 wheel", "pubkey + doctor；重出同版本包"],
            ["「许可证内容格式不合法」", "payload_invalid", "复制完整一整行（AMP2- 开头）", "verify 复核"],
            ["「签名校验未通过」", "signature_invalid", "许可证被改过或来源不对", "verify 复核；重发正确许可证"],
            ["「需要升级扩展包」", "key_id_unknown", f"升级主程序 / 扩展包到 v{VERSION}", "确认 LEGACY 公钥列表，重出包"],
            ["「许可证已过期」", "expired / grace_expired", "拖入续期 .ampkg", "签发续期证"],
            ["仍可用、提示宽限剩余 N 天", "grace", "尽快完成续费（宽限默认 0 天）", "到期前 30 天提醒续费"],
            ["「尚未到生效日」", "not_yet_active", "核对本机日期 / 时区；旧证继续用", "核对 --start 生效日"],
            ["「绑定机器与本机不符」", "machine_mismatch", "确认是否为绑定版；换机需重签", "用新指纹重签并记录换机"],
            ["「许可证已被吊销」", "revoked", "联系发行方确认", "查 CRL；恢复需重签新序列号"],
            ["「检测到系统时钟回拨」", "clock_rollback", "校时（NTP）后等复检或重启", "无需处理；必要时远程协助"],
            ["旧 AMP- 格式许可证", "legacy_hmac_disabled", "换发 AMP2 新证", "签发 AMP2 证"],
            ["拖入后提示需重启", "（正常）", "重启一次", "无需处理"],
        ],
        widths=[3.6, 4.4, 4.4, 3.6], size=8.2, caption="激活失败原因与处置（详细见第 10 章）",
    )
    b.page_break()


# ── 第 7 章 ──────────────────────────────────────────────────────
def _ch7(b: ManualBuilder):
    b.h1("第 7 章  版本升级")

    b.h2("7.1 版本一致性纪律")
    b.p("AutoMind 的版本一致性是硬约束：社区主程序、商业扩展包、前端构建产物、使用手册必须同号；"
        "tests/test_version_consistency.py 会在发版前拦住不一致。对商业运营而言，最常踩的坑是："
        "客户主程序升级了，但扩展包 / .ampkg 还是旧版本。")
    b.table(
        ["组件", "版本来源", "不一致的后果"],
        [
            ["社区主程序", "automind/__init__.py", "——（基准）"],
            ["商业扩展包", "pro/automind_pro/__init__.py + pyproject", ".ampkg 被拒 / 激活失败 / 特性不出现"],
            ["扩展协议", "EXTENSION_API_VERSION", "“扩展包协议版本不匹配”，无法加载"],
            ["前端构建产物", "automind/static/dist", "界面与后端能力不匹配（功能入口缺失）"],
            ["使用手册 / 文档", "版本号头部与 changelog", "客户按旧步骤操作（体验问题，不阻塞激活）"],
        ],
        widths=[3.8, 6.2, 6.0], size=9, caption="版本一致性约束",
    )
    b.callout("warn", "v1.11.17 发布纪律",
              f"核心与商业包必须同为 {VERSION}：v1.11.17 的修复在社区核心（商业特性路由挂载），"
              f"商业包仍按同号发布纪律重建 pro wheel（pro/dist/automind_pro-{VERSION}-*.whl）并重新出 .ampkg。"
              "旧 1.11.15 .ampkg 中的扩展包「不支持续跑参数」，客户升级到 v1.11.16 起必须换发新版 .ampkg；"
              "只升主程序不换商业包会出现续跑不可用 / 版本不一致提示。")
    b.callout("warn", "升级顺序",
              "同一窗口内：先升级社区主程序 → 再用同版本 .ampkg 升级扩展包并激活 → 最后验证 "
              "/api/health 的 edition 与 version。反过来会出现扩展包版本不匹配的报错。")

    b.h2("7.2 升级前检查")
    b.table(
        ["检查项", "动作", "通过标准"],
        [
            ["版本规划", "确认目标版本号与 CHANGELOG", "主程序 / 扩展包 / 手册三处同号"],
            ["Pro 扩展包重建", "本轮 Pro 有协同/循环续跑变更：重建 pro wheel 并出配套 .ampkg", "wheel 版本 = 1.11.17"],
            ["数据备份", "备份数据目录与配置（scripts/offline/backup.ps1 或手工）", "备份可恢复演练通过"],
            ["许可证状态", "GET /api/license 确认当前证有效", "active；到期日足够覆盖升级窗口"],
            ["离线包准备", "准备好新版本 wheel / .ampkg / 安装包", "sha256 已核对"],
            ["冒烟环境", "先在测试机 / 测试租户升级", "激活成功、续跑入口可用"],
            ["回滚方案", "保留旧版本安装包与数据备份", "回滚步骤已确认（见 7.6）"],
        ],
        widths=[3.4, 7.2, 5.4], size=8.8, caption="升级前检查清单",
    )

    b.h2("7.3 升级步骤")
    b.steps([
        "发行方：重建 pro wheel（automind_pro-1.11.17）并重新 bundle 出 .ampkg（含随包 CRL）。",
        "停止服务（Windows 桌面版关闭窗口即可；服务端停掉监听进程）。",
        "备份数据目录与配置（必须；升级失败可回滚）。",
        "升级社区主程序：pip 安装新版本 / 运行新安装包 / 覆盖源码后重装依赖。",
        "使用同版本 .ampkg 升级商业扩展包并激活（拖入升级面板，见第 6 章）；旧 1.11.15 .ampkg 不可复用。",
        "启动服务，按 6.6 节验证清单确认激活状态与功能入口；抽验续跑入口（预算耗尽后可续跑）是否可用。",
        "记录升级日志：版本、时间、操作人、结果、遇到的异常。",
    ])
    b.code(
        "# 源码 / 命令行升级社区版\n"
        f"python -m pip install -U \"automind-agent[web]=={VERSION}\"\n"
        "python -m automind.server --port 8765\n"
        "\n"
        "# 离线环境：在联网机准备 wheel 目录，拷入内网后\n"
        f"python -m pip install --no-index --find-links .\\offline-wheels \"automind-agent[web]=={VERSION}\"\n"
        "\n"
        "# 商业扩展包升级（拖入 .ampkg 后如提示重启）\n"
        "#   发行方先重建 wheel + .ampkg（见第 5 章），客户再拖入新包\n"
        "#   Windows：重启 AutoMind.exe / 服务进程\n"
        "#   Linux：systemctl restart automind（或按部署方式重启）")

    b.h2("7.4 热激活与重启")
    b.table(
        ["场景", "是否需要重启", "原因 / 说明"],
        [
            ["仅许可证变化（同版本扩展包已安装）", "通常不需要", "验签 + 就地激活，特性路由热挂载"],
            ["扩展包 wheel 版本变化", "需要一次", "Python 模块不可热替换；许可证已提前保存"],
            ["主程序版本变化", "需要", "进程重启后加载新核心"],
            ["首次安装扩展包（.ampkg）", "通常不需要；提示则重启", "解压到 extensions/ 并加入加载路径"],
            ["续跑相关 Pro 变更（v1.11.16）", "需要（换新 .ampkg + 重启）", "旧 1.11.15 扩展不接受续跑参数"],
            ["CRL 更新 / 吊销生效", "默认按复检周期生效；重启可立即生效", "复检默认 3600 秒；见 8.5"],
        ],
        widths=[6.0, 4.2, 5.8], size=8.8, caption="热激活 vs 重启",
    )

    b.h2("7.5 升级决策树")
    b.figure("fig04_upgrade_decision_tree.png",
             "版本升级决策树：拖包 / 重启 / 让发行方重出包", note="示意图")

    b.h2("7.6 回滚")
    b.steps([
        "停止服务；备份当前状态（即使要回滚，也先备份现状，避免二次损失）。",
        "回滚社区主程序：安装旧版本 wheel / 运行旧安装包；源码场景切回旧 commit 并重装。",
        "回滚商业扩展包：安装旧版本 wheel，或拖入旧版本 .ampkg（需仍持有有效旧证）。",
        "恢复数据（仅在升级导致数据异常时）：用升级前备份按 docs/OFFLINE_OPS.md 的 rollback 流程恢复。",
        "启动服务并验证版本 / 档位 / 关键功能；记录回滚原因。",
    ])
    b.callout("warn", "回滚不是“撤销激活”",
              "回滚程序版本不会自动回滚许可证；若旧版本不支持新许可证字段（如 v3 的机器绑定），"
              "需要同时换回旧证或让发行方重签兼容证。")

    b.h2("7.7 升级失败处置")
    b.table(
        ["现象", "可能原因", "处置"],
        [
            ["升级后仍是社区版", "扩展包未升级 / 旧证优先级更高", "按 6.7 检查；确认 wheel 版本与许可证"],
            ["提示“扩展包协议版本不匹配”", "扩展包与主程序 EXTENSION_API_VERSION 不一致", "用同版本 .ampkg 重装"],
            ["提示“版本不一致”", "wheel / 主程序版本不同号", "让发行方出同号包（第 5 章）"],
            ["激活后功能入口仍带锁", "前端构建产物未更新 / 特性开关未生效", "重启、强刷前端；确认 dist 已更新"],
            ["服务起不来", "依赖缺失 / 数据损坏", "回滚（7.6）；按日志与 doctor 定位"],
            ["续跑按钮不出现 / 提示不可续跑", "Pro 扩展包仍是 1.11.15（不认续跑参数）", "换发 v1.11.16 .ampkg 后重启"],
            ["许可证丢失", "数据目录被覆盖 / 未备份", "从台账取回许可证原文，重新粘贴或拖入 .ampkg"],
        ],
        widths=[4.6, 6.0, 5.4], size=8.8, caption="升级失败处置",
    )
    b.page_break()


# ── 第 8 章 ──────────────────────────────────────────────────────
def _ch8(b: ManualBuilder):
    b.h1("第 8 章  续期、升档、换机与吊销")

    b.h2("8.1 续期")
    b.steps([
        "到期前 30~90 天与客户确认续费（见第 9.4 节提醒节奏）。",
        "确认续费周期与档位不变，按新到期日重新 bundle 出包（许可证与 wheel 打包在一个 .ampkg）。",
        "客户拖入新 .ampkg：许可证覆盖、特性保持，数据与配置不动。",
        "台账更新到期日、许可证原文、sha256、发出日期。",
    ])
    b.code(
        "# 续期（同档位、新到期日；建议沿用原 serial）\n"
        "python pro/tools/issue_license.py bundle PRO 20281231 acme \\\n"
        "    --serial ACME20260001 --machine <客户指纹> --out dist/upgrades\n"
        "\n"
        "# 提前续约：新证 2027-01-01 才生效，旧证到那天自动接上\n"
        "python pro/tools/issue_license.py bundle PRO 20281231 acme \\\n"
        "    --serial ACME20260001 --start 20270101 --out dist/upgrades\n"
        "\n"
        "# 批量续期：把新到期日 / days 写进 CSV 后重跑 issue-batch\n"
        "python pro/tools/issue_license.py issue-batch --csv renewals.csv \\\n"
        "    --out dist/licenses --bundles")
    b.callout("tip", "续期不影响业务",
              "拖入续期包时，已在运行的商业特性实例保留；不需要停机、不需要重新配置。"
              "旧证与新证可以并存（--start 未到前旧证继续有效）。")

    b.h2("8.2 升档（专业版 → 企业版）")
    b.bullets([
        "确认合同升档信息：档位 ENT、到期日、席位、是否绑定机器。",
        "重新 bundle（tier=ENT），序列号建议新开一个（便于台账区分档位变更）。",
        "客户拖入新包：企业版专属特性解锁；原有专业版配置与数据保留。",
        "台账记录升档日期、旧证到期日、新证信息；旧证可保留到到期（不必吊销）。",
    ])

    b.h2("8.3 换机")
    b.p("机器绑定客户换机时必须重签；非绑定客户换机只需搬迁数据与许可证文件（但要注意数据目录与机器指纹无关）。")
    b.steps([
        "新机安装同版本主程序与扩展包；或直接使用同一数据目录迁移。",
        "绑定客户：新机许可证面板复制指纹 → 发行方按 4.3 节重签 → 客户拖入新 .ampkg。",
        "可选：吊销旧机序列号（revoke 写入 CRL），防止旧机继续使用。",
        "台账登记换机日期、新旧序列号、新指纹（前 8 位 + 设备描述）。",
    ])
    b.code(
        "# 换机重签（绑定新指纹；新序列号便于区分）\n"
        "python pro/tools/issue_license.py bundle ENT acme --days 365 --seats 10 \\\n"
        "    --serial ACME20260008 --machine 0f1e2d3c4b5a6978 --out dist/upgrades\n"
        "\n"
        "# 旧机报废 / 转卖：吊销旧证（写入 CRL）\n"
        "python pro/tools/issue_license.py revoke ACME20260007 \\\n"
        "    --reason \"device replaced\" --crl dist/crl/license_crl.json")

    b.h2("8.4 吊销 CRL")
    b.p("吊销用于合同终止、设备丢失、许可证外泄、换机后旧机处置等场景。v1.11.17 的 CRL 仍是 JSON 文件"
        "（v1.11.15 起引入，格式未变），"
        "记录已吊销序列号与原因；客户端在激活与周期复检时校验，命中即降级社区版并记录原因。"
        "CRL 默认路径 dist/crl/license_crl.json，写入为原子追加 + 按 serial 去重（重复吊销保留首次 reason/time）。")
    b.code(
        "# 按序列号吊销（推荐，台账里有；v2 证用派生序列号 sha256(license)[:12]）\n"
        "python pro/tools/issue_license.py revoke ACME20260007 \\\n"
        "    --reason \"refund\" --crl dist/crl/license_crl.json\n"
        "\n"
        "# 按许可证原文吊销\n"
        "python pro/tools/issue_license.py revoke \"AMP2-e9fffafc0f6f19d4-...\" --reason \"contract terminated\"\n"
        "\n"
        "# 查看吊销清单（list 摘要 / show 原文）\n"
        "python pro/tools/issue_license.py crl list\n"
        "python pro/tools/issue_license.py crl show --crl dist/crl/license_crl.json")
    b.table(
        ["字段", "含义", "示例"],
        [
            ["serial", "被吊销的序列号（v3 的 id 或 v2 派生序列号）", "ACME20260007"],
            ["reason", "吊销原因（自由文本，进日志与审计）", "refund / device replaced / leaked"],
            ["time", "吊销时间（ISO 8601）", "2026-10-22T10:30:00"],
            ["revoked", "CRL 顶层数组", "{\"revoked\": [{...}, {...}]}"],
        ],
        widths=[3.0, 7.6, 5.4], size=9, caption="CRL 字段",
    )
    b.callout("warn", "吊销是离线的",
              "签了吊销但没把 CRL 送到客户机，旧证仍可用 —— 吊销必须伴随分发（随 .ampkg 或单独发 CRL）。"
              "客户端默认每 3600 秒复检一次（AUTOMIND_LICENSE_RECHECK），最迟一个复检周期内生效；"
              "急迫场景随新 .ampkg 携带 CRL 或让客户重启。")

    b.h2("8.5 CRL 分发与生效")
    b.table(
        ["分发方式", "适用", "客户侧位置 / 行为"],
        [
            ["随 .ampkg 携带（默认自动）", "续期 / 换机 / 批量升级时顺带下发", "激活时先把 CRL 合并落盘，再进行验签与激活"],
            ["单独发 CRL 文件", "只吊销、不换证", "放到 ~/.automind/license_crl.json 或 AUTOMIND_LICENSE_CRL 指向"],
            ["运维批量下发", "多台机器 / 内网共享目录", "配置管理统一下发 + 等复检（或重启立即生效）"],
            ["紧急吊销", "许可证外泄 / 设备丢失", "随新包下发 + 通知客户重启 / 等待复检"],
        ],
        widths=[4.6, 5.6, 5.8], size=8.8, caption="CRL 分发方式",
    )
    b.p("CRL 文件结构示例（客户端读取顺序：AUTOMIND_LICENSE_CRL → ~/.automind/license_crl.json）：")
    b.code(
        "{\n"
        "  \"revoked\": [\n"
        "    {\"serial\": \"ACME20260007\", \"reason\": \"refund\", \"time\": \"2026-10-22T10:30:00\"},\n"
        "    {\"serial\": \"0123456789ab\"}\n"
        "  ]\n"
        "}")
    b.callout("info", "不要靠“再签一份新证”绕过 CRL",
              "恢复被吊销客户的标准做法是：从 CRL 删除该条（人工编辑，保留 JSON 结构）→ 重新分发 → 台账记录；"
              "否则台账会留下两条同客户记录，审计无法解释。工具不提供 un-revoke 命令。")

    b.h2("8.6 到期、宽限与降级行为")
    b.p("许可证到期后的行为取决于客户机宽限配置（AUTOMIND_LICENSE_GRACE_DAYS，默认 0）：")
    b.table(
        ["阶段", "客户端行为", "客户可见现象", "运营动作"],
        [
            ["有效期内", "全部商业特性可用", "正常", "按到期日安排续费"],
            ["到期 + 宽限期内", "仍可用，界面标红提示", "“宽限期剩余 N 天”", "催续费；宽限默认 0，建议年度客户 7~15 天"],
            ["宽限期结束 / 无宽限", "降级社区版", "商业入口显示 🔒，服务照常启动、数据不丢", "拖入续期包立即恢复（复检周期内自动恢复）"],
            ["被吊销", "降级社区版并记录原因", "许可证面板显示“已被吊销”", "确认吊销是否误操作；恢复需清理 CRL 并重签"],
            ["机器不符", "降级社区版", "许可证面板显示“绑定机器与本机不符”", "换机重签（8.3）"],
            ["时钟回拨", "降级社区版", "许可证面板显示“检测到系统时钟回拨”", "校时后等复检（≤1 小时）或重启（10.7）"],
            ["续期 / 修正后", "复检自动重新激活", "徽标恢复专业版 / 企业版", "无需重启；急用可重启"],
        ],
        widths=[3.2, 5.2, 4.8, 2.8], size=8.4, caption="到期 / 宽限 / 降级行为",
    )
    b.callout("info", "永久许可证",
              "expiry=00000000 的永久证不会到期，但仍可能因吊销、机器不符、时钟异常而降级；"
              "台账中永久证应设置“年度复核日”，确认客户信息与使用状态。")
    b.page_break()


# ── 第 9 章 ──────────────────────────────────────────────────────
def _ch9(b: ManualBuilder):
    b.h1("第 9 章  商业运营台账与续费提醒")

    b.h2("9.1 台账字段规范")
    b.p("台账是商业运营的唯一事实来源：发证、续费、换机、吊销、审计都以台账为准。"
        "issue-batch 默认生成 <out>/license_ledger.csv（12 列，追加写）；商业运营再补充合同 / 联系人 / "
        "续费跟进字段，建议另建一张运营表按 customer + serial 关联。")
    b.table(
        ["工具生成列（12 列）", "含义", "示例"],
        [
            ["customer", "客户标识（与许可证 c 字段一致）", "acme"],
            ["tier", "PRO / ENT", "ENT"],
            ["expiry", "到期日 YYYYMMDD；00000000 = 永久", "20271231"],
            ["issued", "签发日", "20261022"],
            ["serial", "v3 序列号；v2 为派生序列号 sha256(license)[:12]", "ACME20260001"],
            ["machine", "绑定的机器指纹（空 = 未绑定）", "0123456789abcdef"],
            ["seats", "席位（0 = 不限）", "50"],
            ["license", "许可证原文（机密：持有即可激活）", "AMP2-e9ff…"],
            ["wheel", "交付的扩展包 / 升级包文件名", "AutoMind-ENT-bigcorp-20271231.ampkg"],
            ["sha256", "交付物摘要：有 .ampkg 取包哈希，否则取许可证文本哈希", "7c28…"],
            ["notes", "备注（只进台账，不进许可证）", "续费 3 年 / 渠道：华东"],
            ["created_at", "记录写入时间", "2026-10-22T10:30:00"],
        ],
        widths=[4.2, 8.4, 3.4], size=8.4, caption="issue-batch 台账 12 列（工具生成）",
    )
    b.table(
        ["运营补充字段", "必填", "说明", "示例"],
        [
            ["客户名称 / 合同主体", "✅", "与合同一致的全称", "某某科技有限公司"],
            ["联系人 / 联系方式", "✅", "商务 / IT 对接人", "张三 / zhangsan@example.com"],
            ["客户签收", "建议", "签收人 / 时间 / 工单号", "IT 李四 / INC-00123"],
            ["发出日期 / 渠道", "✅", "谁在何时通过什么渠道发出", "2026-10-22 / 企业邮箱"],
            ["续费提醒日", "✅", "默认到期前 90 天", "2027-10-02"],
            ["续费负责人 / 状态", "✅", "跟进人 + 阶段（待联系 / 报价 / 已付款）", "王五 / 已报价"],
            ["变更记录", "否", "换机 / 升档 / 吊销等变更（追加，不覆盖）", "2027-03-01 换机一次"],
        ],
        widths=[3.8, 1.4, 6.6, 4.2], size=8.4, caption="运营补充字段（可另建关联表）",
    )

    b.h2("9.2 台账维护流程")
    b.steps([
        "签发后 24 小时内登记：客户、序列号、档位、到期、席位、许可证原文、sha256、发出渠道。",
        "客户确认激活后补充：激活日期、激活方式（.ampkg / 粘贴 / 命令行）、版本号、验证结果。",
        "续期 / 升档 / 换机 / 吊销等变更，当日更新对应字段并保留历史（不要覆盖旧值，追加变更记录）。",
        "每周对账：台账与 dist/licenses 产物、财务回款、客户状态三方核对。",
        "每月归档：导出月度台账快照，交商业负责人确认。",
    ])
    b.table(
        ["时间点", "动作", "责任人", "产出"],
        [
            ["签发当天", "登记新证 / 变更", "发证运营", "更新后的台账"],
            ["激活确认后", "补充激活信息", "客服", "激活确认记录"],
            ["每周五", "台账与产物对账", "商业运营", "对账差异清单"],
            ["每月最后一个工作日", "月度巡检 + 快照归档", "商业运营负责人", "月度运营报告"],
            ["每季度", "合规检查（第 11.6 节）", "安全 / 商业负责人", "合规检查表"],
        ],
        widths=[3.8, 6.2, 3.2, 2.8], size=8.8, caption="台账维护节奏",
    )

    b.h2("9.3 月度巡检清单")
    b.table(
        ["检查项", "判据", "异常处理"],
        [
            ["即将到期客户", "90 / 60 / 30 天内到期的客户清单", "按 9.4 提醒节奏跟进"],
            ["已到期未续费", "到期日 < 今天且无新证", "确认降级状态；跟进续费或回收"],
            ["永久证复核", "永久证客户年度复核", "确认客户仍在用、信息未变"],
            ["宽限期使用", "宽限期内客户清单", "优先催续费；评估是否调整宽限策略"],
            ["CRL 清单", "本月新增吊销记录", "核对原因与客户通知记录"],
            ["台账完整性", "必填字段缺失率 < 2%", "补录；检查签发流程"],
            ["sha256 核对", "交付文件与台账记录一致", "不一致立即排查传输 / 篡改"],
            ["复检异常", "客户反馈“降级 / 被吊销”", "按第 10 章排障，必要时重签"],
            ["密钥状态", "key_id 与当前主钥一致；LEGACY 到期提醒", "按 3.6 规划换钥"],
        ],
        widths=[3.4, 6.4, 6.2], size=8.8, caption="月度巡检清单",
    )

    b.h2("9.4 续费提醒节奏与话术")
    b.table(
        ["时间点", "动作", "渠道"],
        [
            ["T-90 天", "首轮续费提醒（附当前证信息与报价）", "客户经理 / 邮件"],
            ["T-60 天", "二轮提醒（确认预算与到期影响）", "电话 / IM"],
            ["T-30 天", "三轮提醒 + 准备续期包（同档位新到期日）", "邮件 + IM"],
            ["T-7 天", "最后提醒（说明到期降级影响）", "电话 / IM"],
            ["T+0", "到期日：确认是否宽限；未续费客户讲解降级范围", "客服主动联系"],
            ["T+7", "降级回访 + 续费方案", "客户经理"],
        ],
        widths=[2.6, 10.0, 3.4], size=9, caption="续费提醒节奏（T = 到期日）",
    )
    b.callout("tip", "催续费的三句话",
              "①“您的专业版将在 X 天后到期，到期后协同 / 循环 / 定时任务等入口会锁定，但数据不会丢。”"
              "②“续期只需拖入一个升级包，1 分钟完成，不需要停机。”"
              "③“需要我先把续期包和操作步骤发您测试环境吗？”")

    b.h2("9.5 客服话术模板")
    b.table(
        ["场景", "话术要点"],
        [
            ["首次激活", "说明 .ampkg 离线、免 pip；发操作步骤截图；提醒拖入后看徽标；如提示重启则重启一次。"],
            ["激活失败", "先要原因码 / 截图；对照第 10 章初步判断；涉及发行侧重签时说明处理时限。"],
            ["续费", "提前 90 天联系；说明到期影响与续期步骤；续期包与 sha256 一并发送。"],
            ["换机", "请客户提供新机机器指纹；说明旧证将失效 / 可选吊销；重签后发新包。"],
            ["被吊销 / 降级", "先确认是否误操作；如确需恢复，走重签新序列号流程；保留客户沟通记录。"],
            ["升级版本", "先确认主程序与扩展包同版本；按第 7 章顺序升级；升级前提醒备份。"],
        ],
        widths=[3.0, 13.0], size=9, caption="客服话术模板（可按实际情况调整）",
    )

    b.h2("9.6 数据保护")
    b.bullets([
        "台账包含客户标识、合同信息与许可证原文，按商业机密管理；外发时脱敏（去掉许可证中间段与联系方式）。",
        "台账只保存在受控目录 / 企业网盘，权限按角色分配；禁止个人网盘 / 微信文件传输。",
        "许可证原文本身可被公开验证（不含私钥），但仍不应公开发布。",
        "导出给第三方的报表只保留统计维度（客户数 / 到期分布 / 档位分布），不含许可证与联系方式。",
        "定期备份台账（至少每周一次），保留历史版本以便审计。",
    ])
    b.page_break()


# ── 第 10 章 ─────────────────────────────────────────────────────
def _ch10(b: ManualBuilder):
    b.h1("第 10 章  排障手册")

    b.h2("10.1 排障总原则")
    b.bullets([
        "先看原因码：v1.11.17 客户端会给出具体原因（empty / signature_invalid / key_id_unknown / payload_invalid / "
        "expired / grace / grace_expired / not_yet_active / machine_mismatch / revoked / clock_rollback / "
        "public_key_not_provisioned / legacy_hmac_disabled）。",
        "先发行侧后客户侧：发行方 verify + doctor 确认证与包没问题，再查客户机。",
        "先文件后界面：检查许可证文件位置与优先级（10.6），再怀疑界面缓存。",
        "先版本后功能：确认主程序 / 扩展包 / 前端同版本，再看特性是否解锁。",
        "留证据：客户截图、原因码、/api/license 输出、日志片段、许可证尾部 4 位（不要发完整证给无关人员）。",
    ])
    b.figure("fig10_error_triage.png",
             "激活失败原因码速查：现象 → 原因码 → 第一步处置", note="示意图")

    b.h2("10.2 原因码速查")
    b.table(
        ["原因码", "含义", "发行方处置", "客户话术"],
        [
            ["ok", "许可证有效", "无需处理", "许可证有效"],
            ["empty", "未配置任何许可证", "确认已发证；核对客户是否放对位置", "请拖入 .ampkg 或按步骤放置许可证"],
            ["signature_invalid", "签名校验失败（内容被改动 / 非本发行方签发）", "verify 复核；确认密钥与客户包版本",
             "许可证可能不完整，请重新复制完整一行或使用新包"],
            ["key_id_unknown", "KID 不在当前公钥 / LEGACY 列表中（轮换过渡）", "确认 LEGACY 公钥列表；重出同版本包",
             "需要升级扩展包 / 由发行方换发新证"],
            ["payload_invalid", "payload 格式非法", "verify 复核；重新签发", "许可证内容格式不合法，请重新复制"],
            ["expired", "已过期（无宽限或宽限已结束）", "签发续期证", "请拖入续期 .ampkg"],
            ["grace", "已过期但处于宽限期", "无需处理（提醒续费）", "宽限期剩余 N 天，请尽快续费"],
            ["grace_expired", "超出宽限期", "签发续期证；评估宽限策略", "已过宽限期，续期包拖入即恢复"],
            ["not_yet_active", "未到生效日（--start / n 字段）", "核对生效日与客户本地日期", "该证 YYYYMMDD 生效；旧证继续用到之前"],
            ["machine_mismatch", "机器指纹与许可证绑定值不一致", "换机重签 / 核对绑定证是否发错", "换机需重新签发；请提供新机指纹"],
            ["revoked", "序列号在 CRL 中被吊销", "查 CRL 原因；确认是否误吊销", "该证已被吊销，请联系商业运营确认"],
            ["clock_rollback", "检测到系统时间回拨 > 1 天", "无需处理（客户端自保护）", "请校时（NTP）后等复检或重启"],
            ["public_key_not_provisioned", "内置公钥未置备 / 安装包不完整", "keygen --write-public-key + 重建 wheel",
             f"安装包不完整，请重新获取 v{VERSION} 同版本包"],
            ["legacy_hmac_disabled", "旧 v1（AMP-）HMAC 许可证已停用", "换发 AMP2 新证", "旧格式已停用，请换发新证"],
        ],
        widths=[3.4, 5.4, 4.4, 4.8], size=7.6, caption="原因码速查（v1.11.17，原因码与 v1.11.15 相同）",
    )

    b.h2("10.3 doctor 输出解读")
    b.code(
        "# 发行前自检（推荐每次出包前运行；可选指定 CRL / 升级包目录）\n"
        "python pro/tools/issue_license.py doctor \\\n"
        "    --upgrades-dir dist/upgrades --crl dist/crl/license_crl.json\n"
        "\n"
        "# 输出结构：\n"
        "# { ok, version, core_version, keys_file, public_key_hex, key_id, wheel,\n"
        "#   bundles_dir, checks: [{name, ok, detail}], warnings: [{name, ok, detail}] }\n"
        "\n"
        "# 只看失败项（示例）\n"
        "python pro/tools/issue_license.py doctor | python -c \"import json,sys; "
        "d=json.load(sys.stdin); print([c for c in d['checks'] if not c['ok']])\"")
    b.table(
        ["输出项", "含义", "异常时"],
        [
            ["ok", "全部 12 项检查通过 = true", "false 时退出码 1，不能出包"],
            ["key_id", "当前主验签公钥指纹", "与台账 / 已发证记录不一致时检查换钥记录"],
            ["wheel", "用于出包的 wheel 文件名", "版本必须等于主程序版本"],
            ["checks[].ok", "单项检查结果（12 项，见 3.5 节）", "按 3.5 节表格定位修复"],
            ["warnings", "非致命但需关注（旧 wheel / 旧钥到期）", "清理旧 wheel / 规划换钥"],
        ],
        widths=[3.4, 7.0, 5.6], size=8.8, caption="doctor 输出解读",
    )

    b.h2("10.4 客户侧诊断")
    b.code(
        "# 客户侧：一次性打印许可证与档位诊断（不包含私钥）\n"
        "python -c \"import json; from automind_pro.licensing import license_status; "
        "print(json.dumps(license_status(), ensure_ascii=False, indent=2))\"\n"
        "\n"
        "# 查看当前档位 / 特性\n"
        "#   GET /api/health  → edition / version\n"
        "#   GET /api/license → step / reason_text / version_match / serial / machine / recheck\n"
        "\n"
        "# 查看许可证文件位置（见 10.6）\n"
        "#   Windows:  %USERPROFILE%\\.automind\\license\n"
        "#   Linux/macOS: ~/.automind/license")
    b.table(
        ["诊断对象", "命令 / 位置", "关注字段"],
        [
            ["档位", "GET /api/health", "edition、version"],
            ["许可证状态", "GET /api/license", "step、reason_text、reason_code、version_match"],
            ["v3 字段", "GET /api/license", "serial、machine、not_before、grace、grace_left"],
            ["复检 / 时钟", "GET /api/license", "recheck_seconds、last_check、clock_ok、license_state"],
            ["机器指纹", "升级面板「本机信息」或 guard.machine_id()", "16 位 hex（与绑定值比对）"],
            ["许可证文件", "见 10.6", "是否存在、内容是否完整"],
            ["CRL", "AUTOMIND_LICENSE_CRL 或 ~/.automind/license_crl.json", "revoked 数组、crl_revoked_count"],
            ["复检配置", "AUTOMIND_LICENSE_RECHECK / GRACE_DAYS", "数值是否合法（0 表示关闭复检）"],
        ],
        widths=[3.4, 7.2, 5.4], size=8.6, caption="客户侧诊断入口",
    )

    b.h2("10.5 批量签发失败处置")
    b.table(
        ["失败类型", "表现", "处置"],
        [
            ["单个客户行失败", "JSON 摘要 counts.failed 与逐行原因（tier 非法 / expiry 与 days 冲突 / seats 非整数）",
             "按 4.4 列定义修正该行后只补发失败行"],
            ["整批参数错误", "--csv 文件不存在 / 表头缺失 / --out 不可写", "修正路径与权限后重跑"],
            ["wheel 缺失 / 版本不一致", "--bundles 时构建或校验失败", "在 pro/ 目录构建同版本 wheel，用 --wheel 指定"],
            ["doctor 失败", "发行前自检不通过", "按 doctor 输出修复后再出包"],
            ["输出目录混用", "不同批次写同一目录导致文件覆盖", "每批次独立 <out> 目录；台账追加写"],
            ["重复签发", "同一客户出现两张新证（台账重复 serial）", "作废其中一张（revoke），保留唯一记录"],
        ],
        widths=[3.6, 6.6, 5.8], size=8.6, caption="批量签发失败处置",
    )

    b.h2("10.6 文件位置与优先级")
    b.table(
        ["对象", "默认位置", "优先级 / 说明"],
        [
            ["许可证", "~/.automind/license", "低（界面激活写入这里）"],
            ["许可证", "./.automind_license", "中"],
            ["许可证", "环境变量 AUTOMIND_LICENSE", "高（覆盖文件）"],
            ["CRL", "~/.automind/license_crl.json", "低"],
            ["CRL", "环境变量 AUTOMIND_LICENSE_CRL", "高（覆盖文件）"],
            ["复检状态", "~/.automind/license_state.json", "记录 last_seen / machine / serial（勿手工改）"],
            ["机器盐", "~/.automind/machine_salt", "机器指纹来源（重装/清目录会变）"],
            ["商业扩展包", "数据目录 extensions/", "激活时写入"],
            [".ampkg", "客户自选（临时目录即可）", "激活后可删除，建议留档"],
        ],
        widths=[3.8, 7.6, 4.6], size=8.6, caption="客户侧文件位置与优先级",
    )
    b.callout("warn", "“界面激活成功但仍是社区版”",
              "最常见原因是存在更高优先级的旧许可证（环境变量或当前目录 .automind_license）。"
              "让客户清掉旧文件 / 环境变量后重启；或把新证写到最高优先级位置。")

    b.h2("10.7 时钟、时区与宽限")
    b.bullets([
        "许可证到期日按客户本地日期判断（YYYYMMDD）；跨时区部署建议统一使用客户所在时区并开启 NTP。",
        "时钟回拨检测：系统时间比上次记录回拨超过 86400 秒（1 天）→ 判 clock_rollback 并降级（防止“改时间续命”）。",
        "注意：把时钟向前调不会触发回拨检测，但会让许可证“提前到期”——这是离线许可证的固有代价；改回正确时间即可恢复。",
        "解决：校时（Windows 时间同步 / Linux chrony）→ 下一次复检（≤1 小时）自动恢复；急用可重启。",
        "宽限期：AUTOMIND_LICENSE_GRACE_DAYS（默认 0；只对到期生效，不豁免吊销 / 机器不符 / 时钟回拨）。",
        "复检：AUTOMIND_LICENSE_RECHECK（默认 3600 秒，0 = 关闭）；关闭会让吊销 / 到期不再自动降级，不建议。",
    ])
    b.code(
        "# 客户侧：查看机器指纹与许可证状态（不打印私钥）\n"
        "python -c \"from automind_pro.licensing import machine_id, license_status; \"\n"
        "        \"print('machine_id =', machine_id()); \"\n"
        "        \"print(license_status())\"\n"
        "\n"
        "# 校时（Windows）\n"
        "w32tm /resync\n"
        "# 校时（Linux）\n"
        "sudo chronyc makestep")

    b.h2("10.8 升级与升级包问题")
    b.table(
        ["现象", "排查", "处置"],
        [
            [".ampkg 无法拖入 / 提示格式错误", "确认扩展名与文件完整性（sha256）", "重新发送；勿改后缀"],
            ["提示清单 format 不支持", "包由更新版本工具生成", "升级主程序到同版本"],
            ["提示 wheel 版本不一致", "manifest.package_version ≠ 主程序", "让发行方重出同号包"],
            ["提示许可证用内置公钥验不过", "wheel 内置公钥与签发钥不匹配", "重出包（3.4 / 5.5）"],
            ["拖入成功但需重启", "进程已加载旧扩展", "重启一次"],
            ["升级后界面没变化", "前端 dist 未更新 / 缓存", "强刷；确认安装了新版前端产物"],
        ],
        widths=[5.0, 6.4, 4.6], size=8.8, caption="升级包常见问题",
    )

    b.h2("10.9 工单升级路径")
    b.steps([
        "客服先收集：客户标识、序列号（或证尾 4 位）、原因码截图、/api/license 输出、版本号、操作系统、时间。",
        "一线可解决的：文件位置 / 优先级、重启、校时、重新拖入 .ampkg、换机重签。",
        "需要发行方介入的：重签 / 吊销 / 换钥 / 版本不一致 / 批量失败——升级到商业运营工单并附证据。",
        "涉及安全事件的（私钥泄露、许可证伪造）：立即按第 11.5 节上报，不走普通工单。",
        "闭环后更新台账备注，并把新问题补充到本手册 FAQ（12.6）。",
    ])
    b.page_break()


# ── 第 11 章 ─────────────────────────────────────────────────────
def _ch11(b: ManualBuilder):
    b.h1("第 11 章  安全与合规")

    b.h2("11.1 密钥安全")
    b.table(
        ["对象", "安全要求", "检查方式"],
        [
            ["签发私钥", "离线介质 / 密码库；双人管控；领用登记；绝不入 git / wheel / 日志", "doctor 无私钥输出；CI 审计；定期扫描"],
            ["验签公钥", "可公开；随 wheel 分发", "doctor 的 wheel_public_key_matches"],
            ["LEGACY 公钥", "仅过渡期保留；retire_after 到期移除", "doctor 的 key_rotation_state 与旧钥到期警告"],
            ["客户许可证", "可公开验证，但按商业机密管理", "台账脱敏；外发前复核"],
            ["CRL", "可公开；含序列号与原因", "随包 / 单独下发均需留痕"],
        ],
        widths=[3.2, 8.2, 4.6], size=8.8, caption="密钥与物料安全要求",
    )

    b.h2("11.2 数据安全")
    b.bullets([
        "许可证只包含档位、到期、席位、序列号、机器指纹（16 位 hash）、生效日 —— 不含姓名、手机号等个人数据。",
        "台账含客户联系人与合同信息，按内部敏感数据管理；导出 / 外发需脱敏与审批。",
        "客户机的 license_state.json 只记录时间 / 指纹 / 序列号，用于时钟与复检，不含其他数据。",
        "所有校验在本地完成，不向发行方回传任何数据（无遥测）。",
    ])

    b.h2("11.3 离线合规")
    b.table(
        ["合规点", "说明"],
        [
            ["离线验签", "Ed25519 验签纯本地，无外网请求；断网可激活与复检"],
            ["不可伪造", "客户端只有公钥；私钥只在发行方离线介质"],
            ["不可篡改", "签名覆盖档位 / 到期 / 席位 / 序列号 / 机器 / 生效日；改动即验签失败"],
            ["可审计", "吊销走 CRL；台账记录全生命周期；doctor 12 项可复核；周期复检自动降级/恢复"],
            ["数据最小化", "许可证不携带个人身份信息；机器指纹为哈希；license_state 只存时间/指纹/序列号"],
        ],
        widths=[3.6, 12.4], size=9, caption="离线与合规特性",
    )

    b.h2("11.4 审计与留痕")
    b.bullets([
        "签发留痕：谁、何时、给哪个客户、什么参数、生成的文件 sha256、许可证原文。",
        "变更留痕：续期 / 升档 / 换机 / 吊销的原因、操作人、时间、影响客户。",
        "交付留痕：渠道、签收人、客户确认激活的记录。",
        "安全事件留痕：泄露 / 伪造 / 吊销事件的时间线、处置动作、通知范围。",
        "保留期：合同期 + 2 年；安全事件记录长期保留。",
    ])

    b.h2("11.5 泄露 / 事故响应")
    b.table(
        ["级别", "判定", "响应"],
        [
            ["P0 私钥泄露", "私钥出现在非受控位置 / 被未授权使用", "立即启动 3.7 泄露应急；24h 内通知受影响客户"],
            ["P1 许可证伪造 / 篡改", "客户侧出现无法验签的证 / 企图绕过", "记录证据；确认是否内部误操作；必要时吊销"],
            ["P1 批量误发", "把 A 客户的包发给 B 客户", "立即回收 / 吊销误发包；重发正确包；评估数据暴露"],
            ["P2 客户环境异常", "时钟回拨 / 机器指纹异常 / 版本不一致", "按第 10 章处理；无需安全升级"],
            ["P3 流程违规", "未登记台账 / 未按审批签发", "纠正 + 复盘 + 流程改进"],
        ],
        widths=[3.6, 6.4, 6.0], size=8.8, caption="安全事件分级与响应",
    )

    b.h2("11.6 合规检查表")
    b.table(
        ["检查项", "频率", "通过标准"],
        [
            ["私钥位置与权限", "每月", "仅受控介质；ACL / 权限正确；无私钥副本扩散"],
            ["git / 构建产物扫描", "每次发版", "无私钥内容；wheel / sdist 无签发工具"],
            ["doctor 全绿", "每次发版", "exit 0，ok=true"],
            ["台账完整性", "每月", "必填字段缺失率 < 2%；与产物对账一致"],
            ["CRL 与吊销记录", "每月", "吊销原因与客户通知齐全"],
            ["密钥轮换记录", "每季度", "key_id 变更记录；LEGACY 到期提醒"],
            ["客户许可证到期分布", "每季度", "提前 90 天进入续费流程"],
            ["安全事件复盘", "按需", "记录完整、改进项闭环"],
        ],
        widths=[5.0, 2.6, 8.4], size=8.8, caption="合规检查表（季度归档）",
    )
    b.page_break()


# ── 第 12 章 附录 ────────────────────────────────────────────────
def _ch12(b: ManualBuilder):
    b.h1("第 12 章  附录")

    b.h2("12.1 CLI 速查")
    b.p("下表覆盖 v1.11.17 发行工具链的常用命令（商业运营 CLI 自 v1.11.15 起未变化；完整参数以 `--help` 为准）：")
    b.table(
        ["子命令", "用途", "示例"],
        [
            ["keygen", "生成 / 轮换签名密钥对", "keygen --write-public-key；keygen --rotate --retire-after 20271231"],
            ["issue", "签发单份许可证（v2 / v3）", "issue PRO 20271231 acme；issue PRO acme --days 365 --serial ACME20260001"],
            ["issue-batch", "按 CSV 批量签发 + 台账（可选 .ampkg）", "issue-batch --csv customers.csv --out dist/licenses --bundles"],
            ["bundle", "签发 + 打成 .ampkg（可附带 CRL）", "bundle ENT 00000000 bigcorp --seats 50 --out dist/upgrades"],
            ["verify", "校验许可证（v3 字段 / 原因）", "verify \"AMP2-…\""],
            ["pubkey", "查看内置公钥与指纹", "pubkey"],
            ["doctor", "发行前 12 项自检", "doctor --upgrades-dir dist/upgrades --crl dist/crl/license_crl.json"],
            ["revoke", "吊销许可证 / 序列号（写 CRL）", "revoke ACME20260007 --reason refund"],
            ["crl", "查看吊销清单 / 原文", "crl list；crl show --crl dist/crl/license_crl.json"],
            ["machine", "打印本机机器指纹（做绑定证用）", "machine"],
        ],
        widths=[2.4, 5.6, 8.0], size=8.0, caption="CLI 速查（v1.11.17，CLI 与 v1.11.15 一致，以 --help 为准）",
    )
    b.code(
        "# 常用完整命令（复制即用）\n"
        "python pro/tools/issue_license.py machine\n"
        "python pro/tools/issue_license.py pubkey\n"
        "python pro/tools/issue_license.py doctor\n"
        "python pro/tools/issue_license.py issue PRO 20271231 acme --json\n"
        "python pro/tools/issue_license.py issue PRO acme --days 365 --serial ACME20260001\n"
        "python pro/tools/issue_license.py bundle PRO 20271231 acme\n"
        "python pro/tools/issue_license.py issue-batch --csv customers.csv --out dist/licenses --bundles\n"
        "python pro/tools/issue_license.py revoke ACME20260007 --reason refund\n"
        "python scripts/build_pro_upgrade.py --tier ENT --expiry 00000000 --customer bigcorp --seats 50")

    b.h2("12.2 发行前检查清单")
    b.table(
        ["#", "检查项", "完成"],
        [
            ["1", "doctor 12 项全绿（公钥 / 私钥 / wheel / bundle / CRL / 轮换 / 复检 / 机器绑定）", "☐"],
            ["2", f"版本三处一致（核心 / 商业包 / pyproject = v{VERSION}）", "☐"],
            ["3", "pro/dist 无旧 wheel；dist/upgrades 无旧包", "☐"],
            ["4", "wheel 内置公钥与 keys.py 一致", "☐"],
            ["5", "许可证参数与合同一致（档位 / 到期或 days / 席位 / 序列号 / 绑定）", "☐"],
            ["6", ".ampkg 已生成并计算 sha256；manifest 记录 crl 摘要", "☐"],
            ["7", "台账已生成并补充运营字段（客户 / 序列号 / 到期 / 证原文 / sha256）", "☐"],
            ["8", "隔离环境做过 install_upgrade 冒烟（含 CRL 场景）", "☐"],
            ["9", f"客户升级步骤与话术已准备（含 v{VERSION} 换包说明与续跑要求）", "☐"],
            ["10", "私钥未出现在任何交付物 / 文档 / 日志中", "☐"],
        ],
        widths=[1.2, 13.0, 1.8], size=8.8, caption="发行前检查清单",
    )

    b.h2("12.3 客户交付检查清单")
    b.table(
        ["#", "交付项", "完成"],
        [
            ["1", "确认客户主程序版本与扩展包同版本", "☐"],
            ["2", ".ampkg（或 wheel + 许可证）已发送并核对 sha256", "☐"],
            ["3", "激活步骤说明（图文 / 话术）已发送", "☐"],
            ["4", "客户确认激活成功（徽标 / edition / 功能入口）", "☐"],
            ["5", "台账记录激活日期与方式", "☐"],
            ["6", "到期日与续费负责人已告知客户", "☐"],
        ],
        widths=[1.2, 13.0, 1.8], size=9, caption="客户交付检查清单",
    )

    b.h2("12.4 CSV 模板")
    b.code(
        "# customers.csv（UTF-8；带表头；# 注释与空行忽略；expiry 与 days 二选一）\n"
        "customer,tier,expiry,seats,machine,serial,start,notes,days\n"
        "# 2026-10 批次：华东区续费\n"
        "acme,PRO,,0,,ACME20260001,,年度续费,365\n"
        "bigcorp,ENT,20271231,50,0123456789abcdef,BIGCORP0001,,永久 50 席，绑定服务器,\n"
        "trial-co,PRO,,5,,TRIAL202601,,试用 30 天,30\n"
        "huanqiu,PRO,,2,,HUANQIU0001,20261201,12 月起生效,365")

    b.h2("12.5 manifest 示例")
    b.code(
        "{\n"
        "  \"format\": 1,\n"
        "  \"tier\": \"PRO\",\n"
        "  \"customer\": \"acme\",\n"
        "  \"expiry\": \"20271231\",\n"
        "  \"seats\": 0,\n"
        "  \"wheel\": \"automind_pro-1.11.17-py3-none-any.whl\",\n"
        "  \"summary\": {\"tier\": \"pro\", \"customer\": \"acme\", \"expiry\": \"20271231\",\n"
        "              \"issued\": \"20261023\", \"seats\": 0, \"serial\": \"ACME20260001\",\n"
        "              \"machine\": null, \"not_before\": null,\n"
        "              \"key_id\": \"e9fffafc0f6f19d4\", \"license_version\": 3},\n"
        "  \"package_version\": \"1.11.17\",\n"
        "  \"core_version\": \"1.11.17\",\n"
        "  \"key_id\": \"e9fffafc0f6f19d4\",\n"
        "  \"crl\": {\"file\": \"license_crl.json\", \"sha256\": \"<摘要>\", \"count\": 3}\n"
        "}")

    b.h2("12.6 FAQ")
    faqs = [
        ("客户断网能激活吗？", "能。许可证与 .ampkg 都是离线验签，不需要外网；断网后重启仍保持商业版。"),
        ("客户重装系统 / 换硬盘，许可证还能用吗？",
         "非绑定证可以；绑定证机器指纹会变化（安装盐在 ~/.automind/machine_salt），需发行方重签（8.3）。"),
        ("客户可以自己改许可证到期日吗？", "不能。到期日被签名覆盖，改动后验签失败（signature_invalid）。"),
        ("一个 .ampkg 能给多台机器用吗？",
         "不绑定机器时可以在多台机器激活（仍受席位与合同约束）；绑定证只能在一台机器用。"),
        ("客户把许可证发到群里了怎么办？",
         "立即 revoke 该序列号并把 CRL 随新包 / 单独下发；给客户重签新序列号。吊销是离线的，"
         "CRL 没送到客户机之前旧证仍可用（见 8.4）。"),
        ("永久证还需要续费吗？", "永久证无到期日，但换档 / 换绑机器需重签；合同维度的维护费另按商务约定。"),
        ("为什么客户装了扩展包还是社区版？",
         "常见：公钥未置备 / wheel 内置旧公钥 / 许可证文件优先级被覆盖 / 许可证过期或机器不符。按第 10 章排查。"),
        ("拖入 .ampkg 提示重启正常吗？", "正常。进程已加载旧扩展，Python 模块不可热替换；重启一次后自动激活。"),
        ("批量签发时能同时出包吗？", "可以，加 --bundles；包输出到 --out 目录，按客户命名。"),
        ("CRL 多久生效？", "客户端默认每 3600 秒复检一次；随 .ampkg 下发或重启可立即生效；离线吊销必须伴随分发。"),
        ("客户时钟被调早会怎样？", "回拨超过 1 天（86400 秒）会降级并记录 clock_rollback；校时后等复检（≤1 小时）自动恢复。"),
        ("客户把时钟向前调会怎样？", "向前调不会触发回拨检测，但会让许可证看起来提前到期（离线许可证固有限制）；改回正确时间即可恢复。"),
        ("宽限期怎么设置？", "客户机环境变量 AUTOMIND_LICENSE_GRACE_DAYS（默认 0，建议年度客户 7~15 天）；"
                              "宽限只对到期生效，不豁免吊销 / 机器不符 / 时钟回拨。"),
        ("换钥后旧许可证还能用吗？",
         "过渡期内可以（旧公钥在 LEGACY 列表）；retire_after 到期或旧钥被移除后全部失效。"),
        ("带序列号 / 机器绑定的 v3 证能用在旧客户端吗？",
         "v3 证需要 v1.11.15+ 扩展包解析；旧客户端请发 v2（不带 serial/machine/start），"
         "或直接用同版本 .ampkg 升级扩展包。旧 v2 证在新客户端继续有效。"),
        ("没有序列号的老证怎么吊销？", "工具用派生序列号 sha256(许可证)[:12]，可用许可证原文 revoke，也可用该派生序列号。"),
        ("客户升级到 v1.11.17 需要重新发证吗？",
         "旧 v2 / v3 证继续有效；建议换发新版 .ampkg（内含 1.11.17 wheel，与主程序同号）。"
         "本版修复的是社区核心的商业特性路由挂载，扩展包功能未变，但同号发布仍是发行纪律。"),
        ("升级到 v1.11.17 后「路由与成本」页仍提示功能未开通 / 被版本门控？",
         "先重启服务；仍提示则说明扩展包与主程序不同号（换发 v1.11.17 的 .ampkg 后重启）。"
         "社区版 / 未激活时该页显示升级入口属于正常门控。"),
        ("升级到 v1.11.17 后「续跑」按钮不出现 / 提示不可续跑？",
         "通常是 Pro 扩展包仍是 1.11.15：换发 v1.11.16 及以上的 .ampkg 后重启即可；协同 / 循环续跑本身需要专业版。"),
        ("许可证丢了怎么办？", "从台账取回许可证原文重新粘贴；或重新拖入 .ampkg。"),
        ("客服需要知道私钥吗？", "不需要，也绝不能接触。客服只需要客户标识、序列号与原因码。"),
    ]
    for i, (q, a) in enumerate(faqs, 1):
        b.p(f"Q{i}：{q}", bold_prefix="", size=10)
        b.p(f"A：{a}", size=9.8, color=COLOR_GRAY)

    b.h2("12.7 版本历史")
    b.table(
        ["版本", "日期", "商业运营相关变更"],
        [
            [f"v{VERSION}", RELEASE_DATE,
             "修复商业特性 REST 路由未挂载（已激活仍提示语义缓存 / 智能路由 / 成本仪表盘未开通）；"
             "工作区会话管理（分组 / 排序 / 筛选 / 搜索）；商业包与 .ampkg 同号重建，licensing CLI 未变化"],
            ["v1.11.16", "2026-10-23",
             "任务执行连续性：Token 预算自动续窗、协同 / 循环 / 子智能体续跑、四模式断点续跑、"
             "工作流自动化强化、执行治理 26 项参数生效、路由与成本页修复"],
            ["v1.11.15", "2026-10-22", "许可证 v3（序列号 / 机器绑定 / 生效日）、批量发证与台账、CRL 吊销、"
                                      "周期复检、宽限期、密钥轮换、客户侧状态展示"],
            ["v1.11.14", "2026-10-21", "商业激活闭环：真实公钥 / 同版本 wheel / .ampkg 一键升级 + doctor"],
            ["v1.11.7", "2026-09-29", "应用内一键升级（.ampkg = wheel + 许可证）"],
            ["v1.7.0", "2026-09-15", "Ed25519 非对称许可证 v2（替代旧 HMAC）"],
        ],
        widths=[2.2, 3.2, 10.6], size=9, caption="商业运营相关版本历史",
    )
    b.blank(1)
    b.p("—— 手册完 ——", align="center", size=11, color=COLOR_MUTED)


# ══════════════════════════════════════════════════════════════════
#  组装与校验
# ══════════════════════════════════════════════════════════════════
def build_docx() -> Path:
    missing = [f for f in CHARTS if not (ASSETS / f"{f}.png").is_file()]
    if missing:
        print(f"[docx] 缺少 {len(missing)} 张配图，先重新生成图表 …", flush=True)
        build_charts()
    b = ManualBuilder()
    _cover(b)
    _doc_info(b)
    _toc(b)
    for fn in (_ch1, _ch2, _ch3, _ch4, _ch5, _ch6,
               _ch7, _ch8, _ch9, _ch10, _ch11, _ch12):
        fn(b)
    path = b.save(OUT_DOCX)
    print(f"[docx] {path.name}  ({path.stat().st_size / 1024 / 1024:.2f} MB)", flush=True)
    return path


def verify_docx(path: Path) -> dict:
    """重新打开 docx，统计结构并做基本一致性检查。"""
    from docx import Document

    doc = Document(str(path))
    headings = [(p.style.name, p.text.strip()) for p in doc.paragraphs
                if p.style.name.startswith("Heading") and p.text.strip()]
    shapes = len(doc.inline_shapes)
    tables = len(doc.tables)
    paragraphs = len(doc.paragraphs)
    size_mb = path.stat().st_size / 1024 / 1024
    text = "\n".join(p.text for p in doc.paragraphs)
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                text += "\n" + cell.text
    checks = {
        "version": VERSION,
        "size_mb": round(size_mb, 2),
        "paragraphs": paragraphs,
        "tables": tables,
        "inline_images": shapes,
        "headings": len(headings),
        "has_ch1": "第 1 章" in text,
        "has_ch12": "第 12 章" in text,
        "has_ampkg": ".ampkg" in text,
        "has_issue_batch": "issue-batch" in text,
        "has_crl": "CRL" in text,
        "has_doctor": "doctor" in text,
        "has_machine": "machine" in text,
        "has_version": VERSION in text,
        "has_budget": "预算" in text,
        "has_resume": "断点续跑" in text,
        "has_workflow_auto": "fallback" in text and "if/unless" in text and "整轮超时" in text,
        "has_governance": "执行治理" in text,
        "has_router_fix": "路由与成本" in text,
        "has_upgrade_1717": "v1.11.17 发布纪律" in text,
        "size_ok": size_mb < 6.0,
        "images_ok": shapes >= 9,
    }
    checks["ok"] = all(v for k, v in checks.items()
                       if k in ("has_ch1", "has_ch12", "has_ampkg", "has_issue_batch",
                                "has_crl", "has_doctor", "has_machine", "has_version",
                                "has_budget", "has_resume", "has_workflow_auto",
                                "has_governance", "has_router_fix", "has_upgrade_1717",
                                "size_ok", "images_ok"))
    checks["heading_list"] = [f"{i + 1:02d} {name}: {text_}"
                              for i, (name, text_) in enumerate(headings)]
    return checks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成商业运营手册（docx + 图表）")
    ap.add_argument("--charts", action="store_true", help="只生成图表")
    ap.add_argument("--docx", action="store_true", help="只生成 Word（图表需已存在）")
    args = ap.parse_args(argv)

    if args.charts:
        build_charts()
        return 0
    if args.docx:
        build_docx()
        return 0
    build_charts()
    path = build_docx()
    report = verify_docx(path)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

