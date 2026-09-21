"""The evaluation harness. It is the instrument every accuracy claim rests on, so it is
tested like one: a scorer that quietly marks correct answers wrong sends the whole project
optimising something that was never broken."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from procurement_agent.eval.gold import NO_FINDINGS, load_gold_set
from procurement_agent.eval.score import (
    SetScore,
    fact_matches,
    score_extraction,
    score_findings,
    values_match,
)
from procurement_agent.extraction.schemas import (
    ExtractedFact,
    ExtractionResult,
    FactType,
    Provenance,
)


def _fact(fact_type: FactType, value: str, *, normalized=None, source="rules") -> ExtractedFact:
    return ExtractedFact(
        type=fact_type,
        label="x",
        value=value,
        normalized=normalized,
        raw_text=value,
        provenance=Provenance(),
        source=source,  # type: ignore[arg-type]
        confidence=0.7,
    )


def _result(facts: list[ExtractedFact]) -> ExtractionResult:
    return ExtractionResult(document_id="d", facts=facts, targets=[])


class TestValueMatching:
    def test_money_compares_numerically(self) -> None:
        assert values_match(FactType.total_amount, "48750.00", "48 750,00")

    def test_names_ignore_inflection(self) -> None:
        assert values_match(FactType.party_director, "ნინო კაპანაძე", "ნინო კაპანაძის")

    def test_different_names_do_not_match(self) -> None:
        assert not values_match(FactType.party_director, "ნინო კაპანაძე", "ნინო კაპანიძე")

    def test_the_canonical_value_is_compared_as_well_as_the_displayed_one(self) -> None:
        """A displayed amount reads '48 750,00 GEL'; the gold file states 48750.0."""
        fact = _fact(FactType.total_amount, "48 750,00 GEL", normalized=48750.0)
        assert not values_match(FactType.total_amount, "48750.00", fact.value)
        assert fact_matches(FactType.total_amount, "48750.00", fact)


class TestScoring:
    def test_a_perfect_extraction_scores_one(self) -> None:
        result = _result([_fact(FactType.party_id_code, "123456789")])
        score = score_extraction(result, {FactType.party_id_code: ["123456789"]}, "d")
        fs = score.fields[FactType.party_id_code]
        assert (fs.precision, fs.recall, fs.f1) == (1.0, 1.0, 1.0)

    def test_an_extra_value_costs_precision_not_recall(self) -> None:
        result = _result(
            [_fact(FactType.party_id_code, "123456789"), _fact(FactType.party_id_code, "999")]
        )
        score = score_extraction(result, {FactType.party_id_code: ["123456789"]}, "d")
        fs = score.fields[FactType.party_id_code]
        assert fs.recall == 1.0
        assert fs.precision == pytest.approx(0.5)

    def test_a_missing_value_costs_recall(self) -> None:
        score = score_extraction(_result([]), {FactType.party_id_code: ["123456789"]}, "d")
        fs = score.fields[FactType.party_id_code]
        assert (fs.recall, fs.false_negatives) == (0.0, 1)

    def test_a_field_absent_from_the_gold_file_is_not_scored(self) -> None:
        """A partial gold file must be honest, not penalise fields nobody has labelled."""
        result = _result([_fact(FactType.penalty, "10%")])
        score = score_extraction(result, {}, "d")
        assert score.fields == {}

    def test_anchor_hit_rate_is_tracked_apart_from_correctness(self) -> None:
        """Regexes tuned to one template can score well on it and be useless on the next;
        tracking whether the anchor fired at all is what makes that visible."""
        anchored = _result([_fact(FactType.party_name, "შპს ალფა", source="anchor")])
        guessed = _result([_fact(FactType.party_name, "შპს ალფა", source="llm")])
        expected = {FactType.party_name: ["შპს ალფა"]}
        a = score_extraction(anchored, expected, "a")
        b = score_extraction(guessed, expected, "b")
        assert a.fields[FactType.party_name].f1 == b.fields[FactType.party_name].f1 == 1.0
        assert a.fields[FactType.party_name].anchored
        assert not b.fields[FactType.party_name].anchored
        assert SetScore("s", [a, b]).anchor_hit_rate(FactType.party_name) == pytest.approx(0.5)


class TestFindingScoring:
    def test_an_expected_rule_that_did_not_fire_is_missing(self) -> None:
        missing, unexpected = score_findings([], ["AOA.ACT_DATE"], [], must_be_clean=False)
        assert missing == ["AOA.ACT_DATE"] and unexpected == []

    def test_a_document_marked_clean_rejects_any_finding(self) -> None:
        _missing, unexpected = score_findings(["AOA.FEE"], [], [NO_FINDINGS], must_be_clean=True)
        assert unexpected == ["AOA.FEE"]

    def test_unrelated_findings_are_allowed_unless_forbidden(self) -> None:
        missing, unexpected = score_findings(
            ["AOA.FEE", "AOA.BANK"], ["AOA.FEE"], ["AOA.ACT_DATE"], must_be_clean=False
        )
        assert missing == [] and unexpected == []


class TestGoldLoading:
    def test_a_gold_file_pointing_at_a_missing_document_fails_loudly(self, tmp_path: Path) -> None:
        (tmp_path / "x.yaml").write_text(
            yaml.safe_dump({"document": "nowhere.docx"}), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="missing document"):
            load_gold_set(tmp_path, root=tmp_path)

    def test_relative_paths_resolve_against_the_root(self, tmp_path: Path) -> None:
        doc = tmp_path / "a.docx"
        doc.write_bytes(b"x")
        (tmp_path / "a.yaml").write_text(
            yaml.safe_dump({"document": "a.docx", "fields": {"party_name": ["x"]}}),
            encoding="utf-8",
        )
        gold = load_gold_set(tmp_path, root=tmp_path)
        assert len(gold) == 1
        assert next(iter(gold)).document == doc

    def test_an_unknown_key_is_rejected_rather_than_ignored(self, tmp_path: Path) -> None:
        """A typo in a gold file must not silently disable an expectation."""
        doc = tmp_path / "a.docx"
        doc.write_bytes(b"x")
        (tmp_path / "a.yaml").write_text(
            yaml.safe_dump({"document": "a.docx", "expected_finding": ["X"]}), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="expected_finding"):
            load_gold_set(tmp_path, root=tmp_path)
