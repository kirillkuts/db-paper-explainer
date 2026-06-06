"""Tests for paper_reader.rasterizer."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")

from paper_reader.rasterizer import image_filename, rasterize_region


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_dimensions(png_bytes: bytes) -> tuple[int, int]:
    """Parse width/height from a PNG's IHDR chunk."""
    assert png_bytes[:8] == PNG_SIGNATURE
    # IHDR is the first chunk; its data starts at byte 16.
    width = struct.unpack(">I", png_bytes[16:20])[0]
    height = struct.unpack(">I", png_bytes[20:24])[0]
    return width, height


def _pdf_exists(path: Path) -> bool:
    return path.exists() and path.is_file()


def test_image_filename_basic():
    assert image_filename(0, 0) == "img-p000-000.png"
    assert image_filename(12, 5) == "img-p012-005.png"
    assert image_filename(999, 999) == "img-p999-999.png"


def test_rasterize_zero_area_returns_none(armbrust_pdf):
    if not _pdf_exists(armbrust_pdf):
        pytest.skip(f"missing sample PDF: {armbrust_pdf}")
    doc = pymupdf.open(armbrust_pdf)
    try:
        page = doc[0]
        result = rasterize_region(page, (100, 100, 100, 100), pad=0.0)
        assert result is None
    finally:
        doc.close()


def test_rasterize_bbox_entirely_outside_returns_none(armbrust_pdf):
    if not _pdf_exists(armbrust_pdf):
        pytest.skip(f"missing sample PDF: {armbrust_pdf}")
    doc = pymupdf.open(armbrust_pdf)
    try:
        page = doc[0]
        # Page widths in points are typically 612 (US Letter) or 595 (A4).
        # 1000+ is definitely outside.
        far_x = page.rect.x1 + 500
        far_y = page.rect.y1 + 500
        result = rasterize_region(
            page,
            (far_x, far_y, far_x + 100, far_y + 100),
            pad=0.0,
        )
        assert result is None
    finally:
        doc.close()


def test_rasterize_bbox_partially_outside_clamps(armbrust_pdf):
    if not _pdf_exists(armbrust_pdf):
        pytest.skip(f"missing sample PDF: {armbrust_pdf}")
    doc = pymupdf.open(armbrust_pdf)
    try:
        page = doc[0]
        # bbox starts off-page (negative) but extends into the page.
        png = rasterize_region(page, (-10, -10, 50, 50), pad=0.0)
        assert png is not None
        assert png.startswith(PNG_SIGNATURE)
        w, h = _png_dimensions(png)
        assert w > 0 and h > 0
    finally:
        doc.close()


def test_rasterize_happy_path_returns_png(armbrust_pdf):
    if not _pdf_exists(armbrust_pdf):
        pytest.skip(f"missing sample PDF: {armbrust_pdf}")
    doc = pymupdf.open(armbrust_pdf)
    try:
        page = doc[0]
        # 100x100 point box near top-left.
        bbox = (50.0, 50.0, 150.0, 150.0)
        png = rasterize_region(page, bbox, dpi=200, pad=0.0)
        assert png is not None
        assert png.startswith(PNG_SIGNATURE)
        w, h = _png_dimensions(png)
        # 100pt at 200dpi -> ~278 px. Aspect should be ~1.0.
        assert w > 100 and h > 100
        aspect = w / h
        assert 0.9 < aspect < 1.1
    finally:
        doc.close()


def test_rasterize_accepts_pymupdf_rect(armbrust_pdf):
    if not _pdf_exists(armbrust_pdf):
        pytest.skip(f"missing sample PDF: {armbrust_pdf}")
    doc = pymupdf.open(armbrust_pdf)
    try:
        page = doc[0]
        rect = pymupdf.Rect(50.0, 50.0, 150.0, 150.0)
        png = rasterize_region(page, rect, dpi=200, pad=0.0)
        assert png is not None
        assert png.startswith(PNG_SIGNATURE)
    finally:
        doc.close()


def test_rasterize_padding_enlarges_output(armbrust_pdf):
    if not _pdf_exists(armbrust_pdf):
        pytest.skip(f"missing sample PDF: {armbrust_pdf}")
    doc = pymupdf.open(armbrust_pdf)
    try:
        page = doc[0]
        bbox = (100.0, 100.0, 200.0, 200.0)
        no_pad = rasterize_region(page, bbox, dpi=200, pad=0.0)
        with_pad = rasterize_region(page, bbox, dpi=200, pad=3.0)
        assert no_pad is not None and with_pad is not None
        w0, h0 = _png_dimensions(no_pad)
        w1, h1 = _png_dimensions(with_pad)
        assert w1 > w0
        assert h1 > h0
    finally:
        doc.close()


def test_rasterize_full_page_rect(armbrust_pdf):
    if not _pdf_exists(armbrust_pdf):
        pytest.skip(f"missing sample PDF: {armbrust_pdf}")
    doc = pymupdf.open(armbrust_pdf)
    try:
        page = doc[0]
        png = rasterize_region(page, page.rect, dpi=100, pad=0.0)
        assert png is not None
        assert png.startswith(PNG_SIGNATURE)
        w, h = _png_dimensions(png)
        # Page aspect ratio should roughly match output aspect ratio.
        page_aspect = page.rect.width / page.rect.height
        img_aspect = w / h
        assert abs(page_aspect - img_aspect) < 0.05
    finally:
        doc.close()
