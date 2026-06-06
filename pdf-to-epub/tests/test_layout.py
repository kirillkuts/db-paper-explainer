"""Tests for paper_reader.layout."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from paper_reader.layout import detect_column_count, order_blocks


PAGE_WIDTH = 600.0
PAGE_HEIGHT = 800.0


@dataclass
class FakeRect:
    x0: float = 0.0
    y0: float = 0.0
    x1: float = PAGE_WIDTH
    y1: float = PAGE_HEIGHT

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0


class FakePage:
    def __init__(self, blocks: list[dict], rect: FakeRect | None = None) -> None:
        self._blocks = blocks
        self.rect = rect or FakeRect()

    def get_text(self, kind: str) -> dict:
        assert kind == "dict"
        return {"blocks": list(self._blocks)}


def _block(label: str, bbox: tuple[float, float, float, float]) -> dict:
    return {"type": 0, "bbox": bbox, "label": label, "lines": []}


def test_two_column_returns_left_then_right_top_to_bottom():
    # Left column x in [50, 280], right column x in [320, 550]; gap at midpoint 300.
    tl = _block("TL", (50, 100, 280, 200))
    bl = _block("BL", (50, 400, 280, 500))
    tr = _block("TR", (320, 110, 550, 210))
    br = _block("BR", (320, 410, 550, 510))
    # Pass them in scrambled order to ensure ordering is enforced, not input order.
    page = FakePage([br, tl, tr, bl])

    ordered = order_blocks(page)

    labels = [b["label"] for b in ordered]
    assert labels == ["TL", "BL", "TR", "BR"]


def test_one_column_returns_top_to_bottom():
    a = _block("A", (50, 100, 550, 200))
    b = _block("B", (50, 250, 550, 350))
    c = _block("C", (50, 400, 550, 500))
    page = FakePage([c, a, b])

    ordered = order_blocks(page)

    assert [x["label"] for x in ordered] == ["A", "B", "C"]


def test_full_width_block_ordered_by_y_within_two_column_layout():
    # Title block spans the entire page width — should appear at top by y0
    # before any column content even though it crosses the midpoint.
    title = _block("TITLE", (50, 40, 550, 80))
    tl = _block("TL", (50, 120, 280, 220))
    tr = _block("TR", (320, 120, 550, 220))
    bl = _block("BL", (50, 400, 280, 500))
    br = _block("BR", (320, 400, 550, 500))
    page = FakePage([tl, tr, bl, br, title])

    ordered = order_blocks(page)

    labels = [b["label"] for b in ordered]
    # Title comes first (lowest y); then left column top-to-bottom; then right.
    assert labels[0] == "TITLE"
    assert labels == ["TITLE", "TL", "BL", "TR", "BR"]


def test_full_width_block_between_column_rows():
    # A mid-page full-width banner appears between top and bottom column rows.
    tl = _block("TL", (50, 100, 280, 200))
    tr = _block("TR", (320, 100, 550, 200))
    banner = _block("BANNER", (50, 250, 550, 290))
    bl = _block("BL", (50, 350, 280, 450))
    br = _block("BR", (320, 350, 550, 450))
    page = FakePage([br, banner, tl, bl, tr])

    ordered = order_blocks(page)
    labels = [b["label"] for b in ordered]

    # Banner sits at y=250; both top column blocks (y=100) precede it,
    # both bottom column blocks (y=350) follow it.
    assert labels.index("BANNER") > labels.index("TL")
    assert labels.index("BANNER") > labels.index("TR")
    assert labels.index("BANNER") < labels.index("BL")
    assert labels.index("BANNER") < labels.index("BR")


def test_empty_page_returns_empty_list():
    page = FakePage([])
    assert order_blocks(page) == []


def test_blocks_only_on_left_side_classified_as_one_column():
    blocks = [
        _block("A", (50, 100, 280, 200)),
        _block("B", (50, 250, 280, 350)),
        _block("C", (50, 400, 280, 500)),
    ]
    page = FakePage(blocks)
    assert detect_column_count(blocks, page.rect) == 1
    ordered = order_blocks(page)
    assert [b["label"] for b in ordered] == ["A", "B", "C"]


def test_detect_column_count_two_columns():
    blocks = [
        _block("TL", (50, 100, 280, 200)),
        _block("BL", (50, 400, 280, 500)),
        _block("TR", (320, 110, 550, 210)),
        _block("BR", (320, 410, 550, 510)),
    ]
    rect = FakeRect()
    assert detect_column_count(blocks, rect) == 2


def test_detect_column_count_single_block_is_one_column():
    blocks = [_block("solo", (50, 100, 280, 200))]
    assert detect_column_count(blocks, FakeRect()) == 1


def test_blocks_preserve_original_dict_shape():
    tl = _block("TL", (50, 100, 280, 200))
    tr = _block("TR", (320, 110, 550, 210))
    page = FakePage([tr, tl])
    ordered = order_blocks(page)
    # Same dict identity preserved — downstream gets the full pymupdf payload.
    assert ordered[0] is tl
    assert ordered[1] is tr
    assert set(ordered[0].keys()) >= {"type", "bbox", "lines"}


def test_integration_armbrust_first_page_reading_order(armbrust_pdf):
    pymupdf = pytest.importorskip("pymupdf")
    if not armbrust_pdf.exists():
        pytest.skip(f"sample PDF not present: {armbrust_pdf}")

    doc = pymupdf.open(str(armbrust_pdf))
    try:
        page = doc[0]
        ordered = order_blocks(page)
        assert ordered, "expected at least one block on page 1"

        page_width = page.rect.width
        midx = page.rect.x0 + page_width / 2.0

        def center_x(b: dict) -> float:
            x0, _, x1, _ = b["bbox"]
            return (x0 + x1) / 2.0

        def is_full_width(b: dict) -> bool:
            x0, _, x1, _ = b["bbox"]
            overhang = page_width * 0.20
            return (midx - x0) >= overhang and (x1 - midx) >= overhang

        left_indices = [
            i for i, b in enumerate(ordered)
            if not is_full_width(b) and center_x(b) < midx
        ]
        right_indices = [
            i for i, b in enumerate(ordered)
            if not is_full_width(b) and center_x(b) >= midx
        ]

        # Two-column paper: must have both sides, and at least one left-column
        # block must appear before any right-column block.
        assert left_indices, "no left-column blocks detected"
        assert right_indices, "no right-column blocks detected"
        assert min(left_indices) < min(right_indices)
    finally:
        doc.close()
