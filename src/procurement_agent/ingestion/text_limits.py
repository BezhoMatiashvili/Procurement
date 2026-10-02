"""Bounds on the text the application will read, applied where the text enters.

Two things make the cost of reading a document grow faster than the document:

* **Long runs of one kind of character.** A document is read by several hundred regular
  expressions, and a pattern that starts with optional whitespace (``\\s*,?\\s*...``) costs the
  *square* of a run of whitespace when it fails after it; a pattern over letters or digits does the
  same to a word of thousands of characters. A run of five thousand spaces and newlines - which no
  contract contains, and a crafted file can - cost 0.6 s per chunk; a file the size of the upload
  limit, hours. ``bound_text`` cuts any run of non-letter, non-digit characters longer than
  ``MAX_SEPARATOR_RUN`` down to that many, and any unbroken run of non-space characters longer
  than ``MAX_TOKEN`` likewise, so no pattern ever sees a longer one. Real text is untouched: dot
  leaders, signature lines and table rules are about a hundred characters or fewer, the longest
  word or code a few dozen (measured on the client's documents; ``tests/unit/test_text_limits.py``
  pins both limits and ``tests/unit/test_regex_budget.py`` checks every pattern against them).
* **Total size.** The upload limit bounds the *file*; a compressed one holds hundreds of megabytes
  of text. ``Settings.max_text_chars`` bounds what a parsed document may hold.

Both are applied to the parsed text itself - when a stored parse is loaded, when a workbook cell is
read - so every consumer (the chunker, the segmenter, the rules, the comparison, the LLM prompt)
sees the same text, and its offsets stay consistent with each other. This is a bound on the cost of
each pattern, not a defence against every slow input: see ``docs/SECURITY.md`` for what remains.
"""

from __future__ import annotations

import re

from docling_core.types.doc.document import DoclingDocument

__all__ = [
    "MAX_SEPARATOR_RUN",
    "MAX_TOKEN",
    "bound_text",
    "compact_document",
    "compact_runs",
    "text_size",
]

#: The longest run of separator characters a document may keep. The longest in the client's 330
#: parsed documents is 116.
MAX_SEPARATOR_RUN = 200
#: The longest unbroken stretch of non-space characters a document may keep: longer than any word,
#: number, code, e-mail address, URL or hash a procurement document holds. The longest in the
#: client's 330 parsed documents is 33.
MAX_TOKEN = 256

#: A run of characters that are neither letters nor digits (the underscore counts as a separator:
#: signature lines are made of it). The lookbehind anchors an attempt at the START of a run, so a
#: scan over ordinary text costs a step per character rather than a step per character per run
#: length; the atomic group stops the engine from backing off and trying the same run again.
_LONG_RUN = re.compile(rf"(?<![\W_])(?>(?:\W|_){{{MAX_SEPARATOR_RUN + 1},}})")
_LONG_TOKEN = re.compile(rf"(?<!\S)(?>\S{{{MAX_TOKEN + 1},}})")


def compact_runs(text: str) -> str:
    """``text`` with every run of separator characters cut to ``MAX_SEPARATOR_RUN``."""
    if len(text) <= MAX_SEPARATOR_RUN:
        return text
    return _LONG_RUN.sub(lambda m: m.group()[:MAX_SEPARATOR_RUN], text)


#: Cutting a long token can join the two separator runs its letters used to keep apart, and cutting
#: a run can join two tokens; each pass only shortens the text, so repeating until nothing changes
#: ends. Real text needs one pass; this caps the pathological case's cost.
_MAX_PASSES = 6


def bound_text(text: str) -> str:
    """``compact_runs`` and the token cut, repeated until stable: the form the patterns read."""
    if len(text) <= MAX_SEPARATOR_RUN:
        return text
    for _ in range(_MAX_PASSES):
        shorter = compact_runs(text)
        if len(shorter) > MAX_TOKEN:
            shorter = _LONG_TOKEN.sub(lambda m: m.group()[:MAX_TOKEN], shorter)
        if shorter == text:
            break
        text = shorter
    return text


def _cells(doc: DoclingDocument) -> list[object]:
    return [cell for table in doc.tables for cell in table.data.table_cells]


def compact_document(doc: DoclingDocument) -> int:
    """Apply ``bound_text`` to every piece of text a parsed document holds; how many were cut."""
    cut = 0
    for item in [*doc.texts, *_cells(doc)]:
        for name in ("text", "orig"):
            value = getattr(item, name, None)
            if isinstance(value, str) and len(value) > MAX_SEPARATOR_RUN:
                shorter = bound_text(value)
                if shorter != value:
                    setattr(item, name, shorter)
                    cut += 1
    return cut


def text_size(doc: DoclingDocument) -> int:
    """How many characters of text a parsed document holds (text items and table cells)."""
    total = sum(len(item.text) for item in doc.texts)
    for cell in _cells(doc):
        total += len(getattr(cell, "text", "") or "")
    return total
