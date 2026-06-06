"""End-to-end integration tests against the sample PDFs in input/.

Each sample PDF is converted to EPUB and the output is checked for:
- file exists and is non-trivially sized
- valid zip container with META-INF/container.xml
- contains at least one chapter XHTML
- ebooklib can read it back without error
- has at least one paragraph and at least one image
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")
ebooklib = pytest.importorskip("ebooklib")
from ebooklib import epub  # noqa: E402

from paper_reader.cli import main
from paper_reader.pipeline import convert_pdf_to_epub


def _epub_is_well_formed(path: Path) -> None:
    assert path.exists(), f"missing: {path}"
    assert path.stat().st_size > 10_000, f"suspiciously small: {path.stat().st_size}B"
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        assert "META-INF/container.xml" in names
        assert any(n.endswith(".xhtml") for n in names), f"no XHTML in {names}"


def _read_epub_summary(path: Path) -> tuple[int, int, int]:
    """Return (chapter_count, paragraph_count, image_count) via ebooklib read."""
    book = epub.read_epub(str(path))
    chapters = 0
    paragraphs = 0
    images = 0
    for item in book.items:
        media_type = getattr(item, "media_type", "")
        if media_type == "application/xhtml+xml":
            chapters += 1
            content = item.get_content().decode("utf-8", errors="replace")
            paragraphs += content.count("<p>")
            images += content.count("<img ")
    return chapters, paragraphs, images


def _discover_sample_pdfs() -> list[Path]:
    here = Path(__file__).resolve().parent.parent / "input"
    return sorted(here.glob("*.pdf"))


@pytest.mark.parametrize(
    "pdf",
    _discover_sample_pdfs(),
    ids=lambda p: p.name,
)
def test_convert_sample_pdf(tmp_path: Path, pdf: Path) -> None:
    if not pdf.exists():
        pytest.skip(f"sample PDF missing: {pdf.name}")

    out = tmp_path / f"{pdf.stem}.epub"
    convert_pdf_to_epub(pdf, out, dpi=150)  # 150 to keep test artifacts small

    _epub_is_well_formed(out)
    chapters, paragraphs, images = _read_epub_summary(out)
    assert chapters >= 1, "expected at least one chapter"
    assert paragraphs >= 5, f"expected >=5 paragraphs, got {paragraphs}"
    assert images >= 1, f"expected >=1 rasterized region, got {images}"


def test_cli_end_to_end(input_dir: Path, tmp_path: Path) -> None:
    pdfs = sorted(input_dir.glob("*.pdf"))
    if not pdfs:
        pytest.skip("no sample PDFs")
    pdf = pdfs[0]
    out = tmp_path / "out.epub"
    rc = main(["--input", str(pdf), "--output", str(out), "--dpi", "150"])
    assert rc == 0
    _epub_is_well_formed(out)


def test_cli_batch_mode(input_dir: Path, tmp_path: Path) -> None:
    """Pointing --input at a directory converts every .pdf inside."""
    pdfs = sorted(input_dir.glob("*.pdf"))
    if not pdfs:
        pytest.skip("no sample PDFs")
    outdir = tmp_path / "batch_out"
    rc = main(["--input", str(input_dir), "--output", str(outdir), "--dpi", "100"])
    assert rc == 0
    produced = sorted(outdir.glob("*.epub"))
    assert len(produced) == len(pdfs), f"expected {len(pdfs)} epubs, got {len(produced)}"
    for epub_path in produced:
        _epub_is_well_formed(epub_path)
