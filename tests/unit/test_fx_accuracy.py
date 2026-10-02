"""Client rule 9, measured: random foreign-currency acts and their invoices.

The harness is ``scripts/fx_accuracy.py``.

Every case is written from scratch with invented parties and random rates, and the invoice the
act check works out is compared with one computed independently from the case's numbers. Two
general slips this harness found, each a rule of its own below: a dot inside a date or a figure
("30.08.2026") and the stop of "2026 წ." in mid-sentence cut a keyword off from its amount.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from procurement_agent.checks.aoa import DocumentView
from procurement_agent.extraction.rules import nearest_keyword_group

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fx_accuracy.py"
_spec = importlib.util.spec_from_file_location("fx_accuracy", _SCRIPT)
assert _spec is not None and _spec.loader is not None
harness: ModuleType = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = harness  # its dataclass looks itself up there
_spec.loader.exec_module(harness)


@pytest.mark.parametrize("seed", [11, 12])
def test_every_random_invoice_is_exact_to_the_tetri(seed: int) -> None:
    passed, failures = harness.run(150, seed)
    assert [why for _, why in failures] == []
    assert passed == 150


@pytest.mark.parametrize(
    "sentence",
    [
        "ავანსის სახით 30.08.2026 ჩარიცხულია 2 000.00 USD.",
        "ავანსის სახით 1 იანვარს, 2026 წ. ჩარიცხულია 2 000.00 USD.",
        "ავანსი (10.5%) შეადგენს 2 000.00 USD.",
    ],
)
def test_a_dot_in_a_date_or_a_figure_does_not_hide_the_advance(sentence: str) -> None:
    view = DocumentView.build("a", "3. საფასური შეადგენს 10 000.00 USD. " + sentence)
    assert [r.role for r in view.amounts()] == ["price", "advance"]


def test_a_full_stop_still_ends_the_sentence() -> None:
    """The keyword of the sentence before stays there: "ქ." and a stop after a word still cut."""
    text = "ავანსი გადაიხადა. შემდეგ 2 000 USD"
    start = text.index("2 000")
    assert nearest_keyword_group(text, start, start + 9, {"advance": ("ავანს",)}) is None
    city = "ავანსი ქ. თბილისში 2 000 USD"
    at = city.index("2 000")
    assert nearest_keyword_group(city, at, at + 9, {"advance": ("ავანს",)}) is None
