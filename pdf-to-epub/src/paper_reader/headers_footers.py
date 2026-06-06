"""Detect and strip repeated page chrome (headers, footers, page numbers).

The detector inspects the topmost and bottommost text blocks on each page and
flags those that either:

1. Contain pure page-number text (matches ``^\\s*\\d+\\s*$``), OR
2. Have normalized text that appears on at least 60% of pages within the same
   y-band (top 10% of page height for header candidates, bottom 10% for footer
   candidates).

The caller passes blocks in whatever order it likes — the detector does not
assume reading order; it only inspects ``bbox`` coordinates and text content.
Block identities are returned as ``(page_index, block_index)`` tuples so the
caller can filter while preserving original indexing.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable

# Pure page number: optional whitespace, one or more digits, optional whitespace.
_PAGE_NUMBER_RE = re.compile(r"^\s*\d+\s*$")

# Fraction of page height that counts as "top band" or "bottom band".
_BAND_FRACTION = 0.10

# Minimum fraction of pages a normalized text must appear in to count as chrome.
_REPETITION_THRESHOLD = 0.60


def _block_text(block: dict) -> str:
    """Concatenate all span text within a block."""
    parts: list[str] = []
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            parts.append(span.get("text", ""))
    return "".join(parts)


def _normalize(text: str) -> str:
    """Normalize text for repetition matching: strip + lowercase + collapse whitespace."""
    return " ".join(text.split()).lower()


def _is_page_number(text: str) -> bool:
    return bool(_PAGE_NUMBER_RE.match(text))


def _block_y_range(block: dict) -> tuple[float, float] | None:
    """Return (y0, y1) of a block's bbox, or None if missing."""
    bbox = block.get("bbox")
    if not bbox or len(bbox) < 4:
        return None
    return float(bbox[1]), float(bbox[3])


def detect_header_footer_blocks(
    pages_blocks: list[list[dict]],
    page_heights: list[float],
) -> set[tuple[int, int]]:
    """Identify header/footer blocks across a document.

    Args:
        pages_blocks: One inner list per page. Each inner list contains block
            dicts in the structure returned by PyMuPDF's ``page.get_text("dict")``
            (i.e. with ``bbox`` and ``lines``/``spans``). Order within a page
            does not matter — block indices are preserved for the return value.
        page_heights: Page height (in PDF points) for each page, parallel to
            ``pages_blocks``.

    Returns:
        A set of ``(page_index, block_index)`` tuples identifying the blocks
        the caller should drop before further processing. Returns an empty set
        for empty input or for a single-page document (no repetition possible
        for the repetition rule, though pure page numbers are still flagged).
    """
    if not pages_blocks:
        return set()
    if len(pages_blocks) != len(page_heights):
        raise ValueError(
            f"pages_blocks length ({len(pages_blocks)}) does not match "
            f"page_heights length ({len(page_heights)})"
        )

    num_pages = len(pages_blocks)
    excluded: set[tuple[int, int]] = set()

    # Collect, per page, the indices and y-ranges of text blocks in top/bottom bands.
    # A "band candidate" is the topmost block (for header) or bottommost (for footer)
    # by y0 / y1, but only if it actually sits inside the band.
    header_candidates: list[tuple[int, int, str] | None] = []  # (page, block_idx, text)
    footer_candidates: list[tuple[int, int, str] | None] = []

    for page_idx, (blocks, page_height) in enumerate(zip(pages_blocks, page_heights)):
        top_band_max_y = page_height * _BAND_FRACTION
        bottom_band_min_y = page_height * (1.0 - _BAND_FRACTION)

        topmost: tuple[float, int, str] | None = None  # (y0, block_idx, text)
        bottommost: tuple[float, int, str] | None = None  # (y1, block_idx, text)

        for block_idx, block in enumerate(blocks):
            y_range = _block_y_range(block)
            if y_range is None:
                continue
            y0, y1 = y_range
            text = _block_text(block)
            if not text.strip():
                continue

            # Header candidate: block sits in top band (its y0 within top 10%).
            if y0 <= top_band_max_y:
                if topmost is None or y0 < topmost[0]:
                    topmost = (y0, block_idx, text)

            # Footer candidate: block sits in bottom band (its y1 within bottom 10%).
            if y1 >= bottom_band_min_y:
                if bottommost is None or y1 > bottommost[0]:
                    bottommost = (y1, block_idx, text)

        header_candidates.append(
            (page_idx, topmost[1], topmost[2]) if topmost else None
        )
        footer_candidates.append(
            (page_idx, bottommost[1], bottommost[2]) if bottommost else None
        )

    # Rule 1: pure page-number text in either band -> always flag.
    for candidate in header_candidates + footer_candidates:
        if candidate is None:
            continue
        page_idx, block_idx, text = candidate
        if _is_page_number(text):
            excluded.add((page_idx, block_idx))

    # Rule 2: normalized text repeats across >= 60% of pages within its band.
    # Tally counts per band separately so a body line that matches a header
    # block on a different page is not counted across bands.
    threshold_count = _REPETITION_THRESHOLD * num_pages

    def _flag_repeats(candidates: list[tuple[int, int, str] | None]) -> None:
        norm_to_positions: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for candidate in candidates:
            if candidate is None:
                continue
            page_idx, block_idx, text = candidate
            norm = _normalize(text)
            if not norm:
                continue
            norm_to_positions[norm].append((page_idx, block_idx))

        for norm, positions in norm_to_positions.items():
            if len(positions) >= threshold_count and len(positions) >= 2:
                excluded.update(positions)

    _flag_repeats(header_candidates)
    _flag_repeats(footer_candidates)

    return excluded


__all__ = ["detect_header_footer_blocks"]
