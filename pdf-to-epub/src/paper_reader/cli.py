"""Command-line entrypoint for paper-reader."""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from .pipeline import ConversionError, convert_pdf_to_epub


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paper-reader",
        description="Convert a 2-column scientific PDF into a reflowable EPUB.",
    )
    parser.add_argument(
        "--input", "-i", required=True, type=Path,
        help="Path to source PDF, or a directory of PDFs for batch mode",
    )
    parser.add_argument(
        "--output", "-o", required=True, type=Path,
        help="Path to write EPUB, or a directory when --input is a directory",
    )
    parser.add_argument("--dpi", type=int, default=200, help="DPI for rasterized regions (default: 200)")
    parser.add_argument("--title", default=None, help="EPUB title (defaults to PDF metadata or filename)")
    parser.add_argument("--author", default="", help="EPUB author (default: empty)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging to stderr")
    return parser


def _convert_one(pdf: Path, epub_out: Path, args: argparse.Namespace) -> int:
    """Convert a single PDF. Returns 0 on success, non-zero on failure."""
    epub_out.parent.mkdir(parents=True, exist_ok=True)
    try:
        convert_pdf_to_epub(
            pdf,
            epub_out,
            dpi=args.dpi,
            title=args.title,
            author=args.author,
            verbose=args.verbose,
        )
    except ConversionError as exc:
        print(f"error converting {pdf.name}: {exc}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001
        print(f"error: unexpected failure on {pdf.name}", file=sys.stderr)
        traceback.print_exc()
        return 2
    print(f"wrote {epub_out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.input.exists():
        print(f"error: input not found: {args.input}", file=sys.stderr)
        return 1

    # Batch mode: directory in, directory out.
    if args.input.is_dir():
        pdfs = sorted(args.input.glob("*.pdf"))
        if not pdfs:
            print(f"error: no .pdf files in {args.input}", file=sys.stderr)
            return 1
        if args.output.exists() and not args.output.is_dir():
            print(
                f"error: --input is a directory but --output is a file: {args.output}",
                file=sys.stderr,
            )
            return 1
        args.output.mkdir(parents=True, exist_ok=True)

        worst_rc = 0
        for pdf in pdfs:
            epub_out = args.output / f"{pdf.stem}.epub"
            rc = _convert_one(pdf, epub_out, args)
            worst_rc = max(worst_rc, rc)
        return worst_rc

    # Single-file mode.
    if args.input.suffix.lower() != ".pdf":
        print(f"error: input must be a .pdf file (got {args.input.suffix})", file=sys.stderr)
        return 1
    if args.output.exists() and args.output.is_dir():
        print(
            f"error: --output is a directory but --input is a file: {args.output}",
            file=sys.stderr,
        )
        return 1
    return _convert_one(args.input, args.output, args)
