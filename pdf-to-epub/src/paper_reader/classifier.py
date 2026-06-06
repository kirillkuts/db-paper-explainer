"""Block classifier: TEXT vs NON_TEXT region.

Given a PyMuPDF page and its block dicts, decide which blocks are clean
paragraph text (to be extracted as reflowable text) and which should be
rasterized as images (figures, tables, equations, anything math-heavy).

This module does not depend on other ``paper_reader`` modules. It accepts
raw PyMuPDF block dicts plus an optional list of vector-drawing rects so
the classifier is straightforward to unit-test without a real page.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Iterable, Sequence


class BlockKind(Enum):
    TEXT = "text"
    NON_TEXT = "non_text"


# Substring tokens that, when found inside a span font name (case-insensitive),
# indicate math typesetting. Computer Modern math families, AMS symbols,
# MathTime, plain Symbol, MathJax-rendered fonts.
_MATH_FONT_TOKENS = (
    "cmsy",
    "cmmi",
    "cmex",
    "msam",
    "msbm",
    "mtsy",
    "mtmi",
    "symbol",
    "mathjax",
)


def _is_math_char(ch: str) -> bool:
    """Return True if ch is in a Unicode block we treat as math/symbol."""
    cp = ord(ch)
    return (
        0x2200 <= cp <= 0x22FF  # Mathematical Operators
        or 0x2A00 <= cp <= 0x2AFF  # Supplemental Mathematical Operators
        or 0x0370 <= cp <= 0x03FF  # Greek and Coptic
        or 0x27C0 <= cp <= 0x27EF  # Miscellaneous Mathematical Symbols-A
        or 0x2980 <= cp <= 0x29FF  # Miscellaneous Mathematical Symbols-B
    )


_CAPTION_RE = re.compile(r"^\s*(figure|fig\.|table|algorithm)\s*\d+\s*[:.]", re.IGNORECASE)


def _block_text(block: dict) -> str:
    """Flatten all spans inside a text block into a single string."""
    parts: list[str] = []
    for line in block.get("lines", ()) or ():
        for span in line.get("spans", ()) or ():
            t = span.get("text")
            if t:
                parts.append(t)
    return "".join(parts)


def _block_fonts(block: dict) -> Iterable[str]:
    for line in block.get("lines", ()) or ():
        for span in line.get("spans", ()) or ():
            font = span.get("font")
            if font:
                yield font


def _bbox_area(bbox: Sequence[float]) -> float:
    x0, y0, x1, y1 = bbox
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def _bbox_intersection_area(a: Sequence[float], b: Sequence[float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0 = max(ax0, bx0)
    iy0 = max(ay0, by0)
    ix1 = min(ax1, bx1)
    iy1 = min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    return (ix1 - ix0) * (iy1 - iy0)


def _drawings_overlap_ratio(block_bbox: Sequence[float], drawing_rects: Sequence[Sequence[float]]) -> float:
    """Fraction of block area covered by the union (approx: sum) of drawing intersections.

    Approximation: we sum intersection areas. Vector drawings on a single
    figure typically overlap each other, so the sum can exceed the block
    area; clamp at 1.0. For the >20% threshold this approximation is fine.
    """
    area = _bbox_area(block_bbox)
    if area <= 0:
        return 0.0
    total = 0.0
    for r in drawing_rects:
        total += _bbox_intersection_area(block_bbox, r)
        if total >= area:
            return 1.0
    return total / area


def _has_math_font(block: dict) -> bool:
    for font in _block_fonts(block):
        f = font.lower()
        for tok in _MATH_FONT_TOKENS:
            if tok in f:
                return True
    return False


def _math_char_ratio(text: str) -> float:
    if not text:
        return 0.0
    non_ws = [c for c in text if not c.isspace()]
    if not non_ws:
        return 0.0
    math = sum(1 for c in non_ws if _is_math_char(c))
    return math / len(non_ws)


def _median_line_height(block: dict) -> float:
    heights: list[float] = []
    for line in block.get("lines", ()) or ():
        bbox = line.get("bbox")
        if bbox and len(bbox) == 4:
            h = bbox[3] - bbox[1]
            if h > 0:
                heights.append(h)
    if not heights:
        # Fall back to block height divided by line count, else a typical body size.
        bbox = block.get("bbox")
        nlines = max(1, len(block.get("lines", ()) or ()))
        if bbox and len(bbox) == 4:
            bh = (bbox[3] - bbox[1]) / nlines
            if bh > 0:
                return bh
        return 12.0
    heights.sort()
    return heights[len(heights) // 2]


def _looks_like_caption(text: str) -> bool:
    head = text[:30]
    return bool(_CAPTION_RE.match(head))


def _drawings_from_page(page) -> list[tuple[float, float, float, float]]:
    """Best-effort extraction of vector-drawing bboxes from a PyMuPDF page.

    Returns an empty list if the page has no drawings or the method is
    unavailable (e.g., in unit tests with a stub page).
    """
    if page is None:
        return []
    getter = getattr(page, "get_drawings", None)
    if getter is None:
        return []
    try:
        drawings = getter() or []
    except Exception:
        return []
    rects: list[tuple[float, float, float, float]] = []
    for d in drawings:
        rect = d.get("rect") if isinstance(d, dict) else None
        if rect is None:
            continue
        # PyMuPDF returns a fitz.Rect; convert to a 4-tuple.
        try:
            rects.append((float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3])))
        except Exception:
            continue
    return rects


def classify_blocks(
    page,
    blocks: list[dict],
    drawings: Sequence[Sequence[float]] | None = None,
) -> list[tuple[BlockKind, dict]]:
    """Classify each block as TEXT or NON_TEXT.

    Parameters
    ----------
    page:
        A PyMuPDF ``Page`` or any object exposing ``get_drawings()``. May be
        ``None`` if ``drawings`` is provided explicitly (useful for tests).
    blocks:
        Ordered list of block dicts (as returned by U2 / ``page.get_text("dict")``).
    drawings:
        Optional explicit list of vector-drawing bboxes. If omitted, drawings
        are pulled from ``page.get_drawings()``. Pass an empty list to
        suppress the drawing-overlap signal.

    Returns
    -------
    list of (BlockKind, block_dict) in the same order as the input.
    """
    if drawings is None:
        drawing_rects = _drawings_from_page(page)
    else:
        drawing_rects = [tuple(r) for r in drawings]

    # First pass: signals 1-4.
    kinds: list[BlockKind] = []
    for block in blocks:
        kinds.append(_classify_one(block, drawing_rects))

    # Second pass: caption-adjacency. Any TEXT block whose head matches the
    # caption regex and which sits within ~1.5x its median line height of a
    # NON_TEXT block becomes NON_TEXT.
    for i, block in enumerate(blocks):
        if kinds[i] is not BlockKind.TEXT:
            continue
        text = _block_text(block)
        if not _looks_like_caption(text):
            continue
        bbox = block.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        threshold = 1.5 * _median_line_height(block)
        bx0, by0, bx1, by1 = bbox
        for j, other in enumerate(blocks):
            if i == j or kinds[j] is not BlockKind.NON_TEXT:
                continue
            obbox = other.get("bbox")
            if not obbox or len(obbox) != 4:
                continue
            ox0, oy0, ox1, oy1 = obbox
            # Vertical gap: distance between the two bboxes on the y axis.
            if by0 >= oy1:
                gap = by0 - oy1  # caption sits below figure
            elif oy0 >= by1:
                gap = oy0 - by1  # caption sits above figure
            else:
                gap = 0.0  # overlapping vertically
            if gap > threshold:
                continue
            # Require some horizontal overlap so we don't merge across columns.
            x_overlap = min(bx1, ox1) - max(bx0, ox0)
            if x_overlap <= 0:
                continue
            kinds[i] = BlockKind.NON_TEXT
            break

    return list(zip(kinds, blocks))


def _classify_one(block: dict, drawing_rects: Sequence[Sequence[float]]) -> BlockKind:
    # Signal 1: PyMuPDF image block.
    if block.get("type") == 1:
        return BlockKind.NON_TEXT

    # Signal 2: vector-drawing overlap.
    bbox = block.get("bbox")
    if bbox and len(bbox) == 4 and drawing_rects:
        if _drawings_overlap_ratio(bbox, drawing_rects) > 0.20:
            return BlockKind.NON_TEXT

    # Signal 3: math-font spans.
    if _has_math_font(block):
        return BlockKind.NON_TEXT

    # Signal 4: math-char ratio.
    text = _block_text(block)
    if _math_char_ratio(text) > 0.05:
        return BlockKind.NON_TEXT

    return BlockKind.TEXT
