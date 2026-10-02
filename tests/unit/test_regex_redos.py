"""No pattern that reads an untrusted document may backtrack exponentially.

CPython's ``re`` holds the GIL for the whole of a search and cannot be interrupted, so one slow
search stops every request, the job worker and the health check at once. The contract-number
reference (``rules._REFERENCE``) split a chain like ``1/1/1/...`` in exponentially many ways
(its inner class contains "/", and so did the separator between its groups), so a number sign
followed by about sixty characters froze the whole service for minutes - and again each time
the document was viewed.

The probe runs in a child process with a wall-clock limit: a regression must fail the test, not
hang the suite.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from procurement_agent.extraction import rules

#: Wall-clock limit for a whole probe. The patterns need milliseconds; anything near this is a
#: backtracking blow-up.
_LIMIT_S = 30
#: What one chain may cost in total (four texts, three patterns each).
_BUDGET_S = 0.5

_PROBE = textwrap.dedent(
    """
    import time

    from procurement_agent.checks import aoa
    from procurement_agent.extraction import rules

    UNITS = {"plain": "/", "spaced": " / ", "mixed": "/1/1/1 / "}
    for groups, style in ((60, "plain"), (400, "plain"), (60, "spaced"), (200, "mixed")):
        chain = UNITS[style].join(["1"] * groups)
        started = time.perf_counter()
        # Texts that leave every pattern failing after the reference: no contract word follows
        # the number, no signing word follows the contract. That is the case that went
        # exponential.
        for text in (
            "\\u2116 " + chain,
            "\\u2116 " + chain + " \\u10d3\\u10d0",
            "\\u10ee\\u10d4\\u10da\\u10e8\\u10d4\\u10d9\\u10e0\\u10e3\\u10da\\u10d4"
            "\\u10d1\\u10d0 \\u2116 " + chain,
            "contract No. " + chain + " and more",
        ):
            rules.find_contract_numbers(text)
            aoa._CONTRACT_REFERENCE.match(text)
            aoa._CONTRACT_DATED_BEFORE.search(text)
        print(f"{groups}-{style} {time.perf_counter() - started:.4f}", flush=True)
    """
)


def test_long_reference_chains_do_not_stall_the_number_reader() -> None:
    cases = ["60-plain", "400-plain", "60-spaced", "200-mixed"]
    try:
        done = subprocess.run(
            [sys.executable, "-c", _PROBE],
            capture_output=True,
            text=True,
            timeout=_LIMIT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        finished = [line.split()[0] for line in out.splitlines() if line.strip()]
        stuck = next((c for c in cases if c not in finished), "?")
        pytest.fail(f"the {stuck} reference chain stalled a pattern for {_LIMIT_S} s")
    assert done.returncode == 0, done.stderr[-500:]
    seconds = {line.split()[0]: float(line.split()[1]) for line in done.stdout.splitlines()}
    assert set(seconds) == set(cases)
    assert all(t < _BUDGET_S for t in seconds.values()), seconds


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ხელშეკრულება № 15/2026", "15/2026"),
        ("ხელშეკრულება № 15 / 2026", "15/2026"),
        ("ხელშეკრულება № 15/ 2026", "15/2026"),
        ("ხელშეკრულება № 15 /2026", "15/2026"),
        ("ხელშეკრულება № 12/2026-ა", "12/2026-ა"),
        ("ხელშეკრულება № ს-45/2026", "ს-45/2026"),
        ("contract No. sp-2026/318", "SP-2026/318"),
        ("№ 27/2026 ხელშეკრულების საფუძველზე", "27/2026"),
        ("ხელშეკრულების №: 45/2026", "45/2026"),
        ("Contract number - 45/2026", "45/2026"),
        ("ხელშეკრულება № 45/2026-ის საფუძველზე", "45/2026"),
        # a chain longer than one group's 32-character reach still reads as one reference
        ("ხელშეკრულება № " + "/".join(["1"] * 12), "/".join(["1"] * 12)),
        ("ხელშეკრულება № " + "/".join(["1"] * 10) + "/ა-7", "/".join(["1"] * 10) + "/ა"),
    ],
)
def test_references_still_read_as_before(text: str, expected: str) -> None:
    assert [h.value for h in rules.find_contract_numbers(text)] == [expected]


def test_a_clause_number_is_still_no_contract_number() -> None:
    assert rules.find_contract_numbers("N 5 პუნქტის") == []
