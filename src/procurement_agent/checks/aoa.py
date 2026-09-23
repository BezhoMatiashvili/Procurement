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
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from procurement_agent.checks.amounts import digits_and_words, money_amounts
from procurement_agent.checks.findings import CheckReport, Finding, Severity, SkipNote
from procurement_agent.extraction.anchors import AnchorHit, Party, find_anchors, find_parties
from procurement_agent.extraction.normalize import (
    amounts_equal,
    iban_is_valid,
    name_stem,
    names_match,
    normalize_confusables,
)
from procurement_agent.extraction.rules import RuleHit, find_dates, run_rules
from procurement_agent.extraction.schemas import FactType
from procurement_agent.extraction.spellcheck import best_match, classify_difference

ContractorType = Literal[
    "company", "natural_person", "individual_entrepreneur_vat", "small_business", "foreign"
]

#: "NET" as a word of its own, or the start of one ("ნეტო", "NET-ით") - never the tail of
#: another: "ინტერნეტ" and "internet" contain the letters and say nothing about taxes.
_NET = r"(?<!\w)(?:net|ნეტ)"

#: Roles that receive the payment an act releases, and roles that make it.
_PAYEE_ROLES = frozenset({"შემსრულებელი", "გამყიდველი"})
_PAYER_ROLES = frozenset({"დამკვეთი", "შემკვეთი", "მყიდველი"})

#: What the act must say about taxes, per contractor type (client rules 5-8). The wording is a
#: set of regular expressions matched against the lower-cased act.
TAX_WORDING: dict[ContractorType, tuple[tuple[str, ...], str, str]] = {
    "natural_person": (
        (_NET, "საშემოსავლო"),
        "ფიზიკური პირისთვის ანაზღაურება უნდა იყოს NET "
        "(საშემოსავლო და საპენსიო გადასახადების გარეშე)",
        "for a natural person the fee must be stated NET of income and pension tax",
    ),
    "individual_entrepreneur_vat": (
        ("დღგ",),
        "დღგ-ს გადამხდელი ინდ. მეწარმისთვის უნდა ეწეროს, რომ თანხა დღგ-ს ჩათვლითაა",
        "for a VAT-registered individual entrepreneur the fee must say it includes VAT",
    ),
    "small_business": (
        ("გადასახად",),
        'მცირე მეწარმისთვის უნდა ეწეროს, რომ "ანგარიშსწორება ითვალისწინებს გადასახადებს"',
        "for a small business the act must state that settlement includes taxes",
    ),
    "foreign": (
        (_NET,),
        "უცხოური ორგანიზაციისთვის უნდა ეწეროს NET თანხა, გადასახადები არ უნდა იყოს ნახსენები",
        "for a foreign organisation the amount must be NET with no mention of taxes",
    ),
    "company": ((), "", ""),
}

_NO_CONTRACT = "this rule compares the act against its contract, and none was supplied"

#: Contractor types for which the client does *not* want an invoice calculation (rule 9).
_FX_EXEMPT: frozenset[str] = frozenset({"small_business", "foreign"})

_ACT_DATE_KEYS = ("გაფორმდა", "გაფორმების თარიღი")
_PERIOD_KEYS = ("შესრულდა", "შესრულების თარიღი", "შესრულების პერიოდი")
_TERM_KEYS = ("შეასრულოს", "მოქმედების ვადა", "ჩათვლით")
_TAX_FREE_MARKERS = ("გადასახად", "დღგ")


@dataclass
class DocumentView:
    """Everything a check needs from one document, read once."""

    doc_id: str
    text: str
    parties: list[Party] = field(default_factory=list)
    _hits: list[AnchorHit] | None = field(default=None, init=False, repr=False, compare=False)

    @classmethod
    def build(cls, doc_id: str, text: str) -> DocumentView:
        text = normalize_confusables(text)
        return cls(doc_id=doc_id, text=text, parties=find_parties(text))

    def hits(self) -> list[AnchorHit]:
        """Every anchor in the document, computed once: the rules ask for them a dozen times."""
        if self._hits is None:
            self._hits = find_anchors(self.text)
        return self._hits

    def anchors(self, ft: FactType) -> list[str]:
        return [h.value for h in self.hits() if h.type == ft]

    def anchor_hits(self, ft: FactType) -> list[AnchorHit]:
        return [h for h in self.hits() if h.type == ft]

    def bank(self, unit: str) -> set[str]:
        return {h.value for h in self.bank_hits(unit)}

    def bank_hits(self, unit: str) -> list[AnchorHit]:
        return [h for h in self.hits() if h.type == FactType.bank_account and h.unit == unit]

    def id_codes(self) -> set[str]:
        return {p.id_code for p in self.parties if p.id_code}


def _date_hits_near(
    text: str, keys: tuple[str, ...], window: int = 160
) -> list[tuple[date, RuleHit]]:
    """Dates that follow one of the given keywords, in document order, with where each was read.

    Anchoring on the section wording is far more reliable than the generic keyword-proximity
    classifier, because these documents put each date under its own numbered heading.
    """
    hits = find_dates(text)
    if not hits:
        return []
    starts = [m.start() for k in keys for m in re.finditer(re.escape(k), text)]
    out: list[tuple[date, RuleHit]] = []
    for h in hits:
        if any(s <= h.start <= s + window for s in starts):
            d = date.fromisoformat(str(h.normalized))
            if all(d != seen for seen, _ in out):
                out.append((d, h))
    return out


def _dates_near(text: str, keys: tuple[str, ...], window: int = 160) -> list[date]:
    return [d for d, _ in _date_hits_near(text, keys, window)]


def _month_typos(text: str) -> list[tuple[str, int, int]]:
    return [
        (h.note or "", h.start, h.end) for h in find_dates(text) if h.note and "misspell" in h.note
    ]


def _last_day(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def add_months(start: date, months: int) -> date:
    """Month arithmetic that survives month ends (31 Jan + 1 month is 28/29 Feb, not a crash)."""
    total = start.month - 1 + months
    year, month = start.year + total // 12, total % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


class AoaChecker:
    def __init__(
        self,
        *,
        aoa: DocumentView,
        contract: DocumentView | None = None,
        registry: object | None = None,
        contractor_type: ContractorType | None = None,
    ) -> None:
        self.aoa = aoa
        self.contract = contract
        self.registry = registry
        self.contractor_type = contractor_type
        self.findings: list[Finding] = []
        self.skipped: list[SkipNote] = []

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
    ) -> None:
        if quote is None and span is not None:
            quote = self.aoa.text[span[0] : span[1]] or None
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
                confidence=confidence,
            )
        )

    def _skip(self, rule_id: str, why_ka: str, why_en: str) -> None:
        note = SkipNote(rule_id=rule_id, reason_ka=why_ka, reason_en=why_en)
        if note not in self.skipped:  # one party with two unlisted directors is one gap
            self.skipped.append(note)

    def _find_span(self, needle: str) -> tuple[int, int] | None:
        i = self.aoa.text.find(needle)
        return (i, i + len(needle)) if i >= 0 else None

    # --- rules ---------------------------------------------------------------------------

    def run(self) -> CheckReport:
        # Every run starts clean, so running a checker twice reports each finding once.
        self.findings = []
        self.skipped = []
        self.rule_amount_words()
        self.rule_required_fields()
        self.rule_act_date()
        self.rule_month_spelling()
        self.rule_iban_format()
        self.rule_bank_code_format()
        self.rule_tax_wording()
        if self.contract is None:
            self._skip(
                "AOA.CONTRACT_*",
                "ხელშეკრულება არ არის ატვირთული; აქტი შემოწმდა დამოუკიდებლად",
                "no contract supplied; act checked on its own",
            )
        else:
            self.rule_parties()
            self.rule_cadastral()
            self.rule_project()
            self.rule_bank()
            self.rule_fee()
            self.rule_period_in_term()
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
        period = _dates_near(self.aoa.text, _PERIOD_KEYS)
        if period:
            out["execution_period"] = f"{period[0]}..{period[-1]}"
        act = _dates_near(self.aoa.text, _ACT_DATE_KEYS)
        if act:
            out["act_date"] = str(act[0])
        return out

    def rule_amount_words(self) -> None:
        """'100 (ასი) ლარს': the digits and the written-out number must agree.

        This is a pure intra-document check, so it works with no contract at all, and it
        doubles as an OCR guard: a misread digit no longer matches its own spelled-out form.
        """
        checked = 0
        for pair in digits_and_words(self.aoa.text):
            digits, words, spelled = pair.digits, pair.words, pair.spelled
            if spelled is None:
                if len(words.split()) <= 3:
                    self._add(
                        "AOA.AMOUNT_WORDS",
                        "warning",
                        f"თანხა სიტყვიერად ვერ წაიკითხა: {pair.raw}",
                        f"could not read the amount in words: {pair.raw}",
                        actual=words,
                        span=pair.span,
                        confidence=0.8,
                    )
                continue
            checked += 1
            if not amounts_equal(float(spelled), digits):
                self._add(
                    "AOA.AMOUNT_WORDS",
                    "error",
                    f"ციფრი და სიტყვიერი ჩანაწერი არ ემთხვევა: {digits:g} ≠ „{words}“ ({spelled})",
                    f"digits and words disagree: {digits:g} vs '{words}' ({spelled})",
                    expected=f"{digits:g}",
                    actual=f"{words} ({spelled})",
                    span=pair.span,
                )
        if not checked:
            self._skip(
                "AOA.AMOUNT_WORDS",
                "აქტში ვერ მოიძებნა თანხა ფორმით „ციფრები (სიტყვიერად)“",
                "no 'digits (words)' amount found",
            )

    def rule_required_fields(self) -> None:
        """Client rule 3: the act must name the project and the cadastral code."""
        if not self.aoa.anchors(FactType.project_name):
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
        period = _dates_near(self.aoa.text, _PERIOD_KEYS)
        act_hits = _date_hits_near(self.aoa.text, _ACT_DATE_KEYS)
        act_dates = [d for d, _ in act_hits]
        if not period:
            self._skip(
                "AOA.ACT_DATE",
                "აქტში ვერ მოიძებნა მომსახურების შესრულების პერიოდი",
                "execution period not found in the act",
            )
            return
        if not act_dates:
            self._add(
                "AOA.ACT_DATE",
                "error",
                "აქტის გაფორმების თარიღი ვერ მოიძებნა",
                "the act's date could not be found",
            )
            return
        end = max(period)
        expected = _last_day(end) if len(period) > 1 else end
        if all(d != expected for d in act_dates):
            shown = ", ".join(str(d) for d in act_dates)
            self._add(
                "AOA.ACT_DATE",
                "error",
                f"აქტი დათარიღებულია {shown}-ით, მომსახურების პერიოდი მთავრდება {end}-ს — "
                f"აქტი უნდა გაფორმდეს იმ თვის ბოლო დღეს, როდესაც პერიოდი მთავრდება ({expected})",
                f"act dated {shown} but the service period ends {end}; it should be dated "
                f"the last day of the month the period ends in ({expected})",
                expected=str(expected),
                actual=shown,
                span=(act_hits[0][1].start, act_hits[0][1].end),
            )

    def rule_month_spelling(self) -> None:
        """Typos inside date wording, surfaced by the typo-tolerant month matcher."""
        for note, start, end in _month_typos(self.aoa.text):
            word = note.split(": ")[-1].strip("'\"")
            self._add(
                "AOA.SPELLING",
                "warning",
                f"თვის დასახელება შეცდომითაა დაწერილი: {word}",
                f"month name appears misspelled: {word}",
                actual=word,
                span=(start, end),
                confidence=0.9,
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
                    span=self._find_span(iban),
                    confidence=0.9,
                )

    def rule_bank_code_format(self) -> None:
        """A bank code that is not a well-formed BIC.

        Unlike the IBAN checksum, this is an error: the client's documents carry anonymised
        account numbers but real bank codes, and a seven-character code is a drafting mistake
        no anonymisation would produce.
        """
        for hit in self.bank_code_problems():
            self._add(
                "AOA.BANK_CODE_FORMAT",
                "error",
                f"ბანკის კოდი არასწორი ფორმატისაა: {hit.value} (BIC უნდა იყოს 8 ან 11 სიმბოლო)",
                f"bank code is not a well-formed BIC: {hit.value} (a BIC is 8 or 11 characters)",
                actual=hit.value,
                span=(hit.start, hit.end),
            )

    def bank_code_problems(self) -> list[AnchorHit]:
        return [h for h in self.aoa.bank_hits("SWIFT") if h.note]

    def rule_tax_wording(self) -> None:
        """Client rules 5-8. Nothing in the document states the contractor type, so when it is
        not supplied the rule is reported as not run rather than guessed at."""
        if self.contractor_type is None:
            self._skip(
                "AOA.TAX_WORDING",
                "შემსრულებლის ტიპი მითითებული არ არის (ფიზიკური პირი / ინდ. მეწარმე / "
                "მცირე მეწარმე / უცხოური ორგანიზაცია), ამიტომ NET/დღგ-ს ფორმულირება არ შემოწმებულა",
                "contractor type not supplied (natural person / individual entrepreneur / "
                "small business / foreign organisation), so NET/VAT wording was not checked",
            )
            return
        required, ka, en = TAX_WORDING[self.contractor_type]
        low = self.aoa.text.lower()
        if self.contractor_type == "foreign":
            offending = [w for w in _TAX_FREE_MARKERS if w in low]
            if offending:
                self._add("AOA.TAX_WORDING", "error", ka, en, actual=", ".join(offending))
            return
        if required and not any(re.search(w, low) for w in required):
            shown = " / ".join("NET" if w == _NET else w for w in required)
            self._add("AOA.TAX_WORDING", "error", ka, en, expected=shown)

    # --- rules that need the contract -----------------------------------------------------

    def _matched_parties(self) -> list[tuple[Party, Party | None]]:
        if self.contract is None:  # run() calls the contract rules only with a contract
            raise RuntimeError(_NO_CONTRACT)
        pairs: list[tuple[Party, Party | None]] = []
        for p in self.aoa.parties:
            match = None
            if p.id_code:
                match = next((q for q in self.contract.parties if q.id_code == p.id_code), None)
            if match is None and p.name:
                match = next(
                    (q for q in self.contract.parties if q.name and names_match(p.name, q.name)),
                    None,
                )
            pairs.append((p, match))
        return pairs

    def rule_parties(self) -> None:
        """Client rule 1 and the contract cross-check: identify by code, then verify the rest."""
        if self.contract is None:  # run() calls the contract rules only with a contract
            raise RuntimeError(_NO_CONTRACT)
        c_ids = self.contract.id_codes()
        a_ids = self.aoa.id_codes()
        if not a_ids:
            self._add(
                "AOA.ID_CODES",
                "error",
                "აქტში საიდენტიფიკაციო კოდები ვერ მოიძებნა",
                "no identification codes found in the act",
            )
        elif c_ids and not a_ids <= c_ids:
            extra = sorted(a_ids - c_ids)
            self._add(
                "AOA.ID_CODES",
                "error",
                f"საიდენტიფიკაციო კოდი ხელშეკრულებაში არ არის: {', '.join(extra)}",
                f"identification code(s) not in the contract: {', '.join(extra)}",
                expected=", ".join(sorted(c_ids)),
                actual=", ".join(sorted(a_ids)),
            )

        for p, match in self._matched_parties():
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
            for d in p.directors:
                if any(names_match(d, cd) for cd in match.directors):
                    continue
                if not match.directors:
                    self._skip(
                        "AOA.DIRECTOR",
                        f"ხელშეკრულებაში {match.name}-ის დირექტორი მითითებული არ არის",
                        f"contract names no director for {match.name}",
                    )
                    continue
                guess = best_match(d, match.directors)
                expected = guess[0] if guess else None
                diff = guess[1] if guess else None
                hint_ka = " (როგორც ჩანს, შეცდომაა)" if diff and diff.looks_like_typo else ""
                hint_en = " (looks like a misspelling)" if diff and diff.looks_like_typo else ""
                self._add(
                    "AOA.DIRECTOR",
                    "error",
                    f"ხელმომწერი არ ემთხვევა ხელშეკრულებას: „{d}“, უნდა იყოს „{expected}“{hint_ka}",
                    f"signatory does not match the contract: '{d}', expected '{expected}'{hint_en}",
                    expected=expected,
                    actual=d,
                    span=self._find_span(d),
                )

    def _compare_field(
        self, rule_id: str, ka_label: str, en_label: str, actual: str | None, expected: str | None
    ) -> None:
        if not actual or not expected:
            return
        diff = classify_difference(actual, expected)
        if diff is None:
            return
        hint_ka = " (როგორც ჩანს, ორთოგრაფიული შეცდომაა)" if diff.looks_like_typo else ""
        hint_en = " (looks like a misspelling)" if diff.looks_like_typo else ""
        self._add(
            rule_id,
            "error",
            f"{ka_label} არ ემთხვევა ხელშეკრულებას: „{actual}“, უნდა იყოს „{expected}“{hint_ka}",
            f"{en_label} does not match the contract: '{actual}', expected '{expected}'{hint_en}",
            expected=expected,
            actual=actual,
            span=self._find_span(actual),
        )

    def rule_cadastral(self) -> None:
        """Every code the act names must be one the contract names.

        A subset, not equality: a contract covering two plots may have an act for one of them,
        which the equality test this replaced reported as an error.
        """
        if self.contract is None:  # run() calls the contract rules only with a contract
            raise RuntimeError(_NO_CONTRACT)
        a = set(self.aoa.anchors(FactType.cadastral_code))
        c = set(self.contract.anchors(FactType.cadastral_code))
        if a and c and not a <= c:
            extra = sorted(a - c)
            self._add(
                "AOA.CADASTRAL",
                "error",
                f"საკადასტრო კოდი განსხვავდება: {extra} vs ხელშეკრულება {sorted(c)}",
                f"cadastral code differs: {extra} vs contract {sorted(c)}",
                expected=", ".join(sorted(c)),
                actual=", ".join(extra),
                span=self._find_span(extra[0]),
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
            self._add(
                "AOA.PROJECT_NAME",
                "error",
                f"პროექტის დასახელება არ ემთხვევა ხელშეკრულებას: „{name}“, "
                f"ხელშეკრულებაში — „{c[0]}“",
                f"project name does not match the contract: '{name}', contract says '{c[0]}'",
                expected=c[0],
                actual=name,
                span=self._find_span(name),
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
            if a and c and not a <= c:
                mismatched = True
                extra = sorted(a - c)
                hint_ka = hint_en = ""
                if (
                    unit == "IBAN"
                    and any(iban_is_valid(x) for x in c)
                    and not any(iban_is_valid(x) for x in a)
                ):
                    hint_ka, hint_en = (
                        " (აქტის IBAN არავალიდურია)",
                        " (the act's IBAN is not valid)",
                    )
                self._add(
                    "AOA.BANK",
                    "error",
                    f"{ka} განსხვავდება: {sorted(a)} vs ხელშეკრულება {sorted(c)}{hint_ka}",
                    f"{en} differs: {sorted(a)} vs contract {sorted(c)}{hint_en}",
                    expected=", ".join(sorted(c)),
                    actual=", ".join(extra),
                    span=self._find_span(extra[0]),
                )
            elif not a and unit == "IBAN":
                self._add(
                    "AOA.BANK",
                    "warning",
                    "აქტში საბანკო ანგარიში არ წერია",
                    "the act does not state a bank account",
                )
        if not mismatched:
            self._bank_owner()
            self._bank_pairing()

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
                f"აქტში მითითებული ანგარიში ხელშეკრულებით დამკვეთს ეკუთვნის: {act_accounts}; "
                f"შემსრულებლის ანგარიშია {payee_accounts}",
                f"the act's account belongs to the customer in the contract: {act_accounts}; "
                f"the contractor's account is {payee_accounts}",
                expected=", ".join(payee_accounts),
                actual=", ".join(act_accounts),
                span=self._find_span(act_accounts[0]),
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
        if len(self.contract.bank("SWIFT")) < 2:
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
            if want is None or want == code:
                continue
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

    def _amounts(self, view: DocumentView) -> set[float]:
        return money_amounts(view.text)

    def rule_fee(self) -> None:
        if self.contract is None:  # run() calls the contract rules only with a contract
            raise RuntimeError(_NO_CONTRACT)
        a, c = self._amounts(self.aoa), self._amounts(self.contract)
        if not a:
            self._add("AOA.FEE", "error", "აქტში საფასური ვერ მოიძებნა", "no fee found in the act")
            return
        if not c:
            self._skip(
                "AOA.FEE",
                "ხელშეკრულებაში ვერ მოიძებნა თანხა შესადარებლად",
                "no amount found in the contract to compare against",
            )
            return
        unmatched = [x for x in a if not any(amounts_equal(x, y) for y in c)]
        if unmatched:
            self._add(
                "AOA.FEE",
                "error",
                f"საფასური ხელშეკრულების არცერთ თანხას არ ემთხვევა: "
                f"{sorted(unmatched)} vs {sorted(c)}",
                f"fee matches no amount in the contract: {sorted(unmatched)} vs {sorted(c)}",
                expected=", ".join(f"{x:g}" for x in sorted(c)),
                actual=", ".join(f"{x:g}" for x in sorted(unmatched)),
                span=_amount_span(self.aoa.text, min(unmatched)),
            )

    def rule_period_in_term(self) -> None:
        if self.contract is None:  # run() calls the contract rules only with a contract
            raise RuntimeError(_NO_CONTRACT)
        period_hits = _date_hits_near(self.aoa.text, _PERIOD_KEYS)
        period = [d for d, _ in period_hits]
        term = _dates_near(self.contract.text, _TERM_KEYS)
        if not term:
            all_dates = [
                date.fromisoformat(str(h.normalized)) for h in find_dates(self.contract.text)
            ]
            term = [min(all_dates), max(all_dates)] if len(all_dates) >= 2 else []
        if not period:
            self._add(
                "AOA.PERIOD",
                "warning",
                "აქტში შესრულების პერიოდი ვერ მოიძებნა",
                "the execution period could not be found in the act",
            )
            return
        if not term:
            self._skip(
                "AOA.PERIOD_IN_TERM",
                "ხელშეკრულების მოქმედების ვადა ვერ დადგინდა",
                "contract term could not be determined",
            )
            return
        lo, hi = min(term), max(term)
        if min(period) < lo or max(period) > hi:
            # anchor on the first date that lies outside, as the act writes it
            outside = next(h for d, h in period_hits if d < lo or d > hi)
            self._add(
                "AOA.PERIOD_IN_TERM",
                "error",
                f"შესრულების პერიოდი {min(period)}..{max(period)} სცდება ხელშეკრულების "
                f"მოქმედების ვადას {lo}..{hi}",
                f"execution period {min(period)}..{max(period)} falls outside the contract term "
                f"{lo}..{hi}",
                expected=f"{lo}..{hi}",
                actual=f"{min(period)}..{max(period)}",
                span=(outside.start, outside.end),
            )

    def rule_registry(self) -> None:
        """Client rule 1: verify name / address / directors against the business registry."""
        if self.registry is None:
            self._skip(
                "AOA.REGISTRY",
                "სამეწარმეო რეესტრის ამონაწერი ჩატვირთული არ არის; მხარეების მონაცემები "
                "შედარდა მხოლოდ ხელშეკრულებას",
                "no business-registry snapshot loaded; party details were compared against the "
                "contract only",
            )
            return
        for p in self.aoa.parties:
            if not p.id_code:
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
                "AOA.REGISTRY_NAME", "დასახელება (რეესტრი)", "name (registry)", p.name, entry.name
            )
            self._compare_field(
                "AOA.REGISTRY_ADDRESS",
                "მისამართი (რეესტრი)",
                "address (registry)",
                p.address,
                entry.address,
            )
            for d in p.directors:
                if entry.directors and not any(names_match(d, x) for x in entry.directors):
                    self._add(
                        "AOA.REGISTRY_DIRECTOR",
                        "error",
                        f"ხელმომწერი რეესტრის მონაცემებს არ ემთხვევა: „{d}“ "
                        f"(რეესტრი: {', '.join(entry.directors)})",
                        f"signatory does not match the registry: '{d}' "
                        f"(registry: {', '.join(entry.directors)})",
                        expected=", ".join(entry.directors),
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


def _word_stems_match(a: str, b: str) -> bool:
    """One word of a name against another, allowing a case ending or postposition on either.

    ``name_stem`` removes the genitive and the nominative vowel, but a project name also turns
    up with "-ისთვის", "-ში" and the like attached. The shorter stem being a prefix of the
    longer one - and long enough to mean something - covers those without letting
    "სკოლა" match "სასტუმრო".
    """
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 4 and long_.startswith(short)


def _project_names_match(a: str, b: str) -> bool:
    """Two project names are the same when every word of the shorter one matches, in order.

    The check this replaced compared only the first word, so "ბათუმის სკოლა" and
    "ბათუმის სასტუმრო" were the same project.
    """
    wa, wb = name_stem(a).split(), name_stem(b).split()
    if not wa or not wb:
        return False
    short, long_ = sorted((wa, wb), key=len)
    it = iter(long_)
    return all(any(_word_stems_match(w, x) for x in it) for w in short)


def _amount_span(text: str, value: float) -> tuple[int, int] | None:
    """Where in ``text`` a monetary amount of ``value`` is written, for a finding's anchor."""
    for h in run_rules(text):
        if h.currency and isinstance(h.normalized, float) and amounts_equal(h.normalized, value):
            return (h.start, h.end)
    return None


def check_aoa(
    *,
    aoa_text: str,
    aoa_id: str = "aoa",
    contract_text: str | None = None,
    contract_id: str = "contract",
    registry: object | None = None,
    contractor_type: ContractorType | None = None,
) -> CheckReport:
    return AoaChecker(
        aoa=DocumentView.build(aoa_id, aoa_text),
        contract=DocumentView.build(contract_id, contract_text) if contract_text else None,
        registry=registry,
        contractor_type=contractor_type,
    ).run()
