"""Normalisation primitives. All fixtures here are synthetic."""

from __future__ import annotations

import pytest

from procurement_agent.extraction.normalize import (
    amounts_equal,
    close_enough,
    edit_distance,
    iban_is_valid,
    names_match,
    normalize_confusables,
    normalize_name,
    parse_georgian_number_words,
    parse_number,
    strip_quotes,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1,250.50", 1250.5),
        ("1.250,50", 1250.5),
        ("12,500", 12500.0),
        ("48 750,00", 48750.0),
        ("100", 100.0),
        ("1 250", 1250.0),
        ("1 250", 1250.0),  # narrow no-break space, produced by Word
        ("-42", -42.0),
        ("abc", None),
        ("", None),
    ],
)
def test_parse_number(text: str, expected: float | None) -> None:
    assert parse_number(text) == expected


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        ("ერთი", 1),
        ("ხუთი", 5),
        ("ათი", 10),
        ("თხუთმეტი", 15),
        ("ოცდაერთი", 21),
        ("ოცდაათი", 30),
        ("ორმოცდაათი", 50),
        ("ოთხმოცდაცხრამეტი", 99),
        ("ასი", 100),
        ("ორასი", 200),
        ("შვიდასი", 700),
        ("ცხრაასი", 900),
        ("ას ორმოცდარვა", 148),
        ("ათასი", 1000),
        ("ორი ათას ხუთასი", 2500),
        ("ორმოცდარვა ათას შვიდას ორმოცდაათი", 48750),
        ("მილიონი", 1_000_000),
    ],
)
def test_parse_georgian_number_words(words: str, expected: int) -> None:
    assert parse_georgian_number_words(words) == expected


@pytest.mark.parametrize("text", ["ლარს", "სამუშაო", "გიორგი", "", "ასოი", "დღის"])
def test_parse_georgian_number_words_rejects_non_numbers(text: str) -> None:
    """A partial parse must fail, not return a plausible number: 'ასოი' is a typo of 'ასი',
    and silently reading it as 100 would hide the very error we are looking for."""
    assert parse_georgian_number_words(text) is None


@pytest.mark.parametrize(
    ("iban", "valid"),
    [
        ("GB29NWBK60161331926819", True),
        ("DE89370400440532013000", True),
        ("GE29NB0000000101904917", True),
        ("FR1420041010050500013M02606", True),
        ("GE29NB0000000101904918", False),  # last digit changed
        ("GE00XX0000000000000001", False),  # anonymised placeholder
        ("nonsense", False),
        ("", False),
    ],
)
def test_iban_is_valid(iban: str, valid: bool) -> None:
    assert iban_is_valid(iban) is valid


def test_iban_tolerates_cyrillic_homoglyphs_from_ocr() -> None:
    """Tesseract emits Cyrillic lookalikes inside otherwise-Latin tokens; folding them is what
    keeps an OCR artefact from reading as a different account number."""
    latin = "GB29NWBK60161331926819"
    cyrillic_b = latin.replace("B", "\u0412", 1)  # CYRILLIC CAPITAL LETTER VE
    assert cyrillic_b != latin
    assert iban_is_valid(cyrillic_b)


def test_amounts_equal_has_tolerance() -> None:
    assert amounts_equal(100.0, 100.004)
    assert not amounts_equal(100.0, 100.5)
    assert not amounts_equal(None, 100.0)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("ნინო კაპანაძე", "ნინო კაპანაძის"),
        ("შპს „ნიმუში“", "შპს ნიმუში"),
        ("დავით მელაძე", "დავით მელაძის"),
    ],
)
def test_names_match_ignores_inflection(a: str, b: str) -> None:
    """Georgian marks the genitive on the name itself; that is not a difference."""
    assert names_match(a, b)


@pytest.mark.parametrize(
    ("a", "b"),
    [("დავით მელაძე", "გიორგი მელაია"), ("შპს ნიმუში", "შპს სხვა ნიმუში")],
)
def test_names_match_rejects_different_names(a: str, b: str) -> None:
    assert not names_match(a, b)


def test_strip_quotes_handles_every_quote_style() -> None:
    assert strip_quotes("შპს „ნიმუში“") == strip_quotes("შპს ”ნიმუში”") == "შპს ნიმუში"


def test_normalize_confusables_folds_cyrillic_and_spaces() -> None:
    assert normalize_confusables("AВC D") == "ABC D"


def test_normalize_name_is_case_and_punctuation_insensitive() -> None:
    assert normalize_name("ШПС, Test.") == normalize_name("шпс test")


def test_edit_distance_gives_up_past_the_cap() -> None:
    assert edit_distance("abc", "abd") == 1
    assert edit_distance("abc", "xyzw", cap=1) == 2


def test_close_enough_is_stricter_for_short_words() -> None:
    assert close_enough("აგვისტო", "აგისტო")
    assert not close_enough("ორი", "სამი")
