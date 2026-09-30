"""Bill-of-quantities comparison: template versus each bidder's returned workbook.

Implements the client's BOQ instructions: compare quantities on every sheet, list added and
removed positions separately per bidder and keep them out of the general analysis, merge all
bidders into the template's sheets stacked vertically, and produce a Summary with the highest
total in red and the lowest in green.

What is ranked is not the bottom line a bidder typed into their Summary. That number carries
their own quantities, lines they added, lines they left out and whatever their spreadsheet does
with it. Every bid is instead *evaluated* the way ``docs/GOLD_SPEC.md`` section 5 lays down:

* a **position** is a template row whose amount cell is a product of that row (``=O12*G12``) or
  a number; a row whose amount sums other rows is a subtotal, a row with no amount a heading.
  A row carrying a quantity or a unit is a position even where the template forgot its formula;
* each position costs the template quantity times the bidder's unit rate (where the template
  leaves the quantity blank, the bidder's own amount, listed as ``filled_blank``);
* ``evaluated_cost`` sums those; added lines never count, removed and unpriced ones contribute
  nothing and are listed;
* the bidder's own markup (stated total incl. VAT / stated TOTAL COST) turns a cost into a
  total, and the **common scope** - the positions every bidder priced - is what red and green
  are decided on, so a bid that left half the work out cannot come out cheapest for it.

Beyond that it cross-foots the arithmetic. A BOQ is a spreadsheet the bidder controls, so the
totals it reports are a claim, not a fact: every line is recomputed from quantity x rate, every
sheet from its positions, and the Summary from the sheets and down its overheads/profit/VAT
chain. Cells where a formula was replaced by a typed-in number are flagged too, which is only
visible because the workbook is read twice, once for cached values and once for formulas.

Entirely deterministic - no LLM is involved at any point.
"""

from __future__ import annotations

import argparse
import difflib
import itertools
import math
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from procurement_agent.checks.findings import CheckReport, Finding, Severity
from procurement_agent.extraction.normalize import normalize_spaces, parse_number
from procurement_agent.report.xlsx_safe import cell_safe

#: Sheets that carry no priced lines.
SKIP_SHEETS = frozenset(
    {"COVER PAGE", "INSTRUCTION TO BIDDERS", "INSTRUCTIONS", "SUMMARY", "READ ME", "README"}
)
_SUMMARY = "SUMMARY"
#: A Summary by another name: "BOQ SUMMARY", "Summary of Prices", "კრებსითი".
_SUMMARY_NAME = re.compile(r"(?<![A-Z])SUMMARY(?![A-Z])|კრებსით|შეჯამებ", re.IGNORECASE)

#: Header labels we understand, normalised. The prototype required the exact lowercase strings
#: "quantity" and "unit" and silently skipped any sheet that spelled them differently.
_HEADER_SYNONYMS: dict[str, tuple[str, ...]] = {
    "no": ("no", "no.", "item", "item no", "№", "#"),
    "desc": (
        "description",
        "descriptions",
        "description of works",
        "work description",
        "აღწერა",
        "დასახელება",
        "სამუშაოს დასახელება",
        "სამუშაოს აღწერა",
        "სამუშაოების დასახელება",
    ),
    "qty": ("quantity", "qty", "q-ty", "qnt", "quant", "quantities", "amount of work", "რაოდენობა"),
    "unit": (
        "unit",
        "units",
        "unit of measure",
        "uom",
        "ერთეული",
        "განზ. ერთეული",
        "ზომის ერთეული",
        "განზომილება",
    ),
    "rate_materials": ("materials", "material", "მასალა", "მასალები"),
    "sum_materials": ("materials sum", "material sum"),
    "rate_labor": ("labor", "labour", "ხელფასი", "შრომა"),
    "sum_labor": ("labor sum", "labour sum"),
    # _norm_label strips surrounding whitespace, so "delivery " needs no entry of its own
    "rate_delivery": ("delivery", "transportation", "ტრანსპორტი", "ტრანსპორტირება", "მიწოდება"),
    "sum_delivery": ("delivery sum", "transportation sum"),
    # a currency the label names ("Rate in USD", "ღირებულება ლარში") is taken off first
    "rate_total": (
        "rate",
        "unit rate",
        "unit price",
        "ერთეულის ფასი",
        "ერთ. ფასი",
        "ერთეულის ღირებულება",
    ),
    "amount": (
        "amount",
        "total",
        "total amount",
        "total price",
        "ღირებულება",
        "სულ",
        "ჯამი",
    ),
}
_LABEL_TO_KEY = {label: key for key, labels in _HEADER_SYNONYMS.items() for label in labels}
#: The columns a number is read from; text in one of them is reported, never read as 0.
_NUMERIC_KEYS = (
    "qty",
    "rate_materials",
    "sum_materials",
    "rate_labor",
    "sum_labor",
    "rate_delivery",
    "sum_delivery",
    "rate_total",
    "amount",
)
_RATE_KEYS = ("rate_total", "rate_materials", "rate_labor", "rate_delivery")
#: The columns a template computes (quantity x rate): a number typed over one is flagged.
_TOTAL_KEYS = ("sum_materials", "sum_labor", "sum_delivery", "amount")

_HEADER_SCAN_ROWS = 14
_HEADER_SCAN_COLS = 30
#: Line totals are money; a cent of drift is rounding, not an error.
_MONEY_TOL = 0.01
_RATE_OUTLIER_FACTOR = 3.0
#: A matched position whose description reads this alike (after folding) is the same position
#: with a typo; below it, two descriptions under one item number are two different positions.
_DESC_SIMILARITY = 0.9
#: More than this share of formula cells without a cached value: the file was saved by a tool
#: that does not calculate (openpyxl, a script), and every number read from it would be None.
_UNCACHED_SHARE = 0.5
#: What sits in an item-number column: a section code ("A", "A1") or a position ("1.1", "2.1.3").
_ITEM_NO = re.compile(r"^(?:[A-Za-z]\d{0,3}|\d{1,3}(?:\.\d{1,3}){0,4})\.?$")
#: The row that closes a priced sheet: "SUB-TOTAL 3. DOORS", "ქვეჯამი 3. კარები".
_SUBTOTAL = re.compile(r"^\s*(?:sub[\s-]?total|ქვეჯამ)", re.IGNORECASE)
#: A closing row by another name: "TOTAL CARRIED TO SUMMARY", "Carried to Summary", "სულ ჯამი",
#: "ჯამი". Only ever read as the sheet's closing total when the row is a sum after every
#: position - a position may well be described "Total station survey".
_CLOSING = re.compile(r"^\s*(?:sub[\s-]?total|total|carried|ქვეჯამ|სულ|ჯამ)", re.IGNORECASE)


def _has_letter(text: str) -> bool:
    """A real description contains a letter - in any script: "Ø12" is a rebar diameter, "2" is a
    column number. ``[A-Za-zა-ჰ]`` missed "Ø" and every position described by a diameter alone
    fell out of the evaluation."""
    return any(ch.isalpha() for ch in text)


#: A cell reference inside a formula, not preceded by a letter (a function name such as LOG10)
#: and not followed by "(" (a function call).
_CELL_REF = re.compile(r"(?<![A-Za-z_\d.])\$?([A-Z]{1,3})\$?(\d+)(?![\d(A-Za-z_])")
#: A reference to another sheet: ``'2. CIVIL WORKS'!J30`` or ``Sheet1!$A$1``.
_SHEET_REF = re.compile(r"(?:'((?:[^']|'')+)'|([^\W\d][\w.]*))!\$?([A-Z]{1,3})\$?(\d+)")

# Summary rows, recognised by their label (folded to lower case).
#: The tax by its names: "VAT", "Value added tax", "დღგ", "დამატებული ღირებულების გადასახადი" -
#: never the letters inside a word ("private", "Vatican").
_VAT_NAME = (
    r"(?<![a-zა-ჰ])(?:vat|value[\s-]+added[\s-]+tax|დღგ|"
    r"დამატებული\s+ღირებულების\s+გადასახად[ა-ჰ]*)(?![a-zა-ჰ])"
)
#: A rate written into the label: "incl. 18% VAT", "Add 18% VAT".
_LABEL_RATE = r"(?:\d{1,2}(?:[.,]\d+)?\s*%\s*)?"
#: The Georgian "including" / "without" after the tax, in any case form: "დღგ-ს ჩათვლით",
#: "დღგ-ის ჩათვლით", "გადასახადის ჩათვლით".
_KA_VAT = r"(?:დღგ(?:-?ის|-?ს)?|გადასახადის)\s*"
_INCL_VAT = re.compile(
    rf"\b(?:incl(?:\.|uding|usive)?|with)\s*(?:of\s+)?(?:the\s+)?{_LABEL_RATE}{_VAT_NAME}"
    rf"|{_VAT_NAME}\s+incl|{_KA_VAT}ჩათვლით",
    re.IGNORECASE,
)
_EXCL_VAT = re.compile(
    rf"\b(?:excl(?:\.|uding|usive)?|without|before|net\s+of)\s*(?:of\s+)?(?:the\s+)?"
    rf"{_LABEL_RATE}{_VAT_NAME}|{_KA_VAT}გარეშე",
    re.IGNORECASE,
)
#: The VAT row: the tax's name opens the label, after an "Add" or its rate at most ("VAT 18%",
#: "Value added tax @ 18%", "Add 18% VAT", "დამატებული ღირებულების გადასახადი 18%").
_VAT = re.compile(rf"^\s*(?:add(?:ing)?\s+)?{_LABEL_RATE}{_VAT_NAME}", re.IGNORECASE)
_TOTAL_COST = re.compile(r"\btotal\s+cost|ჯამური\s+ღირებულება", re.IGNORECASE)
#: Overheads and profit by their usual names, English and Georgian: "Indirect costs", "General
#: expenses", "არაპირდაპირი ხარჯები"; "Margin", "Mark-up", "გეგმიური დაგროვება" (the planned
#: accumulation of a Georgian estimate, i.e. the contractor's profit).
_OVERHEADS = re.compile(
    r"overhead|indirect\s+cost|general\s+expense|ზედნადებ|არაპირდაპირ", re.IGNORECASE
)
_PROFIT = re.compile(r"profit|margin|mark-?up|მოგება|გეგმიური\s+დაგროვ", re.IGNORECASE)

#: Cyrillic letters that print exactly like Latin ones: "м2" typed on a Russian keyboard is m2.
_HOMOGLYPHS = str.maketrans(
    "\u0430\u043c\u0442\u043a\u0441\u0435\u043e\u0440\u0445\u0443", "amtkceopxy"
)
#: Unit spellings that mean the same measure: one table per measure, its English names singular
#: and plural and its Georgian abbreviation and full word ("t", "tn", "ტ", "ტნ", "ტონა"). "item"
#: and "pcs" are deliberately NOT merged: a lump sum and a count are different things, and the
#: difference is worth a question; nor is a set a count of pieces. A metre and a linear metre
#: are one measure (so says the mutation harness too). Plurals are listed, never derived by
#: dropping an "s": "ls" (lump sum) is not "l" (a litre). Spaces and dots are folded out first,
#: so "square metre" is listed as "squaremetre" and "მ.გრძ." as "მგრძ"; a Georgian word cut
#: after its stem ("ცალ.", "ტონ.") is listed as cut.
_UNIT_ALIASES: dict[str, str] = {
    **dict.fromkeys(("m2", "m²", "sqm", "m^2", "მ2", "მ²", "კვმ"), "m2"),
    **dict.fromkeys(("m3", "m³", "cum", "cbm", "m^3", "მ3", "მ³", "კუბმ"), "m3"),
    **dict.fromkeys(
        (
            *("squaremetre", "squaremetres", "squaremeter", "squaremeters", "sqmetre"),
            *("კვადრატულიმეტრი",),
        ),
        "m2",
    ),
    **dict.fromkeys(
        ("cubicmetre", "cubicmetres", "cubicmeter", "cubicmeters", "კუბურიმეტრი"), "m3"
    ),
    **dict.fromkeys(
        (
            *("lm", "rm", "lnm", "linm", "runm", "გრძმ", "მგრძ", "m", "მ", "მეტრი"),
            *("metre", "metres", "meter", "meters"),
            *("runningmetre", "runningmetres", "runningmeter", "runningmeters"),
            *("linearmetre", "linearmetres", "linearmeter", "linearmeters", "გრძივიმეტრი"),
        ),
        "lm",
    ),
    **dict.fromkeys(
        ("km", "kms", "კმ", "kilometre", "kilometres", "kilometer", "kilometers", "კილომეტრი"),
        "km",
    ),
    **dict.fromkeys(("ha", "ჰა", "hectare", "hectares", "ჰექტარი"), "ha"),
    **dict.fromkeys(
        (
            *("l", "lt", "lts", "ltr", "ltrs", "lit", "ლ", "ლიტ", "ლიტრი"),
            *("litre", "litres", "liter", "liters"),
        ),
        "l",
    ),
    **dict.fromkeys(("t", "tn", "ton", "tons", "tonne", "tonnes", "ტ", "ტნ", "ტონ", "ტონა"), "t"),
    **dict.fromkeys(
        ("kg", "kgs", "კგ", "kilogram", "kilograms", "kilogramme", "kilogrammes", "კილოგრამი"),
        "kg",
    ),
    **dict.fromkeys(("month", "months", "mo", "mon", "mth", "თვე", "თვ"), "month"),
    **dict.fromkeys(("week", "weeks", "wk", "wks", "კვირა"), "week"),
    **dict.fromkeys(("day", "days", "დღე", "დღ"), "day"),
    **dict.fromkeys(("set", "sets", "kit", "kits", "კომპლ", "კომპლექტი", "კომპ"), "set"),
    **dict.fromkeys(
        (
            "pcs",
            "pc",
            "nr",
            "nrs",
            "no",
            "nos",
            "number",
            "numbers",
            "piece",
            "pieces",
            "ც",
            "ცალ",
            "ცალი",
            "\u0448\u0442",
        ),
        "pcs",
    ),
    **dict.fromkeys(("hr", "hrs", "h", "hour", "hours", "სთ", "საათი"), "hr"),
}


#: A currency, as a header names it: "GEL", "US $", "₾", "აშშ დოლარი", and the Georgian locative
#: "ლარში" / "დოლარში" ("in lari", "in dollars").
_CURRENCY = r"(?:აშშ\s+)?(?:დოლარ[ა-ჰ]*|ლარ[ა-ჰ]*|ევრო[ა-ჰ]*|usd|gel|eur|us\s*\$|\$|₾|€)"
#: A currency a header names after its label, however it is attached: "ერთეულის ფასი, აშშ
#: დოლარი", "Amount (USD)", "Rate in GEL", "Amount EUR", "ღირებულება ლარში". Only at the end: a
#: label that goes on after the currency ("Unit rates in USD (excl. VAT)", the group label over
#: the component rates) is not the rate itself.
_LABEL_CURRENCY = re.compile(
    rf"\s*(?:[,(]\s*|\s+in\s+|\s+){_CURRENCY}\s*\)?\s*$|^\s*(?:\$|₾|€)\s*", re.IGNORECASE
)


def _norm_label(v: object) -> str:
    """A header label as compared: "Qty." and "Unit:" are "qty" and "unit", and "ღირებულება, აშშ
    დოლარი", "Amount in GEL" and "ღირებულება ლარში" are the amount whatever currency they name.
    """
    label = normalize_spaces(str(v or "")).strip().lower().rstrip(":.").strip()
    return _LABEL_CURRENCY.sub("", label).strip()


#: Text a number reads from two ways: one separator and exactly three digits after it ("1.250",
#: "5,000") is a thousands group, or three decimals as an estimating program exports a quantity.
#: A spreadsheet computes with it by its own locale, so the text alone cannot tell which.
_TWO_WAY_NUMBER = re.compile(r"[+-]?[1-9]\d{0,2}[.,]\d{3}")


def _two_readings(v: object) -> tuple[float, float] | None:
    """The two numbers text such as "1.250" reads as - (1.25, 1250.0) - else None."""
    if not isinstance(v, str):
        return None
    text = normalize_spaces(v).strip()
    if not _TWO_WAY_NUMBER.fullmatch(text):
        return None
    decimal = text.replace(",", ".")
    return float(decimal), float(decimal.replace(".", ""))


#: Text that reads one way as a number (GOLD_SPEC 5): plain digits with at most one separator,
#: a decimal one ("12.5", "1234,5", "0.125"); or digits in groups of three under one grouping
#: character - a space, "," or "." - and at most one decimal separator, the other character,
#: at the end ("1,250.50", "1.250,50", "1 250"). Anything else reads no clear way and stays
#: text: a separator left trailing or doubled ("1450.00.", "7.5,"), groups that are not threes
#: ("14.50.0", "12,34,567"), two grouping characters mixed ("1,250.000.5"), and three decimals
#: after the groups ("1 250.000": one more group, or three decimals?).
_ONE_WAY_NUMBER = re.compile(
    r"(?P<sign>[+-]?)(?:(?P<int>\d+)(?:[.,](?P<frac>\d+))?|"
    r"(?P<groups>\d{1,3}(?P<g>[ ,.])\d{3}(?:(?P=g)\d{3})*)"
    r"(?:(?!(?P=g))[.,](?P<gfrac>\d{1,2}|\d{4,}))?)"
)


def _one_way_number(text: str) -> float | None:
    """The number text reads as when it reads one way (``_ONE_WAY_NUMBER``), else None.

    Read here rather than by ``parse_number``, which strips every separator it cannot place: a
    rate typed "1450.00." was read as 145,000 and evaluated so, and "1234.567" as 1,234,567."""
    m = _ONE_WAY_NUMBER.fullmatch(normalize_spaces(text).strip())
    if m is None:
        return None
    if m.group("int") is not None:
        integer, frac = m.group("int"), m.group("frac")
    else:
        integer, frac = m.group("groups").replace(m.group("g"), ""), m.group("gfrac")
    out = float(f"{integer}.{frac}" if frac else integer)
    return -out if m.group("sign") == "-" else out


def _num(v: object) -> float | None:
    if v is None or v == "" or isinstance(v, bool):  # TRUE in a quantity cell is not 1
        return None
    if isinstance(v, int | float):
        return float(v)
    if _two_readings(v) is not None:
        # "1.250" is 1.25 or 1,250: never guessed. Only the row it stands in can tell
        # (``_resolve_two_way``); read as thousands, 1.25 t of rebar priced as 1,250 t.
        return None
    return _one_way_number(str(v))


#: (product, factor, factor): the number cells of a line that compute one another
_ROW_PRODUCTS = (
    ("amount", "qty", "rate_total"),
    ("sum_materials", "qty", "rate_materials"),
    ("sum_labor", "qty", "rate_labor"),
    ("sum_delivery", "qty", "rate_delivery"),
)
_COMPONENT_RATES = ("rate_materials", "rate_labor", "rate_delivery")
_COMPONENT_SUMS = ("sum_materials", "sum_labor", "sum_delivery")


def _row_checks(v: dict[str, float | None]) -> list[tuple[set[str], float, float]]:
    """What a line's number cells compute from one another, as (the cells used, the number
    stated, the number computed): amount = quantity x rate, a component's sum = quantity x its
    rate, the rate = the component rates added, the amount = the component sums added."""
    out: list[tuple[set[str], float, float]] = []
    for prod, a, b in _ROW_PRODUCTS:
        p, x, y = v.get(prod), v.get(a), v.get(b)
        if p is not None and x is not None and y is not None:
            out.append(({prod, a, b}, p, x * y))
    parts = [k for k in _COMPONENT_RATES if v.get(k) is not None]
    if parts:
        rate = sum(v[k] or 0.0 for k in parts)
        stated, qty, amount = v.get("rate_total"), v.get("qty"), v.get("amount")
        if stated is not None:
            out.append(({"rate_total", *parts}, stated, rate))
        elif qty is not None and amount is not None:
            out.append(({"amount", "qty", *parts}, amount, qty * rate))
    sums = [k for k in _COMPONENT_SUMS if v.get(k) is not None]
    if sums and v.get("amount") is not None:
        out.append(({"amount", *sums}, v["amount"] or 0.0, sum(v[k] or 0.0 for k in sums)))
    return out


def _row_computes(v: dict[str, float | None], key: str) -> bool:
    """Whether some computation of the row that uses ``key`` holds, on numbers that are not all
    nought: an unpriced row (0 = 1.25 x nothing) tells nothing."""
    for keys, stated, computed in _row_checks(v):
        if key not in keys or (abs(stated) < 1e-9 and abs(computed) < 1e-9):
            continue
        if abs(stated - computed) <= max(_MONEY_TOL, 0.005 * max(abs(stated), abs(computed))):
            return True
    return False


def _resolve_two_way(v: dict[str, float | None], raw: dict[str, object]) -> set[str]:
    """Read each two-way text cell of a line ("1.250") the way the row itself computes it.

    The reading the row's cached computations agree with is taken - the sheet's own quantity x
    rate = amount says whether its quantity was 1.25 or 1,250. When neither or both agree (an
    unpriced row, nothing beside it to check), the cell stays text: never read as a number the
    workbook does not bear out. ``v`` is updated in place; returns the keys read."""
    pending = {k: r for k, text in raw.items() if (r := _two_readings(text)) is not None}
    done: set[str] = set()
    progress = True
    while pending and progress:
        progress = False
        for key, readings in list(pending.items()):
            fits = [x for x in readings if _row_computes({**v, key: x}, key)]
            if len(fits) == 1:
                v[key] = fits[0]
                done.add(key)
                del pending[key]
                progress = True
    return done


def _is_text_in_number_cell(v: object) -> bool:
    """Text where a number belongs: "incl.", "-", "N/A", "10 m3", a formula error, TRUE."""
    if v is None or (isinstance(v, int | float) and not isinstance(v, bool)):
        return False
    return bool(str(v).strip()) and _num(v) is None


def _same_quantity(a: float | None, b: float | None) -> bool:
    """Quantities compared with a tolerance: ``0.1 * 3`` and ``0.3`` are the same measurement.

    A blank and a zero are the same too - both mean "nothing quoted here".
    """
    return math.isclose(a or 0.0, b or 0.0, rel_tol=1e-9, abs_tol=1e-6)


def _same_money(a: float, b: float) -> bool:
    """Money compared to the cent, however large the sum. The tolerance used to grow with the
    amount (1e-6 of it): 2.50 off on a three-million sheet then passed as rounding. Float error
    at these magnitudes is about 1e-9, which the second term absorbs."""
    return abs(a - b) <= _MONEY_TOL + 1e-9 * max(abs(a), abs(b))


def _qty(x: float | None) -> str:
    """A quantity as a person writes it: "1,234,567" and "12.75", never "1.23457e+06"."""
    if x is None:
        return "—"
    text = f"{x:,.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _money(x: float) -> str:
    return f"{x:,.2f}"


def norm_unit(unit: str | None) -> str:
    """A unit folded for comparison: case, spaces and dots out, Cyrillic look-alikes read as the
    Latin letters they print as, then known aliases merged."""
    text = unicodedata.normalize("NFC", str(unit or "")).lower()
    text = re.sub(r"[\s.]", "", text)
    if text not in _UNIT_ALIASES:
        text = text.translate(_HOMOGLYPHS)
    return _UNIT_ALIASES.get(text, text)


def _fold_desc(text: str) -> str:
    """A description folded for comparison (GOLD_SPEC section 1, plus case and end punctuation)."""
    text = unicodedata.normalize("NFC", text)
    text = re.sub("[\u201e\u201c\u201d\"'\u2018\u2019\u00ab\u00bb\u201a]", "", text)
    text = re.sub("[\u2013\u2014\u2011\u2212]", "-", text)
    text = normalize_spaces(text).strip().lower()
    return text.rstrip(" .;:,")


@dataclass(frozen=True, slots=True)
class BoqLine:
    sheet: str
    row: int
    item_no: str
    description: str
    unit: str | None
    quantity: float | None
    rate_materials: float | None = None
    rate_labor: float | None = None
    rate_delivery: float | None = None
    rate_total: float | None = None
    amount: float | None = None
    sum_materials: float | None = None
    sum_labor: float | None = None
    sum_delivery: float | None = None
    #: columns whose value was typed in rather than computed by a formula
    hardcoded: tuple[str, ...] = ()
    #: the computed columns (``_TOTAL_KEYS``) whose cell holds a formula
    formulas: tuple[str, ...] = ()
    #: "position" (a priced leaf), "subtotal" (sums other rows) or "heading" (no amount at all),
    #: decided by this workbook's own amount cell - see ``_line_kind``
    kind: str = "position"
    #: (column key, the text) for every number column that holds text instead of a number
    non_numeric: tuple[tuple[str, str], ...] = ()

    @property
    def key(self) -> str:
        """Identity of a position: its number and description.

        Deliberately excludes unit and quantity. A bidder filling in a blank unit is a unit
        change, reported as such - not a deleted line plus an unrelated new one.
        """
        desc = re.sub(r"\s+", " ", self.description).strip().lower()[:70]
        return f"{self.item_no.strip().lower()}|{desc}"

    @property
    def is_priced(self) -> bool:
        return bool(self.amount) or bool(self.rate_total)


@dataclass(slots=True)
class BoqSheet:
    name: str
    columns: dict[str, int]
    header_row: int = 0
    lines: list[BoqLine] = field(default_factory=list)
    #: The amount on the sheet's own closing SUB-TOTAL row, and that row, when there is one.
    #: The positions read must add up to it: that is how a reader that silently skipped rows
    #: is caught (``reconcile``) instead of shipping a total that looks right.
    subtotal: float | None = None
    subtotal_row: int | None = None

    @property
    def leaf_lines(self) -> list[BoqLine]:
        """Priced positions, excluding the section subtotal rows that sum them and headings."""
        return [line for line in self.lines if line.kind == "position"]

    @property
    def total(self) -> float:
        """Recomputed from the positions, never read off a subtotal the bidder wrote."""
        return sum(line.amount or 0.0 for line in self.leaf_lines)


@dataclass(frozen=True, slots=True)
class SummaryRow:
    """One labelled row of the Summary sheet, read as a row - never keyed by its label.

    The client's Summary repeats the same group label under every section; keyed by label, a
    third of its rows collapsed into others and the merged Summary showed one section's value
    under another.
    """

    row: int
    item: str
    label: str
    #: the TOTAL column
    value: float | None
    #: the number right of the TOTAL column where it is a fraction (overheads %, VAT rate)
    rate: float | None = None
    #: (sheet, row) pairs this row's formulas link to, e.g. ("2. CIVIL WORKS", 30)
    refs: tuple[tuple[str, int], ...] = ()
    #: the TOTAL cell holds text instead of a number
    non_numeric: str | None = None

    @property
    def key(self) -> str:
        return f"{self.item.strip().lower()}|{_fold_desc(self.label)}"


@dataclass(slots=True)
class BoqWorkbook:
    name: str
    sheets: dict[str, BoqSheet] = field(default_factory=dict)
    #: Summary values by label, kept for callers that read it as a mapping. A label repeated
    #: under several sections is made unique with " (row N)"; ``summary_rows`` is the Summary.
    summary: dict[str, float | None] = field(default_factory=dict)
    sheet_order: list[str] = field(default_factory=list)
    summary_rows: list[SummaryRow] = field(default_factory=list)
    summary_sheet: str | None = None
    #: sheets that look priced (the Summary links to them, a SUB-TOTAL row, half a BOQ header)
    #: but whose columns could not be recognised - never skipped silently
    unreadable_sheets: list[str] = field(default_factory=list)
    #: sheets with no BOQ columns that nothing in the Summary refers to (a calculation aid)
    helper_sheets: list[str] = field(default_factory=list)
    #: formula cells read, and how many of them carried no cached value
    formula_cells: int = 0
    uncached_cells: int = 0
    #: why the file could not be read at all (``read_bid``); such a bid is reported, not compared
    read_error: str | None = None

    @property
    def total(self) -> float:
        return sum(s.total for s in self.sheets.values())

    @property
    def no_cached_values(self) -> bool:
        """Saved without calculated values: every formula reads as None, i.e. as nothing."""
        return self.formula_cells > 0 and self.uncached_cells > self.formula_cells * _UNCACHED_SHARE


#: A unit column headed the long or the short way: "Unit of measurement", "U.o.M.", "Measure",
#: "განზ. ერთ.", "ზომის ერთ.", "ზ/ე", "ერთ.". Tried only after the exact labels, and never on a
#: label that names money ("ერთ. ფასი" is the rate).
_UNIT_LABEL = re.compile(
    r"^(?:units?\s+of\s+meas|u\s?\.?\s?o\s?\.?\s?m$|measure(?:ment)?$|"
    r"განზ|ზომის\s+ერთ|ზ\s?/\s?ე$|ერთ$)"
)
_MONEY_WORD = re.compile(r"ფას|ღირ|თანხ|rate|price|cost|amount|sum|total")


def _label_key(value: object) -> str | None:
    label = _norm_label(value)
    key = _LABEL_TO_KEY.get(label)
    if key is None and _UNIT_LABEL.match(label) and not _MONEY_WORD.search(label):
        return "unit"
    return key


def _row_values(ws: object, row: int) -> list[object]:
    return [
        ws.cell(row, c).value  # type: ignore[attr-defined]
        for c in range(1, _HEADER_SCAN_COLS + 1)
    ]


def _no_numbers_no_item(values: list[object]) -> bool:
    """Text only, and no item number in the first columns: what a header row looks like."""
    present = [v for v in values if v not in (None, "")]
    if not present or any(isinstance(v, int | float) for v in present):
        return False
    return not any(_ITEM_NO.match(str(v).strip()) for v in values[:3] if v not in (None, ""))


def _is_label_row(ws: object, row: int) -> bool:
    """Whether ``row``, under a header row already found, is more of the header.

    Mostly known labels ("Materials", "Labour Sum"), no numbers and no item number: a header
    row. Anything else is the sheet's content. The unit "item" is itself a header label (the
    Summary's "Item" column), so a data row whose unit reads "item" used to count as header too:
    on every sheet whose first positions are counted in items, the "header" then ran on to row
    14 and those positions were never read - an entire sheet of doors, gone from the quantities,
    the totals and the merged workbook without a word.
    """
    values = _row_values(ws, row)
    if not _no_numbers_no_item(values):
        return False
    present = [v for v in values if v not in (None, "")]
    labels = sum(1 for v in present if _label_key(v))
    return labels > 0 and 2 * labels >= len(present)


#: Header labels that may group sub-columns under them ("Unit Rate" over Materials | Labour |
#: Delivery | Total), and the key each kind of sub-label takes under them.
_GROUP_KEYS: dict[str, dict[str, str]] = {
    "rate_total": {
        "materials": "rate_materials",
        "labor": "rate_labor",
        "delivery": "rate_delivery",
        "total": "rate_total",
    },
    "amount": {
        "materials": "sum_materials",
        "labor": "sum_labor",
        "delivery": "sum_delivery",
        "total": "amount",
    },
    "qty": {"total": "qty"},
}
#: What a sub-label is, whichever group it stands under.
_FAMILY = {
    "rate_materials": "materials",
    "sum_materials": "materials",
    "rate_labor": "labor",
    "sum_labor": "labor",
    "rate_delivery": "delivery",
    "sum_delivery": "delivery",
    "rate_total": "total",
    "amount": "total",
}


def _span(ws: object, row: int, col: int) -> range:
    """The columns a header label covers: its merged range, else itself and the blank cells to
    its right."""
    for merged in getattr(ws, "merged_cells", None) or ():
        if merged.min_row <= row <= merged.max_row and merged.min_col <= col <= merged.max_col:
            return range(merged.min_col, merged.max_col + 1)
    end = col
    while end < _HEADER_SCAN_COLS and ws.cell(row, end + 1).value in (None, ""):  # type: ignore[attr-defined]
        end += 1
    return range(col, end + 1)


def _group(ws: object, row: int, col: int, key: str, below: int) -> dict[int, str] | None:
    """The sub-columns a group label heads in the header row ``below``, as {column: key}.

    A rate, amount or quantity label is a group when the row under it carries a label under the
    label's own column and at least one more within its span. The group binds its key to the
    sub-column labelled Total ("სულ") and the components to the materials/labour/delivery keys
    of its kind - never the group key to the first sub-column, which is what read "Unit Rate"
    over Materials | Labour | Delivery | Total as the materials rate. With no Total beneath it
    the key stays unbound (a rate is then built from its components). None: not a group.
    """
    if key not in _GROUP_KEYS and key not in _COMPONENT_GROUPS:
        return None
    if ws.cell(below, col).value in (None, ""):  # type: ignore[attr-defined]
        return None
    subs = [
        (c, ws.cell(below, c).value)  # type: ignore[attr-defined]
        for c in _span(ws, row, col)
        if ws.cell(below, c).value not in (None, "")  # type: ignore[attr-defined]
    ]
    if len(subs) < 2:
        return None
    bound = {c: bind for c, v in subs if (sub := _label_key(v)) and (bind := _sub_key(key, sub))}
    if key != "qty" and not bound:
        return None  # "Amount" over "USD | GEL": not a breakdown this reader knows
    # every sub-column belongs to the group, bound or not ("Block A | Block B" under Quantity)
    return {c: bound.get(c, "") for c, _ in subs}


#: A component that heads its own sub-columns ("Materials" over Rate | Amount, "მასალა" over
#: "ერთ. ფასი | სულ" in a Georgian estimate): the component is the group, and its rate and amount
#: are the component's rate and sum - never the unit rate and the line amount.
_COMPONENT_GROUPS = {
    "rate_materials": "materials",
    "rate_labor": "labor",
    "rate_delivery": "delivery",
}


def _sub_key(group: str, sub: str) -> str | None:
    """The key a sub-label read as ``sub`` takes under a group label read as ``group``."""
    if group in _COMPONENT_GROUPS:
        part = _COMPONENT_GROUPS[group]
        return {"rate_total": f"rate_{part}", "amount": f"sum_{part}"}.get(sub)
    return _GROUP_KEYS[group].get(_FAMILY.get(sub, ""))


def _continues_header(ws: object, row: int, above: int, first: dict[str, int]) -> bool:
    """Whether ``row`` is more of the header block whose last row is ``above`` (see
    ``_map_headers``)."""
    values = _row_values(ws, row)
    if not _no_numbers_no_item(values):
        return False
    content_cols = {first[k] for k in ("desc", "no") if k in first}
    if any(values[c - 1] not in (None, "") for c in content_cols):
        return False
    if _is_label_row(ws, row):
        return True
    spans: set[int] = set()
    for c in range(1, _HEADER_SCAN_COLS + 1):
        key = _label_key(ws.cell(above, c).value)  # type: ignore[attr-defined]
        if key and (subs := _group(ws, above, c, key, row)) is not None:
            spans.update(subs)
    present = {c for c, v in enumerate(values, 1) if v not in (None, "")}
    return bool(spans) and present <= spans


def _map_headers(ws: object) -> tuple[dict[str, int], int]:
    """Locate columns by label in the header block, and where that block ends.

    The template splits its header over three rows - group label, sub-label, and a row of bare
    column numbers - so a single-row scan finds "Quantity" but not "Materials Sum". The block
    starts at the first row naming at least two columns, one of them the description or the
    quantity, and runs on only through rows that are themselves headers, then past a "1 2 3 ..."
    column-number row. That row reads as a perfectly good line item, and then 5 x 7 != 8 makes
    every sheet look like it cannot add up.

    A row further down is header only while it leaves the description and item-number columns
    empty: "Materials" alone in the Description column, straight under a one-row header, is the
    first heading of the bill - read as a label, it bound the materials rate to the
    descriptions. A row of sub-labels under a group ("Block A | Block B | Total" under
    "Quantity") is header, known labels or not; see ``_group`` for what the group binds.
    """
    block: list[int] = []
    first: dict[str, int] = {}
    for r in range(1, _HEADER_SCAN_ROWS + 1):
        if block:
            if not _continues_header(ws, r, block[-1], first):
                break
            block.append(r)
            continue
        found = [
            (c, key)
            for c in range(1, _HEADER_SCAN_COLS + 1)
            if (key := _label_key(ws.cell(r, c).value))  # type: ignore[attr-defined]
        ]
        keys = {key for _, key in found}
        if len(keys) >= 2 and keys & {"desc", "qty"}:
            block.append(r)
            for c, key in found:
                first.setdefault(key, c)
    if not block:
        return {}, 0

    cols: dict[str, int] = {}
    grouped: dict[str, int] = {}
    consumed: set[tuple[int, int]] = set()
    for i, r in enumerate(block):
        below = block[i + 1] if i + 1 < len(block) else None
        for c in range(1, _HEADER_SCAN_COLS + 1):
            key = _label_key(ws.cell(r, c).value)  # type: ignore[attr-defined]
            if not key or (r, c) in consumed:
                continue
            subs = _group(ws, r, c, key, below) if below is not None else None
            if subs is None:
                cols.setdefault(key, c)
                continue
            for sub_col, sub_key in subs.items():
                consumed.add((below, sub_col))  # type: ignore[arg-type]
                if sub_key:
                    grouped.setdefault(sub_key, sub_col)
    # A column holds one number: what a group binds wins, and a label that would bind a column
    # already bound is dropped. Binding "Rate" under "Materials" as the unit rate once gave one
    # column two keys, and the materials rate priced the whole line.
    out = dict(grouped)
    taken = set(grouped.values())
    for key, c in cols.items():
        if key not in out and c not in taken:
            out[key] = c
            taken.add(c)
    return out, max(block[-1], _column_number_row(ws, block[-1]))


def _column_number_row(ws: object, after: int) -> int:
    """The '1 2 3 4 ...' row some templates put under the header, if present."""
    for r in range(after, min(after + 3, _HEADER_SCAN_ROWS) + 1):
        values = [
            ws.cell(r, c).value  # type: ignore[attr-defined]
            for c in range(1, _HEADER_SCAN_COLS)
        ]
        present = [v for v in values if v not in (None, "")]
        if len(present) >= 4 and all(str(v).strip().isdigit() for v in present):
            return r
    return after


def _looks_priced(ws: object) -> bool:
    """A sheet whose columns were not recognised, but which is a BOQ sheet all the same.

    Half a header (two known labels in one row, one of them a BOQ column) or a SUB-TOTAL row
    is enough: a bidder who renamed "Quantity" to "Qnty" has not turned the sheet into a note.
    """
    for r in range(1, _HEADER_SCAN_ROWS + 1):
        keys = {
            key
            for c in range(1, _HEADER_SCAN_COLS + 1)
            if (key := _label_key(ws.cell(r, c).value))  # type: ignore[attr-defined]
        }
        if len(keys) >= 2 and keys & {"desc", "qty", "unit", "amount", "rate_total"}:
            return True
    max_row = min(ws.max_row, 2000)  # type: ignore[attr-defined]
    for r in range(1, max_row + 1):
        for c in range(1, 7):
            v = ws.cell(r, c).value  # type: ignore[attr-defined]
            if isinstance(v, str) and _SUBTOTAL.match(v):
                return True
    return False


def _item_columns(cols: dict[str, int]) -> tuple[int, ...]:
    """Where a line's item number may be, most specific first.

    The client's template heads a two-column block "No": section codes ("A", "A1") in the first
    column and the item numbers ("1.1", "1.2") in the second, so the second is read first. Every
    unmapped column between "No" and the description is a candidate, right to left: a bidder who
    inserts a column inside the block (blank, or holding their own references "R-02") moves the
    item numbers one column right, and a fixed offset then read every position as a new one.
    ``_item_no`` prefers the cell that reads like an item number. A mapped column is never a
    candidate - on a sheet whose description sits right beside "No", the description would
    otherwise become the item number.
    """
    no_col = cols.get("no", 1)
    desc_col = cols.get("desc", 0)
    mapped = set(cols.values())
    if desc_col > no_col + 1:
        between = [c for c in range(desc_col - 1, no_col, -1) if c not in mapped]
        return (*between, no_col)
    if no_col + 1 in mapped:
        return (no_col,)
    return (no_col + 1, no_col)


def _item_no(ws: object, row: int, item_cols: tuple[int, ...]) -> str:
    """The item number of ``row``: the first candidate cell shaped like one ("1.1", "A1"), else
    the first non-empty candidate."""
    values = [
        str(v).strip()
        for c in item_cols
        if (v := ws.cell(row, c).value) not in (None, "")  # type: ignore[attr-defined]
    ]
    values = [v for v in values if v]
    return next((v for v in values if _ITEM_NO.match(v)), values[0] if values else "")


def _formula_text(cell: object) -> str | None:
    """The formula of a formula cell, as text (an array formula carries it in ``.text``)."""
    if getattr(cell, "data_type", None) != "f":
        return None
    value = cell.value  # type: ignore[attr-defined]
    text = getattr(value, "text", value)
    return str(text or "")


#: A link to another sheet, with its range if it has one: ``'2. CIVIL'!J30``, ``Calc!B5:B9``.
_OTHER_SHEET_REF = re.compile(
    r"(?:'(?:[^']|'')+'|[^\W\d][\w.]*)!\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?"
)


def _formula_rows(formula: str) -> list[int]:
    """The rows of this sheet a formula refers to (range ends included), links to other sheets
    and quoted text left out."""
    body = _OTHER_SHEET_REF.sub("", re.sub(r'"[^"]*"', "", formula))
    return [int(m.group(2)) for m in _CELL_REF.finditer(body)]


def _own_row_formula(formula: str, row: int) -> bool:
    """``=O12*G12`` on row 12: a product of the row's own cells, i.e. a position's amount.

    It must refer to a cell, and only to cells of its own row. A link to another sheet or a
    reference to any other row makes it a subtotal, and so does a formula over a defined name
    (``=SUM(Block1_Amounts)``): with no cell reference at all it used to pass as "nothing but its
    own row", and a group row summing its block through a name was counted as a position on top
    of the positions it sums.
    """
    if "!" in formula:
        return False
    rows = _formula_rows(formula)
    return bool(rows) and set(rows) == {row}


#: A name in a formula that is not a function (followed by "("), a cell, a sheet (followed by
#: "!") or a range end (after ":"): a defined name, ``=SUM(Block1_Qty)``.
_NAME_IN_FORMULA = re.compile(r"(?<![\w.'!$:\\])([A-Za-z_\\][\w.]*)(?![\w.!(])(?!\s*\()")
_CELL_NAME = re.compile(r"^\$?[A-Za-z]{1,3}\$?\d+$|^(?:true|false)$", re.IGNORECASE)


def _resolve_names(formula: str, wf: object) -> str | None:
    """The formula with every defined name replaced by the cells it names (the sheet's own names
    first, then the workbook's); None when a name cannot be followed."""
    sheet_names = getattr(wf, "defined_names", None) or {}
    book_names = getattr(getattr(wf, "parent", None), "defined_names", None) or {}
    unresolved = False

    def swap(m: re.Match[str]) -> str:
        nonlocal unresolved
        name = m.group(1)
        if _CELL_NAME.match(name):
            return name
        found = sheet_names.get(name) or book_names.get(name)
        target = getattr(found, "attr_text", None)
        if not target:
            unresolved = True
            return name
        return str(target)

    # a quoted sheet name ('Take-off') is no name: only the text between them is searched
    parts = re.split(r"('(?:[^']|'')*')", re.sub(r'"[^"]*"', "", formula))
    body = "".join(p if i % 2 else _NAME_IN_FORMULA.sub(swap, p) for i, p in enumerate(parts))
    return None if unresolved else body


def _unqualify_own_sheet(formula: str, sheet: str) -> str:
    """``'1. WORKS'!F8:F12`` on the sheet "1. WORKS" is ``F8:F12``: rows of this sheet."""
    body = formula.replace("'" + sheet.replace("'", "''") + "'!", "")
    if re.fullmatch(r"[^\W\d][\w.]*", sheet):
        body = re.sub(rf"(?<![\w.']){re.escape(sheet)}!", "", body)
    return body


def _sums_other_rows(formula: str, row: int, wf: object = None) -> bool:
    """A quantity that adds up other rows of the bill (``=SUM(G13:G19)``, ``=G13+G14``, a SUM
    over a name for such rows): the row is a parent. One that adds up its own row's cells
    (``=D7+E7``, the quantities of two buildings side by side) is the position's own quantity,
    and so is one added up from a take-off sheet (``=SUM('Take-off'!D2:D5)``) or from numbers
    (``=SUM(30,30,30)``): no row of the bill is added up. Those used to read as parents - "no row
    of this sheet" was taken for a SUM over a name - and the position fell out of the evaluation.
    A defined name is followed to the cells it names; one that cannot be is taken for the parent
    it most likely is."""
    if not _SUM_OF_ROWS.search(formula):
        return False
    body = _resolve_names(formula, wf)
    if body is None:
        return True
    title = getattr(wf, "title", None)
    rows = _formula_rows(_unqualify_own_sheet(body, title) if title else body)
    return bool(rows) and set(rows) != {row}


#: A quantity times a rate, "=O23*G23": a position's amount, whatever rows it points at. One
#: that points at another row ("=O23*G19") is the bidder's arithmetic gone wrong - reported by
#: BOQ.ARITHMETIC - never a subtotal, which adds rows up.
_PRODUCT_OF_CELLS = re.compile(r"=\s*\$?[A-Z]{1,3}\$?\d+\s*\*\s*\$?[A-Z]{1,3}\$?\d+\s*")
#: A formula shaped like a sum: "=SUM(G13:G19)", "=G13+G14". Whose rows it adds decides whether
#: the row is a parent (``_sums_other_rows``).
_SUM_OF_ROWS = re.compile(r"^=\s*(?:SUM\s*\(|\$?[A-Z]{1,3}\$?\d+\s*\+)", re.IGNORECASE)


def _line_kind(
    ws: object, wf: object, cols: dict[str, int], row: int, description: str, has_measure: bool
) -> str:
    """Position, subtotal or heading - from the row's own amount cell (GOLD_SPEC section 5).

    A row with no amount cell is a heading unless it carries a quantity or a unit: the client's
    template has a position with a quantity and no amount formula, which everyone priced.
    """
    if _SUBTOTAL.match(description):
        return "subtotal"
    qty_col = cols.get("qty")
    if qty_col:
        qty_formula = _formula_text(wf.cell(row, qty_col))  # type: ignore[attr-defined]
        if qty_formula is not None and _sums_other_rows(qty_formula, row, wf):
            # "=SUM(G13:G19)": the quantity adds up the items under it - a parent, whose items
            # are the positions. As a position it was always "unpriced", its items being priced.
            return "heading"
    column = cols.get("amount")
    if column:
        formula = _formula_text(wf.cell(row, column))  # type: ignore[attr-defined]
        if formula is not None:
            if _own_row_formula(formula, row) or _PRODUCT_OF_CELLS.fullmatch(formula):
                return "position"
            if not _formula_rows(formula) and not re.search(r"[A-Za-z_!]", formula[1:]):
                # "=1200*1.5": a number typed as a formula, read like a typed number
                return "position" if has_measure else "subtotal"
            return "subtotal"
        if ws.cell(row, column).value not in (None, ""):  # type: ignore[attr-defined]
            # a typed number: a leaf when it has a quantity or a unit, otherwise a section total
            return "position" if has_measure else "subtotal"
    return "position" if has_measure else "heading"


def read_boq(path: Path, *, name: str | None = None) -> BoqWorkbook:
    """Read a BOQ workbook twice: cached values, and formulas, so tampering is visible."""
    values = load_workbook(path, data_only=True)
    try:
        formulas = load_workbook(path, data_only=False)
        try:
            return _read_boq(values, formulas, name=name or path.stem)
        finally:
            formulas.close()
    finally:
        values.close()


def read_bid(path: Path, *, name: str) -> BoqWorkbook:
    """``read_boq`` for one bidder's upload: a workbook that cannot be read at all is that
    bidder's problem, reported on that bidder (``BOQ.UNREADABLE``), never the end of the whole
    comparison for every other bidder."""
    try:
        return read_boq(path, name=name)
    except Exception as exc:  # any failure to parse the upload - reported, not raised
        return BoqWorkbook(name=name, read_error=type(exc).__name__)


def _summary_sheet_name(names: list[str], formulas: Workbook | None = None) -> str | None:
    """The Summary sheet: named "SUMMARY" the way SKIP_SHEETS matches it (a bidder's "Summary" is
    still the Summary), else the sheet whose name says it is a summary ("BOQ SUMMARY").

    Several such names ("BOQ SUMMARY" beside a working tab "Summary of unit rates"): the one that
    carries the totals - a TOTAL COST or incl.-VAT row - and, among those, links to the most
    sheets. Returning none of them dropped the bidder's Summary, and with it their ranking.
    """
    exact = next((n for n in names if n.strip().upper() == _SUMMARY), None)
    if exact is not None:
        return exact
    named = [n for n in names if _SUMMARY_NAME.search(n)]
    if len(named) <= 1 or formulas is None:
        return named[0] if len(named) == 1 else None

    def score(name: str) -> tuple[bool, int]:
        ws = formulas[name]
        chain = any(
            isinstance(v, str)
            and not v.startswith("=")
            and (_TOTAL_COST.search(v) or _INCL_VAT.search(v))
            for row in ws.iter_rows(max_row=300, max_col=_HEADER_SCAN_COLS, values_only=True)
            for v in row
        )
        return chain, len(_referenced_sheets(ws))

    return max(named, key=score)


def _referenced_sheets(ws: object) -> set[str]:
    """Every sheet the Summary's formulas link to."""
    out: set[str] = set()
    for row in ws.iter_rows():  # type: ignore[attr-defined]
        for cell in row:
            formula = _formula_text(cell)
            if formula and "!" in formula:
                for m in _SHEET_REF.finditer(formula):
                    out.add((m.group(1) or m.group(2) or "").replace("''", "'"))
    return out


def _read_boq(values: Workbook, formulas: Workbook, *, name: str) -> BoqWorkbook:
    book = BoqWorkbook(name=name, sheet_order=list(values.sheetnames))
    # a chart sheet ("Move Chart > New sheet") has no cells: listed, never read as a table
    worksheets = [ws.title for ws in values.worksheets]
    summary_sheet = _summary_sheet_name(worksheets, formulas)
    linked: set[str] = set()
    if summary_sheet is not None:
        linked = _referenced_sheets(formulas[summary_sheet])
        book.summary_sheet = summary_sheet
        book.summary_rows = _read_summary_rows(book, values[summary_sheet], formulas[summary_sheet])
        book.summary = _summary_mapping(book.summary_rows)
    linked_rows: dict[str, set[int]] = {}
    for row in book.summary_rows:
        for sheet_name, sheet_row in row.refs:
            linked_rows.setdefault(sheet_name, set()).add(sheet_row)

    for sheet_name in values.sheetnames:
        if sheet_name.strip().upper() in SKIP_SHEETS or sheet_name == summary_sheet:
            continue
        if sheet_name not in worksheets:
            book.helper_sheets.append(sheet_name)
            continue
        ws, wf = values[sheet_name], formulas[sheet_name]
        cols, header_row = _map_headers(ws)
        if "desc" not in cols or "qty" not in cols:
            if sheet_name in linked or _looks_priced(ws):
                book.unreadable_sheets.append(sheet_name)
            else:
                book.helper_sheets.append(sheet_name)
            continue
        sheet = _read_sheet(book, ws, wf, sheet_name, cols, header_row)
        closing = _closing_line(sheet, linked_rows.get(sheet_name, set()))
        if closing is not None:
            sheet.subtotal, sheet.subtotal_row = closing.amount, closing.row
        book.sheets[sheet_name] = sheet
    return book


def _is_number_row(ws: object, row: int) -> bool:
    """A "1 2 3 4 ..." column-number row (the one under the header, or a copy of it below)."""
    present = [v for v in _row_values(ws, row) if v not in (None, "")]
    numbers: list[int] = []
    for v in present:
        text = str(int(v)) if isinstance(v, float) and v.is_integer() else str(v).strip()
        if isinstance(v, bool) or not text.isdigit():
            return False
        numbers.append(int(text))
    return len(numbers) >= 4 and all(b > a for a, b in itertools.pairwise(numbers))


def _repeated_header(ws: object, row: int, cols: dict[str, int]) -> bool:
    """The header block printed again further down (a print layout repeats it on every page):
    "Description" in the description column, another column label beside it, no number."""
    if _label_key(ws.cell(row, cols["desc"]).value) != "desc":  # type: ignore[attr-defined]
        return False
    cells = [ws.cell(row, c).value for k, c in cols.items() if k != "desc"]  # type: ignore[attr-defined]
    if any(isinstance(v, int | float) and not isinstance(v, bool) for v in cells):
        return False
    return any(_label_key(v) for v in cells if v not in (None, ""))


def _numbered_line(
    ws: object, wf: object, row: int, cols: dict[str, int], item_cols: tuple[int, ...]
) -> bool:
    """A row with an item number and a quantity, a unit or an amount of its own: a line of the
    bill, whatever its description reads (a tile size "600x300" has no letter)."""
    if not _ITEM_NO.match(_item_no(ws, row, item_cols)):
        return False
    qty = ws.cell(row, cols["qty"]).value  # type: ignore[attr-defined]
    if isinstance(qty, int | float) and not isinstance(qty, bool):
        return True
    if cols.get("unit") and ws.cell(row, cols["unit"]).value not in (None, ""):  # type: ignore[attr-defined]
        return True
    amount = _formula_text(wf.cell(row, cols["amount"])) if cols.get("amount") else None  # type: ignore[attr-defined]
    return amount is not None and _own_row_formula(amount, row)


def _read_sheet(
    book: BoqWorkbook, ws: object, wf: object, sheet_name: str, cols: dict[str, int], header: int
) -> BoqSheet:
    sheet = BoqSheet(name=sheet_name, columns=cols, header_row=header)
    item_cols = _item_columns(cols)

    for r in range(header + 1, ws.max_row + 1):  # type: ignore[attr-defined]
        desc = ws.cell(r, cols["desc"]).value  # type: ignore[attr-defined]
        description = str(desc).strip() if desc is not None else ""
        if not description or _is_number_row(ws, r) or _repeated_header(ws, r, cols):
            continue
        if not _has_letter(description) and (
            # a bare number is a column number, never a description
            parse_number(description) is not None or not _numbered_line(ws, wf, r, cols, item_cols)
        ):
            continue

        raws = {key: ws.cell(r, c).value for key in _NUMERIC_KEYS if (c := cols.get(key))}  # type: ignore[attr-defined]
        values: dict[str, float | None] = {key: _num(raw) for key, raw in raws.items()}
        read_two_way = _resolve_two_way(values, raws)

        def val(key: str, _values: dict[str, float | None] = values) -> float | None:
            return _values.get(key)

        hardcoded: list[str] = []
        formula_keys: list[str] = []
        non_numeric: list[tuple[str, str]] = []
        for key, raw in raws.items():
            is_formula = wf.cell(r, cols[key]).data_type == "f"  # type: ignore[attr-defined]
            if is_formula:
                book.formula_cells += 1
                formula_keys.append(key)
                if raw is None:
                    book.uncached_cells += 1
            if key not in read_two_way and _is_text_in_number_cell(raw):
                non_numeric.append((key, normalize_spaces(str(raw)).strip()[:40]))
            # the cell's type, not its text: a *string* cell reading "=5" is typed-in text
            if key in _TOTAL_KEYS and val(key) not in (None, 0) and not is_formula:
                hardcoded.append(key)

        unit_raw = ws.cell(r, cols["unit"]).value if cols.get("unit") else None  # type: ignore[attr-defined]
        unit = str(unit_raw).strip() if unit_raw not in (None, "") else None
        unit = unit or None
        qty_raw = ws.cell(r, cols["qty"]).value  # type: ignore[attr-defined]
        has_measure = qty_raw not in (None, "") or unit is not None
        sheet.lines.append(
            BoqLine(
                sheet=sheet_name,
                row=r,
                item_no=_item_no(ws, r, item_cols),
                description=description,
                unit=unit,
                quantity=val("qty"),
                rate_materials=val("rate_materials"),
                rate_labor=val("rate_labor"),
                rate_delivery=val("rate_delivery"),
                rate_total=val("rate_total"),
                amount=val("amount"),
                sum_materials=val("sum_materials"),
                sum_labor=val("sum_labor"),
                sum_delivery=val("sum_delivery"),
                hardcoded=tuple(hardcoded),
                formulas=tuple(k for k in formula_keys if k in _TOTAL_KEYS),
                kind=_line_kind(ws, wf, cols, r, description, has_measure),
                non_numeric=tuple(non_numeric),
            )
        )
    return sheet


def _closing_line(sheet: BoqSheet, linked_rows: set[int]) -> BoqLine | None:
    """The row that closes the sheet, which the positions read must add up to.

    Its SUB-TOTAL row where it has one. Otherwise the closing total by its role: a sum row with
    no quantity after the last position - the one the Summary links to, else the last one
    labelled as a total ("TOTAL CARRIED TO SUMMARY", "სულ ჯამი"). Read by the SUB-TOTAL label
    alone, a sheet closed any other way was never reconciled, and a SUM range one row short
    passed without a word. A VAT row is never the closing total: the positions are without VAT.

    Several SUB-TOTAL rows are the sections' own ("Sub-total Section 1", "ქვეჯამი II თავი"),
    and the sheet is closed by role as above: the last section's sub-total held against every
    position of the sheet was a false gap on each faithful bid. Their grand total is the row the
    Summary links to, else the last total after them; with none, the last sub-total still is.
    """
    subtotals = [
        ln for ln in sheet.lines if _SUBTOTAL.match(ln.description) and ln.quantity is None
    ]
    if len(subtotals) == 1:
        return subtotals[0]
    last = max((ln.row for ln in sheet.lines if ln.kind == "position"), default=0)
    after = [
        ln
        for ln in sheet.lines
        if ln.row > last
        and ln.kind == "subtotal"
        and ln.quantity is None
        and not _VAT.search(ln.description)
        and not _INCL_VAT.search(ln.description)
    ]
    linked = [ln for ln in after if ln.row in linked_rows]
    if linked:
        return linked[-1]
    labelled = [ln for ln in after if _CLOSING.match(ln.description)]
    if labelled:
        return labelled[-1]
    return subtotals[-1] if subtotals else None


def _summary_columns(ws: object) -> tuple[int | None, int | None, int | None, int]:
    """(item, description, total) columns of the Summary, and the last header row.

    Every column is located by its header label. One of the client's bidders returned a Summary
    shifted one column to the right, which a reader that assumed columns A and B silently read
    as all zeroes - a wrong answer that looks like a correct one.
    """
    cells = [
        (cell.row, cell.column, _norm_label(cell.value))
        for row in ws.iter_rows(min_row=1, max_row=_HEADER_SCAN_ROWS, max_col=_HEADER_SCAN_COLS)  # type: ignore[attr-defined]
        for cell in row
        if cell.value not in (None, "")
    ]
    item = next(((r, c) for r, c, label in cells if label in _SUMMARY_ITEM_LABELS), None)
    desc = next(((r, c) for r, c, label in cells if label in _SUMMARY_DESC_LABELS), None)
    # "TOTAL" itself wins: the client's Summary puts it under a group header "AMOUNT EXCL.
    # VAT" that spans the materials, labour and transport columns. Only without one does a label
    # that starts with a total's word name the column - and only in the header's own rows, not
    # in a row below it that says "TOTAL COST".
    total = next(((r, c) for r, c, label in cells if label == "total"), None)
    if total is None and desc is not None:
        total = next(
            (
                (r, c)
                for r, c, label in cells
                if desc[0] <= r <= desc[0] + 1 and label.startswith(_SUMMARY_TOTAL_LABELS)
            ),
            None,
        )
    header_row = max((pos[0] for pos in (item, desc, total) if pos is not None), default=0)
    return (
        item[1] if item else None,
        desc[1] if desc else None,
        total[1] if total else None,
        header_row,
    )


#: The Summary's own column headers: "Item | Description | TOTAL", or "No | Section | Amount,
#: USD excl. VAT" - a label that starts with the word is the column.
_SUMMARY_TOTAL_LABELS = ("total", "amount", "ღირებულება", "ჯამი", "სულ", "თანხა")
_SUMMARY_ITEM_LABELS = frozenset({"item", "no", "№", "#", "n"})
_SUMMARY_DESC_LABELS = frozenset(
    {"description", "section", "sections", "name", "აღწერა", "დასახელება", "განყოფილება"}
)


def _read_summary_rows(book: BoqWorkbook, ws: object, wf: object) -> list[SummaryRow]:
    """Every labelled Summary row, in order, as the bidder states it.

    These are read, never summed: the Summary nests sections inside groups, so adding the
    column up double-counts. They are what the bidder claims; ``reconcile_summary`` holds them
    against the sheets and against each other.
    """
    item_col, desc_col, total_col, header_row = _summary_columns(ws)
    out: list[SummaryRow] = []
    if total_col is None or desc_col is None:
        return out
    max_col = min(ws.max_column, _HEADER_SCAN_COLS)  # type: ignore[attr-defined]
    for r in range(header_row + 1, ws.max_row + 1):  # type: ignore[attr-defined]
        label = str(ws.cell(r, desc_col).value or "").strip()  # type: ignore[attr-defined]
        if not label or not _has_letter(label):
            continue
        raw = ws.cell(r, total_col).value  # type: ignore[attr-defined]
        if wf.cell(r, total_col).data_type == "f":  # type: ignore[attr-defined]
            book.formula_cells += 1
            if raw is None:
                book.uncached_cells += 1
        rate = _num(ws.cell(r, total_col + 1).value)  # type: ignore[attr-defined]
        refs: list[tuple[str, int]] = []
        for c in range(1, max_col + 1):
            formula = _formula_text(wf.cell(r, c))  # type: ignore[attr-defined]
            if formula and "!" in formula:
                for m in _SHEET_REF.finditer(formula):
                    ref = ((m.group(1) or m.group(2) or "").replace("''", "'"), int(m.group(4)))
                    if ref not in refs:
                        refs.append(ref)
        item = ws.cell(r, item_col).value if item_col else None  # type: ignore[attr-defined]
        out.append(
            SummaryRow(
                row=r,
                item=str(item).strip() if item not in (None, "") else "",
                label=normalize_spaces(label).strip(),
                value=_num(raw),
                rate=rate if rate is not None and 0 < rate < 1 else None,
                refs=tuple(refs),
                non_numeric=str(raw).strip()[:40] if _is_text_in_number_cell(raw) else None,
            )
        )
    return out


def _summary_mapping(rows: list[SummaryRow]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for row in rows:
        if row.value is None:
            continue
        key = f"{row.item} {row.label}" if row.item else row.label
        if key in out:
            key = f"{key} (row {row.row})"
        out[key] = row.value
    return out


#: The Summary chain in its order, and what a row playing each part is labelled.
_CHAIN = ("cost", "overheads", "profit", "excl", "vat", "incl")


def _plays(role: str, label: str) -> bool:
    """Whether a Summary row labelled ``label`` may play ``role`` in the chain. A TOTAL COST
    row that says it is excl. VAT ("Total cost, excl. VAT") is the TOTAL COST - the cost is
    before VAT - and ``summary_roles`` lets it play one part only."""
    incl, excl = _INCL_VAT.search(label), _EXCL_VAT.search(label)
    if role == "cost":
        return bool(_TOTAL_COST.search(label)) and not incl
    if role == "overheads":
        return bool(_OVERHEADS.search(label))
    if role == "profit":
        return bool(_PROFIT.search(label))
    if role == "excl":
        return bool(excl)
    if role == "vat":
        return bool(_VAT.search(label)) and not incl and not excl
    return bool(incl)


def summary_roles(book: BoqWorkbook) -> dict[str, SummaryRow]:
    """Which Summary row plays which part of the cost -> overheads -> profit -> excl. VAT -> VAT
    -> incl. VAT chain. Every part is played by one row and every row plays one part at most.

    TOTAL COST is the last row labelled so before the chain goes on (a Summary may state a
    TOTAL COST per building before the overall one). The other parts are taken below it, top
    down, by the first row labelled for a part still open - the earliest open part when a label
    names several. Taken from the last row labelled so, as it used to be, a closing "TOTAL COST
    INCL. OVERHEADS & PROFIT" became the cost, the overheads and the profit at once, and an
    "Advance payment 20% of the price incl. VAT" under the grand total became the grand total:
    the markup, the ranking and the colours all followed from the wrong row.
    """
    rows = book.summary_rows
    roles: dict[str, SummaryRow] = {}
    costs = [r for r in rows if _plays("cost", r.label)]
    start = 0
    if costs:
        after = next(
            (
                r
                for r in rows
                if r.row > costs[0].row
                and not _plays("cost", r.label)
                and any(_plays(role, r.label) for role in _CHAIN[1:])
            ),
            None,
        )
        cost = [c for c in costs if after is None or c.row < after.row][-1]
        roles["cost"] = cost
        start = rows.index(cost) + 1
    for row in rows[start:]:
        role = next((x for x in _CHAIN[1:] if x not in roles and _plays(x, row.label)), None)
        if role is not None:
            roles[role] = row
    return roles


def _summary_role(book: BoqWorkbook, role: str) -> SummaryRow | None:
    """The Summary row playing ``role`` in the chain (``summary_roles``)."""
    return summary_roles(book).get(role)


def grand_total(book: BoqWorkbook, *, incl_vat: bool = True) -> float | None:
    """The bidder's own bottom line, as their Summary states it: the incl.-VAT row, by label.

    Never a fallback to another row: a Summary without a row saying "including VAT" has not
    stated a total including VAT, and reading the excl.-VAT row instead would rank a bidder on
    a number 18% short of everyone else's.
    """
    if book.summary_rows:
        row = _summary_role(book, "incl" if incl_vat else "excl")
        return row.value if row is not None else None
    pattern = _INCL_VAT if incl_vat else _EXCL_VAT
    hits = [v for k, v in book.summary.items() if v is not None and pattern.search(k)]
    return hits[-1] if hits else None


def stated_total_cost(book: BoqWorkbook) -> float | None:
    """The Summary's TOTAL COST: the sum of the sections, before overheads, profit and VAT."""
    row = _summary_role(book, "cost")
    return row.value if row is not None else None


# --- comparison ---------------------------------------------------------------------------


@dataclass(slots=True)
class LineDiff:
    sheet: str
    key: str
    description: str
    template_qty: float | None
    bid_qty: float | None
    row: int
    #: the bidder's own amount on the line (what a filled-in blank position costs)
    amount: float | None = None
    item_no: str = ""


@dataclass(slots=True)
class PositionEval:
    """One template position, as this bidder's bid prices it (GOLD_SPEC section 5)."""

    sheet: str
    row: int
    item_no: str
    description: str
    template_qty: float | None
    #: the bidder's row, None when the position is not in the bid
    bid_row: int | None
    #: unit rate: the rate column, else the sum of the materials, labour and delivery rates
    rate: float | None
    #: template quantity x rate, or the bidder's own amount where the template quantity is blank
    cost: float
    #: priced | filled_blank | unpriced | not_filled | non_numeric | removed | sheet_missing |
    #: sheet_unreadable | optional | optional_priced
    status: str
    in_common_scope: bool = False
    #: the positions priced as one: a section row ("A8") and the rows numbered under it ("8.1")
    group: str = ""

    @property
    def priced(self) -> bool:
        return self.status in ("priced", "filled_blank")

    @property
    def ref(self) -> tuple[str, int]:
        return (self.sheet, self.row)

    @property
    def group_ref(self) -> tuple[str, str]:
        return (self.sheet, self.group or f"r{self.row}")


@dataclass(slots=True)
class BidComparison:
    bidder: str
    quantity_changed: list[LineDiff] = field(default_factory=list)
    filled_blank: list[LineDiff] = field(default_factory=list)
    added: list[BoqLine] = field(default_factory=list)
    removed: list[BoqLine] = field(default_factory=list)
    added_sheets: list[str] = field(default_factory=list)
    removed_sheets: list[str] = field(default_factory=list)
    unpriced_sheets: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    matched: list[tuple[BoqLine, BoqLine]] = field(default_factory=list)
    #: (template line, bid line) matched as one position although the description differs
    description_changed: list[tuple[BoqLine, BoqLine]] = field(default_factory=list)
    #: bid lines priced where the template has no position (a heading, a subtotal row)
    priced_headings: list[BoqLine] = field(default_factory=list)
    #: template positions with a quantity that the bid leaves unpriced (or prices in words)
    unpriced_positions: list[BoqLine] = field(default_factory=list)
    #: template positions with a blank quantity ("bidder fills in") that the bid left blank
    unfilled_positions: list[BoqLine] = field(default_factory=list)
    #: bid lines priced on the template's placeholder for "any other items": the bidder's own
    #: additions, listed and left out of the evaluation like an added line
    priced_optional: list[BoqLine] = field(default_factory=list)
    #: (bid line, column key, text) - text in a number cell, never read as 0
    non_numeric: list[tuple[BoqLine, str, str]] = field(default_factory=list)
    #: template sheets present in the bid whose columns could not be recognised
    unreadable_sheets: list[str] = field(default_factory=list)
    positions: list[PositionEval] = field(default_factory=list)
    #: False when the bid (or the template) cannot be evaluated at all: saved without values
    evaluable: bool = True
    no_cached_values: bool = False
    stated_total_cost: float | None = None
    stated_excl_vat: float | None = None
    stated_incl_vat: float | None = None
    #: stated incl. VAT / stated TOTAL COST: the bidder's own overheads, profit and VAT
    markup_factor: float | None = None
    evaluated_cost: float = 0.0
    evaluated_total: float | None = None
    common_scope_cost: float | None = None
    common_scope_total: float | None = None
    common_scope_positions: int = 0
    #: "red" (highest common-scope total), "green" (lowest) or None; set by ``compare_all``
    colour: str | None = None
    #: stated total incl. VAT minus evaluated total, broken down (incl. VAT, each term x markup):
    #: quantities, added_lines, priced_headings, added_sheets, summary, other
    difference: dict[str, float] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        """Every template position with a quantity is in the bid and priced."""
        return self.evaluable and not (
            self.removed_sheets_priced
            or self.unreadable_sheets
            or self.unpriced_positions
            or any(p.status in ("removed", "sheet_missing") for p in self.positions)
        )

    @property
    def removed_sheets_priced(self) -> list[str]:
        return sorted({p.sheet for p in self.positions if p.status == "sheet_missing"})


def _pair_leftovers(
    removed: list[BoqLine], added: list[BoqLine]
) -> tuple[list[tuple[BoqLine, BoqLine]], list[BoqLine], list[BoqLine]]:
    """Pair what the sequence alignment left over: a moved position, or a typo'd description.

    First the same key anywhere (two positions swapped), then the same item number with a
    description at least ``_DESC_SIMILARITY`` alike - best pairs first, each line used once, so
    two near-twin descriptions ("partition type 1", "type 2") cannot cross over.
    """
    pairs: list[tuple[BoqLine, BoqLine]] = []
    used_t: set[int] = set()
    used_b: set[int] = set()
    for i, t in enumerate(removed):
        for j, b in enumerate(added):
            if j not in used_b and b.key == t.key:
                pairs.append((t, b))
                used_t.add(i)
                used_b.add(j)
                break
    candidates: list[tuple[float, int, int]] = []
    for i, t in enumerate(removed):
        if i in used_t or not t.item_no:
            continue
        for j, b in enumerate(added):
            if j in used_b or b.item_no.strip().lower() != t.item_no.strip().lower():
                continue
            ratio = difflib.SequenceMatcher(
                None, _fold_desc(t.description), _fold_desc(b.description), autojunk=False
            ).ratio()
            if ratio >= _DESC_SIMILARITY:
                candidates.append((ratio, i, j))
    for _, i, j in sorted(candidates, key=lambda c: (-c[0], c[1], c[2])):
        if i in used_t or j in used_b:
            continue
        pairs.append((removed[i], added[j]))
        used_t.add(i)
        used_b.add(j)
    rest_t = [t for i, t in enumerate(removed) if i not in used_t]
    rest_b = [b for j, b in enumerate(added) if j not in used_b]
    return pairs, rest_b, rest_t


def _align(
    template: list[BoqLine], bid: list[BoqLine]
) -> tuple[list[tuple[BoqLine, BoqLine]], list[BoqLine], list[BoqLine]]:
    """Align two line lists: (matched, added, removed).

    Sequence alignment first: it keeps document order, tolerates repeated boilerplate
    descriptions that a dict would silently collapse, and is what turns "added/removed
    position" into a real answer instead of a by-product of key collisions. What it leaves
    over is paired by ``_pair_leftovers``: a position moved or re-worded is still the position,
    not a removal plus an unrelated addition.
    """
    matcher = difflib.SequenceMatcher(
        None, [line.key for line in template], [line.key for line in bid], autojunk=False
    )
    matched: list[tuple[BoqLine, BoqLine]] = []
    removed: list[BoqLine] = []
    added: list[BoqLine] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            matched.extend(zip(template[i1:i2], bid[j1:j2], strict=True))
        else:
            removed.extend(template[i1:i2])
            added.extend(bid[j1:j2])
    pairs, added, removed = _pair_leftovers(removed, added)
    if pairs:
        matched.extend(pairs)
        matched.sort(key=lambda pair: pair[0].row)
    return matched, added, removed


def _finding(
    rule: str,
    severity: Severity,
    ka: str,
    en: str,
    *,
    expected: str | None = None,
    actual: str | None = None,
    confidence: float = 1.0,
) -> Finding:
    return Finding(
        rule_id=rule,
        severity=severity,
        message_ka=ka,
        message_en=en,
        expected=expected,
        actual=actual,
        confidence=confidence,
    )


def _where(line: BoqLine) -> tuple[str, str]:
    """ "2. CIVIL WORKS row 15 (1.3)" in both languages."""
    item = f" ({line.item_no})" if line.item_no else ""
    return (
        f"{line.sheet}, სტრიქონი {line.row}{item}",
        f"{line.sheet} row {line.row}{item}",
    )


def _no_cached_values_finding(book: BoqWorkbook, *, template: bool = False) -> Finding:
    who_ka = "შაბლონი" if template else "ფაილი"
    who_en = "the template" if template else "the workbook"
    return _finding(
        "BOQ.NO_CACHED_VALUES",
        "error",
        f"{book.name}: {who_ka} შენახულია გამოთვლილი მნიშვნელობების გარეშე "
        f"({book.uncached_cells} ფორმულიდან {book.formula_cells}-ს მნიშვნელობა არ აქვს) — "
        "გახსენით Excel-ში, შეინახეთ და ატვირთეთ თავიდან; შედარება არ ჩატარებულა",
        f"{book.name}: {who_en} was saved without calculated values ({book.uncached_cells} "
        f"of {book.formula_cells} formulas have none) - open it in Excel, save and upload it "
        "again; it was not compared",
        actual=f"{book.uncached_cells}/{book.formula_cells}",
    )


def template_findings(template: BoqWorkbook) -> list[Finding]:
    """What is wrong with, or skipped in, the template itself - true for every bidder alike."""
    out: list[Finding] = []
    if template.no_cached_values:
        out.append(_no_cached_values_finding(template, template=True))
    for s in template.unreadable_sheets:
        out.append(
            _finding(
                "BOQ.SHEET_UNREADABLE",
                "error",
                f"შაბლონის გვერდის სვეტები ვერ ამოვიცანით: {s} — მისი პოზიციები არცერთ "
                "კომპანიასთან არ შედარდა",
                f"template sheet whose columns could not be recognised: {s} - its positions "
                "were not compared for any bidder",
                expected=s,
            )
        )
    for s in template.helper_sheets:
        out.append(
            _finding(
                "BOQ.SHEET_SKIPPED",
                "info",
                f"გვერდი {s} BOQ-ის სვეტებს არ შეიცავს და Summary მას არ ეყრდნობა — "
                "შედარებაში არ მონაწილეობს",
                f"sheet {s} has no BOQ columns and the Summary does not refer to it - not compared",
                actual=s,
            )
        )
    for name, sheet in template.sheets.items():
        if "unit" not in sheet.columns:
            # an unknown quantity label fails loudly (the sheet is unreadable); an unknown unit
            # label used to fail silently - no unit was ever compared on the sheet
            out.append(
                _finding(
                    "BOQ.UNITS_NOT_COMPARED",
                    "info",
                    f"გვერდზე {name} ერთეულის სვეტი ვერ ამოვიცანით — ერთეულები არ შედარებულა",
                    f"sheet {name}: no unit column could be recognised - units were not compared",
                    actual=name,
                )
            )
    for name, sheet in template.sheets.items():
        if not _reads_prices(sheet):
            out.append(_prices_not_read(name, template=True))
    for sheet in template.sheets.values():
        for line in sheet.leaf_lines:
            for key, text in line.non_numeric:
                if key == "qty" and (two := _two_readings(text)):
                    ka, en = _where(line)
                    low, high = _qty(two[0]), _qty(two[1])
                    out.append(
                        _finding(
                            "BOQ.NON_NUMERIC",
                            "warning",
                            f"შაბლონი, {ka}: მოცულობა ტექსტია „{text}“ — {low} თუ {high}, "
                            "სტრიქონიდან ვერ დგინდება; პოზიცია არცერთ კომპანიასთან არ ფასდება",
                            f"template, {en}: the quantity is the text '{text}' - {low} or "
                            f"{high}, and its row does not tell which; the position is not "
                            "evaluated for any bidder",
                            actual=text,
                        )
                    )
                elif key == "qty":
                    ka, en = _where(line)
                    out.append(
                        _finding(
                            "BOQ.NON_NUMERIC",
                            "warning",
                            f"შაბლონი, {ka}: მოცულობის უჯრაში ტექსტია „{text}“",
                            f"template, {en}: the quantity cell holds text '{text}'",
                            actual=text,
                        )
                    )
    return out


#: The columns a price is read from: a rate, an amount, or their components.
_PRICE_KEYS = (*_RATE_KEYS, *_TOTAL_KEYS)


def _reads_prices(sheet: BoqSheet) -> bool:
    """Whether any rate or amount column of the sheet was recognised."""
    return any(key in sheet.columns for key in _PRICE_KEYS)


def _prices_not_read(name: str, *, template: bool) -> Finding:
    """A sheet with quantities but no rate or amount column this reader recognised: the
    reader's gap, never "section left unpriced" on bidders who priced every line."""
    who_ka, who_en = ("შაბლონის ", "template ") if template else ("", "")
    return _finding(
        "BOQ.PRICES_NOT_READ",
        "warning",
        f"{who_ka}გვერდზე {name} ერთეულის ფასის და ღირებულების სვეტები ვერ ამოვიცანით — "
        "ფასები არ წაკითხულა და გვერდი შეფასებაში არ მონაწილეობს",
        f"{who_en}sheet {name}: no rate or amount column could be recognised - its prices were "
        "not read and the sheet takes no part in the evaluation",
        actual=name,
    )


def compare_bid(template: BoqWorkbook, bid: BoqWorkbook) -> BidComparison:
    """One bid against the template: differences, arithmetic and the evaluation.

    The common scope and the red/green colours need every bid, so ``compare_all`` sets them.
    """
    out = BidComparison(bidder=bid.name)
    if template.no_cached_values:
        out.evaluable = False  # reported once, in ``template_findings``
        return out
    if bid.read_error is not None:
        out.evaluable = False
        out.findings.append(
            _finding(
                "BOQ.UNREADABLE",
                "error",
                f"{bid.name}: ფაილი ვერ წავიკითხეთ ({bid.read_error}) — შეამოწმეთ, რომ ეს "
                "Excel-ის .xlsx ფაილია, და ატვირთეთ თავიდან; შედარება არ ჩატარებულა",
                f"{bid.name}: the workbook could not be read ({bid.read_error}) - check that it "
                "is an Excel .xlsx file and upload it again; it was not compared",
                actual=bid.read_error,
            )
        )
        return out
    if bid.no_cached_values:
        out.evaluable = False
        out.no_cached_values = True
        out.findings.append(_no_cached_values_finding(bid))
        return out

    priced_template_sheets = set(template.sheets) | set(template.unreadable_sheets)
    out.added_sheets = [s for s in bid.sheet_order if s not in template.sheet_order]
    out.removed_sheets = [s for s in template.sheet_order if s not in bid.sheet_order]
    for s in out.added_sheets:
        out.findings.append(
            _finding(
                "BOQ.SHEET_ADDED",
                "warning",
                f"დამატებულია გვერდი, რომელიც შაბლონში არ არის: {s}",
                f"sheet not present in the template: {s}",
                actual=s,
            )
        )
    for s in out.removed_sheets:
        if s == template.summary_sheet:
            out.findings.append(_summary_missing(s, bid))
            continue
        priced = s in priced_template_sheets
        out.findings.append(
            _finding(
                "BOQ.SHEET_REMOVED",
                "error" if priced else "warning",
                f"შაბლონის გვერდი ამოღებულია: {s}"
                + ("" if priced else " (დამხმარე გვერდი, ფასიანი პოზიციების გარეშე)"),
                f"template sheet is missing: {s}"
                + ("" if priced else " (a helper sheet with no priced positions)"),
                expected=s,
            )
        )

    for sheet_name, t_sheet in template.sheets.items():
        b_sheet = bid.sheets.get(sheet_name)
        if b_sheet is None:
            status = "sheet_missing"
            if sheet_name in bid.sheet_order:
                status = "sheet_unreadable"
                out.unreadable_sheets.append(sheet_name)
                out.findings.append(
                    _finding(
                        "BOQ.SHEET_UNREADABLE",
                        "error",
                        f"გვერდის სვეტები ვერ ამოვიცანით: {sheet_name} — მისი პოზიციები "
                        "შედარებაში და შეფასებაში არ შედის",
                        f"sheet whose columns could not be recognised: {sheet_name} - its "
                        "positions are left out of the comparison and the evaluation",
                        actual=sheet_name,
                    )
                )
            out.positions.extend(
                PositionEval(
                    sheet=sheet_name,
                    row=t.row,
                    item_no=t.item_no,
                    description=t.description,
                    template_qty=t.quantity,
                    bid_row=None,
                    rate=None,
                    cost=0.0,
                    status=status,
                )
                for t in t_sheet.leaf_lines
            )
            continue
        _compare_sheet(out, t_sheet, b_sheet)
        gap = reconcile(b_sheet)
        if gap is not None:
            out.findings.append(gap)

    # Sheets only the bidder has: nothing to compare quantities against, but every line on
    # them still adds into the bidder's own total, so its arithmetic is checked.
    for sheet_name, b_sheet in bid.sheets.items():
        if sheet_name not in template.sheets and sheet_name not in template.unreadable_sheets:
            for b_line in b_sheet.lines:
                out.findings.extend(_arithmetic_findings(b_line, ()))

    _evaluate(out, bid)
    if bid.summary_sheet is not None and out.markup_factor is None:
        out.findings.append(_not_ranked(out))
    out.findings.extend(reconcile_summary(bid))
    _list_findings(out)
    return out


def _not_ranked(out: BidComparison) -> Finding:
    """The bid has a Summary, but no markup could be taken from it: say which row is missing
    rather than leave the bidder uncoloured without a word."""
    if out.stated_incl_vat is None:
        ka, en = "ჯამი დღგ-ს ჩათვლით", "the total incl. VAT"
    else:
        ka, en = "TOTAL COST", "TOTAL COST"
    return _finding(
        "BOQ.NOT_RANKED",
        "warning",
        f"{out.bidder}: Summary-ში ვერ ვიპოვეთ სტრიქონი „{ka}“ — კომპანიის ფასნამატი (ზედნადები, "
        "მოგება, დღგ) ვერ გამოითვალა და კომპანია რანჟირებაში არ მონაწილეობს",
        f"{out.bidder}: no Summary row states {en} - the bidder's markup (overheads, profit, "
        "VAT) could not be worked out, so the bid is not ranked",
        expected=en,
    )


def _summary_missing(name: str, bid: BoqWorkbook) -> Finding:
    """The template's Summary is not in the bid: it holds the totals the bidder states."""
    if bid.summary_sheet is not None:
        return _finding(
            "BOQ.SHEET_REMOVED",
            "warning",
            f"შაბლონის Summary გვერდი ({name}) არ არის; ჯამები წაკითხულია გვერდიდან "
            f"{bid.summary_sheet}",
            f"the template's Summary sheet ({name}) is missing; the stated totals are read from "
            f"sheet {bid.summary_sheet}",
            expected=name,
        )
    return _finding(
        "BOQ.SHEET_REMOVED",
        "error",
        f"შაბლონის Summary გვერდი ამოღებულია: {name} — კომპანიის ჯამები (TOTAL COST, დღგ-ს "
        "ჩათვლით) არ არის მითითებული, ამიტომ რანჟირებაში არ მონაწილეობს",
        f"the template's Summary sheet is missing: {name} - it holds the stated totals (TOTAL "
        "COST, incl. VAT), so the bid states none and is not ranked",
        expected=name,
    )


def _rate_text_blocks(line: BoqLine) -> bool:
    """Whether text in the line's number cells leaves it without a price (GOLD_SPEC 5).

    Text in the quantity, the amount or the rate ("incl.", "N/A", "-") is never read as a
    number: the position is not priced. Text in one component of the rate ("-" under labour)
    matters only when the rate itself is not stated and would have to be built from the parts -
    beside a stated rate it says the part costs nothing."""
    keys = {key for key, _ in line.non_numeric}
    if keys & {"qty", "amount", "rate_total"}:
        return True
    return not line.rate_total and bool(keys & set(_RATE_KEYS))


def _qty_reads_two_ways(line: BoqLine) -> bool:
    """Whether the line's quantity is text such as "1.250" that its row could not settle."""
    return any(key == "qty" and _two_readings(text) for key, text in line.non_numeric)


def _unit_rate(line: BoqLine, columns: dict[str, int]) -> float | None:
    """The bidder's unit rate: the rate column, else the sum of the component rates.

    A sheet with no rate column at all (only an amount) prices a line as amount / quantity.
    """
    if line.rate_total:
        return line.rate_total
    parts = (line.rate_materials, line.rate_labor, line.rate_delivery)
    if any(parts):
        return sum(p or 0.0 for p in parts)
    if not any(k in columns for k in _RATE_KEYS) and line.quantity and line.amount:
        return line.amount / line.quantity
    return None


#: The template's invitation to list what it does not describe: "The tender is to list here
#: below any other items", "Other costs if necessary", "სხვა, აღწერაში არ შეტანილი". A bidder
#: need not price it, and one who does has added work of their own. Matched by what it asks,
#: not by fixed phrases, and only where it asks it of the bidder:
#:
#: * "any other" item, work or cost anywhere ("Any other undescribed items");
#: * the bidder addressed: "the tender(er) / bidder is to list", "list here", "to be listed
#:   here / below / by the tenderer" - "texts to be listed by the Engineer" is described work;
#: * "Undescribed" and "სხვა ხარჯები" as the opening words - "removal of undescribed
#:   obstructions" and "... და სხვა ხარჯები" qualify a described position;
#: * "აღწერაში არ შეტანილი" (not in the description) anywhere, and "Other costs if necessary";
#: * a description that *opens* with "Other(s)" / "სხვა" and asks the bidder to specify or list,
#:   or says it is not included above / in this BOQ / elsewhere.
#:
#: Only the opening word counts: "Paint to walls and other surfaces, colour to be specified by
#: the Engineer" is a described position, and read as a placeholder it would drop out of the
#: evaluation and the common scope. "სხვადასხვა" (sundry items the template describes) is never
#: one. A line under a heading that invites is one too (``_placeholder_rows``).
_PLACEHOLDER = re.compile(
    r"any\s+other\s+(?:[a-z]+\s+)?(?:item|work|cost)s?\b|"
    r"\b(?:tender(?:er)?|bidder)s?\s+(?:is\s+|are\s+)?to\s+list\b|\blist\s+here\b|"
    r"\bto\s+be\s+listed\s+(?:here|below|by\s+the\s+(?:tenderer|bidder))\b|"
    r"^\W*undescribed\b|other\s+costs\s+if\s+necessary|"
    r"^\W*others?\b.*\b(?:specify|to\s+be\s+(?:specified|listed)|"
    r"not\s+(?:otherwise\s+)?(?:included|covered|described|listed|mentioned)\s+"
    r"(?:above|elsewhere|in\s+(?:this|the)\s+(?:boq|bill|list|schedule|tender)))\b|"
    r"აღწერაში\s+არ\s+შეტანილ|^\W*სხვა\s+ხარჯ|"
    r"^\W*სხვა(?![ა-ჰ]).*(?:არ\s+არის\s+გათვალისწინებ|მიუთითეთ|ჩამოწერეთ|ჩამოთვალეთ|"
    r"დააკონკრეტეთ)",
    re.IGNORECASE,
)
#: A section row that is itself priced ("A8 Progress and Final Photographs"), whose own items are
#: numbered under it ("8.1").
_SECTION_POSITION = re.compile(r"^[A-Z](\d+)$")


def _placeholder_rows(lines: list[BoqLine]) -> set[int]:
    """The template's positions that invite the bidder's own items (GOLD_SPEC 5): by their own
    text, or by the heading they stand under.

    "Undescribed items (Bidder to list and price)" over "Item 1", "Item 2": the invitation is
    the heading's, and each line under it is a slot for the bidder's own work - read as a
    filled-in quantity, a bidder's extra counted in their evaluation and flipped the colours. A
    heading reaches the positions below it until the next row that is not a position (the
    group's Sub-total, the next heading) or the next priced section row ("A3").
    """
    out: set[int] = set()
    inviting = False
    for line in lines:
        if line.kind != "position" or _SECTION_POSITION.match(line.item_no.strip()):
            inviting = bool(_PLACEHOLDER.search(line.description))
        if line.kind == "position" and (inviting or _PLACEHOLDER.search(line.description)):
            out.add(line.row)
    return out


def _position_groups(lines: list[BoqLine]) -> dict[int, str]:
    """Each position's group: a priced section row and the positions numbered under it are one.

    The client's preliminaries price some sections on their own row and some on the item under
    it ("A8" and "8.1" both carry an amount formula); bidders price one or the other. Counted
    apart, every bidder "left unpriced" whichever they did not price, and both rows fell out of
    the common scope.
    """
    groups: dict[int, str] = {}
    parent: tuple[str, str] | None = None
    for line in lines:
        if line.kind != "position":
            continue
        item = line.item_no.strip()
        if m := _SECTION_POSITION.match(item):
            parent = (item, m.group(1))
            groups[line.row] = item
        elif parent is not None and item.startswith(parent[1] + "."):
            groups[line.row] = parent[0]
        else:
            groups[line.row] = f"r{line.row}"
    return groups


def _compare_sheet(out: BidComparison, t_sheet: BoqSheet, b_sheet: BoqSheet) -> None:
    matched, added, removed = _align(t_sheet.lines, b_sheet.lines)
    out.matched.extend(matched)
    out.added.extend(added)
    out.removed.extend(removed)
    by_template_row = {t.row: b for t, b in matched}

    for t_line, b_line in matched:
        if _fold_desc(t_line.description) != _fold_desc(b_line.description):
            out.description_changed.append((t_line, b_line))
        t_unit, b_unit = norm_unit(t_line.unit), norm_unit(b_line.unit)
        if t_unit and b_unit and t_unit != b_unit:
            ka, en = _where(b_line)
            out.findings.append(
                _finding(
                    "BOQ.UNIT_CHANGED",
                    "error",
                    f"{ka}: ერთეული შეცვლილია {t_line.unit} → {b_line.unit}",
                    f"{en}: unit changed {t_line.unit} -> {b_line.unit}",
                    expected=t_line.unit,
                    actual=b_line.unit,
                )
            )
        elif t_unit and not b_unit and t_line.kind == "position":
            # the unit cell cleared: most likely a slip, but the bid no longer says what its
            # quantity counts
            ka, en = _where(b_line)
            out.findings.append(
                _finding(
                    "BOQ.UNIT_CHANGED",
                    "warning",
                    f"{ka}: ერთეული ცარიელია, შაბლონში {t_line.unit}",
                    f"{en}: unit left blank, the template says {t_line.unit}",
                    expected=t_line.unit,
                    actual="",
                )
            )
        for key, text in b_line.non_numeric:
            out.non_numeric.append((b_line, key, text))
        typed_over = tuple(k for k in b_line.hardcoded if k in t_line.formulas)
        if t_line.kind != "position":
            if b_line.kind == "position" and b_line.amount:
                out.priced_headings.append(b_line)
            out.findings.extend(_arithmetic_findings(b_line, typed_over))
            continue
        qty_is_text = any(key == "qty" for key, _ in b_line.non_numeric)
        if not qty_is_text and not _same_quantity(t_line.quantity, b_line.quantity):
            diff = LineDiff(
                sheet=t_line.sheet,
                key=t_line.key,
                item_no=t_line.item_no,
                description=t_line.description,
                template_qty=t_line.quantity,
                bid_qty=b_line.quantity,
                row=b_line.row,
                amount=b_line.amount,
            )
            # a blank template quantity means "bidder fills this in" (staffing, insurance)
            if t_line.quantity:
                out.quantity_changed.append(diff)
        out.findings.extend(_arithmetic_findings(b_line, typed_over))
    # An added position is priced by the bidder like any other and counts toward their own
    # total, so its arithmetic is checked too. Keeping it "out of the general analysis" (the
    # client's rule) means out of the quantity comparison and the evaluation - not out of
    # cross-footing.
    # an added line has no template cell of its own: a total typed on it is flagged where the
    # template computes that column on its positions
    computed = {k for t in t_sheet.leaf_lines for k in t.formulas}
    for b_line in added:
        out.findings.extend(
            _arithmetic_findings(b_line, tuple(k for k in b_line.hardcoded if k in computed))
        )
        for key, text in b_line.non_numeric:
            out.non_numeric.append((b_line, key, text))

    # the evaluation of every template position on the sheet
    placeholders = _placeholder_rows(t_sheet.lines)
    for t in t_sheet.leaf_lines:
        b = by_template_row.get(t.row)
        if b is None:
            out.positions.append(
                PositionEval(
                    t.sheet, t.row, t.item_no, t.description, t.quantity, None, None, 0.0, "removed"
                )
            )
            continue
        rate = _unit_rate(b, b_sheet.columns)
        text_cols = {key for key, _ in b.non_numeric}
        if t.row in placeholders:
            if b.amount or rate:
                out.priced_optional.append(b)
            out.positions.append(
                PositionEval(
                    t.sheet,
                    t.row,
                    t.item_no,
                    t.description,
                    t.quantity,
                    b.row,
                    rate,
                    0.0,
                    "optional_priced" if (b.amount or rate) else "optional",
                )
            )
            continue
        if _qty_reads_two_ways(t):
            # the template's own quantity reads 1.25 or 1,250 and its row cannot tell which: not
            # evaluated for any bidder - never at a quantity a thousand times off, and never at
            # the bidder's own amount as if the template had left it blank
            out.positions.append(
                PositionEval(
                    t.sheet, t.row, t.item_no, t.description, None, b.row, rate, 0.0, "non_numeric"
                )
            )
            continue
        if t.quantity:
            if _rate_text_blocks(b):
                cost, status = 0.0, "non_numeric"
            elif rate:
                cost, status = t.quantity * rate, "priced"
            else:
                cost, status = 0.0, "unpriced"
            if status != "priced":
                out.unpriced_positions.append(t)
        else:
            amount = b.amount or ((b.quantity or 0.0) * rate if rate else 0.0)
            cost = amount
            if amount or b.quantity:
                status = "filled_blank"
                out.filled_blank.append(
                    LineDiff(
                        sheet=t.sheet,
                        key=t.key,
                        item_no=t.item_no,
                        description=t.description,
                        template_qty=t.quantity,
                        bid_qty=b.quantity,
                        row=b.row,
                        amount=amount,
                    )
                )
            elif text_cols & {*_RATE_KEYS, "amount", "qty"}:
                status = "non_numeric"
                out.unfilled_positions.append(t)
            else:
                status = "not_filled"
                out.unfilled_positions.append(t)
        out.positions.append(
            PositionEval(
                t.sheet, t.row, t.item_no, t.description, t.quantity, b.row, rate, cost, status
            )
        )

    # a group is priced when any of its rows is: a position is unpriced only when its whole
    # group is
    groups = _position_groups(t_sheet.lines)
    sheet_positions = [p for p in out.positions if p.sheet == t_sheet.name]
    for pos in sheet_positions:
        pos.group = groups.get(pos.row, f"r{pos.row}")
    priced_groups = {pos.group for pos in sheet_positions if pos.priced}
    out.unpriced_positions = [
        t
        for t in out.unpriced_positions
        if t.sheet != t_sheet.name or groups.get(t.row, f"r{t.row}") not in priced_groups
    ]

    priced = [line for line in b_sheet.lines if line.is_priced]
    if not _reads_prices(b_sheet):
        # the reader's gap, not the bidder's: said once, on the template, when it is the same
        if _reads_prices(t_sheet):
            out.findings.append(_prices_not_read(b_sheet.name, template=False))
    elif not priced and any(line.quantity for line in t_sheet.lines):
        out.unpriced_sheets.append(t_sheet.name)


def _evaluate(out: BidComparison, bid: BoqWorkbook) -> None:
    """Evaluated cost and total, the stated totals, and why the two differ (GOLD_SPEC 5)."""
    out.evaluated_cost = sum(p.cost for p in out.positions)
    out.stated_total_cost = stated_total_cost(bid)
    out.stated_excl_vat = grand_total(bid, incl_vat=False)
    out.stated_incl_vat = grand_total(bid, incl_vat=True)
    if out.stated_incl_vat is not None and out.stated_total_cost:
        out.markup_factor = out.stated_incl_vat / out.stated_total_cost
    if out.markup_factor is None:
        return
    markup = out.markup_factor
    out.evaluated_total = out.evaluated_cost * markup

    bid_rows = {(b.sheet, b.row): b for _, b in out.matched}
    quantities = 0.0
    for p in out.positions:
        # a priced placeholder is the bidder's own addition, counted under optional_items: each
        # amount of the bid belongs to one part of the breakdown
        if p.bid_row is None or p.status in ("optional", "optional_priced"):
            continue
        b = bid_rows.get((p.sheet, p.bid_row))
        own = b.amount or 0.0 if b is not None and b.kind == "position" else 0.0
        quantities += own - p.cost
    parts = {
        "quantities": quantities,
        "added_lines": sum(b.amount or 0.0 for b in out.added if b.kind == "position"),
        "optional_items": sum(b.amount or 0.0 for b in out.priced_optional),
        "priced_headings": sum(b.amount or 0.0 for b in out.priced_headings),
        "added_sheets": sum(bid.sheets[s].total for s in out.added_sheets if s in bid.sheets),
        "summary": (out.stated_total_cost or 0.0) - bid.total,
    }
    gap = (out.stated_total_cost or 0.0) - out.evaluated_cost
    parts["other"] = gap - sum(parts.values())
    out.difference = {
        k: round(v * markup, 2) or 0.0 for k, v in parts.items() if abs(v * markup) >= 0.005
    }


def _item(d: LineDiff) -> str:
    return f" ({d.item_no})" if d.item_no else ""


def _list_findings(out: BidComparison) -> None:
    """One finding per listed difference, in reading order."""
    for d in out.quantity_changed:
        out.findings.append(
            _finding(
                "BOQ.QUANTITY_CHANGED",
                "error",
                f"{d.sheet}, სტრიქონი {d.row}{_item(d)}: მოცულობა შეცვლილია "
                f"{_qty(d.template_qty)} → {_qty(d.bid_qty)} ({d.description[:50]}); "
                "შეფასება შაბლონის მოცულობით",
                f"{d.sheet} row {d.row}{_item(d)}: quantity changed "
                f"{_qty(d.template_qty)} -> {_qty(d.bid_qty)} ({d.description[:50]}); "
                "evaluated at the template quantity",
                expected=_qty(d.template_qty),
                actual=_qty(d.bid_qty),
            )
        )
    for d in out.filled_blank:
        out.findings.append(
            _finding(
                "BOQ.FILLED_BLANK",
                "info",
                f"{d.sheet}, სტრიქონი {d.row}{_item(d)}: შაბლონში მოცულობა ცარიელია, შეავსო "
                f"{_qty(d.bid_qty)}, თანხა {_money(d.amount or 0.0)} ({d.description[:50]}); "
                "შეფასებაში კომპანიის თანხით",
                f"{d.sheet} row {d.row}{_item(d)}: the template leaves the quantity blank, "
                f"the bidder filled in {_qty(d.bid_qty)}, amount {_money(d.amount or 0.0)} "
                f"({d.description[:50]}); evaluated at the bidder's own amount",
                expected=_qty(d.template_qty),
                actual=_qty(d.bid_qty),
            )
        )
    for t_line, b_line in out.description_changed:
        ka, en = _where(b_line)
        out.findings.append(
            _finding(
                "BOQ.DESCRIPTION_CHANGED",
                "warning",
                f"{ka}: აღწერა შეცვლილია — შაბლონში „{t_line.description[:60]}“, "
                f"წინადადებაში „{b_line.description[:60]}“",
                f"{en}: description changed - template '{t_line.description[:60]}', "
                f"bid '{b_line.description[:60]}'",
                expected=t_line.description[:60],
                actual=b_line.description[:60],
            )
        )
    for line in out.added:
        ka, en = _where(line)
        out.findings.append(
            _finding(
                "BOQ.LINE_ADDED",
                "warning",
                f"{ka}: დამატებული პოზიცია — {line.description[:60]}; შეფასებაში არ შედის",
                f"{en}: added position - {line.description[:60]}; left out of the evaluation",
                actual=line.description[:60],
            )
        )
    for line in out.removed:
        ka, en = _where(line)
        out.findings.append(
            _finding(
                "BOQ.LINE_REMOVED",
                "error",
                f"{ka}: ამოღებული პოზიცია — {line.description[:60]}",
                f"{en}: removed position - {line.description[:60]}",
                expected=line.description[:60],
            )
        )
    for line in out.priced_headings:
        ka, en = _where(line)
        out.findings.append(
            _finding(
                "BOQ.HEADING_PRICED",
                "warning",
                f"{ka}: ფასი მითითებულია სტრიქონზე, რომელიც შაბლონში პოზიცია არ არის "
                f"({_money(line.amount or 0.0)}, {line.description[:50]}); შეფასებაში არ შედის",
                f"{en}: priced ({_money(line.amount or 0.0)}) on a row that is not a position "
                f"in the template ({line.description[:50]}); left out of the evaluation",
                actual=_money(line.amount or 0.0),
            )
        )
    for line, key, text in out.non_numeric:
        ka, en = _where(line)
        out.findings.append(
            _finding(
                "BOQ.NON_NUMERIC",
                "warning",
                f"{ka}: რიცხვის უჯრაში ({key}) ტექსტია „{text}“ — ნულად არ ითვლება, "
                "პოზიცია ფასდაუდებლად ითვლება",
                f"{en}: text '{text}' in a number cell ({key}) - not read as 0; the position "
                "counts as not priced",
                actual=text,
            )
        )
    for s in out.unpriced_sheets:
        out.findings.append(
            _finding(
                "BOQ.SECTION_UNPRICED",
                "warning",
                f"სექცია ფასდაუდებელია: {s} — სრული ჯამი შედარებადი არ არის; "
                "ფერები საერთო მოცულობით განისაზღვრება",
                f"section left unpriced: {s} - the full total is not like-for-like; the "
                "colours are decided on the common scope",
                actual=s,
            )
        )
    for b in out.priced_optional:
        ka, en = _where(b)
        out.findings.append(
            _finding(
                "BOQ.OPTIONAL_PRICED",
                "info",
                f"{ka}: კომპანიამ „სხვა სამუშაოების“ ველში საკუთარი პოზიცია დაამატა "
                f"({_money(b.amount or 0.0)}) — შეფასებაში არ შედის, როგორც დამატებული პოზიცია",
                f"{en}: the bidder priced the placeholder for other items "
                f"({_money(b.amount or 0.0)}) - its own addition, left out of the evaluation "
                "like an added position",
                actual=_money(b.amount or 0.0),
            )
        )
    unpriced_sheets = set(out.unpriced_sheets)
    # a position priced in words is still unpriced, but BOQ.NON_NUMERIC already names it: one
    # finding per mistake
    in_words = {(p.sheet, p.row) for p in out.positions if p.status == "non_numeric"}
    by_sheet: dict[str, list[BoqLine]] = {}
    for t in out.unpriced_positions:
        if t.sheet not in unpriced_sheets and (t.sheet, t.row) not in in_words:
            by_sheet.setdefault(t.sheet, []).append(t)
    for sheet, lines in by_sheet.items():
        items = [line.item_no or f"row {line.row}" for line in lines]
        shown = ", ".join(items[:15]) + (f" (+{len(items) - 15})" if len(items) > 15 else "")
        out.findings.append(
            _finding(
                "BOQ.POSITIONS_UNPRICED",
                "warning",
                f"{sheet}: {len(items)} პოზიცია ფასის გარეშეა — {shown}",
                f"{sheet}: {len(items)} position(s) left unpriced - {shown}",
                actual=shown,
            )
        )


def _arithmetic_findings(line: BoqLine, typed_over: tuple[str, ...] | None = None) -> list[Finding]:
    """Recompute what the workbook claims. The bidder controls this file.

    ``typed_over``: the computed columns where the bid typed a number over the template's
    formula (``BOQ.HARDCODED_TOTAL``). A number typed where the template itself has a number or
    nothing replaced no formula and is not flagged; without a template to ask (``None``), every
    typed total of the line is.
    """
    out: list[Finding] = []
    qty = line.quantity
    hardcoded = line.hardcoded if typed_over is None else typed_over

    def check(label: str, got: float | None, want: float | None) -> None:
        if got is None or want is None:
            return
        if not _same_money(got, want):
            out.append(
                _finding(
                    "BOQ.ARITHMETIC",
                    "error",
                    f"{line.sheet}, სტრიქონი {line.row}: {label} არ ემთხვევა გამოთვლას "
                    f"({got:,.2f} vs {want:,.2f})",
                    f"{line.sheet} row {line.row}: {label} does not match the computation "
                    f"({got:,.2f} vs {want:,.2f})",
                    expected=f"{want:,.2f}",
                    actual=f"{got:,.2f}",
                )
            )

    def times(rate: float | None) -> float | None:
        # No rate column, or an empty rate cell, means there is nothing to recompute from.
        # Treating it as zero made every priced line of a sheet without a Rate column an error.
        return rate * qty if rate is not None and qty is not None else None

    if qty:
        check("materials sum", line.sum_materials, times(line.rate_materials))
        check("labor sum", line.sum_labor, times(line.rate_labor))
        check("delivery sum", line.sum_delivery, times(line.rate_delivery))
        check("line amount", line.amount, times(_stated_rate(line)))
    rates = [line.rate_materials or 0, line.rate_labor or 0, line.rate_delivery or 0]
    if line.rate_total is not None and any(rates):
        check("total rate", line.rate_total, sum(rates))
    if hardcoded:
        out.append(
            _finding(
                "BOQ.HARDCODED_TOTAL",
                "warning",
                f"{line.sheet}, სტრიქონი {line.row}: ჯამი ფორმულის ნაცვლად ხელით არის ჩაწერილი "
                f"({', '.join(hardcoded)})",
                f"{line.sheet} row {line.row}: total typed in instead of computed "
                f"({', '.join(hardcoded)})",
                actual=", ".join(hardcoded),
            )
        )
    return out


def _stated_rate(line: BoqLine) -> float | None:
    """The unit rate the line's own cells state: the rate column, else - on a sheet with no rate
    column, or with the rate left empty - the sum of the component rates. An amount that leaves
    a component out (``=(E6+F6)*D6`` beside a delivery rate) is then recomputed too."""
    if line.rate_total is not None:
        return line.rate_total
    parts = (line.rate_materials, line.rate_labor, line.rate_delivery)
    if all(p is None for p in parts):
        return None
    return sum(p or 0.0 for p in parts)


def reconcile(sheet: BoqSheet) -> Finding | None:
    """The positions read off a sheet must add up to the sheet's own closing total (its SUB-TOTAL
    row, or the row that closes it otherwise - ``_closing_line``).

    Either the bidder's workbook does not add up, or this reader missed or misread a row; both
    must be said out loud. A reader that dropped the first positions of four sheets once looked
    exactly like a correct one, because nothing ever compared what it read with what the
    workbook itself states.
    """
    if sheet.subtotal is None:
        return None
    lines_sum = sheet.total
    diff = lines_sum - sheet.subtotal
    if _same_money(lines_sum, sheet.subtotal):
        return None
    label = next((ln.description[:40] for ln in sheet.lines if ln.row == sheet.subtotal_row), "")
    return _finding(
        "BOQ.RECONCILE",
        "error",
        f"{sheet.name}: პოზიციების ჯამი {lines_sum:,.2f}-ია, ხოლო გვერდის ჯამის სტრიქონი "
        f"({sheet.subtotal_row} სტრიქონი, „{label}“) — {sheet.subtotal:,.2f}; სხვაობა "
        f"{diff:,.2f}",
        f"{sheet.name}: the positions add up to {lines_sum:,.2f}, but the sheet's closing total "
        f"(row {sheet.subtotal_row}, '{label}') states {sheet.subtotal:,.2f} - a difference of "
        f"{diff:,.2f}",
        expected=f"{sheet.subtotal:,.2f}",
        actual=f"{lines_sum:,.2f}",
    )


def _summary_gap(ka: str, en: str, stated: float, computed: float) -> Finding:
    return _finding(
        "BOQ.RECONCILE",
        "error",
        f"Summary: {ka} — მითითებულია {stated:,.2f}, გამოთვლით {computed:,.2f}; "
        f"სხვაობა {stated - computed:,.2f}",
        f"Summary: {en} - states {stated:,.2f}, computes to {computed:,.2f}; a difference of "
        f"{stated - computed:,.2f}",
        expected=f"{computed:,.2f}",
        actual=f"{stated:,.2f}",
    )


def _sheet_for_label(book: BoqWorkbook, row: SummaryRow) -> BoqSheet | None:
    """A Summary row typed in (no formula), matched to its sheet: "2 | CIVIL WORKS"."""
    label = re.sub(r"[^\w]", "", row.label.lower())
    for name, sheet in book.sheets.items():
        m = re.match(r"^\s*(\d+)\s*\.?\s*(.*)$", name)
        if (
            m
            and m.group(1) == row.item.strip()
            and re.sub(r"[^\w]", "", m.group(2).lower()) == label
        ):
            return sheet
    return None


def reconcile_summary(book: BoqWorkbook) -> list[Finding]:
    """Hold the Summary against the sheets and against itself.

    * every row that links to a sheet row (a section to its SUB-TOTAL, a group to its subtotal)
      must state what that row's amount is;
    * TOTAL COST must be the sum of the top-level sections above it;
    * overheads and profit must be TOTAL COST x their rate, TOTAL PRICE EXCL. VAT the sum of
      TOTAL COST and the rows between them (``_excl_parts``), VAT the excl. total x its rate,
      and the incl.-VAT total excl. + VAT.

    Each difference names the Summary rows involved.
    """
    out: list[Finding] = []
    rows = book.summary_rows
    if not rows:
        return out
    for row in rows:
        if row.non_numeric:
            out.append(
                _finding(
                    "BOQ.NON_NUMERIC",
                    "warning",
                    f"Summary, სტრიქონი {row.row} ({row.label[:40]}): ჯამის უჯრაში ტექსტია "
                    f"„{row.non_numeric}“",
                    f"Summary row {row.row} ({row.label[:40]}): the total cell holds text "
                    f"'{row.non_numeric}'",
                    actual=row.non_numeric,
                )
            )
        if row.value is None:
            continue
        target: tuple[BoqSheet, int] | None = None
        linked = {(s, r) for s, r in row.refs if s in book.sheets}
        if len(linked) == 1:
            sheet_name, sheet_row = next(iter(linked))
            target = (book.sheets[sheet_name], sheet_row)
        elif not row.refs and (sheet := _sheet_for_label(book, row)) and sheet.subtotal_row:
            target = (sheet, sheet.subtotal_row)
        if target is None:
            continue
        sheet, sheet_row = target
        line = next((ln for ln in sheet.lines if ln.row == sheet_row), None)
        if line is None or line.amount is None:
            continue
        if not _same_money(row.value, line.amount):
            out.append(
                _summary_gap(
                    f"სტრიქონი {row.row} ({row.label[:40]}) ≠ გვერდი {sheet.name}, "
                    f"სტრიქონი {sheet_row}",
                    f"row {row.row} ({row.label[:40]}) against sheet {sheet.name} row {sheet_row}",
                    row.value,
                    line.amount,
                )
            )

    roles = summary_roles(book)
    cost = roles.get("cost")
    if cost is not None and cost.value is not None:
        sections = _top_level_sections(rows, cost)
        if sections:
            total = sum(r.value or 0.0 for r in sections)
            if not _same_money(cost.value, total):
                out.append(
                    _summary_gap(
                        f"სტრიქონი {cost.row} ({cost.label[:30]}) ≠ სექციების ჯამი "
                        f"(სტრიქონები {', '.join(str(r.row) for r in sections)})",
                        f"row {cost.row} ({cost.label[:30]}) against the sum of the sections "
                        f"(rows {', '.join(str(r.row) for r in sections)})",
                        cost.value,
                        total,
                    )
                )
    _reconcile_chain(rows, roles, out)
    return out


#: The item of a top-level Summary section: "2", "2.", "C" - not a group under one ("A1").
_TOP_LEVEL_ITEM = re.compile(r"^(?:(\d{1,3})|([A-Za-z]))\.?$")


def _top_level_sections(rows: list[SummaryRow], cost: SummaryRow) -> list[SummaryRow]:
    """The sections TOTAL COST must add up: the valued rows above it numbered like a top-level
    section - all numbered the way the first one is ("0", "1", "2" in the client's Summary, "A",
    "B", "C" in another's), so a lettered section's numbered sub-sections are not added twice."""
    shaped = [
        (r, bool(m.group(1)))
        for r in rows
        if r.row < cost.row and r.value is not None and (m := _TOP_LEVEL_ITEM.match(r.item))
    ]
    if not shaped:
        return []
    digits = shaped[0][1]
    return [r for r, is_digit in shaped if is_digit == digits]


def _reconcile_chain(
    rows: list[SummaryRow], roles: dict[str, SummaryRow], out: list[Finding]
) -> None:
    cost, overheads, profit = roles.get("cost"), roles.get("overheads"), roles.get("profit")
    excl, vat, incl = roles.get("excl"), roles.get("vat"), roles.get("incl")

    def rated(part: SummaryRow | None, base: SummaryRow | None) -> None:
        if part is None or base is None or part.rate is None:
            return
        if part.value is None or base.value is None:
            return
        want = base.value * part.rate
        if not _same_money(part.value, want):
            out.append(
                _summary_gap(
                    f"სტრიქონი {part.row} ({part.label[:30]}) ≠ სტრიქონი {base.row} x "
                    f"{part.rate:g}",
                    f"row {part.row} ({part.label[:30]}) against row {base.row} x {part.rate:g}",
                    part.value,
                    want,
                )
            )

    rated(overheads, cost)
    rated(profit, cost)
    if excl is not None and excl.value is not None and cost is not None and cost.value is not None:
        parts = _excl_parts(rows, cost, excl)
        want = sum(r.value or 0.0 for r in parts)
        if not _same_money(excl.value, want):
            joined = " + ".join(str(r.row) for r in parts)
            out.append(
                _summary_gap(
                    f"სტრიქონი {excl.row} ({excl.label[:30]}) ≠ სტრიქონები {joined}",
                    f"row {excl.row} ({excl.label[:30]}) against rows {joined}",
                    excl.value,
                    want,
                )
            )
    if excl is None and cost is not None and _EXCL_VAT.search(cost.label):
        # "Total cost, excl. VAT" straight on to VAT: the cost is the total VAT is added to
        excl = cost
    rated(vat, excl)
    if incl is not None and incl.value is not None and excl is not None and excl.value is not None:
        want = excl.value + (vat.value or 0.0 if vat is not None else 0.0)
        if not _same_money(incl.value, want):
            joined = f"{excl.row} + {vat.row}" if vat is not None else str(excl.row)
            out.append(
                _summary_gap(
                    f"სტრიქონი {incl.row} ({incl.label[:30]}) ≠ სტრიქონები {joined}",
                    f"row {incl.row} ({incl.label[:30]}) against rows {joined}",
                    incl.value,
                    want,
                )
            )


def _excl_parts(rows: list[SummaryRow], cost: SummaryRow, excl: SummaryRow) -> list[SummaryRow]:
    """What the total excl. VAT adds up: TOTAL COST and every valued row between it and the
    excl.-VAT row - overheads and profit under whatever name ("გეგმიური დაგროვება", "Margin"),
    a contingency, a discount. A row stating the running sum so far ("ჯამი" after the overheads
    of a Georgian estimate) restates it and is not added again. Adding only the rows it
    recognised, the check failed every Summary whose profit is called something else."""
    parts = [cost]
    running = cost.value or 0.0
    for row in rows:
        if not cost.row < row.row < excl.row or row.value is None:
            continue
        if _same_money(row.value, running):
            continue
        parts.append(row)
        running += row.value
    return parts


def compare_all(template: BoqWorkbook, bids: list[BoqWorkbook]) -> list[BidComparison]:
    """Every bid against the template, then the common scope and the colours across them.

    The common scope is the set of template positions every evaluable bidder priced; it is the
    only total on which bidders compare like-for-like, so red and green are decided on it:
    highest red, lowest green, tied bidders the same colour, nobody coloured when fewer than two
    bidders have a total or they all tie.
    """
    comparisons = [compare_bid(template, b) for b in bids]
    evaluable = [c for c in comparisons if c.evaluable]
    if not evaluable:
        return comparisons
    common = set.intersection(*({p.group_ref for p in c.positions if p.priced} for c in evaluable))
    for comp in evaluable:
        comp.common_scope_positions = len(common)
        comp.common_scope_cost = 0.0
        for p in comp.positions:
            p.in_common_scope = p.group_ref in common
            if p.in_common_scope:
                comp.common_scope_cost += p.cost
        if comp.markup_factor is not None:
            comp.common_scope_total = comp.common_scope_cost * comp.markup_factor
    _colour(comparisons)
    return comparisons


def _colour(comparisons: list[BidComparison]) -> None:
    ranked = [
        c for c in comparisons if c.common_scope_total is not None and c.common_scope_positions
    ]
    if len(ranked) < 2:
        return
    totals = [round(c.common_scope_total or 0.0, 2) for c in ranked]
    high, low = max(totals), min(totals)
    if high == low:
        return
    for comp, total in zip(ranked, totals, strict=True):
        if total == high:
            comp.colour = "red"
        elif total == low:
            comp.colour = "green"


def summary_report(template: BoqWorkbook, comparisons: list[BidComparison]) -> CheckReport:
    findings = [*template_findings(template), *(f for c in comparisons for f in c.findings)]
    return CheckReport(
        document_id=template.name,
        findings=findings,
        summary={c.bidder: f"{len(c.findings)} finding(s)" for c in comparisons},
    )


# --- unit rates ---------------------------------------------------------------------------


def rate_outliers(template: BoqWorkbook, comparisons: list[BidComparison]) -> list[Finding]:
    """Flag a unit rate that stands far apart from what the other bidders quoted for the line.

    Like-for-like comparison of the bottom line is misleading when one bidder prices a position
    an order of magnitude differently; this is what makes the difference visible per position
    rather than only in the total.
    """
    # Keyed by the template position (sheet, row), which is unique. The item-and-description
    # key is not: a template that repeats a position ("Excavation" under two sections) pooled
    # both positions' rates into one median.
    by_line: dict[tuple[str, int], list[tuple[str, BoqLine]]] = {}
    for comp in comparisons:
        for t_line, b_line in comp.matched:
            if b_line.rate_total:
                by_line.setdefault((t_line.sheet, t_line.row), []).append((comp.bidder, b_line))

    out: list[Finding] = []
    for entries in by_line.values():
        if len(entries) < 3:
            continue
        rates = sorted(line.rate_total or 0.0 for _, line in entries)
        median = rates[len(rates) // 2]
        if median <= 0:
            continue
        for bidder, line in entries:
            rate = line.rate_total or 0.0
            if rate > median * _RATE_OUTLIER_FACTOR or rate * _RATE_OUTLIER_FACTOR < median:
                direction_ka = "მაღალია" if rate > median else "დაბალია"
                direction_en = "above" if rate > median else "below"
                out.append(
                    _finding(
                        "BOQ.RATE_OUTLIER",
                        "warning",
                        f"{bidder}: {line.sheet}, სტრიქონი {line.row} — ერთეულის ფასი "
                        f"{rate:,.2f} მნიშვნელოვნად {direction_ka} მედიანაზე ({median:,.2f})",
                        f"{bidder}: {line.sheet} row {line.row} - unit rate {rate:,.2f} is far "
                        f"{direction_en} the median ({median:,.2f})",
                        expected=f"{median:,.2f}",
                        actual=f"{rate:,.2f}",
                        confidence=0.7,
                    )
                )
    return out


# --- merged workbook ----------------------------------------------------------------------

_HIGH = PatternFill("solid", fgColor="F4CCCC")
_LOW = PatternFill("solid", fgColor="D9EAD3")
_TEMPLATE_ROW = PatternFill("solid", fgColor="EFEFEF")
_EXCLUDED = PatternFill("solid", fgColor="FFF2CC")
_HEAD = PatternFill("solid", fgColor="D0D7E5")
_BOLD = Font(bold=True)

_MERGED_HEADERS = (
    ("Item", 10),
    ("Description / აღწერა", 52),
    ("Unit", 8),
    ("Template qty", 12),
    ("Bidder", 14),
    ("Qty", 10),
    ("Δ qty", 9),
    ("Materials", 11),
    ("Labor", 11),
    ("Delivery", 11),
    ("Unit rate", 12),
    ("Amount", 14),
    ("Evaluated (template qty)", 16),
    ("Analysis / ანალიზი", 34),
)

#: How each position status reads in the merged workbook's "Analysis" column.
_STATUS_TEXT = {
    "priced": "evaluated",
    "filled_blank": "evaluated at the bidder's amount (template qty blank)",
    "unpriced": "EXCLUDED: not priced",
    "not_filled": "left blank (template qty blank)",
    "non_numeric": "EXCLUDED: text in a number cell",
    "removed": "EXCLUDED: removed by the bidder",
    "sheet_missing": "EXCLUDED: sheet missing",
    "sheet_unreadable": "EXCLUDED: sheet unreadable",
    "optional": "optional (placeholder for other items)",
    "optional_priced": "EXCLUDED: the bidder's own additional items",
}


def _put(ws: object, row: int, col: int, value: object) -> None:
    """Write one cell of the merged workbook. Every value goes through here.

    Descriptions, units, item numbers, sheet names, Summary labels and finding messages all
    come from a workbook the bidder controls; see ``report/xlsx_safe`` for what an unescaped
    ``=`` in one of them would do in the file the buyer opens.
    """
    ws.cell(row, col, cell_safe(value))  # type: ignore[attr-defined]


def _sheet_title(name: str) -> str:
    """A template sheet name as a worksheet title: control characters out, 31 characters."""
    title = str(cell_safe(name)).removeprefix("'")[:31]
    return title or "Sheet"


def _style_header(ws: object, row: int = 1) -> None:
    for col, (title, width) in enumerate(_MERGED_HEADERS, start=1):
        cell = ws.cell(row, col, title)  # type: ignore[attr-defined]
        cell.font = _BOLD
        cell.fill = _HEAD
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        ws.column_dimensions[get_column_letter(col)].width = width  # type: ignore[attr-defined]
    ws.freeze_panes = ws.cell(row + 1, 1)  # type: ignore[attr-defined]


def _colour_extremes(ws: object, rows: list[int], col: int) -> None:
    """Highest red, lowest green - every tied cell the same colour, none when all are equal."""
    values = [(ws.cell(r, col).value, r) for r in rows]  # type: ignore[attr-defined]
    numeric = [(round(v, 2), r) for v, r in values if isinstance(v, int | float) and v]
    if len(numeric) < 2:
        return
    high, low = max(v for v, _ in numeric), min(v for v, _ in numeric)
    if high == low:
        return
    for v, r in numeric:
        if v == high:
            ws.cell(r, col).fill = _HIGH  # type: ignore[attr-defined]
        elif v == low:
            ws.cell(r, col).fill = _LOW  # type: ignore[attr-defined]


def write_merged_workbook(
    template: BoqWorkbook,
    bids: list[BoqWorkbook],
    comparisons: list[BidComparison],
    out_path: Path,
    *,
    outliers: list[Finding] | None = None,
) -> Path:
    """All bidders stacked vertically under every template line, plus Summary and reports.

    A new workbook rather than an edited copy of the template on purpose: inserting rows into
    the template would shift the ranges its own SUM formulas point at, which openpyxl does not
    rewrite, so the file would open with quietly wrong totals.
    """
    wb = Workbook()
    default_sheet = wb.active
    if default_sheet is not None:
        wb.remove(default_sheet)

    # ``comparisons`` is ``compare_all(template, bids)``: one per bid, in the same order, so they
    # are paired by position - two uploads with the same file name are still two bidders.
    if len(comparisons) != len(bids):
        msg = (
            f"{len(bids)} bids but {len(comparisons)} comparisons; pass compare_all(template, bids)"
        )
        raise ValueError(msg)

    _write_summary_sheet(wb, template, bids, comparisons)

    # Keyed by the template position, not by item-and-description: a template that repeats a
    # position collapsed both onto the last bidder line, and the first showed the second's price.
    matched_by_bidder: list[dict[tuple[str, int], BoqLine]] = [
        {(t.sheet, t.row): b for t, b in c.matched} for c in comparisons
    ]
    evals_by_bidder: list[dict[tuple[str, int], PositionEval]] = [
        {p.ref: p for p in c.positions} for c in comparisons
    ]

    for sheet_name, t_sheet in template.sheets.items():
        ws = wb.create_sheet(_sheet_title(sheet_name))
        _style_header(ws)
        row = 2
        for t_line in t_sheet.lines:
            _put(ws, row, 1, t_line.item_no)
            _put(ws, row, 2, t_line.description[:250])
            _put(ws, row, 3, t_line.unit)
            _put(ws, row, 4, t_line.quantity)
            _put(ws, row, 5, "— template —")
            _put(ws, row, 14, t_line.kind)
            for col in range(1, len(_MERGED_HEADERS) + 1):
                ws.cell(row, col).fill = _TEMPLATE_ROW
            row += 1

            bidder_rows: list[int] = []
            ref = (t_line.sheet, t_line.row)
            for bid, matched, evals in zip(bids, matched_by_bidder, evals_by_bidder, strict=True):
                line = matched.get(ref)
                ev = evals.get(ref)
                _put(ws, row, 5, bid.name)
                if line is not None:
                    delta = None
                    if line.quantity is not None and t_line.quantity is not None:
                        delta = round(line.quantity - t_line.quantity, 6) or None
                    _put(ws, row, 6, line.quantity)
                    _put(ws, row, 7, delta)
                    _put(ws, row, 8, line.rate_materials)
                    _put(ws, row, 9, line.rate_labor)
                    _put(ws, row, 10, line.rate_delivery)
                    _put(ws, row, 11, line.rate_total)
                    _put(ws, row, 12, line.amount)
                    if delta and t_line.kind == "position" and t_line.quantity:
                        ws.cell(row, 7).fill = _HIGH
                    if t_line.kind != "position" and line.kind == "position" and line.amount:
                        _put(ws, row, 14, "EXCLUDED: priced where the template has no position")
                        ws.cell(row, 14).fill = _EXCLUDED
                else:
                    _put(ws, row, 6, "— missing —")
                    ws.cell(row, 6).fill = _HIGH
                if ev is not None:
                    _put(ws, row, 13, round(ev.cost, 2))
                    text = _STATUS_TEXT.get(ev.status, ev.status)
                    if ev.priced:
                        text += "; common scope" if ev.in_common_scope else "; outside common scope"
                    _put(ws, row, 14, text)
                    if not ev.priced:
                        ws.cell(row, 14).fill = _EXCLUDED
                bidder_rows.append(row)
                row += 1
            # amounts are comparable on every row that carries one, including the section
            # subtotals; unit rates only mean something on a line with a quantity
            _colour_extremes(ws, bidder_rows, 12)
            if t_line.quantity:
                _colour_extremes(ws, bidder_rows, 11)
        row = _write_added_block(ws, row + 1, sheet_name, comparisons)

    _write_report_sheets(wb, template, comparisons, outliers or [])
    wb.save(out_path)
    return out_path


def _write_added_block(ws: object, row: int, sheet_name: str, comps: list[BidComparison]) -> int:
    """Lines bidders added on this sheet: shown in the merged sheet, kept out of the analysis."""
    added = [(c.bidder, line) for c in comps for line in c.added if line.sheet == sheet_name]
    if not added:
        return row
    _put(ws, row, 2, "Added by bidders - excluded from the analysis / დამატებული, ანალიზის გარეშე")
    ws.cell(row, 2).font = _BOLD  # type: ignore[attr-defined]
    row += 1
    for bidder, line in added:
        for col, value in (
            (1, line.item_no),
            (2, line.description[:250]),
            (3, line.unit),
            (5, bidder),
            (6, line.quantity),
            (11, line.rate_total),
            (12, line.amount),
            (14, "EXCLUDED: added by the bidder"),
        ):
            _put(ws, row, col, value)
        ws.cell(row, 14).fill = _EXCLUDED  # type: ignore[attr-defined]
        row += 1
    return row


def _aligned_summary(template: BoqWorkbook, bid: BoqWorkbook) -> dict[int, SummaryRow]:
    """The bid's Summary rows matched to the template's (template row -> bid row).

    Aligned as sequences of (item, label), not looked up by label: one group label is a
    different row under every section, and a shifted Summary is still the same Summary.
    """
    t_rows, b_rows = template.summary_rows, bid.summary_rows
    matcher = difflib.SequenceMatcher(
        None, [r.key for r in t_rows], [r.key for r in b_rows], autojunk=False
    )
    out: dict[int, SummaryRow] = {}
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1):
            for t_row, b_row in zip(t_rows[i1:i2], b_rows[j1:j2], strict=True):
                out[t_row.row] = b_row
    return out


def _write_summary_sheet(
    wb: Workbook, template: BoqWorkbook, bids: list[BoqWorkbook], comps: list[BidComparison]
) -> None:
    ws = wb.create_sheet("SUMMARY")
    ws.cell(1, 1, "Section / სექცია").font = _BOLD
    ws.cell(1, 2, "Bidder / კომპანია").font = _BOLD
    ws.cell(1, 3, "Stated, USD").font = _BOLD
    for col, width in ((1, 46), (2, 18), (3, 16), (4, 16), (5, 16), (6, 16), (7, 16), (8, 60)):
        ws.column_dimensions[get_column_letter(col)].width = width
    for cell in ws[1]:
        cell.fill = _HEAD
    ws.freeze_panes = ws.cell(2, 1)

    row = 2
    if template.summary_rows:
        aligned = [_aligned_summary(template, bid) for bid in bids]
        for t_row in template.summary_rows:
            label = f"{t_row.item} {t_row.label}".strip()
            for i, (bid, rows) in enumerate(zip(bids, aligned, strict=True)):
                b_row = rows.get(t_row.row)
                _put(ws, row, 1, label if i == 0 else None)
                _put(ws, row, 2, bid.name)
                _put(ws, row, 3, b_row.value if b_row is not None else None)
                row += 1
            row += 1
    else:
        # hand-built books with a Summary mapping only
        labels: list[str] = []
        for bid in bids:
            labels.extend(k for k in bid.summary if k not in labels)
        for label in labels:
            for i, bid in enumerate(bids):
                _put(ws, row, 1, label if i == 0 else None)
                _put(ws, row, 2, bid.name)
                _put(ws, row, 3, bid.summary.get(label))
                row += 1
            row += 1

    row = _write_totals_block(ws, row + 1, comps)
    _write_excluded_block(ws, row + 1, comps)


def _write_totals_block(ws: object, row: int, comps: list[BidComparison]) -> int:
    """Per bidder: stated, evaluated and common-scope totals; red/green on the common scope."""
    positions = next((c.common_scope_positions for c in comps if c.evaluable), 0)
    _put(
        ws,
        row,
        1,
        "Evaluation at template quantities (GOLD_SPEC 5) / შეფასება შაბლონის მოცულობებით",
    )
    ws.cell(row, 1).font = _BOLD  # type: ignore[attr-defined]
    row += 1
    _put(
        ws,
        row,
        1,
        f"Colours: common-scope total, i.e. the {positions} position(s) every bidder priced; "
        "highest red, lowest green",
    )
    row += 1
    headers = (
        "",
        "Bidder / კომპანია",
        "Stated incl. VAT",
        "Markup",
        "Evaluated total",
        "Common-scope total",
        "Stated - evaluated",
        "Why / განმარტება",
    )
    for col, title in enumerate(headers, 1):
        _put(ws, row, col, title or None)
        ws.cell(row, col).font = _BOLD  # type: ignore[attr-defined]
        ws.cell(row, col).fill = _HEAD  # type: ignore[attr-defined]
    row += 1
    for comp in comps:
        _put(ws, row, 2, comp.bidder)
        _put(ws, row, 3, comp.stated_incl_vat)
        _put(ws, row, 4, round(comp.markup_factor, 6) if comp.markup_factor else None)
        _put(
            ws, row, 5, round(comp.evaluated_total, 2) if comp.evaluated_total is not None else None
        )
        total = comp.common_scope_total
        _put(ws, row, 6, round(total, 2) if total is not None else None)
        if comp.colour == "red":
            ws.cell(row, 6).fill = _HIGH  # type: ignore[attr-defined]
        elif comp.colour == "green":
            ws.cell(row, 6).fill = _LOW  # type: ignore[attr-defined]
        if comp.stated_incl_vat is not None and comp.evaluated_total is not None:
            _put(ws, row, 7, round(comp.stated_incl_vat - comp.evaluated_total, 2) or 0.0)
        _put(ws, row, 8, _explain(comp))
        row += 1
    return row


def _explain(comp: BidComparison) -> str:
    if not comp.evaluable:
        return "not evaluated: the workbook could not be read"
    if comp.markup_factor is None:
        return "no total: the Summary states no TOTAL COST or no total incl. VAT"
    names = {
        "quantities": "own quantities / arithmetic",
        "added_lines": "added lines",
        "optional_items": "optional items (placeholders)",
        "priced_headings": "priced non-positions",
        "added_sheets": "added sheets",
        "summary": "Summary vs sheets",
        "other": "other",
    }
    parts = [f"{names.get(k, k)} {v:,.2f}" for k, v in comp.difference.items()]
    return "; ".join(parts) or "no difference"


def _write_excluded_block(ws: object, row: int, comps: list[BidComparison]) -> int:
    """Every position left out of a bidder's analysis, with the reason (client rule 3)."""
    _put(ws, row, 1, "Excluded from the analysis / ანალიზიდან გამორიცხული")
    ws.cell(row, 1).font = _BOLD  # type: ignore[attr-defined]
    row += 1
    for col, title in enumerate(("Kind", "Bidder", "Sheet", "Row", "Item", "Description"), 1):
        _put(ws, row, col, title)
        ws.cell(row, col).font = _BOLD  # type: ignore[attr-defined]
        ws.cell(row, col).fill = _HEAD  # type: ignore[attr-defined]
    row += 1
    for comp in comps:
        entries: list[tuple[str, str, int, str, str]] = []
        entries.extend(("added line", b.sheet, b.row, b.item_no, b.description) for b in comp.added)
        entries.extend(
            ("priced non-position", b.sheet, b.row, b.item_no, b.description)
            for b in comp.priced_headings
        )
        entries.extend(
            (_STATUS_TEXT.get(p.status, p.status), p.sheet, p.row, p.item_no, p.description)
            for p in comp.positions
            if p.status
            in ("removed", "unpriced", "non_numeric", "sheet_missing", "sheet_unreadable")
        )
        for kind, sheet, line_row, item, desc in entries:
            for col, value in enumerate((kind, comp.bidder, sheet, line_row, item, desc[:200]), 1):
                _put(ws, row, col, value)
            row += 1
    return row


def _write_report_sheets(
    wb: Workbook, template: BoqWorkbook, comparisons: list[BidComparison], outliers: list[Finding]
) -> None:
    ws = wb.create_sheet("Quantity differences")
    for col, title in enumerate(
        ("Bidder", "Sheet", "Row", "Description", "Template qty", "Bid qty", "Kind", "Amount"), 1
    ):
        ws.cell(1, col, title).font = _BOLD
        ws.cell(1, col).fill = _HEAD
    ws.column_dimensions["D"].width = 60
    row = 2
    for comp in comparisons:
        for kind, diffs in (
            ("quantity changed", comp.quantity_changed),
            ("filled (template blank)", comp.filled_blank),
        ):
            for d in diffs:
                for col, value in enumerate(
                    (
                        comp.bidder,
                        d.sheet,
                        d.row,
                        d.description[:200],
                        d.template_qty,
                        d.bid_qty,
                        kind,
                        d.amount,
                    ),
                    1,
                ):
                    _put(ws, row, col, value)
                row += 1

    ws = wb.create_sheet("Added and removed")
    for col, title in enumerate(("Bidder", "Kind", "Sheet", "Row", "Description"), 1):
        ws.cell(1, col, title).font = _BOLD
        ws.cell(1, col).fill = _HEAD
    ws.column_dimensions["E"].width = 70
    row = 2
    for comp in comparisons:
        for sheet in comp.added_sheets:
            for col, value in enumerate((comp.bidder, "added sheet", sheet, None, None), 1):
                _put(ws, row, col, value)
            row += 1
        for sheet in comp.removed_sheets:
            for col, value in enumerate((comp.bidder, "removed sheet", sheet, None, None), 1):
                _put(ws, row, col, value)
            row += 1
        for kind, lines in (("added line", comp.added), ("removed line", comp.removed)):
            for line in lines:
                cells: tuple[str | int | None, ...] = (
                    comp.bidder,
                    kind,
                    line.sheet,
                    line.row,
                    line.description[:200],
                )
                for col, value in enumerate(cells, 1):
                    _put(ws, row, col, value)
                row += 1

    ws = wb.create_sheet("Findings")
    for col, title in enumerate(("Bidder", "Severity", "Rule", "Message (EN)", "შეტყობინება"), 1):
        ws.cell(1, col, title).font = _BOLD
        ws.cell(1, col).fill = _HEAD
    for col, width in ((4, 80), (5, 80)):
        ws.column_dimensions[get_column_letter(col)].width = width
    row = 2
    rows: list[tuple[str, Finding]] = [("(template)", f) for f in template_findings(template)]
    rows.extend((c.bidder, f) for c in comparisons for f in c.findings)
    # the outlier message names its bidder; the row is "all bidders" because the finding is
    # about how the quotes compare, not about one workbook
    rows.extend(("(all bidders)", f) for f in outliers)
    for bidder, f in rows:
        for col, value in enumerate((bidder, f.severity, f.rule_id, f.message_en, f.message_ka), 1):
            _put(ws, row, col, value)
        if f.severity == "error":
            ws.cell(row, 2).fill = _HIGH
        row += 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare bidder BOQ workbooks to the template.")
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--bids", type=Path, nargs="+", required=True)
    parser.add_argument("--out", type=Path, default=Path("boq_merged.xlsx"))
    args = parser.parse_args(argv)

    template = read_boq(args.template, name="TEMPLATE")
    bids = [read_bid(p, name=p.stem) for p in args.bids]
    comparisons = compare_all(template, bids)
    # Outliers compare the bidders with each other, so they belong to no one bidder. Filing
    # them under the first one (as this once did) charged that bidder with everyone's prices.
    outliers = rate_outliers(template, comparisons)

    for f in template_findings(template):
        print(f"template: {f.severity} {f.rule_id} {f.message_en}")
    for comp in comparisons:
        errors = sum(1 for f in comp.findings if f.severity == "error")
        total = comp.common_scope_total
        print(
            f"{comp.bidder}: {len(comp.findings)} finding(s), {errors} error(s); "
            f"common-scope total {'-' if total is None else f'{total:,.2f}'}"
            f"{' (' + comp.colour + ')' if comp.colour else ''}"
        )
    print(f"unit-rate outliers across bidders: {len(outliers)}")
    path = write_merged_workbook(template, bids, comparisons, args.out, outliers=outliers)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
