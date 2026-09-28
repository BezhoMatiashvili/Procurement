"""Prose spelling in the act (``checks/aoa_spelling.py``): a word the dictionary does not know, one
letter from a word it does, reported as AOA.SPELLING.

Three layers, so that CI (no ``models/``) still exercises everything but the pinned dictionary:

* the gates, against a small stand-in lexicon: which words are never read, which forms are left
  alone, which correction wins;
* spylls itself, on a hunspell dictionary of a handful of words written here, so that loading,
  affix lookup and the fast pre-check run for real;
* the pinned ka_GE dictionary, skipped when it is absent.

Every text is synthetic; the Georgian words are ordinary dictionary words.
"""

from __future__ import annotations

import importlib.util
import random
import re
from collections.abc import Iterable
from pathlib import Path

import pytest

from procurement_agent.checks.aoa import DocumentView, check_aoa
from procurement_agent.checks.aoa_spelling import (
    DICTIONARY_FILES,
    MAX_SEARCHES,
    Speller,
    SpellingUnavailable,
    load_speller,
    open_speller,
    prose_typos,
    quoted_spans,
)
from procurement_agent.config import Settings
from procurement_agent.extraction.georgian import damerau
from tests.unit.test_aoa_contract_rules import ACT, CONTRACT

ROOT = Path(__file__).resolve().parents[2]
KA_GE = ROOT / "models" / "hunspell"
needs_ka_ge = pytest.mark.skipif(
    not all((KA_GE / name).is_file() for name in DICTIONARY_FILES),
    reason="the hunspell ka_GE dictionary is not fetched (scripts/fetch_spelling_dictionary.py)",
)


class Lexicon:
    """A stand-in for the dictionary: it knows ``words`` and the words of ``texts`` but
    ``unknown``, and a word's neighbours are the ``words`` one edit from it."""

    def __init__(
        self, words: Iterable[str] = (), unknown: Iterable[str] = (), texts: Iterable[str] = ()
    ) -> None:
        self.words = set(words)
        self.unknown = set(unknown)
        self.written = {w for t in texts for w in re.findall("[ა-ჰ]+", t)} - self.unknown

    def reading(self, *texts: str) -> Lexicon:
        """The same lexicon, knowing the words of ``texts`` as well."""
        return Lexicon(self.words, self.unknown, texts)

    def known(self, word: str) -> bool:
        return word in self.words or word in self.written

    def neighbours(self, word: str) -> tuple[str, ...]:
        return tuple(sorted(w for w in self.words if damerau(w, word, cap=1) == 1))


def _typos(act: str, lexicon: Lexicon, contract: str | None = None) -> list[tuple[str, str]]:
    view = DocumentView.build("aoa", act)
    reference = DocumentView.build("contract", contract) if contract is not None else None
    reader = lexicon.reading(act, contract or "")
    return [(t.word, t.correction) for t in prose_typos(view, reference, reader).typos]


# --- what is reported ---------------------------------------------------------------------------

_SENTENCE = "მხარეებს ეთმანეთის მიმართ პრეტენზია არ გააჩნიათ."


def test_a_word_one_letter_from_a_known_word_is_reported_with_that_word() -> None:
    lexicon = Lexicon(words={"ერთმანეთის"}, unknown={"ეთმანეთის"})
    assert _typos(_SENTENCE, lexicon) == [("ეთმანეთის", "ერთმანეთის")]


def test_known_words_and_words_with_no_near_neighbour_are_not_reported() -> None:
    assert _typos(_SENTENCE, Lexicon(words={"ერთმანეთის"})) == []
    assert _typos(_SENTENCE, Lexicon(words={"სხვა"}, unknown={"ეთმანეთის"})) == []


def test_short_words_are_left_alone() -> None:
    # three letters: too many real words sit one letter apart
    lexicon = Lexicon(words={"ასი"}, unknown={"ასო"})
    assert _typos("ეს ასო არის.", lexicon) == []


def test_a_word_the_contract_writes_is_the_documents_own_vocabulary() -> None:
    lexicon = Lexicon(words={"ერთმანეთის"}, unknown={"ეთმანეთის"})
    assert _typos(_SENTENCE, lexicon, contract="... ეთმანეთის ...") == []


# --- which correction ---------------------------------------------------------------------------


def test_the_correction_keeps_the_ending_the_word_was_written_with() -> None:
    # a genitive written without the syncope the noun takes: the genitive is meant, not the
    # nominative or the dative that are also one letter away
    word = "შემსრულებელის"
    lexicon = Lexicon(
        words={"შემსრულებლის", "შემსრულებელი", "შემსრულებელს", "შემსრულებელია"},
        unknown={word},
    )
    assert _typos(f"1. {word} მიერ შესრულებული სამუშაო.", lexicon) == [(word, "შემსრულებლის")]


def test_a_word_the_documents_write_is_preferred_over_one_they_do_not() -> None:
    lexicon = Lexicon(words={"მხარეები", "მარები"}, unknown={"მხარები"})
    act = "მხარები და მათი ხელმოწერები. მხარეები თანხმდებიან."
    assert _typos(act, lexicon) == [("მხარები", "მხარეები")]


def test_two_equally_good_corrections_report_nothing() -> None:
    # a wrong correction is worse than none: a letter dropped either way
    lexicon = Lexicon(words={"სახლის", "სახვის"}, unknown={"სახის"})
    assert _typos("ეს სახის ნაწილია.", lexicon) == []


def test_a_correction_never_changes_the_first_letter() -> None:
    # a writer rarely mistypes the first letter; a word one letter from another at its start is
    # usually another word the dictionary lacks
    lexicon = Lexicon(words={"გარიცხვის"}, unknown={"დარიცხვის"})
    assert _typos("თანხის დარიცხვის გზით.", lexicon) == []


# --- forms the dictionary merely lacks ----------------------------------------------------------


def test_an_adjective_without_its_final_i_is_the_attributive_form() -> None:
    # one letter from the full form, which is the correction it would otherwise get
    lexicon = Lexicon(words={"ვადაგადაცილებული"}, unknown={"ვადაგადაცილებულ"})
    assert _typos("ყოველ ვადაგადაცილებულ დღეზე.", lexicon) == []


def test_a_known_word_ending_in_a_vowel_plus_a_whole_ending_is_a_form_of_it() -> None:
    # each one letter from a known form the dictionary happens to list
    lexicon = Lexicon(
        words={"შეწყვეტა", "შეწყვეტაამდე", "მოლაპარაკება", "მოლაპარაკებითა"},
        unknown={"შეწყვეტამდე", "მოლაპარაკებათა"},
    )
    assert _typos("შეწყვეტამდე ხუთი დღით ადრე.", lexicon) == []
    assert _typos("მოლაპარაკებათა გზით.", lexicon) == []


def test_one_letter_added_to_a_word_ending_in_a_vowel_is_still_a_typo() -> None:
    # the dictionary spells out a vowel stem's one-letter cases itself; one it lacks is a slip
    lexicon = Lexicon(words={"შესრულდა"}, unknown={"შესრულდად"})
    assert _typos("მომსახურება შესრულდად ჯეროვნად.", lexicon) == [("შესრულდად", "შესრულდა")]


def test_a_case_form_of_a_known_noun_the_dictionary_does_not_list_is_a_form_of_it() -> None:
    # the dictionary lists these nouns in the nominative only; each form is one letter from a
    # word it does list, which is the correction it would otherwise get
    lexicon = Lexicon(
        words={"შემსყიდველი", "პირგასამტეხლო", "მარკირება", "მარკირებას", "პირგასამტეხლოა"},
        unknown={"შემსყიდველს", "შემსყიდველმა", "პირგასამტეხლოს", "მარკირების"},
    )
    assert _typos("ხელშეკრულება შემსყიდველს უფლებას აძლევს.", lexicon) == []
    assert _typos("ხელშეკრულება შემსყიდველმა შეწყვიტა.", lexicon) == []
    assert _typos("დაეკისრება პირგასამტეხლოს გადახდა.", lexicon) == []
    assert _typos("გზის მარკირების სამუშაოები.", lexicon) == []


def test_a_genitive_that_keeps_the_vowel_its_noun_drops_is_still_a_typo() -> None:
    # "შემსრულებელი" drops its "ე" before "-ის": the dictionary's "შემსრულებლის" says so
    lexicon = Lexicon(words={"შემსრულებელი", "შემსრულებლის"}, unknown={"შემსრულებელის"})
    act = "1. შემსრულებელის მიერ შესრულებული სამუშაო."
    assert _typos(act, lexicon) == [("შემსრულებელის", "შემსრულებლის")]


def test_a_plural_is_a_form_of_its_singular_only_as_the_singular_forms_it() -> None:
    # "ტრასა" drops its "-ა" before "-ები"; "მხარე" keeps its "-ე" ("მხარეები"), so "მხარები"
    # is a letter dropped, not a plural
    lexicon = Lexicon(
        words={"ტრასა", "ტერასების", "მხარე", "მხარეები"}, unknown={"ტრასების", "მხარები"}
    )
    assert _typos("საავტომობილო ტრასების მოვლა.", lexicon) == []
    assert _typos("მხარები თანხმდებიან.", lexicon) == [("მხარები", "მხარეები")]


def test_the_short_copula_and_a_full_case_ending_are_forms_of_the_word() -> None:
    # "გაწეულია" is "გაწეული არის"; "ფეისბუქისა და" is the genitive's full form before "და"
    lexicon = Lexicon(words={"გაწეული", "ფეისბუქის"}, unknown={"გაწეულია", "ფეისბუქისა"})
    assert _typos("მომსახურება გაწეულია სრულად.", lexicon) == []
    assert _typos("ფეისბუქისა და სხვა გვერდების მართვა.", lexicon) == []


def test_the_copula_after_a_consonant_and_after_a_noun_in_a() -> None:
    # after a consonant the short copula is "-აა": "გარეთაა", "შეცდომითაა" (of "შეცდომა");
    # after a noun in "-ა" it is "-ა": "მომსახურებაა"
    # each is one letter from a word the lexicon knows, which it would otherwise be read as
    lexicon = Lexicon(
        words={"გარეთ", "გარეთა", "შეცდომა", "შეცდომითა", "მომსახურება"},
        unknown={"გარეთაა", "შეცდომითაა", "მომსახურებაა"},
    )
    assert _typos("ეს პუნქტი ხელშეკრულების გარეთაა.", lexicon) == []
    assert _typos("თანხა შეცდომითაა მითითებული.", lexicon) == []
    assert _typos("ეს უკვე მომსახურებაა და არა საქონელი.", lexicon) == []


def test_the_perfect_of_a_participle() -> None:
    # "არ შემოწმებულა", "has not been checked": the participle as a verb
    lexicon = Lexicon(
        words={"შემოწმებული", "შემოწმებულია", "გადახდილი", "გადახდილია"},
        unknown={"შემოწმებულა", "გადახდილა"},
    )
    assert _typos("აქტი ჯერ არ შემოწმებულა.", lexicon) == []
    assert _typos("ავანსი ჯერ არ გადახდილა.", lexicon) == []


def test_the_genitive_with_as_is_while() -> None:
    # "შენიშვნისას", "at the time of the remark"
    lexicon = Lexicon(words={"შენიშვნა", "შენიშვნის", "შენიშვნისა"}, unknown={"შენიშვნისას"})
    assert _typos("შენიშვნისას მიეთითება ვადა.", lexicon) == []


def test_a_verbal_noun_in_a_takes_its_one_letter_cases() -> None:
    # nouns in "-ება", "-ვა" and nouns that start with no preverb are no aorist
    lexicon = Lexicon(
        words={"გადამოწმება", "მედიანა", "უზრუნველყოფა"},
        unknown={"გადამოწმებას", "გადამოწმებამ", "მედიანას", "უზრუნველყოფად"},
    )
    assert _typos("შემკვეთი ახორციელებს გადამოწმებას.", lexicon) == []
    assert _typos("გადამოწმებამ აჩვენა შედეგი.", lexicon) == []
    assert _typos("ფასები შედარდა მედიანას.", lexicon) == []
    assert _typos("თანხა გამოიყენება უზრუნველყოფად.", lexicon) == []


def test_a_legal_form_abbreviation_is_no_word() -> None:
    lexicon = Lexicon(words={"სიპ", "ააი"}, unknown={"სსიპ", "ააიპ"})
    assert _typos("შემკვეთი: სსიპ და ააიპ წარმომადგენლები.", lexicon) == []


def test_a_letter_added_to_a_verb_ending_in_a_is_no_case_form() -> None:
    # an aorist ("შესრულდა") looks like a noun ending in "-ა"; an ending added to it is a slip
    lexicon = Lexicon(words={"შესრულდა"}, unknown={"შესრულდას", "შესრულდაა"})
    assert _typos("მომსახურება შესრულდას ჯეროვნად.", lexicon) == [("შესრულდას", "შესრულდა")]
    assert _typos("მომსახურება შესრულდაა ჯეროვნად.", lexicon) == [("შესრულდაა", "შესრულდა")]


def test_another_person_or_version_of_a_verb_is_a_form_the_dictionary_lacks() -> None:
    # "-ს" and "-თ" are the verb's person, a final "-თ" its plural, and the vowel after the
    # preverb its version: "გამოიწეროს" beside "გამოწეროს", "გაუწეროს" beside "გაიწეროს"
    lexicon = Lexicon(
        words={"გადაამოწმოთ", "გამოწეროს", "გაიწეროს", "გადავურიცხე"},
        unknown={"გადაამოწმოს", "გამოიწეროს", "გაუწეროს", "გადავურიცხეთ"},
    )
    assert _typos("შემკვეთმა უნდა გადაამოწმოს აქტი.", lexicon) == []
    assert _typos("ანგარიშფაქტურა უნდა გამოიწეროს ლარში.", lexicon) == []
    assert _typos("შემსრულებელმა უნდა გაუწეროს სწორი ვარიანტი.", lexicon) == []
    assert _typos("ავანსი გადავურიცხეთ ათ დღეში.", lexicon) == []


def test_a_letter_from_a_distant_key_makes_another_word_not_a_slip() -> None:
    # "ტ" and "ვ", "ა" and "ხ" sit far apart and sound nothing alike: "კონტენტის" is a word the
    # dictionary lacks, not a slip for "კონვენტის"
    lexicon = Lexicon(words={"კონვენტის", "ლხინში"}, unknown={"კონტენტის", "ლაინში"})
    assert _typos("ვებგვერდის კონტენტის განახლება.", lexicon) == []
    assert _typos("ცხრილის რომელ ლაინში რა შეიცვალა.", lexicon) == []


def test_a_letter_from_a_neighbouring_key_or_of_the_same_sound_is_a_slip() -> None:
    lexicon = Lexicon(words={"მომსახურების", "პირობების"}, unknown={"მომსახირების", "პირობეპის"})
    assert _typos("მომსახირების ღირებულება.", lexicon) == [("მომსახირების", "მომსახურების")]
    assert _typos("ხელშეკრულების პირობეპის შესაბამისად.", lexicon) == [("პირობეპის", "პირობების")]


def test_a_distant_letter_is_a_slip_when_the_documents_write_the_word_it_replaced() -> None:
    lexicon = Lexicon(words={"სამუშაოები"}, unknown={"სამუშაოჟბი"})
    act = "სამუშაოები დასრულდა. ყველა სამუშაოჟბი მიღებულია."
    assert _typos(act, lexicon) == [("სამუშაოჟბი", "სამუშაოები")]


def test_the_full_dative_is_a_form_of_the_word() -> None:
    lexicon = Lexicon(words={"ფაილს"}, unknown={"ფაილსა"})
    assert _typos("საწყის ფაილსა და მიღებულ ფაილებს შორის.", lexicon) == []


def test_a_participle_with_the_other_participle_vowel_is_a_usage_question() -> None:
    # past participles take -ილ- or -ულ-; the other one is widespread and not a slip
    lexicon = Lexicon(words={"შედგენილია"}, unknown={"შედგენულია"})
    assert _typos("აქტი შედგენულია ორ ეგზემპლარად.", lexicon) == []


# --- words another rule or a name owns ----------------------------------------------------------


def test_words_in_quotes_are_names_or_defined_terms() -> None:
    lexicon = Lexicon(words={"ბაღისთვის"}, unknown={"ბაღისთის"})
    assert _typos("პროექტ „მწვანე ბაღისთის“ მომსახურება.", lexicon) == []
    assert _typos("მწვანე ბაღისთის მომსახურება.", lexicon) == [("ბაღისთის", "ბაღისთვის")]


def test_a_stray_closing_quote_does_not_hide_the_words_after_it() -> None:
    # the closing quote after "მხარეები" has no opening one; pairing quotes by count would put
    # the rest of the text inside quotes
    text = "= მხარეები“ თანხმდებიან, რომ „შემკვეთი“ ადასტურებს. “მხარეებს” ეთმანეთის მიმართ."
    lexicon = Lexicon(words={"ერთმანეთის"}, unknown={"ეთმანეთის"})
    assert _typos(text, lexicon) == [("ეთმანეთის", "ერთმანეთის")]
    quoted = [text[a:b] for a, b in quoted_spans(text)]
    assert quoted == ["შემკვეთი", "მხარეებს"]


def test_quotes_of_every_style_are_paired() -> None:
    text = '„ალფა“ ,,ბეტა” “გამა” "დელტა" «ეფსილონ» „ ზეტა “'
    assert [text[a:b].strip() for a, b in quoted_spans(text)] == [
        "ალფა",
        "ბეტა",
        "გამა",
        "დელტა",
        "ეფსილონ",
        "ზეტა",
    ]


def test_an_unquoted_name_after_a_legal_form_is_left_alone() -> None:
    lexicon = Lexicon(words={"ბეტანი"}, unknown={"ბეტასი"})
    assert _typos("შპს ბეტასი სერვისი, საიდენტიფიკაციო კოდი: 123456789", lexicon) == []


def test_a_signatory_is_left_alone_wherever_the_name_appears() -> None:
    # the director rule owns a misspelt name, and a mention with no label is the same name: the
    # anchors read it once, so a word of it is protected everywhere, not only at that span
    act = ACT.replace("დავით მელაძე", "დავიტ მელაძე") + "ხელმოწერა: დავიტ მელაძე\n"
    lexicon = Lexicon(words={"დავით"}, unknown={"დავიტ"})
    assert _typos(act, lexicon) == []
    assert _typos("ეს დავიტ არის.", lexicon) == [("დავიტ", "დავით")]


def test_after_a_signatory_s_first_name_and_surname_the_text_is_prose_again() -> None:
    lexicon = Lexicon(words={"სახით"}, unknown={"სახთ"})
    act = "წარმოდგენილი დირექტორის ნინო კაპანაძის სახთ და"
    assert _typos(act, lexicon) == [("სახთ", "სახით")]


def test_words_of_an_amount_and_of_a_date_belong_to_their_rules() -> None:
    lexicon = Lexicon(words={"ასი", "აგვისტოს"}, unknown={"ასოი", "აგვისტს"})
    act = "საფასური შეადგენს 100 (ასოი) ლარს. აქტი გაფორმდა 2026 წლის 31 აგვისტს."
    assert _typos(act, lexicon) == []


def test_a_word_with_a_letter_of_another_alphabet_belongs_to_the_script_rule() -> None:
    lexicon = Lexicon(words={"მომიჯნავე"}, unknown={"მომიჯნaვე"})
    assert _typos("საავტორო და მომიჯნaვე უფლებები.", lexicon) == []


def test_the_dictionary_settles_which_letter_a_stray_one_stood_for() -> None:
    # "e" is the key of "ე": with the layout switched "შეთანხმeბები" reads "შეთანხმებები", with
    # the letter dropped "შეთანხმბები"; neither document writes either, the dictionary knows one
    act = ACT.replace("მომსახურების გაწევა.", "მომსახურების გაწევა. მხარეთა შეთანხმeბები.")

    def corrections(speller: Lexicon | None) -> list[str | None]:
        report = check_aoa(aoa_text=act, contract_text=CONTRACT, speller=speller)
        return [f.expected for f in report.findings if f.actual == "შეთანხმeბები"]

    assert corrections(Lexicon(words={"შეთანხმებები"}).reading(act, CONTRACT)) == ["შეთანხმებები"]
    assert corrections(None) == ["შეთანხმბები"]  # no dictionary: the stray letter goes
    # both readings are words: inside a word the stray letter stood for its key's letter
    both = Lexicon(words={"შეთანხმებები", "შეთანხმბები"}).reading(act, CONTRACT)
    assert corrections(both) == ["შეთანხმებები"]


def test_a_stray_letter_at_the_edge_of_a_word_was_a_key_pressed_as_well() -> None:
    act = ACT.replace("მომსახურების გაწევა.", "მომსახურების გაწევა. მხარეთა sშეთანხმებები.")
    lexicon = Lexicon(words={"შეთანხმებები", "სშეთანხმებები"}).reading(act, CONTRACT)
    report = check_aoa(aoa_text=act, contract_text=CONTRACT, speller=lexicon)
    found = [f.expected for f in report.findings if f.actual == "sშეთანხმებები"]
    assert found == ["შეთანხმებები"]


# --- inside the act check -----------------------------------------------------------------------

_TYPO_ACT = ACT.replace("მომსახურების გაწევა.", "მომსახურების გაწევა. მხარეებს ეთმანეთის მიმართ.")
_LEXICON = Lexicon(words={"ერთმანეთის"}, unknown={"ეთმანეთის"}).reading(_TYPO_ACT, CONTRACT)


def test_the_act_check_reports_a_prose_typo_where_it_is_written() -> None:
    report = check_aoa(aoa_text=_TYPO_ACT, contract_text=CONTRACT, speller=_LEXICON)
    found = [f for f in report.findings if f.rule_id == "AOA.SPELLING"]
    assert len(found) == 1
    f = found[0]
    assert (f.severity, f.actual, f.expected, f.quote) == (
        "warning",
        "ეთმანეთის",
        "ერთმანეთის",
        "ეთმანეთის",
    )
    assert f.span is not None
    assert DocumentView.build("aoa", _TYPO_ACT).text[f.span[0] : f.span[1]] == "ეთმანეთის"
    # the Word comment prints ``expected`` on a line of its own
    assert "ეთმანეთის" in f.message_ka
    assert "ეთმანეთის" in f.message_en
    assert not [s for s in report.skipped if s.rule_id == "AOA.SPELLING"]


def test_on_a_scanned_act_a_prose_typo_may_be_a_misreading() -> None:
    # a misread letter looks exactly like a typo: said so, and left for a person to check
    scanned = check_aoa(aoa_text=_TYPO_ACT, contract_text=CONTRACT, speller=_LEXICON, aoa_ocr=True)
    [f] = [f for f in scanned.findings if f.rule_id == "AOA.SPELLING"]
    assert (f.severity, f.expected, f.needs_review) == ("warning", "ერთმანეთის", True)
    assert f.message_en.startswith("Possibly misread from the scan")
    # a scanned contract says nothing about how the act was read
    typed = check_aoa(
        aoa_text=_TYPO_ACT, contract_text=CONTRACT, speller=_LEXICON, contract_ocr=True
    )
    [g] = [g for g in typed.findings if g.rule_id == "AOA.SPELLING"]
    assert not g.needs_review
    assert g.message_en.startswith("word appears misspelled")


def test_the_correct_act_stays_clean_and_a_rerun_reports_the_same() -> None:
    checker_input = {"aoa_text": ACT, "contract_text": CONTRACT, "speller": _LEXICON}
    assert not [f for f in check_aoa(**checker_input).findings if f.rule_id == "AOA.SPELLING"]
    first = check_aoa(aoa_text=_TYPO_ACT, contract_text=CONTRACT, speller=_LEXICON)
    second = check_aoa(aoa_text=_TYPO_ACT, contract_text=CONTRACT, speller=_LEXICON)
    assert first.findings == second.findings


def test_a_month_typo_is_reported_once_by_the_month_rule() -> None:
    act = ACT.replace("30 აპრილის ჩათვლით", "30 აპრლის ჩათვლით")
    lexicon = Lexicon(words={"აპრილის"}, unknown={"აპრლის"}).reading(act, CONTRACT)
    report = check_aoa(aoa_text=act, contract_text=CONTRACT, speller=lexicon)
    spelling = [f for f in report.findings if f.rule_id == "AOA.SPELLING"]
    assert [(f.actual, f.expected) for f in spelling] == [("აპრლის", "აპრილის")]
    assert "month" in spelling[0].message_en


def test_the_correction_search_stops_at_its_limit_and_counts_the_rest() -> None:
    act = "ერთი სიტყვა: ეთმანეთის, შემდეგ იდენტიურ, ბოლოს ეგზემპლიარად და ეთმანეთის."
    lexicon = Lexicon(
        words={"ერთმანეთის", "იდენტურ", "ეგზემპლარად"},
        unknown={"ეთმანეთის", "იდენტიურ", "ეგზემპლიარად"},
    ).reading(act)
    reading = prose_typos(DocumentView.build("aoa", act), None, lexicon, limit=2)
    # a word already searched is no new search: its second mention is reported too
    assert [t.word for t in reading.typos] == ["ეთმანეთის", "იდენტიურ", "ეთმანეთის"]
    assert reading.unsearched == 1
    short = reading.shortfall()
    assert short is not None
    assert "1 more" in short.reason_en


def test_a_document_past_the_limit_says_how_much_went_unchecked() -> None:
    # 48 distinct unknown words, each a word the lexicon knows with a letter dropped
    unknown = [f"ტექსტ{a}{b}" for a in "აბგდევ" for b in "ვზთიკლმნ"]
    lexicon = Lexicon(
        words={f"ტექსტ{a}ჰ{b}" for a in "აბგდევ" for b in "ვზთიკლმნ"}, unknown=unknown
    )
    act = ACT + " ".join(unknown) + ".\n"
    report = check_aoa(aoa_text=act, contract_text=CONTRACT, speller=lexicon.reading(act, CONTRACT))
    assert len([f for f in report.findings if f.rule_id == "AOA.SPELLING"]) == MAX_SEARCHES
    notes = [s.reason_en for s in report.skipped if s.rule_id == "AOA.SPELLING"]
    assert len(notes) == 1
    assert f"{48 - MAX_SEARCHES} more words" in notes[0]


def test_without_a_dictionary_the_check_says_prose_spelling_did_not_run() -> None:
    report = check_aoa(aoa_text=_TYPO_ACT, contract_text=CONTRACT)
    assert not [f for f in report.findings if f.rule_id == "AOA.SPELLING"]
    notes = [s for s in report.skipped if s.rule_id == "AOA.SPELLING"]
    assert len(notes) == 1
    assert "dictionary" in notes[0].reason_en


def test_an_unavailable_dictionary_says_why_in_the_skip_note(tmp_path: Path) -> None:
    missing = load_speller(tmp_path)
    assert isinstance(missing, SpellingUnavailable)
    report = check_aoa(aoa_text=_TYPO_ACT, contract_text=CONTRACT, speller=missing)
    notes = [s for s in report.skipped if s.rule_id == "AOA.SPELLING"]
    assert [n.reason_en for n in notes] == [missing.reason_en]


# --- loading ------------------------------------------------------------------------------------


def test_missing_files_are_named_and_never_raise(tmp_path: Path) -> None:
    none = load_speller(tmp_path)
    assert isinstance(none, SpellingUnavailable)
    assert "ka_GE.aff" in none.reason_en
    assert "ka_GE.dic" in none.reason_en
    (tmp_path / "ka_GE.aff").write_text("SET UTF-8\n", encoding="utf-8")
    one = load_speller(tmp_path)
    assert isinstance(one, SpellingUnavailable)
    assert "ka_GE.dic" in one.reason_en
    assert "ka_GE.aff" not in one.reason_en


def test_an_unreadable_dictionary_is_reported_not_raised(tmp_path: Path) -> None:
    (tmp_path / "ka_GE.aff").write_bytes(b"SET UTF-8\nSFX A Y 2\nSFX A\n")
    (tmp_path / "ka_GE.dic").write_bytes(b"\xff\xfe\x00 not a count\n")
    broken = load_speller(tmp_path)
    assert isinstance(broken, SpellingUnavailable)


def test_the_switch_turns_prose_spelling_off(tmp_path: Path) -> None:
    off = open_speller(Settings(prose_spelling_enabled=False, spelling_dictionary_path=tmp_path))
    assert isinstance(off, SpellingUnavailable)
    assert "PA_PROSE_SPELLING_ENABLED" in off.reason_en


# --- spylls on a dictionary written here --------------------------------------------------------

_AFF = """SET UTF-8
TRY აიესრმოდლნვბთუგხშცტკქწზყფპღძჩჯჭჰჟ

SFX A Y 3
SFX A ი ის ი
SFX A ი ით ი
SFX A ი ს ი

SFX B Y 3
SFX B 0 ს [აეოუ]
SFX B 0 ში [აეოუ]
SFX B 0 მდე [აეოუ]

SFX E Y 2
SFX E ელი ლის ელი
SFX E ი ს ი

PFX P Y 1
PFX P 0 გა .
"""
_DIC = """6
სახლი/A
ერთმანეთი/A
მხარე/B
კეთება/BP
შემსრულებელი/E
სამუშაო
"""


@pytest.fixture
def small(tmp_path: Path) -> Speller:
    (tmp_path / "ka_GE.aff").write_text(_AFF, encoding="utf-8")
    (tmp_path / "ka_GE.dic").write_text(_DIC, encoding="utf-8")
    speller = load_speller(tmp_path)
    assert isinstance(speller, Speller), speller
    return speller


def test_spylls_reads_words_through_their_affixes(small: Speller) -> None:
    for word in ("სახლის", "სახლით", "მხარეში", "გაკეთება", "გაკეთებამდე", "შემსრულებლის"):
        assert small.known(word), word
    for word in ("სახლში", "მხარის", "გასახლი", "შემსრულებელის", "ეთმანეთის"):
        assert not small.known(word), word


def test_neighbours_are_known_words_one_edit_away(small: Speller) -> None:
    assert small.neighbours("ეთმანეთის") == ("ერთმანეთის",)
    assert set(small.neighbours("შემსრულებელის")) >= {"შემსრულებლის", "შემსრულებელს"}
    assert small.neighbours("გაკეთებაში") == ()  # known words have none to offer here
    assert small.neighbours("ბბბბბბ") == ()


def test_the_fast_path_agrees_with_plain_spylls(small: Speller) -> None:
    words = ["სახლის", "გაკეთებამდე", "შემსრულებლის", "მხარეში", "ერთმანეთით"]
    edits = {e for w in words for e in _one_edits(w)}
    assert small.agrees_with_spylls(sorted(edits)) == []


def _one_edits(word: str) -> list[str]:
    letters = "აიესრმოდლნვბთუგხშცტკქწზყფპღძჩჯჭჰჟ"
    out = [word[:i] + word[i + 1 :] for i in range(len(word))]
    out += [word[:i] + c + word[i:] for i in range(len(word) + 1) for c in letters]
    out += [word[:i] + c + word[i + 1 :] for i in range(len(word)) for c in letters]
    out += [word[:i] + word[i + 1] + word[i] + word[i + 2 :] for i in range(len(word) - 1)]
    return out


# --- the pinned ka_GE dictionary ----------------------------------------------------------------


@pytest.fixture(scope="module")
def ka_ge() -> Speller:
    speller = load_speller(KA_GE)
    assert isinstance(speller, Speller), speller
    return speller


@needs_ka_ge
def test_ka_ge_corrects_the_typo_shapes_of_legal_prose(ka_ge: Speller) -> None:
    act = ACT.replace(
        "მომსახურების გაწევა.",
        "მომსახურების გაწევა. მხარეებს ეთმანეთის მიმართ პრეტენზია არ გააჩნიათ. "
        "1. შემსრულებელის მიერ შექმნილი. აქტი შედგენილია ორ იდენტიურ ეგზემპლარად.",
    )
    report = check_aoa(aoa_text=act, contract_text=CONTRACT, speller=ka_ge)
    found = {(f.actual, f.expected) for f in report.findings if f.rule_id == "AOA.SPELLING"}
    assert found == {
        ("ეთმანეთის", "ერთმანეთის"),
        ("შემსრულებელის", "შემსრულებლის"),
        ("იდენტიურ", "იდენტურ"),
    }


@needs_ka_ge
def test_ka_ge_leaves_regular_forms_it_does_not_list_alone(ka_ge: Speller) -> None:
    """Forms of words it knows, which ka_GE does not list: a dative, an ergative, a genitive, a
    plural, the copula, a full case ending. Each was reported on a correct document once."""
    act = ACT.replace(
        "მომსახურების გაწევა.",
        "მომსახურების გაწევა. მომსახურება გაწეულია და ჩაბარებულია. შემსყიდველს და "
        "შემსყიდველმა ეკისრება პირგასამტეხლოს გადახდა; გზის მარკირების და ტრასების მოვლა; "
        "ფეისბუქისა და სხვა გვერდების მართვა.",
    )
    report = check_aoa(aoa_text=act, contract_text=CONTRACT, speller=ka_ge)
    assert [f.actual for f in report.findings if f.rule_id == "AOA.SPELLING"] == []


@needs_ka_ge
def test_ka_ge_leaves_verb_forms_and_loanwords_it_does_not_list_alone(ka_ge: Speller) -> None:
    """Each was reported on a correct text once: a verb in another person or version, the full
    dative, a loanword one distant letter from a word ka_GE knows."""
    act = ACT.replace(
        "მომსახურების გაწევა.",
        "მომსახურების გაწევა. შემკვეთმა უნდა გადაამოწმოს აქტი, ანგარიშფაქტურა უნდა "
        "გამოიწეროს და გაუწეროს სწორი ვარიანტი; ავანსი გადავურიცხეთ ათ დღეში. საწყის "
        "ფაილსა და ცხრილის რომელ ლაინში რა შეიცვალა; ვებგვერდის კონტენტის განახლება.",
    )
    report = check_aoa(aoa_text=act, contract_text=CONTRACT, speller=ka_ge)
    assert [f.actual for f in report.findings if f.rule_id == "AOA.SPELLING"] == []


@needs_ka_ge
def test_ka_ge_leaves_the_correct_synthetic_act_clean(ka_ge: Speller) -> None:
    report = check_aoa(aoa_text=ACT, contract_text=CONTRACT, speller=ka_ge)
    assert not [f for f in report.findings if f.rule_id == "AOA.SPELLING"]


@needs_ka_ge
def test_ka_ge_fast_path_agrees_with_plain_spylls(ka_ge: Speller) -> None:
    rng = random.Random(7)
    words = re.findall("[ა-ჰ]{5,}", ACT + CONTRACT)
    edits = sorted({e for w in set(words) for e in _one_edits(w)})
    sample = rng.sample(edits, 1500) + sorted(set(words))
    assert ka_ge.agrees_with_spylls(sample) == []


# --- the pinned source, twice -------------------------------------------------------------------


def test_the_dockerfile_pins_the_same_files_as_the_fetch_script() -> None:
    spec = importlib.util.spec_from_file_location(
        "fetch_spelling_dictionary", ROOT / "scripts" / "fetch_spelling_dictionary.py"
    )
    assert spec is not None
    assert spec.loader is not None
    fetch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fetch)
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    add = re.compile(r"ADD\s+--checksum=sha256:([0-9a-f]{64})\s+.*?(https://\S+)")
    pinned = {url: digest for digest, url in add.findall(dockerfile)}
    assert pinned == dict(fetch.FILES.values())
    assert set(fetch.FILES) == set(DICTIONARY_FILES)
