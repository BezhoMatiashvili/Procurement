"""Defects the fourth red-team round found in the act check's signatory, date, fee and period
rules.

Each test states the defect's id (``samples/redteam/aoa/CONFIRMED_R4.md``) and the behaviour the
spec asks for, and every widened reading has a near miss beside it that must still not match.
Synthetic text only: the pair is ``test_aoa_check``'s, with every name, code and account invented.
"""

from __future__ import annotations

from pathlib import Path

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.checks.findings import CheckReport, Finding
from tests.unit.test_aoa_check import ACT, CONTRACT


def _check(act: str = ACT, contract: str | None = CONTRACT, **kw: object) -> CheckReport:
    return check_aoa(aoa_text=act, contract_text=contract, **kw)  # type: ignore[arg-type]


def _of(report: CheckReport, rule_id: str) -> list[Finding]:
    return [f for f in report.findings if f.rule_id == rule_id]


def _substantive(report: CheckReport) -> list[Finding]:
    return [f for f in report.findings if f.severity in ("error", "warning")]


def _annotated(tmp_path: Path, act: str, findings: list[Finding], *, text: bool) -> list[str]:
    """The words each comment covers once the act (one paragraph per line) is annotated."""
    from docx import Document

    from procurement_agent.eval.score import comment_anchors
    from procurement_agent.report.docx_annotate import annotate_docx

    doc = Document()
    for line in act.splitlines():
        doc.add_paragraph(line)
    src = tmp_path / "act.docx"
    doc.save(src)
    out = tmp_path / "annotated.docx"
    annotate_docx(src, findings, out, text=act if text else None)
    return sorted(comment_anchors(out).values())


def _commented_lines(tmp_path: Path, act: str, findings: list[Finding]) -> list[str]:
    """The act's lines that carry a comment once it is annotated."""
    from docx import Document

    from procurement_agent.report.docx_annotate import annotate_docx

    doc = Document()
    for line in act.splitlines():
        doc.add_paragraph(line)
    src = tmp_path / "lines.docx"
    doc.save(src)
    out = tmp_path / "lines_annotated.docx"
    annotate_docx(src, findings, out)
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    marked: list[str] = []
    for p in Document(str(out)).paragraphs:
        if p._p.findall(f".//{w}commentRangeStart"):
            marked.append(p.text)
    return marked


# --- AOA-R4-01: a name copied into one party's signature alone -----------------------------------

#: The act with the contractor's director named in its preamble too.
_TWO_DIRECTORS = ACT.replace(
    "ჭავჭავაძის 5 (შემდგომში „შემსრულებელი“)",
    "ჭავჭავაძის 5 (შემდგომში „შემსრულებელი“) წარმოდგენილი დირექტორის დავით მელაძის სახით",
)
#: The customer's director written at the contractor's signature only; the preamble is right.
_SIGNATURE_ONLY = _TWO_DIRECTORS.replace("დირექტორი: დავით მელაძე", "დირექტორი: ნინო კაპანაძე")


class TestCopiedIntoTheSignatureOnly:
    def test_one_finding_on_the_contractor_s_signature(self) -> None:
        report = _check(_SIGNATURE_ONLY)
        found = [(f.quote, f.expected) for f in _of(report, "AOA.DIRECTOR")]
        assert found == [("ნინო კაპანაძე", "დავით მელაძე")]
        [f] = _of(report, "AOA.DIRECTOR")
        assert f.span is not None
        assert f.span[0] > _SIGNATURE_ONLY.index("„შემსრულებელი“\nშპს")

    def test_the_customer_s_own_preamble_is_not_marked(self, tmp_path: Path) -> None:
        findings = _of(_check(_SIGNATURE_ONLY), "AOA.DIRECTOR")
        for text in (False, True):
            assert _annotated(tmp_path, _SIGNATURE_ONLY, findings, text=text) == [
                "ნინო კაპანაძე"
            ], text
        assert _commented_lines(tmp_path, _SIGNATURE_ONLY, findings) == ["დირექტორი: ნინო კაპანაძე"]

    def test_a_copy_in_the_contractor_s_preamble_alone_is_still_reported(self) -> None:
        """Near miss: the genitive copied into the contractor's preamble stands in the
        contractor's block, and is that party's mistake."""
        act = _TWO_DIRECTORS.replace(
            "„შემსრულებელი“) წარმოდგენილი დირექტორის დავით მელაძის",
            "„შემსრულებელი“) წარმოდგენილი დირექტორის ნინო კაპანაძის",
        )
        found = [(f.quote, f.expected) for f in _of(_check(act), "AOA.DIRECTOR")]
        assert found == [("ნინო კაპანაძის", "დავით მელაძის")]

    def test_a_name_nobody_signs_is_still_reported_everywhere(self) -> None:
        """Near miss: an unrelated person at the signature is in no other party's block."""
        act = _TWO_DIRECTORS.replace("დირექტორი: დავით მელაძე", "დირექტორი: ზურაბ ქობალია")
        found = [(f.quote, f.places) for f in _of(_check(act), "AOA.DIRECTOR")]
        assert found == [("ზურაბ ქობალია", None)]


# --- AOA-R4-06: the act's date is marked where the act is dated, not on a correct copy -----------

_SIGNED = "აქტი გაფორმდა 2026 წლის 30 აპრილი"
_PERIOD = "მომსახურება შესრულდა 2026 წლის 1 აპრილიდან 2026 წლის 30 აპრილის ჩათვლით."
#: The act recites the contract's date, as acts do.
_RECITAL = ACT.replace(
    "„შემკვეთის“ პროექტ",
    "ვინაიდან მხარეებმა 2026 წლის 1 მარტს გააფორმეს ხელშეკრულება,\n„შემკვეთის“ პროექტ",
)


class TestActDateMarkedOnlyWhereTheActIsDated:
    def test_the_recital_s_contract_date_is_not_marked(self, tmp_path: Path) -> None:
        act = _RECITAL.replace(_SIGNED, "აქტი გაფორმდა 2026 წლის 1 მარტს")
        report = _check(act)
        [f] = _of(report, "AOA.ACT_DATE")
        assert (f.quote, f.expected) == ("2026 წლის 1 მარტს", "2026 წლის 30 აპრილს")
        assert f.places == ((1,), 2)
        assert _commented_lines(tmp_path, act, [f]) == ["აქტი გაფორმდა 2026 წლის 1 მარტს"]

    def test_the_period_s_end_is_not_marked(self, tmp_path: Path) -> None:
        act = ACT.replace(
            _PERIOD, "მომსახურება შესრულდა 01.04.2026-დან 29.04.2026-ის ჩათვლით."
        ).replace(_SIGNED, "აქტი გაფორმდა 29.04.2026")
        [f] = _of(_check(act), "AOA.ACT_DATE")
        assert (f.quote, f.expected) == ("29.04.2026", "30.04.2026")
        assert _commented_lines(tmp_path, act, [f]) == ["აქტი გაფორმდა 29.04.2026"]

    def test_the_act_dated_twice_is_marked_twice(self, tmp_path: Path) -> None:
        """Near miss: an act that writes its own date in its heading and again where it is
        signed is wrong at both places."""
        act = ACT.replace("მიღება-ჩაბარების აქტი\n", "მიღება-ჩაბარების აქტი\n05.05.2026\n").replace(
            _SIGNED, "აქტი გაფორმდა 05.05.2026"
        )
        [f] = _of(_check(act), "AOA.ACT_DATE")
        assert f.places is None
        assert _commented_lines(tmp_path, act, [f]) == ["05.05.2026", "აქტი გაფორმდა 05.05.2026"]

    def test_a_date_written_once_is_marked_there(self) -> None:
        [f] = _of(_check(ACT.replace(_SIGNED, "აქტი გაფორმდა 2026 წლის 5 მაისი")), "AOA.ACT_DATE")
        assert f.places is None

    def test_the_act_dated_before_the_contract_marks_the_act_s_date_only(
        self, tmp_path: Path
    ) -> None:
        """AOA.ACT_DATE_IN_TERM reads the same date, and marks it the same way."""
        act = _RECITAL.replace("2026 წლის 1 მარტს გააფორმეს", "2026 წლის 20 თებერვალს გააფორმეს")
        act = act.replace(_SIGNED, "აქტი გაფორმდა 2026 წლის 20 თებერვალს")
        found = _of(_check(act), "AOA.ACT_DATE_IN_TERM")
        assert found
        lines = _commented_lines(tmp_path, act, found)
        assert lines == ["აქტი გაფორმდა 2026 წლის 20 თებერვალს"]

    def test_places_are_counted_as_annotation_counts_them(self) -> None:
        """The shared reader: a date inside a longer word is no place of it (near miss)."""
        from procurement_agent.report.docx_annotate import whole_word_spans

        text = "30.04.2026-ის ჩათვლით; 130.04.20261; აქტი 30.04.2026"
        assert whole_word_spans(text, "30.04.2026") == [(0, 10), (42, 52)]
        assert whole_word_spans("ივნისიდან და ივნის", "ივნის") == [(13, 18)]
        assert whole_word_spans("ტექსტი", " ") == []


# --- AOA-R4-08: work to date, the contract's balance and what was paid before are no fee ---------

_FEE_LINE = "საფასური შეადგენს 100 (ასი) ლარს გადასახადების ჩათვლით."


def _with_figure(sentence: str) -> str:
    assert _FEE_LINE in ACT
    return ACT.replace(_FEE_LINE, f"{_FEE_LINE} {sentence}")


def _roles(text: str) -> list[tuple[float, str | None]]:
    from procurement_agent.checks.contract_terms import amount_roles

    return [(r.money.value, r.role) for r in amount_roles(text)]


class TestRunningFiguresAreNoFee:
    _RUNNING = (
        "ხელშეკრულების ფარგლებში დღემდე შესრულებული მომსახურების ჯამური ღირებულება შეადგენს "
        "300 (სამასი) ლარს.",
        "ხელშეკრულების დარჩენილი ღირებულება შეადგენს 500 (ხუთასი) ლარს.",
        "წინა აქტებით სულ ანაზღაურებულია 200 (ორასი) ლარი.",
        "ხელშეკრულების ნაშთი შეადგენს 700 ლარს.",
        "უკვე გადახდილი თანხა შეადგენს 200 ლარს.",
        "სულ ანაზღაურებულია 200 (ორასი) ლარი.",
    )

    def test_each_leaves_the_act_clean_with_a_note(self) -> None:
        for sentence in self._RUNNING:
            report = _check(_with_figure(sentence))
            assert _substantive(report) == [], sentence
            assert any(s.rule_id == "AOA.FEE" for s in report.skipped), sentence

    def test_the_figure_reads_as_running(self) -> None:
        for sentence in self._RUNNING:
            assert _roles(sentence)[-1][1] == "running", sentence

    def test_the_fee_itself_is_still_compared(self) -> None:
        act = _with_figure(self._RUNNING[1]).replace("100 (ასი) ლარს გადასახადების", "110 ლარს გ")
        assert [f.quote for f in _of(_check(act), "AOA.FEE")] == ["110 ლარს"]

    def test_the_fee_after_a_running_figure_in_one_sentence_is_the_fee(self) -> None:
        """Near miss: the words before the figure before it do not reach the fee."""
        text = "დღემდე შესრულებულის ღირებულება 300 ლარი, ხოლო ამ აქტით - 100 ლარი."
        assert _roles(text) == [(300.0, "running"), (100.0, "price")]

    def test_total_words_and_the_fee_s_remainder_keep_their_role(self) -> None:
        """Near misses: a total word alone, the contract's total, the remainder of the fee after
        an advance, an advance said to be paid, and a period that ends today."""
        assert _roles("სულ: 150 ლარი.")[-1][1] in (None, "price")
        assert _roles("სულ ასანაზღაურებელია: 150 ლარი.")[-1][1] in (None, "price")
        assert _roles("ხელშეკრულების ჯამური ღირებულება შეადგენს 150 ლარს.") == [(150.0, "price")]
        assert _roles("ავანსის შემდეგ დარჩენილი თანხა 70 ლარი.")[-1][1] == "remainder"
        assert _roles("ღირებულების დარჩენილი ნაწილი 70 ლარი გადაიხდება.")[-1][1] == "remainder"
        assert _roles("ავანსი უკვე გადახდილია 30 ლარი.") == [(30.0, "advance")]
        assert _roles("01.06.2026-დან დღემდე შესრულებული მომსახურების საფასური 100 ლარი.")[-1][
            1
        ] in (None, "price")

    def test_a_total_word_alone_is_still_a_wrong_fee(self) -> None:
        act = _with_figure("სულ: 150 ლარი.")
        assert [f.quote for f in _of(_check(act), "AOA.FEE")] == ["150 ლარი"]


# --- AOA-R4-09: the term written in the act's style when the period writes its year once ---------


class TestTermCorrectionWithTheYearWrittenOnce:
    """The contract's term is 2026-03-01..2026-09-01; each period starts before it."""

    def _expected(self, period: str) -> str | None:
        act = ACT.replace(_PERIOD, f"მომსახურება შესრულდა {period}.")
        [f] = _of(_check(act), "AOA.PERIOD_IN_TERM")
        assert f.quote == period
        return f.expected

    def test_figures(self) -> None:
        assert self._expected("25.02-30.04.2026") == "01.03.2026-01.09.2026"

    def test_month_names(self) -> None:
        assert (
            self._expected("25 თებერვალი - 30 აპრილი, 2026 წ.")
            == "1 მარტი, 2026 წ. - 1 სექტემბერი, 2026 წ."
        )

    def test_the_start_keeps_its_postposition(self) -> None:
        assert (
            self._expected("25 თებერვლიდან 30 აპრილის ჩათვლით, 2026 წ.")
            == "1 მარტიდან, 2026 წ. 1 სექტემბრის ჩათვლით, 2026 წ."
        )

    def test_iso_stays_iso_and_full_dates_keep_their_style(self) -> None:
        """Near misses: an act that writes ISO is corrected in ISO, and both ends with a year
        keep their own style, as before."""
        assert self._expected("2026-02-25 - 2026-04-30") == "2026-03-01 - 2026-09-01"
        assert self._expected("25.02.2026-30.04.2026") == "01.03.2026-01.09.2026"

    def test_the_year_is_borrowed_as_the_other_end_writes_it(self) -> None:
        from procurement_agent.checks.aoa import _with_year_of

        assert _with_year_of("25.05", "30.06.2026") == "25.05.2026"
        assert _with_year_of("25 მაისიდან", "30 ივნისის ჩათვლით, 2026 წ.") == (
            "25 მაისიდან, 2026 წ."
        )
        assert _with_year_of("25 მაისიდან", "2026 წლის 30 ივნისის") == "2026 წლის 25 მაისიდან"
        # near misses: an end with its own year, and another end with none to lend
        assert _with_year_of("25.05.2025", "30.06.2026") is None
        assert _with_year_of("25.05", "30.06") is None


# --- AOA-R4-11: a fee in another currency is corrected in the act's own wording ------------------

_DOLLARS = CONTRACT.replace("100 (ასი) ლარს", "100 (ასი) აშშ დოლარს")


class TestCurrencyCorrectionInTheActsWording:
    def test_lari_against_a_dollar_price(self) -> None:
        [f] = _of(_check(contract=_DOLLARS), "AOA.FEE")
        assert (f.quote, f.expected) == ("100 (ასი) ლარს", "100 (ასი) აშშ დოლარს")
        assert "100.00 USD" in f.message_en  # the message still names the contract's price

    def test_the_words_after_the_currency_and_a_code(self) -> None:
        act = _fee_sentence("საფასური შეადგენს 100 ლარს (ასი) გადასახადების ჩათვლით.")
        [f] = _of(_check(act, _DOLLARS), "AOA.FEE")
        assert f.expected == "100 აშშ დოლარს (ასი)"
        act = _fee_sentence("საფასური შეადგენს GEL 100 (ასი) გადასახადების ჩათვლით.")
        [f] = _of(_check(act, _DOLLARS), "AOA.FEE")
        assert f.expected == "USD 100 (ასი)"

    def test_each_form_is_rewritten_in_its_own_case(self) -> None:
        from procurement_agent.checks.amounts import with_currency

        assert with_currency("100 (ასი) აშშ დოლარს", "GEL") == "100 (ასი) ლარს"
        assert with_currency("100 ევროთი", "GEL") == "100 ლარით"
        assert with_currency("100 ლარის", "EUR") == "100 ევროს"
        assert with_currency("100 ₾ (ასი)", "USD") == "100 $ (ასი)"
        assert with_currency("100 (ასი ლარი და 50 თეთრი)", "USD") == (
            "100 (ასი აშშ დოლარი და 50 ცენტი)"
        )

    def test_what_cannot_be_rewritten_is_left_to_the_machine_form(self) -> None:
        """Near misses: a word that only starts like a currency, no currency at all, and a
        currency with no Georgian form here."""
        from procurement_agent.checks.amounts import with_currency

        assert with_currency("100 ევროპული", "GEL") is None
        assert with_currency("100", "GEL") is None
        assert with_currency("100 ლარს", "GBP") is None


def _fee_sentence(sentence: str) -> str:
    assert _FEE_LINE in ACT
    return ACT.replace(_FEE_LINE, sentence)


# --- AOA-R4-12: an attorney at the act's signature is compared, or a note says it was not --------

_BY_PROXY = "წარმოდგენილი მინდობილობით მოქმედი პირის, თამარ გოგიჩაიშვილის სახით"
_PROXY_CONTRACT = CONTRACT.replace("წარმოდგენილი დირექტორის ნინო კაპანაძის სახით", _BY_PROXY)
_PROXY_ACT = ACT.replace("წარმოდგენილი დირექტორის ნინო კაპანაძის სახით", _BY_PROXY)


def _signed_by(name: str, act: str = _PROXY_ACT) -> str:
    return act.replace("დირექტორი ნინო კაპანაძე", f"მინდობილი პირი: {name}")


def _run_with_signing_proxy(act: str, name: str) -> CheckReport:
    """The check with ``name`` read as the customer's attorney from the signature block, as the
    party reader reads it once it knows the signature's attorney title."""
    from procurement_agent.checks.aoa import AoaChecker, DocumentView

    view = DocumentView.build("aoa", act)
    [customer] = [q for q in view.parties if q.name and "ალფა" in q.name]
    customer.proxies.append(name)
    return AoaChecker(aoa=view, contract=DocumentView.build("contract", _PROXY_CONTRACT)).run()


class TestAttorneyAtTheSignature:
    def test_the_pair_signed_by_the_contract_s_attorney_is_clean(self) -> None:
        report = _check(_signed_by("თამარ გოგიჩაიშვილი"), _PROXY_CONTRACT)
        assert _substantive(report) == []
        assert not [s for s in report.skipped if s.rule_id == "AOA.DIRECTOR"]

    def test_an_attorney_not_read_leaves_a_note(self) -> None:
        for name in ("თამარ გოგიჩაიშვილო", "ნათია გოგიჩაიშვილი"):
            report = _check(_signed_by(name), _PROXY_CONTRACT)
            notes = [s for s in report.skipped if s.rule_id == "AOA.DIRECTOR"]
            assert any(name in s.reason_en for s in notes), name

    def test_a_slip_of_the_contract_s_attorney_is_an_error_with_its_correction(self) -> None:
        act = _signed_by("თამარ გოგიჩაიშვილო")
        report = _run_with_signing_proxy(act, "თამარ გოგიჩაიშვილო")
        found = [(f.severity, f.quote, f.expected) for f in _of(report, "AOA.DIRECTOR")]
        assert found == [("error", "თამარ გოგიჩაიშვილო", "თამარ გოგიჩაიშვილი")]

    def test_another_person_asks_for_the_power_of_attorney(self) -> None:
        act = _signed_by("ნათია გოგიჩაიშვილი")
        report = _run_with_signing_proxy(act, "ნათია გოგიჩაიშვილი")
        found = [(f.severity, f.quote, f.expected) for f in _of(report, "AOA.DIRECTOR")]
        assert found == [("warning", "ნათია გოგიჩაიშვილი", None)]

    def test_an_attorney_the_contract_does_not_name_is_still_a_warning(self) -> None:
        """Near miss: with only the director in the contract, an attorney in the act is not
        provably wrong - a warning asking for the power of attorney, never a correction."""
        act = _PROXY_ACT.replace("თამარ გოგიჩაიშვილის", "თამარ გოგიჩაიშვილოს")
        found = [(f.severity, f.expected) for f in _of(_check(act), "AOA.DIRECTOR")]
        assert found and all(f == ("warning", None) for f in found)

    def test_a_slip_in_the_preamble_is_corrected_too(self) -> None:
        act = _PROXY_ACT.replace("თამარ გოგიჩაიშვილის", "თამარ გოგიჩაიშვილოს")
        report = _check(act, _PROXY_CONTRACT)
        found = [(f.severity, f.quote, f.expected) for f in _of(report, "AOA.DIRECTOR")]
        assert found == [("error", "თამარ გოგიჩაიშვილოს", "თამარ გოგიჩაიშვილის")]

    def test_the_word_in_prose_is_no_signature(self) -> None:
        """Near misses: a power of attorney named in a sentence, and the title followed by more
        than a name."""
        act = _PROXY_ACT.replace(
            "4. გაფორმების თარიღი",
            "მინდობილობის საფუძველზე მოქმედებს მხარე.\n"
            "მინდობილი პირი ვალდებულია წარადგინოს მინდობილობა ხელმოწერისას.\n"
            "4. გაფორმების თარიღი",
        )
        report = _check(_signed_by("თამარ გოგიჩაიშვილი", act), _PROXY_CONTRACT)
        assert not [s for s in report.skipped if s.rule_id == "AOA.DIRECTOR"]
