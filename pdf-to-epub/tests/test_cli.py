from pathlib import Path

import pytest

from paper_reader.cli import build_parser, main


def test_parser_defaults():
    parser = build_parser()
    args = parser.parse_args(["--input", "a.pdf", "--output", "b.epub"])
    assert args.input == Path("a.pdf")
    assert args.output == Path("b.epub")
    assert args.dpi == 200
    assert args.author == ""
    assert args.title is None
    assert args.verbose is False


def test_missing_input_returns_1(tmp_path, capsys):
    rc = main(["--input", str(tmp_path / "missing.pdf"), "--output", str(tmp_path / "x.epub")])
    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_non_pdf_input_returns_1(tmp_path, capsys):
    fake = tmp_path / "notes.txt"
    fake.write_text("hello")
    rc = main(["--input", str(fake), "--output", str(tmp_path / "x.epub")])
    assert rc == 1
    assert "must be a .pdf" in capsys.readouterr().err


def test_batch_mode_empty_directory_returns_1(tmp_path, capsys):
    indir = tmp_path / "in"
    indir.mkdir()
    outdir = tmp_path / "out"
    rc = main(["--input", str(indir), "--output", str(outdir)])
    assert rc == 1
    assert "no .pdf files" in capsys.readouterr().err


def test_batch_mode_rejects_file_output_for_directory_input(tmp_path, capsys):
    indir = tmp_path / "in"
    indir.mkdir()
    (indir / "sample.pdf").write_bytes(b"%PDF-1.4 fake")
    outfile = tmp_path / "out.epub"
    outfile.write_bytes(b"existing")
    rc = main(["--input", str(indir), "--output", str(outfile)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "directory" in err
