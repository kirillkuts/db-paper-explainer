"""Tests for the EPUB builder (U7)."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from ebooklib import ITEM_DOCUMENT, ITEM_IMAGE, epub

from paper_reader.epub_builder import (
    Heading,
    ImageRegion,
    Paragraph,
    build_epub,
)


# Minimal 1x1 transparent PNG.
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6300010000050001a5f645400000000049454e44ae"
    "426082"
)


def _chapter_documents(book: epub.EpubBook) -> list[epub.EpubHtml]:
    """Return non-nav XHTML documents in spine order."""
    docs = []
    for spine_entry in book.spine:
        item_id = spine_entry[0] if isinstance(spine_entry, tuple) else spine_entry
        if item_id == "nav":
            continue
        item = book.get_item_with_id(item_id)
        if item is not None:
            docs.append(item)
    return docs


def _all_xhtml_documents(book: epub.EpubBook) -> list[epub.EpubHtml]:
    out = []
    for item in book.get_items_of_type(ITEM_DOCUMENT):
        # Skip nav doc (file_name typically "nav.xhtml").
        if getattr(item, "is_chapter", lambda: True)() is False:
            continue
        if item.file_name.startswith("nav"):
            continue
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Happy path 1: two level-1 chapters with paragraph + image
# ---------------------------------------------------------------------------


def test_two_chapters_with_image(tmp_path: Path) -> None:
    out = tmp_path / "two_chapters.epub"
    events = [
        Heading("Intro", 1),
        Paragraph("Hello world."),
        ImageRegion(png_bytes=TINY_PNG, filename="img-p000-000.png"),
        Heading("Methods", 1),
        Paragraph("Body two."),
    ]

    build_epub(events, out, title="Sample Paper")

    assert out.exists()
    assert out.stat().st_size > 1024

    book = epub.read_epub(str(out))
    chapters = _chapter_documents(book)
    assert len(chapters) == 2

    chap1 = chapters[0].content.decode("utf-8")
    chap2 = chapters[1].content.decode("utf-8")

    assert "Hello world." in chap1
    assert "<img" in chap1
    assert 'src="images/img-p000-000.png"' in chap1
    assert "Body two." in chap2
    assert "<img" not in chap2

    # The image PNG is in the manifest.
    images = list(book.get_items_of_type(ITEM_IMAGE))
    assert any(i.file_name == "images/img-p000-000.png" for i in images)


# ---------------------------------------------------------------------------
# Happy path 2: stream with no headings → single chapter named title
# ---------------------------------------------------------------------------


def test_no_headings_single_chapter(tmp_path: Path) -> None:
    out = tmp_path / "no_headings.epub"
    events = [Paragraph("Just text."), Paragraph("More text.")]

    build_epub(events, out)  # default title="Document"

    book = epub.read_epub(str(out))
    chapters = _chapter_documents(book)
    assert len(chapters) == 1

    body = chapters[0].content.decode("utf-8")
    assert "Just text." in body
    assert "More text." in body
    assert "<h1>Document</h1>" in body


# ---------------------------------------------------------------------------
# Happy path 3: subheadings inside a chapter
# ---------------------------------------------------------------------------


def test_subheadings_inside_chapter(tmp_path: Path) -> None:
    out = tmp_path / "subheadings.epub"
    events = [
        Heading("Intro", 1),
        Heading("Subsection", 2),
        Paragraph("X"),
    ]

    build_epub(events, out)

    book = epub.read_epub(str(out))
    chapters = _chapter_documents(book)
    assert len(chapters) == 1

    body = chapters[0].content.decode("utf-8")
    assert "<h1>Intro</h1>" in body
    assert "<h2>Subsection</h2>" in body
    assert "<p>X</p>" in body


# ---------------------------------------------------------------------------
# Edge case: empty events list → valid EPUB with one empty chapter
# ---------------------------------------------------------------------------


def test_empty_events_produces_valid_epub(tmp_path: Path) -> None:
    out = tmp_path / "empty.epub"
    build_epub([], out)

    assert out.exists()
    assert out.stat().st_size > 1024

    book = epub.read_epub(str(out))
    chapters = _chapter_documents(book)
    assert len(chapters) == 1
    body = chapters[0].content.decode("utf-8")
    # Only h1 with default title, no <p> or <img>.
    assert "<h1>Document</h1>" in body
    assert "<p>" not in body
    assert "<img" not in body


# ---------------------------------------------------------------------------
# Edge case: consecutive level-1 headings (empty section)
# ---------------------------------------------------------------------------


def test_consecutive_level1_headings(tmp_path: Path) -> None:
    out = tmp_path / "consecutive.epub"
    events = [
        Heading("A", 1),
        Heading("B", 1),
        Paragraph("Belongs to B"),
    ]

    build_epub(events, out)

    book = epub.read_epub(str(out))
    chapters = _chapter_documents(book)
    assert len(chapters) == 2

    body_a = chapters[0].content.decode("utf-8")
    body_b = chapters[1].content.decode("utf-8")

    assert "<h1>A</h1>" in body_a
    assert "<p>" not in body_a  # empty section beyond the h1
    assert "<h1>B</h1>" in body_b
    assert "Belongs to B" in body_b


# ---------------------------------------------------------------------------
# Edge case: special characters in paragraph text are properly escaped
# ---------------------------------------------------------------------------


def test_special_characters_escaped(tmp_path: Path) -> None:
    out = tmp_path / "special.epub"
    events = [Paragraph("5 < 10 & 'good' \"day\"")]

    build_epub(events, out)

    # Open as zip and inspect the chapter XHTML directly.
    with zipfile.ZipFile(out) as zf:
        chapter_names = [n for n in zf.namelist() if n.endswith(".xhtml") and "nav" not in n.lower()]
        assert chapter_names, "expected at least one chapter xhtml"
        # The chapter at chap_001.xhtml should contain the paragraph.
        target = next((n for n in chapter_names if "chap_001" in n), chapter_names[0])
        xhtml = zf.read(target).decode("utf-8")

    assert "&lt;" in xhtml
    assert "&amp;" in xhtml
    # The raw "<" of the paragraph text must not leak through unescaped.
    # We check the specific substring "5 < 10" never appears verbatim.
    assert "5 < 10" not in xhtml
    assert "5 &lt; 10" in xhtml

    # Round-trip via ebooklib too — the reader normalizes to text content.
    book = epub.read_epub(str(out))
    chapters = _chapter_documents(book)
    body = chapters[0].content.decode("utf-8")
    assert "5 &lt; 10 &amp;" in body


# ---------------------------------------------------------------------------
# Edge case: produced EPUB round-trips through read_epub without error
# ---------------------------------------------------------------------------


def test_read_epub_roundtrip(tmp_path: Path) -> None:
    out = tmp_path / "roundtrip.epub"
    events = [
        Heading("One", 1),
        Paragraph("alpha"),
        ImageRegion(png_bytes=TINY_PNG, filename="img-p001-000.png", alt="figure"),
        Heading("Two", 1),
        Heading("Sub", 2),
        Paragraph("beta"),
    ]

    build_epub(events, out, title="My Paper", author="Jane Doe")

    # Should not raise.
    book = epub.read_epub(str(out))
    assert book.get_metadata("DC", "title")[0][0] == "My Paper"
    # Author metadata present.
    creators = book.get_metadata("DC", "creator")
    assert any("Jane Doe" in c[0] for c in creators)


# ---------------------------------------------------------------------------
# Integration / file-shape: valid zip with container.xml
# ---------------------------------------------------------------------------


def test_epub_is_valid_zip_with_container(tmp_path: Path) -> None:
    out = tmp_path / "shape.epub"
    events = [Heading("Intro", 1), Paragraph("Hello.")]

    build_epub(events, out)

    assert out.exists()
    assert out.stat().st_size > 1024

    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert "META-INF/container.xml" in names
        # CSS asset is present.
        assert any(n.endswith("style/main.css") for n in names)
