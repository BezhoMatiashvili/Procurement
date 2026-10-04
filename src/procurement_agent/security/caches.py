"""In-process caches that hold document text, and the one place that empties them.

Two ``functools.lru_cache`` functions in the acceptance-act check take the *whole text* of a
document as their argument, so the cache keeps up to eight documents' text in the server's memory
after the documents are deleted. The spelling dictionary is loaded once and kept, but the words
it was asked about (a document's words) are memoised inside it. They are speed-ups only
(recomputing costs milliseconds), so a deletion simply empties them: ``forget_cached_text`` is
called wherever a document is marked deleted. ``tests/unit/test_retention.py`` fails when a new
``lru_cache`` appears that is in neither list below, so the next one is classified when it is
written, not discovered.
"""

from __future__ import annotations

__all__ = ["NO_TEXT", "TEXT_KEYED", "forget_cached_text"]

#: ``(module, function)`` whose cache key is document text. Emptied on every deletion by
#: ``forget_cached_text``, which names them directly; the test compares the two.
TEXT_KEYED: tuple[tuple[str, str], ...] = (
    ("procurement_agent.checks.aoa", "_period_opens"),
    ("procurement_agent.checks.aoa", "_slipped_signing"),
)
#: Caches that never see document text: the settings, and the spelling dictionary loaded from
#: a directory path.
NO_TEXT: frozenset[tuple[str, str]] = frozenset(
    {
        ("procurement_agent.config", "get_settings"),
        ("procurement_agent.checks.aoa_spelling", "_load"),  # its words are cleared separately
    }
)


def forget_cached_text() -> int:
    """Empty every text-keyed cache; returns how many entries were dropped."""
    # imported here: the check module pulls in most of the package, and this one is imported by
    # the worker that the check module's callers also import
    from procurement_agent.checks import aoa, aoa_spelling

    dropped = 0
    for cached in (aoa._period_opens, aoa._slipped_signing):
        dropped += cached.cache_info().currsize
        cached.cache_clear()
    return dropped + aoa_spelling.forget_words()
