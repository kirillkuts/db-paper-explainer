---
title: "feat: 2-column PDF to EPUB converter for iPad reading"
type: feat
status: active
date: 2026-05-10
---

# feat: 2-column PDF to EPUB converter for iPad reading

## Overview

Build a Python CLI that converts 2-column scientific PDFs into reflowable EPUB files readable on iPad (Apple Books, KOReader). Text is extracted in correct reading order and reflowed so font size can be adjusted; figures, tables, and equations are rasterized as PNG and embedded as images so they remain legible without trying to reflow complex layouts.

The hard problems in PDF→EPUB conversion (broken math fonts, layout-dependent tables, vector figures) are sidestepped by treating "anything that isn't a clean text paragraph" as an image region cropped from the PDF and embedded.

---

## Problem Frame

Scientific papers are typeset in fixed-width 2-column PDFs. On a small screen (iPad), this forces zoom-and-pan reading because text doesn't reflow and font size is fixed. The user wants to read these papers comfortably on iPad with adjustable font size, same as a book.

Existing tooling either over-engineers the problem (ML-based extraction like Marker/GROBID — heavy deps, slow on first run) or under-delivers (Calibre's PDF→EPUB mangles 2-column layouts, garbles math). A focused tool that handles 2-column reading order well and gives up gracefully on hard regions (by rasterizing them) is the sweet spot.

---

## Requirements Trace

- R1. Convert a 2-column PDF paper to a valid EPUB file via a CLI command.
- R2. Body text reflows so iPad readers can change font size.
- R3. Reading order is correct for 2-column layouts: left column top-to-bottom, then right column top-to-bottom, across all pages.
- R4. Figures, tables, and equations appear in the EPUB at the right position in the reading flow, as embedded PNG images cropped from the source PDF.
- R5. Output EPUB opens and renders correctly in Apple Books on iPad (primary target) and is at least standards-valid (epubcheck-clean) for portability.
- R6. The tool runs locally without paid APIs or GPU requirements.
- R7. Handles the sample PDFs in `input/` end-to-end without crashing.

---

## Scope Boundaries

- Not extracting structured citations or building a bibliography graph — references section is rendered as plain text (or rasterized if formatting is tight).
- Not doing OCR — input is assumed to have a real text layer. Image-only scanned PDFs are out of scope.
- Not preserving exact paper typography (fonts, kerning, column rules). Goal is *readable*, not *replica*.
- Not handling 3+ column layouts, landscape pages, or rotated text in v1.
- Not extracting math as MathML/LaTeX — equations are rasterized images.
- Not building a GUI — CLI only.
- Not supporting batch/recursive directory conversion in v1 — one PDF in, one EPUB out.

### Deferred to Follow-Up Work

- Batch conversion of a directory of PDFs: separate iteration once single-file conversion is solid.
- MathML extraction for true reflowable equations: would require a different toolchain (e.g., MathPix API, Nougat); revisit if image-equations prove painful in practice.
- Reference link extraction (clickable in-text citations → bibliography): nice-to-have, not core to readability.

---

## Context & Research

### Relevant Code and Patterns

- No existing code in the repo. Greenfield. Sample input PDFs in `input/`: `armbrust-cidr21.pdf`, `p148-zeng (1).pdf`, `p2132-afroozeh.pdf`, `p2679-pedreira.pdf`, `p3044-liu.pdf` — use these as the conversion test corpus.

### Institutional Learnings

- None — no `docs/solutions/` in this repo yet.

### External References

- **PyMuPDF (`fitz`)** — `page.get_text("dict")` returns text blocks with bounding boxes and font metadata; `page.get_pixmap(clip=bbox, dpi=N)` rasterizes any rectangular region of a page to PNG. This is the foundation of both extraction and rasterization paths.
- **ebooklib** — standard Python library for writing EPUB3 files. Handles spine, manifest, NCX, images, CSS.
- **EPUB image embedding** — PNG is universally supported. `<img>` with `max-width: 100%; height: auto;` in CSS gives correct scaling on iPad. Apple Books supports tap-to-zoom on embedded images natively.
- **Reading order for 2-column papers** — well-known heuristic: split page bbox at horizontal midpoint, assign each text block to a column by block-center x-coordinate, sort within each column by y-coordinate, then concatenate left column → right column. Handles >95% of standard ACM/IEEE/USENIX templates correctly.

---

## Key Technical Decisions

- **Language: Python 3.11+.** PyMuPDF and ebooklib are the de facto Python tools for this domain. No reason to fight that.
- **PyMuPDF over pdfplumber.** PyMuPDF is faster, gives better block-level data (font, color, drawings), and handles rasterization in the same library — no second dependency for image output.
- **Rasterize non-text aggressively in v1.** If classification is uncertain (e.g., block contains math fonts, contains vector drawings, sits next to a figure caption, or has irregular line spacing), prefer rasterizing over a broken text extraction. Readability > extraction completeness.
- **EPUB3, single XHTML chapter per source section.** Simpler to debug than per-page chapters; matches how readers expect to navigate a paper (Intro, Methods, …). Section detection is heuristic (font-size-based heading detection); fall back to single chapter if detection fails.
- **Image DPI: 200.** High enough to look crisp on retina iPad after CSS scaling, low enough that EPUB file size stays reasonable (~tens of MB).
- **Strip repeated headers/footers.** Detect text blocks that appear in the same y-region on most pages with identical or near-identical content (page numbers, journal headers) and drop them before reading-order assembly.
- **De-hyphenation at line wraps.** When a text line ends with `-` and the next line begins with a lowercase letter, join without the hyphen. Conservative — preserve hyphen for compound words.
- **CLI framework: argparse, not click.** Stdlib, no extra dep, sufficient for a 2-flag CLI.

---

## Open Questions

### Resolved During Planning

- Should equations be MathML or images? → Images. (See Key Technical Decisions; MathML deferred.)
- One EPUB chapter per PDF page or per detected section? → Per detected section, fallback to single chapter.
- What DPI for rasterized regions? → 200.
- Which EPUB library? → ebooklib.

### Deferred to Implementation

- Exact heuristic for detecting "this text block is part of an equation/figure caption and should be rasterized with its neighbor": will tune against sample PDFs during U3 implementation.
- How aggressively to merge adjacent non-text blocks into a single rasterized region (e.g., a figure + its caption + a sub-label): defer until we see how the samples come out.
- Whether section-heading detection by font size is reliable enough across the 5 sample papers, or whether we need a fallback like "treat all-caps short lines as headings": will know after U6.

---

## Output Structure

```
paper-reader/
├── pyproject.toml
├── README.md
├── src/
│   └── paper_reader/
│       ├── __init__.py
│       ├── __main__.py          # python -m paper_reader entrypoint
│       ├── cli.py                # argparse, orchestration
│       ├── layout.py             # column detection, reading-order sort
│       ├── classifier.py         # text vs non-text block classification
│       ├── rasterizer.py         # crop bbox → PNG bytes
│       ├── text.py               # paragraph stitching, de-hyphenation, heading detection
│       ├── headers_footers.py   # detect & strip repeated chrome
│       └── epub_builder.py       # ebooklib wiring, CSS, metadata
├── tests/
│   ├── test_layout.py
│   ├── test_classifier.py
│   ├── test_text.py
│   ├── test_headers_footers.py
│   ├── test_epub_builder.py
│   └── test_integration.py       # full pipeline on sample PDFs
├── input/                        # existing — sample PDFs
└── output/                       # generated EPUBs (gitignored)
```

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification.*

```
PDF file
   │
   ▼
[PyMuPDF open] ──► list of pages, each with blocks (text + drawings + images)
   │
   ▼
[Header/footer stripper] ──► remove repeated chrome per page
   │
   ▼
[Layout analyzer]
   │   for each page:
   │     - detect 1 vs 2 columns (block-x-centroid clustering)
   │     - assign each block to a column
   │     - sort blocks within column by y
   │     - concat left→right
   ▼
ordered list of blocks (page-wise, then across pages)
   │
   ▼
[Classifier]
   │   for each block: TEXT | NON_TEXT_REGION
   │   NON_TEXT if:
   │     - contains math fonts
   │     - is a figure/table caption-adjacent block
   │     - block bbox overlaps vector drawings
   │     - high ratio of Unicode math/symbol chars
   ▼
sequence of (TEXT_BLOCK | NON_TEXT_REGION_BBOX)
   │
   ▼
[Rasterizer]            [Text stitcher]
crop NON_TEXT bboxes    join TEXT blocks into paragraphs,
to PNG @ 200dpi         de-hyphenate, detect headings
   │                      │
   └──────────┬───────────┘
              ▼
       interleaved stream of (Heading | Paragraph | Image)
              │
              ▼
       [EPUB builder] ── XHTML chapters + CSS + images → EPUB3 file
```

---

## Implementation Units

- U1. **Project scaffolding and dependency setup**

**Goal:** Stand up a runnable Python project with the dependency surface needed for the rest of the work.

**Requirements:** R6, R7

**Dependencies:** None

**Files:**
- Create: `pyproject.toml`
- Create: `src/paper_reader/__init__.py`
- Create: `src/paper_reader/__main__.py`
- Create: `src/paper_reader/cli.py` (stub: parse `--input <pdf>` `--output <epub>`, print args)
- Create: `tests/conftest.py` (fixture pointing at `input/` sample PDFs)
- Create: `.gitignore` (output/, __pycache__, .venv, *.epub)
- Create: `README.md` (usage one-pager)

**Approach:**
- `pyproject.toml` with `[project]` metadata and dependencies: `pymupdf`, `ebooklib`, `pytest` (dev).
- `src/`-layout package so editable install with `pip install -e .` works cleanly.
- CLI stub uses `argparse` (stdlib). Entrypoint registered in `pyproject.toml` as `paper-reader = paper_reader.cli:main`.

**Patterns to follow:** Standard Python src-layout project structure.

**Test scenarios:**
- Happy path: `python -m paper_reader --input input/armbrust-cidr21.pdf --output /tmp/out.epub` parses args and exits 0 without raising.

**Verification:**
- `pip install -e .` succeeds in a fresh venv.
- `python -m paper_reader --help` prints help.
- `pytest` runs and collects zero failures (no tests yet beyond CLI smoke).

---

- U2. **Page layout analyzer: column detection and reading-order sort**

**Goal:** Given a PyMuPDF page, return a list of blocks in correct human reading order.

**Requirements:** R3

**Dependencies:** U1

**Files:**
- Create: `src/paper_reader/layout.py`
- Create: `tests/test_layout.py`

**Approach:**
- Use `page.get_text("dict")["blocks"]` to get blocks with bboxes.
- Detect column count: cluster block-center x-coordinates. If two clear clusters separated by a gap of >5% of page width near the page midpoint → 2 columns; else 1 column.
- For 2-column pages: assign each block to a column by which side of the midpoint its center sits on. Sort within column by y0 ascending. Concatenate left column blocks then right column blocks.
- For 1-column pages: sort all blocks by y0.
- Return blocks in reading order, preserving original bbox and block dict.

**Technical design:** *Directional only — the implementer may choose different clustering thresholds.*

```
def order_blocks(page) -> list[Block]:
    blocks = page.get_text("dict")["blocks"]
    if detect_columns(blocks, page.rect) == 2:
        midx = page.rect.width / 2
        left  = sorted([b for b in blocks if center_x(b) <  midx], key=y0)
        right = sorted([b for b in blocks if center_x(b) >= midx], key=y0)
        return left + right
    return sorted(blocks, key=y0)
```

**Patterns to follow:** None (greenfield).

**Test scenarios:**
- Happy path: a synthetic 2-column page with 4 blocks (top-left, bottom-left, top-right, bottom-right) returns them in that exact order.
- Happy path: a 1-column page with 3 vertically-stacked blocks returns top-to-bottom.
- Edge case: a page with a single full-width block (e.g., title spanning both columns) returns the block first if its bbox spans the midpoint — define and test the spanning-block tiebreak (assign to "left" column for ordering purposes).
- Edge case: empty page (no blocks) returns empty list.
- Edge case: column detection on a page with blocks only on the left side → still classified as 1-column, no false 2-column.
- Integration: run on `input/armbrust-cidr21.pdf` page 1 — assert at least one block from the top of the left column is ordered before any block from the right column.

**Verification:**
- All scenarios above pass.
- Manually spot-check ordered output on one full sample page: read aloud the first sentence of each block in order and confirm it tracks the printed paper.

---

- U3. **Block classifier: text vs non-text region**

**Goal:** Classify each ordered block as either a clean text paragraph (to extract as flowable text) or a non-text region (to rasterize).

**Requirements:** R4

**Dependencies:** U2

**Files:**
- Create: `src/paper_reader/classifier.py`
- Create: `tests/test_classifier.py`

**Approach:**
- Inputs: a block dict from PyMuPDF, plus the page's vector drawings (`page.get_drawings()`) and image list (`page.get_images()`).
- A block is **NON_TEXT** if any of:
  - PyMuPDF reports `block["type"] == 1` (image block).
  - Block bbox overlaps any vector drawing bbox by >20% of the block's area (likely figure or equation rendered via vector paths).
  - Block contains spans whose font name matches a known math-font allowlist (CMSY, CMMI, Symbol, MTSY, MSAM, etc.) or non-trivial ratio of Unicode math symbols (>5% of chars in the U+2200–U+22FF range, or similar greek/math blocks).
  - Block sits within or directly adjacent (within ~1 line height vertically) to a region already marked NON_TEXT due to a figure/equation — caption-extension heuristic.
- Otherwise **TEXT**.
- Return the classification alongside the original block so downstream stages have everything.

**Patterns to follow:** None.

**Test scenarios:**
- Happy path: a block with only Latin-1 text in a body font (e.g., `LinLibertine` or `TimesNewRoman`) classifies as TEXT.
- Happy path: a block at the position of Figure 1 in `input/armbrust-cidr21.pdf` classifies as NON_TEXT.
- Edge case: a block containing inline math symbols but >95% body text classifies as TEXT (don't rasterize whole paragraphs for one Greek letter — note in deferred questions whether this needs tuning).
- Edge case: a block adjacent to a known image block (within ~1 line height) and starting with "Figure 1:" or "Table 1:" classifies as NON_TEXT (caption merges with figure).
- Edge case: a centered single-line block with an equation number on the right (e.g., `(1)`) and math-font characters classifies as NON_TEXT.
- Error path: block with empty `lines` array → classify as TEXT (will produce empty paragraph; harmless).
- Integration: across one page of `input/p2132-afroozeh.pdf`, at least one figure region and one equation region are classified NON_TEXT; the rest of the page is mostly TEXT.

**Verification:**
- For each sample PDF, run a debug dump that highlights NON_TEXT bboxes on a rendered page (manual inspection). Acceptable if no body-paragraph false-positives and figures/equations are captured.

---

- U4. **Region rasterizer**

**Goal:** Given a page and a bbox, produce a PNG byte string at 200 DPI ready to embed in EPUB.

**Requirements:** R4, R5

**Dependencies:** U1

**Files:**
- Create: `src/paper_reader/rasterizer.py`
- Create: `tests/test_rasterizer.py`

**Approach:**
- Thin wrapper around `page.get_pixmap(clip=bbox, dpi=200)` returning `pix.tobytes("png")`.
- Generate stable image filenames: `img-p{page}-{idx:03d}.png`, used as the EPUB-internal asset path.
- Clamp bbox to page rect to avoid PyMuPDF errors on near-edge regions.
- Pad bbox by a small margin (e.g., 3pt on each side) so cropped figures don't lose their border or last glyph.

**Patterns to follow:** None.

**Test scenarios:**
- Happy path: rasterize a known figure bbox from `input/armbrust-cidr21.pdf` page 1 and assert the returned bytes start with the PNG signature (`\x89PNG`) and decode to an image with width/height roughly proportional to the bbox aspect ratio.
- Edge case: bbox with zero area → returns None (or 1x1 transparent PNG); caller knows to skip.
- Edge case: bbox slightly outside page rect → clamped to page rect, no PyMuPDF exception.
- Error path: invalid page index → caller's responsibility, but rasterizer raises a clear ValueError rather than a PyMuPDF internal error.

**Verification:**
- Open one rasterized PNG in an image viewer and confirm it shows the figure/equation legibly.

---

- U5. **Header/footer detection and stripping**

**Goal:** Remove repeated page chrome (journal headers, page numbers, footers) before reading-order assembly so they don't appear inside the EPUB body text.

**Requirements:** R2 (clean reflow), R7

**Dependencies:** U2

**Files:**
- Create: `src/paper_reader/headers_footers.py`
- Create: `tests/test_headers_footers.py`

**Approach:**
- Look at the topmost text block on each page and the bottommost text block on each page across the entire document.
- A block is a header/footer if either:
  - Its text is a pure page number (matches `^\s*\d+\s*$`), OR
  - Its text appears (or near-duplicates with edit distance <10%) on ≥60% of pages in the same y-band (top 10% or bottom 10% of page).
- Mark such blocks for removal; downstream stages skip them.

**Patterns to follow:** None.

**Test scenarios:**
- Happy path: in `input/armbrust-cidr21.pdf`, the page-number footer block on each page is detected and excluded from the ordered output.
- Happy path: across all sample PDFs, no body paragraphs are ever falsely flagged as headers (run debug dump, manually verify).
- Edge case: a single-page PDF → no repetition possible → no blocks flagged (function should not crash and not flag the only header line as a "footer").
- Edge case: title page where the top block is unique (the title itself) → not flagged.
- Error path: empty document → returns no exclusions, no crash.

**Verification:**
- Diff between "ordered blocks with stripping" and "ordered blocks without stripping" for each sample PDF shows only removed page numbers and journal headers, no body content.

---

- U6. **Text stitching, de-hyphenation, and heading detection**

**Goal:** Turn a list of TEXT blocks into a structured stream of `(Heading | Paragraph)` events ready for EPUB chapter rendering.

**Requirements:** R2, R5

**Dependencies:** U3, U5

**Files:**
- Create: `src/paper_reader/text.py`
- Create: `tests/test_text.py`

**Approach:**
- Walk lines within each TEXT block, concatenating into paragraphs. Paragraph breaks happen at block boundaries or at lines whose first-line-indent is significantly larger than the block median (heuristic, optional).
- De-hyphenation: when a line ends with `-` and the next line begins with a lowercase letter, drop the hyphen and concatenate. Skip dehyphenation for known compound words (defer the allowlist; start conservative).
- Heading detection: a TEXT block whose dominant font size is materially larger than the document's body font size (e.g., body-size + 1pt or +20%) and whose text is short (<10 words) is a heading. Heading level inferred from size buckets (largest = h1, next = h2, …).
- Emit a flat stream: `[Heading("Introduction", level=1), Paragraph("…"), Paragraph("…"), Heading("Background", level=1), …]`.
- Non-text regions from U3/U4 are inserted into this same stream at their original document position by the orchestrator (U8), not inside this module.

**Patterns to follow:** None.

**Test scenarios:**
- Happy path: a block with two lines "Recent advances in column-\nbased query engines have …" emits a single paragraph "Recent advances in column-based query engines have …".
- Happy path: a block with body font 10pt and another block with font 14pt, 3-word text "1 Introduction" → heading at level 1 (or 2 depending on the largest size in the document); body block → paragraph.
- Edge case: dehyphenation of "x-\nray" → preserved as "x-ray" (next line starts lowercase, hyphen dropped … but this is also a real compound; document trade-off, accept lossy behavior for v1).
- Edge case: dehyphenation NOT applied when next line starts uppercase: "in 2024-\nQuarter 1" stays as "2024-Quarter 1".
- Edge case: block with 1 line and 1 word in body font → paragraph (don't false-positive as heading on a stray label).
- Edge case: all-caps short block (e.g., "ABSTRACT") in body font size → detected as heading via secondary heuristic (all-caps + short).
- Integration: running over `input/armbrust-cidr21.pdf`, the abstract is captured as a paragraph and section headings ("Introduction", "Related Work", etc.) emerge as Heading events.

**Verification:**
- Manual read-through of stitched output for one sample paper: paragraphs read coherently, section headings appear in the right places, no obvious hyphen artifacts in common words.

---

- U7. **EPUB builder**

**Goal:** Take the interleaved stream of `(Heading | Paragraph | ImageRegion)` events and write a valid EPUB3 file using ebooklib.

**Requirements:** R1, R2, R4, R5

**Dependencies:** U4, U6

**Files:**
- Create: `src/paper_reader/epub_builder.py`
- Create: `tests/test_epub_builder.py`

**Approach:**
- One EPUB chapter (XHTML doc) per top-level Heading. Heading text becomes the chapter title and `<h1>`; subsequent Paragraph and ImageRegion events accumulate into the chapter body until the next top-level Heading.
- If no top-level headings are detected in the entire stream → single chapter containing everything.
- ImageRegion events: rasterize via U4, add the PNG as an ebooklib `EpubImage` to the book, and emit `<figure><img src="images/img-pNN-MMM.png" alt="Figure or equation from page NN" /></figure>` in the XHTML.
- Single CSS file (`styles/main.css`) sets `body { line-height: 1.5; }`, `img { max-width: 100%; height: auto; display: block; margin: 1em auto; }`, `figure { margin: 1em 0; }`. No font-family override — let the reader pick.
- EPUB metadata: title pulled from the document's first detected h1 (or filename as fallback), author left blank in v1, identifier = a generated UUID, language = `en`.
- Build the spine, NCX, and nav doc via ebooklib's standard helpers.

**Patterns to follow:** None.

**Test scenarios:**
- Happy path: a stream of `[Heading("Intro", 1), Paragraph("Hello world."), ImageRegion(bytes=<png>), Heading("Methods", 1), Paragraph("…")]` produces an EPUB with 2 chapters, the first containing the image after the paragraph.
- Happy path: a stream with no headings produces a single-chapter EPUB containing all paragraphs and images.
- Happy path: produced EPUB passes `epubcheck` (run as a subprocess in the integration test, optional but recommended).
- Edge case: stream contains only paragraphs (no images, no headings) → valid 1-chapter EPUB.
- Edge case: a heading directly followed by another heading (empty section) → both chapters created, second with empty body — must not crash.
- Edge case: 200+ images in one chapter — EPUB file builds without exhausting memory (use streaming bytes, not all-images-in-RAM if ebooklib supports it; otherwise document the limit).
- Integration: open generated EPUB in Apple Books (manual) — text reflows, images render and are tap-zoomable.

**Verification:**
- `epubcheck output.epub` reports zero errors on each sample paper's output (warnings acceptable).
- Manual Apple Books inspection on iPad for at least one sample paper: font-size slider works, images visible, reading order matches printed paper.

---

- U8. **CLI orchestration and end-to-end integration**

**Goal:** Wire all the pieces into the CLI entrypoint so `paper-reader --input X.pdf --output Y.epub` works end-to-end on the sample PDFs.

**Requirements:** R1, R7

**Dependencies:** U2, U3, U4, U5, U6, U7

**Files:**
- Modify: `src/paper_reader/cli.py`
- Create: `tests/test_integration.py`

**Approach:**
- `cli.main()`:
  1. Parse `--input`, `--output`, optional `--dpi` (default 200), optional `--verbose`.
  2. Open PDF with PyMuPDF.
  3. Run header/footer detection across pages (U5) to build exclusion set.
  4. For each page: get blocks, drop excluded, order via U2, classify via U3.
  5. Walk classified blocks in document order, collecting TEXT blocks into the stitcher (U6), and emitting ImageRegion placeholders for NON_TEXT blocks with their `(page, bbox)` for later rasterization.
  6. After all pages: hand the structured stream to U7's EPUB builder, which rasterizes images lazily via U4 as it embeds them.
  7. Write EPUB to `--output`.
- Exit codes: 0 success, 1 invalid input (file missing / not a PDF), 2 unexpected error with traceback printed to stderr.

**Patterns to follow:** None.

**Test scenarios:**
- Integration happy path: convert each of the 5 sample PDFs in `input/` to EPUB; assert each output file exists, is >10KB, and passes `epubcheck` (or at minimum, opens in `ebooklib.epub.read_epub`).
- Integration happy path: opening a generated EPUB and walking the spine produces at least one chapter with `<p>` text and at least one `<img>`.
- Error path: `--input nonexistent.pdf` exits with code 1 and a clear error message on stderr.
- Error path: `--input` pointing at a non-PDF file (e.g., a `.txt`) exits with code 1, not a stack trace.
- Edge case: a PDF with no text layer at all (synthetic / image-only) — exit cleanly with a message saying "no extractable text found", code 1. Do not produce an empty EPUB.

**Verification:**
- All 5 sample PDFs produce EPUBs that open in Apple Books on iPad.
- Reading order in the opened EPUBs is correct for at least the first 2 pages of each paper (manual spot-check).
- Font-size adjustment in Books visibly reflows the text.

---

## System-Wide Impact

- **Interaction graph:** Linear pipeline — no callbacks, no async, no shared mutable state across stages. CLI orchestrator (U8) is the only module that touches every other module.
- **Error propagation:** Each stage either succeeds or raises a typed exception (`InvalidPDFError`, `NoTextLayerError`). CLI catches at the top level, prints a clean message, and exits with the right code.
- **State lifecycle risks:** Temporary file handling is minimal — PNGs live in memory as bytes until ebooklib writes the EPUB. No long-running state, no caches that can go stale.
- **API surface parity:** The CLI is the only user-facing surface. No library API stability commitments in v1 — internal modules can be refactored freely.
- **Integration coverage:** Per-module unit tests cover happy paths and edge cases in isolation; `test_integration.py` is the cross-layer guard — if a refactor breaks the end-to-end flow on the sample PDFs, that test catches it.
- **Unchanged invariants:** No existing code to preserve. Input PDFs in `input/` are read-only; the tool never modifies them.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Column detection misfires on papers with full-width section headings or full-width title blocks, putting headings in the wrong reading position. | Tiebreak rule: blocks whose bbox spans the page midpoint by more than 20% on each side are treated as full-width and ordered by y0 ignoring column assignment. Validate against the 5 sample PDFs. |
| Math/figure classification false-positives rasterize body paragraphs as images, breaking reflow. | Classifier defaults to TEXT unless multiple non-text signals fire. Debug dump in U3 highlights every NON_TEXT region on rendered pages for manual review during development. |
| Header/footer detector falsely flags a real body paragraph that happens to repeat (e.g., a recurring "Algorithm 1" label) and drops it. | Repetition threshold (≥60% of pages) is conservative; y-band restriction (top/bottom 10%) makes mid-page false positives near-impossible. |
| ebooklib produces EPUB that Apple Books renders but `epubcheck` flags. | Run `epubcheck` in integration tests against generated output; fix flagged issues. Apple Books is more lenient than epubcheck — passing epubcheck is the harder bar. |
| Sample PDFs use embedded fonts whose Unicode mappings are broken, producing extracted text that's garbage even though it looks fine in the PDF. | Detect via a sanity check: if a page's extracted text has >30% non-printable or replacement chars, rasterize the whole page as a fallback. Defer the exact threshold to implementation. |
| Memory growth on large papers (50+ pages with many figures). | All images are PNG bytes in memory at once. For v1, accept this — even 50 figures at 200dpi rarely exceed 50MB. Document as a known limit; revisit only if it actually bites. |

---

## Documentation / Operational Notes

- `README.md` (created in U1) documents: installation (`pip install -e .`), basic usage (`paper-reader --input X.pdf --output Y.epub`), known limitations (scanned PDFs unsupported, math is rasterized).
- No CI/CD setup in v1 — single-user personal tool. Tests run locally via `pytest`.
- Transferring EPUB to iPad: AirDrop into Books, or use Books for Mac sync. Not covered by the tool itself.

---

## Sources & References

- PyMuPDF docs: text extraction with `get_text("dict")`, rasterization with `get_pixmap(clip=...)`.
- ebooklib docs: EpubBook, EpubHtml, EpubImage, NCX/nav setup.
- Sample input PDFs: `input/armbrust-cidr21.pdf`, `input/p148-zeng (1).pdf`, `input/p2132-afroozeh.pdf`, `input/p2679-pedreira.pdf`, `input/p3044-liu.pdf`.
