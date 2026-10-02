"""Client rule 10, measured: acts written from scratch against their contracts, one planted
difference a case (``scripts/contract_rules_accuracy.py``). Every case must come out exactly
right - the rule that reports the difference, and the contract's value as its correction in the
act's own form.
"""

from __future__ import annotations

import importlib.util
import random
import sys
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


def test_a_declined_name_is_never_planted_as_a_slip() -> None:
    """The harness's own guard: a slip of a name's last letter that only declines it ("ჯგუფი"
    written "ჯგუფს") is the same name, which the check rightly accepts."""
    rng = random.Random(7)
    slips = {harness._slip("ჯგუფი", rng) for _ in range(500)}
    assert not slips & {"ჯგუფს", "ჯგუფო"}
