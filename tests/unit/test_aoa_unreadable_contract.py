"""A contract that was supplied and has no readable text is not 'no contract supplied': the
report says what happened, and the contract rules do not run against nothing."""

from __future__ import annotations

import pytest

from procurement_agent.checks.aoa import check_aoa
from tests.degraded_support import ACT_LINES, CONTRACT_LINES

ACT = "\n".join(ACT_LINES)
CONTRACT = "\n".join(CONTRACT_LINES)


def _skips(report: object) -> dict[str, object]:
    return {s.rule_id: s for s in report.skipped}  # type: ignore[attr-defined]


# --- finding 4: "no contract supplied" and "the contract has no text" are different things ------


def test_no_contract_at_all_still_reads_no_contract_supplied() -> None:
    skips = _skips(check_aoa(aoa_text=ACT))
    assert skips["AOA.CONTRACT_*"].reason_en == "no contract supplied; act checked on its own"  # type: ignore[attr-defined]
    assert "AOA.CONTRACT_UNREADABLE" not in skips


@pytest.mark.parametrize("text", ["", "   ", "\n\t \n"])
def test_a_contract_with_no_readable_text_is_said_to_be_unreadable_not_missing(text: str) -> None:
    report = check_aoa(aoa_text=ACT, contract_text=text, contract_id="c" * 32)
    skips = _skips(report)
    assert "AOA.CONTRACT_*" not in skips, "it must not read as 'no contract supplied'"
    note = skips["AOA.CONTRACT_UNREADABLE"]
    assert "no readable text" in note.reason_en  # type: ignore[attr-defined]
    assert note.reason_ka and note.reason_ka != note.reason_en  # type: ignore[attr-defined]
    assert "supplied" not in note.reason_en.lower()  # type: ignore[attr-defined]
    assert report.reference_document_id == "c" * 32


@pytest.mark.parametrize("text", ["", "  \n "])
def test_the_contract_rules_do_not_run_against_nothing(text: str) -> None:
    """An empty contract view would make every contract rule report a 'missing' value."""
    none = check_aoa(aoa_text=ACT)
    empty = check_aoa(aoa_text=ACT, contract_text=text)
    assert [f.model_dump() for f in empty.findings] == [f.model_dump() for f in none.findings]


def test_a_readable_contract_is_unchanged() -> None:
    report = check_aoa(aoa_text=ACT, contract_text=CONTRACT)
    assert "AOA.CONTRACT_*" not in _skips(report)
    assert "AOA.CONTRACT_UNREADABLE" not in _skips(report)
    assert report.reference_document_id == "contract"
