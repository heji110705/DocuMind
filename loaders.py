"""PDF 版面解析：docling 优先，PyMuPDF 兜底。

为什么需要这一层：
    `page.get_text()` 拿到的是按内容流顺序的纯文本。对纯段落文档够用，
    对带标题层级和表格的技术文档是灾难——表格会被拍平成
    "渠道 字节 GMV 182340.5 腾讯 140221.0"，行列对应关系全丢，
    索引进去之后模型会把不同行的数字拼在一起。

两个后端的差别：
    docling   版面模型识别段落/标题/表格，输出 markdown，表格保留结构。
              代价是重（要 torch，镜像大 2GB+），首次运行还要下模型。
    pymupdf   用字号 + 线条推断版面。不需要模型，几十毫秒一页。
              标题层级靠字号聚类，表格靠 find_tables 的线框检测。

选择策略：装了 docling 就用 docling，没装自动降级，并在返回值里标清用了哪个。
不静默降级——因为解析质量差别很大，使用者有权知道。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Parsed:
    markdown: str
    backend: str
    pages: int
    tables: int = 0
    headings: int = 0
    notes: list[str] = field(default_factory=list)
    # OCR 专属：平均置信度（0~1）。低于 0.6 基本没法用，要让调用方知道
    ocr_confidence: float | None = None


# ------------------------------------------------------------------------ OCR
#
# 为什么要有这一层：
# 扫描件（打印后扫描、手机拍照转 PDF、传真件）里没有任何文本层，
# `page.get_text()` 返回空字符串。以前的表现是索引里插进去 0 字，
# 然后用户问什么都是"证据不足"——看起来像模型不行，其实是根本没读到内容。
#
# 两个后端，按可用性自动选：
#   rapidocr-onnxruntime   纯 pip 安装，自带中英文模型，不需要系统依赖。首选。
#   tesseract             识别质量更稳，但要装系统包和语言包，容器里多一层 apt。
# 都没有时**不静默降级**：抛一个能看懂的错，而不是把空文本插进索引。

OCR_DPI = 200          # 200 够印刷体；再高只会更慢
OCR_MIN_CHARS_PER_PAGE = 20   # 平均每页少于这个字符数，就当没有文本层


def ocr_backend() -> str | None:
    """返回当前可用的 OCR 后端名，没有就 None。"""
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return "rapidocr"
    except Exception:
        pass
    import shutil
    if shutil.which("tesseract"):
        try:
            import pytesseract  # noqa: F401
            return "tesseract"
        except Exception:
            return None
    return None


def _normalize_ocr(text: str) -> str:
    """OCR 结果做一次 NFKC 归一化。

    rapidocr 会把等宽/代码字体里的 ASCII 认成全角字符：`pay_amount` → `ｐａｙ_ａｍｏｕｎｔ`。
    肉眼几乎看不出区别，但入库之后字段名就再也匹配不上了——
    实测扫描件里 "pay_amount" 完全搜不到，直到加了这一步。
    NFKC 把全角字母数字折回半角，对中文正文没有副作用。
    """
    import unicodedata

    return unicodedata.normalize("NFKC", text or "")


def _page_images(path: Path, max_pages: int | None):
    import pymupdf as fitz

    doc = fitz.open(path)
    n = min(max_pages, len(doc)) if max_pages else len(doc)
    try:
        for i in range(n):
            pix = doc[i].get_pixmap(dpi=OCR_DPI)
            yield i, pix
    finally:
        doc.close()


def _ocr_rapidocr(path: Path, max_pages: int | None) -> Parsed:
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    out: list[str] = []
    scores: list[float] = []
    pages = 0
    for i, pix in _page_images(path, max_pages):
        pages = i + 1
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if pix.n == 4:
            img = img[:, :, :3]
        result, _ = engine(img)
        lines = [t for _, t, s in (result or []) if t and t.strip()]
        scores += [float(s) for _, t, s in (result or []) if t and t.strip()]
        out.append(f"\n<!-- 第 {i + 1} 页（OCR） -->\n" + _normalize_ocr("\n".join(lines)))
    conf = round(sum(scores) / len(scores), 3) if scores else None
    notes = [f"扫描件：无文本层，已用 rapidocr 识别（{OCR_DPI} DPI）"]
    if conf is not None and conf < 0.6:
        notes.append(f"OCR 平均置信度偏低（{conf}），数字与英文可能识别错，建议人工复核")
    return Parsed("\n".join(out).strip(), "ocr", pages, notes=notes, ocr_confidence=conf)


def _ocr_tesseract(path: Path, max_pages: int | None) -> Parsed:
    import io

    import pytesseract
    from PIL import Image

    out: list[str] = []
    pages = 0
    for i, pix in _page_images(path, max_pages):
        pages = i + 1
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        txt = pytesseract.image_to_string(img, lang="chi_sim+eng")
        out.append(f"\n<!-- 第 {i + 1} 页（OCR） -->\n" + _normalize_ocr(txt.strip()))
    return Parsed("\n".join(out).strip(), "ocr", pages,
                  notes=[f"扫描件：无文本层，已用 tesseract 识别（{OCR_DPI} DPI）"])


def _parse_ocr(path: Path, max_pages: int | None, backend: str | None = None) -> Parsed:
    which = backend or ocr_backend()
    if which == "rapidocr":
        return _ocr_rapidocr(path, max_pages)
    if which == "tesseract":
        return _ocr_tesseract(path, max_pages)
    raise RuntimeError(
        "这份 PDF 没有文本层（扫描件），需要 OCR 后端：\n"
        "  pip install -r requirements-ocr.txt   # 推荐 rapidocr，纯 pip，无系统依赖\n"
        "或安装 tesseract 并保证它在 PATH 里"
    )



# --------------------------------------------------------------------- docling

def _has_docling() -> bool:
    try:
        import docling  # noqa: F401
        return True
    except Exception:
        return False


def _parse_docling(path: Path, max_pages: int | None) -> Parsed:
    from docling.document_converter import DocumentConverter

    conv = DocumentConverter()
    res = conv.convert(str(path), page_range=(1, max_pages) if max_pages else None)
    md = res.document.export_to_markdown()
    n_tab = md.count("\n|") and len(re.findall(r"\n\|[^\n]*\|\n\|[\s:|-]+\|", md)) or 0
    n_head = len(re.findall(r"^#{1,6} ", md, re.M))
    return Parsed(markdown=md, backend="docling", pages=max_pages or -1,
                  tables=n_tab, headings=n_head)


# -------------------------------------------------------------------- pymupdf

def _clean_md_table(rows: list[list[str]]) -> str:
    """把二维单元格转成 markdown 表。空表头补列号，避免 | | | 这种坏表。"""
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    head = [c.strip().replace("|", "\\|") or f"列{i + 1}" for i, c in enumerate(rows[0])]
    sep = ["---"] * width
    body = [[c.strip().replace("|", "\\|").replace("\n", " ") for c in r] for r in rows[1:]]
    lines = ["| " + " | ".join(head) + " |", "| " + " | ".join(sep) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


def _parse_pymupdf(path: Path, max_pages: int | None) -> Parsed:
    import pymupdf as fitz

    doc = fitz.open(path)
    n = min(max_pages, len(doc)) if max_pages else len(doc)

    # 第一遍：收集所有 span 的字号，用来决定标题的层级阈值。
    # 不用固定阈值（比如 14pt 以上算标题），因为不同文档的正文字号本身就不一样。
    sizes: dict[float, int] = {}
    for i in range(n):
        for b in doc[i].get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                for s in l["spans"]:
                    if s["text"].strip():
                        sizes[round(s["size"], 1)] = sizes.get(round(s["size"], 1), 0) + len(s["text"])
    if not sizes:
        doc.close()
        return Parsed("", "pymupdf", n, notes=["没有可提取文本，可能是扫描件（需 OCR）"])
    body_size = max(sizes.items(), key=lambda kv: kv[1])[0]
    head_sizes = sorted([s for s in sizes if s > body_size + 0.8], reverse=True)
    level_of = {s: min(i + 1, 3) for i, s in enumerate(head_sizes)}

    out: list[str] = []
    n_tab = n_head = 0
    for i in range(n):
        page = doc[i]
        # find_tables 返回的表区域，用来把表格里已识别的文字块从正文里剔掉，
        # 否则同一份内容会出现两次（一次表格，一次被拍平的段落）。
        tab_boxes = []
        tables = []
        try:
            for t in page.find_tables().tables:
                tab_boxes.append(fitz.Rect(t.bbox))
                rows = t.extract()
                tables.append((t.bbox[1], _clean_md_table(rows)))
        except Exception as e:
            out.append(f"")  # 表格识别失败不致命
            n_tab_note = str(e)
        else:
            n_tab_note = ""

        items: list[tuple[float, str]] = []
        for b in page.get_text("dict")["blocks"]:
            box = fitz.Rect(b["bbox"])
            if any(box.intersects(tb) for tb in tab_boxes):
                continue
            if box.y1 < page.rect.height * 0.06 or box.y0 > page.rect.height * 0.94:
                continue  # 页眉页脚：位置在边距内且通常只有一行
            for l in b.get("lines", []):
                text = "".join(s["text"] for s in l["spans"]).strip()
                if not text:
                    continue
                sz = round(max(s["size"] for s in l["spans"]), 1)
                bold = any("Bold" in s["font"] or "Heavy" in s["font"] for s in l["spans"])
                if sz in level_of and (bold or sz > body_size + 2):
                    items.append((l["bbox"][1], "#" * level_of[sz] + " " + text))
                    n_head += 1
                elif re.match(r"^\s*[-•·]\s+", text):
                    items.append((l["bbox"][1], "- " + re.sub(r"^\s*[-•·]\s+", "", text)))
                else:
                    items.append((l["bbox"][1], text))

        for y, md in tables:
            items.append((y, md))
            n_tab += 1

        items.sort(key=lambda x: x[0])
        out.append(f"\n<!-- 第 {i + 1} 页 -->\n")
        prev = ""
        for _, md in items:
            # 段落合并：行尾不是句末标点且下一行不是标题/列表时，接在同一段
            cont_ok = (
                prev
                and not md.startswith(("#", "-", "|", "<!--"))
                and not prev.startswith(("#", "-", "|", "<!--"))
                and not prev.endswith(("。", "：", "；", "！", "？", ".", ":"))
            )
            if cont_ok:
                out[-1] = out[-1].rstrip("\n") + md
            else:
                out.append(md + "\n")
            prev = md
        if n_tab_note:
            out.append(f"<!-- 表格识别异常：{n_tab_note[:80]} -->\n")

    doc.close()
    notes = []
    if n_tab == 0:
        notes.append("未识别到表格；若原文档有表，可能是无线框表格")
    return Parsed("\n".join(out).strip(), "pymupdf", n, tables=n_tab, headings=n_head, notes=notes)


# ---------------------------------------------------------------------- 入口

def parse_pdf(
    path: str | Path,
    backend: str = "auto",
    max_pages: int | None = None,
    ocr: str = "auto",
) -> Parsed:
    """backend: auto | docling | pymupdf | ocr

    ocr: auto（文本层为空才 OCR，默认）| off（绝不 OCR）| always（强制 OCR）
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    if backend == "ocr":
        return _parse_ocr(path, max_pages)

    if backend in ("auto", "docling") and _has_docling():
        try:
            p = _parse_docling(path, max_pages)
        except Exception as e:
            if backend == "docling":
                raise
            p = _parse_pymupdf(path, max_pages)
            p.notes.append(f"docling 失败（{type(e).__name__}），已降级 pymupdf")
    elif backend == "docling":
        raise RuntimeError("指定了 docling 但没装：pip install docling")
    else:
        p = _parse_pymupdf(path, max_pages)

    return _maybe_ocr(path, p, max_pages, ocr)


def _maybe_ocr(path: Path, p: Parsed, max_pages: int | None, ocr: str) -> Parsed:
    """文本层为空时补一次 OCR。

    判据不是"整篇 0 字"，而是"平均每页少于 20 字"：
    扫描件里偶尔会有一行页码之类的文本层残留，只看 0 字会漏掉。
    """
    if ocr == "off":
        return p
    pages = max(1, p.pages)
    need = ocr == "always" or len(p.markdown.strip()) < OCR_MIN_CHARS_PER_PAGE * pages
    if not need:
        return p

    which = ocr_backend()
    if not which:
        p.notes.append(
            "没有文本层，且未安装 OCR 后端（pip install -r requirements-ocr.txt）；"
            "本次返回的是空/近空正文"
        )
        return p

    try:
        o = _parse_ocr(path, max_pages, which)
    except Exception as e:
        p.notes.append(f"OCR 失败（{type(e).__name__}: {e}），已保留原结果")
        return p

    if len(o.markdown.strip()) <= len(p.markdown.strip()):
        p.notes.append(f"识别到的 OCR 文本不比原文更多（{len(o.markdown)} 字），保留原结果")
        return p

    o.notes = p.notes + o.notes
    o.headings = p.headings
    # 扫描件里表格没有线框信息，find_tables 也认不出来，这里如实记 0，
    # 不要沿用 pymupdf 的计数——那个数字此刻已经不代表任何东西了。
    o.tables = 0
    return o



if __name__ == "__main__":
    import sys

    p = parse_pdf(sys.argv[1], backend=sys.argv[2] if len(sys.argv) > 2 else "auto")
    print(f"backend={p.backend} pages={p.pages} tables={p.tables} headings={p.headings}")
    print(f"字符数={len(p.markdown):,}")
    for n in p.notes:
        print("note:", n)
    print("-" * 60)
    print(p.markdown[:1200])
