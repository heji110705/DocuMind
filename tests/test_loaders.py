"""PDF 版面解析的回归测试。

不需要网络、不需要模型，纯本地。跑法：
    pytest tests/test_loaders.py -q
"""

from pathlib import Path

import pytest

from loaders import _clean_md_table, parse_pdf

FIXTURE = Path(__file__).parent / "fixtures" / "sample-report.pdf"


@pytest.fixture(scope="module")
def parsed():
    if not FIXTURE.exists():
        pytest.skip("测试 PDF 未生成：先跑 python tools/make_test_pdf.py")
    return parse_pdf(FIXTURE, backend="pymupdf")


def test_headings_become_markdown(parsed):
    """标题必须带上 # 前缀，否则索引时章节层级丢失。"""
    assert "# InsightPilot 数据字典" in parsed.markdown
    assert "## 一、订单事实表" in parsed.markdown
    assert parsed.headings >= 4


def test_heading_not_glued_to_body(parsed):
    """曾经有个 bug：正文被接到了标题行末尾，变成「## 一、订单事实表本表粒度…」。"""
    for line in parsed.markdown.splitlines():
        if line.startswith("## "):
            assert len(line) < 40, f"标题行被正文污染：{line[:60]}"


def test_tables_keep_structure(parsed):
    """表格必须是 markdown 表，而不是被拍平的一行文字。"""
    assert parsed.tables == 3
    assert "| 字段 | 类型 | 单位 | 说明 |" in parsed.markdown
    assert "| order_id | TEXT" in parsed.markdown


def test_table_cells_not_duplicated(parsed):
    """表格区域内的文字块要被剔除，否则同一内容出现两次。"""
    assert parsed.markdown.count("满 99 包邮") == 1


def test_page_footer_removed(parsed):
    """页脚页码不该进正文。"""
    assert "- 1 -" not in parsed.markdown


def test_clean_md_table_pads_short_rows():
    out = _clean_md_table([["a", "b", "c"], ["1", "2"]])
    assert out.splitlines()[2] == "| 1 | 2 |  |"


def test_clean_md_table_escapes_pipe():
    out = _clean_md_table([["a|b", "c"]])
    assert "a\\|b" in out
