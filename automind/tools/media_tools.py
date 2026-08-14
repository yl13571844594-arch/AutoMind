"""多媒体工具集 —— 截屏 / OCR / 图像处理 / 图表 / 音频 / 视频。

统一遵循可选依赖懒加载：这些库不进核心依赖，工具照常注册，真正调用时缺库
返回可照抄的安装命令（见 ``automind.tools._toolkit``）。视频能力依赖外部
``ffmpeg``/``ffprobe`` 命令（``shutil.which`` 探测），无需 Python 包。
"""

from __future__ import annotations

import base64
import io
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from automind.core.types import PermissionTier, ToolResult
from automind.tools._toolkit import bad, err, need, ok
from automind.tools.base import AbstractTool


# ── 截屏 ──────────────────────────────────────────────────
class ScreenshotTool(AbstractTool):
    """截取屏幕（全屏或指定区域），保存为 PNG 或返回 base64。"""

    name = "screenshot_tool"
    description = (
        "Capture the screen (full screen or a region) and save it as a PNG, "
        "optionally also returning a base64-encoded thumbnail for preview. "
        "Useful for 'see what is on screen' tasks."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Output PNG path (default: auto-generated under cwd)."},
            "region": {
                "type": "object",
                "properties": {
                    "left": {"type": "number"}, "top": {"type": "number"},
                    "right": {"type": "number"}, "bottom": {"type": "number"},
                },
                "description": "Optional bounding box (left, top, right, bottom) for a region capture.",
            },
            "return_base64": {"type": "boolean", "description": "Also return a base64 thumbnail (default false)."},
        },
        "required": [],
    }
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 25

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            need("PIL")
            from PIL import ImageGrab

            region = kwargs.get("region")
            bbox = None
            if isinstance(region, dict) and all(k in region for k in ("left", "top", "right", "bottom")):
                bbox = (int(region["left"]), int(region["top"]),
                        int(region["right"]), int(region["bottom"]))
            img = ImageGrab.grab(bbox=bbox)
            path = Path(str(kwargs.get("path") or "")).expanduser() if kwargs.get("path") else None
            if path is None:
                path = Path.cwd() / f"screenshot_{int(__import__('time').time())}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            img.save(str(path), format="PNG")
            out: dict[str, Any] = {
                "path": str(path), "width": img.width, "height": img.height,
                "size": path.stat().st_size,
            }
            if kwargs.get("return_base64"):
                thumb = img.copy()
                thumb.thumbnail((640, 640))
                buf = io.BytesIO()
                thumb.save(buf, format="PNG")
                out["base64"] = base64.b64encode(buf.getvalue()).decode("ascii")
            return ok(self.name, **out)
        except Exception as e:
            return err(self.name, e)


# ── OCR ───────────────────────────────────────────────────
class OcrTool(AbstractTool):
    """从图片或截图提取文字（OCR）。"""

    name = "ocr_tool"
    description = (
        "Extract text from an image file via OCR (pytesseract + Tesseract). "
        "Actions: image (OCR a local image path), screenshot (capture screen then OCR). "
        "Note: requires 'pip install pytesseract' plus the Tesseract binary installed on the system."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["image", "screenshot"]},
            "path": {"type": "string", "description": "Image path (for image action)."},
            "lang": {"type": "string", "description": "Tesseract language code (default 'chi_sim+eng')."},
        },
        "required": ["action"],
    }
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 25

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = str(kwargs.get("action", "")).lower()
        lang = str(kwargs.get("lang", "chi_sim+eng"))
        try:
            need("pytesseract")
            import pytesseract
            from PIL import Image, ImageGrab

            if action == "image":
                path = Path(str(kwargs.get("path", ""))).expanduser()
                if not path.exists():
                    return bad(self.name, f"文件不存在：{path}")
                img = Image.open(str(path))
            elif action == "screenshot":
                img = ImageGrab.grab()
            else:
                return bad(self.name, f"未知 action：{action}")

            text = pytesseract.image_to_string(img, lang=lang)
            return ok(self.name, action=action, text=text.strip(), length=len(text.strip()))
        except Exception as e:
            return err(self.name, e)


# ── 图像处理 ──────────────────────────────────────────────
class ImageTool(AbstractTool):
    """图像缩放 / 裁剪 / 格式转换 / 水印 / 信息读取。"""

    name = "image_tool"
    description = (
        "Process images with Pillow. Actions: resize (scale by width/height, keep aspect "
        "if only one given), crop (bounding box), convert (change format), watermark "
        "(overlay text), info (size/format/mode)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["resize", "crop", "convert", "watermark", "info"]},
            "path": {"type": "string", "description": "Input image path."},
            "output": {"type": "string", "description": "Output path (default overwrites input for resize/crop/watermark)."},
            "width": {"type": "number", "description": "Target width for resize."},
            "height": {"type": "number", "description": "Target height for resize."},
            "box": {
                "type": "object",
                "properties": {
                    "left": {"type": "number"}, "top": {"type": "number"},
                    "right": {"type": "number"}, "bottom": {"type": "number"},
                },
                "description": "Crop bounding box (left, top, right, bottom).",
            },
            "format": {"type": "string", "description": "Target format for convert (PNG/JPEG/WEBP/...)."},
            "text": {"type": "string", "description": "Watermark text."},
        },
        "required": ["action", "path"],
    }
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 15

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = str(kwargs.get("action", "")).lower()
        path = Path(str(kwargs.get("path", ""))).expanduser()
        try:
            need("PIL")
            from PIL import Image, ImageDraw, ImageFont

            if not path.exists():
                return bad(self.name, f"文件不存在：{path}")
            img = Image.open(str(path))
            output = Path(str(kwargs.get("output") or "")).expanduser() if kwargs.get("output") else path

            if action == "info":
                return ok(self.name, path=str(path), format=img.format, mode=img.mode,
                          size=[img.width, img.height])

            if action == "resize":
                w = kwargs.get("width")
                h = kwargs.get("height")
                if not w and not h:
                    return bad(self.name, "resize 需要 width 或 height")
                if w and h:
                    size = (int(w), int(h))
                elif w:
                    ratio = int(w) / img.width
                    size = (int(w), int(img.height * ratio))
                else:
                    ratio = int(h) / img.height
                    size = (int(img.width * ratio), int(h))
                img = img.resize(size, Image.LANCZOS)
                img.save(str(output))

            elif action == "crop":
                box = kwargs.get("box")
                if not isinstance(box, dict) or not all(k in box for k in ("left", "top", "right", "bottom")):
                    return bad(self.name, "crop 需要 box（left/top/right/bottom）")
                img = img.crop((int(box["left"]), int(box["top"]),
                                int(box["right"]), int(box["bottom"])))
                img.save(str(output))

            elif action == "convert":
                fmt = str(kwargs.get("format") or "").upper() or "PNG"
                if output.suffix.lower() not in (f".{fmt.lower()}",):
                    output = output.with_suffix(f".{fmt.lower()}")
                img.save(str(output), format=fmt)

            elif action == "watermark":
                text = str(kwargs.get("text") or "")
                if not text:
                    return bad(self.name, "watermark 需要 text")
                overlay = img.convert("RGBA")
                layer = Image.new("RGBA", overlay.size, (0, 0, 0, 0))
                draw = ImageDraw.Draw(layer)
                try:
                    font = ImageFont.truetype("arial.ttf", max(14, img.width // 40))
                except Exception:
                    font = ImageFont.load_default()
                draw.text((10, 10), text, fill=(255, 255, 255, 160), font=font)
                img = Image.alpha_composite(overlay, layer).convert("RGB")
                img.save(str(output))

            else:
                return bad(self.name, f"未知 action：{action}")

            return ok(self.name, action=action, output=str(output),
                      size=[img.width, img.height])
        except Exception as e:
            return err(self.name, e)


# ── 图表 ──────────────────────────────────────────────────

#: 各平台常见的中文字体（按优先级），用于让图表标题/坐标轴能显示中文
_CJK_FONTS = (
    "Microsoft YaHei", "SimHei", "SimSun",          # Windows
    "PingFang SC", "Hiragino Sans GB", "STHeiti",   # macOS
    "Noto Sans CJK SC", "Source Han Sans SC",       # Linux
    "WenQuanYi Zen Hei", "Arial Unicode MS",
)
_cjk_font_applied = False


def _use_cjk_font(matplotlib: Any) -> None:
    """给 matplotlib 挑一个能显示中文的字体（只做一次）。

    matplotlib 默认字体 DejaVu Sans 不含汉字，中文标题会画成一排空心方框，
    同时刷一屏 "Glyph ... missing from font" 警告 —— 对一个中文优先的产品来说，
    图表工具等于半残。这里在系统已装字体里挑第一个可用的中文字体。
    """
    global _cjk_font_applied
    if _cjk_font_applied:
        return
    _cjk_font_applied = True
    try:
        from matplotlib import font_manager
        installed = {f.name for f in font_manager.fontManager.ttflist}
        picked = [f for f in _CJK_FONTS if f in installed]
        if picked:
            matplotlib.rcParams["font.sans-serif"] = [*picked, "DejaVu Sans"]
        # 中文字体多数缺 U+2212（真正的减号），会让负数刻度也变方框
        matplotlib.rcParams["axes.unicode_minus"] = False
    except Exception:
        pass   # 挑字体失败不该让画图整个失败，大不了回到方框


class ChartTool(AbstractTool):
    """把数据画成折线图 / 柱状图 / 饼图 / 散点图，导出 PNG。"""

    name = "chart_tool"
    description = (
        "Plot data into a chart image (PNG) with matplotlib. Actions: line, bar, pie, scatter. "
        "Pass x (list) + y (list) for line/bar/scatter, or labels + values for pie."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["line", "bar", "pie", "scatter"]},
            "output": {"type": "string", "description": "Output PNG path."},
            "x": {"type": "array", "description": "X values (line/bar/scatter)."},
            "y": {"type": "array", "description": "Y values (line/bar/scatter)."},
            "labels": {"type": "array", "items": {"type": "string"}, "description": "Slice labels (pie)."},
            "values": {"type": "array", "description": "Slice values (pie)."},
            "title": {"type": "string", "description": "Chart title."},
            "xlabel": {"type": "string", "description": "X axis label."},
            "ylabel": {"type": "string", "description": "Y axis label."},
        },
        "required": ["action", "output"],
    }
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 10

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = str(kwargs.get("action", "")).lower()
        output = Path(str(kwargs.get("output", ""))).expanduser()
        try:
            need("matplotlib")
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            _use_cjk_font(matplotlib)

            # labels/values 是 pie 的参数名，但模型在 bar/line/scatter 上同样
            # 高频使用。此前这些别名被无视 —— x/y 取到空列表，图照画、文件照存、
            # 结果照报 success，用户拿到的是一张**空白图**。宁可报错也不能这样。
            xs = kwargs.get("x") or kwargs.get("labels") or []
            ys = kwargs.get("y") or kwargs.get("values") or []

            if action in ("line", "bar", "scatter") and (not xs or not ys):
                return bad(self.name,
                           f"{action} 需要成对的数据：x（或 labels）与 y（或 values），"
                           "当前有一侧为空，画出来会是一张空白图")
            if action in ("line", "bar", "scatter") and len(xs) != len(ys):
                return bad(self.name,
                           f"{action} 的 x/y 长度不一致（{len(xs)} vs {len(ys)}）")

            fig, ax = plt.subplots(figsize=(8, 5))
            title = kwargs.get("title") or ""

            if action == "line":
                ax.plot(xs, ys, marker="o")
            elif action == "bar":
                ax.bar([str(v) for v in xs], ys)
            elif action == "scatter":
                ax.scatter(xs, ys)
            elif action == "pie":
                vals = kwargs.get("values") or kwargs.get("y") or []
                if not vals:
                    plt.close(fig)
                    return bad(self.name, "pie 需要 values（各扇区数值）")
                ax.pie(vals, labels=kwargs.get("labels") or kwargs.get("x") or [],
                       autopct="%1.1f%%")
                ax.axis("equal")
            else:
                plt.close(fig)
                return bad(self.name, f"未知 action：{action}")

            ax.set_title(title)
            if action in ("line", "bar", "scatter"):
                if kwargs.get("xlabel"):
                    ax.set_xlabel(kwargs["xlabel"])
                if kwargs.get("ylabel"):
                    ax.set_ylabel(kwargs["ylabel"])
            fig.tight_layout()
            output.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(str(output), dpi=120)
            plt.close(fig)
            return ok(self.name, action=action, output=str(output),
                      size=output.stat().st_size)
        except Exception as e:
            return err(self.name, e)


# ── 音频 ──────────────────────────────────────────────────
class AudioTool(AbstractTool):
    """读取音频文件元信息（时长 / 格式 / 采样率 / 声道）。"""

    name = "audio_tool"
    description = (
        "Inspect audio file metadata. Action: info — returns duration, format, "
        "sample rate, channels and bitrate for WAV/MP3/FLAC/M4A etc. (uses mutagen "
        "when available, falling back to stdlib wave for WAV)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["info"]},
            "path": {"type": "string", "description": "Audio file path."},
        },
        "required": ["action", "path"],
    }
    permission_tier = PermissionTier.SAFE
    risk_score = 5

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = str(kwargs.get("action", "")).lower()
        path = Path(str(kwargs.get("path", ""))).expanduser()
        try:
            if action != "info":
                return bad(self.name, f"未知 action：{action}")
            if not path.exists():
                return bad(self.name, f"文件不存在：{path}")

            # 优先标准库 wave（WAV 零依赖），其余格式用 mutagen（可选）
            if path.suffix.lower() == ".wav":
                import wave
                with wave.open(str(path), "rb") as w:
                    frames = w.getnframes()
                    rate = w.getframerate()
                    duration = frames / rate if rate else 0
                    return ok(self.name, path=str(path), format="WAV",
                              duration=round(duration, 2), sample_rate=rate,
                              channels=w.getnchannels(), frames=frames)

            try:
                need("mutagen")
                from mutagen import File as MutagenFile
                mf = MutagenFile(str(path))
                if mf is None:
                    return bad(self.name, "无法识别的音频格式（可安装 mutagen 增强支持）")
                info = mf.info
                return ok(self.name, path=str(path), format=mf.mime or path.suffix.lstrip("."),
                          duration=round(getattr(info, "length", 0) or 0, 2),
                          sample_rate=getattr(info, "sample_rate", 0),
                          channels=getattr(info, "channels", 0),
                          bitrate=getattr(info, "bitrate", 0))
            except Exception:
                return bad(self.name,
                           "该格式需要 mutagen：pip install mutagen（WAV 已内置支持）")
        except Exception as e:
            return err(self.name, e)


# ── 视频 ──────────────────────────────────────────────────
class VideoTool(AbstractTool):
    """读取视频信息 / 抽帧截图（依赖外部 ffmpeg / ffprobe）。"""

    name = "video_tool"
    description = (
        "Inspect video files and extract frames. Actions: info (duration, resolution, "
        "codec via ffprobe), frame (save a frame at a given second as JPEG via ffmpeg). "
        "Requires ffmpeg/ffprobe on PATH."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["info", "frame"]},
            "path": {"type": "string", "description": "Video file path."},
            "time": {"type": "number", "description": "Timestamp in seconds for frame capture (default 0)."},
            "output": {"type": "string", "description": "Output JPEG path for frame."},
        },
        "required": ["action", "path"],
    }
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 10

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = str(kwargs.get("action", "")).lower()
        path = Path(str(kwargs.get("path", ""))).expanduser()
        try:
            if not path.exists():
                return bad(self.name, f"文件不存在：{path}")
            if not shutil.which("ffprobe"):
                return bad(self.name, "未检测到 ffmpeg/ffprobe —— 请先安装 ffmpeg 并加入 PATH")

            if action == "info":
                probe = subprocess.run(
                    ["ffprobe", "-v", "quiet", "-print_format", "json",
                     "-show_format", "-show_streams", str(path)],
                    capture_output=True, text=True, timeout=60, check=False,
                    encoding="utf-8", errors="replace")
                if probe.returncode != 0:
                    return bad(self.name, "ffprobe 解析失败：" + (probe.stderr or "")[:200])
                data = json.loads(probe.stdout)
                fmt = data.get("format", {})
                video = next((s for s in data.get("streams", [])
                              if s.get("codec_type") == "video"), {})
                return ok(self.name, path=str(path),
                          duration=round(float(fmt.get("duration", 0) or 0), 2),
                          size=fmt.get("size"), format=fmt.get("format_name"),
                          width=video.get("width"), height=video.get("height"),
                          codec=video.get("codec_name"), fps=_fps(video.get("avg_frame_rate")))

            if action == "frame":
                output = Path(str(kwargs.get("output") or "")).expanduser()
                if not output:
                    return bad(self.name, "frame 需要 output 路径")
                t = float(kwargs.get("time", 0) or 0)
                output.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    ["ffmpeg", "-y", "-ss", str(t), "-i", str(path),
                     "-frames:v", "1", "-q:v", "2", str(output)],
                    capture_output=True, text=True, timeout=120, check=False,
                    encoding="utf-8", errors="replace")
                if not output.exists():
                    return bad(self.name, "抽帧失败，请确认 time 未超出视频时长")
                return ok(self.name, action=action, output=str(output),
                          time=t, size=output.stat().st_size)

            return bad(self.name, f"未知 action：{action}")
        except Exception as e:
            return err(self.name, e)


def _fps(rate: str | None) -> float:
    """把 '30000/1001' 这类分数帧率转成浮点。"""
    if not rate:
        return 0.0
    try:
        num, _, den = rate.partition("/")
        return round(int(num) / int(den or 1), 2)
    except (ValueError, ZeroDivisionError):
        return 0.0
