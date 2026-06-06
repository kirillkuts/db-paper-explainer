"""Tests for paper_reader.text — stitching, de-hyphenation, headings."""
from __future__ import annotations

from pathlib import Path

import pytest

from paper_reader.text import Heading, Paragraph, stitch_blocks


# ---------------------------------------------------------------------------
# Helpers to fabricate PyMuPDF-shaped block dicts.
# ---------------------------------------------------------------------------


def _span(text: str, size: float = 10.0, font: str = "Times-Roman") -> dict:
    return {"text": text, "size": size, "font": font}


def _line(*spans: dict) -> dict:
    return {"spans": list(spans)}


def _block(*lines: dict) -> dict:
    return {"type": 0, "lines": list(lines)}


# ---------------------------------------------------------------------------
# Stitching + de-hyphenation
# ---------------------------------------------------------------------------


def test_dehyphenation_lowercase_next_line():
    block = _block(
        _line(_span("Recent advances in column-")),
        _line(_span("based query engines have changed everything.")),
    )
    events = stitch_blocks([block])
    assert events == [
        Paragraph(
            text="Recent advances in column-based query engines have changed everything."
        )
    ]


def test_dehyphenation_not_applied_when_next_line_uppercase():
    """Conservative tiebreak: preserve hyphen, insert single space."""
    block = _block(
        _line(_span("in 2024-")),
        _line(_span("Quarter 1 results")),
    )
    events = stitch_blocks([block])
    assert len(events) == 1
    assert isinstance(events[0], Paragraph)
    assert events[0].text == "in 2024- Quarter 1 results"


def test_dehyphenation_not_applied_when_next_line_starts_with_digit():
    block = _block(
        _line(_span("section-")),
        _line(_span("3 covers this")),
    )
    events = stitch_blocks([block])
    assert events == [Paragraph(text="section- 3 covers this")]


def test_simple_two_line_paragraph_joined_with_space():
    block = _block(
        _line(_span("Hello world.")),
        _line(_span("This is a paragraph.")),
    )
    events = stitch_blocks([block])
    assert events == [Paragraph(text="Hello world. This is a paragraph.")]


def test_multiple_spans_within_a_line_are_concatenated():
    block = _block(
        _line(_span("Hello "), _span("world", font="Times-Italic"), _span(".")),
    )
    events = stitch_blocks([block])
    assert events == [Paragraph(text="Hello world.")]


# ---------------------------------------------------------------------------
# Heading detection
# ---------------------------------------------------------------------------


def test_size_based_heading_single_larger_size_becomes_level_1():
    heading = _block(_line(_span("1 Introduction", size=14.0)))
    body1 = _block(
        _line(_span("First paragraph of the introduction body.", size=10.0))
    )
    body2 = _block(_line(_span("Second body paragraph here.", size=10.0)))
    events = stitch_blocks([heading, body1, body2])

    assert events == [
        Heading(text="1 Introduction", level=1),
        Paragraph(text="First paragraph of the introduction body."),
        Paragraph(text="Second body paragraph here."),
    ]


def test_three_font_sizes_map_to_levels_1_and_2():
    h1 = _block(_line(_span("Big Section", size=16.0)))
    h2 = _block(_line(_span("Subsection", size=12.0)))
    # Multiple 10pt body blocks make 10pt the mode.
    body_a = _block(_line(_span("Body text one is fairly long here.", size=10.0)))
    body_b = _block(_line(_span("Body text two also long and prosaic.", size=10.0)))
    body_c = _block(_line(_span("Body text three to weight the mode.", size=10.0)))

    events = stitch_blocks([h1, h2, body_a, body_b, body_c])

    assert events[0] == Heading(text="Big Section", level=1)
    assert events[1] == Heading(text="Subsection", level=2)
    assert all(isinstance(e, Paragraph) for e in events[2:])


def test_long_block_in_larger_font_is_not_a_heading():
    """>10 words disqualifies even with a large font."""
    big_long = _block(
        _line(
            _span(
                "this sentence is much too long to qualify as a heading despite the larger font",
                size=14.0,
            )
        )
    )
    body = _block(_line(_span("normal body text appears here.", size=10.0)))
    events = stitch_blocks([big_long, body])
    assert all(isinstance(e, Paragraph) for e in events)


def test_single_word_in_body_font_is_paragraph_not_heading():
    body_block = _block(_line(_span("Note", size=10.0)))
    other = _block(_line(_span("Surrounding body text.", size=10.0)))
    events = stitch_blocks([body_block, other])
    assert events == [Paragraph(text="Note"), Paragraph(text="Surrounding body text.")]


def test_allcaps_short_standalone_is_heading_via_secondary_rule():
    abstract = _block(_line(_span("ABSTRACT", size=10.0)))
    body = _block(_line(_span("This paper presents a new system.", size=10.0)))
    events = stitch_blocks([abstract, body])

    # No size-based headings exist, so all-caps fallback should pick level 2.
    assert events[0] == Heading(text="ABSTRACT", level=2)
    assert events[1] == Paragraph(text="This paper presents a new system.")


def test_allcaps_when_size_headings_exist_uses_smallest_detected_level():
    h1 = _block(_line(_span("Title", size=16.0)))
    h2 = _block(_line(_span("Subhead", size=12.0)))
    caps = _block(_line(_span("APPENDIX", size=10.0)))
    body_a = _block(_line(_span("Body text one is long enough.", size=10.0)))
    body_b = _block(_line(_span("Body text two for mode weight.", size=10.0)))

    events = stitch_blocks([h1, h2, caps, body_a, body_b])
    # h1=1, h2=2, smallest detected heading level integer is 2.
    assert events[2] == Heading(text="APPENDIX", level=2)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty_list():
    assert stitch_blocks([]) == []


def test_block_with_no_lines_is_skipped():
    events = stitch_blocks([{"type": 0, "lines": []}])
    assert events == []


def test_block_with_empty_spans_is_skipped():
    block = {"type": 0, "lines": [{"spans": []}, {"spans": [{"text": ""}]}]}
    events = stitch_blocks([block])
    assert events == []


def test_block_with_blank_only_line_is_skipped():
    block = _block(_line(_span("   ")))
    assert stitch_blocks([block]) == []


# ---------------------------------------------------------------------------
# Integration: run against a real sample PDF when available.
# ---------------------------------------------------------------------------


def test_integration_armbrust_page1_yields_headings_and_paragraphs(
    armbrust_pdf: Path,
):
    if not armbrust_pdf.exists():
        pytest.skip(f"sample PDF not present: {armbrust_pdf}")

    fitz = pytest.importorskip("fitz")

    doc = fitz.open(str(armbrust_pdf))
    try:
        page = doc[0]
        raw = page.get_text("dict")
        text_blocks = [b for b in raw.get("blocks", []) if b.get("type") == 0]
    finally:
        doc.close()

    events = stitch_blocks(text_blocks)

    headings = [e for e in events if isinstance(e, Heading)]
    paragraphs = [e for e in events if isinstance(e, Paragraph)]

    assert len(headings) >= 1, "expected at least one heading on page 1"
    assert len(paragraphs) >= 2, "expected multiple paragraphs on page 1"

    # Manual-review aid — visible only under `pytest -s`.
    print("\nDetected headings on armbrust page 1:")
    for h in headings:
        print(f"  L{h.level}: {h.text}")
