"""文档解析 — PDF / Word(.docx) / Markdown / TXT → 纯文本。

依赖策略（社区版零强依赖）：
    - PDF：优先 ``pypdf``（项目已依赖）；缺失时报友好错误
    - DOCX：标准库 zipfile + XML 解析（.docx 本质是 zip 包），无需 python-docx
    - MD / TXT：直接按 UTF-8（带 BOM/GBK 回退）读取
"""

from __future__ import annotations

import io
import re
import zipfile

SUPPORTED_EXTS = (".pdf", ".docx", ".md", ".markdown", ".txt")


def extract_text(filename: str, data: bytes) -> str:
    """按扩展名把上传的字节流解析为纯文本；不支持的类型抛 ValueError。"""
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        return _from_pdf(data)
    if name.endswith(".docx"):
        return _from_docx(data)
    if name.endswith((".md", ".markdown", ".txt")):
        return _from_text(data)
    raise ValueError(f"不支持的文件类型：{filename}（支持 PDF / DOCX / MD / TXT）")


def _from_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _from_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise ValueError("解析 PDF 需要 pypdf：pip install pypdf") from e
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    text = "\n\n".join(pages).strip()
    if not text:
        raise ValueError("PDF 未提取到文本（可能是扫描件，需先 OCR）")
    return text


# w:p = 段落，w:t = 文本 run；跨 run 拼接、段落之间换行
_W_T = re.compile(rb"<w:t(?:\s[^>]*)?>(.*?)</w:t>", re.S)
_W_P = re.compile(rb"<w:p[\s>]")


def _from_docx(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml = zf.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as e:
        raise ValueError("DOCX 解析失败：不是有效的 Word 文档（仅支持 .docx，不支持 .doc）") from e
    paragraphs: list[str] = []
    # 按段落切分再取每段中的全部 w:t 文本
    for para in re.split(_W_P, xml)[1:]:
        runs = _W_T.findall(para)
        text = "".join(_unescape_xml(r.decode("utf-8", errors="replace")) for r in runs)
        if text.strip():
            paragraphs.append(text.strip())
    return "\n\n".join(paragraphs)


def _unescape_xml(s: str) -> str:
    return (s.replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&"))


def chunk_text(text: str, chunk_size: int = 700, overlap: int = 80) -> list[str]:
    """把长文本切成语义友好的片段：先按段落聚合，超长段落再按定长滑窗切。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if len(p) > chunk_size:  # 超长段落：滑窗
            if buf:
                chunks.append(buf)
                buf = ""
            step = max(1, chunk_size - overlap)
            for i in range(0, len(p), step):
                seg = p[i:i + chunk_size]
                if seg.strip():
                    chunks.append(seg.strip())
                if i + chunk_size >= len(p):
                    break
        elif len(buf) + len(p) + 1 <= chunk_size:
            buf = (buf + "\n" + p).strip()
        else:
            chunks.append(buf)
            buf = p
    if buf:
        chunks.append(buf)
    return chunks
