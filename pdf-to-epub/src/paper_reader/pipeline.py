"""End-to-end pipeline: PDF -> EPUB.

Wires the per-module pieces together. Keep this module readable as the
top-level recipe; module-internal logic stays in the module that owns it.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable

import pymupdf

from .classifier import BlockKind, classify_blocks
from .epub_builder import Heading, ImageRegion, Paragraph, build_epub
from .headers_footers import detect_header_footer_blocks
from .layout import order_blocks
from .rasterizer import image_filename, rasterize_region
from .text import Heading as TextHeading
from .text import Paragraph as TextParagraph
from .text import stitch_blocks_indexed


class ConversionError(Exception):
    """Raised when conversion cannot proceed (e.g., no text layer)."""


def _block_has_visible_text(block: dict) -> bool:
    for line in block.get("lines", []) or []:
        for span in line.get("spans", []) or []:
            if span.get("text", "").strip():
                return True
    return False


def _to_epub_event(event):
    if isinstance(event, TextHeading):
        return Heading(text=event.text, level=event.level)
    if isinstance(event, TextParagraph):
        return Paragraph(text=event.text)
    raise TypeError(f"Unexpected event type from text.stitch_blocks: {type(event)!r}")


def _document_title_from(doc: pymupdf.Document, fallback: str) -> str:
    meta = doc.metadata or {}
    title = (meta.get("title") or "").strip()
    return title or fallback


def convert_pdf_to_epub(
    pdf_path: Path | str,
    epub_path: Path | str,
    *,
    dpi: int = 200,
    title: str | None = None,
    author: str = "",
    language: str = "en",
    verbose: bool = False,
) -> None:
    """Convert a single PDF to an EPUB file.

    Raises ConversionError if the PDF cannot be processed (e.g., no text layer).
    """
    pdf_path = Path(pdf_path)
    epub_path = Path(epub_path)

    if not pdf_path.exists():
        raise ConversionError(f"Input PDF not found: {pdf_path}")

    doc = pymupdf.open(pdf_path)
    try:
        if doc.page_count == 0:
            raise ConversionError("PDF has zero pages")

        # Pass 1: gather raw text blocks per page for header/footer detection.
        pages_text_blocks_raw: list[list[dict]] = []
        page_heights: list[float] = []
        any_text = False
        for page in doc:
            raw_blocks = page.get_text("dict").get("blocks", [])
            text_only = [b for b in raw_blocks if b.get("type") == 0]
            if any(_block_has_visible_text(b) for b in text_only):
                any_text = True
            pages_text_blocks_raw.append(text_only)
            page_heights.append(float(page.rect.height))

        if not any_text:
            raise ConversionError(
                "No extractable text found. Scanned/image-only PDFs are not supported."
            )

        excluded_ids: set[int] = set()
        excluded = detect_header_footer_blocks(pages_text_blocks_raw, page_heights)
        for page_idx, block_idx in excluded:
            if 0 <= page_idx < len(pages_text_blocks_raw):
                page_blocks = pages_text_blocks_raw[page_idx]
                if 0 <= block_idx < len(page_blocks):
                    excluded_ids.add(id(page_blocks[block_idx]))

        if verbose:
            print(
                f"Pages: {doc.page_count}, excluded header/footer blocks: {len(excluded_ids)}",
                file=sys.stderr,
            )

        # Pass 2: walk pages in reading order, classify, and produce events.
        all_events: list[Heading | Paragraph | ImageRegion] = []
        for page_idx, page in enumerate(doc):
            ordered = order_blocks(page)
            visible = [b for b in ordered if id(b) not in excluded_ids]
            classified = classify_blocks(page, visible)

            # Split into text/non-text in the same reading order.
            page_text_blocks: list[dict] = []
            slots: list[tuple[str, int]] = []  # ("text", idx_into_page_text_blocks) | ("image", image_idx)
            page_images: list[tuple[tuple, str]] = []  # (bbox, filename)

            for kind, block in classified:
                if kind == BlockKind.TEXT:
                    if _block_has_visible_text(block):
                        slots.append(("text", len(page_text_blocks)))
                        page_text_blocks.append(block)
                else:
                    region_idx = len(page_images)
                    filename = image_filename(page_idx, region_idx)
                    bbox = tuple(block["bbox"])
                    slots.append(("image", region_idx))
                    page_images.append((bbox, filename))

            # Stitch this page's text blocks together (consistent body-size per page).
            stitched = stitch_blocks_indexed(page_text_blocks)
            text_idx_to_event = {idx: ev for idx, ev in stitched}

            # Walk slots in document order, emitting events.
            for kind, ref in slots:
                if kind == "text":
                    text_event = text_idx_to_event.get(ref)
                    if text_event is not None:
                        all_events.append(_to_epub_event(text_event))
                else:
                    bbox, filename = page_images[ref]
                    png_bytes = rasterize_region(page, bbox, dpi=dpi)
                    if png_bytes is None:
                        if verbose:
                            print(
                                f"  page {page_idx}: skipped image at {bbox} (empty after clamp)",
                                file=sys.stderr,
                            )
                        continue
                    all_events.append(
                        ImageRegion(
                            png_bytes=png_bytes,
                            filename=filename,
                            alt=f"Figure or equation from page {page_idx + 1}",
                        )
                    )

        if verbose:
            n_h = sum(1 for e in all_events if isinstance(e, Heading))
            n_p = sum(1 for e in all_events if isinstance(e, Paragraph))
            n_i = sum(1 for e in all_events if isinstance(e, ImageRegion))
            print(
                f"Events: {n_h} headings, {n_p} paragraphs, {n_i} images",
                file=sys.stderr,
            )

        resolved_title = title or _document_title_from(doc, fallback=pdf_path.stem)
        build_epub(
            all_events,
            epub_path,
            title=resolved_title,
            author=author,
            language=language,
        )
    finally:
        doc.close()
