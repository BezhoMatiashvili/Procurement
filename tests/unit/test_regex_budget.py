"""The regex budget harness - and, through it, every pattern in the application.

The scan at the bottom is the standing guarantee: no compiled pattern in the package, handed the
strings that can reach it (``bound_text`` has been applied), takes long in the way it is called.
The controls above it are what make a clean scan mean something: the harness must find the
patterns that really did freeze this server, and must stay quiet about linear ones.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence

import pytest

from procurement_agent.extraction import rules
from procurement_agent.ingestion.text_limits import bound_text
from tests import regex_budget as rb

#: The contract-number reference as it was before the fix (``_REFERENCE_GROUP`` without the atomic
#: group), copied from the original source: the ``/`` inside a group's own character class was also
#: the separator between groups, so ``1/1/1/...`` could be split exponentially many ways (0.84 s at
#: 46 characters, doubling with every group).
_KA_LETTER_GROUP = r"(?<![ა-ჰ])[ა-ჰ](?![ა-ჰ])"
_KA_LETTER_LAST = r"(?<![ა-ჰ])(?![სი])[ა-ჰ](?![ა-ჰ])"
_PRE_FIX_REFERENCE_GROUP = (
    rf"(?:{_KA_LETTER_GROUP}[\-/_.](?=[A-Za-z0-9]))?"
    r"[A-Za-z0-9](?:[A-Za-z0-9\-/_.]{0,30}[A-Za-z0-9])?"
    rf"(?:[\-/_.]{_KA_LETTER_LAST})?"
)
_PRE_FIX_REFERENCE = (
    rf"(?=[A-Za-z0-9ა-ჰ\-/_.]*\d){_PRE_FIX_REFERENCE_GROUP}"
    rf"(?:\s*/\s*{_PRE_FIX_REFERENCE_GROUP})*"
)
_PRE_FIX = re.compile(rf"№\s*({_PRE_FIX_REFERENCE})\s*წლის", re.IGNORECASE)


def _kinds(findings: list[rb.Finding]) -> set[str]:
    return {f.kind for f in findings}


# --- the harness finds what it should ----------------------------------------------------------


def test_it_finds_the_pattern_that_froze_the_server() -> None:
    """Negative control: the generic fuzz that came first missed this one."""
    findings = rb.check_pattern("pre-fix contract number", _PRE_FIX, budget_s=0.2)
    assert findings, "the exponential contract-number pattern was not found"
    assert "over budget" in _kinds(findings)


def test_it_finds_a_plain_quadratic_pattern() -> None:
    quadratic = re.compile(r"\s*,?\s*(?:and\s+)?(\d{1,2})\s*tetri")  # the shape of _TETRI_AFTER
    findings = rb.check_pattern("quadratic", quadratic, budget_s=0.1)
    assert findings
    assert findings[0].large_s is None or findings[0].large_s > 0.1


def test_it_finds_the_cubic_label_pattern_as_it_was_before_the_rewrite() -> None:
    from procurement_agent.checks import boq

    cubic = re.compile(
        rf"\s*(?:[,(]\s*|\s+in\s+|\s+){boq._CURRENCY}\s*\)?\s*$|^\s*(?:\$|₾|€)\s*", re.IGNORECASE
    )
    findings = rb.check_pattern("old label currency", cubic, budget_s=0.03, admit=bound_text)
    assert findings, "the cubic pattern passed at the admitted sizes"


@pytest.mark.parametrize(
    "pattern",
    [
        r"[ა-ჰ]+",
        r"(?:\d{1,3}(?:,\d{3})*)(?:\.\d+)?",
        r"№\s*\d+(?:/\d+)?",
        r"\bGE\d{2}[A-Z0-9]{18}\b",
        r"^\s*$",
    ],
)
def test_it_stays_quiet_about_linear_patterns(pattern: str) -> None:
    assert rb.check_pattern("linear", re.compile(pattern), budget_s=0.25) == []


def test_the_fixed_contract_number_pattern_is_clean() -> None:
    assert rb.check_pattern("fixed", rules._CONTRACT_NO_BEFORE, budget_s=0.25) == []


def test_a_runaway_match_is_interrupted_not_waited_for() -> None:
    import time

    started = time.perf_counter()
    assert rb._time(re.compile(r"(a+)+b"), "a" * 60, 0.2) is None
    assert time.perf_counter() - started < 2.0


# --- a busy machine is not a slow pattern ----------------------------------------------------


class _Readings:
    """Stands in for ``rb._time``: each string gets the readings given for its size, one per call,
    and the last one for every call after. ``None`` is a run that was interrupted at the budget."""

    def __init__(self, small: Sequence[float | None], large: Sequence[float | None]) -> None:
        self.small, self.large = small, large
        self.calls: Counter[str] = Counter()

    def __call__(
        self, pattern: re.Pattern[str], text: str, budget_s: float, mode: str = "search"
    ) -> float | None:
        readings = self.large if len(text) > 2250 else self.small
        n = self.calls[text]
        self.calls[text] += 1
        return readings[min(n, len(readings) - 1)]

    def most_timings_of_one_string(self) -> int:
        return max(self.calls.values())


def _check(monkeypatch: pytest.MonkeyPatch, readings: _Readings) -> list[rb.Finding]:
    monkeypatch.setattr(rb, "_time", readings)
    return rb.check_pattern("a busy machine", re.compile(r"\d+"), budget_s=0.25)


@pytest.mark.parametrize(
    ("small", "large"),
    [
        ([0.012], [0.043, 0.026, 0.025]),  # the audit run: 0.012 s -> 0.043 s, linear when quiet
        ([0.012], [0.043, 0.043, 0.025]),  # two busy readings in a row, the third quiet
        ([0.012], [None, 0.030, 0.025]),  # interrupted at the budget once, then well inside it
        ([None, 0.012], [0.030]),  # the small size interrupted once
        ([0.020, 0.012], [0.080, 0.024]),  # both sizes busy at first, the large one more
    ],
)
def test_a_reading_inflated_by_a_busy_machine_is_not_a_finding(
    monkeypatch: pytest.MonkeyPatch, small: list[float | None], large: list[float | None]
) -> None:
    assert _check(monkeypatch, _Readings(small, large)) == []


def test_a_slowdown_that_every_timing_shows_is_a_finding_with_the_best_timings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readings = _Readings([0.012, 0.015, 0.013], [0.090, 0.070, 0.080])
    findings = _check(monkeypatch, readings)
    assert findings
    assert {f.kind for f in findings} == {"superlinear"}
    assert (findings[0].small_s, findings[0].large_s) == (0.012, 0.070)
    assert readings.most_timings_of_one_string() == 3, "re-timed up to three times, no more"


def test_a_pattern_interrupted_every_time_is_over_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    readings = _Readings([0.012], [None])
    findings = _check(monkeypatch, readings)
    assert findings
    assert {f.kind for f in findings} == {"over budget"}
    assert findings[0].large_s is None
    assert readings.most_timings_of_one_string() == 3


def test_a_pattern_that_is_fast_is_timed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    readings = _Readings([0.0001], [0.0002])
    assert _check(monkeypatch, readings) == []
    assert readings.most_timings_of_one_string() == 1


# --- how it works ------------------------------------------------------------------------------


def test_the_representatives_cover_a_separator_a_digit_and_a_letter() -> None:
    chars = rb.alphabet(re.compile(r"[A-Za-z0-9\-/_.]+\s*,"))
    picked = rb._representatives(chars)
    assert len(picked) <= 8
    assert any(c.isspace() for c in picked)
    assert any(c.isdigit() for c in picked)
    assert any(c.isalpha() for c in picked)
    assert "/" in picked and "-" in picked
    labels = [label for label, _ in rb.candidates(re.compile(r"[A-Za-z0-9\-/_.]+\s*,"))]
    pair = re.compile(r"'(?:/''\d'|\d''/')\*n")
    assert any(pair.fullmatch(label) for label in labels), "no digit-slash alternation: 1/1/1/..."


def test_the_alphabet_is_built_from_the_patterns_own_characters() -> None:
    chars = rb.alphabet(re.compile(r"[ა-ჰ]+\s*№?-(?:x|y)\d"))
    assert {"ა", "ჰ", "№", "-", "x", "y"} <= set(chars)
    assert {" ", "\n"} & set(chars)
    assert any(c.isdigit() for c in chars)


def test_the_alphabet_of_an_unparsable_pattern_falls_back_to_the_generic_one() -> None:
    assert rb.alphabet(re.compile("a")) and " " in rb.alphabet(re.compile("a"))


def test_the_candidates_include_runs_alternations_and_framed_runs() -> None:
    labels = [label for label, _ in rb.candidates(re.compile(r"\s*,?\s*\d"))]
    assert any(label.endswith("*n") and "'" in label for label in labels)
    assert any("+" in label and "*n+" in label for label in labels)  # prefix + run + suffix
    texts = [make(100) for _, make in rb.candidates(re.compile(r"\s*,?\s*\d"))]
    assert all(isinstance(t, str) and 50 <= len(t) <= 150 for t in texts)


def test_modes_follow_how_the_package_calls_a_pattern() -> None:
    assert rb.modes_of("procurement_agent.extraction.rules._TETRI_AFTER") == {"match"}
    assert "search" in rb.modes_of("procurement_agent.extraction.rules._CONTRACT_NO_BEFORE")
    assert rb.modes_of("procurement_agent.nowhere._NEVER_CALLED") == {"search"}  # assume a scan


def test_discovery_finds_the_packages_patterns_once_each() -> None:
    found = rb.discover()
    assert len(found) > 400
    keys = [(p.pattern, p.flags) for _, p in found]
    assert len(keys) == len(set(keys))
    assert any(name.endswith("rules._CONTRACT_NO_BEFORE") for name, _ in found)


# --- the standing guarantee --------------------------------------------------------------------


def test_no_pattern_in_the_application_is_slow_on_what_can_reach_it() -> None:
    """Every compiled pattern, in the way it is called, on strings built from its own characters
    after the text limits have been applied: none may take 100 ms (a regular, linear pattern takes
    microseconds; the cubic one this found took 94 ms)."""
    report = rb.scan(budget_s=0.1, admit=bound_text)
    assert report.patterns > 400
    assert report.trials > 20_000
    assert [f.describe() for f in report.findings] == []
