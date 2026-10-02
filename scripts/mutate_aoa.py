#!/usr/bin/env python3
"""AOA + compare mutation harness (docs/GOLD_SPEC.md sections 3 and 4).

For every base case (``samples/*/AOA and Contract/``: the act named "*Correct*" and its
``*ontract*.docx``) the harness plants one error class at DOCX level with python-docx, parses
the mutated act with Docling, and scores

* ``check_aoa(mutated act, base contract)`` against the class's expected findings (rule, the
  offending text, the correction in the act's own form, the number of places a Word comment
  must mark) and against the base act's own findings (anything more is a false positive), and
* ``compare(base act, mutated act)``: one substantive difference with the right label and
  before/after for a planted error, nothing substantive for a harmless change.

Every value that is mutated is discovered from the base documents at runtime; nothing about the
client's documents is written in this file. Mutated documents and per-case details carry client
values, so they go under ``samples/mutations/aoa/`` (git-ignored). The JSON written to
``eval_runs/`` holds aggregates only.

Each mutation is validated before it is scored: the substitutions must land exactly where they
were planned (the mutated paragraph texts are recomputed with plain string operations and must
match), the new text must survive the Docling parse, and each expected text must occur in the
mutated act exactly ``occurrences`` times. A case that fails validation is reported as invalid
and left out of the percentages - it is a harness defect, not a product miss.

    uv run --offline python scripts/mutate_aoa.py --calibrate
    uv run --offline python scripts/mutate_aoa.py --seed 1 --per-class 8
    uv run --offline python scripts/mutate_aoa.py --classes director,fee_both --show
"""

# Georgian quotes and planted Latin/Cyrillic letters are the point here; tables print wide.
# ruff: noqa: E501, RUF001

from __future__ import annotations

import argparse
import calendar
import copy
import difflib
import hashlib
import json
import random
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
import zipfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from docx import Document  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402
from lxml import etree  # noqa: E402

from procurement_agent.checks.aoa import check_aoa  # noqa: E402
from procurement_agent.checks.aoa_spelling import Speller, load_speller, open_speller  # noqa: E402
from procurement_agent.config import Settings  # noqa: E402
from procurement_agent.extraction import georgian as g  # noqa: E402
from procurement_agent.extraction.labels import LABELS  # noqa: E402
from procurement_agent.extraction.pipeline import chunks_text  # noqa: E402
from procurement_agent.extraction.schemas import FactType  # noqa: E402
from procurement_agent.ingestion.chunker import chunk_docling_document  # noqa: E402
from procurement_agent.ingestion.docling_service import convert  # noqa: E402
from procurement_agent.ingestion.segmenter import segments_from_docling  # noqa: E402
from procurement_agent.report.docx_annotate import annotate_docx  # noqa: E402

XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
KA = "ა-ჰ"

# --------------------------------------------------------------------------------------------
# fold (GOLD_SPEC section 1)
# --------------------------------------------------------------------------------------------

_QUOTES = set("„“”\"'‘’«»‚")
_DASHES = {"–": "-", "—": "-", "‑": "-", "−": "-"}


def _fold_chars(s: str) -> tuple[str, list[int]]:
    """fold() without the final trim, plus the index in ``s`` of every folded character."""
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(s):
        if ch in _QUOTES:
            continue
        ch = _DASHES.get(ch, ch)
        if ch.isspace():
            if out and out[-1] == " ":
                continue
            ch = " "
        elif "a" <= ch <= "z":
            ch = ch.upper()
        out.append(ch)
        idx.append(i)
    return "".join(out), idx


def fold(s: str | None) -> str:
    return _fold_chars(unicodedata.normalize("NFC", s or ""))[0].strip()


# --------------------------------------------------------------------------------------------
# DOCX primitives
# --------------------------------------------------------------------------------------------


def paragraphs(doc: Any) -> list[Any]:
    return list(doc.element.body.iter(qn("w:p")))


def ptext(p: Any) -> str:
    return "".join(t.text or "" for t in p.iter(qn("w:t")))


def replace_range(p: Any, i: int, j: int, new: str) -> bool:
    """Replace characters [i, j) of the paragraph's text across its w:t nodes.

    The replacement goes into the first touched node (keeping that run's format), the rest of
    the range is cut out of the following nodes. An insertion (i == j) goes into the node that
    holds position i (or ends at it).
    """
    ts = list(p.iter(qn("w:t")))
    pos = 0
    if i == j:
        for t in ts:
            s = t.text or ""
            a, b = pos, pos + len(s)
            pos = b
            if a <= i <= b:
                t.text = s[: i - a] + new + s[i - a :]
                t.set(XML_SPACE, "preserve")
                return True
        return False
    first = True
    for t in ts:
        s = t.text or ""
        a, b = pos, pos + len(s)
        pos = b
        if b <= i or a >= j:
            continue
        lo, hi = max(i, a) - a, min(j, b) - a
        t.text = s[:lo] + (new if first else "") + s[hi:]
        t.set(XML_SPACE, "preserve")
        first = False
    return not first


def set_text_same_length(p: Any, new: str) -> None:
    """Rewrite a paragraph's text with a string of the same length, node by node."""
    pos = 0
    for t in p.iter(qn("w:t")):
        s = t.text or ""
        t.text = new[pos : pos + len(s)]
        t.set(XML_SPACE, "preserve")
        pos += len(s)


def set_text(p: Any, text: str) -> None:
    """Replace a paragraph's runs by a single run (the first run's format) holding ``text``."""
    runs = [r for r in p if r.tag == qn("w:r")]
    rpr = None
    for r in runs:
        if r.find(qn("w:t")) is not None:
            rpr = r.find(qn("w:rPr"))
            break
    for child in list(p):
        if child.tag in (qn("w:r"), qn("w:hyperlink"), qn("w:ins"), qn("w:smartTag")):
            p.remove(child)
    r = etree.SubElement(p, qn("w:r"))
    if rpr is not None:
        r.append(copy.deepcopy(rpr))
    t = etree.SubElement(r, qn("w:t"))
    t.text = text
    t.set(XML_SPACE, "preserve")


def split_run_at(p: Any, offset: int) -> bool:
    """Split the run holding ``offset`` (strictly inside it) into two runs of equal format."""
    pos = 0
    for t in p.iter(qn("w:t")):
        s = t.text or ""
        a, b = pos, pos + len(s)
        pos = b
        if a < offset < b:
            r = t.getparent()
            if r.tag != qn("w:r") or [c.tag for c in r if c.tag != qn("w:rPr")] != [qn("w:t")]:
                return False
            r2 = copy.deepcopy(r)
            t.text = s[: offset - a]
            t.set(XML_SPACE, "preserve")
            t2 = r2.find(qn("w:t"))
            t2.text = s[offset - a :]
            t2.set(XML_SPACE, "preserve")
            r.addnext(r2)
            return True
        if offset in (a, b) and a < b:
            return True  # already a boundary
    return False


def runs_in(p: Any, i: int, j: int) -> list[Any]:
    out, pos = [], 0
    for t in p.iter(qn("w:t")):
        s = t.text or ""
        a, b = pos, pos + len(s)
        pos = b
        if a >= i and b <= j and a < b:
            out.append(t.getparent())
    return out


def make_bold(r: Any) -> None:
    rpr = r.find(qn("w:rPr"))
    if rpr is None:
        rpr = etree.Element(qn("w:rPr"))
        r.insert(0, rpr)
    if rpr.find(qn("w:b")) is None:
        rpr.insert(0, etree.Element(qn("w:b")))


def delete_to_end(p: Any, i: int) -> None:
    n = len(ptext(p))
    if i < n:
        replace_range(p, i, n, "")


# --------------------------------------------------------------------------------------------
# edits (text-level mutations)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Edit:
    pi: int  # paragraph index in body order
    i: int
    j: int
    new: str


def apply_edits_docx(paras: list[Any], edits: list[Edit]) -> int:
    done = 0
    for e in sorted(edits, key=lambda e: (e.pi, e.i), reverse=True):
        done += replace_range(paras[e.pi], e.i, e.j, e.new)
    return done


def apply_edits_text(texts: list[str], edits: list[Edit]) -> list[str]:
    out = list(texts)
    for e in sorted(edits, key=lambda e: (e.pi, e.i), reverse=True):
        s = out[e.pi]
        out[e.pi] = s[: e.i] + e.new + s[e.j :]
    return out


def find_all(
    texts: list[str], needle: str, *, boundary: str | None = None
) -> list[tuple[int, int, int]]:
    """(paragraph, start, end) of every occurrence; ``boundary`` is a class that must not touch."""
    out = []
    pat = re.escape(needle)
    if boundary:
        pat = f"(?<![{boundary}]){pat}(?![{boundary}])"
    for pi, s in enumerate(texts):
        for m in re.finditer(pat, s):
            out.append((pi, m.start(), m.end()))
    return out


# --------------------------------------------------------------------------------------------
# discovery of the base act's values
# --------------------------------------------------------------------------------------------

QO = r"(?:,,|[„“\"«”])"
QC = r"[“”\"»]"
DATE_KA = re.compile(rf"(\d{{4}})\s+წლის\s+(\d{{1,2}})\s+([{KA}]+)")
DATE_DOT = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})\.(\d{4})(?!\d)")
FEE_RE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[  ,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
    rf"\s*\(([^()]+)\)\s*(ლარ[{KA}]*|GEL|ევრო|აშშ დოლარ[{KA}]*)"
)
MONTHS: dict[str, tuple[int, str]] = dict(g.MONTH_FORMS)


def amount_value(digits: str) -> float | None:
    s = digits.replace(" ", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = s.replace(",", "") if len(tail) == 3 else head.replace(",", "") + "." + tail
    try:
        return float(s)
    except ValueError:
        return None


def format_like(v: int, example: str) -> str:
    """``v`` written the way ``example`` writes an amount (thousands separator, decimals)."""
    m = re.fullmatch(r"(\d{1,3}(?:([  ,.])\d{3})*|\d+)(?:([.,])(\d{1,2}))?", example)
    sep = m.group(2) if m else None
    dec = m.group(3) if m else None
    decs = m.group(4) if m else None
    body = str(v)
    if sep:
        body = f"{v:,}".replace(",", sep)
    if dec:
        body += dec + "0" * len(decs or "00")
    return body


@dataclass
class Span:
    pi: int
    i: int
    j: int
    text: str


@dataclass
class DateSpan(Span):
    d: date
    style: str  # "ka" | "dot"
    case: str | None = None


def dates_in(texts: list[str], pi: int) -> list[DateSpan]:
    s = texts[pi]
    out: list[DateSpan] = []
    for m in DATE_KA.finditer(s):
        mw = m.group(3)
        if mw not in MONTHS:
            continue
        month, case = MONTHS[mw]
        try:
            d = date(int(m.group(1)), month, int(m.group(2)))
        except ValueError:
            continue
        out.append(DateSpan(pi, m.start(), m.end(), m.group(0), d, "ka", case))
    for m in DATE_DOT.finditer(s):
        try:
            d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            continue
        out.append(DateSpan(pi, m.start(), m.end(), m.group(0), d, "dot"))
    return sorted(out, key=lambda x: x.i)


def render_like(d: date, example: DateSpan) -> str:
    if example.style == "dot":
        m = DATE_DOT.fullmatch(example.text)
        day = f"{d.day:02d}" if m and len(m.group(1)) == 2 else str(d.day)
        mon = f"{d.month:02d}" if m and len(m.group(2)) == 2 else str(d.month)
        return f"{day}.{mon}.{d.year}"
    return f"{d.year} წლის {d.day} {g.month_form(d.month, example.case or 'nom')}"


@dataclass
class Facts:
    ids: list[str] = field(default_factory=list)
    iban: str | None = None
    bic: str | None = None
    cadastral: str | None = None
    fee: dict[str, Any] | None = None  # digits, words, currency, value, spans
    act_date: DateSpan | None = None
    period: tuple[DateSpan, DateSpan] | None = None
    contract_date: DateSpan | None = None
    directors: list[dict[str, Any]] = field(default_factory=list)
    party_names: list[tuple[str, str]] = field(default_factory=list)  # (legal form, core)
    addresses: list[str] = field(default_factory=list)
    project_forms: list[Span] = field(default_factory=list)
    roles: list[dict[str, Any]] = field(default_factory=list)
    month_words: list[Span] = field(default_factory=list)


def discover(texts: list[str]) -> Facts:
    f = Facts()
    full = "\n".join(texts)
    for m in re.finditer(r"საიდენტიფიკაციო\s+კოდი\s*:?\s*(\d{9}(?:\d{2})?)(?!\d)", full):
        if m.group(1) not in f.ids:
            f.ids.append(m.group(1))
    if m := re.search(r"(?<![A-Z0-9])GE\d{2}[A-Z]{2}\d{16}(?![A-Z0-9])", full):
        f.iban = m.group(0)
    if m := re.search(r"ბანკის\s+კოდი\s*:?\s*([A-Z0-9]{6,11})(?![A-Z0-9])", full):
        f.bic = m.group(1)
    if m := re.search(r"(?<![\d.])(\d{2}\.\d{2}\.\d{2,3}\.\d{2,4}(?:\.\d{1,4})?)(?![\d])", full):
        f.cadastral = m.group(1)
    for pi, s in enumerate(texts):
        for m in FEE_RE.finditer(s):
            v = amount_value(m.group(1))
            if v is None or v != int(v):
                continue
            if g.parse_number_words(m.group(2)) == int(v):
                f.fee = {
                    "pi": pi,
                    "digits": m.group(1),
                    "words": m.group(2),
                    "currency": m.group(3),
                    "value": int(v),
                    "d": (m.start(1), m.end(1)),
                    "w": (m.start(2), m.end(2)),
                    "c": (m.start(3), m.end(3)),
                    "all": (m.start(1), m.end(3)),
                }
                break
        if f.fee:
            break
    for pi, s in enumerate(texts):
        ds = dates_in(texts, pi)
        if not ds:
            continue
        if "გაფორმდა" in s and "ხელშეკრულება" not in s:
            f.act_date = ds[-1]
        elif "ჩათვლით" in s and len(ds) >= 2 and f.period is None:
            f.period = (ds[0], ds[1])
        elif ("გააფორმეს" in s or "ხელშეკრულებ" in s) and f.contract_date is None:
            f.contract_date = ds[0]
        for dsp in ds:
            if dsp.style == "ka":
                mw = DATE_KA.fullmatch(dsp.text).group(3)
                k = dsp.i + dsp.text.rindex(mw)
                f.month_words.append(Span(pi, k, k + len(mw), mw))
    # directors: first name + surname; the nominative is the form after "დირექტორი"
    seen: dict[str, dict[str, Any]] = {}
    for m in re.finditer(rf"დირექტორ(ი|ის)?\s*:?\s*([{KA}]{{2,}})\s+([{KA}]{{2,}})", full):
        first, sur = m.group(2), m.group(3)
        d = seen.setdefault(first, {"first": first, "nom": None, "any": sur})
        if m.group(1) == "ი" or m.group(1) is None:
            d["nom"] = d["nom"] or sur
    for first, d in seen.items():
        nom = d["nom"] or d["any"]
        stem = nom[:-1] if nom[-1] in "ეი" else nom
        forms = Counter()
        spans = []
        for pi, s in enumerate(texts):
            for m in re.finditer(rf"(?<![{KA}]){re.escape(first)}\s+({re.escape(stem)}[{KA}]*)", s):
                forms[m.group(1)] += 1
                spans.append(Span(pi, m.start(1), m.end(1), m.group(1)))
        f.directors.append(
            {"first": first, "nom": nom, "stem": stem, "forms": forms, "spans": spans}
        )
    for m in re.finditer(rf"(შპს|სს|ააიპ|ი/მ)\s*{QO}([^„“”\"«»\n]{{2,80}}?){QC}", full):
        core = m.group(2).strip()
        if "ბანკ" in core or any(core == c for _, c in f.party_names):
            continue
        f.party_names.append((m.group(1), core))
    for m in re.finditer(r"იურიდიული\s+მისამართი\s*:?\s*(.+?)[\s,;]*\(შემდგომში", full):
        f.addresses.append(m.group(1).strip())
    seen_pos = set()
    for pi, s in enumerate(texts):
        pats = [
            rf"პროექტ[{KA}]*\s+{QO}([^„“”\"«»]{{2,80}}?){QC}",
            rf"{QO}([^„“”\"«»]{{2,80}}?){QC}\s*(?:\(ს/კ|პროექტ)",
        ]
        for pat in pats:
            for m in re.finditer(pat, s):
                if re.match(r"(შემკვეთ|შემსრულებ|მხარე|ხელშეკრულებ|მომსახურებ|აქტ)", m.group(1)):
                    continue  # a defined term in quotes, not the project
                if (pi, m.start(1)) not in seen_pos:
                    seen_pos.add((pi, m.start(1)))
                    f.project_forms.append(Span(pi, m.start(1), m.end(1), m.group(1)))
    f.project_forms.sort(key=lambda x: (x.pi, x.i))
    for pi, s in enumerate(texts):
        if not re.search(r"საიდენტიფიკაციო\s+კოდი", s):
            continue
        for m in re.finditer(rf"შემდგომში\s*-?\s*{QO}(შემკვეთი|შემსრულებელი){QC}", s):
            f.roles.append(
                {
                    "pi": pi,
                    "frag": (m.start(), m.end()),
                    "word": (m.start(1), m.end(1)),
                    "role": m.group(1),
                }
            )
    return f


# --------------------------------------------------------------------------------------------
# cases and expectations
# --------------------------------------------------------------------------------------------


@dataclass
class Expect:
    rules: tuple[str, ...]
    text: str | None  # the offending words as the mutated act writes them
    corrections: tuple[str, ...] = ()  # any of these (folded) is right; () = not scored
    occurrences: int | None = None


@dataclass
class CmpExpect:
    mode: str  # "one" | "none" | "text_only"
    rules: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    before: tuple[str, ...] = ()
    after: tuple[str, ...] = ()  # () with mode "one" and removed=True means "nothing after"
    removed: bool = False
    max_count: int = 1  # mode "one": up to this many findings, each matching
    extra_ok: tuple[str, ...] = ()  # text_only: typed rules that may appear too


@dataclass
class Case:
    cls: str
    variant: str
    base: Base
    edits: list[Edit] = field(default_factory=list)
    custom: Callable[[Any], int] | None = None  # structural mutation on the Document
    expected_texts: Callable[[list[str]], list[str]] | None = None
    validator: Callable[[list[str], list[str]], bool] | None = None
    n_applied: int | None = None
    expects: list[Expect] = field(default_factory=list)
    allowed: tuple[str, ...] = ()
    cmp: CmpExpect = field(default_factory=lambda: CmpExpect("none"))
    must_parse: list[str] = field(default_factory=list)
    must_not_parse: list[str] = field(default_factory=list)
    summary: str = ""


def label(*types: str) -> tuple[str, ...]:
    return tuple(LABELS[FactType(t)][0] for t in types)


def letter_edit(
    word: str, rng: random.Random, lo: int, hi: int, kinds: tuple[str, ...] | None = None
) -> str:
    kinds = kinds or ("drop", "double", "swap", "replace")
    alphabet = [chr(c) for c in range(0x10D0, 0x10F1)]
    for _ in range(50):
        if hi <= lo:
            return word
        k = rng.choice(kinds)
        i = rng.randrange(lo, hi)
        if k == "drop":
            new = word[:i] + word[i + 1 :]
        elif k == "double":
            new = word[:i] + word[i] + word[i:]
        elif k == "swap" and i + 1 < len(word) and word[i] != word[i + 1]:
            new = word[:i] + word[i + 1] + word[i] + word[i + 2 :]
        elif k == "replace":
            new = word[:i] + rng.choice([a for a in alphabet if a != word[i]]) + word[i + 1 :]
        else:
            continue
        if new != word:
            return new
    return word


def digit_edit(code: str, rng: random.Random, positions: list[int]) -> str:
    for _ in range(50):
        i = rng.choice(positions)
        if rng.random() < 0.5 and i + 1 in positions and code[i] != code[i + 1]:
            return code[:i] + code[i + 1] + code[i] + code[i + 2 :]
        d = rng.choice([c for c in "0123456789" if c != code[i]])
        return code[:i] + d + code[i + 1 :]
    return code


# Generic replacement pools. They are common Georgian words and surnames, none taken from the
# client's documents; any value that occurs in a base document is excluded at runtime.
SURNAMES_E = ["კაპანაძე", "მაისურაძე", "ქავთარაძე", "ნოზაძე", "ჯაფარიძე", "ბოლქვაძე", "სურმავაძე"]
SURNAMES_I = ["გელაშვილი", "მამულაშვილი", "ცქიტიშვილი", "ხარაიშვილი", "ნატროშვილი"]
PROJECT_WORDS = ["ოქროს", "მთის", "ახალი", "მზიანი", "ცისფერი", "ძველი", "მწვანე"]


class NotApplicableError(Exception):
    pass


def need(x: Any, what: str) -> Any:
    if not x:
        raise NotApplicableError(what)
    return x


def replace_all_edits(
    texts: list[str], old: str, new: str, boundary: str | None = None
) -> list[Edit]:
    return [Edit(pi, i, j, new) for pi, i, j in find_all(texts, old, boundary=boundary)]


# ---- error classes ---------------------------------------------------------------------------


def gen_id_code(b: Base, rng: random.Random) -> Case:
    ids = need(b.facts.ids, "ID codes")
    k = rng.randrange(len(ids))
    old = ids[k]
    new = old
    while new == old or new in ids:
        new = digit_edit(old, rng, list(range(1, len(old))))
    edits = replace_all_edits(b.texts, old, new, boundary="0-9")
    return Case(
        "id_code",
        ["client", "contractor"][k] if k < 2 else f"party{k}",
        b,
        edits,
        expects=[Expect(("AOA.ID_CODES",), new, (old,), len(edits))],
        cmp=CmpExpect("one", ("CMP.ID_CODE",), label("party_id_code", "id_code"), (old,), (new,)),
        must_parse=[new],
        summary=f"{old} -> {new}",
    )


def gen_iban(b: Base, rng: random.Random) -> Case:
    old = need(b.facts.iban, "IBAN")
    new = digit_edit(old, rng, list(range(6, len(old))))
    edits = replace_all_edits(b.texts, old, new, boundary="A-Z0-9")
    return Case(
        "iban",
        "digit",
        b,
        edits,
        expects=[Expect(("AOA.BANK",), new, (old,), len(edits))],
        allowed=("AOA.IBAN_CHECKSUM",),
        cmp=CmpExpect("one", ("CMP.BANK",), label("bank_account"), (old,), (new,)),
        must_parse=[new],
        summary=f"{old} -> {new}",
    )


def gen_bic_other(b: Base, rng: random.Random) -> Case:
    old = need(b.facts.bic, "BIC")
    if len(old) < 8:
        raise NotApplicableError("base BIC malformed")
    new = old
    while new == old or new in b.full:
        new = "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(4)) + old[4:]
    edits = replace_all_edits(b.texts, old, new, boundary="A-Z0-9")
    return Case(
        "bic_other",
        "valid-looking",
        b,
        edits,
        expects=[Expect(("AOA.BANK",), new, (old,), len(edits))],
        allowed=("AOA.BANK_PAIRING",),
        cmp=CmpExpect("one", ("CMP.BANK",), label("bank_code"), (old,), (new,)),
        must_parse=[new],
        summary=f"{old} -> {new}",
    )


def gen_bic_malformed(b: Base, rng: random.Random) -> Case:
    old = need(b.facts.bic, "BIC")
    if len(old) < 8:
        raise NotApplicableError("base BIC malformed")
    kind = rng.choice(["drop", "digit", "extra"])
    i = rng.randrange(1, 4)
    if kind == "drop":
        new = old[:i] + old[i + 1 :]
    elif kind == "digit":
        new = old[:i] + rng.choice("0123456789") + old[i + 1 :]
    else:
        new = old[:8] + rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") + old[8:]
    edits = replace_all_edits(b.texts, old, new, boundary="A-Z0-9")
    rules = ("AOA.BANK", "AOA.BANK_CODE_FORMAT")
    return Case(
        "bic_malformed",
        kind,
        b,
        edits,
        expects=[Expect(rules, new, (old,), len(edits))],
        allowed=(*rules, "AOA.BANK_PAIRING"),
        cmp=CmpExpect("one", ("CMP.BANK",), label("bank_code"), (old,), (new,)),
        must_parse=[new],
        summary=f"{old} -> {new}",
    )


def gen_cadastral(b: Base, rng: random.Random) -> Case:
    old = need(b.facts.cadastral, "cadastral code")
    pos = [
        i
        for i in range(len(old) - 1)
        if old[i].isdigit() and old[i + 1].isdigit() and old[i] != old[i + 1]
    ]
    i = rng.choice(need(pos, "swappable digits"))
    new = old[:i] + old[i + 1] + old[i] + old[i + 2 :]
    edits = replace_all_edits(b.texts, old, new, boundary="0-9.")
    return Case(
        "cadastral",
        "digit-swap",
        b,
        edits,
        expects=[Expect(("AOA.CADASTRAL",), new, (old,), len(edits))],
        cmp=CmpExpect("one", ("CMP.CADASTRAL",), label("cadastral_code"), (old,), (new,)),
        must_parse=[new],
        summary=f"{old} -> {new}",
    )


def _other_value(v: int, rng: random.Random) -> int:
    step = 100 if v >= 1000 else (10 if v >= 100 else 1)
    while True:
        n = v + rng.choice([-1, 1]) * rng.randint(1, 9) * step
        if n > 0 and n != v:
            return n


def _fee_form(fee: dict[str, Any]) -> str:
    """The fee as the act writes it: digits (words) currency."""
    return f"{fee['digits']} ({fee['words']}) {fee['currency']}"


def gen_fee_both(b: Base, rng: random.Random) -> Case:
    fee = need(b.facts.fee, "fee")
    v2 = _other_value(fee["value"], rng)
    d2, w2 = format_like(v2, fee["digits"]), g.number_to_words(v2)
    pi = fee["pi"]
    edits = [Edit(pi, *fee["d"], d2), Edit(pi, *fee["w"], w2)]
    text = f"{d2} ({w2})"
    corr = _fee_form(fee)
    return Case(
        "fee_both",
        "consistent",
        b,
        edits,
        expects=[Expect(("AOA.FEE",), text, (corr,), 1)],
        cmp=CmpExpect(
            "one",
            ("CMP.AMOUNT",),
            label("total_amount", "price", "amount"),
            (fee["digits"],),
            (d2,),
        ),
        must_parse=[d2, w2],
        summary=f"{fee['digits']} ({fee['words']}) -> {text}",
    )


def gen_fee_words(b: Base, rng: random.Random) -> Case:
    fee = need(b.facts.fee, "fee")
    w2 = g.number_to_words(_other_value(fee["value"], rng))
    edits = [Edit(fee["pi"], *fee["w"], w2)]
    return Case(
        "fee_words",
        "words-only",
        b,
        edits,
        expects=[Expect(("AOA.AMOUNT_WORDS",), w2, (fee["words"], _fee_form(fee)), 1)],
        allowed=("AOA.FEE",),
        cmp=CmpExpect("one", ("CMP.AMOUNT_WORDS",), (), (fee["words"],), (w2,)),
        must_parse=[w2],
        summary=f"({fee['words']}) -> ({w2})",
    )


def gen_fee_digits(b: Base, rng: random.Random) -> Case:
    fee = need(b.facts.fee, "fee")
    d2 = format_like(_other_value(fee["value"], rng), fee["digits"])
    edits = [Edit(fee["pi"], *fee["d"], d2)]
    return Case(
        "fee_digits",
        "digits-only",
        b,
        edits,
        expects=[
            Expect(("AOA.FEE",), d2, (fee["digits"], _fee_form(fee)), 1),
            Expect(("AOA.AMOUNT_WORDS",), d2, (fee["digits"], _fee_form(fee)), 1),
        ],
        cmp=CmpExpect("one", ("CMP.AMOUNT", "CMP.AMOUNT_WORDS"), (), (fee["digits"],), (d2,)),
        must_parse=[d2],
        summary=f"{fee['digits']} -> {d2}",
    )


def gen_act_date(b: Base, rng: random.Random) -> Case:
    ad = need(b.facts.act_date, "act date")
    per = need(b.facts.period, "service period")
    end = per[1].d
    last = date(end.year, end.month, calendar.monthrange(end.year, end.month)[1])
    correct = render_like(last, ad)
    while True:
        new_d = ad.d + timedelta(days=rng.choice([*range(-20, 0), *range(1, 21)]))
        if new_d != last:
            break
    new = render_like(new_d, ad)
    edits = [Edit(ad.pi, ad.i, ad.j, new)]
    return Case(
        "act_date",
        "earlier" if new_d < ad.d else "later",
        b,
        edits,
        expects=[Expect(("AOA.ACT_DATE",), new, (correct,), 1)],
        cmp=CmpExpect(
            "one",
            ("CMP.DATE",),
            label("act_date"),
            (ad.text, ad.d.isoformat()),
            (new, new_d.isoformat()),
        ),
        must_parse=[new],
        summary=f"{ad.text} -> {new} (correct {correct})",
    )


def gen_period_year(b: Base, rng: random.Random) -> Case:
    a, z = need(b.facts.period, "service period")
    if a.pi != z.pi:
        raise NotApplicableError("period split over paragraphs")
    dy = rng.choice([-1, 1])
    s = b.texts[a.pi]
    old = s[a.i : z.j]
    new = re.sub(r"(?<!\d)(\d{4})(?!\d)", lambda m: str(int(m.group(1)) + dy), old)
    edits = [Edit(a.pi, a.i, z.j, new)]
    tail = s[z.j :].lstrip()
    alts = (old, f"{old} ჩათვლით") if tail.startswith("ჩათვლით") else (old,)
    iso_old = f"{a.d.isoformat()}/{z.d.isoformat()}"
    iso_new = f"{a.d.replace(year=a.d.year + dy).isoformat()}/{z.d.replace(year=z.d.year + dy).isoformat()}"
    return Case(
        "period_year",
        f"{dy:+d}y",
        b,
        edits,
        expects=[Expect(("AOA.PERIOD_IN_TERM",), new, alts, 1)],
        cmp=CmpExpect(
            "one",
            ("CMP.DURATION", "CMP.DATE"),
            label("execution_period"),
            (old, iso_old),
            (new, iso_new),
        ),
        must_parse=[new],
        summary=f"{old} -> {new}",
    )


def gen_director(b: Base, rng: random.Random) -> Case:
    dirs = need([d for d in b.facts.directors if d["spans"]], "directors")
    d = rng.choice(dirs)
    stem, nom = d["stem"], d["nom"]
    kind = rng.choice(["replace", "typo"])
    if kind == "replace":
        pool = SURNAMES_E if nom.endswith("ე") else SURNAMES_I if nom.endswith("ი") else []
        pool = [s for s in pool if s not in b.full_all]
        if not pool:
            kind = "typo"
        else:
            new_nom = rng.choice(pool)
            new_stem = new_nom[:-1]
    if kind == "typo":
        new_stem = letter_edit(stem, rng, 1, max(2, len(stem) - 1))
    if new_stem == stem:
        raise NotApplicableError("no director edit")
    edits = [Edit(sp.pi, sp.i, sp.i + len(stem), new_stem) for sp in d["spans"]]
    expects = []
    for form, n in d["forms"].items():
        new_form = new_stem + form[len(stem) :]
        expects.append(
            Expect(("AOA.DIRECTOR",), f"{d['first']} {new_form}", (f"{d['first']} {form}",), n)
        )
    new_nom_full = f"{d['first']} {new_stem + nom[len(stem) :]}"
    return Case(
        "director",
        kind,
        b,
        edits,
        expects=expects,
        cmp=CmpExpect(
            "one",
            ("CMP.PARTY",),
            label("party_director"),
            (f"{d['first']} {nom}",),
            (new_nom_full,),
        ),
        must_parse=[e.text for e in expects],
        summary=f"{d['first']} {nom} -> {new_nom_full} x{len(edits)}",
    )


def gen_party_name(b: Base, rng: random.Random) -> Case:
    names = need(b.facts.party_names, "party names")
    legal, core = rng.choice(names)
    words = [(m.start(), m.end()) for m in re.finditer(rf"[{KA}]{{4,}}", core)]
    a, z = rng.choice(need(words, "name word"))
    w = core[a:z]
    new_core = core[:a] + letter_edit(w, rng, 1, len(w) - 1) + core[z:]
    edits = replace_all_edits(b.texts, core, new_core, boundary=KA)
    return Case(
        "party_name",
        "typo",
        b,
        edits,
        expects=[Expect(("AOA.PARTY_NAME",), new_core, (f"{legal} {core}",), len(edits))],
        cmp=CmpExpect("one", ("CMP.PARTY",), label("party_name"), (core,), (new_core,)),
        must_parse=[new_core],
        summary=f"{legal} {core} -> {new_core} x{len(edits)}",
    )


def gen_address(b: Base, rng: random.Random) -> Case:
    addrs = need(b.facts.addresses, "addresses")
    old = rng.choice(addrs)
    m = re.search(r"N\s?(\d+)", old)
    if m and rng.random() < 0.6:
        n = int(m.group(1))
        n2 = n
        while n2 == n or n2 <= 0:
            n2 = n + rng.choice([-1, 1]) * rng.randint(1, 9)
        new = old[: m.start(1)] + str(n2) + old[m.end(1) :]
        kind = "number"
    else:
        words = [(x.start(), x.end()) for x in re.finditer(rf"[{KA}]{{5,}}", old)]
        a, z = rng.choice(need(words, "address word"))
        new = old[:a] + letter_edit(old[a:z], rng, 1, z - a - 1) + old[z:]
        kind = "typo"
    edits = replace_all_edits(b.texts, old, new)
    return Case(
        "address",
        kind,
        b,
        edits,
        expects=[Expect(("AOA.PARTY_ADDRESS",), new, (old,), len(edits))],
        cmp=CmpExpect("one", ("CMP.PARTY",), label("party_address"), (old,), (new,)),
        must_parse=[new],
        summary=f"{old} -> {new}",
    )


def gen_project_name(b: Base, rng: random.Random) -> Case:
    spans = need(b.facts.project_forms, "project name")
    kind = rng.choice(["replace", "typo"])
    words0 = spans[0].text.split()
    if kind == "replace" and len(words0) > 1:
        pool = [w for w in PROJECT_WORDS if w not in b.full_all]
        nw = rng.choice(pool)
        chosen = spans
        new_of = {sp.text: nw + sp.text[len(sp.text.split()[0]) :] for sp in chosen}
        old_word, new_word = words0[0], nw
    else:
        kind = "typo"
        chosen = [rng.choice(spans)]
        w = chosen[0].text.split()[0]
        hi = len(w) - 1 if len(chosen[0].text.split()) > 1 else max(2, len(w) - 5)
        nw = letter_edit(w, rng, 1, hi)
        new_of = {chosen[0].text: nw + chosen[0].text[len(w) :]}
        old_word, new_word = w, nw
    edits = [Edit(sp.pi, sp.i, sp.j, new_of[sp.text]) for sp in chosen]
    forms = Counter(sp.text for sp in chosen)
    expects = [Expect(("AOA.PROJECT_NAME",), new_of[t], (t,), n) for t, n in forms.items()]
    return Case(
        "project_name",
        kind,
        b,
        edits,
        expects=expects,
        cmp=CmpExpect("one", ("CMP.PROJECT",), label("project_name"), (old_word,), (new_word,)),
        must_parse=list(new_of.values()),
        summary=f"{old_word} -> {new_word} x{len(edits)}",
    )


def gen_month_typo(b: Base, rng: random.Random) -> Case:
    sp = rng.choice(need(b.facts.month_words, "month words"))
    for _ in range(50):
        new = letter_edit(sp.text, rng, 1, max(2, len(sp.text) - 2))
        if new not in MONTHS and new != sp.text:
            break
    else:
        raise NotApplicableError("no month typo")
    return Case(
        "month_typo",
        MONTHS[sp.text][1],
        b,
        [Edit(sp.pi, sp.i, sp.j, new)],
        expects=[Expect(("AOA.SPELLING",), new, (sp.text,), 1)],
        cmp=CmpExpect("one", ("CMP.SPELLING", "CMP.TEXT_CHANGED"), (), (sp.text,), (new,)),
        must_parse=[new],
        summary=f"{sp.text} -> {new}",
    )


def gen_number_word_typo(b: Base, rng: random.Random) -> Case:
    fee = need(b.facts.fee, "fee")
    words = fee["words"]
    ws = [(m.start(), m.end()) for m in re.finditer(rf"[{KA}]{{3,}}", words)]
    for _ in range(50):
        a, z = rng.choice(need(ws, "number words"))
        new = words[:a] + letter_edit(words[a:z], rng, 1, z - a - 1) + words[z:]
        if new != words and g.parse_number_words(new) is None:
            break
    else:
        raise NotApplicableError("no number-word typo")
    pi, (i, j) = fee["pi"], fee["w"]
    return Case(
        "number_word_typo",
        "letter",
        b,
        [Edit(pi, i, j, new)],
        expects=[Expect(("AOA.AMOUNT_WORDS",), new, (words,), 1)],
        cmp=CmpExpect(
            "one", ("CMP.AMOUNT_WORDS", "CMP.SPELLING", "CMP.TEXT_CHANGED"), (), (words,), (new,)
        ),
        must_parse=[new],
        summary=f"{words} -> {new}",
    )


def gen_script_letter(b: Base, rng: random.Random) -> Case:
    protected = set(re.findall(rf"[{KA}]+", " ".join(b.protected)))
    cands = [
        Span(pi, m.start(), m.end(), m.group(0))
        for pi, s in enumerate(b.texts)
        for m in re.finditer(rf"(?<![\w-])[{KA}]{{6,}}(?![\w-])", s)
        if m.group(0) not in protected and m.group(0) not in MONTHS
    ]
    sp = rng.choice(need(cands, "prose words"))
    script = rng.choice(["latin", "cyrillic"])
    ch = rng.choice("aoeicxpk" if script == "latin" else "аоехсрк")
    k = rng.randrange(1, len(sp.text))
    new = sp.text[:k] + ch + sp.text[k:]
    return Case(
        "script_letter",
        script,
        b,
        [Edit(sp.pi, sp.i, sp.j, new)],
        expects=[Expect(("AOA.SPELLING",), new, (sp.text,), 1)],
        cmp=CmpExpect(
            "one", ("CMP.SPELLING", "CMP.HOMOGLYPH", "CMP.TEXT_CHANGED"), (), (sp.text,), (new,)
        ),
        must_parse=[new],
        summary=f"{sp.text} -> {new} ({script} {ch!r})",
    )


#: The Georgian alphabet a typo draws a letter from.
GEORGIAN_LETTERS = [chr(c) for c in range(0x10D0, 0x10F1)]
PROSE_EDITS = ("drop", "insert", "swap", "replace")
_LEGAL_FORM_OR_TITLE = re.compile(r"(?:შპს|სს|ი/მ|ფ/პ|ააიპ|დირექტორ[ა-ჰ]*)\s*:?\s*$")


@dataclass
class _ProseWords:
    cands: list[Span]
    speller: Speller
    act_words: set[str]
    contract_words: set[str]

    def no_word(self, w: str, new: str) -> bool:
        """The typo is one typo in one place: no word the dictionary knows, the contract writes
        or the act has anywhere else."""
        return not (
            new == w
            or new in self.act_words
            or new in self.contract_words
            or self.speller.known(new)
        )


def _prose_words(b: Base) -> _ProseWords:
    """The act's ordinary prose words a typo class may edit.

    The word is five letters or more and nobody else's: not a name, an address, a project or the
    fee's words (``protected``), not quoted, not in a date, no month or number word, not a word
    the base act already reports."""
    speller = load_speller(ROOT / "models" / "hunspell")
    if not isinstance(speller, Speller):
        raise NotApplicableError("no spelling dictionary (scripts/fetch_spelling_dictionary.py)")
    protected = set(re.findall(rf"[{KA}]+", " ".join(b.protected)))
    reported = {w for f in b.findings for w in re.findall(rf"[{KA}]+", f.actual or "")}
    contract_words = set(re.findall(rf"[{KA}]+", b.contract_text))
    act_words = set(re.findall(rf"[{KA}]+", b.full))
    cands = []
    for pi, s in enumerate(b.texts):
        fenced = [m.span() for m in re.finditer(rf"{QO}[^„“”\"«»]{{1,90}}?{QC}", s)]
        fenced += [m.span() for m in DATE_KA.finditer(s)]
        fenced += [m.span() for m in re.finditer(r"\([^()]{1,80}\)", s)]  # "2 (ორი)"
        for m in re.finditer(rf"(?<![\w-])[{KA}]{{5,}}(?![\w-])", s):
            w = m.group(0)
            if w in protected or w in MONTHS or w in reported:
                continue
            if g.parse_number_words(w) is not None or _LEGAL_FORM_OR_TITLE.search(s[: m.start()]):
                continue
            if any(a <= m.start() < z for a, z in fenced):
                continue
            cands.append(Span(pi, m.start(), m.end(), w))
    need(cands, "prose words")
    return _ProseWords(cands, speller, act_words, contract_words)


def gen_prose_typo(b: Base, rng: random.Random) -> Case:
    """One letter edit in an ordinary word of the act's prose (the prose spelling rule): a letter
    dropped, a random letter inserted, two neighbours swapped or one replaced - any letter, as
    no hand types. Never the first letter: the rule never corrects that one, and a writer rarely
    mistypes it (report recall with it included as this class times the share of edits that
    fall there). ``prose_slip`` makes the edits a hand makes."""
    pw = _prose_words(b)
    cands, speller = pw.cands, pw.speller
    for _ in range(200):
        sp = rng.choice(cands)
        w, kind = sp.text, rng.choice(PROSE_EDITS)
        if kind == "drop":
            i = rng.randrange(1, len(w))
            new = w[:i] + w[i + 1 :]
        elif kind == "insert":
            i = rng.randrange(1, len(w) + 1)
            new = w[:i] + rng.choice(GEORGIAN_LETTERS) + w[i:]
        elif kind == "swap":
            i = rng.randrange(1, len(w) - 1)
            new = w[:i] + w[i + 1] + w[i] + w[i + 2 :]
        else:
            i = rng.randrange(1, len(w))
            new = w[:i] + rng.choice([c for c in GEORGIAN_LETTERS if c != w[i]]) + w[i + 1 :]
        if not pw.no_word(w, new):
            continue
        break
    else:
        raise NotApplicableError("no typo that is no word")
    known = "known" if speller.known(w) else "unknown"
    return Case(
        "prose_typo",
        f"{kind}/{known}",
        b,
        [Edit(sp.pi, sp.i, sp.j, new)],
        expects=[Expect(("AOA.SPELLING",), new, (w,), 1)],
        cmp=CmpExpect("one", ("CMP.SPELLING", "CMP.TEXT_CHANGED"), (), (w,), (new,)),
        must_parse=[new],
        summary=f"{w} -> {new} ({kind}, original {known} to the dictionary)",
    )


#: The edits a hand makes: a neighbouring key or the shift slipped, a letter doubled, a
#: neighbouring key caught as well, two letters swapped, one dropped - and a letter of the same
#: sound series written by ear.
SLIP_EDITS = ("key", "sound", "double", "extra", "swap", "drop")


def gen_prose_slip(b: Base, rng: random.Random) -> Case:
    """``prose_typo`` with the edits typing and spelling by ear make (``SLIP_EDITS``) instead of
    any letter anywhere: the typos the prose rule is built for, beside the random ones it is
    measured on as well."""
    pw = _prose_words(b)
    keys = {
        c: [o for o in GEORGIAN_LETTERS if o != c and g.typing_slip(o, c) and not _same_sound(o, c)]
        for c in GEORGIAN_LETTERS
    }
    sounds = {
        c: [o for o in GEORGIAN_LETTERS if o != c and _same_sound(o, c)] for c in GEORGIAN_LETTERS
    }
    for _ in range(300):
        sp = rng.choice(pw.cands)
        w, kind = sp.text, rng.choice(SLIP_EDITS)
        i = rng.randrange(1, len(w))
        if kind in ("key", "sound"):
            pool = (keys if kind == "key" else sounds).get(w[i], [])
            if not pool:
                continue
            new = w[:i] + rng.choice(pool) + w[i + 1 :]
        elif kind == "double":
            new = w[:i] + w[i] + w[i:]
        elif kind == "extra":
            pool = keys.get(w[i], [])
            if not pool:
                continue
            new = w[:i] + rng.choice(pool) + w[i:]
        elif kind == "swap":
            if i >= len(w) - 1:
                continue
            new = w[:i] + w[i + 1] + w[i] + w[i + 2 :]
        else:
            new = w[:i] + w[i + 1 :]
        if pw.no_word(w, new):
            break
    else:
        raise NotApplicableError("no slip that is no word")
    return Case(
        "prose_slip",
        kind,
        b,
        [Edit(sp.pi, sp.i, sp.j, new)],
        expects=[Expect(("AOA.SPELLING",), new, (w,), 1)],
        cmp=CmpExpect("one", ("CMP.SPELLING", "CMP.TEXT_CHANGED"), (), (w,), (new,)),
        must_parse=[new],
        summary=f"{w} -> {new} ({kind})",
    )


def _same_sound(a: str, b: str) -> bool:
    return any(a in series and b in series for series in SOUND_SERIES)


#: Georgian's stop and affricate series (voiced, aspirated, ejective), as the spelling rule
#: reads them (``georgian.typing_slip``).
SOUND_SERIES = ("ბფპ", "დთტ", "გქკ", "ძცწ", "ჯჩჭ", "ქხყ")


def gen_project_removed(b: Base, rng: random.Random) -> Case:
    spans = need(b.facts.project_forms, "project name")
    edits = []
    for sp in spans:
        s = b.texts[sp.pi]
        i, j = sp.i, sp.j
        if i > 0 and s[i - 1] in '„“"«”':
            i -= 1
            if i > 0 and s[i - 1] == ",":
                i -= 1
        if j < len(s) and s[j] in '“”"»':
            j += 1
        if i > 0 and s[i - 1] == " ":
            i -= 1
        edits.append(Edit(sp.pi, i, j, ""))
    word = spans[0].text.split()[0]
    return Case(
        "project_removed",
        f"x{len(edits)}",
        b,
        edits,
        expects=[Expect(("AOA.PROJECT_PRESENT",), None)],
        cmp=CmpExpect("one", ("CMP.PROJECT",), label("project_name"), (word,), (), removed=True),
        must_not_parse=[sp.text for sp in spans],
        summary=f"removed {len(edits)} project mentions",
    )


def gen_cadastral_removed(b: Base, rng: random.Random) -> Case:
    code = need(b.facts.cadastral, "cadastral code")
    edits = []
    for pi, s in enumerate(b.texts):
        for m in re.finditer(rf"\s*\(ს/კ\s*:?\s*{re.escape(code)}\s*\)", s):
            edits.append(Edit(pi, m.start(), m.end(), ""))
        if code in s and not re.search(rf"\(ს/კ\s*:?\s*{re.escape(code)}", s):
            for m in re.finditer(rf"[\s,;:]*{re.escape(code)}", s):
                edits.append(Edit(pi, m.start(), m.end(), ""))
    return Case(
        "cadastral_removed",
        f"x{len(edits)}",
        b,
        need(edits, "cadastral mentions"),
        expects=[Expect(("AOA.CADASTRAL_PRESENT",), None)],
        cmp=CmpExpect(
            "one", ("CMP.CADASTRAL",), label("cadastral_code"), (code,), (), removed=True
        ),
        must_not_parse=[code],
        summary=f"removed {len(edits)} cadastral mentions",
    )


def gen_role_swap(b: Base, rng: random.Random) -> Case:
    roles = b.facts.roles
    if len(roles) < 2 or roles[0]["role"] == roles[1]["role"]:
        raise NotApplicableError("role definitions")
    r0, r1 = roles[0], roles[1]
    edits = [Edit(r0["pi"], *r0["word"], r1["role"]), Edit(r1["pi"], *r1["word"], r0["role"])]
    expects = []
    for r, other in ((r0, r1), (r1, r0)):
        s = b.texts[r["pi"]]
        frag = s[r["frag"][0] : r["frag"][1]]
        new_frag = frag.replace(r["role"], other["role"])
        expects.append(Expect(("AOA.PARTY_ROLE",), new_frag, (frag, r["role"]), 1))
    return Case(
        "role_swap",
        "intro",
        b,
        edits,
        expects=expects,
        cmp=CmpExpect(
            "one",
            ("CMP.PARTY", "CMP.TEXT_CHANGED"),
            (),
            (),
            (),
        ),
        must_parse=[e.text for e in expects],
        summary="role labels swapped in the intro",
    )


# ---- harmless classes ------------------------------------------------------------------------


def _charmap_case(
    cls: str, variant: str, b: Base, fn: Callable[[str], str], cmp_mode: str = "none"
) -> Case:
    def custom(doc: Any) -> int:
        n = 0
        for p in paragraphs(doc):
            s = ptext(p)
            t = fn(s)
            if t != s:
                set_text_same_length(p, t)
                n += 1
        return n

    changed = sum(1 for s in b.texts if fn(s) != s)
    if not changed:
        raise NotApplicableError("nothing to change")
    return Case(
        cls,
        variant,
        b,
        custom=custom,
        expected_texts=lambda ts: [fn(s) for s in ts],
        n_applied=changed,
        cmp=CmpExpect(cmp_mode),
        summary=f"{changed} paragraphs",
    )


def _quote_map(style: str) -> Callable[[str], str]:
    def fn(s: str) -> str:
        out = list(s)
        for k, ch in enumerate(s):
            if ch not in '„“”"«»':
                continue
            opening = k == 0 or s[k - 1] in " (\t[,-–" or ch in "„«"
            if style == "straight":
                out[k] = '"'
            elif style == "guillemets":
                out[k] = "«" if opening else "»"
            else:
                out[k] = "“" if opening else "”"
        return "".join(out)

    return fn


def gen_h_quotes(b: Base, rng: random.Random) -> Case:
    style = rng.choice(["straight", "guillemets", "english"])
    return _charmap_case("h_quotes", style, b, _quote_map(style))


def gen_h_dash_nbsp(b: Base, rng: random.Random) -> Case:
    nb_hyphen = rng.random() < 0.5

    def fn(s: str) -> str:
        s = s.replace(" - ", " – ")
        s = re.sub(r"(?<=\d) (?=\d)", " ", s)
        s = re.sub(r"(?<=ქ\.) ", " ", s)
        s = re.sub(r" (?=N\d)", " ", s)
        if nb_hyphen:
            s = re.sub(rf"(?<=[{KA}])-(?=[{KA}])", "‑", s)
        return s

    return _charmap_case("h_dash_nbsp", "nbhyphen" if nb_hyphen else "dash", b, fn)


def gen_h_amount_format(b: Base, rng: random.Random) -> Case:
    fee = need(b.facts.fee, "fee")
    v = fee["value"]
    variants = {str(v), f"{v:,.2f}"}
    if v >= 1000:
        variants |= {f"{v:,}".replace(",", " "), f"{v:,}".replace(",", " ") + ".00"}
    variants.discard(fee["digits"])
    new = rng.choice(sorted(variants))
    return Case(
        "h_amount_format",
        new.replace(" ", " "),
        b,
        [Edit(fee["pi"], *fee["d"], new)],
        must_parse=[new],
        summary=f"{fee['digits']} -> {new}",
    )


def gen_h_date_style(b: Base, rng: random.Random) -> Case:
    ad = need(b.facts.act_date, "act date")
    d = ad.d
    nom = g.month_form(d.month, "nom")
    styles = {
        "ka": f"{d.year} წლის {d.day} {g.month_form(d.month, ad.case or 'nom')}",
        "dot": f"{d.day:02d}.{d.month:02d}.{d.year}",
        "ka_comma": f"{d.day} {nom}, {d.year} წ.",
    }
    styles = {k: v for k, v in styles.items() if v != ad.text}
    k = rng.choice(sorted(styles))
    return Case(
        "h_date_style",
        k,
        b,
        [Edit(ad.pi, ad.i, ad.j, styles[k])],
        must_parse=[styles[k]],
        summary=f"{ad.text} -> {styles[k]}",
    )


def gen_h_run_split_bold(b: Base, rng: random.Random) -> Case:
    f = b.facts
    targets = sorted({x.pi for x in f.month_words} | ({f.fee["pi"]} if f.fee else set()))
    targets += [
        pi for pi, s in enumerate(b.texts) if (f.iban and f.iban in s) or any(i in s for i in f.ids)
    ]
    targets = sorted(set(targets))
    need(targets, "entity paragraphs")
    plan = []
    for pi in rng.sample(targets, min(3, len(targets))):
        s = b.texts[pi]
        words = [(m.start(), m.end()) for m in re.finditer(r"\S+", s)]
        bold = rng.choice(words)
        cut = rng.randrange(1, len(s)) if len(s) > 1 else None
        plan.append((pi, bold, cut))

    def custom(doc: Any) -> int:
        ps = paragraphs(doc)
        n = 0
        for pi, (a, z), cut in plan:
            p = ps[pi]
            if split_run_at(p, a) and split_run_at(p, z):
                for r in runs_in(p, a, z):
                    make_bold(r)
                n += 1
            if cut is not None:
                split_run_at(p, cut)
        return n

    return Case(
        "h_run_split_bold",
        f"{len(plan)} paras",
        b,
        custom=custom,
        expected_texts=lambda ts: list(ts),
        n_applied=len(plan),
        summary=f"bold+split in paragraphs {[p[0] for p in plan]}",
    )


def gen_h_empty_para(b: Base, rng: random.Random) -> Case:
    k = rng.randrange(len(b.texts))

    def custom(doc: Any) -> int:
        p = paragraphs(doc)[k]
        p.addnext(etree.Element(qn("w:p")))
        return 1

    return Case(
        "h_empty_para",
        f"after {k}",
        b,
        custom=custom,
        expected_texts=lambda ts: [*ts[: k + 1], "", *ts[k + 1 :]],
        n_applied=1,
        summary=f"empty paragraph after paragraph {k}",
    )


def _numbered_table(doc: Any) -> Any | None:
    for tbl in doc.element.body.iterchildren(qn("w:tbl")):
        rows = tbl.findall(qn("w:tr"))
        nums = 0
        for tr in rows:
            tcs = tr.findall(qn("w:tc"))
            if tcs and re.fullmatch(
                r"\d{1,2}\.?", "".join(t.text or "" for t in tcs[0].iter(qn("w:t"))).strip()
            ):
                nums += 1
        if nums >= 3:
            return tbl
    return None


def gen_h_table_to_paras(b: Base, rng: random.Random) -> Case:
    if _numbered_table(Document(str(b.act))) is None:
        raise NotApplicableError("no numbered-sections table")

    def custom(doc: Any) -> int:
        tbl = _numbered_table(doc)
        new: list[Any] = []
        for tr in tbl.findall(qn("w:tr")):
            tcs = tr.findall(qn("w:tc"))
            cell_text = ["".join(t.text or "" for t in tc.iter(qn("w:t"))).strip() for tc in tcs]
            num, title = [*cell_text, "", ""][:2]
            body_cells = tcs[2:] if len(tcs) >= 3 else tcs[1:]
            if num or title:
                src = (tcs[1] if len(tcs) > 1 else tcs[0]).find(qn("w:p"))
                h = copy.deepcopy(src)
                set_text(h, f"{num}. {title}" if num and title else (title or num))
                new.append(h)
            for tc in body_cells:
                new.extend(copy.deepcopy(p) for p in tc.findall(qn("w:p")))
        for p in new:
            tbl.addprevious(p)
        tbl.getparent().remove(tbl)
        return 1

    def validator(base: list[str], mut: list[str]) -> bool:
        squash = lambda ts: re.sub(r"[\s.]", "", "".join(ts))  # noqa: E731
        return squash(base) == squash(mut)  # same words in the same order, only the layout moved

    return Case(
        "h_table_to_paras",
        "rows->paragraphs",
        b,
        custom=custom,
        validator=validator,
        n_applied=1,
        summary="numbered-sections table converted to paragraphs",
    )


def gen_h_contract_date_sentence(b: Base, rng: random.Random) -> Case:
    ad = need(b.facts.act_date, "act date")
    cd = need(b.facts.contract_date, "contract date")
    sentence = f"ხელშეკრულება გაფორმდა {cd.text}."

    def custom(doc: Any) -> int:
        p = paragraphs(doc)[ad.pi]
        q = copy.deepcopy(p)
        set_text(q, sentence)
        p.addprevious(q)
        return 1

    return Case(
        "h_contract_date_sentence",
        cd.style,
        b,
        custom=custom,
        expected_texts=lambda ts: [*ts[: ad.pi], sentence, *ts[ad.pi :]],
        n_applied=1,
        cmp=CmpExpect("text_only"),
        must_parse=[sentence],
        summary=sentence,
    )


def gen_h_item_number(b: Base, rng: random.Random) -> Case:
    fee = need(b.facts.fee, "fee")
    i = fee["d"][0]
    return Case(
        "h_item_number",
        "1.",
        b,
        [Edit(fee["pi"], i, i, "1. ")],
        cmp=CmpExpect("text_only"),
        must_parse=[f"1. {fee['digits']}"],
        summary="'1. ' before the fee",
    )


def gen_h_currency_equivalent(b: Base, rng: random.Random) -> Case:
    fee = need(b.facts.fee, "fee")
    j = fee["c"][1]
    n = max(1, round(fee["value"] / rng.choice([2.65, 2.7, 2.75])))
    ins = f" (ეკვივალენტი {n} აშშ დოლარი)"
    return Case(
        "h_currency_equivalent",
        str(n),
        b,
        [Edit(fee["pi"], j, j, ins)],
        cmp=CmpExpect("text_only", extra_ok=("CMP.AMOUNT",)),
        must_parse=[ins.strip()],
        summary=f"inserted{ins}",
    )


def gen_h_tetri(b: Base, rng: random.Random) -> Case:
    fee = need(b.facts.fee, "fee")
    if not fee["currency"].startswith("ლარ") or re.search(r"[.,]\d{1,2}$", fee["digits"]):
        raise NotApplicableError("fee not in whole lari")
    kind = rng.choice(["words", "suffix"])
    if kind == "words":  # 2 400.00 (ორი ათას ოთხასი ლარი და 00 თეთრი)
        new = f"{fee['digits']}.00 ({fee['words']} ლარი და 00 თეთრი)"
    else:  # 2 400 (ორი ათას ოთხასი) ლარს და 00 თეთრს
        new = f"{fee['digits']} ({fee['words']}) {fee['currency']} და 00 თეთრ{fee['currency'][3:]}"
    return Case(
        "h_tetri",
        kind,
        b,
        [Edit(fee["pi"], *fee["all"], new)],
        cmp=CmpExpect("text_only"),
        must_parse=[new],
        summary=f"fee -> {new}",
    )


def gen_h_unquoted_names(b: Base, rng: random.Random) -> Case:
    names = need(b.facts.party_names, "party names")
    edits = []
    for _, core in names:
        for pi, s in enumerate(b.texts):
            for m in re.finditer(rf"({QO}){re.escape(core)}({QC})", s):
                edits.append(Edit(pi, m.start(2), m.end(2), ""))
                edits.append(Edit(pi, m.start(1), m.end(1), ""))
    return Case(
        "h_unquoted_names",
        f"{len(edits) // 2} names",
        b,
        need(edits, "quoted names"),
        summary=f"quotes removed around {len(edits) // 2} party names",
    )


def gen_h_currency_moved(b: Base, rng: random.Random) -> Case:
    fee = need(b.facts.fee, "fee")
    pi, (ci, _) = fee["pi"], fee["c"]
    kind = rng.choice(["paragraph", "break"])

    def custom(doc: Any) -> int:
        p = paragraphs(doc)[pi]
        if kind == "paragraph":
            q = copy.deepcopy(p)
            delete_to_end(p, ci)
            replace_range(q, 0, ci, "")
            p.addnext(q)
            return 1
        if not split_run_at(p, ci):
            return 0
        pos = 0
        for t in p.iter(qn("w:t")):
            if pos == ci:
                r = t.getparent()
                br = etree.Element(qn("w:r"))
                etree.SubElement(br, qn("w:br"))
                r.addprevious(br)
                return 1
            pos += len(t.text or "")
        return 0

    def expected(ts: list[str]) -> list[str]:
        if kind == "paragraph":
            s = ts[pi]
            return [*ts[:pi], s[:ci], s[ci:], *ts[pi + 1 :]]
        return list(ts)

    return Case(
        "h_currency_moved",
        kind,
        b,
        custom=custom,
        expected_texts=expected,
        n_applied=1,
        summary=f"currency word moved to the next {'paragraph' if kind == 'paragraph' else 'line'}",
    )


def gen_h_fee_relabel(b: Base, rng: random.Random) -> Case:
    for old, new in (("საფასური", "ღირებულება"), ("ღირებულება", "ფასი"), ("ფასი", "ღირებულება")):
        edits = replace_all_edits(b.texts, old, new, boundary=KA)
        if edits:
            return Case(
                "h_fee_relabel",
                f"{old}->{new}",
                b,
                edits,
                cmp=CmpExpect("text_only"),
                summary=f"{old} -> {new} x{len(edits)}",
            )
    raise NotApplicableError("no fee label")


GENERATORS: dict[str, Callable[[Base, random.Random], Case]] = {
    "id_code": gen_id_code,
    "iban": gen_iban,
    "bic_other": gen_bic_other,
    "bic_malformed": gen_bic_malformed,
    "cadastral": gen_cadastral,
    "fee_both": gen_fee_both,
    "fee_words": gen_fee_words,
    "fee_digits": gen_fee_digits,
    "act_date": gen_act_date,
    "period_year": gen_period_year,
    "director": gen_director,
    "party_name": gen_party_name,
    "address": gen_address,
    "project_name": gen_project_name,
    "month_typo": gen_month_typo,
    "number_word_typo": gen_number_word_typo,
    "script_letter": gen_script_letter,
    "prose_typo": gen_prose_typo,
    "prose_slip": gen_prose_slip,
    "project_removed": gen_project_removed,
    "cadastral_removed": gen_cadastral_removed,
    "role_swap": gen_role_swap,
    "h_quotes": gen_h_quotes,
    "h_dash_nbsp": gen_h_dash_nbsp,
    "h_amount_format": gen_h_amount_format,
    "h_date_style": gen_h_date_style,
    "h_run_split_bold": gen_h_run_split_bold,
    "h_empty_para": gen_h_empty_para,
    "h_table_to_paras": gen_h_table_to_paras,
    "h_contract_date_sentence": gen_h_contract_date_sentence,
    "h_item_number": gen_h_item_number,
    "h_currency_equivalent": gen_h_currency_equivalent,
    "h_tetri": gen_h_tetri,
    "h_unquoted_names": gen_h_unquoted_names,
    "h_currency_moved": gen_h_currency_moved,
    "h_fee_relabel": gen_h_fee_relabel,
}

# --------------------------------------------------------------------------------------------
# bases
# --------------------------------------------------------------------------------------------


@dataclass
class Base:
    alias: str
    synthetic: bool  # a generated set (has an answer key): its values may be printed
    act: Path
    contract: Path
    texts: list[str]
    full: str
    full_all: str  # act + contract text: replacement pools must avoid anything in here
    act_text: str
    contract_text: str
    findings: list[Any]
    segments: list[Any]
    facts: Facts
    protected: list[str]


def docx_texts(path: Path) -> list[str]:
    return [ptext(p) for p in paragraphs(Document(str(path)))]


def parse(path: Path, settings: Settings) -> tuple[str, list[Any]]:
    doc = convert(path, settings).document
    return chunks_text(chunk_docling_document(doc, doc_id="x")), segments_from_docling(doc)


#: Sets kept out of every harness run: the held-out set is scored once, never tuned against.
HELD_OUT = frozenset({"Test Set 3"})


def base_folders() -> list[Path]:
    """Every ``AOA and Contract`` folder under ``samples/`` except the held-out sets."""
    return [
        folder
        for folder in sorted((ROOT / "samples").glob("*/AOA and Contract"))
        if folder.parent.name not in HELD_OUT
    ]


def load_bases(settings: Settings) -> list[Base]:
    bases = []
    for folder in base_folders():
        acts = sorted(folder.glob("*Correct*.docx"))
        contracts = sorted(folder.glob("*ontract*.docx"))
        if not acts or not contracts:
            continue
        act, contract = acts[0], contracts[0]
        texts = docx_texts(act)
        act_text, segments = parse(act, settings)
        contract_text, _ = parse(contract, settings)
        report = check_aoa(
            aoa_text=act_text, contract_text=contract_text, speller=open_speller(settings)
        )
        facts = discover(texts)
        protected = [c for _, c in facts.party_names] + facts.addresses
        protected += [f"{d['first']} {f}" for d in facts.directors for f in d["forms"]]
        protected += [sp.text for sp in facts.project_forms]
        if facts.fee:
            protected.append(facts.fee["words"])
        set_dir = folder.parent
        alias = re.sub(r"[^a-z0-9]+", "-", set_dir.name.lower()).strip("-")
        bases.append(
            Base(
                alias=alias,
                synthetic=(set_dir / "ANSWER_KEY.md").exists(),
                act=act,
                contract=contract,
                texts=texts,
                full="\n".join(texts),
                full_all="\n".join(texts + docx_texts(contract)),
                act_text=act_text,
                contract_text=contract_text,
                findings=list(report.findings),
                segments=segments,
                facts=facts,
                protected=protected,
            )
        )
    return bases


# --------------------------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------------------------


def comment_coverage(docx_path: Path, needle: str) -> tuple[int, int, int]:
    """(occurrences of ``needle``, fully covered by a comment, touched by a comment), in the
    document's own words: a tracked change's deleted text counts, its inserted text does not."""
    with zipfile.ZipFile(docx_path) as z:
        root = etree.fromstring(z.read("word/document.xml"))
    body = root.find(qn("w:body"))
    chars: list[str] = []
    cov: list[bool] = []
    open_: set[str] = set()
    for el in body.iter():
        tag = el.tag
        if tag == qn("w:p"):
            chars.append("\n")
            cov.append(False)
        elif tag == qn("w:commentRangeStart"):
            open_.add(el.get(qn("w:id")))
        elif tag == qn("w:commentRangeEnd"):
            open_.discard(el.get(qn("w:id")))
        elif tag == qn("w:delText") or (
            tag == qn("w:t") and not any(a.tag == qn("w:ins") for a in el.iterancestors())
        ):
            for ch in el.text or "":
                chars.append(ch)
                cov.append(bool(open_))
    folded, idx = _fold_chars("".join(chars))
    want = fold(needle)
    found = full = touched = 0
    start = 0
    while want and (k := folded.find(want, start)) >= 0:
        start = k + 1
        # whole tokens only: "20" is not an occurrence inside "2026"
        if (want[0].isalnum() and k > 0 and folded[k - 1].isalnum()) or (
            want[-1].isalnum() and k + len(want) < len(folded) and folded[k + len(want)].isalnum()
        ):
            continue
        found += 1
        flags = [cov[idx[x]] for x in range(k, k + len(want))]
        full += all(flags)
        touched += any(flags)
    return found, full, touched


def count_in_docx(docx_path: Path, needle: str) -> int:
    return comment_coverage(docx_path, needle)[0]


def docx_text(docx_path: Path, *, accepted: bool) -> str:
    """The body's text with its tracked changes rejected (the document's own words) or, with
    ``accepted``, accepted - as a reviewer reads it after "accept all"."""
    with zipfile.ZipFile(docx_path) as z:
        root = etree.fromstring(z.read("word/document.xml"))
    out: list[str] = []
    for el in root.find(qn("w:body")).iter():
        if el.tag == qn("w:p"):
            out.append("\n")
        elif el.tag in (qn("w:t"), qn("w:delText")):
            inserted = any(a.tag == qn("w:ins") for a in el.iterancestors())
            if (el.tag == qn("w:delText") and accepted) or (inserted and not accepted):
                continue
            out.append(el.text or "")
    return "".join(out)


def whole_count(text: str, needle: str) -> int:
    """How often ``needle`` stands in ``text`` as whole tokens, folded."""
    folded, want = fold(text), fold(needle)
    n = start = 0
    while want and (k := folded.find(want, start)) >= 0:
        start = k + 1
        if (want[0].isalnum() and k > 0 and folded[k - 1].isalnum()) or (
            want[-1].isalnum() and k + len(want) < len(folded) and folded[k + len(want)].isalnum()
        ):
            continue
        n += 1
    return n


def _num(s: str | None) -> float | None:
    if not s:
        return None
    m = re.search(r"\d[\d  ,.]*", s)
    return amount_value(m.group(0).strip(" ,.")) if m else None


def value_match(want: str, got: str | None) -> bool:
    if got is None:
        return False
    if fold(want) and fold(want) in fold(got):
        return True
    if re.fullmatch(r"[\d  ,.]+", want.strip()):
        a, b2 = _num(want), _num(got)
        return a is not None and a == b2
    return False


def fsum(f: Any) -> dict[str, Any]:
    return {
        "rule": f.rule_id,
        "severity": f.severity,
        "expected": f.expected,
        "actual": f.actual,
        "quote": f.quote,
        "needs_review": getattr(f, "needs_review", False),
        "message_en": f.message_en,
    }


def _iban_valid(code: str | None) -> bool:
    """Whether ``code`` is an IBAN whose ISO 13616 checksum holds."""
    s = re.sub(r"\s", "", code or "")
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", s):
        return False
    return int("".join(str(int(c, 36)) for c in s[4:] + s[:4])) % 97 == 1


def score_aoa(case: Case, findings: list[Any], annotated: Path) -> dict[str, Any]:
    base = Counter(f.rule_id for f in case.base.findings)
    got = Counter(f.rule_id for f in findings)
    exp_rules = {r for e in case.expects for r in e.rules}
    extra = got - base
    for r in list(extra):
        if r in exp_rules or r in case.allowed:
            del extra[r]
    missing = base - got
    for r in list(missing):
        if r in exp_rules:
            del missing[r]
    # a digit edit that happens to make the act's account pass its checksum takes the base's
    # checksum note with it: that is the note being right, not a finding lost
    if "AOA.IBAN_CHECKSUM" in missing and any(
        f.rule_id == "AOA.BANK" and _iban_valid(f.actual) for f in findings
    ):
        del missing["AOA.IBAN_CHECKSUM"]
    out: dict[str, Any] = {
        "extra": sorted(extra.elements()),
        "missing_base": sorted(missing.elements()),
    }
    out["fp"] = bool(extra or missing)
    if not case.expects:
        return out
    det, corr, occ, track, notes = [], [], [], [], []
    original = accepted = None
    for e in case.expects:
        cands = [f for f in findings if f.rule_id in e.rules]
        if e.text is not None:
            cands = [
                f for f in cands if fold(e.text) in fold(f.quote) or fold(e.text) in fold(f.actual)
            ]
        det.append(bool(cands))
        if not cands:
            fired = [f.quote or f.actual for f in findings if f.rule_id in e.rules]
            notes.append(
                f"{'/'.join(e.rules)} fired on {fired!r}, not on {e.text!r}"
                if fired
                else f"missed {'/'.join(e.rules)} on {e.text!r}"
            )
        if e.corrections:
            ok = any(fold(f.expected) in {fold(c) for c in e.corrections} for f in cands)
            corr.append(ok)
            if cands and not ok:
                notes.append(f"correction {[f.expected for f in cands]!r} != {e.corrections[0]!r}")
        if e.occurrences is not None and e.text is not None:
            _found, full, touched = comment_coverage(annotated, e.text)
            occ.append(full == e.occurrences)
            if full != e.occurrences:
                notes.append(
                    f"comments cover {full}/{e.occurrences} (touch {touched}) of {e.text!r}"
                )
        # the tracked change: after "accept all", every wrong place reads the correction
        if e.corrections and e.text and e.occurrences and any(f.replacement for f in cands):
            if original is None or accepted is None:
                original = docx_text(annotated, accepted=False)
                accepted = docx_text(annotated, accepted=True)
            fixed = whole_count(original, e.text) - whole_count(accepted, e.text)
            written = max(
                whole_count(accepted, c) - whole_count(original, c) for c in e.corrections
            )
            track.append(fixed == e.occurrences and written >= e.occurrences)
            if not track[-1]:
                notes.append(
                    f"tracked changes fix {fixed}/{e.occurrences} of {e.text!r}, write the "
                    f"correction {written} time(s)"
                )
    out.update(
        detected=all(det),
        correction=all(corr) if corr else None,
        occurrences=all(occ) if occ else None,
        tracked=all(track) if track else None,
        notes=notes,
    )
    return out


def substantive(report: Any) -> list[Any]:
    return [f for f in report.findings if f.severity != "info" and f.rule_id != "CMP.FORMATTING"]


def score_compare(case: Case, report: Any) -> dict[str, Any]:
    subs = substantive(report)
    ce = case.cmp
    summ = [f"{f.rule_id}:{f.expected!r}->{f.actual!r}" for f in subs]
    if ce.mode == "none":
        return {"exact": not subs, "got": summ}
    if ce.mode == "text_only":
        ok = all(f.rule_id.startswith("CMP.TEXT_") or f.rule_id in ce.extra_ok for f in subs)
        return {"exact": ok, "got": summ}
    if not 1 <= len(subs) <= ce.max_count:
        return {"exact": False, "got": summ}
    if ce.max_count > 1:
        return {"exact": all(f.rule_id in ce.rules for f in subs), "got": summ}
    f = subs[0]
    ok = f.rule_id in ce.rules
    if ok and ce.before:
        ok = any(
            value_match(w, f.expected) or value_match(w, f.quote if ce.removed else None)
            for w in ce.before
        )
    if ok and ce.removed:
        ok = not f.actual
    elif ok and ce.after:
        ok = any(value_match(w, f.actual) or value_match(w, f.quote) for w in ce.after)
    if ok and ce.labels:
        deltas = [d for h in getattr(report, "hunks", []) for d in getattr(h, "deltas", [])]
        deltas += list(getattr(report, "field_deltas", []))
        ok = any(
            getattr(d, "label_en", None) in ce.labels
            and (not ce.before or any(value_match(w, d.left) for w in ce.before))
            and (ce.removed or not ce.after or any(value_match(w, d.right) for w in ce.after))
            for d in deltas
        )
    return {"exact": ok, "got": summ}


# --------------------------------------------------------------------------------------------
# running a case
# --------------------------------------------------------------------------------------------


def run_case(case: Case, settings: Settings, work: Path, tag: str) -> dict[str, Any]:
    b = case.base
    doc = Document(str(b.act))
    if case.custom is not None:
        applied = case.custom(doc)
        want_applied = case.n_applied
    else:
        applied = apply_edits_docx(paragraphs(doc), case.edits)
        want_applied = len(case.edits)
    mut = work / f"{tag}.docx"
    doc.save(str(mut))
    res: dict[str, Any] = {
        "class": case.cls,
        "variant": case.variant,
        "base": b.alias,
        "summary": case.summary,
    }
    with zipfile.ZipFile(mut) as z:  # distinct mutated documents, for the "uniq" column
        res["digest"] = hashlib.sha256(z.read("word/document.xml")).hexdigest()[:16]
    problems = []
    if want_applied is not None and applied != want_applied:
        problems.append(f"applied {applied} of {want_applied}")
    mut_texts = docx_texts(mut)
    if case.validator is not None:
        if not case.validator(b.texts, mut_texts):
            problems.append("validator rejected the mutated text")
    else:
        exp = (
            case.expected_texts(b.texts)
            if case.expected_texts
            else apply_edits_text(b.texts, case.edits)
        )
        if exp != mut_texts:
            bad = [k for k, (x, y) in enumerate(zip(exp, mut_texts, strict=False)) if x != y]
            problems.append(
                f"mutated text differs from plan (len {len(exp)}/{len(mut_texts)}, paras {bad[:3]})"
            )
    act_text, segments = parse(mut, settings)
    ftext = fold(act_text)
    for s in case.must_parse:
        if fold(s) not in ftext:
            problems.append(f"not in the Docling parse: {s!r}")
    for s in case.must_not_parse:
        if fold(s) in ftext:
            problems.append(f"still in the Docling parse: {s!r}")
    for e in case.expects:
        if e.text is not None and e.occurrences is not None:
            n = count_in_docx(mut, e.text)
            if n != e.occurrences:
                problems.append(f"{e.text!r} occurs {n}x, planned {e.occurrences}")
    res["mutated_paragraphs"] = (
        [(x, y) for x, y in zip(b.texts, mut_texts, strict=False) if x != y][:6]
        if len(b.texts) == len(mut_texts)
        else "structure changed"
    )
    if problems:
        res["invalid"] = problems
        res["path"] = str(mut)
        return res
    report = check_aoa(
        aoa_text=act_text, contract_text=b.contract_text, speller=open_speller(settings)
    )
    findings = list(report.findings)
    annotated = work / f"{tag}.annotated.docx"
    annotate_docx(mut, findings, annotated, text=act_text)
    res["aoa"] = score_aoa(case, findings, annotated)
    res["findings"] = [fsum(f) for f in findings]
    res["expects"] = [e.__dict__ for e in case.expects]
    from procurement_agent.checks.compare import compare

    crep = compare(
        b.segments,
        segments,
        left_document_id="L",
        right_document_id="R",
        left_kind="docx",
        right_kind="docx",
    )
    res["compare"] = score_compare(case, crep)
    res["path"] = str(mut)
    res["annotated"] = str(annotated)
    return res


def failed(r: dict[str, Any]) -> bool:
    if "invalid" in r:
        return True
    a = r["aoa"]
    return (
        a["fp"]
        or a.get("detected") is False
        or a.get("correction") is False
        or a.get("occurrences") is False
        or a.get("tracked") is False
        or not r["compare"]["exact"]
    )


def pct(xs: list[bool | None]) -> str:
    ys = [x for x in xs if x is not None]
    return f"{100 * sum(ys) / len(ys):5.0f}%" if ys else "     -"


def aggregate(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    agg: dict[str, dict[str, Any]] = {}
    for cls in GENERATORS:
        rs = [r for r in results if r["class"] == cls]
        if not rs:
            continue
        ok = [r for r in rs if "invalid" not in r and "na" not in r]

        def rate(key: str, sub: str = "aoa", ok: list[dict[str, Any]] = ok) -> float | None:
            ys = [r[sub].get(key) for r in ok if r[sub].get(key) is not None]
            return round(sum(bool(y) for y in ys) / len(ys), 4) if ys else None

        agg[cls] = {
            "n": len(ok),
            "uniq": len({(r["base"], r.get("digest")) for r in ok}),
            "invalid": sum("invalid" in r for r in rs),
            "not_applicable": sum("na" in r for r in rs),
            "detected": rate("detected"),
            "correction": rate("correction"),
            "occurrences": rate("occurrences"),
            "tracked": rate("tracked"),
            "false_positive": rate("fp"),
            "compare_exact": rate("exact", "compare"),
        }
    return agg


def redact(r: dict[str, Any], bases: dict[str, Base]) -> bool:
    return not bases[r["base"]].synthetic


def fail_reason(r: dict[str, Any], hide: bool) -> str:
    if "invalid" in r:
        return "INVALID " + (
            "; ".join(r["invalid"]) if not hide else f"{len(r['invalid'])} problem(s)"
        )
    parts = []
    a = r["aoa"]
    if not hide:
        parts += a.get("notes", [])
    else:
        if a.get("detected") is False:
            rules = sorted({x for e in r["expects"] for x in e["rules"]})
            fired = any(f["rule"] in rules for f in r["findings"])
            parts.append(
                ("fired but not on every expected text: " if fired else "missed ") + ",".join(rules)
            )
        if a.get("correction") is False:
            parts.append("wrong correction")
        if a.get("occurrences") is False:
            parts.append("occurrences wrong")
        if a.get("tracked") is False:
            parts.append("tracked change wrong")
    if a["extra"]:
        parts.append(f"extra {a['extra']}")
    if a["missing_base"]:
        parts.append(f"base finding gone {a['missing_base']}")
    if not r["compare"]["exact"]:
        got = r["compare"]["got"]
        parts.append(
            f"compare {got!r}" if not hide else f"compare {[g_.split(':')[0] for g_ in got]}"
        )
    return "; ".join(parts)


# --------------------------------------------------------------------------------------------
# calibration on the real planted acts
# --------------------------------------------------------------------------------------------


def _answer_key(folder: Path) -> dict[str, tuple[set[str], set[str], list[str]]]:
    key_path = folder.parent / "ANSWER_KEY.md"
    out: dict[str, tuple[set[str], set[str], list[str]]] = {}
    if not key_path.exists():
        return out
    for line in key_path.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or not re.fullmatch(r"\d{2}_\w+", cells[0]):
            continue
        errors = set(re.findall(r"AOA\.[A-Z_]+", cells[2]))
        other = set(re.findall(r"AOA\.[A-Z_]+", cells[3]))
        notes = []
        if "AOA.PERIOD_IN_TERM" in errors and "AOA.ACT_DATE" in errors:
            errors.discard("AOA.ACT_DATE")
            notes.append(
                "GOLD_SPEC s3 override: AOA.ACT_DATE dropped (period-year is PERIOD_IN_TERM only)"
            )
        out[cells[0]] = (errors, other, notes)
    return out


def _client_gold(act: Path) -> tuple[set[str], set[str]] | None:
    import yaml

    for gp in sorted((ROOT / "samples" / "gold").glob("aoa_*.yaml")):
        data = yaml.safe_load(gp.read_text(encoding="utf-8")) or {}
        if (ROOT / str(data.get("document", ""))).resolve() == act.resolve():
            return set(data.get("expected_findings") or []), set(
                data.get("forbidden_findings") or []
            )
    return None


def word_fragments(a: list[str], b2: list[str]) -> list[tuple[str, str]]:
    ta = " \n ".join(a).split()
    tb = " \n ".join(b2).split()
    strip = " ,.;:()„“”\"'«»"
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        out.append((" ".join(ta[i1:i2]).strip(strip), " ".join(tb[j1:j2]).strip(strip)))
    return out


_BIC_OK = re.compile(r"[A-Z]{4}GE[A-Z0-9]{2}(?:[A-Z0-9]{3})?")
_FOREIGN = re.compile(rf"[{KA}][A-Za-zЀ-ӿ]|[A-Za-zЀ-ӿ][{KA}]")


def classify_fragment(old: str, new: str, f: Facts) -> set[str]:
    """The rule(s) GOLD_SPEC section 3 assigns to one word-diff fragment of a real planted act.

    Only the mistake kinds the planted sets use are recognised; anything else is "?" and is
    left to the set's legacy rule list.
    """
    if _FOREIGN.search(new) and not _FOREIGN.search(old):
        return {"AOA.SPELLING"}
    if f.bic and (old == f.bic or _BIC_OK.fullmatch(old or "")):
        return {"AOA.BANK"} | (set() if _BIC_OK.fullmatch(new) else {"AOA.BANK_CODE_FORMAT"})
    if f.iban and old == f.iban:
        return {"AOA.BANK"}
    if old in f.ids:
        return {"AOA.ID_CODES"}
    if f.cadastral and old == f.cadastral:
        return {"AOA.CADASTRAL"}
    if f.fee and (
        old and (old in f.fee["digits"] or old in f.fee["words"] or f.fee["digits"] in old)
    ):
        if not re.search(r"\d", new):  # the words alone: misspelt or another number
            return {"AOA.AMOUNT_WORDS"}
        if re.fullmatch(r"[\d  ,.]+", new):
            return {"AOA.FEE", "AOA.AMOUNT_WORDS"}
        return {"AOA.FEE"}
    words_old = old.split()
    if any(w in MONTHS for w in words_old):
        if not any(w in MONTHS for w in new.split()):
            return {"AOA.SPELLING"}
        if f.act_date and old in f.act_date.text:
            return {"AOA.ACT_DATE"}
    if f.period and re.fullmatch(r"\d{4}", old) and old in f.period[0].text:
        return {"AOA.PERIOD_IN_TERM"}
    for d in f.directors:
        if old in (d["first"], *d["forms"]) or any(old in form for form in d["forms"]):
            return {"AOA.DIRECTOR"}
    if any(old and old in core for _, core in f.party_names):
        return {"AOA.PARTY_NAME"}
    if any(old and old in a for a in f.addresses):
        return {"AOA.PARTY_ADDRESS"}
    if any(old and old in sp.text for sp in f.project_forms):
        return {"AOA.PROJECT_NAME"}
    return {"?"}


def calibrate(settings: Settings, work: Path) -> list[dict[str, Any]]:
    from procurement_agent.checks.compare import compare

    rows = []
    print("\n== calibration: real planted acts vs their answer keys ==")
    print("key = ANSWER_KEY.md rows (generated set) or the word diff against the correct act, each")
    print(
        "fragment classified by GOLD_SPEC s3 (+ the legacy samples/gold rule list) for the client set"
    )
    for folder in base_folders():
        corrects = sorted(folder.glob("*Correct*.docx"))
        contracts = sorted(folder.glob("*ontract*.docx"))
        if not corrects or not contracts:
            continue
        synthetic = (folder.parent / "ANSWER_KEY.md").exists()
        alias = re.sub(r"[^a-z0-9]+", "-", folder.parent.name.lower()).strip("-")
        key = _answer_key(folder)
        ctext, _ = parse(contracts[0], settings)
        base_texts = docx_texts(corrects[0])
        facts = discover(base_texts)
        base_text, base_segs = parse(corrects[0], settings)
        base_rep = check_aoa(
            aoa_text=base_text, contract_text=ctext, speller=open_speller(settings)
        )
        base_info = {f.rule_id for f in base_rep.findings if f.severity == "info"}
        acts = sorted(p for p in folder.glob("*.docx") if "_AoA_" in p.name)
        for act in acts:
            short = act.stem.split("_AoA_", 1)[1]
            atext, segs = parse(act, settings)
            rep = check_aoa(aoa_text=atext, contract_text=ctext, speller=open_speller(settings))
            fired = {f.rule_id for f in rep.findings}
            info = {f.rule_id for f in rep.findings if f.severity == "info"}
            notes: list[str] = []
            frags = (
                [
                    (o, n)
                    for o, n in word_fragments(base_texts, docx_texts(act))
                    if fold(o) != fold(n)
                ]
                if act != corrects[0]
                else []
            )
            derived: set[str] = set()
            for o, n in frags:
                derived |= classify_fragment(o, n, facts)
            if synthetic:
                kk = short.split("_")[0] + "_" + short.split("_", 1)[1] if "_" in short else short
                errors, other, notes = key.get(kk, (None, set(), []))
                if errors is None:
                    continue
                expected, allowed = errors | {r for r in other if "SPELLING" in r}, set(other)
            else:
                gold = _client_gold(act)
                legacy = gold[0] if gold else set()
                expected, allowed = (derived - {"?"}) | legacy, set()
                if legacy - derived:
                    notes.append(f"legacy rule list adds {sorted(legacy - derived)}")
                if (derived - {"?"}) - legacy:
                    notes.append(
                        f"word diff adds {sorted((derived - {'?'}) - legacy)} (GOLD_SPEC s3)"
                    )
            if "?" in derived:
                notes.append("some diff fragments are not classified")
            # an info note the correct act also carries (a test IBAN failing its checksum) is baseline
            allowed |= base_info
            missing = expected - fired
            extra = fired - expected - allowed
            annotated = work / f"cal-{alias}-{len(rows)}.docx"
            annotate_docx(act, rep.findings, annotated, text=atext)
            det = corr = occ = 0
            for old, new in frags:
                if not new:
                    det += bool(rep.findings)
                    continue
                hits = [
                    f
                    for f in rep.findings
                    if fold(new) in fold(f.quote)
                    or fold(new) in fold(f.actual)
                    or (len(fold(f.quote)) >= 3 and fold(f.quote) in fold(new))
                ]
                det += bool(hits)
                corr += any(old and fold(old) in fold(f.expected) for f in hits)
                found, full, _ = comment_coverage(annotated, new)
                occ += found > 0 and full == found
            crep = compare(
                base_segs,
                segs,
                left_document_id="L",
                right_document_id="R",
                left_kind="docx",
                right_kind="docx",
            )
            subs = substantive(crep)
            ccov = sum(
                1
                for old, new in frags
                if any(
                    new and (fold(new) in fold(f.actual) or fold(new) in fold(f.quote))
                    for f in subs
                )
            )
            row = {
                "set": alias,
                "act": short,
                "expected": sorted(expected),
                "fired": sorted(fired),
                "missing": sorted(missing),
                "extra": sorted(extra),
                "info": sorted(info - expected),
                "agree": not missing and not (extra - info),
                "fragments": len(frags),
                "frag_detected": det,
                "frag_correction": corr,
                "frag_occurrences": occ,
                "compare_substantive": len(subs),
                "compare_covers": ccov,
                "notes": notes,
            }
            rows.append(row)
    print(
        f"{'set':<12} {'act':<20} {'agree':<5} {'missing':<30} {'extra (non-info)':<30} frags det/corr/occ  cmp subst/cover"
    )
    for r in rows:
        extra = [x for x in r["extra"] if x not in r["info"]]
        print(
            f"{r['set']:<12} {r['act'][:20]:<20} {'yes' if r['agree'] else 'NO':<5} "
            f"{','.join(x[4:] for x in r['missing'])[:30]:<30} {','.join(x[4:] for x in extra)[:30]:<30} "
            f"{r['fragments']:>5}: {r['frag_detected']}/{r['frag_correction']}/{r['frag_occurrences']}"
            f"{'':<9} {r['compare_substantive']:>2}/{r['compare_covers']}"
        )
        for n in r["notes"]:
            print(f"{'':<14}note: {n}")
    agree = sum(r["agree"] for r in rows)
    tf = sum(r["fragments"] for r in rows)
    print(
        f"rule agreement: {agree}/{len(rows)} acts; diff fragments detected "
        f"{sum(r['frag_detected'] for r in rows)}/{tf}, correction {sum(r['frag_correction'] for r in rows)}/{tf}, "
        f"comments {sum(r['frag_occurrences'] for r in rows)}/{tf}, compare covers {sum(r['compare_covers'] for r in rows)}/{tf}"
    )
    return rows


# --------------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------------


def git_state() -> dict[str, Any]:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", "src"], cwd=ROOT, capture_output=True, text=True
        ).stdout
        return {"head": head, "src_dirty": sorted(line[3:] for line in dirty.splitlines())}
    except OSError:
        return {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--per-class", type=int, default=4)
    ap.add_argument("--classes", default="", help="comma-separated class names (default: all)")
    ap.add_argument(
        "--out", default=None, help="aggregate JSON (default eval_runs/mutate_aoa-<seed>.json)"
    )
    ap.add_argument("--calibrate", action="store_true", help="score the real planted acts first")
    ap.add_argument(
        "--keep-failures", default=None, help="dir for failing mutated documents + details"
    )
    ap.add_argument("--show", action="store_true", help="print each case's mutated paragraphs")
    ap.add_argument(
        "--show-client-text", action="store_true", help="print client-set text in failures"
    )
    ap.add_argument("--list-classes", action="store_true")
    args = ap.parse_args(argv)
    if args.list_classes:
        print("\n".join(GENERATORS))
        return 0
    classes = [c.strip() for c in args.classes.split(",") if c.strip()] or list(GENERATORS)
    unknown = [c for c in classes if c not in GENERATORS]
    if unknown:
        ap.error(f"unknown classes: {unknown}")
    import logging

    logging.disable(logging.WARNING)
    settings = Settings(env="dev", docling_artifacts_path=ROOT / "models" / "docling")
    keep = (
        # absolute, not resolved: samples/ may be a symlink (a worktree), and a resolved path
        # would no longer sit under ROOT
        Path(args.keep_failures).absolute()
        if args.keep_failures
        else ROOT / "samples" / "mutations" / "aoa" / f"seed-{args.seed}"
    )
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="mutate_aoa-") as tmp:
        work = Path(tmp)
        if args.calibrate:
            calibrate(settings, work)
            if not args.classes and "--per-class" not in " ".join(argv or sys.argv):
                return 0
        bases = load_bases(settings)
        by_alias = {b.alias: b for b in bases}
        if not bases:
            print("no base cases found under samples/*/AOA and Contract/")
            return 1
        print(
            f"\nbases: {', '.join(f'{b.alias} ({len(b.findings)} base findings)' for b in bases)}"
        )
        results: list[dict[str, Any]] = []
        for cls in classes:
            for k in range(args.per_class):
                b = bases[k % len(bases)]
                rng = random.Random(f"{args.seed}-{cls}-{k}")  # noqa: S311 - reproducible test data
                tag = f"{cls}-{k}-{b.alias}"
                try:
                    case = GENERATORS[cls](b, rng)
                except NotApplicableError as e:
                    results.append({"class": cls, "base": b.alias, "na": str(e), "variant": "-"})
                    continue
                r = run_case(case, settings, work, tag)
                r["tag"] = tag
                results.append(r)
                if args.show:
                    hide = not b.synthetic and not args.show_client_text
                    print(
                        f"--- {tag} [{case.variant}] {'(client text hidden)' if hide else case.summary}"
                    )
                    if not hide and isinstance(r.get("mutated_paragraphs"), list):
                        for x, y in r["mutated_paragraphs"]:
                            for old, new in word_fragments([x], [y]):
                                print(f"    {old!r} -> {new!r}")
        agg = aggregate(results)
        print(
            f"\n== mutation results (seed {args.seed}, {args.per_class} per class, {time.time() - t0:.0f}s) =="
        )
        print(
            f"{'class':<26} {'n':>3} {'uniq':>4} {'inv':>3} {'na':>3} {'detect':>6} {'corr':>6} {'occ':>6} {'track':>6} {'fp':>6} {'cmp':>6}"
        )
        for cls, a in agg.items():
            f = lambda x: "     -" if x is None else f"{100 * x:5.0f}%"  # noqa: E731
            print(
                f"{cls:<26} {a['n']:>3} {a['uniq']:>4} {a['invalid']:>3} {a['not_applicable']:>3} {f(a['detected']):>6} "
                f"{f(a['correction']):>6} {f(a['occurrences']):>6} {f(a['tracked']):>6} {f(a['false_positive']):>6} {f(a['compare_exact']):>6}"
            )
        print("\n== failure examples (up to 3 per class) ==")
        for cls in agg:
            fails = [r for r in results if r["class"] == cls and "na" not in r and failed(r)]
            for r in fails[:3]:
                hide = redact(r, by_alias) and not args.show_client_text
                print(f"  {r['tag']} [{r['variant']}] {fail_reason(r, hide)}")
        # details and failing documents stay under samples/ (they carry client values)
        keep.mkdir(parents=True, exist_ok=True)
        for r in results:
            if "path" in r and failed(r):
                for key in ("path", "annotated"):
                    if key in r and Path(r[key]).exists():
                        dst = keep / Path(r[key]).name
                        dst.write_bytes(Path(r[key]).read_bytes())
                        r[key] = str(dst.relative_to(ROOT) if dst.is_relative_to(ROOT) else dst)
            elif "path" in r:
                r.pop("path", None)
                r.pop("annotated", None)
        (keep / "cases.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
        )
        out = Path(args.out) if args.out else ROOT / "eval_runs" / f"mutate_aoa-{args.seed}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        by_base = {b.alias: aggregate([r for r in results if r["base"] == b.alias]) for b in bases}
        payload = {
            "generated": datetime.now(UTC).isoformat(timespec="seconds"),
            "seed": args.seed,
            "per_class": args.per_class,
            "git": git_state(),
            "classes": agg,
            "by_base": by_base,
        }
        out.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        print(f"\naggregates: {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")
        print(
            f"details + failing documents: {keep.relative_to(ROOT) if keep.is_relative_to(ROOT) else keep}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
