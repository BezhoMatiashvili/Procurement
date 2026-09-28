"""Scanned documents: codes put back in shape, and misreadings told apart from mistakes.
Synthetic values only."""

from __future__ import annotations

from procurement_agent.extraction.ocr import misread_explains, repair_codes


def test_a_scanned_iban_is_put_back_in_shape() -> None:
    assert repair_codes("ა/ა: CL29NB0000000101904917.") == "ა/ა: GE29NB0000000101904917."
    # a bank code read with a look-alike letter, and a digit read as a letter among the digits
    assert repair_codes("GE18BC00000006777777O7") == "GE18BG0000000677777707"


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
