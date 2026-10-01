"""Scanned documents: codes put back in shape, and misreadings told apart from mistakes.
Synthetic values only."""

from __future__ import annotations

from procurement_agent.extraction.ocr import misread_explains, repair_codes


def test_a_scanned_iban_is_put_back_in_shape() -> None:
    assert repair_codes("ა/ა: CL29NB0000000101904917.") == "ა/ა: GE29NB0000000101904917."
    # a bank code read with a look-alike letter, and a digit read as a letter among the digits
    assert repair_codes("GE29N800000001019049O7") == "GE29NB0000000101904907"


def test_what_cannot_be_an_iban_is_left_alone() -> None:
    for text in ("ABCDEFGHIJKLMNOPQRSTUV", "GE29NB0000000101904917", "XX29NB0000000101904917"):
        assert repair_codes(text) == text


def test_a_scanned_bank_code_after_its_label_is_put_back() -> None:
    assert repair_codes("ბანკის კოდი: AAAA6E22;") == "ბანკის კოდი: AAAAGE22;"
    assert repair_codes("SWIFT: TBCB6E22") == "SWIFT: TBCBGE22"
    # no label, no repair: eight letters and digits are not a bank code by themselves
    assert repair_codes("AAAA6E22") == "AAAA6E22"


def test_repair_keeps_the_text_length() -> None:
    text = "ა/ა: CL29NB0000000101904917; ბანკის კოდი: AAAA6E22"
    assert len(repair_codes(text)) == len(text)


def test_letter_slips_are_a_misreading() -> None:
    assert misread_explains("ნინო კაპანაშე", "ნინო კაპანაძე")
    assert misread_explains("რუსთაველის NI2", "რუსთაველის N12")
    assert misread_explains("ზღვის ხედისთვისს", "ზღვის ხედისთვის")
    assert misread_explains("ქ. თბილისი, რუსთაველის 12", "ქ თბილისი რუსთაველის 12")


def test_a_real_change_is_no_misreading() -> None:
    assert not misread_explains("ნინო კაპანია", "ნინო კაპანაძე")
    assert not misread_explains("დავით მელაძე", "ნინო კაპანაძე")
    assert not misread_explains("შპს ბეტა", "შპს ალფა ჯგუფი")


def test_digits_swapped_or_unlike_are_no_misreading() -> None:
    assert not misread_explains("GE29NB0000000101904971", "GE29NB0000000101904917")
    assert not misread_explains("123456781", "123456789")
    # "8" and "3" do look alike on a page
    assert misread_explains("123456739", "123456789")


# --- the act check on a scan -------------------------------------------------------------------

from procurement_agent.checks.aoa import check_aoa  # noqa: E402
from tests.unit.test_aoa_check import ACT, CONTRACT  # noqa: E402


def _findings(act: str, contract: str = CONTRACT, **kw: bool) -> list[tuple[str, str]]:
    report = check_aoa(aoa_text=act, contract_text=contract, **kw)  # type: ignore[arg-type]
    return [(f.rule_id, f.severity) for f in report.findings if f.severity != "info"]


def test_on_a_scan_a_letter_slip_is_a_misreading_to_check() -> None:
    act = ACT.replace("ნინო კაპანაძე", "ნინო კაპანაშე")
    assert _findings(act) == [("AOA.DIRECTOR", "error")]
    report = check_aoa(aoa_text=act, contract_text=CONTRACT, aoa_ocr=True)
    [finding] = [f for f in report.findings if f.rule_id == "AOA.DIRECTOR"]
    assert finding.severity == "warning" and finding.needs_review
    assert "paper original" in finding.message_en
    assert finding.expected == "ნინო კაპანაძე"


def test_on_a_scan_a_real_change_is_still_an_error() -> None:
    act = ACT.replace("ნინო კაპანაძე", "ნინო ბერიძე")
    assert ("AOA.DIRECTOR", "error") in _findings(act, aoa_ocr=True)


def test_a_misread_inflected_name_is_a_misreading() -> None:
    """A doubled letter at the end of a project written with a postposition."""
    contract = CONTRACT.replace("„მზე“ პროექტისთვის", "„ზღვის რეზიდენსი“ პროექტისთვის")
    act = ACT.replace("პროექტ „მზე“", "პროექტ „ზღვის რეზიდენსისთვისს“")
    assert ("AOA.PROJECT_NAME", "warning") in _findings(act, contract, aoa_ocr=True)


def test_a_scanned_contracts_iban_is_read_back() -> None:
    contract = CONTRACT.replace("GE29NB0000000101904917", "CL29NB0000000101904917")
    assert _findings(ACT, contract) != []  # read as written, the account differs
    assert _findings(ACT, contract, contract_ocr=True) == []


def test_a_bracket_closed_by_a_misread_colon_is_still_the_amount() -> None:
    act = ACT.replace("100 (ასი) ლარს", "100 (ასი: ლარს")
    assert _findings(act, aoa_ocr=True) == []


def test_a_dot_after_the_day_does_not_hide_the_date() -> None:
    act = ACT.replace("2026 წლის 30 აპრილის ჩათვლით", "2026 წლის 30. აპრილის ჩათვლით")
    report = check_aoa(aoa_text=act, contract_text=CONTRACT, aoa_ocr=True)
    assert report.summary["execution_period"] == "2026-04-01..2026-04-30"


def test_a_period_whose_end_was_not_read_does_not_date_the_act() -> None:
    """Only "from 1 April" read, its end lost to the scan: no period to date the act by."""
    act = ACT.replace("2026 წლის 30 აპრილის ჩათვლით", "2026 26 წლის 30 აპრილის ჩათვლით")
    report = check_aoa(aoa_text=act, contract_text=CONTRACT, aoa_ocr=True)
    assert "AOA.ACT_DATE" not in [f.rule_id for f in report.findings]
    assert "AOA.ACT_DATE" in [s.rule_id for s in report.skipped]


def test_without_the_contracts_term_a_year_apart_is_a_question_not_an_answer() -> None:
    """The act's date and its period a year apart, the contract's term unreadable: which one is
    wrong cannot be told, so neither is corrected."""
    contract = CONTRACT.replace(
        "2.1. შეასრულოს მომსახურება 2026 წლის 1 მარტიდან - 2026 წლის 1 სექტემბრის ჩათვლით.\n", ""
    )
    act = ACT.replace("2026 წლის 1 აპრილიდან", "2025 წლის 1 აპრილიდან").replace(
        "2026 წლის 30 აპრილის", "2025 წლის 30 აპრილის"
    )
    report = check_aoa(aoa_text=act, contract_text=contract)
    [finding] = [f for f in report.findings if f.rule_id == "AOA.ACT_DATE"]
    assert finding.severity == "warning" and finding.expected is None
    assert "year" in finding.message_en


def test_a_one_read_as_a_bar_before_a_month_is_put_back() -> None:
    assert repair_codes("2026 წლის | აგვისტოდან") == "2026 წლის 1 აგვისტოდან"
    assert repair_codes("მუხლი | და") == "მუხლი | და"
