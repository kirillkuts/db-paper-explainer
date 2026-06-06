"""Crop a page region to PNG bytes for EPUB embedding."""
from __future__ import annotations

from typing import Optional, Tuple, Union

import pymupdf


BBoxLike = Union[Tuple[float, float, float, float], "pymupdf.Rect"]


def _to_tuple(bbox: BBoxLike) -> Tuple[float, float, float, float]:
    if isinstance(bbox, pymupdf.Rect):
        return (bbox.x0, bbox.y0, bbox.x1, bbox.y1)
    x0, y0, x1, y1 = bbox
    return (float(x0), float(y0), float(x1), float(y1))


def rasterize_region(
    page,
    bbox: BBoxLike,
    dpi: int = 200,
    pad: float = 3.0,
) -> Optional[bytes]:
    """Render `bbox` (clamped + padded) on `page` to PNG bytes.

    Returns None for empty/zero-area bboxes (including bboxes that fall
    entirely outside the page rect after clamping).
    """
    x0, y0, x1, y1 = _to_tuple(bbox)

    # Normalize possibly-inverted coordinates.
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0

    # Pad outward.
    x0 -= pad
    y0 -= pad
    x1 += pad
    y1 += pad

    page_rect = page.rect
    # Clamp to page rect.
    cx0 = max(x0, page_rect.x0)
    cy0 = max(y0, page_rect.y0)
    cx1 = min(x1, page_rect.x1)
    cy1 = min(y1, page_rect.y1)

    if cx1 - cx0 <= 0 or cy1 - cy0 <= 0:
        return None

    clip = pymupdf.Rect(cx0, cy0, cx1, cy1)
    pix = page.get_pixmap(clip=clip, dpi=dpi)
    return pix.tobytes("png")


def image_filename(page_index: int, region_index: int) -> str:
    """Stable, sortable filename for a rasterized region asset."""
    return f"img-p{page_index:03d}-{region_index:03d}.png"
