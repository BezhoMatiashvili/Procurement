"""The Georgian morphology the checks rely on. Every name here is invented."""

from __future__ import annotations

from datetime import date

import pytest

from procurement_agent.extraction.georgian import (
    MONTH_FORMS,
    case_of,
    in_case_of,
    inflect,
    legal_form_kind,
    lemma,
    mixed_script_words,
    month_typo,
    number_to_words,
    number_words_typo,
    parse_number_words,
    render_date_like,
)


@pytest.mark.parametrize(
    ("inflected", "nominative"),
    [
        ("ნინო კაპანაძის", "ნინო კაპანაძე"),  # the truncating -ძე surname
        ("ლევან მამულაშვილის", "ლევან მამულაშვილი"),  # consonant stem
        ("ლევან მამულაშვილით", "ლევან მამულაშვილი"),
        ("ეკა ლომაიას", "ეკა ლომაია"),  # vowel stem
        ("მზის ქალაქისთვის", "მზის ქალაქი"),  # a project name with a postposition
        ("მზის ქალაქის", "მზის ქალაქი"),
        ("ტყის სახლში", "ტყის სახლი"),
        ("„მზის ქალაქისთვის“", "„მზის ქალაქი“"),  # the quotes stay where they were
        ("ნინო კაპანაძე", "ნინო კაპანაძე"),  # already nominative
    ],
)
def test_lemma_gives_the_nominative(inflected: str, nominative: str) -> None:
    assert lemma(inflected) == nominative


@pytest.mark.parametrize(
    ("lemma_", "case", "form"),
    [
        ("ნინო კაპანაძე", "gen", "ნინო კაპანაძის"),
        ("ნინო კაპანაძე", "dat", "ნინო კაპანაძეს"),
        ("ლევან მამულაშვილი", "gen", "ლევან მამულაშვილის"),
        ("ეკა ლომაია", "gen", "ეკა ლომაიას"),
        ("მზის ქალაქი", "gen_tvis", "მზის ქალაქისთვის"),
        ("ტყის სახლი", "dat_shi", "ტყის სახლში"),
    ],
)
def test_inflect(lemma_: str, case: str, form: str) -> None:
    assert inflect(lemma_, case) == form  # type: ignore[arg-type]


def test_a_correction_takes_the_case_of_the_word_it_corrects() -> None:
    # a signature block writes the nominative; the contract's preamble had the genitive
    assert in_case_of("ნინო კაპანია", "ნინო კაპანაძის") == "ნინო კაპანაძე"
    # a preamble writes the genitive; the contract's signature block had the nominative
    assert in_case_of("ნინო კაპანიას", "ნინო კაპანაძე") == "ნინო კაპანაძის"
    # a typo in the first name: the surname's case is what counts
    assert in_case_of("ნანო კაპანაძე", "ნინო კაპანაძის") == "ნინო კაპანაძე"
    assert in_case_of("ზღვის ხედი", "მზის ქალაქის") == "მზის ქალაქი"
    assert in_case_of("ზღვის ხედისთვის", "მზის ქალაქის") == "მზის ქალაქისთვის"


def test_case_of_reads_the_ending() -> None:
    assert case_of("კაპანაძის") == "gen"
    assert case_of("ქალაქისთვის") == "gen_tvis"
    assert case_of("კაპანაძე") == "nom"
    assert case_of("სილქ") == "nom"


@pytest.mark.parametrize(
    ("typo", "correct"),
    [
        ("აგისტოდან", "აგვისტოდან"),  # a letter dropped in the stem
        ("აგვუისტას", "აგვისტოს"),  # one inserted, one replaced
        ("სექტემბრდან", "სექტემბრიდან"),  # a letter dropped after the stem
        ("სეტემბრის", "სექტემბრის"),
        ("ოქტომბერის", "ოქტომბრის"),  # the genitive written without the syncope
        ("დეკემბერის", "დეკემბრის"),
        ("აგვისტოოს", "აგვისტოს"),
        ("იანვრს", "იანვარს"),
    ],
)
def test_a_misspelled_month_is_corrected_in_its_own_case(typo: str, correct: str) -> None:
    found = month_typo(typo)
    assert found is not None
    assert found.correction == correct


@pytest.mark.parametrize(
    "word",
    ["აგვისტოს", "სექტემბრიდან", "თებერვლის", "ნოემბერს", "მაისიდან", "მარტო", "მარტივად"],
)
def test_correct_month_forms_and_ordinary_words_are_not_typos(word: str) -> None:
    assert month_typo(word) is None


def test_a_typo_equally_close_to_two_months_gets_no_correction() -> None:
    found = month_typo("ივმისი")  # ივნისი or ივლისი
    assert found is not None
    assert found.correction is None


def test_every_month_has_its_forms() -> None:
    assert {m for m, _ in MONTH_FORMS.values()} == set(range(1, 13))


@pytest.mark.parametrize(
    ("example", "expected"),
    [
        ("2026 წლის 5 სექტემბერი", "2026 წლის 31 აგვისტო"),
        ("2026 წლის 5 სექტემბრის", "2026 წლის 31 აგვისტოს"),
        ("05.09.2026", "31.08.2026"),
        ("5/9/2026", "31/8/2026"),
        ("5 სექტემბერი, 2026 წ.", "31 აგვისტო, 2026 წ."),
        ("2026-09-05", "2026-08-31"),
    ],
)
def test_a_date_is_rendered_in_the_documents_style(example: str, expected: str) -> None:
    assert render_date_like(date(2026, 8, 31), example) == expected


@pytest.mark.parametrize(
    ("n", "words"),
    [
        (0, "ნული"),
        (5, "ხუთი"),
        (30, "ოცდაათი"),
        (99, "ოთხმოცდაცხრამეტი"),
        (100, "ასი"),
        (101, "ას ერთი"),
        (200, "ორასი"),
        (1000, "ათასი"),
        (2000, "ორი ათასი"),
        (2400, "ორი ათას ოთხასი"),
        (48750, "ორმოცდარვა ათას შვიდას ორმოცდაათი"),
        (1_000_000, "ერთი მილიონი"),
    ],
)
def test_number_to_words(n: int, words: str) -> None:
    assert number_to_words(n) == words
    assert parse_number_words(words) == n


def test_malformed_number_words_do_not_parse() -> None:
    assert parse_number_words("ათასი ათასი") is None
    assert parse_number_words("ასი ორასი") is None
    assert parse_number_words("ერთი ათასი") == 1000


def test_a_misspelled_numeral_is_told_from_a_different_one() -> None:
    assert number_words_typo("ასოი", 100) == "ასი"
    assert number_words_typo("ორი ათას ოთხსი", 2400) == "ორი ათას ოთხასი"
    assert number_words_typo("ორასი", 100) is None  # another number, not a typo
    assert number_words_typo("ასი", 100) is None


def test_a_latin_letter_inside_a_georgian_word_is_found() -> None:
    found = mixed_script_words("საავტორო და sმომიჯნავე უფლებები")
    assert [(w.word, w.correction) for w in found] == [("sმომიჯნავე", "მომიჯნავე")]


def test_codes_and_abbreviations_mixing_scripts_are_not_words() -> None:
    text = "GEO/ENG ს/კ: 01.02.03.456 N6 PDF-ის ა/ა GE00XX0000000000000001 2026წ."
    assert mixed_script_words(text) == []


def test_a_cyrillic_letter_or_a_digit_inside_a_word_is_found() -> None:
    cyrillic_o = chr(0x043E)
    found = mixed_script_words(f"მომსახურებ{cyrillic_o}ს სექ7ემბერი")
    assert [w.word for w in found] == [f"მომსახურებ{cyrillic_o}ს", "სექ7ემბერი"]


def test_legal_form_kind() -> None:
    assert legal_form_kind("შპს „ნიმუში“") == "company"
    assert legal_form_kind("ი/მ ნინო კაპანაძე") == "individual_entrepreneur"
    assert legal_form_kind("ფ/პ ნინო კაპანაძე") == "natural_person"
    assert legal_form_kind("Example Trading LLC") == "foreign"
    assert legal_form_kind("ნინო კაპანაძე") is None


def test_a_letter_typed_on_the_wrong_layout_is_put_back_on_its_key() -> None:
    """A Latin "e" where the Georgian "ე" belongs - the same key on the Georgian layout - is a
    substitution, and the correction restores the letter when the documents write the word."""
    from procurement_agent.extraction.georgian import mixed_script_words

    vocabulary = frozenset({"უფლებებს", "მოვალეობები"})
    [found] = mixed_script_words("მხარეების უფლeბები", vocabulary=vocabulary)
    assert found.correction == "უფლებები"
    # a stray key pressed as well is dropped, as before
    [stray] = mixed_script_words("და sმომიჯნავე", vocabulary=frozenset({"მომიჯნავე"}))
    assert stray.correction == "მომიჯნავე"


def test_a_common_noun_in_a_drops_it_before_the_genitive() -> None:
    from procurement_agent.extraction.georgian import inflect_word, lemma, lemma_word

    assert inflect_word("ხეობა", "gen") == "ხეობის"
    assert inflect_word("ხეობა", "dat") == "ხეობას"
    assert inflect_word("ხეობა", "gen_tvis") == "ხეობისთვის"
    assert lemma_word("ხეობისთვის") == "ხეობა"
    assert lemma_word("ხეობას") == "ხეობა"
    assert lemma("„თოვლის ხეობისთვის“") == "„თოვლის ხეობა“"
    # the "-ი" nouns and the surnames keep their forms
    assert lemma_word("ველის") == "ველი"
    assert lemma_word("ლომიას") == "ლომია"
    assert lemma_word("ტოგონიძის") == "ტოგონიძე"
    assert inflect_word("ტყებუჩავა", "gen") == "ტყებუჩავას"


def test_a_stray_letter_that_doubles_one_beside_it_is_dropped() -> None:
    from procurement_agent.extraction.georgian import mixed_script_words

    [found] = mixed_script_words("დირექტორiი", vocabulary=frozenset({"დირექტორი"}))
    assert found.correction == "დირექტორი"


def test_the_word_after_a_misspelled_month_decides_its_case() -> None:
    """ "სექტემბირს" is one edit from "სექტემბრის" and from "სექტემბერს"; before "ჩათვლით"
    only the genitive fits."""
    from procurement_agent.extraction.georgian import month_typo

    found = month_typo("სექტემბირს", following="ჩათვლით")
    assert found is not None and found.correction == "სექტემბრის"


def test_a_letter_added_before_the_ending_is_no_inflection() -> None:
    from procurement_agent.extraction.georgian import same_word
    from procurement_agent.extraction.spellcheck import classify_difference

    assert not same_word("მთაწმინდიის", "მთაწმინდის")
    assert same_word("მოედნის", "მოედანი")  # the vowel some nouns drop
    assert same_word("სექტემბრის", "სექტემბერი")
    assert same_word("ლაზურ", "ლაზური")
    assert classify_difference("ქ. თბილისი, ვაკიის რაიონი", "ქ. თბილისი, ვაკის რაიონი")
    assert classify_difference("ნინო კაპანაძის", "ნინო კაპანაძე") is None
