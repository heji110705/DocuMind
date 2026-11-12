"""上传文件的落盘、校验与解析。

这一层刻意不含任何 async / 模型调用：它只回答"这个文件能不能收、收下来是什么文本"。
好处是它可以在 CI 里用几个字节的假文件跑完，不需要索引也不需要 API Key。

校验顺序是有讲究的：**先看大小再看格式**。反过来的话，一个 300MB 的 .zip
会先被当成"不支持的后缀"礼貌拒绝，但字节已经读到内存里了。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ALLOWED = {".md": "markdown", ".txt": "text", ".pdf": "pdf"}
MAX_BYTES = 20 * 1024 * 1024      # 单文件 20MB：再大就该走批量入库而不是网页上传
MAX_CHARS = 200_000               # 约 300 页技术文档；超了截断并明确告知，不静默丢
UPLOAD_ROOT = ROOT / "data" / "uploads"


class IngestError(Exception):
    def __init__(self, code: str, status: int, message: str):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message


@dataclass
class Doc:
    text: str
    backend: str          # markdown / text / pymupdf / docling / ocr
    display_name: str
    stored_path: Path
    notes: list[str]

    @property
    def chars(self) -> int:
        return len(self.text)


def safe_name(filename: str) -> str:
    """只保留文件名本身，并做一次规范化。

    两个目的：
    1. 防路径穿越：`../../.env` 这种名字不能被当成路径用。
    2. 防"同名覆盖"：文件名前面拼上内容哈希，同一份内容重复上传天然去重，
       不同内容同名文件也不会互相覆盖。
    """
    base = Path(str(filename or "")).name
    base = unicodedata.normalize("NFKC", base)
    base = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", base).strip(" .")
    if not base:
        raise IngestError("E2205", 400, "文件名无效")
    if len(base) > 80:
        stem, dot, ext = base.rpartition(".")
        base = (stem[:70] + dot + ext) if dot else base[:80]
    return base


def check_ext(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED:
        raise IngestError(
            "E2202", 415,
            f"不支持的格式 {ext or '（无后缀）'}，只收 {'/'.join(sorted(ALLOWED))}",
        )
    return ext


def save_bytes(index_tag: str, filename: str, data: bytes) -> Path:
    """按内容哈希落盘，返回存储路径。"""
    if not data:
        raise IngestError("E2201", 400, "文件是空的")
    if len(data) > MAX_BYTES:
        raise IngestError("E2203", 413, f"文件超过 {MAX_BYTES // 1024 // 1024}MB 上限")
    name = safe_name(filename)
    digest = hashlib.sha1(data).hexdigest()[:8]
    folder = UPLOAD_ROOT / index_tag
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{digest}-{name}"
    path.write_bytes(data)
    return path


def decode_text(data: bytes) -> str:
    """utf-8-sig 优先：它同时接受"带 BOM"和"不带 BOM"的 UTF-8。

    先试 utf-8 的话，带 BOM 的文件会解出一个看不见的 \\ufeff 挂在正文开头，
    后续按内容哈希去重、按首字匹配全都会受影响，而且肉眼完全看不出来。
    """
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def parse_document(path: Path, data: bytes | None = None, backend: str = "auto",
                   name: str | None = None) -> Doc:
    """把上传的文件变成可入库的文本。

    name 是**原始文件名**。落盘时为了防同名覆盖会在前面拼内容哈希，
    但引用回链里显示的必须是用户认得的那个名字——`2bfd5f99-字段字典.md`
    出现在引用抽屉里，等于把"这是哪份文件"这条线索弄丢了。
    """
    ext = check_ext(path.name)
    if ext == ".pdf":
        from loaders import parse_pdf

        p = parse_pdf(path, backend=backend)
        text, used, notes = p.markdown, p.backend, list(p.notes)
    else:
        text = decode_text(data if data is not None else path.read_bytes())
        used, notes = ALLOWED[ext], []

    text = (text or "").strip()
    if len(text) < 20:
        raise IngestError(
            "E2204", 422,
            "解析不出文字。若是扫描件，需要 OCR 后端（pip install rapidocr-onnxruntime）；"
            "若是加密 PDF，先解除密码保护再上传。",
        )
    if len(text) > MAX_CHARS:
        notes.append(f"正文超过 {MAX_CHARS:,} 字，已截断；建议拆成多篇再传")
        text = text[:MAX_CHARS]
    return Doc(text=text, backend=used, display_name=safe_name(name or path.name),
               stored_path=path, notes=notes)
