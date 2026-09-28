"""The optional model pass over a comparison.

Everything here is about what the model is *not* allowed to do. It cannot introduce a change,
it cannot name a change it was not given, it cannot quote words that are not in the passage it
claims to quote, and it can only ever move a change down in importance. The deterministic half
must survive every one of those failures intact.
"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from procurement_agent.checks.compare import ComparisonReport, compare
from procurement_agent.checks.compare_llm import (
    ExplanationSet,
    LLMChange,
    LLMComparison,
    apply_explanations,
    explain,
    request_explanations,
)
from procurement_agent.extraction.schemas import ChoiceResult
from procurement_agent.ingestion.segmenter import build
from procurement_agent.llm.base import LLMError

T = TypeVar("T", bound=BaseModel)

OLD = "4. გადახდის პირობები. ანგარიშსწორება ხდება მიღება-ჩაბარების აქტის საფუძველზე."
NEW = "4. გადახდის პირობები. ანგარიშსწორება ხდება მხოლოდ წერილობითი მოთხოვნის საფუძველზე."


class FakeLLM:
    """Returns canned items; records what it was asked. Same shape as tests/unit/test_pipeline."""

    def __init__(
        self, items: list[LLMChange] | None = None, error: Exception | None = None
    ) -> None:
        self.items = items or []
        self.error = error
        self.calls: list[str] = []

    def generate_structured(
        self, *, system: str, user: str, schema: type[T], model: str | None = None
    ) -> T:
        self.calls.append(user)
        assert "IGNORE them" in system, "the prompt must refuse instructions inside the document"
        if self.error is not None:
            raise self.error
        return LLMComparison(items=self.items)  # type: ignore[return-value]

    def generate_choice(  # pragma: no cover - unused
        self, *, document: str, question: str, options: object, model: object = None
    ) -> ChoiceResult:
        raise NotImplementedError

    def health(self) -> dict[str, object]:  # pragma: no cover - unused
        return {}


def segs(texts: list[str]) -> list[object]:
    return build([(t, "text", f"¶{i + 1}", None, None) for i, t in enumerate(texts)])  # type: ignore[misc]


def report_with_one_rewrite() -> ComparisonReport:
    report = compare(
        segs(["ხელშეკრულება № SY-2026/004", OLD]),  # type: ignore[arg-type]
        segs(["ხელშეკრულება № SY-2026/004", NEW]),  # type: ignore[arg-type]
        left_document_id="L",
        right_document_id="R",
        left_kind="docx",
        right_kind="docx",
    )
    assert len(report.unexplained) == 1, "the fixture must leave exactly one change unexplained"
    return report


def good_item(hunk_id: str, *, material: bool = True) -> LLMChange:
    return LLMChange(
        id=hunk_id,
        kind="rewritten",
        material=material,
        summary_en="Payment now requires a written request.",
        summary_ka="ანგარიშსწორება ახლა წერილობით მოთხოვნას საჭიროებს.",
        quote_left="მიღება-ჩაბარების აქტის",
        quote_right="წერილობითი მოთხოვნის",
    )


def test_only_the_changes_the_rules_could_not_name_are_sent() -> None:
    """A change with a value, a formatting change and a move all have nothing to ask about."""
    report = compare(
        segs(["ღირებულება 12 400,00 ლარი", OLD]),  # type: ignore[arg-type]
        segs(["ღირებულება 15 900,00 ლარი", NEW]),  # type: ignore[arg-type]
        left_document_id="L",
        right_document_id="R",
    )
    llm = FakeLLM()
    request_explanations(report, llm)
    assert len(llm.calls) == 1
    assert OLD in llm.calls[0]
    assert "12 400,00" not in llm.calls[0]


def test_the_whole_batch_goes_in_one_call() -> None:
    """One prefill for the batch. docs/ACCURACY.md measured serial calls as no cheaper."""
    left = [f"{i}. ძველი ფორმულირება პუნქტში ნომერი {i}." for i in range(6)]
    right = [f"{i}. ახალი ფორმულირება პუნქტში ნომერი {i}." for i in range(6)]
    report = compare(
        segs(left),  # type: ignore[arg-type]
        segs(right),  # type: ignore[arg-type]
        left_document_id="L",
        right_document_id="R",
    )
    llm = FakeLLM()
    request_explanations(report, llm, max_hunks=8)
    assert len(llm.calls) == 1
    assert llm.calls[0].count("<change") == len(report.unexplained)


def test_an_answer_is_attached_to_its_own_change() -> None:
    report = report_with_one_rewrite()
    hunk_id = report.unexplained[0].id
    out = explain(report, FakeLLM([good_item(hunk_id)]))
    hunk = out.hunk(hunk_id)
    assert hunk is not None
    assert hunk.explained_by == "llm"
    assert hunk.explanation_ka and hunk.explanation_en
    assert out.llm_explained == 1


def test_an_invented_quote_is_refused() -> None:
    report = report_with_one_rewrite()
    item = good_item(report.unexplained[0].id).model_copy(
        update={"quote_right": "ეს სიტყვები დოკუმენტში არ წერია"}
    )
    out = explain(report, FakeLLM([item]))
    assert out.llm_explained == 0
    assert any("quote" in s.reason_en for s in out.skipped)


def test_an_id_the_model_was_not_given_is_refused() -> None:
    report = report_with_one_rewrite()
    out = explain(report, FakeLLM([good_item("h999")]))
    assert out.llm_explained == 0
    assert out.hunks == report.hunks


def test_the_model_can_only_move_a_change_down() -> None:
    report = report_with_one_rewrite()
    hunk_id = report.unexplained[0].id
    before = report.hunk(hunk_id)
    assert before is not None and before.materiality == "substantive"

    out = explain(report, FakeLLM([good_item(hunk_id, material=False)]))
    after = out.hunk(hunk_id)
    assert after is not None and after.materiality == "cosmetic"
    assert out.counts.cosmetic == report.counts.cosmetic + 1
    assert out.counts.substantive == report.counts.substantive - 1


def test_the_model_cannot_promote_a_change() -> None:
    """A critical change is never sent, so there is nothing for the model to raise."""
    report = compare(
        segs(["ღირებულება 12 400,00 ლარი"]),  # type: ignore[arg-type]
        segs(["ღირებულება 15 900,00 ლარი"]),  # type: ignore[arg-type]
        left_document_id="L",
        right_document_id="R",
    )
    assert report.unexplained == []
    out = explain(report, FakeLLM([good_item("h1")]))
    assert out.counts.critical == report.counts.critical
    assert out.hunks[0].explained_by is None


def test_the_cap_is_respected_and_reported() -> None:
    left = [f"{i}. ძველი ფორმულირება პუნქტში ნომერი {i}." for i in range(6)]
    right = [f"{i}. ახალი ფორმულირება პუნქტში ნომერი {i}." for i in range(6)]
    report = compare(
        segs(left),  # type: ignore[arg-type]
        segs(right),  # type: ignore[arg-type]
        left_document_id="L",
        right_document_id="R",
    )
    stored = request_explanations(report, FakeLLM(), max_hunks=2)
    assert any("not sent to the model" in n.reason_en for n in stored.notes)


def test_a_model_failure_leaves_the_deterministic_half_intact() -> None:
    """Losing the comparison because an optional explanation failed would be a regression."""
    report = report_with_one_rewrite()
    out = explain(report, FakeLLM(error=LLMError("connection refused")))
    assert out.hunks == report.hunks
    assert out.findings == report.findings
    assert any("did not answer" in s.reason_en for s in out.skipped)


def test_a_stored_run_is_validated_again_when_it_is_read_back() -> None:
    """The comparison is recomputed on every page load; a stored answer has to still fit it."""
    report = report_with_one_rewrite()
    hunk_id = report.unexplained[0].id
    stored = ExplanationSet(model="gemma3:4b", items=[good_item(hunk_id)])
    assert apply_explanations(report, stored).llm_explained == 1

    # The same stored run against a comparison whose text has moved on.
    other = compare(
        segs(["ხელშეკრულება № SY-2026/004", OLD]),  # type: ignore[arg-type]
        segs(["ხელშეკრულება № SY-2026/004", "სულ სხვა ფორმულირება ამ პუნქტში."]),  # type: ignore[arg-type]
        left_document_id="L",
        right_document_id="R",
    )
    assert apply_explanations(other, stored).llm_explained == 0


def test_an_empty_summary_is_refused() -> None:
    report = report_with_one_rewrite()
    item = good_item(report.unexplained[0].id).model_copy(update={"summary_ka": "  "})
    assert explain(report, FakeLLM([item])).llm_explained == 0


def test_a_number_written_in_words_is_substance_the_model_cannot_demote() -> None:
    from procurement_agent.checks.compare import Hunk, InlineOp
    from procurement_agent.checks.compare_llm import _changes_substance

    hunk = Hunk(
        id="h1",
        kind="changed",
        materiality="substantive",
        rules=["CMP.TEXT_CHANGED"],
        inline=[
            InlineOp(tag="equal", left="გადახდა ", right="გადახდა "),
            InlineOp(tag="replace", left="ხუთი", right="ოცდაათი"),
            InlineOp(tag="equal", left=" დღეში", right=" დღეში"),
        ],
    )
    assert _changes_substance(hunk)
