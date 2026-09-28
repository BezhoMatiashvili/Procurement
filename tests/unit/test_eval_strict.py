"""Strict scoring (docs/GOLD_SPEC.md). Synthetic values only: invented names, codes and banks.

The strict scorer is the yardstick the accuracy work is measured with, so every rule it applies
is pinned here: what counts as the same value, that an unlisted type must be absent, and that a
finding only counts when it points at the right words and proposes the right fix.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from docx import Document
from pydantic import ValidationError

from procurement_agent.checks.findings import CheckReport, Finding, Severity
from procurement_agent.config import Settings
from procurement_agent.eval import runner, score
from procurement_agent.eval.gold import ExpectedFinding, GoldDocument
from procurement_agent.eval.score import (
    STRICT_TARGETS,
    DocumentScore,
    canonical,
    comment_anchors,
    count_marked_places,
    fold,
    score_extraction_strict,
    score_findings_strict,
    strict_totals,
    strict_values_match,
)
from procurement_agent.extraction.schemas import (
    ExtractedFact,
    ExtractionResult,
    FactType,
    Provenance,
)


def _fact(fact_type: FactType, value: str, normalized: str | float | None = None) -> ExtractedFact:
    return ExtractedFact(
        type=fact_type,
        label="x",
        value=value,
        normalized=normalized,
        raw_text=value,
        provenance=Provenance(),
        source="rules",
        confidence=0.7,
    )


def _result(*facts: ExtractedFact) -> ExtractionResult:
    return ExtractionResult(document_id="d", facts=list(facts), targets=[])


def _finding(
    rule: str,
    *,
    quote: str | None = None,
    expected: str | None = None,
    actual: str | None = None,
    severity: Severity = "error",
    message_en: str = "m",
) -> Finding:
    return Finding(
        rule_id=rule,
        severity=severity,
        message_ka="m",
        message_en=message_en,
        expected=expected,
        actual=actual,
        quote=quote,
    )


class TestFold:
    def test_quotes_dashes_and_spaces(self) -> None:
        assert fold("  შპს „ნიმუში“ –  test ") == "შპს ნიმუში - TEST"

    def test_georgian_is_not_upper_cased(self) -> None:
        """str.upper() would turn Mkhedruli into Mtavruli and break every comparison."""
        assert fold("ნიმუში") == "ნიმუში"

    def test_only_latin_is_upper_cased(self) -> None:
        assert fold("abc éé бв") == "ABC ÉÉ бв"

    def test_canonical_prefers_the_normalized_value(self) -> None:
        assert canonical(_fact(FactType.total_amount, "1 200,00 GEL", 1200.0)) == "1200.0"
        assert canonical(_fact(FactType.project_name, "ზღვის ხედი")) == "ზღვის ხედი"


class TestStrictValues:
    def test_names_are_exact_after_folding(self) -> None:
        assert strict_values_match(FactType.party_director, "გიორგი ნიმუშაძე", "გიორგი  ნიმუშაძე")
        assert not strict_values_match(
            FactType.party_director, "გიორგი ნიმუშაძე", "გიორგი ნიმუშაძის"
        )

    def test_no_substring_match(self) -> None:
        assert not strict_values_match(FactType.project_name, "ზღვის ხედი", "ზღვის ხედი 2")
        assert not strict_values_match(FactType.party_name, "შპს ნიმუში", "ნიმუში")

    def test_quotes_do_not_count(self) -> None:
        assert strict_values_match(FactType.party_name, "შპს ნიმუში", "შპს „ნიმუში“")

    def test_money_is_numeric(self) -> None:
        assert strict_values_match(FactType.total_amount, "100", "100.0")
        assert strict_values_match(FactType.price, "2400", "2400.004")
        assert not strict_values_match(FactType.price, "2400", "2400.01")

    def test_money_display_forms_are_not_canonical(self) -> None:
        assert not strict_values_match(FactType.total_amount, "48750", "48 750,00")

    def test_percentages_are_numeric_and_keep_their_period(self) -> None:
        assert strict_values_match(FactType.penalty, "0.5% per day", "0.50% per day")
        assert not strict_values_match(FactType.penalty, "0.5% per day", "0.5% per month")
        assert not strict_values_match(FactType.penalty, "5%", "5")

    def test_quantity_is_text(self) -> None:
        assert strict_values_match(FactType.quantity, "4 item", "4 ITEM")
        assert not strict_values_match(FactType.quantity, "4 item", "4.0 item")

    def test_iso_dates_and_ranges_are_exact(self) -> None:
        assert strict_values_match(FactType.act_date, "2026-08-31", "2026-08-31")
        assert not strict_values_match(FactType.act_date, "2026-08-31", "2026-08-30")
        assert strict_values_match(
            FactType.execution_period, "2026-08-01/2026-08-31", "2026-08-01/2026-08-31"
        )
        assert not strict_values_match(
            FactType.execution_period, "2026-08-01/2026-08-31", "2026-08-01"
        )

    def test_codes_upper_case(self) -> None:
        assert strict_values_match(FactType.bank_code, "ZZBKGE22", "zzbkge22")
        assert strict_values_match(
            FactType.bank_account, "GE00ZZ0000000000000001", "ge00zz0000000000000001"
        )

    def test_subject_word_overlap_tolerates_inflection(self) -> None:
        gold = "საკონსულტაციო მომსახურების გაწევა"
        assert strict_values_match(FactType.subject, gold, "საკონსულტაციო მომსახურება გაწევა")

    def test_subject_needs_most_gold_words(self) -> None:
        gold = "საკონსულტაციო მომსახურების გაწევა ოფისისთვის"
        assert not strict_values_match(FactType.subject, gold, "მომსახურების გაწევა")

    def test_subject_rejects_a_much_longer_fact(self) -> None:
        gold = "დასუფთავება"
        fact = "დასუფთავება და სხვა ბევრი დამატებითი სამუშაო"
        assert not strict_values_match(FactType.subject, gold, fact)


class TestStrictExtraction:
    def test_every_target_is_scored_and_act_date_bank_code_included(self) -> None:
        s = score_extraction_strict(_result(), {}, "d")
        assert set(s.fields) == set(STRICT_TARGETS)
        assert FactType.act_date in s.fields and FactType.bank_code in s.fields

    def test_unlisted_type_is_expected_absent(self) -> None:
        s = score_extraction_strict(_result(_fact(FactType.penalty, "1%")), {}, "d")
        fs = s.fields[FactType.penalty]
        assert (fs.expected, fs.true_positives, fs.false_positives) == (0, 0, 1)
        assert fs.wrong == ["1%"] and fs.precision == 0.0 and fs.recall is None

    def test_ignored_type_is_skipped(self) -> None:
        s = score_extraction_strict(
            _result(_fact(FactType.penalty, "1%")), {}, "d", ignore=[FactType.penalty]
        )
        assert FactType.penalty not in s.fields
        assert s.ignored == [FactType.penalty]

    def test_generic_types_are_unclassified_not_scored(self) -> None:
        s = score_extraction_strict(
            _result(_fact(FactType.amount, "5"), _fact(FactType.date, "2026-01-01")), {}, "d"
        )
        assert s.unclassified_total == 2
        assert FactType.amount not in s.fields
        assert all(fs.false_positives == 0 for fs in s.fields.values())

    def test_hits_misses_and_wrong_values(self) -> None:
        facts = _result(
            _fact(FactType.party_id_code, "000000001"),
            _fact(FactType.party_id_code, "000000009"),
            _fact(FactType.total_amount, "1 200 GEL", 1200.0),
        )
        expected = {
            FactType.party_id_code: ["000000001", "000000002"],
            FactType.total_amount: ["1200"],
        }
        s = score_extraction_strict(facts, expected, "d", provisional=[FactType.total_amount])
        ids = s.fields[FactType.party_id_code]
        assert (ids.true_positives, ids.false_positives, ids.false_negatives) == (1, 1, 1)
        assert ids.missing == ["000000002"] and ids.wrong == ["000000009"]
        assert s.fields[FactType.total_amount].correct
        assert s.provisional == {FactType.total_amount}

    def test_totals_keep_provisional_apart(self) -> None:
        doc = DocumentScore(name="d")
        doc.strict = score_extraction_strict(
            _result(_fact(FactType.subject, "x")),
            {FactType.subject: ["x"], FactType.project_name: ["ზღვის ხედი"]},
            "d",
            provisional=[FactType.subject],
        )
        totals = strict_totals([doc])
        assert FactType.subject in totals.provisional and FactType.subject not in totals.derived
        assert totals.derived[FactType.project_name].false_negatives == 1


class TestStrictFindings:
    NAME_ENTRY = ExpectedFinding(
        rule="AOA.DIRECTOR", text="გიორგი ნიმუშიშვილი", correction="გიორგი ნიმუშაძე"
    )

    def test_text_and_correction_match(self) -> None:
        f = _finding(
            "AOA.DIRECTOR", quote="დირექტორი გიორგი ნიმუშიშვილი", expected="გიორგი ნიმუშაძე"
        )
        result = score_findings_strict([self.NAME_ENTRY], [f], strict=True)
        assert result.ok and len(result.matched) == 1

    def test_text_falls_back_to_actual(self) -> None:
        f = _finding("AOA.DIRECTOR", actual="გიორგი ნიმუშიშვილი", expected="გიორგი ნიმუშაძე")
        assert score_findings_strict([self.NAME_ENTRY], [f]).ok

    def test_wrong_correction_is_missing_and_unexpected(self) -> None:
        f = _finding("AOA.DIRECTOR", quote="გიორგი ნიმუშიშვილი", expected="გიორგი ნიმუშაძის")
        result = score_findings_strict([self.NAME_ENTRY], [f], strict=True)
        assert len(result.missing) == 1 and len(result.unexpected) == 1 and not result.ok

    def test_no_correction_never_matches_a_stated_one(self) -> None:
        f = _finding("AOA.DIRECTOR", quote="გიორგი ნიმუშიშვილი")
        assert score_findings_strict([self.NAME_ENTRY], [f]).missing

    def test_wrong_text_is_missing(self) -> None:
        f = _finding("AOA.DIRECTOR", quote="სხვა სახელი", expected="გიორგი ნიმუშაძე")
        assert score_findings_strict([self.NAME_ENTRY], [f]).missing

    def test_severity_when_stated(self) -> None:
        entry = ExpectedFinding(rule="AOA.FEE", severity="error")
        assert score_findings_strict([entry], [_finding("AOA.FEE", severity="warning")]).missing

    def test_extras_count_only_with_strict_findings(self) -> None:
        extra = _finding("AOA.SPELLING", severity="info")
        assert score_findings_strict([], [extra], strict=False).ok
        result = score_findings_strict([], [extra], strict=True)
        assert len(result.unexpected) == 1 and not result.ok

    def test_allowed_rules_are_not_unexpected(self) -> None:
        extra = _finding("AOA.IBAN_CHECKSUM", severity="info")
        result = score_findings_strict([], [extra], strict=True, allowed=["AOA.IBAN_CHECKSUM"])
        assert result.ok and len(result.allowed) == 1

    def test_second_finding_for_one_mistake_is_a_duplicate(self) -> None:
        entry = ExpectedFinding(rule="AOA.FEE")
        result = score_findings_strict(
            [entry], [_finding("AOA.FEE"), _finding("AOA.FEE")], strict=True
        )
        assert len(result.matched) == 1 and len(result.duplicates) == 1 and not result.ok

    def test_a_broad_entry_does_not_steal_the_specific_ones_finding(self) -> None:
        broad = ExpectedFinding(rule="AOA.SPELLING")
        specific = ExpectedFinding(rule="AOA.SPELLING", text="აგვისტო", correction="აგვისტო")
        found = [
            _finding("AOA.SPELLING", quote="აგვისტო", expected="აგვისტო"),
            _finding("AOA.SPELLING", quote="სხვა", expected="სხვა"),
        ]
        result = score_findings_strict([broad, specific], found, strict=True)
        assert result.ok and len(result.matched) == 2

    def test_compare_label_before_after(self) -> None:
        f = _finding(
            "CMP.BANK",
            expected="ZZBKGE22",
            actual="YYBKGE22",
            message_en="Bank code: ZZBKGE22 → YYBKGE22",
        )
        label = runner.compare_label(f)
        assert label == "Bank code"
        entry = ExpectedFinding(
            rule="CMP.BANK", label="Bank code", before="ZZBKGE22", after="YYBKGE22"
        )
        assert score_findings_strict([entry], [f], labels=[label], strict=True).ok
        swapped = ExpectedFinding(rule="CMP.BANK", before="YYBKGE22", after="ZZBKGE22")
        assert score_findings_strict([swapped], [f], labels=[label]).missing

    def test_compare_label_strips_change_kind_and_place(self) -> None:
        added = _finding("CMP.X", message_en="Penalty added: 1%")
        place = _finding("CMP.Y", message_en="text changed (body/para: 3)")
        assert runner.compare_label(added) == "Penalty"
        assert runner.compare_label(place) == "text changed"

    def test_occurrence_count_is_checked(self) -> None:
        entry = ExpectedFinding(rule="AOA.FEE", occurrences=2)
        f = _finding("AOA.FEE")
        assert score_findings_strict([entry], [f], count_occurrences=lambda e, x: 2).ok
        result = score_findings_strict([entry], [f], count_occurrences=lambda e, x: 1)
        assert result.occurrence_errors and not result.ok
        unknown = score_findings_strict([entry], [f], count_occurrences=lambda e, x: None)
        assert unknown.occurrence_errors


_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx_with_comment_ranges(path: Path, paragraphs: list[tuple[str, str | None]]) -> Path:
    """A .docx whose paragraphs are optionally wrapped in a comment range with the given id."""
    Document().save(str(path))
    body = "".join(
        (f'<w:p><w:commentRangeStart w:id="{cid}"/>' if cid else "<w:p>")
        + f"<w:r><w:t>{text}</w:t></w:r>"
        + (f'<w:commentRangeEnd w:id="{cid}"/></w:p>' if cid else "</w:p>")
        for text, cid in paragraphs
    )
    xml = f'<?xml version="1.0"?><w:document xmlns:w="{_W_NS}"><w:body>{body}</w:body></w:document>'
    rebuilt = path.with_suffix(".tmp")
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(rebuilt, "w") as dst:
        for item in src.infolist():
            data = xml.encode() if item.filename == "word/document.xml" else src.read(item)
            dst.writestr(item, data)
    rebuilt.replace(path)
    return path


class TestWordComments:
    def test_anchors_are_read_per_comment(self, tmp_path: Path) -> None:
        doc = _docx_with_comment_ranges(
            tmp_path / "a.docx", [("ერთი ab", "1"), ("ორი", None), ("სამი", "2")]
        )
        assert comment_anchors(doc) == {"1": "ერთი AB", "2": "სამი"}

    def test_count_marked_places(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        source = tmp_path / "act.docx"
        Document().save(str(source))

        def fake_annotate(
            src: Path, findings: list[Finding], out: Path
        ) -> tuple[Path, list[Finding]]:
            _docx_with_comment_ranges(out, [("ას ლარი", "1"), ("სხვა", "2"), ("ას ლარი", "3")])
            return out, []

        monkeypatch.setattr(score, "annotate_docx", fake_annotate)
        assert count_marked_places(source, [_finding("AOA.FEE")], "ას ლარი") == 2
        assert count_marked_places(source, [_finding("AOA.FEE")], None) == 3
        assert count_marked_places(tmp_path / "act.pdf", [], "x") is None


class TestGoldAndRunner:
    def _gold(self, tmp_path: Path, **extra: object) -> GoldDocument:
        doc = tmp_path / "act.docx"
        Document().save(str(doc))
        return GoldDocument.model_validate({"name": "g", "document": str(doc), **extra})

    def test_new_keys_load_and_entries_forbid_extras(self, tmp_path: Path) -> None:
        gold = self._gold(
            tmp_path,
            ignore=["penalty"],
            provisional=["subject"],
            findings=[{"rule": "AOA.FEE", "text": "ას", "occurrences": 1}],
            allowed_findings=["AOA.IBAN_CHECKSUM"],
            strict_findings=True,
        )
        assert gold.ignore == [FactType.penalty] and gold.findings[0].occurrences == 1
        with pytest.raises(ValidationError):
            self._gold(tmp_path, findings=[{"rule": "AOA.FEE", "wrong_key": 1}])

    def test_a_contract_without_check_or_reference_loads(self, tmp_path: Path) -> None:
        gold = self._gold(tmp_path, fields={"party_id_code": ["000000001"]})
        assert gold.reference is None and not gold.findings

    def _stub(self, monkeypatch: pytest.MonkeyPatch, report: CheckReport) -> list[int]:
        calls: list[int] = []
        monkeypatch.setattr(runner, "load_chunks_for", lambda *a, **k: [])
        monkeypatch.setattr(runner, "run_extraction", lambda **k: _result())

        def fake_check(**kwargs: object) -> CheckReport:
            calls.append(1)
            return report

        monkeypatch.setattr(runner, "check_aoa", fake_check)
        monkeypatch.setattr(runner, "chunks_text", lambda chunks: "")
        return calls

    def test_findings_without_reference_are_unscorable_and_no_check_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._stub(monkeypatch, CheckReport(document_id="g"))
        gold = self._gold(tmp_path, findings=[{"rule": "AOA.FEE"}])
        doc_score, _ = runner.run_document(
            gold,
            settings=Settings(),
            llm=None,
            config=runner.RunConfig(strict=True),
            cache_dir=tmp_path / "cache",
        )
        assert doc_score.strict_findings is not None and doc_score.strict_findings.unscorable
        assert calls == [] and doc_score.strict is not None

    def test_strict_findings_without_reference_or_entries_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A contract's gold may say strict_findings without naming a reference: no check."""
        calls = self._stub(monkeypatch, CheckReport(document_id="g"))
        gold = self._gold(tmp_path, strict_findings=True)
        doc_score, _ = runner.run_document(
            gold,
            settings=Settings(),
            llm=None,
            config=runner.RunConfig(strict=True),
            cache_dir=tmp_path / "cache",
        )
        assert doc_score.strict_findings is None and calls == []

    def test_one_check_run_feeds_legacy_and_strict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = CheckReport(
            document_id="g",
            findings=[
                _finding("AOA.FEE", quote="ას ლარი", expected="ორასი ლარი"),
                _finding("AOA.NOTE", severity="info"),
            ],
        )
        calls = self._stub(monkeypatch, report)
        contract = tmp_path / "contract.docx"
        Document().save(str(contract))
        gold = self._gold(
            tmp_path,
            reference=str(contract),
            expected_findings=["AOA.FEE"],
            findings=[{"rule": "AOA.FEE", "text": "ას ლარი", "correction": "ორასი ლარი"}],
            strict_findings=True,
        )
        doc_score, _ = runner.run_document(
            gold,
            settings=Settings(),
            llm=None,
            config=runner.RunConfig(strict=True),
            cache_dir=tmp_path / "cache",
        )
        assert calls == [1]
        assert doc_score.findings_ok  # legacy: errors only, AOA.FEE expected
        sf = doc_score.strict_findings
        assert sf is not None and len(sf.matched) == 1 and len(sf.unexpected) == 1

    def test_strict_is_opt_in(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub(monkeypatch, CheckReport(document_id="g"))
        gold = self._gold(tmp_path)
        doc_score, _ = runner.run_document(
            gold,
            settings=Settings(),
            llm=None,
            config=runner.RunConfig(),
            cache_dir=tmp_path / "cache",
        )
        assert doc_score.strict is None and doc_score.strict_findings is None


def test_a_compare_value_matches_as_a_value() -> None:
    from procurement_agent.eval.score import same_value

    assert same_value("2400", "2,400.00 GEL")
    assert same_value("ორი ათას ოთხასი", "ორი ათას ოთხასი (2400)")
    assert same_value("TBCBGE22", "TBCBGE22")
    assert not same_value("2400", "2,600.00 GEL")
    assert not same_value("2400 USD", "2,400.00 GEL")
    assert not same_value("ორი ათას ოთხასი", "ორი ათას ხუთასი (2500)")
    assert not same_value("2400", None)
    assert same_value("50%", "50%")
    assert not same_value("50%", "50.00 GEL")
    assert not same_value("50", "50%")
