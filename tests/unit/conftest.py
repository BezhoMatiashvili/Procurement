"""Fixtures shared by the unit tests."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def sock_dir() -> Iterator[Path]:
    """A directory with a short path for Unix sockets (an address holds 107 bytes, and a pytest
    ``tmp_path`` can be longer), mode 0700 as the parser service insists."""
    short = Path(tempfile.mkdtemp(prefix="pa-ps-"))
    yield short
    shutil.rmtree(short, ignore_errors=True)
