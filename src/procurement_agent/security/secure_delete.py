"""Zeroing a file before it is unlinked.

Lives apart from ``jobs.retention`` because more than retention removes files that held client
data (the database's pre-upgrade copies, ``migrations``), and importing the retention module drags
in the whole extraction pipeline, which a database upgrade must not wait for.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["overwrite", "remove_file"]

#: Overwritten in blocks this size, so a large file is not read into memory.
_BLOCK = 1024 * 1024


def overwrite(path: Path) -> None:
    """Zero a file's blocks, flush them to the device, before it is unlinked.

    Best effort, and said so: on a copy-on-write filesystem or an SSD the old blocks may survive,
    which is why the deployment's protection is an encrypted disk (docs/SECURITY.md). It costs a
    few milliseconds and removes the plain read of an undeleted-file tool on an ordinary one.
    """
    try:
        remaining = path.stat().st_size
        with path.open("r+b") as fh:
            while remaining > 0:
                fh.write(b"\0" * min(remaining, _BLOCK))
                remaining -= _BLOCK
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass


def remove_file(path: Path) -> None:
    """Overwrite a file, then unlink it: how anything document-derived leaves the data directory,
    whether a deletion removes it or a job discards it."""
    overwrite(path)
    path.unlink(missing_ok=True)
