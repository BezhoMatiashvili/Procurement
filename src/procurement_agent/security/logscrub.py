"""Backstops for "log records never carry document text".

The rule is kept at the call sites: nothing logs ``str(e)`` (``safe_exc``), a value, a quote or a
name. These are the layers behind that, for the day somebody does anyway, and for the loggers this
project does not write:

* ``content_free`` (a structlog processor) refuses an event name that is not an identifier,
  withholds fields whose *name* says they hold content (``value``, ``text``, ``quote``...),
  withholds any string too long to be a status word, and replaces ``exc_info`` - a traceback
  prints the exception's message - with ``safe_exc`` of it.
* ``ScrubFilter`` (stdlib ``logging``) does the same for what uvicorn and the libraries emit: it
  cuts the query string off an access-log line (``?advance_amount=...`` is typed by a user) and
  renders a traceback as frames and exception kinds, without messages.

None of this makes a leak impossible: a library's own message text cannot be inspected. The
guarantee is ``tests/integration/test_log_canary.py``, which plants a marker in every place document
text arrives and asserts it is in no log line and no audit row.
"""

from __future__ import annotations

import logging
import re
import sys
import traceback
from types import TracebackType
from typing import Any

from procurement_agent.security.safe_errors import safe_exc

__all__ = ["MAX_STRING", "ScrubFilter", "content_free", "install_stdlib_scrub", "scrub_traceback"]

#: A status word or an identifier, not a paragraph: longer strings are withheld.
MAX_STRING = 200
#: What a deliberate event name looks like (``job_crashed``, ``llm_call``). An f-string with a
#: document in it does not.
_EVENT = re.compile(r"[A-Za-z][A-Za-z0-9_.:\- ]{0,79}")
#: Field names that say the value is the content of a document or a person's input.
_CONTENT_KEYS = frozenset(
    {
        "value",
        "values",
        "text",
        "quote",
        "raw_text",
        "content",
        "prompt",
        "document",
        "filename",
        "file_name",
        "name",
        "sheet",
        "sheet_name",
        "question",
        "answer",
        "body",
        "snippet",
        "input",
        "input_value",
        "label",
    }
)
_MAX_DEPTH = 3


def _clean(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return f"[withheld: {len(value)} chars]" if len(value) > MAX_STRING else value
    if depth >= _MAX_DEPTH:
        return value
    if isinstance(value, dict):
        return {
            k: (f"[withheld: {k}]" if str(k).lower() in _CONTENT_KEYS else _clean(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return (
            type(value)(_clean(v, depth + 1) for v in value)
            if not isinstance(value, set | frozenset)
            else value
        )
    return value


def content_free(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor: see the module docstring."""
    event = event_dict.get("event")
    if not isinstance(event, str) or not _EVENT.fullmatch(event):
        size = len(event) if isinstance(event, str) else 0
        event_dict["event"] = "log_event_withheld"
        event_dict["withheld_chars"] = size
    exc = event_dict.pop("exc_info", None)
    if exc:
        error = exc if isinstance(exc, BaseException) else sys.exc_info()[1]
        if isinstance(exc, tuple) and len(exc) == 3 and isinstance(exc[1], BaseException):
            error = exc[1]
        if error is not None:
            event_dict["error"] = safe_exc(error)
    for key in list(event_dict):
        if key == "event":
            continue
        if key.lower() in _CONTENT_KEYS:
            event_dict[key] = "[withheld]"
        else:
            event_dict[key] = _clean(event_dict[key])
    return event_dict


def scrub_traceback(
    exc_info: tuple[type[BaseException], BaseException, TracebackType | None],
) -> str:
    """The frames and the kinds of exception, never the messages (which may quote a document)."""
    chain: list[BaseException] = []
    error: BaseException | None = exc_info[1]
    while error is not None and len(chain) < 5 and error not in chain:
        chain.append(error)
        error = error.__cause__ or error.__context__
    lines: list[str] = []
    for e in reversed(chain):
        lines.append("Traceback (most recent call last):")
        lines += [
            f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}'
            for frame in traceback.extract_tb(e.__traceback__)
        ]
        lines.append(safe_exc(e))
    return "\n".join(lines)


class ScrubFilter(logging.Filter):
    """stdlib ``logging`` filter: no query strings in access lines, no messages in tracebacks."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if record.name == "uvicorn.access" and isinstance(args, tuple) and len(args) >= 3:
            target = args[2]
            if isinstance(target, str) and "?" in target:
                record.args = (*args[:2], target.split("?", 1)[0] + "?[query withheld]", *args[3:])
        if record.exc_info and isinstance(record.exc_info, tuple) and record.exc_info[1]:
            # the formatter prints exc_text when it is set, and only formats exc_info when not
            record.exc_text = scrub_traceback(record.exc_info)
        return True


_LOGGERS = ("", "uvicorn", "uvicorn.error", "uvicorn.access")


def install_stdlib_scrub() -> None:
    """Attach the filter to the root logger, uvicorn's, and every handler they own. Idempotent."""
    scrub = ScrubFilter()
    for name in _LOGGERS:
        logger = logging.getLogger(name)
        targets: list[logging.Filterer] = [logger, *logger.handlers]
        for target in targets:
            if not any(isinstance(f, ScrubFilter) for f in target.filters):
                target.addFilter(scrub)
