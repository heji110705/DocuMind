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

def parse_pdf(path: str | Path, backend: str = "auto", max_pages: int | None = None) -> Parsed:
    """backend: auto | docling | pymupdf"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    if backend in ("auto", "docling") and _has_docling():
        try:
            return _parse_docling(path, max_pages)
        except Exception as e:
            if backend == "docling":
                raise
            p = _parse_pymupdf(path, max_pages)
            p.notes.append(f"docling 失败（{type(e).__name__}），已降级 pymupdf")
            return p
    if backend == "docling":
        raise RuntimeError("指定了 docling 但没装：pip install docling")
    return _parse_pymupdf(path, max_pages)


if __name__ == "__main__":
    import sys

    p = parse_pdf(sys.argv[1], backend=sys.argv[2] if len(sys.argv) > 2 else "auto")
    print(f"backend={p.backend} pages={p.pages} tables={p.tables} headings={p.headings}")
    print(f"字符数={len(p.markdown):,}")
    for n in p.notes:
        print("note:", n)
    print("-" * 60)
    print(p.markdown[:1200])
