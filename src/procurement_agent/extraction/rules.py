"""Deterministic extraction (no LLM): amounts, dates, ID codes, percentages, durations,
contract numbers — in Georgian and English.

Each hit carries the character span inside the input text so the caller can attach
provenance. Classification into procurement-specific types uses nearby keywords; anything
ambiguous stays generic (``amount``/``date``/...) and is left for the LLM to refine.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Hashable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date

from procurement_agent.extraction.normalize import (
    amounts_equal,
    edit_distance,
    iban_is_valid,
    unlabelled_bic_plausible,
)
from procurement_agent.extraction.normalize import parse_number as _parse_number
from procurement_agent.extraction.schemas import FactType

# --- helpers -----------------------------------------------------------------------------------

_KA_MONTHS = {
    "იანვ": 1, "თებერ": 2, "მარტ": 3, "აპრილ": 4, "მაის": 5, "ივნის": 6,
    "ივლის": 7, "აგვისტ": 8, "სექტემბ": 9, "ოქტომბ": 10, "ნოემბ": 11, "დეკემბ": 12,
}  # fmt: skip
_EN_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip
_KA_MONTH_RE = "|".join(_KA_MONTHS)
# A month name, possibly misspelled. Resolved by _month_ka, which rejects non-months, so
# the surrounding day+year structure plus that rejection is what keeps this from over-matching.
_KA_MONTH_FUZZY = r"[ა-ჰ]{3,14}"
_EN_MONTH_RE = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)

#: A currency as the documents write it after an amount: the code or sign, or the name in the
#: case the sentence needs - "აშშ დოლარს" (dative) is how a Georgian act states a dollar fee.
#: Sterling only as "pound sterling": "100 pounds" is a weight as often as it is money.
_CUR_RE = (
    r"₾|\$|€|£|GEL|USD|EUR|GBP|ლარ(?:ი|ის|ს)?"
    r"|(?:აშშ\s*|ა\.შ\.შ\.\s*)?დოლარ(?:ი|ის|ს|ით|ად)?"
    r"|ევრო(?:ს|თი|დ)?"
    r"|(?:გირვანქა|ფუნტი?)\s*სტერლინგ(?:ი|ის|ს|ით|ად)?"
    r"|(?:US\s*|U\.S\.\s*)?dollars?|euros?|pounds?\s+sterling"
)
#: A currency written *before* an amount: a code or a sign only. A name there is the end of a
#: sentence about something else - "... აშშ დოლარს 2026 წლის" read as 2,026 lari, because the
#: name pattern found "ლარს" inside "დოლარს" - never "ლარი 100".
_CUR_BEFORE_RE = r"(?<![A-Za-z])(?:GEL|USD|EUR|GBP)|₾|\$|€|£"
#: What each currency is called, tested in this order: dollars before lari, because "დოლარ"
#: (dollar) contains "ლარ" (lari).
_CURRENCY_NAMES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("USD", re.compile(r"\$|usd|დოლარ|dollar", re.IGNORECASE)),
    ("EUR", re.compile(r"€|eur|ევრო", re.IGNORECASE)),
    ("GBP", re.compile(r"£|gbp|სტერლინგ|sterling", re.IGNORECASE)),
    ("GEL", re.compile(r"₾|gel|ლარ", re.IGNORECASE)),
)
_NUM_RE = r"\d{1,3}(?:[ \u00a0,.]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"

# context windows (chars) used to classify generic hits
_CTX = 80

KEYWORDS: dict[FactType, tuple[str, ...]] = {
    FactType.warranty_period: ("გარანტი", "საგარანტიო", "warrant", "guarantee period"),
    FactType.delivery_deadline: ("მიწოდებ", "მოწოდებ", "deliver", "delivery"),
    FactType.execution_period: (
        "შესრულებ",
        "შესრულდა",
        "დასრულებ",
        "execution",
        "completion",
        "perform",
    ),
    FactType.payment_terms: ("გადახდ", "ანგარიშსწორებ", "payment", "invoice", "ავანს", "advance"),
    FactType.validity_period: ("ძალაშ", "მოქმედებ", "valid", "validity", "expire"),
    FactType.penalty: ("პირგასამტეხლო", "ჯარიმ", "საურავ", "penalt", "liquidated damages"),
    FactType.total_amount: (
        "სულ",
        "ჯამ",
        "საერთო ღირებულებ",
        "საფასურ",
        "ღირებულებ",
        "ანაზღაურებ",
        "total",
        "grand total",
        "sum",
        "fee",
    ),
    FactType.price: ("ერთეულის ფას", "ფას", "price", "unit price", "cost", "rate"),
    # The act's own signing: "„აქტი“ გაფორმდა 2026 წლის 31 აგვისტო". These end where the bare
    # "გაფორმ" of contract_date ends, and the longer keyword wins a tie, so the date an act was
    # signed stops being reported as the contract's date. Every quote style is spelled out
    # because Georgian stems match as plain substrings.
    FactType.act_date: (
        "აქტი გაფორმ",
        "აქტი“ გაფორმ",
        "აქტი” გაფორმ",
        'აქტი" გაფორმ',
        "აქტი» გაფორმ",
        "აქტის გაფორმ",
        "აქტის თარიღ",
        "act was signed",
        "act date",
    ),
    FactType.contract_date: (
        "ხელშეკრულებ",
        "შეთანხმებ",
        "გაფორმ",
        "contract",
        "agreement",
        "dated",
        "თარიღ",
    ),
    FactType.vat_status: ("დღგ", "vat", "დამატებული ღირებულების"),
}


@dataclass(frozen=True, slots=True)
class RuleHit:
    type: FactType
    value: str
    raw_text: str
    start: int
    end: int
    normalized: str | float | None = None
    unit: str | None = None
    currency: str | None = None
    confidence: float = 0.6
    note: str | None = None


# Newlines are NOT boundaries: Docling emits inline runs ("შეადგენს\n100\n(ასი) ლარს") and
# table cells on separate lines; the nearest-keyword rule keeps neighbouring rows apart.
_SENTENCE_BOUNDARY = re.compile(r"[.;!?]")
_KW_CACHE: dict[str, re.Pattern[str]] = {}


_GEORGIAN_LETTER = re.compile(r"[\u10a0-\u10ff]")


def keyword_pattern(kw: str) -> re.Pattern[str]:
    """Latin keywords must start at a word boundary ("sum" must not match inside "ipsum").

    Georgian stems are matched anywhere: the language builds words with prefixes, so "ფას"
    must match inside "საფასური" (fee) and "ღირებულებ" inside "მთლიანი ღირებულების".
    """
    pat = _KW_CACHE.get(kw)
    if pat is None:
        prefix = "" if _GEORGIAN_LETTER.search(kw) else r"(?<!\w)"
        pat = _KW_CACHE[kw] = re.compile(prefix + re.escape(kw))
    return pat


def keyword_in(text: str, kw: str) -> bool:
    return keyword_pattern(kw).search(text) is not None


_AFTER_PENALTY = 20  # keywords that follow a value are weaker evidence than those preceding it


def nearest_keyword_group[K: Hashable](
    text: str, start: int, end: int, groups: Mapping[K, Iterable[str]]
) -> K | None:
    """The group whose keyword sits closest to ``text[start:end]`` *within the same sentence*.

    At the same distance the longer keyword wins, because Georgian builds words with prefixes
    and stems therefore nest: "ფას" (price) matches two characters inside "საფასურ" (fee), and
    without this the fee in an acceptance act is labelled a unit price. Remaining ties are
    resolved by group order, so callers list the more specific group first.

    Shared by the fact classifier below and by the checks that give an amount its role in a
    contract (price, advance, penalty ...), so both weigh the same evidence the same way.
    """
    pre = text[max(0, start - _CTX) : start].lower()
    post = text[end : min(len(text), end + _CTX)].lower()
    # keep only the sentence fragment adjacent to the hit
    cut = list(_SENTENCE_BOUNDARY.finditer(pre))
    if cut:
        pre = pre[cut[-1].end() :]
    m = _SENTENCE_BOUNDARY.search(post)
    if m:
        post = post[: m.start()]

    best: tuple[int, int, K] | None = None
    for group, keywords in groups.items():
        for kw in keywords:
            pat = keyword_pattern(kw)
            pre_matches = list(pat.finditer(pre))
            if pre_matches:
                dist = len(pre) - pre_matches[-1].end()
                # sort key: nearer first, then longer keyword
                if best is None or (dist, -len(kw)) < (best[0], -best[1]):
                    best = (dist, len(kw), group)
            m_post = pat.search(post)
            if m_post:
                dist = m_post.start() + _AFTER_PENALTY
                if best is None or (dist, -len(kw)) < (best[0], -best[1]):
                    best = (dist, len(kw), group)
    return best[2] if best else None


def _classify(text: str, start: int, end: int, candidates: tuple[FactType, ...]) -> FactType | None:
    """Pick the candidate type whose keyword sits closest to the hit (``nearest_keyword_group``
    over ``KEYWORDS``). Callers list the more specific type first: it wins a tie."""
    return nearest_keyword_group(text, start, end, {ft: KEYWORDS.get(ft, ()) for ft in candidates})


def parse_number(s: str) -> float | None:
    """Kept as a name for callers of this module; the one implementation is in ``normalize``.

    This used to be a copy that had drifted: it did not know a minus sign, nor any space
    character other than the no-break space, so the rules and the scorer could read the same
    string as two different numbers.
    """
    return _parse_number(s)


def _norm_currency(raw: str) -> str | None:
    """The ISO code of a currency as written: "აშშ დოლარს" is USD, "ლარს" GEL."""
    return next((code for code, name in _CURRENCY_NAMES if name.search(raw)), None)


# --- amounts -----------------------------------------------------------------------------------

# "48 750,00 (ორმოცდარვა ათას ...) ლარს": contracts spell the number out between digits and currency
_PAREN = r"(?:\([^()]{0,160}\)\s*)?"
_AMOUNT_AFTER = re.compile(
    rf"(?<![\d.,])({_NUM_RE})\s*{_PAREN}({_CUR_RE})(?![A-Za-zა-ჰ])", re.IGNORECASE
)
_AMOUNT_BEFORE = re.compile(rf"({_CUR_BEFORE_RE})\s*({_NUM_RE})(?![\d])", re.IGNORECASE)

# A number whose only separators are spaces and which has no decimal part: "5 100". Written
# after a quantity keyword it is far more likely a count followed by a price ("რაოდენობა 5
# 100 ლარი") than one amount of 5 100, so only the group next to the currency is the amount.
# With a decimal part ("1 234 567,89") the grouping is unambiguous and is kept whole.
_SPACE_GROUPED = re.compile(r"\d{1,3}(?:[ \u00a0]\d{3})+")
_QUANTITY_BEFORE = re.compile(r"(?:რაოდენობ[ა-ჰ]*|quantity|qty)\.?\s*$", re.IGNORECASE)


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """Half-open interval overlap, including one span wholly containing the other."""
    return a[0] < b[1] and b[0] < a[1]


def find_amounts(text: str) -> list[RuleHit]:
    hits: list[RuleHit] = []
    seen: set[tuple[int, int]] = set()
    for pat, num_group, cur_group in ((_AMOUNT_AFTER, 1, 2), (_AMOUNT_BEFORE, 2, 1)):
        for m in pat.finditer(text):
            span = (m.start(), m.end())
            if any(_overlaps(span, other) for other in seen):
                continue
            number = m.group(num_group)
            if (
                pat is _AMOUNT_AFTER
                and _SPACE_GROUPED.fullmatch(number)
                and _QUANTITY_BEFORE.search(text[max(0, span[0] - 30) : span[0]])
            ):
                cut = max(number.rfind(" "), number.rfind("\u00a0")) + 1
                number = number[cut:]
                span = (m.start(num_group) + cut, span[1])
            value = parse_number(number)
            if value is None:
                continue
            cur = _norm_currency(m.group(cur_group))
            ft = _classify(text, *span, (FactType.total_amount, FactType.penalty, FactType.price))
            seen.add(span)
            hits.append(
                RuleHit(
                    type=ft or FactType.amount,
                    value=f"{value:,.2f} {cur}" if cur else f"{value:,.2f}",
                    raw_text=text[span[0] : span[1]],
                    start=span[0],
                    end=span[1],
                    normalized=value,
                    currency=cur,
                    confidence=0.7 if ft else 0.5,
                )
            )
    return hits


# --- dates -------------------------------------------------------------------------------------

_DATE_NUMERIC = re.compile(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](\d{4})(?!\d)")
_DATE_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_DATE_KA_YMD = re.compile(
    rf"(?<!\d)(\d{{4}})[.,\s]*წ(?:ლის|\.)?\s*(\d{{1,2}})\s*({_KA_MONTH_FUZZY})", re.IGNORECASE
)
_DATE_KA_DMY = re.compile(
    rf"(?<!\d)(\d{{1,2}})\s*({_KA_MONTH_FUZZY}),?\s*(\d{{4}})\s*(?:წ(?:ელი|ლის|\.)?)?",
    re.IGNORECASE,
)
_DATE_EN_DMY = re.compile(
    rf"(?<!\d)(\d{{1,2}})(?:st|nd|rd|th)?\s+({_EN_MONTH_RE})\.?,?\s+(\d{{4}})", re.IGNORECASE
)
_DATE_EN_MDY = re.compile(
    rf"\b({_EN_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})", re.IGNORECASE
)


def _safe_date(y: int, mo: int, d: int) -> date | None:
    try:
        dt = date(y, mo, d)
    except ValueError:
        return None
    return dt if 1990 <= y <= 2100 else None


def _month_ka(s: str) -> int | None:
    """Resolve a Georgian month name, tolerating the typos these documents actually contain.

    Exact prefix first (the common case, and free). Otherwise the word is compared against
    each stem at three prefix lengths, because a typo may add or drop a letter:
    "აგისტოდან" (dropped ვ), "აგვუისტას" (inserted უ), "აგივსტოდან" (transposed ვი).
    A word that is not within the distance budget of any stem is not a month at all, which
    is what stops the widened regex from matching arbitrary Georgian words.
    """
    s = s.lower()
    exact = next((v for k, v in _KA_MONTHS.items() if s.startswith(k)), None)
    if exact is not None:
        return exact
    best: tuple[int, int] | None = None
    for stem, value in _KA_MONTHS.items():
        budget = max(1, len(stem) // 3)
        for n in (len(stem) - 1, len(stem), len(stem) + 1):
            if n < 3 or n > len(s):
                continue
            d = edit_distance(s[:n], stem, cap=budget)
            if d <= budget and (best is None or d < best[0]):
                best = (d, value)
    return best[1] if best else None


def month_ka_is_misspelled(s: str) -> bool:
    """True when the word resolves to a month but is not spelled like one."""
    s = s.lower()
    if any(s.startswith(k) for k in _KA_MONTHS):
        return False
    return _month_ka(s) is not None


#: The case forms a month takes in a date: nominative, genitive, dative, and the "-დან",
#: "-მდე", "-ში" and "-ით" forms (August's genitive and dative are the same word). A misspelled
#: month is corrected to the nearest of these, so the correction keeps the case the act used.
_KA_MONTH_FORMS: dict[int, tuple[str, ...]] = {
    1: ("იანვარი", "იანვრის", "იანვარს", "იანვრიდან", "იანვრამდე", "იანვარში", "იანვრით"),
    2: (
        "თებერვალი",
        "თებერვლის",
        "თებერვალს",
        "თებერვლიდან",
        "თებერვლამდე",
        "თებერვალში",
        "თებერვლით",
    ),
    3: ("მარტი", "მარტის", "მარტს", "მარტიდან", "მარტამდე", "მარტში", "მარტით"),
    4: ("აპრილი", "აპრილის", "აპრილს", "აპრილიდან", "აპრილამდე", "აპრილში", "აპრილით"),
    5: ("მაისი", "მაისის", "მაისს", "მაისიდან", "მაისამდე", "მაისში", "მაისით"),
    6: ("ივნისი", "ივნისის", "ივნისს", "ივნისიდან", "ივნისამდე", "ივნისში", "ივნისით"),
    7: ("ივლისი", "ივლისის", "ივლისს", "ივლისიდან", "ივლისამდე", "ივლისში", "ივლისით"),
    8: ("აგვისტო", "აგვისტოს", "აგვისტოდან", "აგვისტომდე", "აგვისტოში", "აგვისტოთი"),
    9: (
        "სექტემბერი",
        "სექტემბრის",
        "სექტემბერს",
        "სექტემბრიდან",
        "სექტემბრამდე",
        "სექტემბერში",
        "სექტემბრით",
    ),
    10: (
        "ოქტომბერი",
        "ოქტომბრის",
        "ოქტომბერს",
        "ოქტომბრიდან",
        "ოქტომბრამდე",
        "ოქტომბერში",
        "ოქტომბრით",
    ),
    11: ("ნოემბერი", "ნოემბრის", "ნოემბერს", "ნოემბრიდან", "ნოემბრამდე", "ნოემბერში", "ნოემბრით"),
    12: (
        "დეკემბერი",
        "დეკემბრის",
        "დეკემბერს",
        "დეკემბრიდან",
        "დეკემბრამდე",
        "დეკემბერში",
        "დეკემბრით",
    ),
}


#: How far a misspelled month may lie from the form it is corrected to. A typo in the stem and
#: one in the ending are two edits ("აგვუისტას" / "აგვისტოს"). A word further away carries an
#: ending the forms do not list, "-სთვის" or "-ვე", and its nearest form says something else:
#: "აგისტოსთვის" is nearest to "აგვისტოთი", another case.
_MONTH_CORRECTION_DISTANCE = 2


def month_ka_correction(word: str) -> str | None:
    """How a misspelled Georgian month should be written: its case form nearest to ``word``.

    "აგვუისტას" becomes "აგვისტოს" and "აპრლიდან" "აპრილიდან". None when the word is not a
    month at all, and when even the nearest form is more than two edits away: a wrong
    correction is worse than none. A tie goes to the form listed first.
    """
    month = _month_ka(word)
    if month is None:
        return None
    low = word.lower()
    forms = _KA_MONTH_FORMS[month]
    # an uncapped distance: capped, every far form would tie at cap + 1
    distance = {f: edit_distance(low, f, cap=max(len(low), len(f))) for f in forms}
    best = min(forms, key=distance.__getitem__)
    return best if distance[best] <= _MONTH_CORRECTION_DISTANCE else None


def _month_en(s: str) -> int | None:
    return _EN_MONTHS.get(s.lower()[:3])


def find_dates(text: str) -> list[RuleHit]:
    found: dict[tuple[int, int], tuple[date, str, str | None]] = {}

    def add(m: re.Match[str], dt: date | None, month_word: str | None = None) -> None:
        if dt is None:
            return
        note = None
        if month_word and month_ka_is_misspelled(month_word):
            note = f"month name appears misspelled: {month_word!r}"
        found[(m.start(), m.end())] = (dt, m.group(0), note)

    for m in _DATE_KA_YMD.finditer(text):
        mo = _month_ka(m.group(3))
        add(m, _safe_date(int(m.group(1)), mo, int(m.group(2))) if mo else None, m.group(3))
    for m in _DATE_KA_DMY.finditer(text):
        mo = _month_ka(m.group(2))
        add(m, _safe_date(int(m.group(3)), mo, int(m.group(1))) if mo else None, m.group(2))
    for m in _DATE_EN_DMY.finditer(text):
        mo = _month_en(m.group(2))
        add(m, _safe_date(int(m.group(3)), mo, int(m.group(1))) if mo else None)
    for m in _DATE_EN_MDY.finditer(text):
        mo = _month_en(m.group(1))
        add(m, _safe_date(int(m.group(3)), mo, int(m.group(2))) if mo else None)
    for m in _DATE_ISO.finditer(text):
        add(m, _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    for m in _DATE_NUMERIC.finditer(text):
        add(m, _safe_date(int(m.group(3)), int(m.group(2)), int(m.group(1))))

    hits: list[RuleHit] = []
    last_end = -1
    for (start, end), (dt, raw, note) in sorted(
        found.items(), key=lambda kv: (kv[0][0], -kv[0][1])
    ):
        # drop hits contained in or overlapping an earlier, longer one
        # (e.g. "1 აგვისტოდან 2026" straddling "2026 წლის 1 აგვისტოდან" and the next date)
        if start < last_end:
            continue
        last_end = end
        ft = _classify(
            text,
            start,
            end,
            (
                FactType.execution_period,
                FactType.delivery_deadline,
                FactType.validity_period,
                FactType.act_date,
                FactType.contract_date,
            ),
        )
        hits.append(
            RuleHit(
                type=ft or FactType.date,
                value=dt.isoformat(),
                raw_text=raw,
                start=start,
                end=end,
                normalized=dt.isoformat(),
                confidence=0.7 if ft else 0.5,
                note=note,
            )
        )
    return hits


# --- identification codes ----------------------------------------------------------------------

_ID_CODE = re.compile(
    r"(?:ს\s*/\s*კ|ს\s*\.\s*კ\s*\.|საიდენტიფიკაციო\s*(?:კოდი|ნომერი|№)?|პირადი\s*ნომერი|"
    # English labels start at a word boundary: "paid 204567890" is not "ID 204567890"
    r"(?<![A-Za-z])(?:ID(?![A-Za-z])\s*(?:code|no\.?|number|№)?|"
    r"identification\s*(?:code|number|no\.?)|tax\s*id(?![A-Za-z])))"
    r"\s*[:№#\-]?\s*(\d{9}|\d{11})(?!\d)",
    re.IGNORECASE,
)


def find_id_codes(text: str) -> list[RuleHit]:
    return [
        RuleHit(
            type=FactType.party_id_code,
            value=m.group(1),
            raw_text=m.group(0),
            start=m.start(),
            end=m.end(),
            normalized=m.group(1),
            confidence=0.85,
        )
        for m in _ID_CODE.finditer(text)
    ]


# --- percentages -------------------------------------------------------------------------------

_PCT = re.compile(r"(?<![\d.,])(\d{1,3}(?:[.,]\d{1,2})?)\s*%")


def find_percentages(text: str) -> list[RuleHit]:
    hits: list[RuleHit] = []
    for m in _PCT.finditer(text):
        v = parse_number(m.group(1))
        if v is None or v > 100:
            continue
        ft = _classify(
            text,
            m.start(),
            m.end(),
            (FactType.penalty, FactType.vat_status, FactType.payment_terms),
        )
        hits.append(
            RuleHit(
                type=ft or FactType.percentage,
                value=f"{v:g}%",
                raw_text=m.group(0),
                start=m.start(),
                end=m.end(),
                normalized=v,
                unit="%",
                confidence=0.65 if ft else 0.45,
            )
        )
    return hits


# --- durations ---------------------------------------------------------------------------------

_DUR_KA = re.compile(
    r"(?<!\d)(\d{1,4})\s*(?:\(\s*[ა-ჰ ]+\s*\)\s*)?(კალენდარული|სამუშაო|საბანკო)?\s*"
    # "დღ(?!გ)": დღგ is VAT, and "18 დღგ-ს ჩათვლით" is not eighteen days
    r"(დღ(?!გ)|კვირ|თვ|წელ|წლ)[ა-ჰ]*",
    re.IGNORECASE,
)
_DUR_EN = re.compile(
    r"(?<!\d)(\d{1,4})\s*(?:\([a-z ]+\)\s*)?(calendar|working|business|banking)?\s*"
    r"(day|week|month|year)s?\b",
    re.IGNORECASE,
)
_UNIT_KA = {"დღ": "day", "კვირ": "week", "თვ": "month", "წელ": "year", "წლ": "year"}
_QUAL_KA = {"კალენდარული": "calendar", "სამუშაო": "working", "საბანკო": "banking"}


def find_durations(text: str) -> list[RuleHit]:
    hits: list[RuleHit] = []
    for pat, is_ka in ((_DUR_KA, True), (_DUR_EN, False)):
        for m in pat.finditer(text):
            n = int(m.group(1))
            qual = m.group(2).lower() if m.group(2) else None
            unit = m.group(3).lower()
            if is_ka and unit.startswith("წ") and n >= 1900:
                continue  # "2026 წლის ..." is a date ("of the year 2026"), not a duration
            if is_ka:
                # group 3 is exactly one of the stems, so it is the key as it stands; slicing
                # it to three letters turned "კვირ" into "კვი" and weeks into "2 კვირs"
                unit = _UNIT_KA.get(unit, unit)
                qual = _QUAL_KA.get(qual, qual) if qual else None
            ft = _classify(
                text,
                m.start(),
                m.end(),
                (
                    FactType.warranty_period,
                    FactType.delivery_deadline,
                    FactType.execution_period,
                    FactType.payment_terms,
                    FactType.validity_period,
                ),
            )
            label = f"{n} {qual + ' ' if qual else ''}{unit}{'s' if n != 1 else ''}"
            hits.append(
                RuleHit(
                    type=ft or FactType.duration,
                    value=label,
                    raw_text=m.group(0),
                    start=m.start(),
                    end=m.end(),
                    normalized=f"P{n}{unit[0].upper()}" if unit != "week" else f"P{n}W",
                    unit=unit,
                    confidence=0.7 if ft else 0.45,
                )
            )
    return hits


# --- contract numbers --------------------------------------------------------------------------

# The document kinds that carry a reference number. "contract_number" is documented as the
# "contract/offer/tender reference number", but the pattern only knew about contracts, so an
# offer number was never found. A preceding keyword is still required: a bare "№ 12" in a
# table is a row number far more often than a reference.
_REFERENCED_DOC = (
    r"ხელშეკრულებ[ა-ჰ]*|შეთანხმებ[ა-ჰ]*|წინადადებ[ა-ჰ]*|შემოთავაზებ[ა-ჰ]*|განაცხად[ა-ჰ]*|"
    r"ტენდერ[ა-ჰ]*|შესყიდვ[ა-ჰ]*|ინვოის[ა-ჰ]*|აქტ[ა-ჰ]*|"
    r"contract|agreement|offer|proposal|quotation|quote|tender|invoice|order|act"
)
# A number sign, or an "N" that stands alone as one: "N 12", "N12", "No.", "Nº", "N:". The
# letter must not be the start of a word - the old "N[oº]?\.?" read the n of "number" as a
# marker and returned "umber" - and the reference itself must contain a digit.
_NUMBER_MARKER = r"(?:№|#|(?<![A-Za-z])N(?:[oº°]\.?|\.|:)?(?![A-Za-z]))"
_CONTRACT_NO = re.compile(
    rf"(?:{_REFERENCED_DOC})\s*{_NUMBER_MARKER}\s*"
    r"((?=[A-Za-z0-9\-/_.]*\d)[A-Za-z0-9][A-Za-z0-9\-/_.]{0,30}[A-Za-z0-9])",
    re.IGNORECASE,
)


def find_contract_numbers(text: str) -> list[RuleHit]:
    return [
        RuleHit(
            type=FactType.contract_number,
            value=m.group(1),
            raw_text=m.group(0),
            start=m.start(),
            end=m.end(),
            normalized=m.group(1),
            confidence=0.8,
        )
        for m in _CONTRACT_NO.finditer(text)
        if any(ch.isdigit() for ch in m.group(1))
    ]


# --- bank accounts & cadastral codes -----------------------------------------------------------

# Georgian IBAN, compact or printed in groups of four ("GE29 NB00 0000 ...").
_IBAN_GE = re.compile(r"\bGE\d{2}[ \u00a0]?[A-Z]{2}\d{2}(?:[ \u00a0]?\d{4}){3}[ \u00a0]?\d{2}\b")
_SWIFT = re.compile(r"\b[A-Z]{4}GE[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b")
_SPACE_CHARS = re.compile(r"[ \u00a0]")
#: A cadastral code: the parcel, "01.02.03.456", and a building or unit under it written with up
#: to three more groups ("01.19.21.004.024.01.513"). The full stop that ends a sentence may
#: follow it ("კოდი: 01.02.03.456."); more digits, or a dot and more digits, may not. Shared
#: with the anchors, so an act's code and a fact's code are read by one pattern.
CADASTRAL_RE = re.compile(
    r"(?<![\d.])\d{2}\.\d{2}\.\d{2}\.\d{3}(?:\.\d{3}(?:\.\d{2}(?:\.\d{3})?)?)?(?!\d|\.\d)"
)


def find_bank_accounts(text: str) -> list[RuleHit]:
    """Georgian IBANs and bank codes.

    An IBAN that fails its ISO 13616 checksum is kept, with a note and a lower confidence,
    rather than dropped: a mistyped account number is precisely what the acceptance check
    exists to report, and dropping it would turn "wrong account" into "no account". A bank code
    is taken only where ``unlabelled_bic_plausible`` accepts it, because the pattern alone also
    fits ordinary capitalised words.
    """
    hits: list[RuleHit] = []
    for m in _IBAN_GE.finditer(text):
        iban = _SPACE_CHARS.sub("", m.group(0))
        valid = iban_is_valid(iban)
        hits.append(
            RuleHit(
                type=FactType.bank_account,
                value=iban,
                raw_text=m.group(0),
                start=m.start(),
                end=m.end(),
                normalized=iban,
                unit="IBAN",
                confidence=0.9 if valid else 0.6,
                note=None if valid else "IBAN checksum does not validate (possible typo)",
            )
        )
    for m in _SWIFT.finditer(text):
        if not unlabelled_bic_plausible(text, m.start(), m.end()):
            continue
        hits.append(
            RuleHit(
                type=FactType.bank_code,
                value=m.group(0),
                raw_text=m.group(0),
                start=m.start(),
                end=m.end(),
                normalized=m.group(0),
                unit="SWIFT",
                confidence=0.8,
            )
        )
    return hits


def find_cadastral_codes(text: str) -> list[RuleHit]:
    return [
        RuleHit(
            type=FactType.cadastral_code,
            value=m.group(0),
            raw_text=m.group(0),
            start=m.start(),
            end=m.end(),
            normalized=m.group(0),
            confidence=0.85,
        )
        for m in CADASTRAL_RE.finditer(text)
    ]


# --- entry point -------------------------------------------------------------------------------


def run_rules(text: str) -> list[RuleHit]:
    """All deterministic extractors over one piece of text, sorted by position."""
    hits = [
        *find_amounts(text),
        *find_dates(text),
        *find_id_codes(text),
        *find_percentages(text),
        *find_durations(text),
        *find_contract_numbers(text),
        *find_bank_accounts(text),
        *find_cadastral_codes(text),
    ]
    return sorted(hits, key=lambda h: (h.start, h.end))


# --- normalising a value that did not come from these rules -------------------------------------

_MONEY_TYPES = frozenset({FactType.price, FactType.total_amount, FactType.amount})
_TIME_TYPES = frozenset(
    {
        FactType.contract_date,
        FactType.date,
        FactType.delivery_deadline,
        FactType.execution_period,
        FactType.validity_period,
        FactType.warranty_period,
        FactType.duration,
    }
)
_PERCENT_TYPES = frozenset({FactType.percentage, FactType.vat_status})
_MIXED_TYPES = frozenset({FactType.penalty, FactType.payment_terms})
_ID_DIGITS = re.compile(r"(?<!\d)(\d{9}|\d{11})(?!\d)")
_BIC_SHAPE = re.compile(r"[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?")
_IBAN_SHAPE = re.compile(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}")
_REFERENCE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-/_.]{0,30}[A-Za-z0-9]")
_LEADING_MARKER = re.compile(r"^\s*(?:№|#|N[oº°]?\.?|N:)\s*", re.IGNORECASE)
#: A hit has to cover this share of the value to stand for it. "24 months" is a duration;
#: "30% advance, remainder within 10 days" is a summary that merely contains one.
_MIN_COVER = 0.5


def _only(hits: list[RuleHit], value: str) -> RuleHit | None:
    distinct = {str(h.normalized) for h in hits}
    if len(distinct) != 1:
        return None
    hit = hits[0]
    return hit if len(hit.raw_text.strip()) >= _MIN_COVER * len(value) else None


def normalize_fact_value(ft: FactType, value: str) -> str | float | None:
    """The canonical form the rules would have given ``value`` for a fact of type ``ft``.

    A fact from the LLM arrives as display text ("1000 GEL", "24 months"); the same fact from
    a rule arrives with ``normalized`` set (1000.0, "P24M"). The merger keys facts on the
    canonical form, so without this every LLM value was a different key from the matching
    rule value, and a document where both sources agreed was reported as a conflict. Running
    the value through the very functions that normalise rule hits is what guarantees the two
    keys are computed the same way. Returns None when the value is not a single recognisable
    entity - free text is then compared as text.
    """
    v = value.strip()
    if not v:
        return None
    if ft in (FactType.party_id_code, FactType.id_code):
        ids = {m.group(1) for m in _ID_DIGITS.finditer(v)}
        return ids.pop() if len(ids) == 1 else None
    if ft in (FactType.bank_account, FactType.bank_code):
        compact = re.sub(r"\s", "", v).upper()
        if _IBAN_SHAPE.fullmatch(compact) or _BIC_SHAPE.fullmatch(compact):
            return compact
        return None
    if ft == FactType.cadastral_code:
        m = CADASTRAL_RE.search(v)
        return m.group(0) if m else None
    if ft == FactType.contract_number:
        m = _REFERENCE_TOKEN.fullmatch(_LEADING_MARKER.sub("", v))
        return m.group(0) if m and any(ch.isdigit() for ch in m.group(0)) else None
    if ft in _MONEY_TYPES:
        hit = _only(find_amounts(v), v)
        if hit is not None:
            return hit.normalized
        numbers = {parse_number(n) for n in re.findall(_NUM_RE, v)}
        numbers.discard(None)
        if len(numbers) == 1 and len(re.sub(r"[^\d]", "", v)) * 2 >= len(re.sub(r"\s", "", v)):
            return numbers.pop()
        return None
    finders: list[Callable[[str], list[RuleHit]]] = []
    if ft in _TIME_TYPES:
        finders = [find_dates, find_durations]
    elif ft in _PERCENT_TYPES:
        finders = [find_percentages]
    elif ft in _MIXED_TYPES:
        finders = [find_percentages, find_amounts, find_durations]
    for find in finders:
        hit = _only(find(v), v)
        if hit is not None:
            return hit.normalized
    return None


def value_supported_by_quote(normalized: str | float, quote: str) -> bool:
    """Whether a canonical value can be read back out of the quote that supposedly states it.

    ``quote_in_chunk`` proves the quote is in the document; it says nothing about whether the
    value the model wrote next to it came from that quote. A model can copy a real sentence and
    attach a number it made up. This reads the quote with the same rules and asks whether any
    value found there is the one claimed.
    """
    if isinstance(normalized, float):
        for m in re.finditer(_NUM_RE, quote):
            n = parse_number(m.group(0))
            if n is not None and amounts_equal(n, normalized):
                return True
        return False
    target = str(normalized)
    compact_quote = re.sub(r"\s", "", quote).upper()
    if re.sub(r"\s", "", target).upper() in compact_quote:
        return True
    found = {str(h.normalized) for h in [*find_dates(quote), *find_durations(quote)]}
    if target in found:
        return True
    # "P30D" against "ოცდაათი (30) კალენდარული დღე": the duration pattern cannot read the
    # count through the parenthesis, but the count is plainly in the quote.
    duration = re.fullmatch(r"P(\d+)[DWMY]", target)
    if duration:
        return re.search(rf"(?<!\d){duration.group(1)}(?!\d)", quote) is not None
    return False
