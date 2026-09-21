"""Deterministic extraction (no LLM): amounts, dates, ID codes, percentages, durations,
contract numbers — in Georgian and English.

Each hit carries the character span inside the input text so the caller can attach
provenance. Classification into procurement-specific types uses nearby keywords; anything
ambiguous stays generic (``amount``/``date``/...) and is left for the LLM to refine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from procurement_agent.extraction.normalize import edit_distance
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

_CURRENCY_ALIASES = {
    "₾": "GEL", "gel": "GEL", "ლარი": "GEL", "ლარის": "GEL", "ლარს": "GEL", "ლარ": "GEL",
    "$": "USD", "usd": "USD", "დოლარი": "USD", "დოლარის": "USD", "დოლარ": "USD",
    "€": "EUR", "eur": "EUR", "ევრო": "EUR", "ევროს": "EUR",
    "£": "GBP", "gbp": "GBP",
}  # fmt: skip
_CUR_RE = r"₾|\$|€|£|GEL|USD|EUR|GBP|ლარ(?:ი|ის|ს)?|დოლარ(?:ი|ის)?|ევრო(?:ს)?"
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


def _classify(text: str, start: int, end: int, candidates: tuple[FactType, ...]) -> FactType | None:
    """Pick the candidate whose keyword sits closest to the hit *within the same sentence*.

    At the same distance the longer keyword wins, because Georgian builds words with prefixes
    and stems therefore nest: "ფას" (price) matches two characters inside "საფასურ" (fee), and
    without this the fee in an acceptance act is labelled a unit price. Remaining ties are
    resolved by candidate order, so callers list the more specific type first.
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

    best: tuple[int, int, FactType] | None = None
    for ft in candidates:
        for kw in KEYWORDS.get(ft, ()):
            pat = keyword_pattern(kw)
            pre_matches = list(pat.finditer(pre))
            if pre_matches:
                dist = len(pre) - pre_matches[-1].end()
                # sort key: nearer first, then longer keyword
                if best is None or (dist, -len(kw)) < (best[0], -best[1]):
                    best = (dist, len(kw), ft)
            m_post = pat.search(post)
            if m_post:
                dist = m_post.start() + _AFTER_PENALTY
                if best is None or (dist, -len(kw)) < (best[0], -best[1]):
                    best = (dist, len(kw), ft)
    return best[2] if best else None


def parse_number(s: str) -> float | None:
    s = s.strip().replace("\u00a0", " ")
    if not s:
        return None
    # decide decimal separator: a trailing [.,] followed by 1-2 digits
    m = re.fullmatch(r"(.*?)([.,])(\d{1,2})", s)
    if m and (len(m.group(1)) > 3 or not re.search(r"[ ,.]", m.group(1))):
        integer, frac = m.group(1), m.group(3)
    else:
        integer, frac = s, ""
    integer = re.sub(r"[ ,.]", "", integer)
    if not integer.isdigit():
        return None
    return float(f"{integer}.{frac}" if frac else integer)


def _norm_currency(raw: str) -> str | None:
    return _CURRENCY_ALIASES.get(raw.strip().lower()) or _CURRENCY_ALIASES.get(raw.strip())


# --- amounts -----------------------------------------------------------------------------------

# "48 750,00 (ორმოცდარვა ათას ...) ლარს": contracts spell the number out between digits and currency
_PAREN = r"(?:\([^()]{0,160}\)\s*)?"
_AMOUNT_AFTER = re.compile(
    rf"(?<![\d.,])({_NUM_RE})\s*{_PAREN}({_CUR_RE})(?![A-Za-zა-ჰ])", re.IGNORECASE
)
_AMOUNT_BEFORE = re.compile(rf"({_CUR_RE})\s*({_NUM_RE})(?![\d])", re.IGNORECASE)


def find_amounts(text: str) -> list[RuleHit]:
    hits: list[RuleHit] = []
    seen: set[tuple[int, int]] = set()
    for pat, num_group, cur_group in ((_AMOUNT_AFTER, 1, 2), (_AMOUNT_BEFORE, 2, 1)):
        for m in pat.finditer(text):
            span = (m.start(), m.end())
            if any(s <= span[0] < e or s < span[1] <= e for s, e in seen):
                continue
            value = parse_number(m.group(num_group))
            if value is None:
                continue
            cur = _norm_currency(m.group(cur_group))
            ft = _classify(text, *span, (FactType.total_amount, FactType.penalty, FactType.price))
            seen.add(span)
            hits.append(
                RuleHit(
                    type=ft or FactType.amount,
                    value=f"{value:,.2f} {cur}" if cur else f"{value:,.2f}",
                    raw_text=m.group(0),
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
    r"ID\s*(?:code|no\.?|number|№)?|identification\s*(?:code|number|no\.?)|tax\s*id)"
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
    r"(დღ|კვირ|თვ|წელ|წლ)[ა-ჰ]*",
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
                unit = _UNIT_KA.get(unit[:2] if unit.startswith("წლ") else unit[:3], unit)
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
    r"ტენდერ[ა-ჰ]*|შესყიდვ[ა-ჰ]*|ინვოის[ა-ჰ]*|აქტ[ი-ჰ]*|"
    r"contract|agreement|offer|proposal|quotation|quote|tender|invoice|order|act"
)
_CONTRACT_NO = re.compile(
    rf"(?:{_REFERENCED_DOC})\s*(?:№|N[oº]?\.?|#)\s*"
    r"([A-Za-z0-9][A-Za-z0-9\-/_.]{0,30}[A-Za-z0-9])",
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
    ]


# --- bank accounts & cadastral codes -----------------------------------------------------------

_IBAN_GE = re.compile(r"\bGE\d{2}[A-Z]{2}\d{16}\b")
_SWIFT = re.compile(r"\b[A-Z]{4}GE[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b")
_CADASTRAL = re.compile(r"(?<![\d.])\d{2}\.\d{2}\.\d{2}\.\d{3}(?:\.\d{3})?(?![\d.])")


def find_bank_accounts(text: str) -> list[RuleHit]:
    hits = [
        RuleHit(
            type=FactType.bank_account,
            value=m.group(0),
            raw_text=m.group(0),
            start=m.start(),
            end=m.end(),
            normalized=m.group(0),
            unit="IBAN",
            confidence=0.9,
        )
        for m in _IBAN_GE.finditer(text)
    ]
    hits += [
        RuleHit(
            type=FactType.bank_account,
            value=m.group(0),
            raw_text=m.group(0),
            start=m.start(),
            end=m.end(),
            normalized=m.group(0),
            unit="SWIFT",
            confidence=0.8,
        )
        for m in _SWIFT.finditer(text)
    ]
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
        for m in _CADASTRAL.finditer(text)
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
