"""上传链路：文件名净化、格式/大小校验、编码兜底、PDF 与 OCR。

这些用例全部用几十字节的假文件跑，不花钱、不依赖索引。
真实 PDF 用 tests/fixtures/sample-report.pdf（3 页，含 3 个线框表格）。
"""

from pathlib import Path

import pytest

from app import ingest
from app.ingest import IngestError, check_ext, decode_text, parse_document, safe_name, save_bytes

FIXTURE = Path(__file__).parent / "fixtures" / "sample-report.pdf"


@pytest.fixture()
def upload_dir(tmp_path, monkeypatch):
    """把落盘目录指到临时目录，测试不往仓库里写东西。"""
    monkeypatch.setattr(ingest, "UPLOAD_ROOT", tmp_path / "uploads")
    return tmp_path / "uploads"


class TestSafeName:
    def test_strips_path(self):
        assert "/" not in safe_name("../../etc/passwd")
        assert ".." not in safe_name("../../.env")

    def test_removes_windows_separators(self):
        assert "\\" not in safe_name(r"C:\Windows\secret.md")

    def test_keeps_chinese(self):
        assert safe_name("字段字典.md") == "字段字典.md"

    def test_strips_trailing_dots(self):
        """Windows 上以点结尾的文件名会被系统悄悄截断，入库名和展示名就对不上了。"""
        assert safe_name("文档.md...") == "文档.md"

    def test_long_name_truncated(self):
        out = safe_name("长" * 200 + ".md")
        assert len(out) <= 80 and out.endswith(".md")

    def test_empty_rejected(self):
        with pytest.raises(IngestError) as e:
            safe_name("   ")
        assert e.value.code == "E2205"


class TestCheckExt:
    def test_allowed(self):
        for n in ("a.md", "a.txt", "a.pdf", "A.PDF"):
            assert check_ext(n)

    def test_rejected(self):
        for n in ("a.docx", "a.zip", "a", "a.md.exe"):
            with pytest.raises(IngestError) as e:
                check_ext(n)
            assert (e.value.status, e.value.code) == (415, "E2202")


class TestSaveBytes:
    def test_hash_prefix_makes_name_unique(self, upload_dir):
        p1 = save_bytes("t", "a.md", b"hello world")
        p2 = save_bytes("t", "a.md", b"hello mars!")
        assert p1 != p2
        assert p1.name.endswith("-a.md")
        assert len(p1.name.split("-")[0]) == 8   # 内容哈希前缀

    def test_same_content_same_path(self, upload_dir):
        assert save_bytes("t", "a.md", b"same") == save_bytes("t", "a.md", b"same")

    def test_empty_rejected(self, upload_dir):
        with pytest.raises(IngestError) as e:
            save_bytes("t", "a.md", b"")
        assert e.value.code == "E2201"

    def test_oversize_rejected(self, upload_dir, monkeypatch):
        monkeypatch.setattr(ingest, "MAX_BYTES", 10)
        with pytest.raises(IngestError) as e:
            save_bytes("t", "a.md", b"x" * 11)
        assert (e.value.status, e.value.code) == (413, "E2203")


class TestDecode:
    def test_utf8(self):
        assert decode_text("中文".encode()) == "中文"

    def test_gbk_fallback(self):
        """Windows 上导出的 txt 经常是 GBK，直接 utf-8 解码会乱码甚至报错。"""
        assert decode_text("中文".encode("gb18030")) == "中文"

    def test_bom_stripped(self):
        assert decode_text(b"\xef\xbb\xbfhello") == "hello"


class TestParseDocument:
    def test_markdown_ok(self, upload_dir):
        p = save_bytes("t", "说明.md", "这是一份说明文档。".encode() * 5)
        doc = parse_document(p)
        assert doc.backend == "markdown" and "说明文档" in doc.text

    def test_too_short_rejected(self, upload_dir):
        p = save_bytes("t", "空.md", b"hi")
        with pytest.raises(IngestError) as e:
            parse_document(p)
        assert (e.value.status, e.value.code) == (422, "E2204")
        assert "OCR" in e.value.message

    def test_truncates_huge_text(self, upload_dir, monkeypatch):
        monkeypatch.setattr(ingest, "MAX_CHARS", 100)
        p = save_bytes("t", "长.md", ("很长的正文。" * 100).encode())
        doc = parse_document(p)
        assert len(doc.text) == 100 and doc.notes

    def test_pdf_text_layer(self):
        doc = parse_document(FIXTURE, backend="pymupdf")
        assert doc.backend in ("pymupdf", "docling")
        assert len(doc.text) > 500


@pytest.mark.slow
class TestOcr:
    """扫描件：把文本 PDF 的每一页渲染成图，做成没有文本层的 PDF，再解析。"""

    @staticmethod
    def _scanned(src: Path, dst: Path) -> Path:
        import pymupdf as fitz

        s = fitz.open(src)
        out = fitz.open()
        for page in s:
            pix = page.get_pixmap(dpi=150)
            p = out.new_page(width=pix.width * 72 / 150, height=pix.height * 72 / 150)
            p.insert_image(p.rect, pixmap=pix)
        out.save(dst)
        out.close()
        s.close()
        return dst

    def test_no_text_layer_detected(self, tmp_path):
        import pymupdf as fitz

        from loaders import parse_pdf

        scanned = self._scanned(FIXTURE, tmp_path / "scanned.pdf")
        with fitz.open(scanned) as d:
            assert d[0].get_text().strip() == ""      # 确认这份 fixture 真的没有文本层
        p = parse_pdf(scanned, ocr="off")
        assert p.markdown.strip() == ""
        assert any("扫描件" in n for n in p.notes)

    def test_ocr_recovers_text(self, tmp_path):
        from loaders import ocr_backend, parse_pdf

        if not ocr_backend():
            pytest.skip("没装 OCR 后端（pip install -r requirements-ocr.txt）")

        scanned = self._scanned(FIXTURE, tmp_path / "scanned.pdf")
        p = parse_pdf(scanned, max_pages=1)
        assert p.backend == "ocr"
        assert len(p.markdown) > 200
        assert p.ocr_confidence and p.ocr_confidence > 0.5
        # NFKC 归一化：OCR 会把等宽的 pay_amount 认成全角，不归一化就搜不到
        assert "pay_amount" in p.markdown.replace(" ", "")
