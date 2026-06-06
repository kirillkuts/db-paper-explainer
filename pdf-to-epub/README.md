# paper-reader

Convert 2-column scientific PDFs into reflowable EPUB files for comfortable iPad reading.

Body text reflows so you can change font size in your reader (Apple Books, KOReader, Kindle). Figures, tables, and equations are rasterized as PNG images and embedded at the correct position in the reading flow — this sidesteps the usual PDF-to-EPUB problems with math fonts and complex layouts.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Usage

Single file:

```bash
paper-reader --input path/to/paper.pdf --output paper.epub
```

Batch (every `.pdf` in a directory):

```bash
paper-reader --input input/ --output output/
```

Then AirDrop the `.epub` files to your iPad and open in Apple Books.

## Limitations

- Input must have a real text layer (no OCR built in — scanned PDFs unsupported).
- Math equations and tables are rendered as images, not reflowable text.
- 3+ column layouts and rotated text are out of scope.

## Development

```bash
pytest
```

Sample input PDFs live in `input/`. Generated EPUBs go to `output/` (gitignored).
