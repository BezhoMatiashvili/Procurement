"""Client rules 3 and 4, measured: acts written from scratch in three layouts, three date styles
and five ways of saying the work was done (``scripts/act_rules_accuracy.py``). Every case must
come out exactly right - the project and cadastral notes, and the act's date against the work's.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "act_rules_accuracy.py"
_spec = importlib.util.spec_from_file_location("act_rules_accuracy", _SCRIPT)
assert _spec is not None and _spec.loader is not None
harness: ModuleType = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = harness  # its dataclass looks itself up there
_spec.loader.exec_module(harness)


@pytest.mark.parametrize("seed", [31, 32])
def test_every_rule_3_and_4_case_comes_out_right(seed: int) -> None:
    passed, failures = harness.run(25, seed)
    assert [(c.cls, why) for c, why in failures] == []
    assert sum(passed.values()) == 25 * len(harness.CLASSES)
