"""How many cells an .xlsx holds, counted from the archive without opening it as a workbook.

A full read (``openpyxl.load_workbook`` without ``read_only``) builds an object for every cell,
about 420 bytes each. The upload guard bounds an archive's inflated size, not its cells, so a
2.6 MB file can hold the 512 MiB of cell XML the guard admits - gigabytes in memory, twice over
for the BOQ reader that opens each workbook twice. The parse job reads read-only and capped; this
count is what lets the readers that cannot do that refuse before they start.

The count streams the XML parts in chunks, so its own memory is constant however large the archive
says it is, and it stops as soon as the limit is passed.
"""

from __future__ import annotations

import re
import zipfile
import zlib
from pathlib import Path

from procurement_agent.security.safe_errors import ContentFree

__all__ = ["DEFAULT_MAX_CELLS", "WorkbookTooLargeError", "count_cells", "ensure_cell_count"]

#: A full read of the BOQ reader's kind may hold this many cells. The largest real BOQ workbook
#: measured has about 10 000, one sheet at most 1 300; this is thirty times that and, at about 420
#: bytes a cell, some 130 MB per read.
DEFAULT_MAX_CELLS = 300_000

#: A cell element: ``<c r="A1" ...>``, ``<c>`` or ``<c/>``. Elements that merely start with "c"
#: (``<cols>``, ``<cfRule>``, ``<c:chart>``) have another character right after it.
_CELL = re.compile(rb"<c[\s/>]")
_CHUNK = 1 << 20


class WorkbookTooLargeError(ContentFree, ValueError):
    """The workbook has more cells than a full read may hold in memory."""


def count_cells(path: Path | str, limit: int) -> int:
    """The cell elements in the archive's XML parts - at least ``limit + 1`` once it has more.

    Every ``.xml`` part is counted, not just ``xl/worksheets/``: a relationship may point a sheet
    anywhere. That can over-count (a chart has no ``<c ...>``) and never under-counts.
    """
    total = 0
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if not info.filename.lower().endswith(".xml"):
                continue
            tail = b""  # a match is three bytes; two are kept so one cut by a chunk edge is found
            with archive.open(info) as part:
                while chunk := part.read(_CHUNK):
                    data = tail + chunk
                    total += len(_CELL.findall(data))
                    if total > limit:
                        return total
                    tail = data[-2:]
    return total


def ensure_cell_count(path: Path | str, limit: int) -> None:
    """Raise ``WorkbookTooLargeError`` when the workbook has more than ``limit`` cells.

    A file that is no zip archive is left to the reader to refuse in its own words.
    """
    try:
        cells = count_cells(path, limit)
    except (
        zipfile.BadZipFile,
        ValueError,
        EOFError,
        zlib.error,
        NotImplementedError,
        RuntimeError,
    ):
        return  # an archive that cannot be read is the reader's to refuse, in its own words
    if cells > limit:
        msg = (
            f"the workbook has more than {limit:,} cells; checking it reads every cell into memory"
        )
        raise WorkbookTooLargeError(msg)
