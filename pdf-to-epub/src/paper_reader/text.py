"""Text stitching, de-hyphenation, and heading detection.

Turns a list of TEXT-classified PyMuPDF block dicts into a flat stream of
``Heading`` and ``Paragraph`` events for downstream EPUB rendering.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True)
class Heading:
    text: str
    level: int  # 1, 2, or 3


@dataclass(frozen=True)
class Paragraph:
    text: str


Event = Union[Heading, Paragraph]


# ---------------------------------------------------------------------------
# Body-size detection
# ---------------------------------------------------------------------------


def _body_size(blocks: list[dict]) -> float:
    """Most common span size, weighted by character count.

    Sizes are rounded to 0.1pt to bucket near-identical values together.
    Falls back to 10.0pt when no spans are present.
    """
    counter: Counter[float] = Counter()
    for block in blocks:
        for line in block.get("lines", []) or []:
            for span in line.get("spans", []) or []:
                text = span.get("text", "") or ""
                if not text:
                    continue
                size = round(float(span.get("size", 0.0)), 1)
                counter[size] += len(text)
    if not counter:
        return 10.0
    return counter.most_common(1)[0][0]


def _dominant_block_size(block: dict) -> float:
    """Char-weighted dominant span size for a single block."""
    counter: Counter[float] = Counter()
    for line in block.get("lines", []) or []:
        for span in line.get("spans", []) or []:
            text = span.get("text", "") or ""
            if not text:
                continue
            counter[round(float(span.get("size", 0.0)), 1)] += len(text)
    if not counter:
        return 0.0
    return counter.most_common(1)[0][0]


# ---------------------------------------------------------------------------
# Block -> text stitching with de-hyphenation
# ---------------------------------------------------------------------------


def _line_text(line: dict) -> str:
    """Concatenate all span texts within a line."""
    return "".join((span.get("text", "") or "") for span in line.get("spans", []) or [])


def _stitch_block_text(block: dict) -> str:
    """Join the lines of a block into a single paragraph string.

    De-hyphenation rule: when a line ends with ``-`` AND the next line starts
    with a lowercase letter, treat the hyphen as part of a (possibly real)
    compound word — keep the hyphen and join the two lines with NO space.
    This is the conservative tiebreak: we can't reliably distinguish a
    line-break hyphenation from a true compound, and preserving the hyphen
    is harmless for the line-break case (e.g. "column-based" is correct;
    "columnbased" would be wrong if the source was a real compound).

    Otherwise (uppercase, digit, symbol, or any non-lowercase next line) the
    hyphen is preserved and a single space separates the lines.
    """
    lines = [
        _line_text(line).rstrip("\n")
        for line in (block.get("lines", []) or [])
    ]
    # Filter empty lines but preserve order.
    lines = [ln for ln in lines if ln.strip() != ""]
    if not lines:
        return ""

    out = lines[0].rstrip()
    for nxt in lines[1:]:
        nxt_stripped = nxt.lstrip()
        if not nxt_stripped:
            continue
        if out.endswith("-") and nxt_stripped[:1].islower():
            # Preserve hyphen, glue with no space — see docstring tiebreak.
            out = out + nxt_stripped
        else:
            out = out + " " + nxt_stripped
    return out.strip()


# ---------------------------------------------------------------------------
# Heading detection
# ---------------------------------------------------------------------------


def _is_size_heading(dom_size: float, body: float, word_count: int) -> bool:
    if word_count == 0 or word_count > 10:
        return False
    return dom_size >= body + 1.0 or dom_size >= body * 1.15


def _is_allcaps_heading(text: str, line_count: int) -> bool:
    """Secondary heading rule: all-caps, short, single-line."""
    if line_count != 1:
        return False
    words = text.split()
    if not words or len(words) > 6:
        return False
    alpha = [c for c in text if c.isalpha()]
    if not alpha:
        return False
    upper = sum(1 for c in alpha if c.isupper())
    return (upper / len(alpha)) > 0.9


def _heading_level_map(
    blocks: list[dict], body: float
) -> dict[float, int]:
    """Map each 'larger-than-body' size bucket to a heading level (1..3).

    Largest distinct bucket -> level 1, next -> level 2, etc. Capped at 3.
    """
    larger: set[float] = set()
    for block in blocks:
        dom = _dominant_block_size(block)
        if dom == 0.0:
            continue
        # Bucket to 0.5pt to coalesce near-identical sizes.
        bucket = round(dom * 2) / 2
        # Compare against the *unbucketed* body size, but the heading-trigger
        # check below uses the original dom too. Here we only collect candidate
        # buckets that are clearly larger than body.
        if bucket >= body + 1.0 or bucket >= body * 1.15:
            larger.add(bucket)
    ordered = sorted(larger, reverse=True)
    return {size: min(i + 1, 3) for i, size in enumerate(ordered)}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def stitch_blocks_indexed(text_blocks: list[dict]) -> list[tuple[int, Event]]:
    """Like ``stitch_blocks`` but pairs each event with its source block index.

    Blocks that yield no event (empty / whitespace-only) are simply omitted
    from the result. Lets callers interleave non-text events at the right
    document position.
    """
    if not text_blocks:
        return []

    body = _body_size(text_blocks)
    size_levels = _heading_level_map(text_blocks, body)
    fallback_caps_level = max(size_levels.values()) if size_levels else 2

    out: list[tuple[int, Event]] = []
    for idx, block in enumerate(text_blocks):
        text = _stitch_block_text(block)
        if not text:
            continue

        line_count = sum(
            1
            for line in (block.get("lines", []) or [])
            if _line_text(line).strip() != ""
        )
        word_count = len(text.split())
        dom = _dominant_block_size(block)
        dom_bucket = round(dom * 2) / 2

        if _is_size_heading(dom, body, word_count):
            level = size_levels.get(dom_bucket, 3)
            out.append((idx, Heading(text=text, level=level)))
            continue

        if _is_allcaps_heading(text, line_count):
            out.append((idx, Heading(text=text, level=fallback_caps_level)))
            continue

        out.append((idx, Paragraph(text=text)))

    return out


def stitch_blocks(text_blocks: list[dict]) -> list[Event]:
    """Convert TEXT-classified block dicts into a flat ``Heading|Paragraph`` stream."""
    return [event for _, event in stitch_blocks_indexed(text_blocks)]
