"""Describe a failure without quoting what it was about.

An exception's message is written by whoever raised it - often a library, often with the offending
value in it: ``ValueError("could not parse '12 345 ლარი'")``, a pydantic error that carries
``input_value``, a parser that names a sheet. Logged, stored in a job row or written to the audit
trail, that is document text in places this project promises never hold any. So nothing here ever
prints ``str(e)``: ``safe_exc`` says what *kind* of failure it was, and for the few families whose
parts are structural, how many and which kinds.

An exception class whose messages this project writes itself - from numbers and constants, never
from a document - opts in by inheriting ``ContentFree``; its message is then kept (cut short),
because "the document has 9,000 segments, more than the 5,000 this comparison aligns" is the
useful half of the failure and carries nothing of the document.
"""

from __future__ import annotations

import errno
from typing import Any

from pydantic import ValidationError

__all__ = ["ContentFree", "safe_exc"]

_MAX_KINDS = 5
_MAX_CAUSES = 3


class ContentFree:
    """Mixin for exceptions whose message is composed only of our own words and numbers.

    A new message that interpolates anything taken from a document must not be raised from a class
    that has this mixin; ``tests/unit/test_safe_errors.py`` fails when one of the marked classes is
    raised with a probe string in its arguments and the probe comes back out.
    """


def _kind(e: BaseException) -> str:
    """``Name`` plus whatever of the failure is structural, never textual."""
    name = type(e).__name__
    if isinstance(e, ValidationError):
        kinds = sorted(
            {
                str(err.get("type", "?"))
                for err in e.errors(include_url=False, include_context=False, include_input=False)
            }
        )
        shown = ", ".join(kinds[:_MAX_KINDS]) + (", ..." if len(kinds) > _MAX_KINDS else "")
        return f"{name}({e.error_count()} error(s): {shown})"
    if isinstance(e, OSError) and e.errno is not None:
        return f"{name}(errno={errno.errorcode.get(e.errno, e.errno)})"
    response: Any = getattr(e, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return f"{name}({status})"
    return name


def safe_exc(e: BaseException, *, limit: int = 200) -> str:
    """A description of ``e`` that cannot carry document text; at most ``limit`` characters."""
    if isinstance(e, ContentFree):
        text = f"{type(e).__name__}: {' '.join(str(e).split())}"
    else:
        text = _kind(e)
    causes: list[str] = []
    cause = e.__cause__
    while cause is not None and len(causes) < _MAX_CAUSES:
        causes.append(type(cause).__name__)
        cause = cause.__cause__
    if causes:
        text += f" (caused by {' <- '.join(causes)})"
    return text[:limit]
