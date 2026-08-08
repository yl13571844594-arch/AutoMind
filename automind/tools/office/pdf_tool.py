"""PDF 工具 —— 文本抽取 / 合并 / 拆分 / 旋转 / 元信息。

社区版动作：extract / info / merge / split / rotate
专业版动作（office_pro）：watermark（叠加水印层）/ encrypt（AES-256 打开密码）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from automind.core.types import PermissionTier, ToolResult
from automind.tools._toolkit import bad, delegate_pro, err, need, ok
from automind.tools.base import AbstractTool

#: 进阶动作（由专业版 office_pro 实现，社区版仅转交）
PRO_ACTIONS = {"watermark", "encrypt"}


class PdfTool(AbstractTool):
    """读取与重组 PDF。"""

    name = "pdf_tool"
    description = (
        "Work with PDF files. Actions: extract (text per page), info (page count + metadata), "
        "merge (combine several PDFs), split (extract a page range), rotate. "
        "Advanced actions (watermark/encrypt) require the Pro edition. "
        "Note: scanned PDFs have no text layer, so extract returns empty text for them."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["extract", "info", "merge", "split", "rotate",
                         "watermark", "encrypt"],
            },
            "path": {"type": "string", "description": "Input PDF path (or output path for merge)."},
            "inputs": {
                "type": "array", "items": {"type": "string"},
                "description": "Input PDF paths for merge (in order).",
            },
            "output": {"type": "string", "description": "Output path for split/rotate."},
            "pages": {
                "type": "string",
                "description": "Page range like '1-5' or '2,4,7' (1-based, inclusive).",
            },
            "degrees": {"type": "number", "description": "Rotation in degrees (90/180/270)."},
            "max_chars": {"type": "number", "description": "Cap on extracted characters (default 40000)."},
        },
        "required": ["action"],
    }
    permission_tier = PermissionTier.SENSITIVE
    risk_score = 30

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = str(kwargs.get("action", "")).lower()
        try:
            if action in PRO_ACTIONS:
                need("pypdf")
                return ok(self.name, **delegate_pro("office_pro", self.name, action, kwargs))
            need("pypdf")
            import pypdf  # noqa: PLC0415 - 懒加载

            if action == "merge":
                return self._merge(pypdf, kwargs)
            path = Path(str(kwargs.get("path", ""))).expanduser()
            if not path.is_file():
                return bad(self.name, f"文件不存在：{path}")
            if action == "info":
                return self._info(pypdf, path)
            if action == "extract":
                return self._extract(pypdf, path, kwargs)
            if action == "split":
                return self._split(pypdf, path, kwargs)
            if action == "rotate":
                return self._rotate(pypdf, path, kwargs)
            return bad(self.name, f"不支持的 action：{action}")
        except Exception as e:
            return err(self.name, e)

    # ── 页码解析 ──────────────────────────────────────────

    @staticmethod
    def _parse_pages(spec: str, total: int) -> list[int]:
        """把 '1-5' / '2,4,7' 解析成 0-based 页索引；越界的静默丢弃。"""
        out: list[int] = []
        for part in str(spec or "").split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                a, _, b = part.partition("-")
                try:
                    lo, hi = int(a), int(b)
                except ValueError:
                    continue
                out.extend(range(lo - 1, hi))
            else:
                try:
                    out.append(int(part) - 1)
                except ValueError:
                    continue
        return [i for i in out if 0 <= i < total]

    # ── 具体动作 ──────────────────────────────────────────

    def _info(self, pypdf: Any, path: Path) -> ToolResult:
        r = pypdf.PdfReader(str(path))
        meta = {k.lstrip("/"): str(v) for k, v in (r.metadata or {}).items()}
        return ok(self.name, path=str(path), pages=len(r.pages),
                  encrypted=r.is_encrypted, metadata=meta)

    def _extract(self, pypdf: Any, path: Path, kw: dict) -> ToolResult:
        cap = int(kw.get("max_chars") or 40000)
        r = pypdf.PdfReader(str(path))
        if r.is_encrypted:
            # 空密码能解开的加密 PDF 很常见，先试一把再报错
            try:
                r.decrypt("")
            except Exception:
                return bad(self.name, "PDF 已加密且需要密码，无法抽取文本")
        idx = self._parse_pages(kw["pages"], len(r.pages)) if kw.get("pages") \
            else range(len(r.pages))
        pages, total = [], 0
        for i in idx:
            t = r.pages[i].extract_text() or ""
            total += len(t)
            if total > cap:
                pages.append({"page": i + 1, "text": t[:max(0, cap - (total - len(t)))]})
                break
            pages.append({"page": i + 1, "text": t})
        joined = sum(len(p["text"].strip()) for p in pages)
        return ok(self.name, path=str(path), pages=pages, page_count=len(pages),
                  chars=total, truncated=total > cap,
                  note=("未抽到任何文字：该 PDF 很可能是扫描件（只有图像、没有文本层）。"
                        "本工具不含 OCR，请先用外部 OCR 工具转成可搜索 PDF 再试。")
                  if joined == 0 else None)

    def _merge(self, pypdf: Any, kw: dict) -> ToolResult:
        inputs = [Path(str(p)).expanduser() for p in (kw.get("inputs") or [])]
        if len(inputs) < 2:
            return bad(self.name, "merge 至少需要 2 个输入文件（inputs）")
        missing = [str(p) for p in inputs if not p.is_file()]
        if missing:
            return bad(self.name, f"以下文件不存在：{', '.join(missing)}")
        out = Path(str(kw.get("path") or kw.get("output") or "")).expanduser()
        if not str(out):
            return bad(self.name, "merge 需要通过 path 指定输出文件")
        w = pypdf.PdfWriter()
        n = 0
        for p in inputs:
            r = pypdf.PdfReader(str(p))
            for page in r.pages:
                w.add_page(page)
                n += 1
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "wb") as f:
            w.write(f)
        return ok(self.name, path=str(out), pages=n, sources=len(inputs),
                  message=f"已合并 {len(inputs)} 个 PDF 共 {n} 页 → {out.name}")

    def _split(self, pypdf: Any, path: Path, kw: dict) -> ToolResult:
        r = pypdf.PdfReader(str(path))
        idx = self._parse_pages(kw.get("pages", ""), len(r.pages))
        if not idx:
            return bad(self.name, f"pages 未指定或超出范围（该文档共 {len(r.pages)} 页）")
        out = Path(str(kw.get("output") or path.with_name(f"{path.stem}_split.pdf"))).expanduser()
        w = pypdf.PdfWriter()
        for i in idx:
            w.add_page(r.pages[i])
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "wb") as f:
            w.write(f)
        return ok(self.name, path=str(out), pages=len(idx),
                  message=f"已提取 {len(idx)} 页 → {out.name}")

    def _rotate(self, pypdf: Any, path: Path, kw: dict) -> ToolResult:
        deg = int(kw.get("degrees") or 90)
        if deg % 90:
            return bad(self.name, "degrees 必须是 90 的整数倍")
        r = pypdf.PdfReader(str(path))
        idx = self._parse_pages(kw["pages"], len(r.pages)) if kw.get("pages") \
            else range(len(r.pages))
        idx_set = set(idx)
        w = pypdf.PdfWriter()
        for i, page in enumerate(r.pages):
            if i in idx_set:
                page.rotate(deg)
            w.add_page(page)
        out = Path(str(kw.get("output") or path)).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "wb") as f:
            w.write(f)
        return ok(self.name, path=str(out), rotated=len(idx_set), degrees=deg,
                  message=f"已旋转 {len(idx_set)} 页 {deg}° → {out.name}")
