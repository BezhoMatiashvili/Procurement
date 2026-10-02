"""Client rule 1, measured: random parties, their registry extracts, the act's planted mistakes.

The harness is ``scripts/registry_accuracy.py``: each case invents two companies, writes their
extracts in the 2011 or 2025 layout, reads them back with the application's own reader, and
plants one class of mistake (or none) in the act. Every case must come out exactly right.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "registry_accuracy.py"
_spec = importlib.util.spec_from_file_location("registry_accuracy", _SCRIPT)
assert _spec is not None and _spec.loader is not None
harness: ModuleType = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = harness  # its dataclasses look themselves up there
_spec.loader.exec_module(harness)


@pytest.mark.parametrize("seed", [21, 22])
def test_every_registry_case_comes_out_right(seed: int) -> None:
    passed, failures = harness.run(40, seed)
    assert [(c.cls, why) for c, why in failures] == []
    assert sum(passed.values()) == 40 * len(harness.CLASSES)
