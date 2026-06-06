"""Shared pytest fixtures."""
from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
INPUT_DIR = REPO_ROOT / "input"


@pytest.fixture(scope="session")
def input_dir() -> Path:
    return INPUT_DIR


@pytest.fixture(scope="session")
def sample_pdfs(input_dir: Path) -> list[Path]:
    return sorted(input_dir.glob("*.pdf"))


@pytest.fixture(scope="session")
def armbrust_pdf(input_dir: Path) -> Path:
    return input_dir / "armbrust-cidr21.pdf"
