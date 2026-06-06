"""Tests for header/footer detection and stripping."""
from __future__ import annotations

from pathlib import Path

import pytest

from paper_reader.headers_footers import detect_header_footer_blocks


PAGE_HEIGHT = 800.0
PAGE_WIDTH = 600.0


def _block(text: str, bbox: tuple[float, float, float, float]) -> dict:
    """Build a minimal PyMuPDF-style text block dict."""
    return {
        "type": 0,
        "bbox": bbox,
        "lines": [{"spans": [{"text": text}]}],
    }


def _footer_bbox(y_offset: float = 10.0) -> tuple[float, float, float, float]:
    """A bbox sitting near the bottom of the page (within bottom 10% band)."""
    # Bottom band starts at PAGE_HEIGHT * 0.9 = 720.
    y0 = PAGE_HEIGHT - y_offset - 12
    y1 = PAGE_HEIGHT - y_offset
    return (50.0, y0, 100.0, y1)


def _header_bbox(y_offset: float = 10.0) -> tuple[float, float, float, float]:
    """A bbox sitting near the top of the page (within top 10% band)."""
    # Top band ends at PAGE_HEIGHT * 0.1 = 80.
    return (50.0, y_offset, 500.0, y_offset + 12)


def _body_bbox(y: float = 400.0) -> tuple[float, float, float, float]:
    return (50.0, y, 500.0, y + 12)


def test_pure_page_number_footers_excluded():
    """Each page has a footer block with just a page number -> all 5 flagged."""
    pages = []
    for i in range(1, 6):
        pages.append([
            _block("Some body paragraph that varies.", _body_bbox()),
            _block(str(i), _footer_bbox()),
        ])
    heights = [PAGE_HEIGHT] * 5

    excluded = detect_header_footer_blocks(pages, heights)

    assert excluded == {(p, 1) for p in range(5)}


def test_repeated_journal_header_excluded():
    """Top block on every page has identical journal title -> all 5 flagged."""
    journal = "Journal of Important Things, Vol. 42"
    pages = []
    for i in range(5):
        pages.append([
            _block(journal, _header_bbox()),
            _block(f"Body content unique to page {i}.", _body_bbox()),
        ])
    heights = [PAGE_HEIGHT] * 5

    excluded = detect_header_footer_blocks(pages, heights)

    assert excluded == {(p, 0) for p in range(5)}
    # Body blocks must not be flagged.
    for p in range(5):
        assert (p, 1) not in excluded


def test_single_page_no_repetition():
    """Single-page doc: no repetition possible; non-numeric header stays."""
    pages = [[
        _block("Some Unique Header", _header_bbox()),
        _block("Body paragraph here.", _body_bbox()),
    ]]
    heights = [PAGE_HEIGHT]

    excluded = detect_header_footer_blocks(pages, heights)

    assert excluded == set()


def test_single_page_pure_page_number_still_flagged():
    """Page number rule fires even with a single page (no repetition needed)."""
    pages = [[
        _block("Title", _header_bbox()),
        _block("Body.", _body_bbox()),
        _block("1", _footer_bbox()),
    ]]
    heights = [PAGE_HEIGHT]

    excluded = detect_header_footer_blocks(pages, heights)

    assert excluded == {(0, 2)}


def test_unique_title_page_top_block_not_flagged():
    """Title page top block (unique title) is not flagged across the doc."""
    pages = [
        [_block("BlazingFast Query Engine", _header_bbox()),
         _block("Abstract content.", _body_bbox())],
        [_block("Section 1 Introduction", _header_bbox()),
         _block("Body of page two.", _body_bbox())],
        [_block("Section 2 Background", _header_bbox()),
         _block("Body of page three.", _body_bbox())],
        [_block("Section 3 Methods", _header_bbox()),
         _block("Body of page four.", _body_bbox())],
    ]
    heights = [PAGE_HEIGHT] * 4

    excluded = detect_header_footer_blocks(pages, heights)

    assert excluded == set()


def test_paragraph_repeating_below_threshold_not_flagged():
    """A line repeating on 2/5 pages (<60%) is not flagged."""
    copyright_line = "(c) 2025 Authors. All rights reserved."
    pages = []
    for i in range(5):
        page_blocks = [_block(f"Body of page {i}.", _body_bbox())]
        # Put copyright in top band only on pages 0 and 1.
        if i < 2:
            page_blocks.insert(0, _block(copyright_line, _header_bbox()))
        else:
            page_blocks.insert(0, _block(f"Header unique to page {i}", _header_bbox()))
        pages.append(page_blocks)
    heights = [PAGE_HEIGHT] * 5

    excluded = detect_header_footer_blocks(pages, heights)

    # 2/5 = 40% < 60% threshold. Headers are all unique-ish; nothing flagged.
    assert excluded == set()


def test_page_number_mid_page_not_flagged():
    """A '3' block mid-page (outside the top/bottom 10% band) is NOT flagged."""
    pages = []
    for i in range(5):
        pages.append([
            _block(f"Unique header {i}", _header_bbox()),
            # Mid-page numeric block -- looks like a page number but is in body.
            _block("3", _body_bbox(y=400.0)),
            _block(f"Body of page {i}.", _body_bbox(y=500.0)),
        ])
    heights = [PAGE_HEIGHT] * 5

    excluded = detect_header_footer_blocks(pages, heights)

    # Mid-page "3" is never in the band, so never a candidate -> not flagged.
    assert excluded == set()


def test_empty_input_returns_empty_set():
    assert detect_header_footer_blocks([], []) == set()


def test_pages_with_no_blocks_returns_empty_set():
    pages = [[], [], []]
    heights = [PAGE_HEIGHT] * 3
    assert detect_header_footer_blocks(pages, heights) == set()


def test_mismatched_lengths_raise():
    with pytest.raises(ValueError):
        detect_header_footer_blocks([[], []], [PAGE_HEIGHT])


def test_normalization_handles_whitespace_and_case():
    """Header text with varying whitespace/case still matches as repeated."""
    pages = [
        [_block("Journal of Things", _header_bbox()),
         _block("Body 1.", _body_bbox())],
        [_block("  journal  of  things  ", _header_bbox()),
         _block("Body 2.", _body_bbox())],
        [_block("JOURNAL OF THINGS", _header_bbox()),
         _block("Body 3.", _body_bbox())],
        [_block("Journal of Things", _header_bbox()),
         _block("Body 4.", _body_bbox())],
        [_block("Journal of Things", _header_bbox()),
         _block("Body 5.", _body_bbox())],
    ]
    heights = [PAGE_HEIGHT] * 5

    excluded = detect_header_footer_blocks(pages, heights)

    assert excluded == {(p, 0) for p in range(5)}


def test_header_in_top_band_only_others_ignored():
    """A repeated string that appears in different bands across pages isn't conflated."""
    repeated = "Repeated Line"
    pages = []
    for i in range(5):
        # Put the same text in the top band on some pages, in body on others.
        if i % 2 == 0:
            pages.append([
                _block(repeated, _header_bbox()),
                _block(f"Body {i}.", _body_bbox()),
            ])
        else:
            pages.append([
                _block(f"Unique header {i}", _header_bbox()),
                _block(repeated, _body_bbox()),
            ])
    heights = [PAGE_HEIGHT] * 5

    excluded = detect_header_footer_blocks(pages, heights)

    # Only 3/5 of the header candidates match the repeated string -> 60% exactly,
    # which IS >= 60%, so the 3 header instances on pages 0, 2, 4 are flagged.
    assert excluded == {(0, 0), (2, 0), (4, 0)}
    # Body instances on pages 1, 3 stay.
    assert (1, 1) not in excluded
    assert (3, 1) not in excluded


def _load_pdf_blocks(pdf_path: Path):
    """Helper: open a PDF with PyMuPDF and return (pages_blocks, page_heights)."""
    import fitz

    doc = fitz.open(pdf_path)
    try:
        pages_blocks: list[list[dict]] = []
        heights: list[float] = []
        for page in doc:
            raw_blocks = page.get_text("dict")["blocks"]
            text_blocks = [b for b in raw_blocks if b.get("type") == 0]
            pages_blocks.append(text_blocks)
            heights.append(page.rect.height)
    finally:
        doc.close()
    return pages_blocks, heights


def test_integration_armbrust_runs_cleanly(armbrust_pdf: Path):
    """Detector runs against the real armbrust PDF without crashing.

    Note: armbrust uses alternating left-page / right-page running heads, each
    appearing on 50% of pages — below the 60% threshold by design. We only
    require the detector to execute cleanly and return valid identifiers.
    """
    if not armbrust_pdf.exists():
        pytest.skip(f"Sample PDF not present: {armbrust_pdf}")
    pytest.importorskip("fitz")

    pages_blocks, heights = _load_pdf_blocks(armbrust_pdf)
    excluded = detect_header_footer_blocks(pages_blocks, heights)

    for page_idx, block_idx in excluded:
        assert 0 <= page_idx < len(pages_blocks)
        assert 0 <= block_idx < len(pages_blocks[page_idx])


def test_integration_corpus_flags_at_least_one_doc(sample_pdfs: list[Path]):
    """Across the sample corpus, at least one PDF should yield flagged chrome.

    Validates the detector finds real header/footer patterns on at least one
    real paper (most ACM/VLDB papers use a stable repeated running header).
    """
    pytest.importorskip("fitz")
    if not sample_pdfs:
        pytest.skip("No sample PDFs available")

    total_flagged_docs = 0
    for pdf_path in sample_pdfs:
        pages_blocks, heights = _load_pdf_blocks(pdf_path)
        excluded = detect_header_footer_blocks(pages_blocks, heights)
        # Sanity: all returned ids are valid.
        for page_idx, block_idx in excluded:
            assert 0 <= page_idx < len(pages_blocks)
            assert 0 <= block_idx < len(pages_blocks[page_idx])
        if excluded:
            total_flagged_docs += 1

    assert total_flagged_docs >= 1, (
        f"Expected at least one of {len(sample_pdfs)} sample PDFs to have "
        "detectable header/footer chrome"
    )
