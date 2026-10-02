"""Client rules 5-8, measured (``scripts/tax_rules_accuracy.py``): the tax wording an act must
carry by contractor type - given, or read from the act where its legal form, code and stated
status agree, and never guessed where they do not. Every case must come out exactly right.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "tax_rules_accuracy.py"
_spec = importlib.util.spec_from_file_location("tax_rules_accuracy", _SCRIPT)
assert _spec is not None and _spec.loader is not None
harness: ModuleType = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = harness  # its dataclass looks itself up there
_spec.loader.exec_module(harness)


@pytest.mark.parametrize("seed", [41, 42])
def test_every_tax_wording_case_comes_out_right(seed: int) -> None:
    passed, failures = harness.run(25, seed)
    assert [(c.cls, why) for c, why in failures] == []
    assert sum(passed.values()) == 25 * len(harness.CLASSES)
