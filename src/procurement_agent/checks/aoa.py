"""Acceptance act (მიღება-ჩაბარების აქტი) verification against its contract.

Implements the rules the client wrote in "AI instructions.docx", one rule id each, so that
every rule can be scored, switched off, or explained on its own. The check is fully
deterministic: it reads label-anchored fields and rule hits, and never calls the LLM.

Two principles run through it:

* A rule that cannot run says so (``CheckReport.skipped``) instead of passing quietly. Silent
  no-ops are how a checker comes to look clean while checking nothing.
* Nothing is reported as an error unless the contract (or registry) states the correct value.
  The correct act must produce no findings at all, which is the hardest constraint here and
  the one that decides whether the client trusts the rest.
"""

from __future__ import annotations

import calendar
import re
import unicodedata
from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from functools import lru_cache
from itertools import pairwise
from typing import Literal

from procurement_agent.checks.amounts import (
    CURRENCY,
    FIGURE_TETRI,
    DigitsAndWords,
    Money,
    digits_and_words,
    header_money,
    render_figure_like,
    with_currency,
    words_for,
    words_like,
)
from procurement_agent.checks.aoa_llm import (
    SUBJECT_ID,
    Decision,
    ModelVerdict,
    ReviewOutcome,
    ScopeItem,
    ScopeReviewSet,
    decide,
    escalated,
    model_reading,
    resolve_review,
    review_items,
    service_id,
)
from procurement_agent.checks.aoa_scope import (
    MATCH_SHARE,
    Alignment,
    ScopePart,
    ScopeReading,
    align,
    read_scope,
    scope_part,
)
from procurement_agent.checks.aoa_spelling import (
    NOT_SUPPLIED,
    Lexicon,
    SpellingUnavailable,
    prose_typos,
    spelled_right,
)
from procurement_agent.checks.contract_terms import _ADVANCE as _ADVANCE_NAMED
from procurement_agent.checks.contract_terms import (
    _NOT_A_TERM,
    _VALIDITY,
    AdvanceTerms,
    AmountRole,
    ContractTerm,
    Deadline,
    MethodHit,
    PaymentMethod,
    PaymentTerms,
    RoledAmount,
    TaxPhrase,
    _range_in,
    amount_roles,
    fee_scope,
    read_advance,
    read_payment_terms,
    read_term,
    sentence_at,
    sentence_spans,
    tax_phrases,
)
from procurement_agent.checks.findings import CheckReport, Finding, Severity, SkipNote
from procurement_agent.checks.fx_invoice import (
    LOCAL_CURRENCY,
    AdvanceInput,
    InvoicePlan,
    InvoiceTerm,
    NoInvoice,
    PricedTerm,
    fee_currencies,
    foreign_currencies,
    invoice_gel,
    plan_invoice,
    price_terms,
    to_money,
)
from procurement_agent.extraction.anchors import (
    PATRONYMIC,
    AnchorHit,
    Party,
    defined_sides,
    find_anchors,
    find_parties,
    preamble_end,
    split_patronymic,
    with_patronymic,
)
from procurement_agent.extraction.georgian import (
    CASES,
    GEORGIAN,
    MONTH_FORMS,
    attested_nominatives,
    damerau,
    in_case_of,
    inflect,
    is_georgian_word,
    lemma,
    lemma_word,
    mixed_script_words,
    number_words_typo,
    render_date_like,
    same_word,
    word_lemmas,
)
from procurement_agent.extraction.georgian import words as georgian_words
from procurement_agent.extraction.normalize import (
    ID_CODE_WRITTEN_RE,
    amounts_equal,
    edit_distance,
    fold_comma_quotes_mapped,
    fold_look_alikes,
    iban_is_valid,
    id_code_digits,
    in_grouping,
    keep_initial,
    name_stem,
    names_match,
    normalize_confusables,
    normalize_layout,
    normalize_name,
)
from procurement_agent.extraction.ocr import misread_explains, repair_codes
from procurement_agent.extraction.rules import (
    _NUMBER_MARKER,
    _REFERENCE,
    RuleHit,
    _month_ka,
    _words_before,
    find_dates,
    month_ka_correction,
)
from procurement_agent.extraction.schemas import FactType
from procurement_agent.extraction.spellcheck import Difference, best_match, classify_difference
from procurement_agent.fx.provider import ENTERED, NullRateProvider, RateProvider
from procurement_agent.report.docx_annotate import whole_word_spans

ContractorType = Literal[
    "company", "natural_person", "individual_entrepreneur_vat", "small_business", "foreign"
]

#: Rules whose value is a name, which the act may write in any case: a correction is given in
#: the case the act used (``georgian.in_case_of``).
_NAME_RULES = frozenset({"AOA.PARTY_NAME", "AOA.REGISTRY_NAME"})

#: Rules comparing a value the act writes with the one the contract (or the registry) writes.
#: On a scan, a difference scanning explains is a misreading to check, not a mistake.
_OCR_COMPARED = frozenset(
    {
        "AOA.DIRECTOR", "AOA.PARTY_NAME", "AOA.PARTY_ADDRESS", "AOA.PROJECT_NAME", "AOA.BANK",
        "AOA.ID_CODES", "AOA.CADASTRAL", "AOA.REGISTRY_NAME", "AOA.REGISTRY_ADDRESS",
        "AOA.REGISTRY_DIRECTOR",
    }
)  # fmt: skip
#: Of those, the ones whose value is a name that takes case endings.
_NAME_VALUED = frozenset(
    {"AOA.DIRECTOR", "AOA.PARTY_NAME", "AOA.PROJECT_NAME", "AOA.REGISTRY_NAME",
     "AOA.REGISTRY_DIRECTOR"}
)  # fmt: skip
_MISREAD_KA = "შესაძლოა, სკანირებისას არასწორად წაიკითხა — გადაამოწმეთ ქაღალდის ორიგინალთან. "
_MISREAD_EN = "Possibly misread from the scan - check the paper original. "

#: Bank details: the account (IBAN) and the bank's code (BIC), told apart by ``unit``.
_BANK_TYPES = frozenset({FactType.bank_account, FactType.bank_code})

#: Roles that receive the payment an act releases, and roles that make it.
_PAYEE_ROLES = frozenset({"შემსრულებელი", "გამყიდველი", "მიმწოდებელი"})
_PAYER_ROLES = frozenset({"დამკვეთი", "შემკვეთი", "მყიდველი"})

Side = Literal["payer", "payee"]
#: A side as the English messages name it; the Georgian ones quote the document's own role word.
_SIDE_EN: dict[Side, str] = {"payer": "the customer", "payee": "the contractor"}


@dataclass(frozen=True)
class _TaxRule:
    """What the act must say about taxes for one contractor type (client rules 5-8)."""

    #: the kinds of tax wording (``contract_terms.TaxPhrase.kind``) that contradict the rule
    offends: frozenset[str]
    #: the rule, as a finding states it
    rule_ka: str
    rule_en: str
    #: what is missing when the act never says what the rule asks for
    absent_ka: str
    absent_en: str
    #: the wording to write instead, for a Georgian and for an English sentence
    fix_ka: str
    fix_en: str


_TAX_RULES: dict[ContractorType, _TaxRule] = {
    "natural_person": _TaxRule(
        offends=frozenset({"inclusion"}),
        rule_ka="ფიზიკური პირისთვის ანაზღაურება უნდა ეწეროს NET ღირებულებით "
        "(საშემოსავლო და საპენსიო გადასახადების გარეშე)",
        rule_en="for a natural person the fee must be stated NET "
        "(excluding income tax and pension contributions)",
        absent_ka="აქტში არც NET წერია და არც ის, რომ საშემოსავლო და საპენსიო გადასახადები "
        "გამორიცხულია",
        absent_en="the act says neither NET nor that income tax and pension contributions are "
        "both excluded",
        fix_ka="NET (საშემოსავლო და საპენსიო გადასახადების გარეშე)",
        fix_en="NET (excluding income tax and pension contributions)",
    ),
    "individual_entrepreneur_vat": _TaxRule(
        offends=frozenset({"exclusion"}),
        rule_ka="დღგ-ს გადამხდელი ინდ. მეწარმისთვის უნდა ეწეროს, რომ თანხა დღგ-ს ჩათვლითაა",
        rule_en="for a VAT-registered individual entrepreneur the fee must say it includes VAT",
        absent_ka="აქტში არ წერია, რომ თანხა დღგ-ს ჩათვლითაა",
        absent_en="the act does not say VAT is included",
        fix_ka="დღგ-ს ჩათვლით",
        fix_en="including VAT",
    ),
    "small_business": _TaxRule(
        offends=frozenset({"exclusion"}),
        rule_ka="მცირე მეწარმისთვის უნდა ეწეროს, რომ „ანგარიშსწორება ითვალისწინებს გადასახადებს“",
        rule_en="for a small business the act must state that settlement includes taxes",
        absent_ka="აქტში არ წერია, რომ ანგარიშსწორება გადასახადებს ითვალისწინებს",
        absent_en="the act does not say taxes are included",
        fix_ka="გადასახადების ჩათვლით",
        fix_en="including taxes",
    ),
    "foreign": _TaxRule(
        offends=frozenset({"inclusion", "exclusion", "mention"}),
        rule_ka="უცხოური ორგანიზაციისთვის თანხა უნდა ეწეროს NET, გადასახადების ხსენების გარეშე",
        rule_en="for a foreign organisation the amount must be stated NET, with no mention "
        "of taxes",
        absent_ka="აქტში NET არ წერია",
        absent_en="the act does not say NET",
        fix_ka="NET",
        fix_en="NET",
    ),
}

_GEORGIAN = re.compile(r"[ა-ჰ]")
_LATIN = re.compile(r"[A-Za-z]")

#: What an act amount is called in a fee finding, by its role - (Georgian, its genitive,
#: English). An advance or a remainder line is the fee paid in parts.
_FEE_LABEL: dict[AmountRole | None, tuple[str, str, str]] = {
    None: ("საფასური", "საფასურის", "fee"),
    "price": ("საფასური", "საფასურის", "fee"),
    "advance": ("ავანსი", "ავანსის", "advance"),
    "remainder": ("დარჩენილი თანხა", "დარჩენილი თანხის", "remainder"),
}
#: A contract amount that is not a price, as a fee finding names it when the act's fee equals
#: it: (Georgian, in the dative "ემთხვევა" takes; English).
_ROLE_NAME: dict[AmountRole, tuple[str, str]] = {
    "advance": ("ავანსს", "the advance"),
    "remainder": ("დარჩენილ თანხას", "the remainder"),
    "penalty": ("პირგასამტეხლოს", "the penalty"),
    "guarantee": ("საგარანტიო თანხას", "the guarantee"),
    "tax": ("გადასახადის თანხას", "the tax amount"),
}
#: Act amounts that are neither the fee nor a part of it. A running figure of the contract -
#: done to date, the balance left, paid before - is none (red team R4, AOA-R4-08).
_NOT_THE_FEE: frozenset[AmountRole | None] = frozenset(
    {"penalty", "guarantee", "tax", "equivalent", "running"}
)

_NO_CONTRACT = "this rule compares the act against its contract, and none was supplied"

#: How far after a fee before tax its "excl. VAT" may stand and still be its label.
_NET_LABEL_REACH = 30

#: What a party's field was compared against, as the messages name it: (Georgian, in the
#: dative that "არ ემთხვევა" takes; English).
_SOURCE: dict[str, tuple[str, str]] = {
    "contract": ("ხელშეკრულებას", "the contract"),
    "registry": ("რეესტრის მონაცემებს", "the registry"),
}

#: Contractor types for which the client does *not* want an invoice calculation (rule 9).
_FX_EXEMPT: frozenset[str] = frozenset({"small_business", "foreign"})

#: Rules the client's instructions do not apply to a contractor type at all, by rule id. They
#: go in the summary, not in a skip note: a skip note reads "could not run", and nothing here
#: failed to run - there was nothing for it to check.
_NOT_APPLICABLE: tuple[tuple[str, frozenset[str]], ...] = (
    ("AOA.TAX_WORDING", frozenset({"company"})),
    ("AOA.FX_INVOICE", _FX_EXEMPT),
)

#: A label that dates the act - its signing ("გაფორმების"), its drawing up ("შედგენის") or its
#: signature ("ხელმოწერის") "თარიღი", also with its copula ("აქტის გაფორმების თარიღია ...", red
#: team R4). The label must stand as a word: "ხელმოწერის თარიღიდან 5 დღეში" counts a deadline
#: from the date instead.
_ACT_DATE_LABEL = r"(?:გაფორმების|შედგენის|ხელმოწერის)\s+თარიღია?(?![ა-ჰ])"
#: The verbs that say the act was signed or drawn up, with the act named before them: the
#: signing in the aorist and the perfect ("გაფორმდა", "გაფორმებულია"), the drawing up
#: ("შედგა", "შედგენილია"), the signature ("ხელმოწერილია"; "აქტს ხელი მოეწერა"). Each a whole
#: word: "აქტი შედგება ორი გვერდისგან" is what it consists of. One alternation for every reader
#: of the act's date (``_ACT_SIGNED``, ``_ACT_DATED_BEFORE``), red team R4 (AOA-R4-07). Only with
#: a date in the same sentence (``AoaChecker._act_date``): "აქტი შედგენილია ორ ეგზემპლარად" dates
#: nothing. An act in English says it the same way: "This Act was signed on 30 June 2026", "the
#: Certificate is executed on ..." (red team R5, AOA-R5-11); "The Agreement was signed on ..."
#: is the contract's, and "This Act is made in two copies" dates nothing.
_ACT_VERB = (
    r"(?<![ა-ჰ])(?:აქტი[\s„“”\"']*(?:გაფორმდა|გაფორმებულია|შედგა|შედგენილია|ხელმოწერილია)"
    r"|აქტს?[\s„“”\"']*ხელი\s+მოეწერა"
    r"|(?i:(?<![a-z])(?:act|certificate)\s+(?:was|is|has\s+been)\s+(?:signed|executed|drawn\s+up)"
    r"(?:\s+(?:by\s+the\s+parties\s+)?on)?(?![a-z])))(?![ა-ჰ])"
)
#: Where a date may follow the words that date the act, further off than their sentence: the
#: signing and the labels, never the verbs of drawing up.
_ACT_DATE_KEYS = re.compile(rf"გაფორმდა|{_ACT_DATE_LABEL}")
#: The words that date the act, right before a date: "აქტი გაფორმდა 30.11.2026".
_ACT_DATED_BEFORE = re.compile(rf"(?:{_ACT_VERB}|{_ACT_DATE_LABEL})[\s:,]*$")
#: The act's own signing, "აქტი გაფორმდა <date>" / "გაფორმების თარიღი: <date>", and the other
#: ways an act says when it was drawn up or signed.
_ACT_SIGNED = re.compile(rf"გაფორმდა|{_ACT_DATE_LABEL}|{_ACT_VERB}")
#: The signing verb, and a word the length of a slip of it (``_slipped_signing``).
_SIGNING_VERB = "გაფორმდა"
_SIGNING_VERB_LIKE = re.compile(
    r"(?<![ა-ჰa-zA-Z\u0430-\u044f\u0451\u0410-\u042f\u0401])[ა-ჰa-zA-Z\u0430-\u044f\u0451\u0410-\u042f\u0401]{7,11}(?![ა-ჰa-zA-Z\u0430-\u044f\u0451\u0410-\u042f\u0401])"
)
#: A Latin or Cyrillic letter typed into a Georgian word ("გაფოeრმდა"): the stray-letter rule's.
_FOREIGN_LETTER = re.compile(r"[a-zA-Z\u0430-\u044f\u0451\u0410-\u042f\u0401]")
#: The act named right before its signing verb: "აქტი ", "„აქტი“ ".
_ACT_NAMED_BEFORE = re.compile(r"აქტი[\s„“”\"']*$")
#: An advance or prepayment clause: a date there is a payment's, never the act's.
_ADVANCE_WORDS = re.compile(r"ავანს|წინასწარ|advance|prepay", re.IGNORECASE)
#: What opens the service period, its dates within ``_date_hits_near``'s reach after it, past
#: the end of its sentence: "შესრულდა …", a "შესრულების თარიღი" heading, and the other ways acts
#: say it - "გაწეულია 01.11.2026 - 30.11.2026 პერიოდში" (the services rendered), a "საანგარიშო
#: პერიოდი" (reporting period) heading, the period of rendering or of delivery ("მომსახურების
#: გაწევის პერიოდი:", "მიწოდების პერიოდი:", red team R4).
_PERIOD_KEYS = (
    "შესრულდა",
    "შესრულების თარიღი",
    "შესრულების პერიოდი",
    "გაწეულია",
    "გაიწია",
    "საანგარიშო პერიოდ",
    "მომსახურების პერიოდ",
    "გაწევის პერიოდ",
    "მიწოდების პერიოდ",
    # the month the act reports on, as a label: "საანგარიშო თვე: 2026 წლის ივნისი" (red team R5,
    # AOA-R5-07)
    "საანგარიშო თვ",
    "ანგარიშგების თვ",
)
#: ... and a bare "პერიოდი:" label, opening its line after a clause number: "2. პერიოდი:
#: 01.06.2026 - 30.06.2026". A period another word qualifies ("საგარანტიო პერიოდი:",
#: "ხელშეკრულების მოქმედების პერიოდი:") is not the work's.
_BARE_PERIOD_LABEL = re.compile(r"(?<![ა-ჰ])პერიოდი\s*[:\u2013\u2014-]")
_LINE_LEAD = re.compile(r"[\s\d.)]*")
#: The work done, in the past - the aorist and the perfect, active and passive, of the roots
#: that say it (perform, render, carry out, conduct, deliver): "შესრულებულია", "გაწეულ იქნა",
#: "განხორციელდა", "ჩატარდა", "მიეწოდა", "„შემსრულებელმა“ … გასწია". The future and the
#: subjunctive ("შესრულდება", "შეასრულოს") are the contract's window, never the act's period
#: (``_CONTRACT_WINDOW``). A date after one of these is the period's only in its sentence:
#: "მომსახურება შესრულებულია სრულად." dates nothing after it (red team R4, AOA-R4-03).
_PERFORMED = re.compile(
    r"(?<![ა-ჰ])(?:(?:შესრულებულ|გაწეულ|განხორციელებულ|ჩატარებულ|მიწოდებულ)(?:ია|\s+იქნა)"
    r"|შესრულდა|გაიწია|განხორციელდა|ჩატარდა|მიეწოდა"
    r"|შეასრულა|გასწია|განახორციელა|ჩაატარა|მიაწოდა)(?![ა-ჰ])"
)
#: The work as a duty still to be done: a recital restating the contract's window ("…
#: ვალდებულია გასწიოს მომსახურება 2026 წლის 1 მარტიდან …") states no period of the act's.
_CONTRACT_WINDOW = re.compile(
    r"(?<![ა-ჰ])(?:შეასრულოს|გასწიოს|მიაწოდოს|განახორციელოს|ჩაატაროს|შეასრულებს|გასწევს"
    r"|გაიწევა|გაეწევა|მიეწოდება|შესრულდება|განხორციელდება|ჩატარდება|დაიწყება|დასრულდება)"
    r"(?![ა-ჰ])"
)
#: What an act says a service period of, where no verb or label opens it: the work done,
#: rendered, carried out, or a period named as one (``AoaChecker._period_ranges``).
_SERVICE_WORDS = re.compile(
    r"შესრულ|გაწე|გაიწ|განხორციელ|ჩატარ|პერიოდ|მომსახურებ|სამუშაო|მიწოდ|"
    r"(?<![a-z])(?:perform|render|provid|period)",
    re.IGNORECASE,
)
#: A range's end written without its year after a dash, the start's year understood: "2026
#: წლის 1 ივნისიდან - 30 ივნისის ჩათვლით", "... - 30 ივნისამდე".
_YEARLESS_END = re.compile(
    r"\s*[-\u2013\u2014]?\s*(?P<d>\d{1,2})\s*(?P<m>[ა-ჰ]{3,14})(?P<mark>\s*\(?\s*ჩათვლით)?"
)
#: A period stated as a month: "2026 წლის ივნისის თვეში", "2026 წ. ივნისის თვის განმავლობაში".
_MONTH_PERIOD = re.compile(r"(?<!\d)(?P<y>\d{4})\s*წ(?:ლის|\.)?\s*(?P<m>[ა-ჰ]{3,14})\s+თვ[ეი]")
#: How far after the words that open the period its dates may stand (``_date_hits_near``).
_PERIOD_REACH = 160
#: How surely a range no verb and no service word stands beside is the period
#: (``AoaChecker._period_ranges``): only when it is the act's one range.
_BARE_RANGE = 2


@lru_cache(maxsize=8)
def _period_opens(text: str) -> tuple[tuple[int, int, bool], ...]:
    """Where each of the words that open the service period starts and ends, and whether a date
    after it must stand in its sentence - a verb of work done (``_PERFORMED``) - or may stand
    within the keys' reach - a key or a label (``_PERIOD_KEYS``, ``_BARE_PERIOD_LABEL``)."""
    opens = [
        (m.start(), m.end(), False) for k in _PERIOD_KEYS for m in re.finditer(re.escape(k), text)
    ]
    opens += [
        (m.start(), m.end(), False)
        for m in _BARE_PERIOD_LABEL.finditer(text)
        if _LINE_LEAD.fullmatch(text, text.rfind("\n", 0, m.start()) + 1, m.start())
    ]
    opens += [(m.start(), m.end(), True) for m in _PERFORMED.finditer(text)]
    return tuple(sorted(opens))


def _period_key_starts(text: str) -> list[int]:
    """Where the words that open the service period start (``_period_opens``)."""
    return [a for a, _, _ in _period_opens(text)]


#: The window a service period is held to (``_term_window``), as the messages name it: (Georgian,
#: in the dative "სცდება" takes; English).
_WINDOW_NAME: dict[str, tuple[str, str]] = {
    "performance": ("ხელშეკრულების შესრულების ვადას", "the contract's performance period"),
    "validity": ("ხელშეკრულების მოქმედების ვადას", "the contract term"),
}
#: A payment method as the messages name it: (Georgian, English).
_METHOD_NAME: dict[PaymentMethod, tuple[str, str]] = {
    "non_cash": ("უნაღდო ანგარიშსწორება", "non-cash (a transfer to an account)"),
    "cash": ("ნაღდი ანგარიშსწორება", "cash"),
}
#: Which days a payment deadline counts, as the Georgian messages name them.
_DAYS_KA = {"working": "სამუშაო", "calendar": "კალენდარული"}
#: The parts of the payment terms, and the document that alone states one, as a skip names them.
_PAYMENT_PART: dict[str, tuple[str, str]] = {
    "deadline": ("გადახდის ვადა", "the payment deadline"),
    "method": ("ანგარიშსწორების ფორმა", "the payment method"),
    "advance": ("ავანსი", "the advance"),
}
_ONLY_IN: dict[bool, tuple[str, str]] = {
    True: ("მხოლოდ ხელშეკრულებაში", "contract only"),
    False: ("მხოლოდ აქტში", "act only"),
}


@dataclass
class DocumentView:
    """Everything a check needs from one document, read once."""

    doc_id: str
    text: str
    parties: list[Party] = field(default_factory=list)
    #: Each line normalised on its own. ``text`` is flattened to one line, which erases the
    #: line breaks and the "- " bullets a list of services is written with; spans still index
    #: ``text``. Empty lines are left out. After ``parties``, so a positional build still works.
    lines: tuple[str, ...] = ()
    #: ``text`` with the letters as the document writes them: a Cyrillic "o" that ``text``
    #: folds to Latin stays Cyrillic here. The same length, so a span of ``text`` is a span of
    #: this; empty on a view built by hand.
    written: str = ""
    #: The text came from a scan (``build``): a letter in it may be a misreading.
    ocr: bool = False
    #: For each offset into ``text``, and for its end, the offset into ``written``, where the
    #: fold is not one character for one (two commas typed for a quote); None when it is.
    _to_written: list[int] | None = field(default=None, init=False, repr=False, compare=False)
    _hits: list[AnchorHit] | None = field(default=None, init=False, repr=False, compare=False)
    _sentences: list[tuple[int, int]] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _amounts: list[RoledAmount] | None = field(default=None, init=False, repr=False, compare=False)
    _term: ContractTerm | None = field(default=None, init=False, repr=False, compare=False)
    _payment: PaymentTerms | None = field(default=None, init=False, repr=False, compare=False)
    _advance: dict[bool, AdvanceTerms] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    @classmethod
    def build(cls, doc_id: str, text: str, *, ocr: bool = False) -> DocumentView:
        """``ocr`` says the text came from a scan: its IBANs and bank codes are put back in shape
        (``repair_codes``, which keeps the length), and ``written`` keeps what the scan read."""
        fix = repair_codes if ocr else (lambda t: t)
        lines = tuple(
            line for line in (fix(normalize_confusables(x)) for x in text.split("\n")) if line
        )
        written = normalize_layout(text)
        folded, to_written = fold_comma_quotes_mapped(written)
        text = fold_look_alikes(fix(folded))
        # one code under two names stays two parties here: that is AOA.ID_CODE_PARTY's evidence
        parties = find_parties(text, split_conflicts=True)
        view = cls(doc_id=doc_id, text=text, parties=parties, lines=lines, written=written, ocr=ocr)
        if len(folded) != len(written):
            view._to_written = to_written
        return view

    def as_written(self, start: int, end: int) -> str:
        """The text at a span of ``text``, in the letters the document writes."""
        back = self._to_written
        if back is not None and len(back) == len(self.text) + 1 and 0 <= start <= end < len(back):
            return self.written[back[start] : back[end]]
        source = self.written if len(self.written) == len(self.text) else self.text
        return source[start:end]

    def hits(self) -> list[AnchorHit]:
        """Every anchor in the document, computed once: the rules ask for them a dozen times."""
        if self._hits is None:
            self._hits = find_anchors(self.text)
        return self._hits

    def sentences(self) -> list[tuple[int, int]]:
        """The text's sentences (``contract_terms.sentence_spans``), computed once."""
        if self._sentences is None:
            self._sentences = sentence_spans(self.text)
        return self._sentences

    def amounts(self) -> list[RoledAmount]:
        """Every amount and the role it plays (``contract_terms.amount_roles``), computed once:
        the fee rule and the tax wording rule both read them."""
        if self._amounts is None:
            self._amounts = amount_roles(self.text, sentences=self.sentences())
        return self._amounts

    def term(self) -> ContractTerm:
        """The signing date, the validity and the performance window (``read_term``), read once."""
        if self._term is None:
            self._term = read_term(self.text, sentences=self.sentences())
        return self._term

    def payment_terms(self) -> PaymentTerms:
        """The payment deadline and method (``read_payment_terms``), read once."""
        if self._payment is None:
            self._payment = read_payment_terms(self.text, sentences=self.sentences())
        return self._payment

    def advance(self, *, in_act: bool) -> AdvanceTerms:
        """What the document says about an advance (``read_advance``), read once: the payment
        terms and the invoice rule both ask. ``in_act`` dates each amount by the payment."""
        if in_act not in self._advance:
            self._advance[in_act] = read_advance(
                self.text, in_act=in_act, sentences=self.sentences(), amounts=self.amounts()
            )
        return self._advance[in_act]

    def anchors(self, ft: FactType) -> list[str]:
        return [h.value for h in self.hits() if h.type == ft]

    def anchor_hits(self, ft: FactType) -> list[AnchorHit]:
        return [h for h in self.hits() if h.type == ft]

    def bank(self, unit: str) -> set[str]:
        return {h.value for h in self.bank_hits(unit)}

    def bank_hits(self, unit: str) -> list[AnchorHit]:
        return [h for h in self.hits() if h.type in _BANK_TYPES and h.unit == unit]

    def id_codes(self) -> set[str]:
        return {p.id_code for p in self.parties if p.id_code}


#: How far into a document its heading - and a date written in it - can reach.
_HEADER_CHARS = 200
#: A signature block's line for an attorney: its title, then the name alone on the line
#: ("მინდობილი პირი: მზევინარ გოგიჩაიშვილი", "მინდობილობით მოქმედი პირი მზევინარ
#: გოგიჩაიშვილი ____").
_SIGNING_PROXY = re.compile(
    r"^\s*(?:მინდობილი\s+პირი|მინდობილობით\s+მოქმედი\s+პირი"
    r"|წარმომადგენელი\s*\(\s*მინდობილობით\s*\)|რწმუნებული)"
    r"\s*[:\-\u2013]?\s*(?P<name>[ა-ჰ]+(?:\s+[ა-ჰ]+){1,2})[\s_./]*$"
)
_CONTRACT_WORD = re.compile(r"ხელშეკრულებ|contract", re.IGNORECASE)
_ACT_WORD = re.compile(r"აქტ|\bact\b", re.IGNORECASE)


#: every label the party reader knows, read by the one shared pattern (red team R2, AOA-R2-01),
#: a slip's wrong length included (R3, AOA-R3-01)
_ID_LABELLED = ID_CODE_WRITTEN_RE


#: The share of an act's letters that must be Georgian for the rules to read it. A Georgian act
#: is nearly all Georgian (the client's are over 80%: bank codes and IBANs are the Latin); an act
#: in two languages, each clause in both, is about half, and the rules read its Georgian half;
#: an act in English, with a Georgian name or address in it, is far below this.
_GEORGIAN_SHARE = 0.2


def _reads_georgian(text: str) -> bool:
    """Whether ``text`` is written in Georgian, alone or beside another language: a fair share
    of its letters are Georgian (``_GEORGIAN_SHARE``). The rules read Georgian labels; an act in
    English has none of them to read."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return True  # nothing to judge by: the rules report what they find, as they always did
    georgian = sum(1 for c in letters if "\u10a0" <= c <= "\u10ff" or "\u1c90" <= c <= "\u1cbf")
    return georgian >= _GEORGIAN_SHARE * len(letters)


def _labelled_codes(text: str) -> set[str]:
    """Identification codes read straight off their labels, for a document whose parties the
    party parser could not make out (a name without quotes, say): the codes are still there."""
    return {id_code_digits(m.group(1)) for m in _ID_LABELLED.finditer(text)}


def _contract_is_subject(text: str, pos: int) -> bool:
    """Whether the signing named at ``pos`` is the contract's: "ხელშეკრულება გაფორმდა",
    "ხელშეკრულების გაფორმების თარიღი" - the contract named just before it, and no act after."""
    before = text[max(0, pos - 40) : pos]
    contract = [m.end() for m in _CONTRACT_WORD.finditer(before)]
    if not contract:
        return False
    return _ACT_WORD.search(before, contract[-1]) is None


_Q = "\u201e\u201c\u201d\"'"
#: A contract's number as a reference writes it: "N 27/2026", "№27/2026", "# 15", "No. 15"
#: (``rules._NUMBER_MARKER`` and ``rules._REFERENCE``, the contract-number reader's own).
_CONTRACT_NUMBER = rf"(?:{_NUMBER_MARKER})\s*{_REFERENCE}"
#: A date that names the contract it belongs to, the contract right after it: "2026 წლის 1
#: მარტის ხელშეკრულების ...", "2026 წლის 9 მარტის № 318 ხელშეკრულების საფუძველზე" (its number
#: in every form the number reader reads, red team R4: COMPARE-R4-01), "მხარეებმა 01.09.2026
#: გააფორმეს ხელშეკრულება", "15.05.2026 წლის ხელშეკრულება". A comma only before a signing verb:
#: "... 30 ივნისის ჩათვლით, ხელშეკრულების პირობების შესაბამისად" is the period's end. A numeric
#: date takes its case on a hyphen, the genitive or the instrumental ("15.05.2026-ის
#: ხელშეკრულება", "-ისა", "-ით", red team R5: AOA-R5-01); "-დან", "-მდე" and "-ის ჩათვლით" are a
#: period's first or last day, and a contract word after them names nothing.
_CONTRACT_REFERENCE = re.compile(
    r"(?:-(?:ისა?|ით)(?![ა-ჰ]))?"
    rf"(?:[\s,]*(?:გააფორმეს|გაფორმებული|დადებული|დადეს))?[\s{_Q}]*"
    rf"(?:{_CONTRACT_NUMBER}\s*|N\s*\S+\s*)?ხელშეკრულებ"
)
#: ... or the contract, then its signing: "ხელშეკრულება გაფორმდა 2026 წლის 1 მარტს",
#: "ხელშეკრულების თარიღი: 01.03.2026", "N 27/2026 ხელშეკრულება, დადებული 2026 წლის 3 მარტს",
#: "ხელშეკრულება № 27, გაფორმებული ...", "ხელშეკრულების (თარიღი: 03.03.2026)". The signing
#: words stand right after the contract and its number: "ხელშეკრულების შესაბამისად, 2026 წლის
#: 10 ივნისს ..." dates something else.
_CONTRACT_DATED_BEFORE = re.compile(
    rf"ხელშეკრულებ[ა-ჰ]*[{_Q}]*(?:\s*{_CONTRACT_NUMBER})?[\s,]*\(?\s*"
    r"(?:გაფორმდა|დაიდო|დადებული|გაფორმებული|(?:გაფორმების\s+)?თარიღია?)[\s:,-]*$"
)
#: How far before a date the contract and its signing words may stand.
_CONTRACT_DATED_REACH = 60


def contract_named_by(text: str, hit: RuleHit) -> int | None:
    """Where the contract word stands that the date ``hit`` names as the contract's own date -
    the contract right after it (``_CONTRACT_REFERENCE``) or named with its signing right before
    it (``_CONTRACT_DATED_BEFORE``) - or None when the date is not the contract's. One reader of
    "this date is the contract's" for the act's date, its period and the contract date the
    extraction states (red team R4)."""
    after = _CONTRACT_REFERENCE.match(text, hit.end)
    if after is not None:
        return after.end() - len("ხელშეკრულებ")
    reach = max(0, hit.start - _CONTRACT_DATED_REACH)
    before = _CONTRACT_DATED_BEFORE.search(text, reach, hit.start)
    return before.start() if before is not None else None


def _date_hit(d: date, raw: str, start: int, end: int) -> RuleHit:
    """A date the period reader read past ``find_dates`` - a month, a range's yearless end - as
    a hit like the reader's own."""
    return RuleHit(
        type=FactType.date,
        value=d.isoformat(),
        raw_text=raw,
        start=start,
        end=end,
        normalized=d.isoformat(),
    )


def _yearless_end(text: str, first: RuleHit, limit: int) -> RuleHit | None:
    """The last day of a range whose end is written without a year after a dash, right after
    its first day, the start's year understood ("2026 წლის 1 ივნისიდან - 30 ივნისის ჩათვლით"):
    marked as an end ("ჩათვლით", "-მდე"), and not before the first day."""
    m = _YEARLESS_END.match(text, first.end, limit)
    if m is None:
        return None
    word = m.group("m")
    month = _month_ka(word)
    if month is None or not (m.group("mark") or word.endswith("მდე")):
        return None
    start = date.fromisoformat(str(first.normalized))
    for year in (start.year, start.year + 1):
        try:
            end = date(year, month, int(m.group("d")))
        except ValueError:
            return None
        if end >= start:
            return _date_hit(end, text[m.start("d") : m.end("m")], m.start("d"), m.end("m"))
    return None


def _heading_date(text: str, hits: list[RuleHit]) -> tuple[date, RuleHit] | None:
    """An act dated only in its heading ("ქ. თბილისი, 31.08.2026"): the first date that opens
    the document and is not the contract's.

    A date that names the contract (``contract_named_by``) - a title citing it, "მიღება-ჩაბარების
    აქტი (2026 წლის 15 მაისის N 12/2026 ხელშეკრულებაზე)" - is passed over, and the contract word it
    names is its own: it no longer makes the heading date under it the contract's (red team R4,
    AOA-R4-05). Any other contract word near a date leaves whose date it is unsaid, and no date
    is taken. A date after one passed over is the act's only before the preamble ends
    (``anchors.preamble_end``): further on it is a clause's - the period's first day, say."""
    claimed: set[int] = set()
    passed_over = False
    for h in hits:
        if h.start >= _HEADER_CHARS:
            return None
        named = contract_named_by(text, h)
        if named is not None:
            claimed.add(named)
            passed_over = True
            continue
        near = [
            m.start()
            for m in _CONTRACT_WORD.finditer(text, max(0, h.start - 60), h.end + 30)
            if m.start() not in claimed
        ]
        if near:
            return None
        if passed_over:
            end = preamble_end(text)
            if end is None or h.end > end:
                return None
        return date.fromisoformat(str(h.normalized)), h
    return None


def _date_hits_near(
    text: str,
    keys: tuple[str, ...] | re.Pattern[str] | Callable[[str], list[int]],
    window: int = 160,
    *,
    skip: Callable[[RuleHit], bool] | None = None,
) -> list[tuple[date, RuleHit]]:
    """Dates that follow one of the given keywords (or a match of a pattern), in document order,
    with where each was read,
    each date once. ``skip`` drops a hit before that: a date set aside must not take the place
    of the same date written where it counts - an act dated the day its period ends, read first
    under the period's heading and dropped as the act's, took the period's end with it.

    Anchoring on the section wording is far more reliable than the generic keyword-proximity
    classifier, because these documents put each date under its own numbered heading.
    """
    hits = find_dates(text)
    if not hits:
        return []
    starts = _key_starts(text, keys)
    out: list[tuple[date, RuleHit]] = []
    for h in hits:
        if any(s <= h.start <= s + window for s in starts):
            if skip is not None and skip(h):
                continue
            d = date.fromisoformat(str(h.normalized))
            if all(d != seen for seen, _ in out):
                out.append((d, h))
    return out


def _key_starts(
    text: str, keys: tuple[str, ...] | re.Pattern[str] | Callable[[str], list[int]]
) -> list[int]:
    """Where each keyword (or each match of a pattern, or each start a reader finds) starts in
    ``text``."""
    if isinstance(keys, re.Pattern):
        return [m.start() for m in keys.finditer(text)]
    if callable(keys):
        return keys(text)
    return [m.start() for k in keys for m in re.finditer(re.escape(k), text)]


@lru_cache(maxsize=8)
def _slipped_signing(text: str) -> tuple[tuple[int, int], ...]:
    """The signing verb written with a one-letter slip, after the act's name: "„აქტი“ გაორმდა
    2026 წლის 30 სექტემბერი" (red team R3). The prose spelling rule reports the slip; the date
    after it is still the act's. Only after "აქტი": "ხელშეკრულება გაორმდა" is the contract's. A
    Latin or Cyrillic letter typed into the verb is read past ("გაფოeრმდა"): the stray-letter
    rule reports it (mutation harness, seed 223)."""
    out = []
    for m in _SIGNING_VERB_LIKE.finditer(text):
        word = _FOREIGN_LETTER.sub("", m.group())
        if len(m.group()) - len(word) > 2:
            continue  # a word of another script, not a stray letter in this one
        if m.group() == _SIGNING_VERB or damerau(word, _SIGNING_VERB, cap=1) > 1:
            continue
        if _ACT_NAMED_BEFORE.search(text, max(0, m.start() - 12), m.start()):
            out.append(m.span())
    return tuple(out)


def _act_date_key_starts(text: str) -> list[int]:
    """Where a date may follow the words that date the act (``_ACT_DATE_KEYS``), the signing
    verb with a slip in it too (``_slipped_signing``)."""
    starts = [m.start() for m in _ACT_DATE_KEYS.finditer(text)]
    return sorted(starts + [a for a, _ in _slipped_signing(text)])


def _act_dated_right_before(text: str, pos: int) -> bool:
    """Whether the words that date the act stand right before ``pos`` (``_ACT_DATED_BEFORE``),
    the signing verb with a slip in it too."""
    if _ACT_DATED_BEFORE.search(text, max(0, pos - 30), pos) is not None:
        return True
    return any(
        b <= pos and pos - b <= 30 and not re.sub(r"[\s:,]", "", text[b:pos])
        for _, b in _slipped_signing(text)
    )


#: A month and its year with no day before the month: "2026 წლის ივნისის (თვეში)", "2026 წ.
#: ივნისი", and the other order, "ივნისი 2026", "ივნისი, 2026 წ.", the month's own noun between
#: them as a date's postposition may stand there ("ივნისის თვეში, 2026 წ.", "ივნისის თვის
#: განმავლობაში, 2026 წელი", red team R5: AOA-R5-07). The month is any of its forms
#: (``MONTH_FORMS``), which a pattern of letters leaves to be looked up.
_YEAR_MONTH = re.compile(
    rf"(?<![\d.])(?P<y>(?:19|20)\d\d)\s*(?:წლის|წ\.)\s*(?P<m>[{GEORGIAN}]+)(?![{GEORGIAN}])"
)
_MONTH_YEAR = re.compile(
    rf"(?<![{GEORGIAN}\d])(?P<m>[{GEORGIAN}]+)"
    r"(?:\s+თვ(?:ე|ის|ეში)(?:\s+განმავლობაში)?)?"
    r"\s*,?\s+(?P<y>(?:19|20)\d\d)(?!\d|\.\d)"
    rf"(?:\s*(?:წლის|წელი|წ\.)(?![{GEORGIAN}]))?"
)


def _month_near(
    text: str,
    keys: tuple[str, ...] | re.Pattern[str] | Callable[[str], list[int]],
    window: int = 160,
) -> list[tuple[date, RuleHit]]:
    """The first month named with its year and no day after one of ``keys``, as the whole month:
    its first and its last day, each read at the year and the month as written. A month that is
    part of a date ("30 ივნისი 2026") is the date's, and not read here."""
    starts = _key_starts(text, keys)
    dated = [(h.start, h.end) for h in find_dates(text)]
    found: list[tuple[int, int, int, int]] = []  # (start, end, year, month)
    for pattern in (_YEAR_MONTH, _MONTH_YEAR):
        for m in pattern.finditer(text):
            form = MONTH_FORMS.get(m.group("m"))
            if form is None or not any(s <= m.start() <= s + window for s in starts):
                continue
            end = m.end("m") if pattern is _YEAR_MONTH else m.end()
            if any(a < end and m.start() < b for a, b in dated):
                continue
            found.append((m.start(), end, int(m.group("y")), form[0]))
    if not found:
        return []
    start, end, year, month = min(found)
    first = date(year, month, 1)
    raw = text[start:end]
    return [
        (
            d,
            RuleHit(
                type=FactType.date,
                value=d.isoformat(),
                raw_text=raw,
                start=start,
                end=end,
                normalized=d.isoformat(),
            ),
        )
        for d in (first, _last_day(first))
    ]


def _next_word(text: str, pos: int) -> str:
    """The word after ``pos``, for a correction whose case the next word decides."""
    m = re.match(r"[\s,]*([ა-ჰ]+)", text[pos : pos + 40])
    return m.group(1) if m else ""


def _month_typos(text: str) -> list[tuple[str, int, int]]:
    return [
        (h.note or "", h.start, h.end) for h in find_dates(text) if h.note and "misspell" in h.note
    ]


def _finding_key(f: Finding) -> tuple[str, str, str, str | None, str | None]:
    """What makes two findings the same mistake: everything but where the text says it."""
    return (f.rule_id, f.severity, f.message_en, f.expected, f.actual)


def _last_day(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def add_months(start: date, months: int) -> date:
    """Month arithmetic that survives month ends (31 Jan + 1 month is 28/29 Feb, not a crash)."""
    total = start.month - 1 + months
    year, month = start.year + total // 12, total % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


@dataclass(frozen=True)
class _Period:
    """The act's service period: its dates as the act writes them, and as they are meant.

    They differ only when the period ends before it starts - a period across the New Year with
    one year not moved on ("2026 წლის 15 დეკემბრიდან 2026 წლის 14 იანვრის") - and one year
    repair orders it (``_repair_year``). Then ``repaired`` is the endpoint put right and the
    years it moved."""

    hits: list[tuple[date, RuleHit]]
    dates: list[date]
    repaired: tuple[RuleHit, int] | None = None


@dataclass(frozen=True, eq=False)
class _PartyMatch:
    """An act party, the contract party it is, and - when the act gives it another contract
    party's identification code - that other party (AOA.ID_CODE_PARTY). Compared by identity:
    a ``Party`` is mutable, so it has no value to compare."""

    party: Party
    match: Party | None
    code_of: Party | None = None


class AoaChecker:
    def __init__(
        self,
        *,
        aoa: DocumentView,
        contract: DocumentView | None = None,
        registry: object | None = None,
        contractor_type: ContractorType | None = None,
        rates: RateProvider | None = None,
        advance: AdvanceInput | None = None,
        review: ScopeReviewSet | None = None,
        ocr: bool = False,
        speller: Lexicon | SpellingUnavailable | None = None,
    ) -> None:
        self.aoa = aoa
        self.contract = contract
        #: either document was read from a scan: a difference scanning explains is reported as a
        #: misreading to check against the paper, not as a mistake (``_misread``)
        self.ocr = ocr
        #: the dictionary for the prose spelling rule (``aoa_spelling.open_speller``), or why
        #: there is none; None skips that rule with a note
        self.speller = speller
        self.registry = registry
        self.contractor_type = contractor_type
        #: the NBG rates for a foreign-currency act's invoice (``rule_fx_invoice``)
        self.rates: RateProvider = rates if rates is not None else NullRateProvider()
        #: an advance the user entered; what it leaves blank is read from the act
        self.advance = advance
        #: a stored model review of the notes the scope rules could not decide (``aoa_llm``);
        #: None checks with the rules alone
        self.review = review
        self.findings: list[Finding] = []
        self.skipped: list[SkipNote] = []
        #: the invoice ``rule_fx_invoice`` worked out, and its total in lari, for the summary
        self._fx: tuple[InvoicePlan, Decimal | None, tuple[PricedTerm, ...]] | None = None
        #: which contract party each act party is (``_matched_parties``), worked out once a run
        self._party_matches: list[_PartyMatch] | None = None
        #: both documents' subjects and services, aligned (``_scope_reading``), once a run
        self._scope: ScopeReading | None = None
        #: ``review`` checked against this run's notes (``_review_outcome``), once a run
        self._review: ReviewOutcome | None = None
        #: the service period (``_period``), read once a run
        self._period_reading: _Period | None = None

    # --- plumbing ------------------------------------------------------------------------

    def _add(
        self,
        rule_id: str,
        severity: Severity,
        ka: str,
        en: str,
        *,
        expected: str | None = None,
        actual: str | None = None,
        span: tuple[int, int] | None = None,
        quote: str | None = None,
        confidence: float = 1.0,
        needs_review: bool = False,
        reviewed_by: Literal["llm"] | None = None,
        places: tuple[tuple[int, ...], int] | None = None,
    ) -> None:
        if severity == "error" and self._misread(rule_id, actual, expected):
            severity, needs_review = "warning", True
            ka, en = _MISREAD_KA + ka, _MISREAD_EN + en
        # One finding per mistake. The same text can be read twice - a clause repeated in an
        # annex, a table cell the parser emits in two places - and each copy sits at another
        # offset, so the key leaves the span out: with it, no copy would ever match.
        if quote is None and span is not None:
            quote = self.aoa.as_written(*span) or None
        if severity == "error" and expected and quote and expected.split() == quote.split():
            # a correction that is the act's own words corrects nothing: none is better
            expected = None
        key = (rule_id, severity, en, expected, actual)
        if any(_finding_key(f) == key for f in self.findings):
            return
        self.findings.append(
            Finding(
                rule_id=rule_id,
                severity=severity,
                message_ka=ka,
                message_en=en,
                expected=expected,
                actual=actual,
                span=span,
                quote=quote,
                places=places,
                confidence=confidence,
                needs_review=needs_review,
                reviewed_by=reviewed_by,
            )
        )

    def _misread(self, rule_id: str, actual: str | None, expected: str | None) -> bool:
        """On a scanned document, whether a value's difference from the contract's is one that
        scanning makes (``ocr.misread_explains``) - for a name, in any of its case forms, since a
        misread act may inflect it differently from the correction it was given."""
        if not (self.ocr and actual and expected and rule_id in _OCR_COMPARED):
            return False
        if misread_explains(actual, expected):
            return True
        if rule_id not in _NAME_VALUED:
            return False
        meant = lemma(expected)
        return any(misread_explains(actual, inflect(meant, case)) for case in CASES)

    def _unread(self, rule_id: str, what_ka: str, what_en: str) -> bool:
        """For a rule that found nothing: when the act is not written in Georgian, say that the
        rule could not read it (a skip note) and return True. The rules read Georgian labels and
        wording ("ს/კ", "შესრულდა", "პროექტ"); an act in English has none of them, and nothing
        found there is not something missing from it."""
        if _reads_georgian(self.aoa.text):
            return False
        self._skip(
            rule_id,
            f"აქტი ქართულად არ არის დაწერილი, {what_ka} კი მხოლოდ ქართული ტექსტიდან იკითხება: "
            "არ შემოწმდა",
            f"the act is not written in Georgian, and {what_en} can be read only from Georgian "
            "text: not checked",
        )
        return True

    def _skip(self, rule_id: str, why_ka: str, why_en: str) -> None:
        note = SkipNote(rule_id=rule_id, reason_ka=why_ka, reason_en=why_en)
        if note not in self.skipped:  # one party with two unlisted directors is one gap
            self.skipped.append(note)

    def _find_span(self, needle: str) -> tuple[int, int] | None:
        i = self.aoa.text.find(needle)
        return (i, i + len(needle)) if i >= 0 else None

    def _code_span(self, code: str) -> tuple[int, int] | None:
        """Where the act writes an identification code, as printed: a code in groups ("123 456
        789") is read compact, and only the party reader's span, or its digits found with the
        group spaces between them, lead back to the characters the act has. Never the same
        digits inside a longer number."""
        for p in self.aoa.parties:
            span = p.id_span
            if (
                p.id_code == code
                and span is not None
                and 0 <= span[0] < span[1]
                and re.sub(r"\D", "", self.aoa.text[span[0] : span[1]]) == code
            ):
                return span
        grouped = r"[ \u00a0.\-]?".join(code)
        m = re.search(
            rf"(?<!\d)(?<!\d[ \u00a0.\-]){grouped}(?!\d)(?![ \u00a0.\-]\d)", self.aoa.text
        )
        return m.span() if m is not None else None

    def _account_span(self, iban: str) -> tuple[int, int] | None:
        """Where the act writes an account: an IBAN printed in groups or broken over a line is
        read compact, and only the reader's span finds the characters the document has."""
        hit = next((h for h in self.aoa.bank_hits("IBAN") if h.value == iban), None)
        return (hit.start, hit.end) if hit is not None else self._find_span(iban)

    def _act_date(self) -> tuple[date, RuleHit] | None:
        """The act's own date, and where it is written. Every rule that needs it reads it here,
        so no two of them can cite different dates.

        The date right after "გაფორმდა" / "გაფორმების თარიღი" in the same sentence; failing
        that, the first date after either keyword as ``_date_hits_near`` reads it. A date in an
        advance clause is a payment's, never the act's: "... აქტის გაფორმების თარიღიდან 5 დღეში;
        ავანსი გადახდილია 2026 წლის 10 სექტემბერს" must not date the act 10 September.

        Every reader passes over a date that names the contract (``contract_named_by``) and
        takes the next one: "აქტი შედგენილია 2026 წლის 15 მაისის ხელშეკრულების საფუძველზე" is
        dated by its heading, "აქტი შედგა 2026 წლის 15 მაისის ხელშეკრულების საფუძველზე, 2026
        წლის 30 ივნისს" on 30 June (red team R5, AOA-R5-01).
        """
        text = self.aoa.text
        hits = [h for h in find_dates(text) if contract_named_by(text, h) is None]
        # the words that date the act, a slip in the signing verb allowed (red team R3)
        signed = sorted([m.span() for m in _ACT_SIGNED.finditer(text)] + [*_slipped_signing(text)])
        for key_start, key_end in signed:
            if _contract_is_subject(text, key_start):
                continue  # "ხელშეკრულება გაფორმდა ..." dates the contract, not the act
            end = self._sentence_of(key_start)[1]
            hit = next((h for h in hits if key_end <= h.start < end), None)
            if hit is not None and not _ADVANCE_WORDS.search(text, key_end, hit.start):
                return date.fromisoformat(str(hit.normalized)), hit
        key_starts = _act_date_key_starts(text)

        def named(h: RuleHit) -> bool:
            return contract_named_by(text, h) is not None

        for d, hit in _date_hits_near(text, _act_date_key_starts, skip=named):
            key_at = max((s for s in key_starts if s <= hit.start), default=hit.start)
            if not self._in_advance_clause(hit.start) and not _contract_is_subject(text, key_at):
                return d, hit
        return _heading_date(text, find_dates(text))

    def _act_date_places(self, hit: RuleHit) -> tuple[tuple[int, ...], int] | None:
        """Which places of the act's date, as written at ``hit``, a finding about it marks
        (``Finding.places``; red team R4, AOA-R4-06): the act's own date - where ``_act_date``
        read it, after the words that date the act, or alone in the heading - and never another
        correct copy of the same date, the recital's contract date or the period's end. None
        when every place is the act's own, or when ``hit`` is not among them."""
        text = self.aoa.text
        spans = whole_word_spans(text, text[hit.start : hit.end])
        at = next((i for i, (a, b) in enumerate(spans) if a < hit.end and hit.start < b), None)
        if at is None:
            return None

        def own(span: tuple[int, int]) -> bool:
            a, b = span
            if _act_dated_right_before(text, a):
                return True
            return a < _HEADER_CHARS and not _CONTRACT_WORD.search(text, max(0, a - 60), b + 30)

        mine = [i for i, span in enumerate(spans) if i == at or own(span)]
        return None if len(mine) == len(spans) else (tuple(mine), len(spans))

    def _period_hits(self) -> list[tuple[date, RuleHit]]:
        """The service period's dates, and where each is written. Every rule that needs the
        period reads it here, and so does the extraction (``document_facts``): one reader.

        Read first after the words that open it (``_period_opens``): a key or a label
        ("შესრულდა", "შესრულების თარიღი", "მომსახურების გაწევის პერიოდი:", "პერიოდი:") within
        ``_date_hits_near``'s reach, a verb of work done in the past ("შესრულებულია", "გაწეულ
        იქნა", "განხორციელდა") in its own sentence; failing dates, a month named after them.
        One date read so is the range it is a day of, when its sentence writes one. Failing all
        of that, the period is known by its form (``_period_ranges``, ``_month_by_form``): GOLD_SPEC
        2 reads an act's service period "by its form whatever the verb" (red team R4, AOA-R4-03).

        A date an advance is named before is a payment's, as for the act's own date
        (``_act_date``): "ავანსი გადახდილია 2026 წლის 10 სექტემბერს" written after the period does
        not make it end on 10 September. Named between the words that open the period and the
        date, or anywhere in a sentence those words are not in ("10 სექტემბერს გადახდილი
        ავანსი ..."). Named after the period's own dates, it takes nothing from them: "... 30
        აპრილის ჩათვლით, ავანსი კი ..." still ends on 30 April. The advance is named as
        ``read_advance`` names it, so "წინასწარ შეთანხმებული გრაფიკით" (a schedule agreed
        beforehand) and "in advance" name none. A date that names the contract
        (``contract_named_by``, "2026 წლის 9 მარტის № 318 ხელშეკრულების") is never the period's.
        """
        text = self.aoa.text
        opens = _period_opens(text)
        ends = [e for _, e, _ in opens]
        act = self._act_date()

        def is_act(h: RuleHit) -> bool:
            """The act's own date is not part of the period, nor is any date written right
            after the words that date the act: a table whose first row the parser took for
            its header repeats "„აქტი“ გაფორმდა 30.11.2026" beside every row, the period's
            among them."""
            if act is not None and h.start == act[1].start:
                return True
            return _act_dated_right_before(text, h.start)

        def opened_for(h: RuleHit) -> bool:
            """Whether words that open the period reach ``h``: a key or a label within the
            keys' reach, a verb of work done only in ``h``'s own sentence."""
            a, _ = self._sentence_of(h.start)
            return any(
                e <= h.start and (not scoped or a <= start)
                for start, e, scoped in opens
                if h.start - start <= _PERIOD_REACH
            )

        def in_period(h: RuleHit) -> bool:
            if contract_named_by(text, h) is not None:
                return False  # "... შესრულდა 2026 წლის 1 მარტის ხელშეკრულების შესაბამისად"
            if not opened_for(h):
                return False
            i = bisect_right(ends, h.start)
            opened = ends[i - 1] if i else None
            a, b = self._sentence_of(h.start)
            if opened is not None and _ADVANCE_NAMED.search(text, opened, h.start):
                return False
            return not ((opened is None or opened < a) and _ADVANCE_NAMED.search(text, a, b))

        out = [
            (d, h)
            for d, h in _date_hits_near(text, _period_key_starts, _PERIOD_REACH, skip=is_act)
            if in_period(h)
        ]
        if len({d for d, _ in out}) >= 2:
            return out
        if not out:
            # "შესრულდა 2026 წლის ივნისის თვეში": a month with no day is the whole month
            month = [(d, h) for d, h in _month_near(text, _period_key_starts) if in_period(h)]
            if month:
                return month[:2]
        ranges = self._period_ranges()
        if out:
            # one date read after the words that open the period: the range it is a day of
            at = out[0][1].start
            around = [r for r in ranges if r[0][1].start <= at < r[1][1].end]
            return list(around[0][:2]) if around else out
        best = min((r[2] for r in ranges), default=None)
        chosen = [r for r in ranges if r[2] == best]
        if best is not None and (best < _BARE_RANGE or len(ranges) == 1):
            return list(chosen[0][:2])
        return self._month_by_form()

    def _period_ranges(
        self,
    ) -> list[tuple[tuple[date, RuleHit], tuple[date, RuleHit], int]]:
        """Every range an act's sentences write, in document order, that may be its service
        period: its first day, its last, and how surely it is the period - a sentence with a
        verb of work done or a period label first (0), then one naming the service (1), then
        any other (``_BARE_RANGE``).

        Not a range in a sentence about an advance, the contract's validity, a guarantee or the
        work still to be done (a recital of the contract's window, ``_CONTRACT_WINDOW``, where
        no verb of work done stands beside it); not a date that dates the act or names the
        contract. Two dates of one sentence written as a range - "... 1 ივნისიდან ... 30 ივნისის
        ჩათვლით", "01.06.2026 - 30.06.2026", "2026 წლის 1-30 ივნისის პერიოდში" - are one."""
        text = self.aoa.text
        act = self._act_date()
        hits = [
            h
            for h in find_dates(text)
            if (act is None or h.start != act[1].start)
            and not _act_dated_right_before(text, h.start)
            and contract_named_by(text, h) is None
        ]
        labels = [a for a, _, scoped in _period_opens(text) if not scoped]
        out: list[tuple[tuple[date, RuleHit], tuple[date, RuleHit], int]] = []
        for a, b in self.aoa.sentences():
            if (
                _ADVANCE_WORDS.search(text, a, b)
                or _VALIDITY.search(text, a, b)
                or _NOT_A_TERM.search(text, a, b)
            ):
                continue
            performed = _PERFORMED.search(text, a, b) is not None
            if _CONTRACT_WINDOW.search(text, a, b) and not performed:
                continue
            inside = [h for h in hits if a <= h.start < b]
            if not inside:
                continue
            found = _range_in(text, (a, b), inside, takes_effect=False)
            start, end = found.start, found.end
            if start is None:
                continue
            first = next(h for h in inside if h.normalized == start.isoformat())
            last = next((h for h in reversed(inside) if h.normalized == str(end)), None)
            if end is None:
                last = _yearless_end(text, first, b)
                end = date.fromisoformat(str(last.normalized)) if last is not None else None
            if end is None or last is None or start > end:
                continue
            if performed or any(a <= k < b for k in labels):
                rank = 0
            elif _SERVICE_WORDS.search(text, a, b):
                rank = 1
            else:
                rank = _BARE_RANGE
            out.append(((start, first), (end, last), rank))
        return out

    def _month_by_form(self) -> list[tuple[date, RuleHit]]:
        """A service period stated as a whole month in a sentence about the service, no word
        opening it: "2026 წლის ივნისის თვეში" - its first day and its last, both at the month."""
        text = self.aoa.text
        for m in _MONTH_PERIOD.finditer(text):
            month = _month_ka(m.group("m"))
            if month is None:
                continue
            a, b = self._sentence_of(m.start())
            if not _SERVICE_WORDS.search(text, a, b) or _ADVANCE_WORDS.search(text, a, b):
                continue
            first = date(int(m.group("y")), month, 1)
            return [
                (d, _date_hit(d, m.group(0), m.start(), m.end())) for d in (first, _last_day(first))
            ]
        return []

    def _period(self) -> _Period:
        """The service period (``_period_hits``), with a year put right when the period ends
        before it starts and exactly one repair orders it (``_repair_year``). Every rule that
        needs the period's days reads them here, once a run."""
        if self._period_reading is None:
            hits = self._period_hits()
            dates = [d for d, _ in hits]
            repaired = None
            if len(hits) >= 2 and hits[-1][0] < hits[0][0]:
                window = _term_window(self.contract.term()) if self.contract is not None else None
                act = self._act_date()
                repaired = _repair_year(hits, window, act[0] if act else None)
                if repaired is not None:
                    hit, shift = repaired
                    dates = [_add_years(d, shift) if h is hit else d for d, h in hits]
            self._period_reading = _Period(hits=hits, dates=dates, repaired=repaired)
        return self._period_reading

    def _sentence_of(self, pos: int) -> tuple[int, int]:
        """The act's sentence holding ``pos``; an empty span when it falls between two."""
        return sentence_at(self.aoa.sentences(), pos) or (pos, pos)

    def _in_advance_clause(self, pos: int) -> bool:
        a, b = self._sentence_of(pos)
        return _ADVANCE_WORDS.search(self.aoa.text, a, b) is not None

    def _scope_reading(self) -> ScopeReading:
        """Both documents' subjects and services, aligned (``aoa_scope.read_scope``), read once:
        the two scope rules, the summary and the model review all use it."""
        if self.contract is None:  # the contract rules call this only with a contract
            raise RuntimeError(_NO_CONTRACT)
        if self._scope is None:
            self._scope = read_scope(
                self.aoa.text, self.aoa.lines, self.contract.text, self.contract.lines
            )
        return self._scope

    def _as_corrected(self, found: Alignment, candidates: Sequence[ScopePart]) -> Alignment:
        """``found`` aligned again with the words AOA.SPELLING reported put right: a typo in a
        listed service or in the subject is that rule's finding, not another service. The
        alignment as it was when no reported word stands in the part."""
        if found.verdict == "match":
            return found
        text = found.item.text
        for f in self.findings:
            if f.rule_id == "AOA.SPELLING" and f.actual and f.expected:
                text = re.sub(
                    rf"(?<![{GEORGIAN}]){re.escape(f.actual)}(?![{GEORGIAN}])", f.expected, text
                )
        if text == found.item.text:
            return found
        return align(scope_part(text, found.item.span), candidates)

    def _review_outcome(self) -> ReviewOutcome | None:
        """``review`` checked against the notes this run leaves open (``aoa_llm.resolve_review``),
        once; None when no review was supplied."""
        if self.review is None or self.contract is None:
            return None
        if self._review is None:
            self._review = resolve_review(
                review_items(self._scope_reading()),
                self.review,
                aoa_document_id=self.aoa.doc_id,
                contract_document_id=self.contract.doc_id,
            )
        return self._review

    def _decision(self, item_id: str, found: Alignment) -> Decision:
        """What becomes of one part of the act (``aoa_llm.decide``): the rules' verdict, and the
        model's when it was asked about this part - never about one the rules settled."""
        verdict: ModelVerdict | None = None
        outcome = self._review_outcome()
        if outcome is not None and escalated(found):
            verdict = outcome.verdicts.get(item_id)
        return decide(found.verdict, verdict)

    def _scope_note(
        self,
        rule_id: str,
        item_id: str,
        decision: Decision,
        ka: str,
        en: str,
        span: tuple[int, int] | None,
    ) -> None:
        """A note the scope rules could not decide, with the model's reading and its reason when
        the review answered for it."""
        if decision.severity is None:
            return
        outcome = self._review_outcome()
        if decision.model is not None and outcome is not None:
            model_ka, model_en = model_reading(rule_id, outcome.answers[item_id])
            ka, en = f"{ka}; {model_ka}", f"{en}; {model_en}"
        self._add(
            rule_id,
            decision.severity,
            ka,
            en,
            span=span,
            confidence=0.5,
            needs_review=decision.needs_review,
            reviewed_by="llm" if decision.model is not None else None,
        )

    def _review_notes(self) -> None:
        """What the model review left open or could not do, as ``AOA.REVIEW`` notes; nothing when
        no review was supplied, so a check with the rules alone reads as it always has."""
        outcome = self._review_outcome()
        for note in outcome.notes if outcome is not None else ():
            if note not in self.skipped:
                self.skipped.append(note)

    # --- rules ---------------------------------------------------------------------------

    def run(self) -> CheckReport:
        # Every run starts clean, so running a checker twice reports each finding once.
        self.findings = []
        self.skipped = []
        self._fx = None
        self._party_matches = None
        self._scope = None
        self._review = None
        self._period_reading = None
        self.rule_amount_words()
        self.rule_required_fields()
        self.rule_act_date()
        self.rule_month_spelling()
        self.rule_script_spelling()
        self.rule_prose_spelling()
        self.rule_iban_format()
        self.rule_bank_code_format()
        self.rule_tax_wording()
        self.rule_fx_invoice()
        if self.contract is None:
            self._skip(
                "AOA.CONTRACT_*",
                "ხელშეკრულება არ არის ატვირთული; აქტი შემოწმდა დამოუკიდებლად",
                "no contract supplied; act checked on its own",
            )
        else:
            self.rule_parties()
            self.rule_party_roles()
            self.rule_party_missing()
            self.rule_cadastral()
            self.rule_project()
            self.rule_subject()
            self.rule_services()
            self._review_notes()
            self.rule_bank()
            self.rule_fee()
            self.rule_period_in_term()
            self.rule_act_date_in_term()
            self.rule_payment_terms()
        self.rule_registry()
        return CheckReport(
            document_id=self.aoa.doc_id,
            reference_document_id=self.contract.doc_id if self.contract else None,
            findings=self.findings,
            skipped=self.skipped,
            summary=self._summary(),
        )

    def _summary(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for i, p in enumerate(self.aoa.parties, 1):
            out[f"party_{i}"] = f"{p.role or '?'}: {p.name} ({p.id_code or 'no id'})"
        period = self._period().dates
        if period:
            out["execution_period"] = f"{min(period)}..{max(period)}"
        act = self._act_date()
        if act:
            out["act_date"] = str(act[0])
        not_applicable = [r for r, types in _NOT_APPLICABLE if self.contractor_type in types]
        if not_applicable:
            out["not_applicable"] = ", ".join(not_applicable)
        out.update(self._fx_summary())
        if self.contract is not None:
            # the notes the model review can be asked about, so the report can offer it
            open_notes = len(review_items(self._scope_reading()))
            if open_notes:
                out["review_items"] = str(open_notes)
        outcome = self._review_outcome()
        if outcome is not None:
            # whether a supplied review was applied, or set aside as answering something else
            out["review"] = "stale" if outcome.stale else "current"
        return out

    def _fx_summary(self) -> dict[str, str]:
        """The act's currency, and for a foreign-currency act the invoice figures: amounts as
        plain decimals and days as ISO dates, so the report's form can offer them back as they
        are - the advance read from the act, to be confirmed or corrected.

        A single advance is offered back; the act's several tranches are not, because one field
        cannot hold them, and left blank they are read from the act again. An advance of 0 the
        user entered is kept, or a re-run would quietly read the act's advance once more.
        """
        out: dict[str, str] = {}
        currencies = fee_currencies(self.aoa.amounts())
        foreign = foreign_currencies(self.aoa.amounts())
        if currencies:
            out["currency"] = ", ".join(foreign) or LOCAL_CURRENCY
        if self._fx is not None:
            plan, gel, priced = self._fx
            out["fx_total"] = f"{plan.total:.2f}"
            # the days whose rates the invoice takes, and the rate each got: the report's form
            # offers a field per day, to type a rate in or correct one
            days = list(dict.fromkeys(p.term.on for p in priced if p.term.on is not None))
            if days:
                out["fx_days"] = ",".join(d.isoformat() for d in days)
            for p in priced:
                if p.quote is not None and p.term.on is not None:
                    out[f"fx_rate_{p.term.on.isoformat()}"] = f"{p.quote.rate:f}"
                    out[f"fx_source_{p.term.on.isoformat()}"] = p.quote.source
            advances = plan.advances
            if len(advances) == 1:
                out["advance_amount"] = f"{advances[0].amount:.2f}"
                if advances[0].on is not None:
                    out["advance_date"] = advances[0].on.isoformat()
            elif not advances and plan.advance_source == "input":
                out["advance_amount"] = "0.00"
            if plan.advance_source is not None:
                out["advance_source"] = plan.advance_source
            if gel is not None:
                out["invoice_gel"] = f"{gel:.2f}"
        elif self.advance is not None and foreign and self.contractor_type not in _FX_EXEMPT:
            # nothing was worked out (see the skip note); what the user entered stays in the form
            if self.advance.amount is not None:
                out["advance_amount"] = f"{self.advance.amount:.2f}"
            if self.advance.paid_on is not None:
                out["advance_date"] = self.advance.paid_on.isoformat()
            out["advance_source"] = "input"
        return out

    def rule_amount_words(self) -> None:
        """'100 (ასი) ლარს': the digits and the written-out number must agree.

        This is a pure intra-document check, so it works with no contract at all, and it
        doubles as an OCR guard: a misread digit no longer matches its own spelled-out form.

        The finding marks the half that is wrong and says how it should read. Which half that is
        comes from the contract when there is one: words that state a figure the contract
        states - its price, or a count it writes in digits and words alike ("12 (თორმეტი)
        პოსტი") - and digits that do not mean the digits are wrong; otherwise - and with no
        contract - the figure is taken as written and the words are corrected to it. Words
        that are the right number misspelled ("ასოი" for "ასი") are a spelling slip with its
        correction, not an unreadable amount; a bracket that holds no numeral at all
        ("დანართი 1 (ტექნიკური დავალება)") is no amount and is left alone.
        """
        checked = 0
        prices = self._contract_figures()
        for pair in digits_and_words(self.aoa.text):
            if not pair.numeral:
                continue
            digits, words, spelled = pair.digits, pair.words, pair.spelled
            if spelled is None:
                fix = number_words_typo(words, int(digits)) if digits.is_integer() else None
                if fix is not None:
                    # the amount in words is the fee's operative statement: misspelled, it must be
                    # corrected before the act is signed, however plain the intended number is
                    self._add(
                        "AOA.AMOUNT_WORDS",
                        "error",
                        f"თანხა სიტყვიერად შეცდომითაა დაწერილი: „{words}“, უნდა იყოს „{fix}“",
                        f"the amount in words is misspelled: '{words}', should be '{fix}'",
                        expected=fix,
                        actual=words,
                        span=pair.words_span,
                        confidence=0.9,
                    )
                elif len(words.split()) <= 3:
                    self._add(
                        "AOA.AMOUNT_WORDS",
                        "warning",
                        f"თანხა სიტყვიერად ვერ წაიკითხა: {pair.raw}",
                        f"could not read the amount in words: {pair.raw}",
                        actual=words,
                        span=pair.words_span,
                        confidence=0.8,
                    )
                continue
            checked += 1
            if amounts_equal(spelled, digits):
                continue
            figures = [*prices, *self._counterpart_figures(pair)]
            words_are_right = any(amounts_equal(p, spelled) for p in figures) and not any(
                amounts_equal(p, digits) for p in figures
            )
            if words_are_right:
                wrong, span = pair.digits_raw, pair.digits_span
                fix = render_figure_like(spelled, pair.digits_raw)
            else:
                wrong, span = words, pair.words_span
                fix = words_for(digits)
            shown_d, shown_w = _plain(digits), _plain(spelled)
            self._add(
                "AOA.AMOUNT_WORDS",
                "error",
                f"ციფრი და სიტყვიერი ჩანაწერი არ ემთხვევა: {shown_d} ≠ „{words}“ ({shown_w}); "
                f"უნდა იყოს „{fix}“",
                f"digits and words disagree: {shown_d} vs '{words}' ({shown_w}); should be '{fix}'",
                expected=fix,
                actual=wrong,
                span=span,
            )
        if not checked:
            self._skip(
                "AOA.AMOUNT_WORDS",
                "აქტში ვერ მოიძებნა თანხა ფორმით „ციფრები (სიტყვიერად)“",
                "no 'digits (words)' amount found",
            )

    def _contract_figures(self) -> list[float]:
        """The figures the contract states for certain: its prices, and every figure it writes
        in digits and words that agree. Empty without a contract."""
        if self.contract is None:
            return []
        agreed = [
            p.digits
            for p in digits_and_words(self.contract.text)
            if p.spelled is not None and amounts_equal(p.spelled, p.digits)
        ]
        return [*self._contract_price_values(), *agreed]

    def _counterpart_figures(self, pair: DigitsAndWords) -> list[float]:
        """The numbers the contract's counterpart of the act's listed service or subject that
        holds ``pair`` states (``aoa_scope.align``), in digits alone or in words: the contract's
        "- 4 სერვისის გვერდი" states the count the act's "- 6 (ოთხი) სერვისის გვერდი" is of. A
        number written anywhere else in the contract is no counterpart. Empty without a
        contract, or when the pair stands in no aligned item."""
        if self.contract is None:
            return []
        reading = self._scope_reading()
        aligned = [*reading.services, *([reading.subject] if reading.subject else [])]
        for found in aligned:
            if found.best is None:
                continue
            span = found.item.span or self._find_span(found.item.text)
            if span is None or not (span[0] <= pair.span[0] and pair.span[1] <= span[1]):
                continue
            stated = [float(n) for n in found.best.numbers if re.fullmatch(r"\d+(?:\.\d+)?", n)]
            agreed = [
                p.digits
                for p in digits_and_words(found.best.text)
                if p.spelled is not None and amounts_equal(p.spelled, p.digits)
            ]
            return [*stated, *agreed]
        return []

    def _contract_price_values(self) -> list[float]:
        """The contract's prices as figures, as the fee rule reads them; empty without one."""
        return [m.value for m in self._contract_prices()]

    def _contract_prices(self) -> list[Money]:
        """The contract's prices: the amounts called a price, or failing those, the ones plainly
        not something else. Empty without a contract."""
        if self.contract is None:
            return []
        contract = self.contract.amounts()
        prices = [r.money for r in contract if r.role == "price"]
        if not prices:  # nothing is called a price: take what is plainly not something else
            prices = [
                r.money for r in contract if r.role not in ("advance", "penalty", "tax", "running")
            ]
        return prices

    def _net_parts(self) -> list[RoledAmount]:
        """The act's fee before tax, where the act breaks the tax out: an amount that with a tax
        amount the act states adds up to another fee the act states, or to a price the contract
        states - "დღგ-ს გარეშე 84.75 ლარს, დღგ - 15.25 ლარს, სულ დღგ-ს ჩათვლით 100 ლარს". It is
        a part of the fee, not a second fee, and "დღგ-ს გარეშე" beside it says what it is.

        With no tax amount stated, the fee before VAT is the figure an exclusion of VAT labels
        ("… ლარს დღგ-ს ჩათვლით (დღგ-ს გარეშე 2 500 ლარი)") that is another fee the act states
        less VAT at its rate (``_vat_rates``). The fee incl. VAT must be in the act: a figure
        before VAT that only the contract's price makes whole is the act stating its fee before
        VAT, and that is still compared. A VAT amount the act does state decides alone."""
        amounts = self.aoa.amounts()
        taxes = [r.money for r in amounts if r.role == "tax"]
        fees = [r for r in amounts if r.role in (None, "price")]
        if not taxes:
            return self._nets_at_vat_rate(fees)
        gross = [r.money for r in fees] + self._contract_prices()
        return [
            r
            for r in fees
            if any(
                t.currency == r.money.currency == g.currency
                and g is not r.money
                and amounts_equal(r.money.value + t.value, g.value)
                for t in taxes
                for g in gross
            )
        ]

    def _nets_at_vat_rate(self, fees: list[RoledAmount]) -> list[RoledAmount]:
        """The fees an exclusion of VAT labels that another act fee, in the same currency, is
        with VAT added (``_net_parts``)."""
        labels = [
            p for p in tax_phrases(self.aoa.text) if p.kind == "exclusion" and "vat" in p.names
        ]
        if not labels:
            return []
        rates = self._vat_rates()
        return [
            r
            for r in fees
            if any(self._labels_amount(p, r) for p in labels)
            and any(
                g is not r
                and g.money.currency == r.money.currency
                and any(_plus_vat(r.money.value, g.money.value, rate) for rate in rates)
                for g in fees
            )
        ]

    def _net_labelled(self) -> list[RoledAmount]:
        """The act's fees an exclusion of VAT ("დღგ-ს გარეშე") labels, each phrase one figure:
        the one it follows straight after ("500 ლარი დღგ-ს გარეშე"), else the first after it in
        its sentence ("(დღგ-ს გარეშე 505 ლარი)", "რაც დღგ-ს გარეშე შეადგენს 500 ლარს"), else the
        one before it within ``_NET_LABEL_REACH``. In "590 ლარს დღგ-ს ჩათვლით (დღგ-ს გარეშე 500
        ლარი)" the phrase is the 500's, not the 590's (red team R3, AOA-R3-08)."""
        amounts = self.aoa.amounts()
        fees = [r for r in amounts if r.role in (None, "price")]
        out: list[RoledAmount] = []
        for phrase in tax_phrases(self.aoa.text):
            if phrase.kind != "exclusion" or "vat" not in phrase.names:
                continue
            a, b = phrase.span
            before = max(
                (r for r in amounts if r.money.span[1] <= a),
                key=lambda r: r.money.span[1],
                default=None,
            )
            after = min(
                (r for r in amounts if r.money.span[0] >= b),
                key=lambda r: r.money.span[0],
                default=None,
            )
            gap = self.aoa.text[before.money.span[1] : a] if before is not None else None
            if before is not None and gap is not None and not re.sub(r"[\s,(]", "", gap):
                meant: RoledAmount | None = before
            elif after is not None and self._sentence_of(after.money.span[0])[0] <= a:
                meant = after
            elif before is not None and a - before.money.span[1] <= _NET_LABEL_REACH:
                meant = before
            else:
                meant = None
            if meant is not None and meant in fees and meant not in out:
                out.append(meant)
        return out

    def _before_vat(self) -> list[RoledAmount]:
        """Every figure the act gives as its fee before VAT: the parts of ``_net_parts``, and a
        figure an exclusion of VAT labels (``_net_labelled``) beside another fee the act states
        in its currency that no such label names - "… 590 ლარს დღგ-ს ჩათვლით (დღგ-ს გარეშე 505
        ლარი)", right or not (red team R3, AOA-R3-08). It is compared with the price before VAT,
        never with the price. A figure before VAT the act states alone is its fee, and stays
        one (round 2)."""
        nets = self._net_parts()
        fees = [r for r in self.aoa.amounts() if r.role in (None, "price") and r not in nets]
        labelled = [r for r in self._net_labelled() if r in fees]
        beside_a_fee = [
            r
            for r in labelled
            if any(g not in labelled and g.money.currency == r.money.currency for g in fees)
        ]
        return [*nets, *beside_a_fee]

    def _vat_rate(self) -> float:
        """The VAT rate a price is taken apart at: the one rate the documents write beside VAT
        other than Georgia's 18%, else 18%."""
        written = [r for r in self._vat_rates() if r != _VAT_RATE_DEFAULT]
        return written[0] if len(written) == 1 else _VAT_RATE_DEFAULT

    def _vat_rates(self) -> list[float]:
        """The VAT rates a fee may be restated at: Georgia's 18%, and any rate the act or the
        contract writes beside VAT ("დღგ 18%", "20% VAT")."""
        rates = [_VAT_RATE_DEFAULT]
        for doc in (self.aoa, self.contract):
            if doc is None:
                continue
            for m in _VAT_RATE.finditer(doc.text):
                rate = float((m.group(1) or m.group(2)).replace(",", ".")) / 100
                if 0 < rate < 1 and rate not in rates:
                    rates.append(rate)
        return rates

    def rule_required_fields(self) -> None:
        """Client rule 3: the act must name the project and the cadastral code."""
        if not self.aoa.anchors(FactType.project_name) and not self._unread(
            "AOA.PROJECT_PRESENT", "პროექტის დასახელება", "the project name"
        ):
            self._add(
                "AOA.PROJECT_PRESENT",
                "warning",
                "აქტში არ წერია პროექტის დასახელება",
                "the act does not state the project name",
            )
        if not self.aoa.anchors(FactType.cadastral_code):
            self._add(
                "AOA.CADASTRAL_PRESENT",
                "warning",
                "აქტში არ წერია საკადასტრო კოდი",
                "the act does not state the cadastral code",
            )

    def rule_act_date(self) -> None:
        """Client rule 4: when the service period is a period, the act is dated month-end.

        The instruction asks that the execution date and the act date match, and for a period
        spells it out: "1 to 31 August inclusive" is signed on the last day of the month, the
        31st. Its example is a whole month, where the period's end and the month's end are the
        same day; for a partial month (1-15 August) this rule follows the instruction's wording
        and expects the month's end. The messages say so, rather than "end of the period".
        """
        reading = self._period()
        hits, period = reading.hits, reading.dates
        act = self._act_date()
        if not period:
            self._skip(
                "AOA.ACT_DATE",
                "აქტში ვერ მოიძებნა მომსახურების შესრულების პერიოდი",
                "execution period not found in the act",
            )
            return
        if len(hits) == 1 and hits[0][1].raw_text.rstrip().endswith("დან"):
            # "from 1 April" and no end: the end is there but was not read (a scan garbled it),
            # and a start is no date to sign the act on
            self._skip(
                "AOA.ACT_DATE",
                "მომსახურების პერიოდის დასასრული ვერ წაიკითხა, ამიტომ აქტის თარიღი არ შემოწმდა",
                "the end of the service period could not be read, so the act's date was not "
                "checked against it",
            )
            return
        if act is None:
            # an act not in Georgian: its date's words were not read, nothing is missing from
            # it (GOLD_SPEC 3, red team R5: AOA-R5-11)
            if self._unread("AOA.ACT_DATE", "აქტის თარიღი", "the act's date"):
                return
            self._add(
                "AOA.ACT_DATE",
                "warning",
                "აქტის გაფორმების თარიღი ვერ მოიძებნა — შეამოწმეთ, დათარიღებულია თუ არა აქტი",
                "the act's date could not be found - check that the act is dated",
            )
            return
        act_date, act_hit = act
        end = max(period)
        expected = _last_day(end) if len(period) > 1 else end
        if act_date == expected:
            return
        window = _term_window(self.contract.term()) if self.contract is not None else None
        if window is None and (act_date.month, act_date.day) == (expected.month, expected.day):
            # a year apart, and no contract term to say which year is right: one of the two is
            # mistyped, and the report asks rather than corrects
            self._add(
                "AOA.ACT_DATE",
                "warning",
                f"აქტის თარიღი ({act_date}) და მომსახურების პერიოდი ({end}) სხვადასხვა წელსაა — "
                "ერთ-ერთში წელი შეცდომითაა; ხელშეკრულების ვადა ვერ წაიკითხა, ამიტომ ვერ "
                "დადგინდა, რომელში",
                f"the act's date ({act_date}) and the service period ({end}) are a year apart - "
                "one of them has the year wrong; the contract's term could not be read, so which "
                "one cannot be told",
                actual=act_hit.raw_text,
                span=(act_hit.start, act_hit.end),
                places=self._act_date_places(act_hit),
            )
            return
        if window is not None and _within(act_date, window) and not _within(expected, window):
            # The act is dated inside the contract's term and the period is not: the period is
            # the odd one out (a year typed wrong), and AOA.PERIOD_IN_TERM names it. Moving the
            # act to the period's month would move it out of the term - a correction that
            # breaks another rule is no correction.
            return
        self._add(
            "AOA.ACT_DATE",
            "error",
            f"აქტი დათარიღებულია {act_date}-ით, მომსახურების პერიოდი მთავრდება {end}-ს — "
            f"აქტი უნდა გაფორმდეს იმ თვის ბოლო დღეს, როდესაც პერიოდი მთავრდება ({expected})",
            f"act dated {act_date} but the service period ends {end}; it should be dated "
            f"the last day of the month the period ends in ({expected})",
            # written the way the act writes its date: "2026 წლის 31 აგვისტო", not ISO
            expected=render_date_like(
                expected, act_hit.raw_text, following=self.aoa.text[act_hit.end : act_hit.end + 40]
            ),
            actual=act_hit.raw_text,
            span=(act_hit.start, act_hit.end),
            places=self._act_date_places(act_hit),
        )

    def rule_month_spelling(self) -> None:
        """Typos inside date wording, surfaced by the typo-tolerant month matcher.

        The finding names the correct form (``month_ka_correction``) and anchors on the
        misspelled word itself, so the Word comment says what to write, and where.
        """
        for note, start, end in _month_typos(self.aoa.text):
            word = note.split(": ")[-1].strip("'\"")
            at = self.aoa.text.find(word, start, end)
            after = at + len(word) if at >= 0 else end
            self._add(
                "AOA.SPELLING",
                "warning",
                f"თვის დასახელება შეცდომითაა დაწერილი: {word}",
                f"month name appears misspelled: {word}",
                expected=month_ka_correction(word, following=_next_word(self.aoa.text, after)),
                actual=word,
                span=(at, at + len(word)) if at >= 0 else (start, end),
                confidence=0.9,
            )

    def rule_script_spelling(self) -> None:
        """A Latin or Cyrillic letter, or a digit, inside a Georgian word: "და sმომიჯნავე".

        No Georgian word holds another alphabet's letter, so on a typed document this is always
        a slip - one the eye passes over, since the stray letter sits beside the right ones. The
        correction drops it, preferring a word the contract or the act itself writes; when they
        write neither reading - the stray key's Georgian letter put back, or the stray letter
        dropped - the dictionary picks the one that is a word ("უფლeბები" is "უფლებები").
        """
        vocabulary = frozenset(
            w
            for doc in (self.aoa, self.contract)
            if doc is not None
            for w, _, _ in georgian_words(doc.text)
            if is_georgian_word(w)
        )
        speller = None if isinstance(self.speller, SpellingUnavailable) else self.speller
        for found in mixed_script_words(self.aoa.text, vocabulary=vocabulary):
            # the word as the act writes it: the text read here has Cyrillic look-alikes folded
            # to Latin, and a quote in the wrong alphabet is found neither by eye nor in Word
            word = self.aoa.as_written(found.start, found.end)
            stray_ka, stray_en = _stray_letters(word)
            correction = found.correction
            # a letter guessed in the stray one's place loses to a reading the dictionary knows:
            # the stray letter dropped, or its key's letter (mutation harness, seed 229)
            if speller is not None and (not found.attested or found.guessed):
                known = [r for r in found.readings if spelled_right(r, speller)]
                if len(known) == 1:
                    correction = known[0]
                elif len(known) > 1 and _inside(found.word):
                    # both are words: inside a word the stray letter stood for its key's letter,
                    # at an edge it was a key pressed as well
                    correction = found.readings[0]
            self._add(
                "AOA.SPELLING",
                "warning",
                f"ქართულ სიტყვაში ურევია {stray_ka}: „{word}“",
                f"a Georgian word holds {stray_en}: '{word}'",
                expected=correction,
                actual=word,
                quote=word,
                span=(found.start, found.end),
                confidence=0.95,
            )

    def rule_prose_spelling(self) -> None:
        """A word the dictionary does not know, one letter from one it does: "ეთმანეთის".

        The gates are ``aoa_spelling``'s. It runs after the month, number-word and stray-letter
        rules, and a word one of them reported is not reported again.
        """
        if self.speller is None or isinstance(self.speller, SpellingUnavailable):
            why = self.speller or NOT_SUPPLIED
            self._skip("AOA.SPELLING", why.reason_ka, why.reason_en)
            return
        taken = [f.span for f in self.findings if f.span is not None]
        reading = prose_typos(self.aoa, self.contract, self.speller, taken=taken)
        if (short := reading.shortfall()) is not None:
            self._skip("AOA.SPELLING", short.reason_ka, short.reason_en)
        for typo in reading.typos:
            word = self.aoa.as_written(typo.start, typo.end)
            if typo.attested:
                ka = f"სიტყვა შეცდომითაა დაწერილი: „{word}“"
                en = f"word appears misspelled: '{word}'"
            else:  # the dictionary's reading alone: it lacks words and forms, so check it
                ka = f"სიტყვა შესაძლოა შეცდომითაა დაწერილი: „{word}“ — გადაამოწმეთ"
                en = f"word may be misspelled: '{word}' - check it"
            if self.aoa.ocr:  # on a scan, a misread letter looks just like a typo
                ka, en = _MISREAD_KA + ka, _MISREAD_EN + en
            self._add(
                "AOA.SPELLING",
                "warning" if typo.attested else "info",
                ka,
                en,
                expected=typo.correction,
                actual=word,
                quote=word,
                span=(typo.start, typo.end),
                confidence=0.8 if typo.attested else 0.6,
                needs_review=self.aoa.ocr or not typo.attested,
            )

    def rule_iban_format(self) -> None:
        """A failing checksum only means something once the account differs from the contract.

        The client's sample documents carry anonymised account numbers whose check digits are
        invalid, so this is reported as information, never as an error, and the comparison in
        rule_bank is what decides whether an account is actually wrong.
        """
        for iban in self.aoa.bank("IBAN"):
            if not iban_is_valid(iban):
                self._add(
                    "AOA.IBAN_CHECKSUM",
                    "info",
                    f"IBAN-ის საკონტროლო ციფრები არ იკრიბება: {iban}",
                    f"IBAN checksum does not validate: {iban}",
                    actual=iban,
                    span=self._account_span(iban),
                    confidence=0.9,
                )

    def rule_bank_code_format(self) -> None:
        """A bank code that is not a well-formed BIC.

        Unlike the IBAN checksum, this is an error: the client's documents carry anonymised
        account numbers but real bank codes, and a seven-character code is a drafting mistake
        no anonymisation would produce.
        """
        for hit in self.bank_code_problems():
            # the code itself, not its label: the comment belongs on the characters to fix
            at = self.aoa.text.find(hit.value, hit.start, hit.end)
            span = (at, at + len(hit.value)) if at >= 0 else (hit.start, hit.end)
            codes = sorted(self.contract.bank("SWIFT")) if self.contract is not None else []
            self._add(
                "AOA.BANK_CODE_FORMAT",
                "error",
                f"ბანკის კოდი არასწორი ფორმატისაა: {hit.value} (BIC უნდა იყოს 8 ან 11 სიმბოლო)",
                f"bank code is not a well-formed BIC: {hit.value} (a BIC is 8 or 11 characters)",
                expected=codes[0] if len(codes) == 1 else None,
                actual=hit.value,
                span=span,
            )

    def bank_code_problems(self) -> list[AnchorHit]:
        return [h for h in self.aoa.bank_hits("SWIFT") if h.note]

    def rule_tax_wording(self) -> None:
        """Client rules 5-8: what the act says about taxes, by contractor type.

        Nothing in the document states the contractor type, so when it is not supplied the rule
        is reported as not run rather than guessed at.

        The wording is read where the fee is written - each price's sentence and the next one
        (``fee_scope``). A phrase there that contradicts the rule ("გადასახადების ჩათვლით" for a
        natural person) is reported on the phrase, once per fee, with the wording to write
        instead. The wording the rule asks for counts anywhere in the act: an act that writes
        NET beside its fee and restates the total later without it says NET all the same. It is
        missing only when the act never says it, and is then reported on the fee. A foreign
        organisation's act must not mention taxes at all, so for it every tax phrase anywhere
        is reported, each once.
        """
        if self.contractor_type is None:
            self._skip(
                "AOA.TAX_WORDING",
                "შემსრულებლის ტიპი მითითებული არ არის (ფიზიკური პირი / ინდ. მეწარმე / "
                "მცირე მეწარმე / უცხოური ორგანიზაცია), ამიტომ NET/დღგ-ს ფორმულირება არ შემოწმებულა",
                "contractor type not supplied (natural person / individual entrepreneur / "
                "small business / foreign organisation), so NET/VAT wording was not checked",
            )
            return
        if self.contractor_type == "company":
            return  # rules 5-8 do not apply to a company; the summary says so (_NOT_APPLICABLE)
        rule = _TAX_RULES[self.contractor_type]
        nets = self._before_vat()
        # "დღგ-ს გარეშე" beside the fee before tax says what that part is, not what the fee is
        phrases = [p for p in tax_phrases(self.aoa.text) if not self._qualifies_net(p, nets)]
        fees = [m for m in self._fee_amounts() if all(m != r.money for r in nets)]
        scopes = [fee_scope(self.aoa.sentences(), m.span) for m in fees]
        if not scopes:  # no fee found to read beside: the whole act is where the wording is
            scopes = [(0, len(self.aoa.text))]
        if self.contractor_type == "foreign":
            fixed = self._tax_mentions(rule, phrases, scopes)
        else:
            fixed = self._tax_contradictions(rule, phrases, scopes)
        # a contradiction reported beside the fee already names the wording to write there
        if fixed or _tax_wording_met(self.contractor_type, phrases):
            return
        fee = fees[0] if fees else None
        fix = self._in_script(rule, fee.span[0] if fee else 0)
        self._add(
            "AOA.TAX_WORDING",
            "error",
            f"{rule.rule_ka}; {rule.absent_ka}",
            f"{rule.rule_en}; {rule.absent_en}",
            expected=f"{fee.raw} {fix}" if fee else fix,
            span=fee.span if fee else None,
        )

    def _qualifies_net(self, phrase: TaxPhrase, nets: list[RoledAmount]) -> bool:
        """Whether an exclusion ("დღგ-ს გარეშე", "excl. VAT") is the label of a fee before tax."""
        return phrase.kind == "exclusion" and any(self._labels_amount(phrase, r) for r in nets)

    def _labels_amount(self, phrase: TaxPhrase, r: RoledAmount) -> bool:
        """Whether ``phrase`` is the label of the amount ``r``: in its sentence, before the
        figure with no other amount between, or right after it."""
        starts = [x.money.span[0] for x in self.aoa.amounts()]
        a, b = phrase.span
        start, end = r.money.span
        lo, hi = (b, start) if b <= start else (end, a)
        if any(lo <= x < hi for x in starts):
            return False  # another amount stands between: the phrase is that one's
        if b <= start and self._sentence_of(start)[0] <= a:
            return True
        return a >= end and a - end <= _NET_LABEL_REACH

    def _fee_amounts(self) -> list[Money]:
        """The act's fee as written: the amounts called a price, or failing those, the ones
        called nothing at all. An advance, a penalty or a tax amount is not where the fee's
        tax wording goes."""
        amounts = self.aoa.amounts()
        return [r.money for r in amounts if r.role == "price"] or [
            r.money for r in amounts if r.role is None
        ]

    def _in_script(self, rule: _TaxRule, pos: int) -> str:
        """The rule's wording in the script of the sentence at ``pos``: an English sentence gets
        "including VAT", a Georgian one "დღგ-ს ჩათვლით"."""
        a, b = next(((a, b) for a, b in self.aoa.sentences() if a <= pos < b), (0, 0))
        sentence = self.aoa.text[a:b]
        latin = len(_LATIN.findall(sentence)) > len(_GEORGIAN.findall(sentence))
        return rule.fix_en if latin else rule.fix_ka

    def _tax_contradiction(self, rule: _TaxRule, phrase: TaxPhrase, fix: str | None) -> None:
        self._add(
            "AOA.TAX_WORDING",
            "error",
            f"{rule.rule_ka}; აქტში წერია „{phrase.text}“",
            f"{rule.rule_en}; the act says '{phrase.text}'",
            expected=fix,
            actual=phrase.text,
            span=phrase.span,
        )

    def _tax_contradictions(
        self, rule: _TaxRule, phrases: list[TaxPhrase], scopes: list[tuple[int, int]]
    ) -> bool:
        """Wording beside a fee that says the opposite of the rule, one finding per fee.

        A phrase two fees share (their sentences overlap) is reported with the first. True when
        anything was reported.
        """
        seen: set[tuple[int, int]] = set()
        for lo, hi in scopes:
            wrong = [
                p for p in phrases if p.kind in rule.offends and p.span[0] < hi and p.span[1] > lo
            ]
            fresh = [p for p in wrong if p.span not in seen]
            seen.update(p.span for p in wrong)
            if fresh:
                self._tax_contradiction(rule, fresh[0], self._in_script(rule, fresh[0].span[0]))
        return bool(seen)

    def _tax_mentions(
        self, rule: _TaxRule, phrases: list[TaxPhrase], scopes: list[tuple[int, int]]
    ) -> bool:
        """A foreign organisation's act: every tax phrase anywhere, each distinct one once.

        Beside a fee, an inclusion or exclusion ("VAT included") is the place NET goes, and the
        finding says so; a mere mention elsewhere is only to be removed. True when a finding
        beside a fee already names NET as the wording to write.
        """

        def beside_a_fee(p: TaxPhrase) -> bool:
            return any(p.span[0] < hi and p.span[1] > lo for lo, hi in scopes)

        # the copy beside the fee first, so that is the one reported with its NET
        offending = sorted(
            (p for p in phrases if p.kind in rule.offends), key=lambda p: not beside_a_fee(p)
        )
        named_net = False
        seen: set[str] = set()
        for p in offending:
            key = p.text.lower()
            if key in seen:
                continue
            seen.add(key)
            replaced = beside_a_fee(p) and p.kind != "mention"
            named_net |= replaced
            self._tax_contradiction(rule, p, self._in_script(rule, p.span[0]) if replaced else None)
        return named_net

    def rule_fx_invoice(self) -> None:
        """Client rule 9: an act in a foreign currency is invoiced in lari at the NBG rate - an
        advance at the rate of the day it was paid, the rest at the rate of the act's date.

        One note, not an error: nothing in the act is wrong. The client asked to be told how the
        invoice must be worked out, so that it can be checked (``fx_invoice``). The note is marked
        on the act's total and carries no expected value, which would read as a mismatch. With no
        rate source connected (``fx.provider``) it gives the formula and the days whose rates to
        look up, and a skip note says the total in lari was not calculated.

        A small business or a foreign organisation needs no such invoice, so for them the rule
        does not apply and the summary says so (``_NOT_APPLICABLE``). With the contractor type
        unknown the invoice is worked out all the same, and the note says who does not need it.
        """
        self._fx = None
        if self.contractor_type in _FX_EXEMPT:
            return
        act = self._act_date()
        found = plan_invoice(
            self.aoa.amounts(),
            self.aoa.advance(in_act=True),
            act_date=act[0] if act else None,
            entered=self.advance,
        )
        if found is None:  # an act in lari: there is nothing to convert
            return
        if isinstance(found, NoInvoice):
            self._skip("AOA.FX_INVOICE", found.reason_ka, found.reason_en)
            return
        priced = price_terms(found, self.rates)
        gel = invoice_gel(priced)
        self._fx = (found, gel, priced)
        formula_ka = " + ".join(
            _fx_term_ka(p, found.currency, priced=gel is not None) for p in priced
        )
        formula_en = " + ".join(
            _fx_term_en(p, found.currency, priced=gel is not None) for p in priced
        )
        if gel is not None and len(priced) > 1:
            formula_ka += f"; სულ {gel:,.2f} {LOCAL_CURRENCY}"
            formula_en += f"; in total {gel:,.2f} {LOCAL_CURRENCY}"
        ka = (
            f"აქტი შედგენილია {found.currency}-ში. ანგარიშფაქტურა უნდა იყოს: {formula_ka} — "
            "გადაამოწმეთ ანგარიშფაქტურა."
        )
        en = (
            f"The act is in {found.currency}. The invoice must be: {formula_en} — verify the "
            "invoice."
        )
        if found.advance_source == "act":
            ka += " ავანსი აღებულია აქტიდან — დაადასტურეთ."
            en += " The advance is taken from the act: confirm it."
        if self.contractor_type is None:
            ka += " მცირე მეწარმესა და უცხოურ ორგანიზაციას ასეთი ანგარიშფაქტურა არ სჭირდება."
            en += " A small business or a foreign organisation needs no such invoice."
        self._add("AOA.FX_INVOICE", "info", ka, en, span=found.anchor)
        if gel is None:
            self._fx_rate_skip(found.currency, priced)
        if act:
            self._late_advances(found, act[0])

    def _late_advances(self, plan: InvoicePlan, act_day: date) -> None:
        """An advance dated after the act's own date: an advance is paid before the act, so one of
        the two dates is wrong - and the invoice would convert it at the wrong day's rate. A
        warning, with no "should be": which date is wrong is not for the check to say. Marked on
        the act's advance where the act states it."""
        stated = self.aoa.advance(in_act=True).payments
        for term in plan.advances:
            if term.on is None or term.on <= act_day:
                continue
            span = next(
                (
                    p.money.span
                    for p in stated
                    if p.paid_on == term.on and to_money(p.money.value) == term.amount
                ),
                None,
            )
            paid, signed = term.on.isoformat(), act_day.isoformat()
            self._add(
                "AOA.FX_INVOICE",
                "warning",
                f"ავანსის გადახდის თარიღი ({paid}) აქტის თარიღზე ({signed}) გვიანაა — ავანსი "
                "აქტამდე იხდება და ლარში მისი გადახდის დღის კურსით გადაიანგარიშება; შეამოწმეთ "
                "თარიღები",
                f"the advance is dated {paid}, after the act's date {signed}: an advance is paid "
                "before the act and converted at the rate of its payment day - check the dates",
                span=span,
            )

    def _fx_rate_skip(self, currency: str, priced: tuple[PricedTerm, ...]) -> None:
        """Say the total in lari was not calculated, name the days whose rates it needs, and
        say what would supply them: the imported NBG rates cover other days, or none are
        imported - either way a rate typed in on the form is enough for this check."""
        missing = [p.term for p in priced if p.quote is None]
        days_ka = ", ".join(dict.fromkeys(_fx_day(t)[0] for t in missing))
        days_en = ", ".join(dict.fromkeys(_fx_day(t)[1] for t in missing))
        if isinstance(self.rates, NullRateProvider):
            self._skip(
                "AOA.FX_RATE",
                "გაცვლითი კურსის წყარო დაკავშირებული არ არის, ამიტომ ანგარიშფაქტურის თანხა ლარში "
                f"არ გამოთვლილა; მოიძიეთ NBG-ის {currency} კურსი: {days_ka}",
                "no exchange-rate source is connected, so the invoice total in lari was not "
                f"calculated; look up the NBG {currency} rate of: {days_en}",
            )
            return
        held = self.rates.coverage(currency)
        if held is None:
            ka = (
                f"NBG-ის {currency} კურსები სისტემაში შემოტანილი არ არის, ამიტომ ანგარიშფაქტურის "
                f"თანხა ლარში არ გამოთვლილა; შეიყვანეთ {currency} კურსი: {days_ka}"
            )
            en = (
                f"no NBG {currency} rates have been imported, so the invoice total in lari was "
                f"not calculated; enter the {currency} rate of: {days_en}"
            )
        else:
            first, last = (d.isoformat() for d in held)
            ka = (
                f"ანგარიშფაქტურის თანხა ლარში არ გამოთვლილა — NBG-ის {currency} კურსი ვერ "
                f"მოიძებნა: {days_ka} (შემოტანილი კურსები მოიცავს {first} … {last}); შეიყვანეთ "
                "კურსი ან შემოიტანეთ ახალი კურსები"
            )
            en = (
                f"the invoice total in lari was not calculated: no NBG {currency} rate was found "
                f"for {days_en} (the imported rates cover {first} … {last}); enter the rate or "
                "import newer rates"
            )
        self._skip("AOA.FX_RATE", ka, en)

    # --- rules that need the contract -----------------------------------------------------

    def _matched_parties(self) -> list[_PartyMatch]:
        """Which contract party each act party is, worked out once a run: the party rules all
        ask, and only the rules report what it found.

        The identification code decides, and the name when there is no code. A code and a name
        that point at two contract parties, each with a code of its own, mean one of them is
        wrong in the act, and the rest of the act's block tells which. When its address and
        signatories are more the code's party's than the name's, the name is the mistake, and
        matching by code lets AOA.PARTY_NAME say so. Otherwise the code is (AOA.ID_CODE_PARTY):
        a contractor written with the customer's code is still the contractor, not the customer
        with a wrong name, address and director.
        """
        if self.contract is None:  # run() calls the contract rules only with a contract
            raise RuntimeError(_NO_CONTRACT)
        if self._party_matches is None:
            self._party_matches = [_match_party(p, self.contract.parties) for p in self.aoa.parties]
        return self._party_matches

    def _party_without_contract_code(self, code: str) -> str | None:
        """The name of the contract party the act's party writing ``code`` is, when the contract
        states no code for it; None otherwise."""
        for m in self._matched_parties():
            if m.party.id_code == code and m.match is not None and not m.match.id_code:
                return m.match.name or m.party.name
        return None

    def _unread_party_of(self, form: str, match: Party, theirs: list[str]) -> Party | None:
        """The contract party other than ``match`` whose signatory ``form`` names or is nearer
        to than to ``theirs`` (``match``'s), when no act party is that party (red team R2,
        AOA-R2-04): the act writes it in a form not read as a party, so its signatory - written
        right or with a slip - stands in a neighbour's block and is no slip of the neighbour's."""
        if self.contract is None:
            return None
        matched = [m.match for m in self._matched_parties()]
        others = [
            (q, s)
            for q in self.contract.parties
            if q is not match and not any(q is x for x in matched)
            for s in [*q.directors, *q.deputies, *q.proxies]
        ]
        if not others:
            return None

        def distance(named: str) -> int:
            return edit_distance(normalize_name(lemma(form)), normalize_name(lemma(named)), cap=99)

        own = min((distance(s) for s in theirs), default=99)
        q, nearest = min(((q, distance(s)) for q, s in others), key=lambda t: t[1])
        return q if nearest <= own else None

    def _span_in_act(self, span: tuple[int, int] | None, holds: str) -> tuple[int, int] | None:
        """``span`` when it lies in the act's text and covers ``holds``; a party built by hand, or
        from another text, has none that can be trusted."""
        if span is None:
            return None
        a, b = span
        return span if 0 <= a < b <= len(self.aoa.text) and holds in self.aoa.text[a:b] else None

    def _written_forms(self, name: str) -> list[tuple[str, tuple[int, int]]]:
        """Each form in which the act writes ``name`` - its last word in any case, the words
        before it as they are - with the first place of each, in the act's order. A person's
        name is read once per party, but a preamble's genitive and a signature's nominative are
        two spellings of it to correct."""
        parts = name.split()
        if len(parts) < 2 or not all(is_georgian_word(w) for w in parts):
            return []
        head = r"\s+".join(map(re.escape, parts[:-1]))
        forms: dict[str, tuple[int, int]] = {}
        for m in re.finditer(rf"(?<![{GEORGIAN}]){head}\s+([{GEORGIAN}]+)", self.aoa.text):
            if same_word(m.group(1), parts[-1]):
                forms.setdefault(self.aoa.as_written(*m.span()), m.span())
        return list(forms.items())

    def _person_forms(self, name: str) -> list[tuple[str, list[tuple[int, int]]]]:
        """Each form in which the act writes the person ``name``, spelled right or one slip away
        (``_near_word``), with every place of each, in the act's order.

        The party reader keeps one form of a signatory and takes another that differs only in
        its ending for the same person, a slip in the ending included ("მელაძის" and "მელაძი");
        the rule compares each form found here on its own (``_same_person``). So does it a form
        with the first name as its initial ("ნ. ბერიძი" beside "ნოდარ ბერიძის", red team R3,
        AOA-R3-07): the party reader takes the two for one person, and the initialled form's
        surname is compared on its own."""
        parts = normalize_name(name).split()
        if len(parts) < 2 or not all(is_georgian_word(w) for w in parts):
            return [(form, [span]) for form, span in self._written_forms(name)]
        found = georgian_words(self.aoa.text)
        forms: dict[str, list[tuple[int, int]]] = {}
        for i in range(len(found) - len(parts) + 1):
            window = found[i : i + len(parts)]
            # the words of one name stand side by side, spaces between them and nothing else
            if any(self.aoa.text[a[2] : b[1]].strip() for a, b in pairwise(window)):
                continue
            if all(_near_word(w, p) for (w, _, _), p in zip(window, parts, strict=True)):
                span = (window[0][1], window[-1][2])
                forms.setdefault(self.aoa.as_written(*span), []).append(span)
            elif len(parts) == 2 and i + 3 < len(found):
                # a patronymic between the first name and the surname (red team R3, AOA-R3-06):
                # the form is the name as written, patronymic and all
                first, surname = window[0], found[i + 3]
                between = self.aoa.text[first[2] : surname[1]]
                if (
                    PATRONYMIC.fullmatch(between.strip())
                    and _near_word(first[0], parts[0])
                    and _near_word(surname[0], parts[1])
                ):
                    span = (first[1], surname[2])
                    forms.setdefault(self.aoa.as_written(*span), []).append(span)
        initials = r"\s*".join(rf"{re.escape(p[0])}\." for p in parts[:-1])
        pattern = rf"(?<![{GEORGIAN}]){initials}\s*([{GEORGIAN}]+)(?![{GEORGIAN}])"
        for m in re.finditer(pattern, self.aoa.text):
            if _near_word(m.group(1), parts[-1]):
                forms.setdefault(self.aoa.as_written(*m.span()), []).append(m.span())
        return sorted(forms.items(), key=lambda form: form[1][0])

    def _owner_at(self, pos: int) -> Party | None:
        """The act party whose block ``pos`` stands in: the one named last before it - by its
        name, its identification code or its role label ("„შემკვეთი“" over a signature). None
        when no party is named before it."""
        best: tuple[int, Party] | None = None
        for q in self.aoa.parties:
            for start in _mention_starts(self.aoa.text, q):
                if start < pos and (best is None or start > best[0]):
                    best = (start, q)
        return best[1] if best else None

    def _places_of_director(
        self, p: Party, form: str, spans: list[tuple[int, int]]
    ) -> tuple[tuple[int, int] | None, tuple[tuple[int, ...], int] | None] | None:
        """Where the wrong ``form`` of ``p``'s director is ``p``'s: the first such place, and
        which of the form's places those are (``Finding.places``). A name copied from the other
        party - the customer's director written for the contractor - is right where it stands
        in the customer's block, and only the contractor's places are to be marked. Every place
        is, when none of them is plainly another party's correct director.

        None when every place is another party's correct director (red team R4, AOA-R4-01): the
        customer's director copied into the contractor's signature alone leaves the customer's
        own preamble genitive right, and that form is no mistake of the contractor's at all."""
        others: list[int] = []
        for i, (start, _) in enumerate(spans):
            owner = self._owner_at(start)
            if owner is None or owner is p:
                continue
            owned = next((m.match for m in self._matched_parties() if m.party is owner), None)
            if owned is not None and any(_same_person(form, cd) for cd in owned.directors):
                others.append(i)
        mine = [i for i in range(len(spans)) if i not in others]
        if not mine and others:
            return None
        if not others:
            return (spans[0] if spans else None), None
        return spans[mine[0]], (tuple(mine), len(spans))

    def rule_parties(self) -> None:
        """Client rule 1 and the contract cross-check: identify by code, then verify the rest."""
        if self.contract is None:  # run() calls the contract rules only with a contract
            raise RuntimeError(_NO_CONTRACT)
        # every code the contract writes behind a label, not only the one per party the party
        # reader takes: a code the contract states is never "not in the contract"
        c_ids = self.contract.id_codes() | _labelled_codes(self.contract.text)
        a_ids = self.aoa.id_codes()
        if not a_ids or len(a_ids) < len(self.contract.id_codes()):
            # a party the reader did not make out (a name without quotes) leaves its code in a
            # neighbour's window, where only the neighbour's is taken: read them off the labels
            a_ids |= _labelled_codes(self.aoa.text)
        if not a_ids or not _reads_georgian(self.aoa.text):
            # an act not in Georgian is not read for its parties (round 1, AOA-15), even where
            # an English label ("ID: …") gives a code: its parties are not made out
            if not self._unread(
                "AOA.ID_CODES",
                "მხარეთა საიდენტიფიკაციო კოდები",
                "the parties' identification codes",
            ):
                self._add(
                    "AOA.ID_CODES",
                    "error",
                    "აქტში საიდენტიფიკაციო კოდები ვერ მოიძებნა",
                    "no identification codes found in the act",
                )
        elif c_ids and not a_ids <= c_ids:
            # one finding per code, marked where the act writes it, with the contract's code it
            # most likely stands for: a transposed digit is one code to fix, not a list to read
            for code in sorted(a_ids - c_ids):
                if (party := self._party_without_contract_code(code)) is not None:
                    # the contract states no code for this party: the other party's code is no
                    # correction for it (red team R2, AOA-R2-01)
                    self._skip(
                        "AOA.ID_CODES",
                        f"{party}: ხელშეკრულებაში ამ მხარის საიდენტიფიკაციო კოდი არ იკითხება, "
                        f"ამიტომ აქტის კოდი {code} არ შედარდა",
                        f"{party}: the contract states no identification code for this party that "
                        f"could be read, so the act's code {code} was not compared",
                    )
                    continue
                closest: str | None
                closest = min(sorted(c_ids), key=lambda c: (_digit_distance(code, c), c))
                if len(closest) != len(code) and _digit_distance(code, closest) > 2:
                    closest = None  # a digit or two dropped or doubled, or no correction
                span = self._code_span(code)
                written = self.aoa.as_written(*span) if span else code
                # a code of the wrong length says so (red team R3, AOA-R3-01)
                ka_len = en_len = ""
                if len(code) not in (9, 11):
                    ka_len = f" ({len(code)} ციფრი; კოდი 9- ან 11-ნიშნაა)"
                    en_len = f" ({len(code)} digits; a code has 9 or 11)"
                self._add(
                    "AOA.ID_CODES",
                    "error",
                    f"საიდენტიფიკაციო კოდი ხელშეკრულებაში არ არის: {code}{ka_len}",
                    f"identification code not in the contract: {code}{en_len}",
                    # in the act's grouping: "987-654-312" is corrected to "987-654-321"
                    expected=in_grouping(closest, written) if closest else None,
                    actual=code,
                    span=span,
                )

        if not self.contract.parties:
            self._skip(
                "AOA.PARTY_NAME",
                "ხელშეკრულებაში მხარეები ვერ ამოიცნო (დასახელება ბრჭყალების გარეშე?), ამიტომ "
                "დასახელება, მისამართი და დირექტორი არ შედარდა",
                "the contract's parties could not be read (names without quotes?), so their "
                "names, addresses and directors were not compared",
            )
            return
        # a side the contract defines whose party it names in a form not read as one: its
        # name, address and signatory go uncompared, and the report says so (red team R4)
        sides = defined_sides(self.contract.text)
        for role in sides if len(self.contract.parties) < len(sides) else []:
            if not any(_side(q.role) == _side(role) for q in self.contract.parties):
                self._skip(
                    "AOA.PARTY_NAME",
                    f"ხელშეკრულება განსაზღვრავს მხარეს „{role}“, მაგრამ მისი დასახელება ვერ "
                    "წაიკითხა (სამართლებრივი ფორმა და დასახელება ბრჭყალებში), ამიტომ მისი "
                    "დასახელება, მისამართი და ხელმომწერი არ შედარდა",
                    f"the contract defines the party '{role}', but its name could not be read "
                    "(a legal form and a quoted name), so its name, address and signatory were "
                    "not compared",
                )
        for m in self._matched_parties():
            p, match = m.party, m.match
            if m.code_of is not None and match is not None:
                self._id_code_party(p, match, m.code_of)
            if match is None:
                self._add(
                    "AOA.PARTY_NAME",
                    "error",
                    f"მხარე ხელშეკრულებაში ვერ მოიძებნა: {p.name}",
                    f"party not found in the contract: {p.name}",
                    actual=p.name,
                    span=self._find_span(p.name or ""),
                )
                continue
            self._compare_field(
                "AOA.PARTY_NAME", "მხარის დასახელება", "party name", p.name, match.name
            )
            self._compare_field(
                "AOA.PARTY_ADDRESS",
                "იურიდიული მისამართი",
                "legal address",
                p.address,
                match.address,
            )
            self._proxies(p, match)
            self._signatories(p, match, deputy=False)
            self._signatories(p, match, deputy=True)
        self._unread_signing_proxies()

    def _unread_signing_proxies(self) -> None:
        """A skip note for an attorney the act's signature block names ("მინდობილი პირი: …") that
        the party reader did not take for any party's signatory: what was not read is not
        compared, and the report says so rather than stay silent (red team R4, AOA-R4-12)."""
        # a name the act's parties are read with, or one the contract names (written right
        # there, it needs no comparing)
        parties = [*self.aoa.parties, *(self.contract.parties if self.contract else [])]
        read = [n for q in parties for n in (*q.proxies, *q.directors, *q.deputies) if n]
        for line in self.aoa.lines:
            m = _SIGNING_PROXY.match(line)
            if m is None:
                continue
            name = m.group("name")
            if any(_same_person(name, n) for n in read):
                continue
            self._skip(
                "AOA.DIRECTOR",
                f"აქტს ხელს მინდობილობით აწერს „{name}“, რომელიც რომელიმე მხარის ხელმომწერად "
                "ვერ წაიკითხა: ხელშეკრულებას არ შედარდა",
                f"the act is signed under a power of attorney by '{name}', who was not read as "
                "either party's signatory: not compared with the contract",
            )

    def _signatories(self, p: Party, match: Party, *, deputy: bool) -> None:
        """AOA.DIRECTOR: the act's signatories for ``p`` against the contract's for ``match`` -
        its head's (``deputy`` False) or its deputy's or acting head's (``deputy`` True). A
        deputy is compared only with the deputy the contract names: signing in the head's place
        can be right, so a deputy the contract does not name is a skip note, never an error."""
        names, theirs = (p.deputies, match.deputies) if deputy else (p.directors, match.directors)
        for d in names:
            if not theirs:
                if not deputy:
                    self._skip(
                        "AOA.DIRECTOR",
                        f"ხელშეკრულებაში {match.name}-ის დირექტორი მითითებული არ არის",
                        f"contract names no director for {match.name}",
                    )
                elif not any(_same_person(d, x) for x in [*match.directors, *match.proxies]):
                    self._skip(
                        "AOA.DIRECTOR",
                        f"აქტს {match.name}-ის სახელით ხელს აწერს მოადგილე ან მოვალეობის "
                        f"შემსრულებელი „{d}“, ხელშეკრულება მოადგილეს არ ასახელებს: არ შედარდა",
                        f"the act is signed for {match.name} by a deputy or an acting head, "
                        f"'{d}', and the contract names no deputy: not compared",
                    )
                continue
            self._person_findings(p, match, d, theirs)

    def _person_findings(self, p: Party, match: Party, d: str, theirs: list[str]) -> None:
        """AOA.DIRECTOR errors for the act's signatory ``d`` of ``p`` against the people the
        contract names for ``match`` in the same capacity (``theirs``: its heads, its deputies or
        its attorneys)."""
        # The preamble writes the genitive, the signature block the nominative, and the
        # party reader keeps one of them: every form the act writes of this person is
        # compared, so a slip in one ("მელაძი") does not hide behind the other written
        # right. Each wrong form has its own correction and every place of it marked.
        forms = self._person_forms(d)
        if not forms:
            found = self._find_span(d)
            forms = [(d, [found] if found else [])]
        for written, spans in forms:
            # a patronymic is no part of the name compared, and is kept in a correction
            form, patronymic = split_patronymic(written)
            # an inflection of the contract's name, and nothing else, is the same name
            if any(_same_person(form, cd) for cd in theirs):
                continue
            if (other := self._unread_party_of(form, match, theirs)) is not None:
                # the act writes that party in a form not read as one (AOA.PARTY_MISSING's
                # note): its signatory is no slip of this party's
                self._skip(
                    "AOA.DIRECTOR",
                    f"„{form}“ ყველაზე ახლოსაა {other.name}-ის ხელმომწერთან, მხარესთან, "
                    f"რომელიც აქტში ვერ ამოიცნო: {match.name}-ის ხელმომწერს არ შედარდა",
                    f"'{form}' is nearest to the signatory the contract names for "
                    f"{other.name}, a party the act does not name in a form read as one: not "
                    f"compared with {match.name}'s",
                )
                continue
            where = self._places_of_director(p, form, spans)
            if where is None:
                continue  # every place of it is another party's own director, written right
            span, places = where
            # compared on the nominative, so a genitive in either document is no mistake
            # the nominative the contract writes, never one read off a genitive that drops a
            # vowel ("წერეთლის" is "წერეთელი", red team R5, AOA-R5-05)
            guess = best_match(lemma(form), attested_nominatives(theirs))
            diff = guess[1] if guess else None
            typo = diff is not None and diff.looks_like_typo
            hint_ka = " (როგორც ჩანს, შეცდომაა)" if typo else ""
            hint_en = " (looks like a misspelling)" if typo else ""
            # the correction in the act's own grammar, form by form
            expected = (
                keep_initial(form, in_case_of(form, guess[0], written=theirs)) if guess else None
            )
            if expected is not None:
                expected = with_patronymic(expected, patronymic)
            self._add(
                "AOA.DIRECTOR",
                "error",
                f"ხელმომწერი არ ემთხვევა ხელშეკრულებას: „{written}“, უნდა იყოს "
                f"„{expected}“{hint_ka}",
                f"signatory does not match the contract: '{written}', expected "
                f"'{expected}'{hint_en}",
                expected=expected,
                actual=written,
                span=span,
                places=places,
            )

    def _proxies(self, p: Party, match: Party) -> None:
        """AOA.DIRECTOR, as a warning: the act is signed for ``p`` under a power of attorney by
        someone the contract does not name. Not provably wrong - the client's rule says the
        authority must be checked - so the report asks for the power of attorney, at every place
        the attorney is named, and proposes no name to write instead.

        An attorney the contract names, written with a slip ("მზევინარ გოგიჩაიშვილო" for the
        contract's "მზევინარ გოგიჩაიშვილი"), is no other person: an error corrected to the
        contract's attorney, as for a director (red team R4, AOA-R4-12). The act's attorneys are
        the preamble's and the signature block's, as the party reader reads them
        (``Party.proxies``)."""
        named = [*match.directors, *match.proxies]
        for proxy in p.proxies:
            if any(_same_person(proxy, n) for n in named):
                continue
            if any(_slip_of_person(proxy, n) for n in match.proxies):
                self._person_findings(p, match, proxy, match.proxies)
                continue
            if any(names_match(proxy, n) for n in named):
                continue
            # a slip of the attorney the contract names is that attorney misspelt, corrected
            # as a director's slip is (red team R4, AOA-R4-12); anyone else is the warning below
            guess = best_match(lemma(proxy), [lemma(c) for c in match.proxies])
            if guess is not None and guess[1] is not None and guess[1].looks_like_typo:
                written: list[tuple[str, tuple[int, int] | None]] = [*self._written_forms(proxy)]
                for form, span in written or [(proxy, self._find_span(proxy))]:
                    expected = keep_initial(form, in_case_of(form, guess[0]))
                    self._add(
                        "AOA.DIRECTOR",
                        "error",
                        f"მინდობილობით მოქმედი პირი არ ემთხვევა ხელშეკრულებას: „{form}“, უნდა "
                        f"იყოს „{expected}“ (როგორც ჩანს, შეცდომაა)",
                        f"the attorney does not match the contract: '{form}', expected "
                        f"'{expected}' (looks like a misspelling)",
                        expected=expected,
                        actual=form,
                        span=span,
                    )
                continue
            director = match.directors[0] if match.directors else None
            ka_dir = f"; ხელშეკრულებით ხელმომწერია {director}" if director else ""
            en_dir = f"; the contract names {director}" if director else ""
            forms: list[tuple[str, tuple[int, int] | None]] = [*self._written_forms(proxy)]
            for form, span in forms or [(proxy, self._find_span(proxy))]:
                self._add(
                    "AOA.DIRECTOR",
                    "warning",
                    f"აქტს {match.name}-ის სახელით ხელს აწერს მინდობილობით მოქმედი პირი „{form}“"
                    f"{ka_dir} — გადაამოწმეთ მინდობილობა",
                    f"the act is signed for {match.name} under a power of attorney by '{form}'"
                    f"{en_dir} - check the power of attorney",
                    actual=form,
                    span=span,
                )

    def _id_code_party(self, p: Party, match: Party, owner: Party) -> None:
        """AOA.ID_CODE_PARTY: the act gives ``p`` (the contract's ``match``) the code the contract
        gives ``owner``. Marked from the party's name to the code, where the act's text allows:
        the code alone is also written, correctly, in the owner's own block."""
        span = self._span_in_act(p.id_span, p.id_code or "")
        named = self._span_in_act(p.span, "")
        if span is not None and named is not None and 0 <= span[0] - named[0] <= 200:
            span = (named[0], span[1])
        self._add(
            "AOA.ID_CODE_PARTY",
            "error",
            f"საიდენტიფიკაციო კოდი სხვა მხარისაა: აქტში {p.name}-ის კოდად წერია {p.id_code}, "
            f"რაც ხელშეკრულებით {owner.name}-ის კოდია; {match.name}-ის კოდია {match.id_code}",
            f"identification code belongs to another party: the act gives {p.name} the code "
            f"{p.id_code}, which is {owner.name}'s in the contract; {match.name}'s code is "
            f"{match.id_code}",
            expected=match.id_code,
            actual=p.id_code,
            span=span,
        )

    def rule_party_roles(self) -> None:
        """Client rules 10.1/10.2, the role: each party must be on the same side in the act as in
        the contract - the customer who pays, or the contractor who is paid (AOA.PARTY_ROLE).

        Sides, not words: "შემკვეთი" in one document and "დამკვეთი" in the other is the same
        customer. Roles are taken only from each document's own definition, "(შემდგომში
        „შემკვეთი“)". A signature block's bare role word stands above a name that a side-by-side
        layout may have put in the other column, and a guess must not produce an error; a party
        whose role either document does not define is named in a skip note instead.
        """
        if self.contract is None:  # run() calls the contract rules only with a contract
            raise RuntimeError(_NO_CONTRACT)
        parties = [q for q in self.contract.parties if _side(q.role)]
        if not parties:
            self._skip(
                "AOA.PARTY_ROLE",
                "ხელშეკრულებაში ვერ მოიძებნა არც დამკვეთი და არც შემსრულებელი (მაგ. „შემკვეთი“, "
                "„შემსრულებელი“), ამიტომ მხარეების როლები არ შედარდა",
                "the contract names no customer and no contractor (such as „შემკვეთი“, "
                "„შემსრულებელი“), so the parties' roles were not compared",
            )
            return
        matches = self._matched_parties()
        for q in parties:
            mine = [m.party for m in matches if m.match is q]
            if not mine:
                continue  # the party is not in the act at all: AOA.PARTY_MISSING says so
            if q.role_source != "defined":
                self._skip(
                    "AOA.PARTY_ROLE",
                    f"{q.name}: ხელშეკრულებაში მხარის როლი განსაზღვრული არ არის (შემდგომში „…“), "
                    "ამიტომ აქტში მისი როლი არ შედარდა",
                    f"{q.name}: the contract does not define this party's role (შემდგომში „…“), so "
                    "its role in the act was not compared",
                )
                continue
            defined = [p for p in mine if p.role_source == "defined" and _side(p.role)]
            if not defined:
                self._skip(
                    "AOA.PARTY_ROLE",
                    f"{q.name}: აქტში მხარის როლი განსაზღვრული არ არის (შემდგომში „…“), ამიტომ "
                    "ხელშეკრულებას არ შედარდა",
                    f"{q.name}: the act does not define this party's role (შემდგომში „…“), so it "
                    "was not compared with the contract",
                )
                continue
            for p in defined:
                a_side, c_side = _side(p.role), _side(q.role)
                if a_side is None or c_side is None or a_side == c_side:
                    continue
                self._add(
                    "AOA.PARTY_ROLE",
                    "error",
                    f"მხარის როლი არ ემთხვევა ხელშეკრულებას: {p.name} აქტში განსაზღვრულია როგორც "
                    f"„{p.role}“, ხელშეკრულებაში კი — როგორც „{q.role}“",
                    f"party role does not match the contract: the act defines {p.name} as "
                    f"'{p.role}' ({_SIDE_EN[a_side]}), the contract as '{q.role}' "
                    f"({_SIDE_EN[c_side]})",
                    expected=q.role,
                    actual=p.role,
                    span=self._span_in_act(p.role_span, p.role or ""),
                )

    def rule_party_missing(self) -> None:
        """Client rules 10.1/10.2, presence: the contract's customer and contractor must both be in
        the act (AOA.PARTY_MISSING).

        A party is there when the act names a party with its code or its name. When the act
        writes its code or its name only in a form not read as a party - no legal form, no
        quotes - the party is not missing, but could not be compared, and a skip note says so.
        """
        if self.contract is None:  # run() calls the contract rules only with a contract
            raise RuntimeError(_NO_CONTRACT)
        parties = [q for q in self.contract.parties if _side(q.role)]
        if not parties:
            self._skip(
                "AOA.PARTY_MISSING",
                "ხელშეკრულებაში ვერ მოიძებნა არც დამკვეთი და არც შემსრულებელი, ამიტომ არ "
                "შემოწმებულა, არის თუ არა ორივე მხარე აქტში",
                "the contract names no customer and no contractor, so whether both are in the act "
                "was not checked",
            )
            return
        for q in parties:
            side = _side(q.role)
            if side is None or any(_is_party(p, q) for p in self.aoa.parties):
                continue
            if _mentions(self.aoa.text, q):
                self._skip(
                    "AOA.PARTY_MISSING",
                    f"{q.name}: აქტში მისი კოდი ან დასახელება წერია, მაგრამ მხარედ (სამართლებრივი "
                    "ფორმა და დასახელება ბრჭყალებში) ვერ წაიკითხა, ამიტომ მისი მონაცემები არ "
                    "შედარდა",
                    f"{q.name}: the act gives this party's code or name, but not in a form read as "
                    "a party (a legal form and a quoted name), so its details were not compared",
                )
                continue
            code_ka = f", საიდენტიფიკაციო კოდი {q.id_code}" if q.id_code else ""
            code_en = f", identification code {q.id_code}" if q.id_code else ""
            self._add(
                "AOA.PARTY_MISSING",
                "error",
                f"ხელშეკრულების მხარე აქტში არ არის: {q.name} („{q.role}“{code_ka})",
                f"a party to the contract is missing from the act: {q.name} "
                f"({_SIDE_EN[side]}{code_en})",
                expected=f"{q.name} ({q.id_code})" if q.id_code else q.name,
            )

    def _compare_field(
        self,
        rule_id: str,
        ka_label: str,
        en_label: str,
        actual: str | None,
        expected: str | None,
        *,
        against: Literal["contract", "registry"] = "contract",
    ) -> None:
        # A side with no value is not compared: a signature block restates a party's name
        # without its address, and that is how acts are written, not a gap in the act.
        if not actual or not expected:
            return
        diff = classify_difference(actual, expected)
        if diff is None and rule_id in _NAME_RULES:
            # a stem is in no case: the name to write is the contract's, as it stands
            diff = self._name_cut_short(actual, expected)
        elif diff is not None and rule_id in _NAME_RULES:
            expected = in_case_of(actual, expected)
        if diff is None:
            return
        hint_ka = " (როგორც ჩანს, ორთოგრაფიული შეცდომაა)" if diff.looks_like_typo else ""
        hint_en = " (looks like a misspelling)" if diff.looks_like_typo else ""
        source_ka, source_en = _SOURCE[against]
        self._add(
            rule_id,
            "error",
            f"{ka_label} არ ემთხვევა {source_ka}: „{actual}“, უნდა იყოს „{expected}“{hint_ka}",
            f"{en_label} does not match {source_en}: '{actual}', expected '{expected}'{hint_en}",
            expected=expected,
            actual=actual,
            span=self._find_span(actual),
        )

    def _name_cut_short(self, actual: str, expected: str) -> Difference | None:
        """A name with a word's final "ი" dropped: "შპს „ბეტა სერვის“" for "ბეტა სერვისი".

        ``classify_difference`` takes each word in any of its forms, and the bare stem an
        attribute takes ("ლაზურ რეზიდენსი") is one of them - but a company's name is written
        whole: its words do not shed their ending inside the quotes. The stem is the name only
        where an ending is attached to it ("„ბეტა სერვის“-ის", "სერვის-თან"); written bare
        anywhere in the act, it is the name misspelt."""
        written, meant = normalize_name(actual).split(), normalize_name(expected).split()
        if len(written) != len(meant):
            return None
        cut = [
            (w, m)
            for w, m in zip(written, meant, strict=True)
            if w != m and is_georgian_word(w) and m == f"{w}ი" and w[-1] not in "აეიოუ"
        ]
        if not cut:
            return None
        text = self.aoa.text
        for w, _ in cut:
            places = list(re.finditer(rf"(?<![ა-ჰ]){re.escape(w)}(?![ა-ჰ])", text))
            # an ending attached after the stem, the closing quote between or not
            attached = [
                m for m in places if re.match(r"[“”\"»']?-?[ა-ჰ]", text[m.end() : m.end() + 3])
            ]
            if places and len(attached) < len(places):
                return Difference(actual=actual, expected=expected, kind="typo", words=tuple(cut))
        return None

    def rule_cadastral(self) -> None:
        """Every code the act names must be one the contract names, or lie inside one.

        A subset, not equality: a contract covering two plots may have an act for one of them,
        which the equality test this replaced reported as an error. And a unit code
        ("01.02.03.456.001.01.502") names part of its parcel ("01.02.03.456"), so either one
        written in the act matches the other in the contract (``_cadastral_covers``).
        """
        if self.contract is None:  # run() calls the contract rules only with a contract
            raise RuntimeError(_NO_CONTRACT)
        a = set(self.aoa.anchors(FactType.cadastral_code))
        c = set(self.contract.anchors(FactType.cadastral_code))
        if a and not c:
            self._skip(
                "AOA.CADASTRAL",
                "ხელშეკრულებაში საკადასტრო კოდი ვერ მოიძებნა, ამიტომ აქტის კოდი არ შედარდა",
                "no cadastral code found in the contract, so the act's code was not compared",
            )
            return
        # an act with no code at all needs no note here: CADASTRAL_PRESENT already reports it
        extra = sorted(x for x in a if not any(_cadastral_covers(x, y) for y in c))
        for x in extra:
            # the one plot this code stands for, as AOA.ID_CODES corrects a code; none when two
            # are as near, and the message lists them all
            self._add(
                "AOA.CADASTRAL",
                "error",
                f"საკადასტრო კოდი განსხვავდება: {x} vs ხელშეკრულება {', '.join(sorted(c))}",
                f"cadastral code differs: {x} vs contract {', '.join(sorted(c))}",
                expected=_closest(x, sorted(c)),
                actual=x,
                span=self._find_span(x),
            )

    def rule_project(self) -> None:
        """Project names carry different case endings in the two documents, so compare stems,
        word by word (``_project_names_match``)."""
        if self.contract is None:  # run() calls the contract rules only with a contract
            raise RuntimeError(_NO_CONTRACT)
        a = self.aoa.anchors(FactType.project_name)
        c = self.contract.anchors(FactType.project_name)
        if not a or not c:
            self._skip(
                "AOA.PROJECT_NAME",
                "პროექტის დასახელება ორივე დოკუმენტში მითითებული არ არის",
                "project name not stated in both documents",
            )
            return
        for name in a:
            if not name_stem(name) or any(_project_names_match(name, x) for x in c):
                continue
            stated = lemma(c[0])
            self._add(
                "AOA.PROJECT_NAME",
                "error",
                f"პროექტის დასახელება არ ემთხვევა ხელშეკრულებას: „{name}“, "
                f"ხელშეკრულებაში — „{stated}“",
                f"project name does not match the contract: '{name}', contract says '{stated}'",
                expected=in_case_of(name, c[0]),
                actual=name,
                span=self._find_span(name),
            )

    def rule_subject(self) -> None:
        """Client rule 10.3: the act restates the contract's subject (``aoa_scope``).

        Only a number that differs, in a clause that otherwise reads as the contract's, is an
        error. A subject in other words is marked for a person to read, as a note: the rules
        cannot tell a paraphrase from another service, and a correct act must not fail on one.
        The model review, when one was asked for, reads such a note (``aoa_llm.decide``).
        The project and the plot the clause names are left to AOA.PROJECT_NAME and AOA.CADASTRAL.
        """
        reading = self._scope_reading()
        contract = reading.contract_subject
        if contract is None:
            self._skip(
                "AOA.SUBJECT",
                "ხელშეკრულებაში საგნის პუნქტი (ხელშეკრულების საგანი … (შემდგომში „მომსახურება“)) "
                "ვერ მოიძებნა, ამიტომ აქტის საგანი არ შედარდა",
                "no subject clause ('ხელშეკრულების საგანი … (შემდგომში „მომსახურება“)') was found "
                "in the contract, so the act's subject was not compared",
            )
            return
        act = reading.act_subject
        if act is None:
            self._skip(
                "AOA.SUBJECT",
                "აქტში ხელშეკრულების საგანი (… საგანს წარმოადგენს … (შემდგომში „მომსახურება“)) "
                "გამეორებული არ არის, ამიტომ არ შედარდა",
                "the act does not restate the contract's subject ('… საგანს წარმოადგენს … "
                "(შემდგომში „მომსახურება“)'), so it was not compared",
            )
            return
        if not act.stems:
            self._skip(
                "AOA.SUBJECT",
                "აქტის საგანში მხოლოდ პროექტი და საკადასტრო კოდი წერია, რომლებიც ცალკე შედარდა",
                "the act's subject names only the project and the plot, which are compared on "
                "their own",
            )
            return
        found = reading.subject
        if found is None:  # pragma: no cover - read_scope aligns two clauses with words
            return
        if self._as_corrected(found, [contract]).verdict == "match":
            return  # a misspelled word, which AOA.SPELLING reports
        decision = self._decision(SUBJECT_ID, found)
        if decision.severity is None:
            return
        if found.verdict == "number":
            differ, stated = ", ".join(found.differ), ", ".join(contract.numbers)
            self._add(
                "AOA.SUBJECT",
                decision.severity,
                f"ხელშეკრულების საგანში რიცხვი განსხვავდება: აქტში {differ}, ხელშეკრულებაში "
                f"{stated}",
                f"a number in the subject differs from the contract: {differ} in the act, "
                f"{stated} in the contract",
                expected=stated,
                actual=differ,
                span=act.span,
            )
            return
        shown_act, shown_contract = _shorten(act.text), _shorten(contract.text)
        if found.share >= MATCH_SHARE:  # the words are the contract's; a number is the act's own
            differ = ", ".join(found.differ)
            ka = (
                f"აქტში ხელშეკრულების საგანს რიცხვი ({differ}) აქვს დამატებული, რომელიც "
                f"ხელშეკრულებაში არ წერია — გადაამოწმეთ: ხელშეკრულებაში „{shown_contract}“"
            )
            en = (
                f"the act's subject states a number ({differ}) the contract's does not - check "
                f"it: the contract says '{shown_contract}'"
            )
        else:
            ka = (
                f"აქტში ხელშეკრულების საგანი სხვა სიტყვებითაა აღწერილი — გადაამოწმეთ: აქტში "
                f"„{shown_act}“, ხელშეკრულებაში „{shown_contract}“"
            )
            en = (
                f"the act words the subject differently from the contract - check it: the act "
                f"says '{shown_act}', the contract '{shown_contract}'"
            )
        self._scope_note("AOA.SUBJECT", SUBJECT_ID, decision, ka, en, act.span)

    def rule_services(self) -> None:
        """Client rule 10.6, the services: each service the act lists must be one the contract
        lists (``aoa_scope``). The act may list fewer - an act covers a month of the work - so
        a service the act leaves out is not reported.

        As for the subject, only a number that differs in an item that otherwise reads as the
        contract's is an error; an item whose words the contract's list does not clearly hold is
        a note for a person to read, and for the model review when one was asked for. A contract
        that lists no services, or an act that does not restate them, is named in a skip note.
        """
        reading = self._scope_reading()
        if not reading.contract_services:
            self._skip(
                "AOA.SERVICES",
                "ხელშეკრულებაში მომსახურების ჩამონათვალი (სია „… ჩააბაროს:“, „კერძოდ:“ ან "
                "„მოიცავს:“ ქვეშ) ვერ მოიძებნა, ამიტომ აქტის მომსახურება არ შედარდა",
                "no list of services (a list under '… ჩააბაროს:', 'კერძოდ:' or 'მოიცავს:') was "
                "found in the contract, so the act's services were not compared",
            )
            return
        if not reading.act_services:
            self._skip(
                "AOA.SERVICES",
                "აქტში მომსახურების ჩამონათვალი (სია „მომსახურების აღწერა:“ ან „კერძოდ:“ ქვეშ) "
                "ვერ მოიძებნა, ამიტომ ხელშეკრულებას არ შედარდა",
                "the act lists no services (a list under 'მომსახურების აღწერა:' or 'კერძოდ:'), so "
                "none were compared with the contract's",
            )
            return
        for n, found in enumerate(reading.services, 1):
            if self._as_corrected(found, reading.contract_services).verdict == "match":
                continue  # a misspelled word, which AOA.SPELLING reports
            decision = self._decision(service_id(n), found)
            if decision.severity is None:
                continue
            item, best = found.item, found.best
            if found.verdict == "number" and best is not None:
                self._add(
                    "AOA.SERVICES",
                    decision.severity,
                    f"ჩამოთვლილ მომსახურებაში რიცხვი განსხვავდება ხელშეკრულებისგან: აქტში "
                    f"„{item.text}“, ხელშეკრულებაში „{best.text}“",
                    f"a number in a listed service differs from the contract: '{item.text}' in "
                    f"the act, '{best.text}' in the contract",
                    expected=best.text,
                    actual=item.text,
                    span=self._find_span(item.text),
                )
                continue
            if best is not None and found.share >= MATCH_SHARE:
                # the words are the contract's item's; a number is the act's own
                ka = (
                    f"აქტში ჩამოთვლილ მომსახურებას რიცხვი აქვს დამატებული, რომელიც ხელშეკრულებაში "
                    f"არ წერია: აქტში „{item.text}“, ხელშეკრულებაში „{best.text}“ — გადაამოწმეთ"
                )
                en = (
                    f"a service the act lists states a number the contract's does not: "
                    f"'{item.text}' in the act, '{best.text}' in the contract - check it"
                )
            else:
                closest_ka = closest_en = ""
                if best is not None and found.share > 0:
                    closest_ka = f"; ყველაზე ახლოს არის „{best.text}“"
                    closest_en = f"; the closest is '{best.text}'"
                ka = (
                    "აქტში ჩამოთვლილი მომსახურება ხელშეკრულების ჩამონათვალში ცალსახად არ ჩანს: "
                    f"„{item.text}“{closest_ka} — გადაამოწმეთ"
                )
                en = (
                    f"a service the act lists is not clearly in the contract's list: "
                    f"'{item.text}'{closest_en} - check it"
                )
            self._scope_note(
                "AOA.SERVICES", service_id(n), decision, ka, en, self._find_span(item.text)
            )

    def rule_bank(self) -> None:
        """Every account and bank code in the act must be in the contract, and belong together.

        A subset, not equality: a contract lists both parties' bank details and the act only
        the contractor's, which the equality test this replaced reported as an error on every
        such act. A subset alone is too loose, though - the contractor's account with the
        customer's bank code passes it - so accounts and codes are also checked as pairs
        (``_bank_pairing``).
        """
        if self.contract is None:  # run() calls the contract rules only with a contract
            raise RuntimeError(_NO_CONTRACT)
        mismatched = False
        for unit, ka, en in (
            ("IBAN", "ანგარიშის ნომერი", "account number"),
            ("SWIFT", "ბანკის კოდი", "bank code"),
        ):
            a, c = self.aoa.bank(unit), self.contract.bank(unit)
            # a BIC and its "XXX" branch form name one office (ISO 9362): compared as one code
            known = {_bank_key(x) for x in c}
            extra = sorted(x for x in a if _bank_key(x) not in known)
            if unit == "SWIFT" and c:
                # a real branch of a bank the contract names by its 8-character code: the same
                # institution, so a warning naming the branch (red team R5, AOA-R5-13, policy)
                branches = [x for x in extra if _branch_of(x, known)]
                extra = [x for x in extra if x not in branches]
                for x in branches:
                    self._branch_warning(x)
            if a and c and extra:
                mismatched = True
                payee = self._payee_bank(unit)
                for x in extra:
                    hint_ka = hint_en = ""
                    if unit == "IBAN" and any(iban_is_valid(y) for y in c) and not iban_is_valid(x):
                        hint_ka, hint_en = (
                            " (აქტის IBAN არავალიდურია)",
                            " (the act's IBAN is not valid)",
                        )
                    # the one contract value this one stands for - a slip in the contractor's
                    # account is not corrected to a list that holds the customer's own
                    self._add(
                        "AOA.BANK",
                        "error",
                        f"{ka} განსხვავდება: {x} vs ხელშეკრულება {', '.join(sorted(c))}{hint_ka}",
                        f"{en} differs: {x} vs contract {', '.join(sorted(c))}{hint_en}",
                        expected=_closest(x, sorted(c), preferred=payee),
                        actual=x,
                        span=self._account_span(x) if unit == "IBAN" else self._find_span(x),
                    )
            elif not a and unit == "IBAN":
                self._add(
                    "AOA.BANK",
                    "warning",
                    "აქტში საბანკო ანგარიში არ წერია",
                    "the act does not state a bank account",
                )
        self._sub_accounts()
        if not mismatched:
            self._bank_owner()
            self._bank_pairing()

    def _branch_warning(self, code: str) -> None:
        """AOA.BANK, a warning: the act's bank code is a branch (its last three characters) of
        the bank the contract names by the 8-character code. The institution is the same and
        the account is the IBAN's, so the payment is not misdirected; the branch is named for a
        person to confirm."""
        branch, bank = code[8:].upper(), code[:8].upper()
        stated = sorted(self.contract.bank("SWIFT")) if self.contract is not None else []
        expected = next((x for x in stated if _bank_key(x) == bank), bank)
        self._add(
            "AOA.BANK",
            "warning",
            f"ბანკის კოდს დამატებული აქვს ფილიალის კოდი {branch}: {code} vs ხელშეკრულება "
            f"{expected} — ბანკი იგივეა, შეამოწმეთ ფილიალი",
            f"the bank code adds the branch code {branch}: {code} vs contract {expected} - the "
            "same bank; check the branch",
            expected=expected,
            actual=code,
            span=self._find_span(code),
        )

    def _sub_accounts(self) -> None:
        """AOA.BANK, a warning: one account whose currency sub-account the act and the contract
        name differently ("…17GEL" against "…17USD"). The account is the same, so the IBAN is not
        wrong (red team R2, AOA-R2-03), but the payment may go to the other currency."""
        if self.contract is None:  # the bank rules run only with a contract
            return
        stated = _sub_accounts(self.contract)
        for hit in self.aoa.bank_hits("IBAN"):
            mine = _sub_account(hit)
            theirs = stated.get(hit.value, set())
            if mine and theirs and mine not in theirs:
                expected = sorted(theirs)[0]
                self._add(
                    "AOA.BANK",
                    "warning",
                    f"ანგარიშის ვალუტა განსხვავდება: {hit.value}{mine} vs ხელშეკრულება "
                    f"{hit.value}{expected}",
                    f"the account's currency differs: {hit.value}{mine} vs contract "
                    f"{hit.value}{expected}",
                    expected=expected,
                    actual=mine,
                    span=(hit.end - len(mine), hit.end),
                )

    def _payee_bank(self, unit: str) -> frozenset[str]:
        """The contract's bank details the contractor is paid on: the accounts in a paid party's
        requisites block, and the bank codes a block pairs with them. Empty when none can be
        attributed."""
        if self.contract is None:  # the bank rules run only with a contract
            return frozenset()
        accounts = frozenset(
            a for q in self.contract.parties if q.role in _PAYEE_ROLES for a in q.accounts
        )
        if unit == "IBAN":
            return accounts
        pairs = _bank_pairs_with_hits(self.contract) or []
        return frozenset(code for iban, code, _ in pairs if iban in accounts)

    def _bank_owner(self) -> None:
        """The act's account must be the contractor's, not the customer's.

        The subset test cannot see this: the customer's account is in the contract too. Each
        contract account is attributed to the party whose requisites block states it; the act
        is flagged only when every account it states belongs to a paying party (დამკვეთი /
        შემკვეთი / მყიდველი) and the contract does give the contractor an account. When any
        account cannot be attributed, or a role was not read, nothing is claimed.
        """
        if self.contract is None:  # run() calls the contract rules only with a contract
            raise RuntimeError(_NO_CONTRACT)
        act_accounts = sorted(self.aoa.bank("IBAN"))
        if not act_accounts:
            return
        # A party's block runs to the next party's name, so the last party's block also holds
        # later clauses, and a side-by-side requisites table puts both accounts in one block.
        # An account stated in more than one block has no reliable owner: claim nothing then.
        holders: dict[str, list[Party]] = {}
        for party in self.contract.parties:
            for account in party.accounts:
                holders.setdefault(account, []).append(party)
        if any(len(ps) != 1 for ps in holders.values()):
            return
        owners = [holders[a][0] if a in holders else None for a in act_accounts]
        if any(o is None or o.role is None for o in owners):
            return
        payee_accounts = sorted(
            a for q in self.contract.parties if q.role in _PAYEE_ROLES for a in q.accounts
        )
        if payee_accounts and all(o is not None and o.role in _PAYER_ROLES for o in owners):
            self._add(
                "AOA.BANK",
                "error",
                f"აქტში მითითებული ანგარიში ხელშეკრულებით დამკვეთს ეკუთვნის: "
                f"{', '.join(act_accounts)}; შემსრულებლის ანგარიშია {', '.join(payee_accounts)}",
                f"the act's account belongs to the customer in the contract: "
                f"{', '.join(act_accounts)}; the contractor's account is "
                f"{', '.join(payee_accounts)}",
                expected=", ".join(payee_accounts),
                actual=", ".join(act_accounts),
                span=self._account_span(act_accounts[0]),
            )

    def _bank_pairing(self) -> None:
        """The bank code beside each account must be the one the contract gives that account.

        Pairs are read only where a document lays its details out one party at a time - a
        code immediately followed by its account, or the other way round, with no other bank
        detail in between. A two-column requisites table serialises as "code A, code B,
        account 1, account 2", where nearness pairs the wrong things and would call a correct
        act wrong; such a layout is skipped and said so. Only needed when the contract has
        more than one bank code: with one, the subset test above already decides.
        """
        if self.contract is None:  # run() calls the contract rules only with a contract
            raise RuntimeError(_NO_CONTRACT)
        if len({_bank_key(x) for x in self.contract.bank("SWIFT")}) < 2:
            return
        contract_pairs = _bank_pairs_with_hits(self.contract)
        act_pairs = _bank_pairs_with_hits(self.aoa)
        if contract_pairs is None or act_pairs is None:
            self._skip(
                "AOA.BANK_PAIRING",
                "საბანკო რეკვიზიტები ცხრილის სახითაა, ამიტომ ვერ დადგინდა, რომელი ბანკის კოდი "
                "რომელ ანგარიშს ეკუთვნის; შემოწმდა მხოლოდ, რომ ყველა მათგანი ხელშეკრულებაშია",
                "bank details are laid out side by side, so which code belongs to which account "
                "could not be read; only that each appears in the contract was checked",
            )
            return
        c_pairs = {iban: code for iban, code, _ in contract_pairs}
        for iban, code, hit in act_pairs:
            want = c_pairs.get(iban)
            if want is None or _bank_key(want) == _bank_key(code):
                continue
            if _branch_of(code, {_bank_key(want)}):
                continue  # a branch of the very bank: rule_bank's warning says so
            self._add(
                "AOA.BANK",
                "error",
                f"ბანკის კოდი {code} ხელშეკრულებაში სხვა ანგარიშს ეკუთვნის; ანგარიშისთვის {iban} "
                f"ხელშეკრულებაში მითითებულია {want}",
                f"bank code {code} belongs to another account in the contract; for account "
                f"{iban} the contract gives {want}",
                expected=want,
                actual=code,
                span=(hit.start, hit.end),
            )

    def rule_fee(self) -> None:
        """Client rule 10.6, the amount: the act's fee must be a price the contract states.

        A price, not any amount (``amount_roles``): an act whose fee equals the contract's
        advance or penalty states the wrong fee, and the finding names the amount it took.
        Amounts match on their currency as well as their figure, so 100 USD against a price of
        100 GEL is reported as a currency that differs. The act's advance and remainder lines
        are its fee in parts, not a second fee, and are set aside when they add up to it
        (``_fee_parts_add_up``); when they do not, each is compared like a fee.
        """
        if self.contract is None:  # run() calls the contract rules only with a contract
            raise RuntimeError(_NO_CONTRACT)
        act = [r for r in self.aoa.amounts() if r.role not in _NOT_THE_FEE]
        # a price table's total whose currency only the column header names ("ღირებულება
        # (ლარი)") is a fee the act states too (red team R3, AOA-R3-09)
        table_totals, table_lines = header_money(self.aoa.text)
        read = [r.money.span for r in self.aoa.amounts()]
        act += [
            RoledAmount(money=m, role="price")
            for m in table_totals
            if not any(a < m.span[1] and m.span[0] < b for a, b in read)
        ]
        running = [r.money for r in self.aoa.amounts() if r.role == "running"]
        if not act and running:
            # every figure the act states is a running one: the note names them (GOLD_SPEC 3),
            # never a "no fee" warning (red team R5, AOA-R5-08)
            self._running_note(running)
            return
        if not act:
            # A fee whose currency stands in another table cell is not read as money: say that
            # no fee could be read and let a person look, rather than call the act wrong.
            self._add(
                "AOA.FEE",
                "warning",
                "აქტში საფასური ვერ წაიკითხა — შეამოწმეთ, მითითებულია თუ არა თანხა ვალუტით",
                "no fee could be read in the act - check that it states the amount with its "
                "currency",
            )
            return
        contract = self.contract.amounts()
        prices = self._contract_prices()
        if not prices:
            self._skip(
                "AOA.FEE",
                "ხელშეკრულებაში ვერ მოიძებნა ღირებულება შესადარებლად",
                "no price found in the contract to compare against",
            )
            return
        if running:
            self._running_note(running)
        if table_lines:
            shown = ", ".join(m.raw for m in table_lines[:5])
            self._skip(
                "AOA.FEE",
                f"აქტის ცხრილში თანხები ({shown}) ვალუტას მხოლოდ სვეტის სათაურში ასახელებს და "
                "ჯამის სტრიქონი არ აქვს: ხელშეკრულების ღირებულებას არ შედარდა",
                f"the act's table gives amounts ({shown}) whose currency only the column header "
                "names, with no total row: they were not compared with the contract's price",
            )
        # the fee before tax, beside the tax that makes it the fee, is the fee in parts; a
        # figure labelled before VAT beside the fee is compared with the price before VAT
        nets = self._net_parts()
        before_vat = [r for r in self._before_vat() if r not in nets]
        fees = [r for r in act if r.role in (None, "price") and r not in nets + before_vat]
        parts = [r for r in act if r.role in ("advance", "remainder")]
        matched = [r.money for r in fees if _states(prices, r.money)]
        wrong = [r for r in fees if not _states(prices, r.money)]
        # an advance stated only as a share ("ავანსის (30%) გამოქვითვით"): the remainder is the
        # rest of the fee (red team R5, AOA-R5-02)
        shares = [
            share.percent
            for doc in (self.aoa, self.contract)
            for share in doc.advance(in_act=doc is self.aoa).shares
            if 0 < share.percent < 100
        ]
        if parts and not _fee_parts_add_up(parts, matched, prices, shares):
            # a part the contract itself states (its advance) is not the one that is wrong
            wrong += [
                r
                for r in parts
                if not _states(prices, r.money)
                and not _states([c.money for c in contract if c.role == r.role], r.money)
                and not (r.role == "advance" and _share_of(r.money, prices, shares))
            ]
        for r in sorted(wrong, key=lambda r: r.money.span):
            self._fee_finding(r, prices, contract)
        for r in fees:
            if r not in wrong:
                self._fee_words_finding(r, prices)
        for r in before_vat:
            self._net_finding(r, prices, contract)
        self._vat_amount_findings(fees, [*nets, *before_vat], prices)

    def _running_note(self, running: list[Money]) -> None:
        """The skip note for the act's running figures of the contract: the work done to date,
        the contract's balance or what was paid before - no fee of this act, and nothing the
        contract's price can confirm (red team R4, AOA-R4-08)."""
        shown = ", ".join(m.raw for m in running[:5])
        self._skip(
            "AOA.FEE",
            f"აქტში მითითებული თანხები ({shown}) ხელშეკრულების მიმდინარე მაჩვენებლებია "
            "(დღემდე შესრულებული, დარჩენილი ან უკვე გადახდილი) და არა ამ აქტის საფასური: "
            "ხელშეკრულების ღირებულებას არ შედარდა",
            f"the act's amounts {shown} are running figures of the contract (done to date, "
            "the balance left or paid before), not this act's fee: they were not compared "
            "with the contract's price",
        )

    def _net_finding(
        self, r: RoledAmount, prices: list[Money], contract: list[RoledAmount]
    ) -> None:
        """A figure the act gives as its fee before VAT (``_before_vat``) against the contract's
        prices before VAT: a price the contract states as such, or a price with VAT taken off at
        a rate the documents allow. Its correction is the price before VAT, as the act writes the
        figure - never the price itself, which written there would make the act wrong."""
        m = r.money
        same = list(dict.fromkeys(p.value for p in prices if p.currency == m.currency))
        if not same:
            self._fee_finding(r, prices, contract)  # another currency: the fee rule says so
            return
        rates = self._vat_rates()
        if any(
            amounts_equal(m.value, p) or any(_plus_vat(m.value, p, rate) for rate in rates)
            for p in same
        ):
            return
        rate = self._vat_rate()
        nets = list(dict.fromkeys(round(p / (1 + rate), 2) for p in same))
        meant = nets[0] if len(nets) == 1 else _nearest_price(m.value, nets)
        span = self._fee_span(m)
        fix = None if meant is None else _fee_like(meant, self.aoa.text[span[0] : span[1]])
        actual = _money_text(m)
        shown = ", ".join(f"{n:,.2f} {m.currency}" for n in nets)
        self._add(
            "AOA.FEE",
            "error",
            f"საფასური დღგ-ს გარეშე {actual} არ ემთხვევა ხელშეკრულებით განსაზღვრულ ღირებულებას "
            f"დღგ-ს გარეშე ({shown})",
            f"the fee before VAT {actual} matches no contract price before VAT ({shown})",
            expected=fix,
            actual=actual,
            span=span,
        )

    def _vat_amount_findings(
        self, fees: list[RoledAmount], nets: list[RoledAmount], prices: list[Money]
    ) -> None:
        """The VAT amount of a breakdown that does not add up: the act states its fee, the fee
        before VAT and the VAT, and net + VAT is not the fee (GOLD_SPEC 3). When the fee is the
        contract's price, the one wrong is the VAT when the net is right, and the finding is on
        the VAT with the fee less the net as its correction (red team R3, AOA-R3-08); a wrong
        net is ``_net_finding``'s, and when net + VAT make the fee the VAT follows the net."""
        rates = self._vat_rates()
        for t in (r for r in self.aoa.amounts() if r.role == "tax"):
            cur = t.money.currency
            gross = [g for g in fees if g.money.currency == cur and _states(prices, g.money)]
            parts = [n for n in nets if n.money.currency == cur]
            if not gross or not parts:
                continue
            at = t.money.span[0]
            g = min(gross, key=lambda r: abs(r.money.span[0] - at)).money
            n = min(parts, key=lambda r: abs(r.money.span[0] - at)).money
            if amounts_equal(n.value + t.money.value, g.value):
                continue
            if not any(_plus_vat(n.value, g.value, rate) for rate in rates):
                continue  # the net is wrong too: its own finding says so
            vat = round(g.value - n.value, 2)
            if amounts_equal(t.money.value, vat):
                continue
            actual = _money_text(t.money)
            fix = _fee_like(vat, self.aoa.text[t.money.span[0] : t.money.span[1]])
            self._add(
                "AOA.FEE",
                "error",
                f"დღგ-ს თანხა {actual} არ ემთხვევა: {_money_text(n)} დღგ-ს გარეშე და {actual} "
                f"დღგ ერთად არ შეადგენს საფასურს {_money_text(g)}; დღგ უნდა იყოს "
                f"{vat:,.2f} {cur}",
                f"the VAT amount {actual} does not fit: {_money_text(n)} before VAT plus {actual} "
                f"VAT is not the fee {_money_text(g)}; the VAT is {vat:,.2f} {cur}",
                expected=fix,
                actual=actual,
                span=t.money.span,
            )

    def _fee_words_finding(self, r: RoledAmount, prices: list[Money]) -> None:
        """A fee whose figure is the contract's price while its words state another amount: the
        act states two fees, and the one in words is not the contract's. Marked on the words,
        with the contract's price in words to write there. Words that do not read as a number
        are a misspelling, AOA.AMOUNT_WORDS's to report."""
        m = r.money
        pair = self._pair_of(m)
        if pair is None or not pair.numeral or pair.spelled is None:
            return
        same = [p for p in prices if p.currency == m.currency]
        if any(amounts_equal(p.value, pair.spelled) for p in same):
            return
        price = next((p for p in same if amounts_equal(p.value, m.value)), None)
        if price is None:
            return
        name_ka, _, name_en = _FEE_LABEL[r.role]
        stated, shown = _plain(pair.spelled), _money_text(price)
        self._add(
            "AOA.FEE",
            "error",
            f"{name_ka} სიტყვიერად „{pair.words}“ ({stated}) არ ემთხვევა ხელშეკრულებით "
            f"განსაზღვრულ ღირებულებას ({shown})",
            f"the {name_en} in words '{pair.words}' ({stated}) matches no price in the contract "
            f"({shown})",
            expected=words_for(price.value),
            actual=pair.words,
            span=pair.words_span,
        )

    def _fee_finding(
        self, r: RoledAmount, prices: list[Money], contract: list[RoledAmount]
    ) -> None:
        m = r.money
        name_ka, genitive_ka, name_en = _FEE_LABEL[r.role]
        actual = _money_text(m)
        other = next(
            (p for p in prices if p.currency != m.currency and amounts_equal(p.value, m.value)),
            None,
        )
        if other is not None:
            shown = _money_text(other)
            # the correction is the fee as the act writes it, in the contract's currency (red
            # team R4, AOA-R4-11); the machine form only when the act's currency cannot be
            # rewritten
            span = self._fee_span(m)
            written = self.aoa.text[span[0] : span[1]]
            fix = with_currency(_fee_like(other.value, written), other.currency)
            self._add(
                "AOA.FEE",
                "error",
                f"{genitive_ka} ვალუტა განსხვავდება ხელშეკრულებისგან: აქტში {actual}, "
                f"ხელშეკრულებაში {shown}",
                f"the {name_en}'s currency differs from the contract: the act says {actual}, "
                f"the contract {shown}",
                expected=fix or shown,
                actual=actual,
                span=span if fix else m.span,
            )
            return
        shown = ", ".join(dict.fromkeys(_money_text(p) for p in prices))
        span = self._fee_span(m)
        same_currency = list(dict.fromkeys(p.value for p in prices if p.currency == m.currency))
        # the price it should have been: the only one, or the one it is a slip of. The
        # correction says it the way the act writes its fee, words and all, so it can be typed
        # over the marked words as it stands; with no one price meant, the message lists them
        meant = (
            same_currency[0] if len(same_currency) == 1 else _nearest_price(m.value, same_currency)
        )
        fix = None if meant is None else _fee_like(meant, self.aoa.text[span[0] : span[1]])
        hint_ka = hint_en = ""
        for c in contract:
            if c.role is not None and c.role in _ROLE_NAME and _states([c.money], m):
                role_ka, role_en = _ROLE_NAME[c.role]
                hint_ka = f"; ის ემთხვევა ხელშეკრულებით განსაზღვრულ {role_ka}"
                hint_en = f"; it equals {role_en} in the contract"
                break
        self._add(
            "AOA.FEE",
            "error",
            f"{name_ka} {actual} არ ემთხვევა ხელშეკრულებით განსაზღვრულ ღირებულებას "
            f"({shown}){hint_ka}",
            f"{name_en} {actual} matches no price in the contract ({shown}){hint_en}",
            expected=fix,
            actual=actual,
            span=span,
        )

    def _fee_span(self, m: Money) -> tuple[int, int]:
        """Where a fee is written, its words in brackets included wherever they stand: after
        the figure, after the currency ("100 ლარს (ასი)"), or after a figure the currency comes
        before ("GEL 100 (ასი)")."""
        pair = self._pair_of(m)
        if pair is None:
            return m.span
        return min(m.span[0], pair.span[0]), max(m.span[1], pair.span[1])

    def _pair_of(self, m: Money) -> DigitsAndWords | None:
        """The "digits (words)" pair whose figure is the amount ``m``'s."""
        return next(
            (
                p
                for p in digits_and_words(self.aoa.text)
                if m.span[0] <= p.digits_span[0] and p.digits_span[1] <= m.span[1]
            ),
            None,
        )

    def rule_period_in_term(self) -> None:
        """Client rule 10.5: the service period must lie inside the contract's term.

        The term is the contract's performance window ("შეასრულოს მომსახურება ... -დან ...
        ჩათვლით"), or failing that its validity - but only a validity with a last day, since
        "until the obligations are fulfilled" bounds nothing (``_term_window``). When the
        contract states neither, the rule says so. It used to fall back to the contract's first
        and last dates, and to any date before "ჩათვლით", which let a payment deadline or "დღგ-ს
        ჩათვლით" stretch the term.
        """
        if self.contract is None:  # run() calls the contract rules only with a contract
            raise RuntimeError(_NO_CONTRACT)
        reading = self._period()
        period_hits, period = reading.hits, reading.dates
        if not period:
            if self._unread("AOA.PERIOD", "შესრულების პერიოდი", "the execution period"):
                return
            self._add(
                "AOA.PERIOD",
                "warning",
                "აქტში შესრულების პერიოდი ვერ მოიძებნა",
                "the execution period could not be found in the act",
            )
            return
        window = _term_window(self.contract.term())
        if reading.repaired is not None:
            self._period_year_repair(reading, window)
            return
        if window is None:
            self._skip(
                "AOA.PERIOD_IN_TERM",
                "ხელშეკრულებაში ვერ მოიძებნა არც შესრულების ვადა და არც მოქმედების ვადის "
                "დასასრული, ამიტომ შესრულების პერიოდი არ შედარდა",
                "the contract states neither a performance period nor an end to its validity, "
                "so the execution period was not compared with it",
            )
            return
        lo, hi, kind = window
        # anchor on the first date that lies outside, as the act writes it
        outside = [
            h for d, h in period_hits if (lo is not None and d < lo) or (hi is not None and d > hi)
        ]
        if not outside:
            return
        name_ka, name_en = _WINDOW_NAME[kind]
        shown = _window_text(lo, hi)
        first, last = min(h.start for _, h in period_hits), max(h.end for _, h in period_hits)
        written = self.aoa.text[first:last]
        shift = _year_shift_into(period, window)
        if shift is not None:
            # every date of the period lands in the term when its year is corrected: that is
            # the mistake, and the correction is the period as written with the right year
            fixed = _shift_years(written, [d.year for d in period], shift)
            self._add(
                "AOA.PERIOD_IN_TERM",
                "error",
                f"შესრულების პერიოდი {min(period)}..{max(period)} სცდება {name_ka} {shown}; "
                f"როგორც ჩანს, წელი არასწორადაა მითითებული: „{fixed}“",
                f"execution period {min(period)}..{max(period)} falls outside {name_en} {shown}; "
                f"the year looks mistyped: '{fixed}'",
                expected=fixed,
                actual=written,
                span=(first, last),
            )
            return
        repair = _endpoint_year_into(period_hits, window)
        if repair is not None:
            # one end's year typed wrong ("2025 წლის 1 ივნისიდან 2026 წლის 30 ივნისის"): the
            # period as written, corrected in that end only - the other end's same year stays
            written, fixed = self._with_year_moved(reading, *repair)
            if fixed != written:
                self._add(
                    "AOA.PERIOD_IN_TERM",
                    "error",
                    f"შესრულების პერიოდი {min(period)}..{max(period)} სცდება {name_ka} {shown}; "
                    f"როგორც ჩანს, წელი არასწორადაა მითითებული: „{fixed}“",
                    f"execution period {min(period)}..{max(period)} falls outside {name_en} "
                    f"{shown}; the year looks mistyped: '{fixed}'",
                    expected=fixed,
                    actual=written,
                    span=(first, last),
                )
                return
        # no one year is the mistake: the correction is the term itself, written the way the
        # act writes its period, over the whole period as written (red team R3, AOA-R3-11)
        written, fixed = self._period_as_term(period_hits, lo, hi)
        self._add(
            "AOA.PERIOD_IN_TERM",
            "error",
            f"შესრულების პერიოდი {min(period)}..{max(period)} სცდება {name_ka} {shown}",
            f"execution period {min(period)}..{max(period)} falls outside {name_en} {shown}",
            expected=fixed,
            actual=written,
            span=(first, last),
        )

    def _period_as_term(
        self, hits: list[tuple[date, RuleHit]], lo: date | None, hi: date | None
    ) -> tuple[str, str]:
        """The period as the act writes it, and the same text with its ends moved to the term's
        ``lo`` and ``hi``, each end in its own date style - its case, its postposition
        ("-დან"), its separators. An end the term leaves open stays as written. A month written
        with no day ("2026 წლის ივნისის თვეში") has no day to move: the term is written out in
        the act's long style instead ("2026 წლის 1 ივნისიდან 2027 წლის 31 მაისის ჩათვლით")."""
        text = self.aoa.text
        ordered = sorted((h for _, h in hits), key=lambda h: (h.start, h.end))
        a, b = ordered[0], ordered[-1]
        written = self.aoa.as_written(a.start, b.end)

        def moved(d: date | None, h: RuleHit, dated: RuleHit) -> str:
            if d is None:
                return self.aoa.as_written(h.start, h.end)
            following = text[h.end : h.end + 40]
            out = render_date_like(d, h.raw_text, following=following)
            if out != d.isoformat() or _ISO_WRITTEN.match(h.raw_text):
                return out
            # an end written without its year ("25.05-30.06.2026", "25 მაისიდან 30 ივნისის
            # ჩათვლით, 2026 წ."): its own day and month, its "-დან" kept, with the year written
            # as the other end writes it - or failing that, in the other end's style; never ISO
            # unless the act wrote ISO (red team R4, AOA-R4-09)
            for example in (_with_year_of(h.raw_text, dated.raw_text), dated.raw_text):
                if example is not None and _YEAR_WRITTEN.search(example):
                    out = render_date_like(d, example, following=following)
                    if out != d.isoformat():
                        return out
            return out

        if (a.start, a.end) == (b.start, b.end):
            words = re.findall(rf"[{GEORGIAN}]+", a.raw_text)
            if any(w in MONTH_FORMS for w in words) and lo and hi:
                # the month's genitive stem, "ივნის" of "ივნისის", carries both ends' forms
                start = render_date_like(lo, f"{lo.year} წლის 1 მაისიდან")
                end = render_date_like(hi, f"{hi.year} წლის 1 მაისის", following="ჩათვლით")
                return written, f"{start} {end} ჩათვლით"
            return written, f"{moved(lo, a, a)} - {moved(hi, a, a)}"
        fixed = moved(lo, a, b) + self.aoa.as_written(a.end, b.start) + moved(hi, b, a)
        return written, fixed

    def _period_year_repair(
        self, reading: _Period, window: tuple[date | None, date | None, str] | None
    ) -> None:
        """A period that ends before it starts, and the one year that orders it (``_period``):
        the period as written is the mistake, and the correction is it with that year put
        right. Held to the contract's term it is AOA.PERIOD_IN_TERM's; with no term to hold it
        to, the act's own date chose the repair, and it is AOA.PERIOD's."""
        if reading.repaired is None:  # pragma: no cover - called only with a repair
            return
        written, fixed = self._with_year_moved(reading, *reading.repaired)
        first = min(h.start for _, h in reading.hits)
        last = max(h.end for _, h in reading.hits)
        shown = f"{min(reading.dates)}..{max(reading.dates)}"
        ka = (
            f"შესრულების პერიოდი მთავრდება მანამ, სანამ დაიწყება: „{written}“; როგორც ჩანს, "
            f"წელი არასწორადაა მითითებული — უნდა იყოს „{fixed}“ ({shown})"
        )
        en = (
            f"the execution period ends before it starts: '{written}'; the year looks mistyped - "
            f"it should read '{fixed}' ({shown})"
        )
        if window is not None:
            shown_window = _window_text(window[0], window[1])
            ka += f", ხელშეკრულების ვადის ({shown_window}) ფარგლებში"
            en += f", inside {_WINDOW_NAME[window[2]][1]} {shown_window}"
        self._add(
            "AOA.PERIOD_IN_TERM" if window is not None else "AOA.PERIOD",
            "error",
            ka,
            en,
            expected=fixed,
            actual=written,
            span=(first, last),
        )

    def _with_year_moved(self, reading: _Period, hit: RuleHit, shift: int) -> tuple[str, str]:
        """The period as the act writes it, and the same text with the year of the one end
        ``hit`` moved by ``shift`` - in that end only, so a year both ends write is changed once."""
        first = min(h.start for _, h in reading.hits)
        last = max(h.end for _, h in reading.hits)
        year = str(next(d.year for d, h in reading.hits if h is hit))
        endpoint = re.sub(
            rf"(?<!\d){year}(?!\d)", str(int(year) + shift), self.aoa.as_written(hit.start, hit.end)
        )
        written = self.aoa.as_written(first, last)
        fixed = (
            self.aoa.as_written(first, hit.start) + endpoint + self.aoa.as_written(hit.end, last)
        )
        return written, fixed

    def rule_act_date_in_term(self) -> None:
        """Client rule 10.5, the act itself: it must be dated inside the contract's term.

        Dated before the contract was signed, or after a validity that states its last day, is an
        error. Dated after the performance window only is a warning: the contract is still in
        force. Rule 4 dates an act on the last day of the month, which for a window ending
        mid-month falls after it, and when that is the case the message says so - which rule
        gives way is for the client to decide.
        """
        if self.contract is None:  # run() calls the contract rules only with a contract
            raise RuntimeError(_NO_CONTRACT)
        act = self._act_date()
        if act is None:
            self._skip(
                "AOA.ACT_DATE_IN_TERM",
                "აქტის გაფორმების თარიღი ვერ მოიძებნა, ამიტომ ხელშეკრულების ვადას არ შედარდა",
                "the act's date could not be found, so it was not compared with the contract's "
                "term",
            )
            return
        term = self.contract.term()
        valid_until = term.validity.end if term.validity else None
        perform_until = term.performance.end if term.performance else None
        if term.signed is None and valid_until is None and perform_until is None:
            self._skip(
                "AOA.ACT_DATE_IN_TERM",
                "ხელშეკრულებაში ვერ მოიძებნა არც გაფორმების თარიღი და არც ვადის დასასრული, "
                "ამიტომ აქტის თარიღი არ შედარდა",
                "the contract states neither when it was signed nor when its term ends, so the "
                "act's date was not compared with it",
            )
            return
        d, hit = act
        allowed = _window_text(term.signed, valid_until or perform_until)
        if term.signed is not None and d < term.signed:
            self._add(
                "AOA.ACT_DATE_IN_TERM",
                "error",
                f"აქტი დათარიღებულია {d}-ით, ხელშეკრულების გაფორმებამდე ({term.signed})",
                f"the act is dated {d}, before the contract was signed ({term.signed})",
                expected=allowed,
                actual=str(d),
                span=(hit.start, hit.end),
                places=self._act_date_places(hit),
            )
        elif valid_until is not None and d > valid_until:
            self._add(
                "AOA.ACT_DATE_IN_TERM",
                "error",
                f"აქტი დათარიღებულია {d}-ით, ხელშეკრულების მოქმედების ვადის ამოწურვის "
                f"({valid_until}) შემდეგ",
                f"the act is dated {d}, after the contract term ended ({valid_until})",
                expected=allowed,
                actual=str(d),
                span=(hit.start, hit.end),
                places=self._act_date_places(hit),
            )
        elif perform_until is not None and d > perform_until:
            ka = (
                f"აქტი დათარიღებულია {d}-ით, ხელშეკრულების შესრულების ვადის ({perform_until}) "
                "შემდეგ"
            )
            en = f"the act is dated {d}, after the contract's performance period ended " + (
                f"({perform_until})"
            )
            if d == _last_day(perform_until):
                ka += (
                    "; მე-4 წესით აქტი თვის ბოლო დღით თარიღდება, რაც ამ შემთხვევაში ვადის შემდეგ "
                    "მოდის — გადაამოწმეთ, რომელი წესი მოქმედებს"
                )
                en += (
                    "; rule 4 dates an act on the last day of the month, which here falls after "
                    "the term - check which of the two applies"
                )
            self._add(
                "AOA.ACT_DATE_IN_TERM",
                "warning",
                ka,
                en,
                expected=_window_text(term.signed, perform_until),
                actual=str(d),
                span=(hit.start, hit.end),
                places=self._act_date_places(hit),
                confidence=0.8,
            )

    def rule_payment_terms(self) -> None:
        """Client rule 10.7, the terms: the act's payment deadline, method and advance must be
        the contract's (AOA.PAYMENT_DEADLINE, AOA.PAYMENT_METHOD, AOA.PAYMENT_ADVANCE).

        A part is compared only when both documents state it. An act need not restate its
        contract's terms, and a contract that states none has nothing to compare with, so a part
        only one of them states is named in one skip note, not reported. The advance is the
        exception, because an act that records one records a payment: when the contract sets out
        how it is paid - a deadline or a method - and provides for no advance, that is a warning.
        """
        if self.contract is None:  # run() calls the contract rules only with a contract
            raise RuntimeError(_NO_CONTRACT)
        contract, act = self.contract.payment_terms(), self.aoa.payment_terms()
        c_advance = self.contract.advance(in_act=False)
        a_advance = self.aoa.advance(in_act=True)
        if not contract.stated and not c_advance.mentioned:
            self._skip(
                "AOA.PAYMENT_*",
                "ხელშეკრულებაში გადახდის პირობები (ვადა, ანგარიშსწორების ფორმა, ავანსი) ვერ "
                "მოიძებნა, ამიტომ აქტის პირობები არ შედარდა",
                "no payment terms (deadline, method, advance) were found in the contract, so the "
                "act's were not compared",
            )
            return
        one_sided: list[tuple[str, bool]] = []  # (part, whether the contract is the one)
        if contract.deadlines and act.deadlines:
            self._payment_deadlines(contract.deadlines, act.deadlines)
        elif contract.deadlines or act.deadlines:
            one_sided.append(("deadline", bool(contract.deadlines)))
        if contract.method and act.method:
            self._payment_method(contract.method, act.method)
        elif contract.method or act.method:
            one_sided.append(("method", contract.method is not None))
        if a_advance.stated and not c_advance.mentioned:
            # the contract states payment terms here (see above), and none is an advance
            self._add(
                "AOA.PAYMENT_ADVANCE",
                "warning",
                "აქტში ავანსი წერია, ხელშეკრულების ანგარიშსწორების პირობებში კი ავანსი "
                "გათვალისწინებული არ არის",
                "the act states an advance, but the contract's payment terms provide for none",
                span=a_advance.anchor,
                confidence=0.8,
            )
        elif a_advance.stated:
            self._payment_advance(c_advance, a_advance)
        elif c_advance.stated and not a_advance.mentioned:
            one_sided.append(("advance", True))
        if one_sided:
            self._skip(
                "AOA.PAYMENT_*",
                "მხოლოდ ერთ დოკუმენტშია მითითებული, ამიტომ არ შედარდა: "
                + ", ".join(f"{_PAYMENT_PART[p][0]} ({_ONLY_IN[c][0]})" for p, c in one_sided),
                "stated in only one of the two documents, so not compared: "
                + ", ".join(f"{_PAYMENT_PART[p][1]} ({_ONLY_IN[c][1]})" for p, c in one_sided),
            )

    def _payment_deadlines(self, contract: tuple[Deadline, ...], act: tuple[Deadline, ...]) -> None:
        """Every deadline the act states must be one the contract states, with the same days.

        A different number or unit is an error, and so is working days against calendar days.
        A deadline that says which days count against one that does not is a warning: "5 დღე"
        may be meant as either.
        """
        stated = {(c.count, c.unit, c.qualifier) for c in contract}
        for d in act:
            if (d.count, d.unit, d.qualifier) in stated:
                continue
            same = next((c for c in contract if (c.count, c.unit) == (d.count, d.unit)), None)
            if same is None:
                expected = ", ".join(dict.fromkeys(c.raw for c in contract))
                values = ", ".join(dict.fromkeys(c.value for c in contract))
                self._add(
                    "AOA.PAYMENT_DEADLINE",
                    "error",
                    f"გადახდის ვადა არ ემთხვევა ხელშეკრულებას: აქტში „{d.raw}“, ხელშეკრულებაში "
                    f"„{expected}“",
                    f"payment deadline differs from the contract: {d.value} in the act, "
                    f"{values} in the contract",
                    expected=expected,
                    actual=d.raw,
                    span=d.span,
                )
            elif d.qualifier and same.qualifier:
                self._add(
                    "AOA.PAYMENT_DEADLINE",
                    "error",
                    f"გადახდის ვადაში დღეები სხვაგვარადაა დათვლილი: აქტში — "
                    f"{_DAYS_KA[d.qualifier]} („{d.raw}“), ხელშეკრულებაში — "
                    f"{_DAYS_KA[same.qualifier]} („{same.raw}“)",
                    f"the payment deadline counts {d.qualifier} days in the act ('{d.raw}') and "
                    f"{same.qualifier} days in the contract ('{same.raw}')",
                    expected=same.raw,
                    actual=d.raw,
                    span=d.span,
                )
            else:
                known = d.qualifier or same.qualifier or ""
                where_ka, where_en = (
                    ("აქტში", "the act")
                    if d.qualifier
                    else (
                        "ხელშეკრულებაში",
                        "the contract",
                    )
                )
                self._add(
                    "AOA.PAYMENT_DEADLINE",
                    "warning",
                    f"გადახდის ვადა მხოლოდ {where_ka} ითვლება {_DAYS_KA[known]} დღეებით: აქტში "
                    f"„{d.raw}“, ხელშეკრულებაში „{same.raw}“",
                    f"only {where_en} says the payment deadline counts {known} days: "
                    f"'{d.raw}' in the act, '{same.raw}' in the contract",
                    expected=same.raw,
                    actual=d.raw,
                    span=d.span,
                    confidence=0.8,
                )

    def _payment_method(self, contract: MethodHit, act: MethodHit) -> None:
        if contract.method == act.method:
            return
        c_ka, c_en = _METHOD_NAME[contract.method]
        a_ka, a_en = _METHOD_NAME[act.method]
        self._add(
            "AOA.PAYMENT_METHOD",
            "error",
            f"ანგარიშსწორების ფორმა არ ემთხვევა ხელშეკრულებას: აქტში — {a_ka} („{act.text}“), "
            f"ხელშეკრულებაში — {c_ka} („{contract.text}“)",
            f"payment method differs from the contract: {a_en} ('{act.text}') in the act, "
            f"{c_en} ('{contract.text}') in the contract",
            expected=contract.text,
            actual=act.text,
            span=act.span,
        )

    def _payment_advance(self, contract: AdvanceTerms, act: AdvanceTerms) -> None:
        """The act's advance against the contract's, when both speak of one.

        An error when the contract rules one out, or when both state its amount (in one
        currency) or its share of the price and the two differ. An amount against a share is
        not compared: which price the share is of is not always plain.
        """
        if contract.excluded and not (contract.payments or contract.shares):
            self._add(
                "AOA.PAYMENT_ADVANCE",
                "error",
                "აქტში ავანსი წერია, ხელშეკრულებით კი ავანსი გამორიცხულია",
                "the act states an advance, which the contract rules out",
                span=act.anchor,
            )
            return
        planned = [p.money for p in contract.payments]
        for p in act.payments:
            same_currency = [m for m in planned if m.currency == p.money.currency]
            if not same_currency or _states(same_currency, p.money):
                continue
            expected = ", ".join(dict.fromkeys(_money_text(m) for m in same_currency))
            actual = _money_text(p.money)
            self._add(
                "AOA.PAYMENT_ADVANCE",
                "error",
                f"ავანსი არ ემთხვევა ხელშეკრულებას: აქტში {actual}, ხელშეკრულებაში {expected}",
                f"the advance differs from the contract: {actual} in the act, {expected} in the "
                "contract",
                expected=expected,
                actual=actual,
                span=p.money.span,
            )
        shares = {s.percent for s in contract.shares}
        for s in act.shares:
            if not shares or s.percent in shares:
                continue
            expected = ", ".join(f"{x:g}%" for x in sorted(shares))
            self._add(
                "AOA.PAYMENT_ADVANCE",
                "error",
                f"ავანსის წილი არ ემთხვევა ხელშეკრულებას: აქტში {s.percent:g}%, "
                f"ხელშეკრულებაში {expected}",
                f"the advance's share differs from the contract: {s.percent:g}% in the act, "
                f"{expected} in the contract",
                expected=expected,
                actual=f"{s.percent:g}%",
                span=s.span,
            )

    def rule_registry(self) -> None:
        """Client rule 1: verify name / address / directors against the business registry.

        Each party, and each of its fields, is either compared or named in a skip note saying
        why not - a party with no identification code cannot be looked up, and a field missing
        from the act or from the registry export has nothing to be compared with.
        """
        if self.registry is None:
            if self.contract is not None:
                self._skip(
                    "AOA.REGISTRY",
                    "სამეწარმეო რეესტრის ამონაწერი ჩატვირთული არ არის; მხარეების მონაცემები "
                    "შედარდა მხოლოდ ხელშეკრულებას",
                    "no business-registry snapshot loaded; party details were compared against "
                    "the contract only",
                )
            else:
                self._skip(
                    "AOA.REGISTRY",
                    "სამეწარმეო რეესტრის ამონაწერი ჩატვირთული არ არის და ხელშეკრულებაც არ არის "
                    "ატვირთული; მხარეების მონაცემები არ შემოწმებულა",
                    "no business-registry snapshot loaded and no contract supplied; party "
                    "details were not verified",
                )
            return
        if not self.aoa.parties:
            self._skip(
                "AOA.REGISTRY",
                "აქტში მხარე (სამართლებრივი ფორმა და დასახელება) ვერ მოიძებნა, ამიტომ რეესტრს "
                "არაფერი შედარებია",
                "no party (a legal form and a name) was found in the act, so nothing was "
                "checked against the registry",
            )
            return
        # A party written with another party's code (AOA.ID_CODE_PARTY): that code's entry is
        # the other company, and would "correct" this one's name, address and director into it.
        misplaced = (
            [m.party for m in self._matched_parties() if m.code_of is not None]
            if self.contract is not None
            else []
        )
        for p in self.aoa.parties:
            if not p.id_code:
                self._skip(
                    "AOA.REGISTRY",
                    f"{p.name}: აქტში საიდენტიფიკაციო კოდი არ წერია, ამიტომ რეესტრში ვერ მოიძებნა",
                    f"{p.name}: the act gives no identification code, so the party was not "
                    "looked up in the registry",
                )
                continue
            if any(p is q for q in misplaced):
                self._skip(
                    "AOA.REGISTRY",
                    f"{p.name}: აქტში ამ მხარეს სხვა მხარის საიდენტიფიკაციო კოდი აქვს "
                    "(AOA.ID_CODE_PARTY), ამიტომ მისი მონაცემები ამ კოდის რეესტრის ჩანაწერს არ "
                    "შედარდა",
                    f"{p.name}: the act gives this party another party's identification code "
                    "(AOA.ID_CODE_PARTY), so its details were not compared with that code's "
                    "registry entry",
                )
                continue
            entry = self.registry.lookup(p.id_code)  # type: ignore[attr-defined]
            if entry is None:
                self._add(
                    "AOA.REGISTRY",
                    "warning",
                    f"საიდენტიფიკაციო კოდი {p.id_code} რეესტრში ვერ მოიძებნა",
                    f"identification code {p.id_code} not found in the registry snapshot",
                    actual=p.id_code,
                )
                continue
            self._compare_field(
                "AOA.REGISTRY_NAME", "დასახელება", "name", p.name, entry.name, against="registry"
            )
            if not p.address:
                self._skip(
                    "AOA.REGISTRY_ADDRESS",
                    f"{p.name}: აქტში იურიდიული მისამართი არ წერია, ამიტომ რეესტრს არ შედარდა",
                    f"{p.name}: the act states no legal address, so none was compared with the "
                    "registry",
                )
            elif not entry.address:
                self._skip(
                    "AOA.REGISTRY_ADDRESS",
                    f"{p.name}: რეესტრის ამონაწერში მისამართი არ არის, ამიტომ აქტის მისამართი "
                    "არ შემოწმებულა",
                    f"{p.name}: the registry snapshot has no address for this company, so the "
                    "act's address was not checked",
                )
            else:
                self._compare_field(
                    "AOA.REGISTRY_ADDRESS",
                    "მისამართი",
                    "address",
                    p.address,
                    entry.address,
                    against="registry",
                )
            self._registry_directors(p, entry.directors)

    def _registry_directors(self, p: Party, registered: tuple[str, ...]) -> None:
        """The act's signatories for ``p`` against the registry's directors, or why not."""
        if not p.directors:
            self._skip(
                "AOA.REGISTRY_DIRECTOR",
                f"{p.name}: აქტში ხელმომწერი (დირექტორი) არ არის მითითებული, ამიტომ რეესტრს "
                "არ შედარდა",
                f"{p.name}: the act names no signatory (director), so none was compared with the "
                "registry",
            )
            return
        if not registered:
            self._skip(
                "AOA.REGISTRY_DIRECTOR",
                f"{p.name}: რეესტრის ამონაწერში დირექტორი მითითებული არ არის, ამიტომ ხელმომწერი "
                "არ შემოწმებულა",
                f"{p.name}: the registry snapshot names no director for this company, so the "
                "signatory was not checked",
            )
            return
        for d in p.directors:
            if not any(_same_person(d, x) for x in registered):
                self._add(
                    "AOA.REGISTRY_DIRECTOR",
                    "error",
                    f"ხელმომწერი რეესტრის მონაცემებს არ ემთხვევა: „{d}“ "
                    f"(რეესტრი: {', '.join(registered)})",
                    f"signatory does not match the registry: '{d}' "
                    f"(registry: {', '.join(registered)})",
                    expected=", ".join(registered),
                    actual=d,
                )


#: How far apart an account and its bank code may be and still be read as one block.
_PAIR_WINDOW = 400


def _bank_pairs_with_hits(view: DocumentView) -> list[tuple[str, str, AnchorHit]] | None:
    """(account, its bank code, the code's hit) per party block, or None when not block-shaped.

    Block-shaped means the bank details, in document order, fall into adjacent code/account
    couples - "code, account, code, account" or "account, code, ...". Anything else (two codes
    in a row, an odd one out, a couple too far apart to be one block) is None: pairing it by
    distance is a guess, and a guess must not produce an error.
    """
    hits = sorted(view.bank_hits("SWIFT") + view.bank_hits("IBAN"), key=lambda h: h.start)
    if len(hits) % 2:
        return None
    out: list[tuple[str, str, AnchorHit]] = []
    for first, second in zip(hits[::2], hits[1::2], strict=True):
        if first.unit == second.unit or second.start - first.end > _PAIR_WINDOW:
            return None
        code, account = (first, second) if first.unit == "SWIFT" else (second, first)
        out.append((account.value, code.value, code))
    return out


def _side(role: str | None) -> Side | None:
    """The side a role word puts a party on: the customer who pays, or the contractor paid."""
    if role in _PAYER_ROLES:
        return "payer"
    if role in _PAYEE_ROLES:
        return "payee"
    return None


def _is_party(p: Party, q: Party) -> bool:
    """Whether the act's ``p`` is the contract's ``q``: the same code, or the same name."""
    return bool(
        (p.id_code and p.id_code == q.id_code)
        or (p.name and q.name and names_match(p.name, q.name))
    )


def _same_person(written: str, named: str) -> bool:
    """Whether the act's ``written`` is the contract's (or registry's) ``named``: the same words,
    each in some grammatical form of the other's - the surname an inflection of the nominative
    ``named`` stands for, the first name one of its forms ("ნოდარ" and "ნოდარი" alike).

    ``names_match`` strips a final "ე" or "ი" as an ending, so it takes "მელაძი" for "მელაძე";
    neither is a form of the other: "-ძე" gives "-ძის", "-ძეს", "-ძემ", and "-შვილი" gives
    "-შვილის", "-შვილმა". A name in another script is compared as ``names_match`` does.

    A first name written as its initial ("ნ. ბერიძე") is the named person's when it is the
    first letter of theirs; the surname is then held to the same rule (red team R3, AOA-R3-07):
    "ნ. ბერიძი" is no form of "ნოდარ ბერიძე"."""
    a, b = normalize_name(written).split(), normalize_name(named).split()
    if not a or len(a) != len(b):
        return False
    if not all(is_georgian_word(w) for w in (*a, *b)):
        return names_match(written, named)
    if not all(_same_first(x, y) for x, y in zip(a[:-1], b[:-1], strict=True)):
        return False
    return lemma_word(b[-1]) in word_lemmas(a[-1])


def _slip_of_person(written: str, named: str) -> bool:
    """Whether ``written`` is ``named`` with a slip: the same number of words, each a form of
    the other's or one slip from it (``_near_word``), and not the same person."""
    a, b = normalize_name(written).split(), normalize_name(named).split()
    if not a or len(a) != len(b) or _same_person(written, named):
        return False
    return all(_near_word(x, y) for x, y in zip(a, b, strict=True))


def _same_first(written: str, named: str) -> bool:
    """A first name (or a patronymic) the same word in some form, or its initial."""
    if len(written) == 1 or len(named) == 1:
        return written[0] == named[0]
    return same_word(written, named)


def _near_word(word: str, target: str) -> bool:
    """``word`` is ``target`` in another form, or one slip from it once both are put in the
    nominative: "მელაძი" beside "მელაძის". Short words must be forms of each other."""
    if same_word(word, target):
        return True
    if len(target) < 4 or not is_georgian_word(word):
        return False
    return damerau(lemma_word(word), lemma_word(target), cap=1) <= 1


def _agreement(p: Party, q: Party) -> int:
    """How much of the act's block for ``p`` - its address and its signatories - is ``q``'s."""
    address = bool(p.address and q.address and classify_difference(p.address, q.address) is None)
    return address + sum(any(names_match(d, x) for x in q.directors) for d in p.directors)


def _match_party(p: Party, parties: list[Party]) -> _PartyMatch:
    """The contract party the act's ``p`` is (see ``AoaChecker._matched_parties``).

    Only a name that points at a party with a code of its own, another code, contradicts the
    code: a contract may write one party twice, the second time with no code or with the name
    spelled otherwise, and those two records are one party, not a conflict.
    """
    by_code = next((q for q in parties if p.id_code and q.id_code == p.id_code), None)
    by_name = next((q for q in parties if p.name and q.name and names_match(p.name, q.name)), None)
    if by_code is None:
        return _PartyMatch(p, by_name or _slipped_name_of(p, parties))
    if (
        by_name is None
        or by_name is by_code
        or not by_name.id_code
        or by_name.id_code == p.id_code
        or _agreement(p, by_code) > _agreement(p, by_name)
    ):
        return _PartyMatch(p, by_code)
    return _PartyMatch(p, by_name, code_of=by_code)


def _slipped_name_of(p: Party, parties: list[Party]) -> Party | None:
    """The contract party whose name the act's ``p`` - a party with no code, as a signature block
    writes one - writes with a slip: a letter typed wrong or a word's final "ი" dropped (red
    team R5, AOA-R5-04). Matched to it, its name is compared as the same slip in the preamble
    is, and corrected to the contract's. Only one party's name may be that close; two are told
    apart by the side the role word above the name gives. A name nothing like any party's is
    matched to none: another company has no correction."""
    if p.id_code or not p.name:
        return None

    def slip_of(name: str) -> bool:
        diff = classify_difference(p.name or "", name)
        if diff is not None:
            return diff.looks_like_typo
        written, meant = normalize_name(p.name or "").split(), normalize_name(name).split()
        return len(written) == len(meant) and any(
            m == f"{w}ი" for w, m in zip(written, meant, strict=True)
        )

    near = [q for q in parties if q.name and slip_of(q.name)]
    if len(near) > 1:
        near = [q for q in near if _side(q.role) is not None and _side(q.role) == _side(p.role)]
    return near[0] if len(near) == 1 else None


#: Legal forms, as ``normalize_name`` leaves them, that a party's name starts with: the name
#: proper is what follows them.
_LEGAL_FORM_WORDS = frozenset(
    {
        *("შპს", "სს", "ააიპ", "სსიპ", "კს", "სპს", "ი/მ", "ი", "მ"),
        *("ინდ", "ინდივიდუალური", "მეწარმე", "მეწარმ"),
    }
)


def _mention_starts(text: str, q: Party) -> list[int]:
    """Where ``text`` names the party ``q``: its identification code as a number of its own,
    the words of its name after its legal form (any case ending, any quotes between), and its
    role label as a word of its own ("შემკვეთი", not "შემკვეთის" in the body's prose)."""
    out: list[int] = []
    if q.id_code:
        code = rf"(?<![0-9A-Za-z]){re.escape(q.id_code)}(?![0-9A-Za-z])"
        out += [m.start() for m in re.finditer(code, text)]
    words = [
        w
        for w in normalize_name(q.name or "").split()
        if w not in _LEGAL_FORM_WORDS and any(c.isalpha() for c in w)
    ]
    if words:
        pattern = r"[\W_]+".join(rf"{re.escape(w)}[{GEORGIAN}]*" for w in words)
        out += [m.start() for m in re.finditer(rf"(?<!\w){pattern}", text, re.IGNORECASE)]
    if q.role and is_georgian_word(q.role):
        role = rf"(?<![{GEORGIAN}]){re.escape(q.role)}(?![{GEORGIAN}])"
        out += [m.start() for m in re.finditer(role, text)]
    return out


def _mentions(text: str, q: Party) -> bool:
    """Whether ``text`` names the party ``q`` in any form: its identification code as a number
    of its own - not digits inside an account number - or the words of its name, in order and
    with any case ending, after its legal form."""
    if q.id_code and re.search(rf"(?<![0-9A-Za-z]){re.escape(q.id_code)}(?![0-9A-Za-z])", text):
        return True
    words = [
        w
        for w in name_stem(q.name or "").split()
        if w not in _LEGAL_FORM_WORDS and any(c.isalpha() for c in w)
    ]
    if not words:
        return False
    pattern = r"\W+".join(rf"{re.escape(w)}\w*" for w in words)
    return re.search(rf"(?<!\w){pattern}", normalize_name(text)) is not None


def _cadastral_covers(a: str, c: str) -> bool:
    """Two cadastral codes name the same land when they are equal or one lies under the other.

    A code lies under another when it continues it after a dot: "01.02.03.456.001" is inside
    "01.02.03.456", while "01.02.03.4567" would be another parcel (and is no code at all).
    """
    return a == c or a.startswith(c + ".") or c.startswith(a + ".")


_FEE_WORDS = re.compile(r"\(\s*[ა-ჰ][ა-ჰ0-9 ,.]*\)")


#: The words after a fee's figure, the currency standing before them or not: " (ასი)",
#: " ლარს (ასი)", " ₾ (ასი)".
_FEE_WORDS_AFTER = re.compile(rf"(\s*(?:{CURRENCY}\s*)?)({_FEE_WORDS.pattern})")
#: Tetri written in figures after the lari: " ლარი და 50 თეთრი".
_TETRI_AFTER = re.compile(rf"(\s*{CURRENCY}\s+)და\s+(\d{{1,2}})(\s*თეთრ[ა-ჰ]*)")


def _fee_like(value: float, written: str) -> str:
    """``value`` as the act writes its fee: "700 (შვიდასი) ლარს" with 100 gives "100 (ასი) ლარს".

    The figure keeps the act's grouping and decimals, the words are written anew wherever they
    stand ("700 ლარს (შვიდასი)" too), and the currency word keeps its case. Tetri written after
    the lari ("700 ლარი და 50 თეთრი") are the value's own, and dropped when it has none. Text
    the pattern does not recognise yields the bare figure.
    """
    figure = re.search(FIGURE_TETRI, written)
    if figure is None:
        return _plain(value)
    rest = written[figure.end() :]
    tetri = _TETRI_AFTER.match(rest)
    if tetri is not None:
        lari, cents = int(round(value, 2)), round(value * 100) % 100
        out = written[: figure.start()] + render_figure_like(lari, figure.group(0))
        if cents:
            width = len(tetri.group(2))
            out += f"{tetri.group(1)}და {cents:0{width}d}{tetri.group(3)}"
        else:
            out += tetri.group(1).rstrip()
        return (out + rest[tetri.end() :]).strip()
    lead = written[: figure.start()]
    words_at = _words_before(lead, len(lead.rstrip(" (")))
    if words_at is not None and lead.rstrip().endswith("("):
        # the words first, the figure in brackets: "სამი ათას (3 000) ლარს" (red team R5)
        lead = lead[:words_at] + words_like(value, lead[words_at:].rstrip(" (")) + " ("
    out = lead + render_figure_like(value, figure.group(0))
    words = _FEE_WORDS_AFTER.match(rest)
    if words is not None:
        # the bracket's own currency and tetri words kept: "(ასი ლარი და 00 თეთრი)"
        inner = words.group(2)[1:-1].strip()
        out += f"{words.group(1)}({words_like(value, inner)})" + rest[words.end() :]
    else:
        out += rest
    return out.strip()


def _sub_account(hit: AnchorHit) -> str:
    """The currency code written straight after an account ("GE…17GEL"), or ""."""
    return re.sub(r"\s", "", hit.raw_text)[len(hit.value) :]


def _sub_accounts(view: DocumentView) -> dict[str, set[str]]:
    """Each account's currency sub-accounts as a document writes them."""
    out: dict[str, set[str]] = {}
    for hit in view.bank_hits("IBAN"):
        if tail := _sub_account(hit):
            out.setdefault(hit.value, set()).add(tail)
    return out


def _nearest_price(value: float, prices: Sequence[float]) -> float | None:
    """The one of ``prices`` a fee of ``value`` is a slip of, for its correction: the fewest
    digit edits (a swap of two neighbours is one), a slip or two at most, then the smallest
    difference. None when two are equally near or none is near: the message lists them."""

    def key(p: float) -> tuple[int, float]:
        written, meant = _plain(value).replace(".", ""), _plain(p).replace(".", "")
        edits = damerau(written, meant, cap=max(len(written), len(meant)))
        return edits, abs(value - p) / max(abs(p), 0.01)

    ranked = sorted((key(p), p) for p in dict.fromkeys(prices))
    if not ranked or ranked[0][0][0] > 2:
        return None
    if len(ranked) > 1 and ranked[1][0] == ranked[0][0]:
        return None
    return ranked[0][1]


def _bank_key(code: str) -> str:
    """A bank detail as it is compared: a BIC's 11-character form with the branch "XXX" is its
    8-character form, the institution's primary office (ISO 9362). Anything else as it is."""
    code = code.upper()
    return code[:8] if len(code) == 11 and code.endswith("XXX") else code


def _branch_of(code: str, banks: set[str]) -> str | None:
    """The branch code of an 11-character BIC whose first eight characters - the institution,
    its country and its location (ISO 9362) - are one of ``banks`` (8-character forms, as
    ``_bank_key`` gives them), with a real branch, not "XXX". None for anything else."""
    code = code.upper()
    if len(code) == 11 and not code.endswith("XXX") and code[:8] in banks:
        return code[8:]
    return None


def _closest(
    value: str, candidates: Sequence[str], *, preferred: frozenset[str] = frozenset()
) -> str | None:
    """The one of ``candidates`` that ``value`` was meant to be, for a correction: the nearest by
    edit distance (a swap of two neighbours is one edit), when it is near - a slip or two, a
    fifth of the characters at most - and nearer than any other. A tie is settled by
    ``preferred`` (the contractor's own account, the one an act is paid on) when that leaves
    one. A value near none of them is not a slip of any: then the only candidate, or the only
    preferred one, is meant, and otherwise nothing is proposed rather than a guess."""
    if not candidates:
        return None
    distance = {
        c: damerau(value.upper(), c.upper(), cap=max(len(value), len(c))) for c in candidates
    }
    best = min(distance.values())
    tied = [c for c in candidates if distance[c] == best]
    if best <= max(2, len(value) // 5):
        if len(tied) == 1:
            return tied[0]
        mine = [c for c in tied if c in preferred]
        return mine[0] if len(mine) == 1 else None
    mine = [c for c in candidates if c in preferred]
    if len(mine) == 1:
        return mine[0]
    return candidates[0] if len(candidates) == 1 else None


def _digit_distance(a: str, b: str) -> int:
    """How many digits apart two codes are: digits changed for codes of one length, and for
    two lengths the digits dropped, doubled or changed (red team R3, AOA-R3-01)."""
    if len(a) != len(b):
        return edit_distance(a, b, cap=len(a) + len(b))
    return sum(x != y for x, y in zip(a, b, strict=True))


def _plain(value: float) -> str:
    """A figure as a person writes it: "2400", "48750.5" - never "2.4e+03"."""
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _money_text(m: Money) -> str:
    return f"{m.value:,.2f} {m.currency}"


def _shorten(text: str, limit: int = 160) -> str:
    """A long clause cut down for a message; the finding marks all of it in the document."""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


#: The multiplication sign of the invoice formula. Escaped: in the source it reads as an "x".
_TIMES = "\u00d7"


def _fx_day(term: InvoiceTerm) -> tuple[str, str]:
    """The day whose NBG rate an invoice term takes, as the messages name it: (Georgian, English).
    A day nobody states is named for what it is, so the reader knows which rate to look up."""
    if term.on is not None:
        return term.on.isoformat(), term.on.isoformat()
    if term.part == "advance":
        return "ავანსის გადახდის დღე — მითითებული არ არის", "the advance payment date (not stated)"
    return "აქტის თარიღი — ვერ მოიძებნა", "the act's date (not found)"


def _typed(p: PricedTerm) -> bool:
    """Whether the term's rate is one the user typed in, not an imported NBG rate."""
    return p.quote is not None and p.quote.source == ENTERED


def _fx_term_ka(p: PricedTerm, currency: str, *, priced: bool) -> str:
    """One term of the invoice formula in Georgian - the amount times "NBG-ის კურსი (2026-09-10)"
    - with the rate and the lari when every term has its rate. A rate the user typed in is named
    so ("შეყვანილი კურსი"): the figure is theirs, not the National Bank's."""
    day = _fx_day(p.term)[0]
    name = "შეყვანილი კურსი" if _typed(p) else "NBG-ის კურსი"
    head = f"{p.term.amount:,.2f} {currency} {_TIMES} {name} ({day}"
    if priced and p.quote is not None and p.gel is not None:
        return f"{head}: {p.quote.rate:f}) = {p.gel:,.2f} {LOCAL_CURRENCY}"
    return f"{head})"


def _fx_term_en(p: PricedTerm, currency: str, *, priced: bool) -> str:
    """One term of the invoice formula in English - the amount times "NBG rate of 2026-09-10" -
    with the rate and the lari when every term has its rate; "the rate entered for 2026-09-10"
    when the user typed it in."""
    name = "the rate entered for" if _typed(p) else "NBG rate of"
    head = f"{p.term.amount:,.2f} {currency} {_TIMES} {name} {_fx_day(p.term)[1]}"
    if priced and p.quote is not None and p.gel is not None:
        return f"{head} ({p.quote.rate:f}) = {p.gel:,.2f} {LOCAL_CURRENCY}"
    return head


#: A date the act writes in ISO, which a correction may then write in ISO too.
_ISO_WRITTEN = re.compile(r"\s*\d{4}-\d{2}-\d{2}")
#: A four-digit year in a date as written.
_YEAR_WRITTEN = re.compile(r"(?<!\d)(?:19|20)\d\d(?!\d)")
#: A date that opens with its year: "2026 წლის 30 ივნისის", "2026 წ. 30 ივნისი".
_YEAR_FIRST = re.compile(r"^\s*(?:19|20)\d\d\s*(?:წლის|წ\.)\s*")
#: A date that closes with its year, and how it is joined on: "30.06.2026", "30 ივნისი, 2026 წ.".
_YEAR_LAST = re.compile(
    r"(?P<join>[./-]|,?\s*)(?P<year>(?:19|20)\d\d)(?P<tail>\s*წ(?:\.|[ა-ჰ]*))?\s*$"
)


def _with_year_of(yearless: str, dated: str) -> str | None:
    """``yearless`` - a period's end written as a day and a month - with the year written the way
    ``dated``, the period's other end, writes its own: "25.05" beside "30.06.2026" reads
    "25.05.2026", "25 მაისიდან" beside "30 ივნისის ჩათვლით, 2026 წ." reads "25 მაისიდან, 2026
    წ.", and beside "2026 წლის 30 ივნისის" reads "2026 წლის 25 მაისიდან". None when ``dated``
    writes no year to borrow."""
    own = yearless.strip()
    if _YEAR_WRITTEN.search(own):
        return None
    if (m := _YEAR_FIRST.match(dated)) is not None:
        return m.group(0).lstrip() + own
    if (m := _YEAR_LAST.search(dated)) is not None:
        return own + m.group("join") + m.group("year") + (m.group("tail") or "")
    return None


def _window_text(lo: date | None, hi: date | None) -> str:
    """A window of dates as the messages show it, an open end left blank: "2026-03-01.."."""
    return f"{lo or ''}..{hi or ''}"


def _within(d: date, window: tuple[date | None, date | None, str]) -> bool:
    lo, hi, _ = window
    return (lo is None or d >= lo) and (hi is None or d <= hi)


def _add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 February into a common year
        return d.replace(year=d.year + years, day=28)


def _repair_year(
    hits: list[tuple[date, RuleHit]],
    window: tuple[date | None, date | None, str] | None,
    act: date | None,
) -> tuple[RuleHit, int] | None:
    """For a period that ends before it starts, the endpoint whose year was typed wrong and the
    years to move it by: the end one year on ("2026 წლის 15 დეკემბრიდან 2026 წლის 14 იანვრის"
    is meant to end in 2027) or the start one year back. A repair counts when it orders the
    period, leaves it shorter than a year and, when the contract states its term, inside it;
    of two such, the one whose last month the act is dated in. None unless exactly one is left:
    the dates as written are then compared as they are."""
    (first, first_hit), (last, last_hit) = hits[0], hits[-1]
    options: list[tuple[date, date, RuleHit, int]] = [
        (first, _add_years(last, 1), last_hit, 1),
        (_add_years(first, -1), last, first_hit, -1),
    ]
    fits = [
        o
        for o in options
        if o[0] <= o[1]
        and (o[1] - o[0]).days < 365
        and (window is None or (_within(o[0], window) and _within(o[1], window)))
    ]
    if len(fits) > 1 and act is not None:
        fits = [o for o in fits if (act.year, act.month) == (o[1].year, o[1].month)]
    if len(fits) != 1:
        return None
    return fits[0][2], fits[0][3]


def _year_shift_into(dates: list[date], window: tuple[date | None, date | None, str]) -> int | None:
    """The whole number of years (1 or 2 either way) that moves every date into ``window``,
    when exactly one does: the signature of a year typed wrong ("2025" for "2026")."""
    fits = [k for k in (-1, 1, -2, 2) if all(_within(_add_years(d, k), window) for d in dates)]
    return fits[0] if len(fits) == 1 else None


def _endpoint_year_into(
    hits: list[tuple[date, RuleHit]], window: tuple[date | None, date | None, str]
) -> tuple[RuleHit, int] | None:
    """For a two-ended period that lies outside ``window``, the one end whose year, moved by a
    year or two, puts the whole period inside it in order; None unless exactly one such move
    exists (then no single year is the mistake, and the term is what the finding cites)."""
    if len(hits) != 2:
        return None
    (start, start_hit), (end, end_hit) = hits
    fits: list[tuple[RuleHit, int]] = []
    for moved_start, hit in ((True, start_hit), (False, end_hit)):
        if _within(start if moved_start else end, window):
            continue
        for k in (-1, 1, -2, 2):
            a = _add_years(start, k) if moved_start else start
            b = end if moved_start else _add_years(end, k)
            if a <= b and _within(a, window) and _within(b, window):
                fits.append((hit, k))
    return fits[0] if len(fits) == 1 else None


def _shift_years(written: str, years: list[int], shift: int) -> str:
    """``written`` with each of ``years`` moved by ``shift`` where it stands as a year."""
    out = written
    for y in sorted(set(years), reverse=True):
        out = re.sub(rf"(?<!\d){y}(?!\d)", str(y + shift), out)
    return out


def _term_window(term: ContractTerm) -> tuple[date | None, date | None, str] | None:
    """The dates a service period must lie between (``rule_period_in_term``), and which window
    they are: the performance window, else the validity when it states its last day.

    A window's first day is its own, else the signing: no work is done under a contract before
    it is signed. A performance window with no last day of its own ends when the validity does.
    None when neither window bounds anything.
    """
    valid_from = term.validity.start if term.validity else None
    valid_until = term.validity.end if term.validity else None
    performance = term.performance
    if performance is not None and (performance.start or performance.end):
        return performance.start or term.signed, performance.end or valid_until, "performance"
    if valid_until is not None:
        return valid_from or term.signed, valid_until, "validity"
    return None


def _states(amounts: list[Money], m: Money) -> bool:
    """Whether ``amounts`` holds ``m``: the same figure in the same currency."""
    return any(a.currency == m.currency and amounts_equal(a.value, m.value) for a in amounts)


#: Georgia's VAT rate, which a fee before VAT is restated at unless the documents name another.
_VAT_RATE_DEFAULT = 0.18
#: A VAT rate written beside VAT: "დღგ 18%", "დღგ-ს (18%)", "18% დღგ", "VAT 20%".
_VAT_RATE = re.compile(
    r"(?:დ\.?ღ\.?გ\.?|(?<![a-z])vat)(?:-?ი?ს)?\s*\(?\s*(\d{1,2}(?:[.,]\d{1,2})?)\s*%"
    r"|(\d{1,2}(?:[.,]\d{1,2})?)\s*%\s*(?:დ\.?ღ\.?გ|vat(?![a-z]))",
    re.IGNORECASE,
)


def _plus_vat(net: float, gross: float, rate: float) -> bool:
    """Whether ``gross`` is ``net`` with VAT at ``rate`` added, to the tetri, rounded either way
    (the act may have worked out either figure from the other)."""
    return amounts_equal(round(net * (1 + rate), 2), gross) or amounts_equal(
        round(gross / (1 + rate), 2), net
    )


def _share_of(money: Money, prices: list[Money], shares: Sequence[float]) -> bool:
    """Whether ``money`` is a stated share (percent) of a price in its currency: the advance
    the contract sets as "30% of the price", written out as a figure in the act."""
    return any(
        amounts_equal(round(p.value * n / 100, 2), money.value)
        for p in prices
        if p.currency == money.currency
        for n in shares
    )


def _fee_parts_add_up(
    parts: list[RoledAmount],
    fees: list[Money],
    prices: list[Money],
    shares: Sequence[float] = (),
) -> bool:
    """Whether the act's advance and remainder lines are its fee paid in parts.

    Per currency, the advances and the remainder must add up to a fee the act states and the
    contract confirms (``fees``). With no remainder line the remainder is whatever the advances
    leave of that fee, so any advance up to it adds up: an act states its total and the advance
    already paid, and "the rest" goes without saying. An act that states its fee only in parts
    has no fee line for them to make up; then the advance and the remainder must add up to a
    price the contract states. A line the act restates counts once or as often as it is written,
    whichever adds up. With no advance figure, an advance the act or the contract states as a
    share (``shares``, in percent) leaves the remainder that share's rest of the fee: 70 of 100
    after a 30% advance (red team R5, AOA-R5-02).
    """
    for currency in dict.fromkeys(r.money.currency for r in parts):
        advances = [
            r.money.value for r in parts if r.money.currency == currency and r.role == "advance"
        ]
        rests = [
            r.money.value for r in parts if r.money.currency == currency and r.role == "remainder"
        ]
        advance = {sum(advances), sum(set(advances))}
        rest = {sum(rests), sum(set(rests))}
        totals = [f.value for f in fees if f.currency == currency]
        if rests and not advances and shares:
            targets = totals or [p.value for p in prices if p.currency == currency]
            ok = any(
                amounts_equal(round(t * (100 - n) / 100, 2), r)
                for n in shares
                for r in rest
                for t in targets
            )
        elif totals and rests:
            ok = any(amounts_equal(a + r, t) for a in advance for r in rest for t in totals)
        elif totals:
            ok = any(min(advance) <= t + 0.01 for t in totals)
        else:
            targets = [p.value for p in prices if p.currency == currency]
            ok = bool(rests) and any(
                amounts_equal(a + r, t) for a in advance for r in rest for t in targets
            )
        if not ok:
            return False
    return True


def _tax_wording_met(contractor_type: ContractorType, phrases: list[TaxPhrase]) -> bool:
    """Whether the act says what the contractor type needs about taxes, anywhere in it.

    A natural person's fee is NET, or excludes both the income tax and the pension contribution
    by name: "without taxes" alone does not say NET. A VAT-registered entrepreneur's fee says
    VAT is included; a small business's that taxes are; a foreign organisation's is NET.
    """
    kinds = {p.kind for p in phrases}
    if contractor_type == "natural_person":
        excluded: set[str] = set()
        for p in phrases:
            if p.kind == "exclusion":
                excluded |= p.names
        return "net" in kinds or {"income", "pension"} <= excluded
    if contractor_type == "individual_entrepreneur_vat":
        return any(p.kind == "inclusion" and "vat" in p.names for p in phrases)
    if contractor_type == "small_business":
        return "inclusion" in kinds
    return "net" in kinds


def _project_names_match(a: str, b: str) -> bool:
    """Two project names are the same when every word of the shorter one is a word of the
    longer one, in order, in any grammatical form (``same_word``): "-ისთვის", "-ში" and the like
    attach to a project's name, a misspelling does not.

    The check this replaced compared only the first word, so "ბათუმის სკოლა" and
    "ბათუმის სასტუმრო" were the same project; the one after it took any stem that began
    another word for that word, so a letter dropped from the name ("ლაზუი" for "ლაზური") passed.
    """
    wa, wb = normalize_name(a).split(), normalize_name(b).split()
    if not wa or not wb:
        return False
    short, long_ = sorted((wa, wb), key=len)
    it = iter(long_)
    return all(any(same_word(w, x) for x in it) for w in short)


def _inside(word: str) -> bool:
    """Whether every letter of another alphabet in ``word`` stands between two Georgian ones."""
    return all(0 < i < len(word) - 1 for i, ch in enumerate(word) if not is_georgian_word(ch))


def _stray_letters(word: str) -> tuple[str, str]:
    """What the characters in ``word`` that are not Georgian letters are, in Georgian and in
    English: "the Cyrillic letter 'o'" - which looks exactly like the Latin one."""
    ka: list[str] = []
    en: list[str] = []
    for ch in dict.fromkeys(c for c in word if not is_georgian_word(c)):
        if unicodedata.name(ch, "").startswith("CYRILLIC"):
            ka.append(f"კირილიცის ასო „{ch}“")
            en.append(f"the Cyrillic letter '{ch}'")
        elif ch.isdigit():
            ka.append(f"ციფრი „{ch}“")
            en.append(f"the digit '{ch}'")
        else:
            ka.append(f"ლათინური ასო „{ch}“")
            en.append(f"the Latin letter '{ch}'")
    return ", ".join(ka) or "სხვა ანბანის ასო", " and ".join(en) or "a letter of another alphabet"


def check_aoa(
    *,
    aoa_text: str,
    aoa_id: str = "aoa",
    contract_text: str | None = None,
    contract_id: str = "contract",
    registry: object | None = None,
    contractor_type: ContractorType | None = None,
    rates: RateProvider | None = None,
    advance: AdvanceInput | None = None,
    review: ScopeReviewSet | None = None,
    aoa_ocr: bool = False,
    contract_ocr: bool = False,
    speller: Lexicon | SpellingUnavailable | None = None,
) -> CheckReport:
    """``aoa_ocr`` / ``contract_ocr`` say a document's text came from a scan (OCR)."""
    return AoaChecker(
        aoa=DocumentView.build(aoa_id, aoa_text, ocr=aoa_ocr),
        contract=(
            DocumentView.build(contract_id, contract_text, ocr=contract_ocr)
            if contract_text
            else None
        ),
        registry=registry,
        contractor_type=contractor_type,
        rates=rates,
        advance=advance,
        review=review,
        ocr=aoa_ocr or contract_ocr,
        speller=speller,
    ).run()


def scope_review_items(*, aoa_text: str, contract_text: str) -> list[ScopeItem]:
    """The notes the model review can be asked about: the parts of the act the scope rules
    could not decide, read exactly as ``check_aoa`` reads them. The report, the queue and the
    worker all take them from here, so a stored review always answers the notes on the page."""
    aoa = DocumentView.build("aoa", aoa_text)
    contract = DocumentView.build("contract", contract_text)
    return review_items(read_scope(aoa.text, aoa.lines, contract.text, contract.lines))
