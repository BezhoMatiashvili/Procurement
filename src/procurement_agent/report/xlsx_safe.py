"""Make untrusted text safe to write into an openpyxl cell.

Every workbook this project writes carries text that someone outside the organisation typed:
a bidder's descriptions, units and sheet names, a counterparty's paragraphs. Two things go
wrong when that text is written verbatim.

* **Formula injection.** openpyxl stores any string that starts with ``=`` as a *formula*, and
  Excel and LibreOffice treat a leading ``+``, ``-`` or ``@`` the same way when a cell is
  edited or the file is round-tripped through CSV. A description reading ``=HYPERLINK(...)`` or
  ``=cmd|' /C ...'!A0`` in a bid would become live in the merged workbook a buyer opens - the
  one file in the flow that is trusted because *we* produced it. The OWASP mitigation is a
  leading apostrophe, which Excel shows as text and never evaluates.
* **Characters XML cannot carry.** A C0 control character (a stray ``\\x0b`` pasted from a PDF,
  a ``\\x1f`` from a legacy system) makes openpyxl raise ``IllegalCharacterError`` - so one
  bidder's odd byte fails the whole export instead of one cell looking slightly different.

Numbers, dates and booleans pass through untouched: they cannot be formulas, and turning them
into text would break the sums and the red/green colouring built on them.
"""

from __future__ import annotations

from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

__all__ = ["cell_safe", "text_safe"]

#: The leading characters a spreadsheet may read as the start of a formula. Tab and carriage
#: return are on the OWASP list too: some importers strip them and then see what follows.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def text_safe(value: str) -> str:
    """``value`` with XML-illegal characters removed and a formula lead neutralised."""
    clean = ILLEGAL_CHARACTERS_RE.sub("", value)
    if clean[:1] in _FORMULA_LEAD:
        return "'" + clean
    return clean


def cell_safe(value: object) -> object:
    """``text_safe`` for strings; everything else is returned as it is."""
    if isinstance(value, str):
        return text_safe(value)
    return value
