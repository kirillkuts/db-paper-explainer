"""Tests for the block classifier."""
from __future__ import annotations

from pathlib import Path

import pytest

from paper_reader.classifier import BlockKind, classify_blocks


def _text_block(
    text: str,
    *,
    bbox: tuple[float, float, float, float] = (50.0, 100.0, 250.0, 200.0),
    font: str = "TimesNewRoman",
    size: float = 10.0,
    n_lines: int = 1,
) -> dict:
    """Build a minimal PyMuPDF-shaped text block.

    Splits ``text`` into ``n_lines`` equal segments and stretches them
    vertically inside the block bbox.
    """
    x0, y0, x1, y1 = bbox
    if n_lines <= 0:
        n_lines = 1
    # Distribute lines vertically.
    step = (y1 - y0) / n_lines
    seg_len = max(1, len(text) // n_lines)
    lines = []
    for i in range(n_lines):
        ly0 = y0 + i * step
        ly1 = ly0 + step
        if i == n_lines - 1:
            chunk = text[i * seg_len :]
        else:
            chunk = text[i * seg_len : (i + 1) * seg_len]
        lines.append(
            {
                "bbox": (x0, ly0, x1, ly1),
                "spans": [
                    {
                        "text": chunk,
                        "font": font,
                        "size": size,
                        "bbox": (x0, ly0, x1, ly1),
                    }
                ],
            }
        )
    return {"type": 0, "bbox": bbox, "lines": lines}


def _image_block(bbox: tuple[float, float, float, float] = (50.0, 50.0, 250.0, 250.0)) -> dict:
    return {"type": 1, "bbox": bbox, "lines": []}


def test_plain_body_text_is_text():
    block = _text_block(
        "Recent advances in column-based query engines have reshaped analytics workloads.",
        font="LinLibertine",
    )
    out = classify_blocks(page=None, blocks=[block], drawings=[])
    assert out == [(BlockKind.TEXT, block)]


def test_image_block_is_non_text():
    block = _image_block()
    out = classify_blocks(page=None, blocks=[block], drawings=[])
    assert out[0][0] is BlockKind.NON_TEXT


def test_single_greek_letter_in_latin_text_is_text():
    # One Greek alpha among ~80 chars -> ratio ~1.2%, below 5% threshold.
    block = _text_block(
        "The model parameter α controls the learning rate across the entire training loop here.",
    )
    out = classify_blocks(page=None, blocks=[block], drawings=[])
    assert out[0][0] is BlockKind.TEXT


def test_high_math_symbol_ratio_is_non_text():
    block = _text_block("∑ x ∈ ℝ : ∀ y, x ⊕ y ≤ ε")
    out = classify_blocks(page=None, blocks=[block], drawings=[])
    assert out[0][0] is BlockKind.NON_TEXT


def test_math_font_span_is_non_text():
    block = _text_block("xyz", font="CMSY10")
    out = classify_blocks(page=None, blocks=[block], drawings=[])
    assert out[0][0] is BlockKind.NON_TEXT


def test_vector_drawing_overlap_is_non_text():
    block = _text_block("axis label", bbox=(100.0, 100.0, 300.0, 300.0))
    # A drawing covering >20% of the block area.
    drawing = (50.0, 50.0, 250.0, 250.0)  # 150x150 inside block -> 150*150 / 200*200 = 56%
    out = classify_blocks(page=None, blocks=[block], drawings=[drawing])
    assert out[0][0] is BlockKind.NON_TEXT


def test_small_vector_drawing_overlap_stays_text():
    block = _text_block(
        "An ordinary paragraph that happens to share a page with a tiny decorative rule somewhere else entirely.",
        bbox=(100.0, 100.0, 300.0, 300.0),
    )
    # Drawing intersects only a small corner: 10x10 / 200x200 = 0.25%
    drawing = (95.0, 95.0, 105.0, 105.0)
    out = classify_blocks(page=None, blocks=[block], drawings=[drawing])
    assert out[0][0] is BlockKind.TEXT


def test_caption_adjacent_to_image_becomes_non_text():
    image = _image_block(bbox=(50.0, 100.0, 250.0, 300.0))
    # Caption sits 5pt below the image, single line ~12pt tall -> within 1.5x line height.
    caption = _text_block(
        "Figure 1: System architecture overview.",
        bbox=(50.0, 305.0, 250.0, 317.0),
        n_lines=1,
    )
    out = classify_blocks(page=None, blocks=[image, caption], drawings=[])
    kinds = [k for k, _ in out]
    assert kinds == [BlockKind.NON_TEXT, BlockKind.NON_TEXT]


def test_caption_far_from_image_stays_text():
    image = _image_block(bbox=(50.0, 100.0, 250.0, 200.0))
    # Caption 500pt below the image -- nowhere near 1.5x line height.
    caption = _text_block(
        "Figure 1: This caption is way too far from any figure to merge.",
        bbox=(50.0, 700.0, 250.0, 712.0),
        n_lines=1,
    )
    out = classify_blocks(page=None, blocks=[image, caption], drawings=[])
    kinds = [k for k, _ in out]
    assert kinds == [BlockKind.NON_TEXT, BlockKind.TEXT]


def test_equation_line_with_math_font_is_non_text():
    # Centered single-line block: "f(x) = ax + b      (1)" with CMMI font.
    block = _text_block(
        "f(x) = ax + b    (1)",
        bbox=(200.0, 400.0, 400.0, 415.0),
        font="CMMI10",
        n_lines=1,
    )
    out = classify_blocks(page=None, blocks=[block], drawings=[])
    assert out[0][0] is BlockKind.NON_TEXT


def test_empty_lines_array_is_text():
    block = {"type": 0, "bbox": (0.0, 0.0, 10.0, 10.0), "lines": []}
    out = classify_blocks(page=None, blocks=[block], drawings=[])
    assert out[0][0] is BlockKind.TEXT


def test_drawings_pulled_from_page_when_not_passed():
    """If `drawings` is omitted, classifier should call `page.get_drawings()`."""

    class StubPage:
        def __init__(self, rects):
            self._rects = rects
            self.calls = 0

        def get_drawings(self):
            self.calls += 1
            return [{"rect": r} for r in self._rects]

    block = _text_block("axis label", bbox=(100.0, 100.0, 300.0, 300.0))
    page = StubPage([(50.0, 50.0, 250.0, 250.0)])
    out = classify_blocks(page=page, blocks=[block])
    assert page.calls == 1
    assert out[0][0] is BlockKind.NON_TEXT


def test_page_without_get_drawings_method_does_not_crash():
    """A stub page object missing get_drawings is fine — drawings default to empty."""

    class BarePage:
        pass

    block = _text_block("plain body text that should stay text-classified.")
    out = classify_blocks(page=BarePage(), blocks=[block])
    assert out[0][0] is BlockKind.TEXT


def test_integration_real_pdf_has_non_text_block(input_dir: Path):
    """Open p2132-afroozeh.pdf and verify at least one block is NON_TEXT."""
    pymupdf = pytest.importorskip("pymupdf")
    pdf_path = input_dir / "p2132-afroozeh.pdf"
    if not pdf_path.exists():
        pytest.skip(f"Sample PDF not present: {pdf_path}")

    doc = pymupdf.open(pdf_path)
    try:
        found_non_text = False
        # Scan first few pages — figures/equations appear early.
        for page_idx in range(min(5, doc.page_count)):
            page = doc[page_idx]
            blocks = page.get_text("dict")["blocks"]
            classified = classify_blocks(page, blocks)
            if any(k is BlockKind.NON_TEXT for k, _ in classified):
                found_non_text = True
                break
        assert found_non_text, "expected at least one NON_TEXT block in first 5 pages"
    finally:
        doc.close()
