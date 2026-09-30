"""Names already in the nominative stay as written; a correction takes the case of the slip it
corrects. Every name here is invented."""

from __future__ import annotations

import pytest

from procurement_agent.extraction.georgian import (
    case_of,
    in_case_of,
    lemma,
    same_phrase,
    word_lemmas,
)


@pytest.mark.parametrize("word", ["ბონუს", "კამპუს", "ტაუნჰაუს", "პრემიუმ", "ფორუმ", "ოუკვუდ"])
def test_a_loanword_in_u_plus_one_letter_is_a_nominative(word: str) -> None:
    # "-უს", "-უმ", "-უდ": Georgian has next to no vowel stems in "-უ"; these are loanwords
    assert case_of(word) == "nom"


@pytest.mark.parametrize(
    "name",
    ["მთის პრემიუმ", "ზღვის ფორუმ", "ოუკვუდ", "ტყის ბონუს", "მზის კამპუს", "ველის ტაუნჰაუს"],
)
def test_a_nominative_loanword_name_is_not_cut(name: str) -> None:
    assert lemma(name) == name


def test_the_one_letter_cases_of_other_vowel_stems_still_read() -> None:
    assert case_of("მხარემ") == "erg"
    assert case_of("უზრუნველყოფად") == "adv"
    assert lemma("ეკა ლომაიამ") == "ეკა ლომაია"
    assert lemma("ეკა ლომაიას") == "ეკა ლომაია"


def test_a_vowel_is_never_followed_by_the_consonant_stems_i() -> None:
    # "პანორა" + "ი" would be a consonant-stem nominative after a vowel, which Georgian lacks
    assert lemma("მთის პანორამა") != "მთის პანორაი"
    assert "პანორაი" not in word_lemmas("პანორამა")


@pytest.mark.parametrize(
    ("written", "nominative"),
    [
        ("მთის პანორამა", "მთის პანორამა"),  # "-მა" is no ergative here
        ("ლურჯი სიგმა", "ლურჯი სიგმა"),
        ("ზღვის ექსპრეს", "ზღვის ექსპრეს"),  # "-ეს" is no dative here
        ("მზის ქალაქის", "მზის ქალაქი"),  # "-ის" after a consonant is the genitive
        ("მზის ქალაქისთვის", "მზის ქალაქი"),  # a postposition is always one
    ],
)
def test_a_name_in_apposition_loses_only_the_endings_apposition_allows(
    written: str, nominative: str
) -> None:
    assert lemma(written, cases=("gen", "gen_tvis", "dat_shi")) == nominative


def test_a_genitive_attribute_is_read_as_a_genitive_only() -> None:
    # "„X“ პროექტი": X stands in the genitive, so "-ეს" (a dative) is part of the name
    assert lemma("ზღვის ექსპრეს", cases=("gen",)) == "ზღვის ექსპრეს"
    assert lemma("ეკა ლომიას", cases=("gen",)) == "ეკა ლომია"
    assert lemma("მზის ქალაქის", cases=("gen",)) == "მზის ქალაქი"


def test_a_slip_on_the_dze_ending_is_corrected_in_the_nominative() -> None:
    # "ძ" and "ზ" share a key: "ხარაზე" is the nominative with Shift missed, not "-ზე"
    assert in_case_of("ლევან ხარაზე", "ლევან ხარაძე") == "ლევან ხარაძე"
    assert in_case_of("ლევან ხარაზის", "ლევან ხარაძე") == "ლევან ხარაძის"


def test_a_correction_keeps_the_case_its_ending_states_when_no_form_is_closer() -> None:
    assert in_case_of("ნინო კაპანიას", "ნინო კაპანაძე") == "ნინო კაპანაძის"
    assert in_case_of("ზღვის ხედისთვის", "მზის ქალაქის") == "მზის ქალაქისთვის"
    assert in_case_of("ლევან ბერიშვილზე", "ლევან ხარაძე") == "ლევან ხარაძეზე"


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("მთის ვერანდისთვის", "მთის ვერანდა", True),  # "-ა" noun: ვერანდა -> ვერანდისთვის
        ("ძველი ბაზისთვის", "ძველი ბაზა", True),
        ("მზის ქალაქისთვის", "მზის ქალაქი", True),
        ("მთის ვერანდისთვის", "ზღვის ვერანდა", False),  # another first word
        ("მთის ვერანდისთვის", "მთის ბაღი", False),
    ],
)
def test_same_phrase_reads_every_nominative_a_form_can_have(a: str, b: str, same: bool) -> None:
    assert same_phrase(a, b) is same
