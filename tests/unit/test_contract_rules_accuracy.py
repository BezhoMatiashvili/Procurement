"""Client rule 10, measured: acts written from scratch against their contracts, one planted
difference a case (``scripts/contract_rules_accuracy.py``). Every case must come out exactly
right - the rule that reports the difference, and the contract's value as its correction in the
act's own form.
"""

from __future__ import annotations

import importlib.util
import random
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "contract_rules_accuracy.py"
_spec = importlib.util.spec_from_file_location("contract_rules_accuracy", _SCRIPT)
assert _spec is not None and _spec.loader is not None
harness: ModuleType = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = harness  # its dataclasses look themselves up there
_spec.loader.exec_module(harness)


@pytest.mark.parametrize("seed", [41, 42])
def test_every_rule_10_case_comes_out_right(seed: int) -> None:
    passed, failures = harness.run(5, seed)
    assert [(c.cls, why) for c, why in failures] == []
    assert sum(passed.values()) == 5 * len(harness.CLASSES)


def _subject_case(subject: str, act_subject: str) -> object:
    p = harness.make_pair(random.Random(3))
    p.subject = subject
    # the act lists five of something too: the same digits, written right, in another line
    p.services = tuple(s.format(k=5) for s in harness.SERVICES[1])
    p.act["subject"] = act_subject
    return harness.Case(
        "subject_number",
        harness._act(p),
        harness._contract(p),
        (),
        harness._act(replace(p, act={})),
    )


def test_a_subject_number_is_corrected_where_the_subject_writes_it_alone() -> None:
    """The subject's "5" for the contract's "4" is rewritten in the subject; the service list's
    own "5", written right, is not (red team R6: the marking check of the harness)."""
    case = _subject_case(
        "4 ერთეული სერვერის ტექნიკური მომსახურების გაწევა",
        "5 ერთეული სერვერის ტექნიკური მომსახურების გაწევა",
    )
    marked: Counter[str] = Counter()
    assert harness.score(case, marked) is None
    assert marked["subject_number:right"] == marked["subject_number:wrong"] == 1


def test_a_subject_number_with_its_words_is_left_to_its_comment() -> None:
    """Near miss: "5 (ხუთი)" corrected to "4" would read "4 (ხუთი)": the comment says what to
    write, and nothing is rewritten."""
    case = _subject_case(
        "4 (ოთხი) ერთეული სერვერის ტექნიკური მომსახურების გაწევა",
        "5 (ხუთი) ერთეული სერვერის ტექნიკური მომსახურების გაწევა",
    )
    marked: Counter[str] = Counter()
    assert harness.score(case, marked) is None
    assert marked["subject_number:right"] == 0


def test_a_declined_name_is_never_planted_as_a_slip() -> None:
    """The harness's own guard: a slip of a name's last letter that only declines it ("ჯგუფი"
    written "ჯგუფს") is the same name, which the check rightly accepts."""
    rng = random.Random(7)
    slips = {harness._slip("ჯგუფი", rng) for _ in range(500)}
    assert not slips & {"ჯგუფს", "ჯგუფო"}
