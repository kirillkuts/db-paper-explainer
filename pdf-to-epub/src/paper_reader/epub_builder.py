"""EPUB3 builder.

Takes an interleaved stream of `Heading | Paragraph | ImageRegion` events and
writes a valid EPUB3 file using `ebooklib`.

Event types are defined here locally so this module does not depend on peer
modules in the package. The orchestrator (U8) adapts whatever upstream events
exist into these types.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence, Union
from uuid import uuid4

from ebooklib import epub


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Heading:
    text: str
    level: int  # 1, 2, or 3


@dataclass(frozen=True)
class Paragraph:
    text: str


@dataclass(frozen=True)
class ImageRegion:
    png_bytes: bytes
    filename: str  # e.g. "img-p003-001.png"
    alt: str = ""


Event = Union[Heading, Paragraph, ImageRegion]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


CSS_CONTENT = """body { line-height: 1.5; }
img { max-width: 100%; height: auto; display: block; margin: 1em auto; }
figure { margin: 1em 0; }
h1, h2, h3 { line-height: 1.25; }
p { margin: 0.5em 0; }
"""


_CHAPTER_HEAD = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    "<!DOCTYPE html>\n"
    '<html xmlns="http://www.w3.org/1999/xhtml">\n'
    "<head>\n"
    "    <title>{title}</title>\n"
    '    <link rel="stylesheet" type="text/css" href="style/main.css"/>\n'
    "</head>\n"
    "<body>\n"
)
_CHAPTER_TAIL = "</body>\n</html>\n"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


@dataclass
class _ChapterDraft:
    title: str
    body_parts: list[str]
    images: list[ImageRegion]


def _new_chapter(title: str) -> _ChapterDraft:
    return _ChapterDraft(title=title, body_parts=[], images=[])


def _render_event_body(event: Event) -> str:
    """Render a single event into XHTML body fragment. Heading level 1 is
    rendered by the chapter header itself, so level-1 headings are not passed
    here."""
    if isinstance(event, Heading):
        # level 2/3 (level 1 is handled as chapter title)
        lvl = max(2, min(3, event.level))
        return f"<h{lvl}>{html.escape(event.text)}</h{lvl}>"
    if isinstance(event, Paragraph):
        return f"<p>{html.escape(event.text)}</p>"
    if isinstance(event, ImageRegion):
        alt = html.escape(event.alt)
        # The chapter XHTML lives at the EPUB root alongside the `images/`
        # directory created by EpubImage(file_name="images/<name>"), so the
        # relative href is `images/<name>` (no `../` prefix).
        return (
            f'<figure><img src="images/{html.escape(event.filename)}" '
            f'alt="{alt}"/></figure>'
        )
    raise TypeError(f"Unknown event type: {type(event).__name__}")


def _split_into_chapters(
    events: Sequence[Event], default_title: str
) -> list[_ChapterDraft]:
    """Group events into chapters keyed by level-1 Headings.

    - A level-1 Heading starts a new chapter (its text is the title).
    - Events before any level-1 Heading go into an implicit chapter named
      `default_title`. If there were no events at all, we still produce one
      empty chapter so the EPUB is valid.
    - Level-2/3 Headings, Paragraphs, and ImageRegions accumulate into the
      current chapter.
    """
    chapters: list[_ChapterDraft] = []
    current = _new_chapter(default_title)
    current_is_default = True  # is `current` the implicit pre-heading chapter?
    current_has_content = False

    for ev in events:
        if isinstance(ev, Heading) and ev.level == 1:
            # Flush the existing chapter unless it's an empty implicit
            # pre-heading chapter (we don't want a stub "Document" chapter
            # before the first real h1 when nothing preceded it).
            if not (current_is_default and not current_has_content):
                chapters.append(current)
            current = _new_chapter(ev.text)
            current_is_default = False
            current_has_content = False
            continue

        current.body_parts.append(_render_event_body(ev))
        if isinstance(ev, ImageRegion):
            current.images.append(ev)
        current_has_content = True

    # Always append the final chapter, even if empty — guarantees at least one
    # chapter exists.
    chapters.append(current)
    return chapters


def _build_chapter_xhtml(chapter: _ChapterDraft) -> str:
    title_escaped = html.escape(chapter.title)
    head = _CHAPTER_HEAD.format(title=title_escaped)
    h1 = f"    <h1>{title_escaped}</h1>\n"
    body = "".join(f"    {part}\n" for part in chapter.body_parts)
    return head + h1 + body + _CHAPTER_TAIL


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_epub(
    events: Iterable[Event],
    output_path: Union[str, Path],
    *,
    title: str = "Document",
    author: str = "",
    language: str = "en",
) -> None:
    """Write an EPUB3 file containing the rendered events.

    Behavior on edge cases:
    - Empty `events`: produces a valid EPUB with a single empty chapter
      titled `title` (default "Document"). Caller does not have to special-case.
    - No level-1 Heading in stream: produces a single chapter titled `title`.
    - Consecutive level-1 Headings with no body in between: each becomes its
      own chapter; the empty one contains only its `<h1>`.
    """
    events_list = list(events)
    chapters_drafts = _split_into_chapters(events_list, default_title=title)

    book = epub.EpubBook()
    book.set_identifier(str(uuid4()))
    book.set_title(title)
    book.set_language(language)
    if author:
        book.add_author(author)

    # CSS shared by all chapters.
    css_item = epub.EpubItem(
        uid="style_main",
        file_name="style/main.css",
        media_type="text/css",
        content=CSS_CONTENT.encode("utf-8"),
    )
    book.add_item(css_item)

    # Track which image filenames have been added — same image referenced
    # twice would otherwise duplicate the manifest entry.
    added_images: set[str] = set()
    chapter_items: list[epub.EpubHtml] = []

    for idx, draft in enumerate(chapters_drafts, start=1):
        file_name = f"chap_{idx:03d}.xhtml"
        chapter = epub.EpubHtml(
            title=draft.title,
            file_name=file_name,
            lang=language,
        )
        chapter.content = _build_chapter_xhtml(draft).encode("utf-8")
        chapter.add_item(css_item)
        book.add_item(chapter)
        chapter_items.append(chapter)

        for image in draft.images:
            if image.filename in added_images:
                continue
            added_images.add(image.filename)
            img_item = epub.EpubImage(
                uid=f"img_{image.filename}",
                file_name=f"images/{image.filename}",
                media_type="image/png",
                content=image.png_bytes,
            )
            book.add_item(img_item)

    book.toc = tuple(chapter_items)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", *chapter_items]

    epub.write_epub(str(output_path), book)
