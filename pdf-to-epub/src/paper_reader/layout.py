"""Page layout analysis: column detection and reading-order sorting."""
from __future__ import annotations

from typing import Any

# A block whose bbox spans the page midpoint by more than this fraction
# of the page width on each side is considered "full-width" (e.g., titles,
# section headers spanning both columns) and ordered purely by y0.
FULL_WIDTH_OVERHANG_FRAC = 0.20

# Gap between the two column-center clusters must be at least this fraction
# of the page width for the page to be classified as 2-column.
MIN_COLUMN_GAP_FRAC = 0.05


def _bbox(block: dict) -> tuple[float, float, float, float]:
    return tuple(block["bbox"])  # type: ignore[return-value]


def _center_x(block: dict) -> float:
    x0, _, x1, _ = _bbox(block)
    return (x0 + x1) / 2.0


def _y0(block: dict) -> float:
    return _bbox(block)[1]


def _page_bounds(page_rect: Any) -> tuple[float, float]:
    """Return (x0, width) tolerating both PyMuPDF Rect and plain tuples."""
    if hasattr(page_rect, "x0") and hasattr(page_rect, "width"):
        return float(page_rect.x0), float(page_rect.width)
    x0, _y0v, x1, _y1 = page_rect
    return float(x0), float(x1 - x0)


def _is_full_width(block: dict, page_x0: float, page_width: float) -> bool:
    x0, _, x1, _ = _bbox(block)
    midx = page_x0 + page_width / 2.0
    overhang = page_width * FULL_WIDTH_OVERHANG_FRAC
    return (midx - x0) >= overhang and (x1 - midx) >= overhang


def detect_column_count(blocks: list[dict], page_rect: Any) -> int:
    """Return 1 or 2 based on block-center x clustering."""
    page_x0, page_width = _page_bounds(page_rect)
    if page_width <= 0:
        return 1

    # Ignore full-width blocks (titles, banners) when clustering.
    candidates = [b for b in blocks if not _is_full_width(b, page_x0, page_width)]
    if len(candidates) < 2:
        return 1

    midx = page_x0 + page_width / 2.0
    left_centers = [_center_x(b) for b in candidates if _center_x(b) < midx]
    right_centers = [_center_x(b) for b in candidates if _center_x(b) >= midx]
    if not left_centers or not right_centers:
        return 1

    gap = min(right_centers) - max(left_centers)
    if gap >= MIN_COLUMN_GAP_FRAC * page_width:
        return 2
    return 1


def order_blocks(page: Any) -> list[dict]:
    """Return blocks from `page` in human reading order.

    Accepts a PyMuPDF page or any object exposing `.get_text("dict")` and
    `.rect`. Full-width blocks (e.g., titles spanning the midpoint) are
    sorted by y0 alongside column blocks rather than forced into a column.
    """
    blocks = list(page.get_text("dict").get("blocks", []))
    if not blocks:
        return []

    page_x0, page_width = _page_bounds(page.rect)
    columns = detect_column_count(blocks, page.rect)

    if columns == 1:
        return sorted(blocks, key=_y0)

    midx = page_x0 + page_width / 2.0
    full_width: list[dict] = []
    left: list[dict] = []
    right: list[dict] = []
    for b in blocks:
        if _is_full_width(b, page_x0, page_width):
            full_width.append(b)
        elif _center_x(b) < midx:
            left.append(b)
        else:
            right.append(b)

    left.sort(key=_y0)
    right.sort(key=_y0)

    if not full_width:
        return left + right

    # Full-width blocks split the page into horizontal bands. Within each
    # band we apply the standard left-column-then-right-column ordering.
    full_width.sort(key=_y0)
    result: list[dict] = []
    li = ri = 0
    for fw in full_width:
        fw_y = _y0(fw)
        while li < len(left) and _y0(left[li]) <= fw_y:
            result.append(left[li])
            li += 1
        while ri < len(right) and _y0(right[ri]) <= fw_y:
            result.append(right[ri])
            ri += 1
        result.append(fw)
    result.extend(left[li:])
    result.extend(right[ri:])
    return result
