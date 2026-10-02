"""A party's extract from the business registry (NAPR's „ამონაწერი“): client rule 1, offline.

The client's first rule checks each party's name, signatories and legal address against the
business registry, by its identification code. The registry has no API and this system no
internet, so the evidence is the extract a user downloads from the National Agency of Public
Registry and uploads beside the act. This module reads one into a ``RegistryEntry``.

Two layouts are known, and the reader is written for neither in particular. An extract of 2011
writes "საიდენტიფიკაციო კოდი" and lists each authorised person as "<personal number>, <name>,"
with the position on the line after; one of 2025 writes "საიდენტიფიკაციო ნომერი", heads the list
with the position and writes "<name>, <personal number> ,ერთპიროვნული". Fields are therefore read
by their labels, in any order, a label's words allowed to wrap onto two lines and a value running
on until the next label or section heading; persons are found by their eleven-digit personal
numbers inside the section of those authorised to manage and represent the company - never in
the partners' or founders' lists beside it - and a person's position is taken from the side the
section writes positions on.

A PDF is read from its text layer with pdfium, in the order the page draws it, which keeps a label
and its value on one line; Docling's reading of the same page puts every label before every value
and cannot be read this way. A file that is not an extract - no extract title, or no
identification code - is refused (``NotAnExtractError``) rather than read as one.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from procurement_agent.extraction.normalize import normalize_spaces
from procurement_agent.registry.snapshot import RegistryEntry, normalize_code

__all__ = [
    "NotAnExtractError",
    "Person",
    "parse_extract",
    "persons",
    "read_extract",
    "text_of_file",
]

#: The pages of an extract read at most; a company's extract runs to a few pages.
MAX_PAGES = 30
#: The most text read from one file.
MAX_CHARS = 400_000

#: The extract's title: "ამონაწერი მეწარმეთა და არასამეწარმეო (არაკომერციული) იურიდიული პირების
#: რეესტრიდან" - the first words are enough, whatever the wrapping.
_TITLE = re.compile(r"ამონაწერი\s+მეწარმეთა\s+და\s+არასამეწარმეო")


def _label(*words: str) -> str:
    """A label whose words may wrap onto the next line, followed by its colon."""
    return r"\s+".join(words) + r"\s*:"


#: The labels whose values are read, each with every wording seen.
_FIELDS: dict[str, tuple[str, ...]] = {
    "name": (_label("საფირმო", "სახელწოდება"),),
    "legal_form": (_label("სამართლებრივი", "ფორმა"),),
    "id_code": (_label("საიდენტიფიკაციო", "(?:კოდი|ნომერი)"),),
    "address": (_label("იურიდიული", "მისამართი"),),
    "prepared": (
        _label("ამონაწერის", "მომზადების", "თარიღი"),
        _label("განაცხადის", "რეგისტრაციის", "ნომერი,", "მომზადების", "თარიღი"),
    ),
}
#: Labels that are read for nothing but where they end the value before them.
_OTHER_LABELS: tuple[str, ...] = (
    _label("განაცხადის", "ნომერი"),
    _label("განაცხადის", "რეგისტრაციის", "ნომერი"),
    _label("სახელმწიფო", "რეგისტრაციის", "(?:თარიღი|ნომერი)"),
    _label("რეგისტრაციის", "ნომერი,", "თარიღი"),
    _label("რეგისტრაციის", "(?:ნომერი|თარიღი)"),
    _label("მარეგისტრირებელი", "ორგანო"),
    _label("საგადასახადო", "ინსპექცია"),
    _label("ელექტრონული", "ფოსტა"),
    _label("ფაქტობრივი", "მისამართი"),
)
#: Section headings, in either layout: each ends the field or section before it.
_SECTION_WORDS: dict[str, str] = {
    "subject": r"სუბიექტი",
    "history": r"რეორგანიზაციის\s+ისტორია",
    "status": r"ინფორმაცია\s+ლიკვიდაციის\s*/\s*რეორგანიზაციის\s*/\s*გადახდისუუნარობის\s+"
    r"პროცესის\s+მიმდინარეობის\s+შესახებ",
    "governance": r"მმართველობის\s+ორგანო",
    "representation": r"ხელმძღვანელობა(?:ზე)?\s*/\s*წარმომადგენლობა(?:ზე)?"
    r"(?:\s+უფლებამოსილი\s+პირები)?",
    "founders": r"დამფუძნებლები",
    "partners": r"პარტნიორები",
    "capital": r"კაპიტალი(?!\s+არ)",
    "obligations": r"ვალდებულება",
    "seizure": r"ყადაღა\s*/\s*აკრძალვა",
    "tax_lien": r"საგადასახადო\s+გირავნობა",
    "pledge": r"მოძრავ\s+ნივთებსა",
    "debtors": r"მოვალეთა\s+რეესტრი",
    "footer": r"(?:საჯარო\s+რეესტრის\s+ეროვნული\s+სააგენტო\.\s*)?http://public\.reestri\.gov\.ge",
}
_SECTIONS = re.compile("|".join(f"(?P<{key}>{pattern})" for key, pattern in _SECTION_WORDS.items()))
_ALL_LABELS = re.compile(
    "|".join(f"(?:{p})" for p in (*(p for ps in _FIELDS.values() for p in ps), *_OTHER_LABELS))
)
#: "რეგისტრირებული არ არის": the section states nothing (no liquidation, no seizure ...).
_NOTHING = re.compile(r"^\W*რეგისტრირებული\s+არ\s+არის\W*$")
_PERSONAL_NUMBER = re.compile(r"(?<!\d)\d{11}(?!\d)")
_DATE = re.compile(r"(?<!\d)(\d{2})[/.](\d{2})[/.](\d{4})(?!\d)")
_GEORGIAN_WORD = re.compile(r"[ა-ჰ]{2,}(?:-[ა-ჰ]+)?")
#: How a person's sole or joint representation is written after the 2025 layout's number.
_REPRESENTATION = re.compile(r"ერთპიროვნ[ა-ჰ]*|ერთობლივ[ა-ჰ]*")


class NotAnExtractError(ValueError):
    """The file is no business-registry extract: no title, or no identification code."""


@dataclass(frozen=True, slots=True)
class Person:
    """One person authorised to manage or represent the party, as the extract lists them."""

    name: str
    personal_number: str
    position: str | None
    representation: str | None = None


# --- reading the file -------------------------------------------------------------------------


def _pdf_text(path: Path) -> str:
    import pypdfium2 as pdfium  # imported here: only an extract upload needs it

    from procurement_agent.ingestion.pdfium_lock import PDFIUM_LOCK

    parts: list[str] = []
    with PDFIUM_LOCK:  # pdfium is not thread-safe, not even across documents
        pdf = pdfium.PdfDocument(str(path))
        try:
            for i in range(min(len(pdf), MAX_PAGES)):
                page = pdf[i]
                textpage = page.get_textpage()
                try:
                    parts.append(textpage.get_text_range())
                finally:
                    textpage.close()
                    page.close()
                if sum(map(len, parts)) > MAX_CHARS:
                    break
        finally:
            pdf.close()
    return "\n".join(parts).replace("\r\n", "\n").replace("\r", "\n")[:MAX_CHARS]


def _docx_text(path: Path) -> str:
    from docx import Document
    from docx.opc.exceptions import PackageNotFoundError

    from procurement_agent.report.docx_annotate import paragraphs_in_order

    try:
        doc = Document(str(path))
    except (ValueError, KeyError, zipfile.BadZipFile, PackageNotFoundError) as e:
        msg = f"not a Word document: {e}"
        raise NotAnExtractError(msg) from e
    return "\n".join(p.text for p in paragraphs_in_order(doc))[:MAX_CHARS]


def text_of_file(path: Path, kind: str) -> str:
    """The extract's text in its drawing order: a PDF's text layer, or a Word file's paragraphs."""
    if kind == "pdf":
        return _pdf_text(path)
    if kind == "docx":
        return _docx_text(path)
    msg = f"a registry extract is a PDF or a Word file, not {kind}"
    raise NotAnExtractError(msg)


# --- reading the text -------------------------------------------------------------------------


def _markers(text: str) -> list[int]:
    """Where every label and section heading starts: each ends the value before it."""
    found = [m.start() for m in _ALL_LABELS.finditer(text)]
    found += [m.start() for m in _SECTIONS.finditer(text)]
    return sorted(set(found))


def _value_after(text: str, end: int, markers: list[int]) -> str:
    stop = next((m for m in markers if m >= end), len(text))
    return normalize_spaces(text[end:stop].replace("\n", " ")).strip(" ,;")


def _field(text: str, key: str, markers: list[int]) -> str | None:
    for pattern in _FIELDS[key]:
        m = re.search(pattern, text)
        if m is not None:
            value = _value_after(text, m.end(), markers)
            if value:
                return value
    return None


def _section(text: str, key: str) -> str | None:
    """The text under a section heading, up to the next heading; None when there is none."""
    heads = list(_SECTIONS.finditer(text))
    for i, m in enumerate(heads):
        if m.lastgroup == key:
            stop = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            return text[m.end() : stop]
    return None


def _lines(block: str) -> list[str]:
    return [ln.strip(" \t-•,") for ln in block.split("\n") if ln.strip(" \t-•,")]


def _name_beside(line: str, start: int, end: int) -> tuple[str | None, str | None]:
    """The name written beside a personal number on its line, and what follows the 2025 number."""
    before = line[:start].strip(" ,-•")
    after = line[end:].strip(" ,-•")
    if len(_GEORGIAN_WORD.findall(before)) >= 2:
        rep = _REPRESENTATION.search(after)
        return normalize_spaces(before), rep.group(0) if rep else None
    head = after.split(",")[0].strip()
    if len(_GEORGIAN_WORD.findall(head)) >= 2:
        return normalize_spaces(head), None
    return None, None


def _is_position(line: str) -> bool:
    """A line naming a position: words only, no number, short."""
    return not any(ch.isdigit() for ch in line) and 0 < len(line.split()) <= 6


def persons(block: str) -> list[Person]:
    """The authorised persons a representation section lists, in order, with their positions.

    The position is on the line before each person (2025) or the line after (2011): the section
    shows which, by what comes first - a position line before the first person, or not.
    """
    lines = _lines(block)
    numbered = [i for i, ln in enumerate(lines) if _PERSONAL_NUMBER.search(ln)]
    if not numbered:
        return []
    before_style = numbered[0] > 0 and _is_position(lines[numbered[0] - 1])
    out: list[Person] = []
    for i in numbered:
        line = lines[i]
        m = _PERSONAL_NUMBER.search(line)
        if m is None:  # pragma: no cover - ``numbered`` holds only lines that match
            continue
        name, representation = _name_beside(line, m.start(), m.end())
        if name is None:
            continue
        j = i - 1 if before_style else i + 1
        position = None
        if 0 <= j < len(lines) and j not in numbered and _is_position(lines[j]):
            position = normalize_spaces(lines[j].rstrip(" ,"))
        out.append(Person(name, m.group(0), position, representation))
    return out


def _prepared(value: str | None) -> str | None:
    if not value:
        return None
    m = _DATE.search(value)
    if m is None:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
    except ValueError:
        return None


def _status(text: str) -> str | None:
    block = _section(text, "status")
    if block is None:
        return None
    lines = _lines(block)
    if not lines or all(_NOTHING.match(ln) for ln in lines):
        return None
    return normalize_spaces(" ".join(lines))[:200]


def parse_extract(text: str) -> tuple[RegistryEntry, list[Person]]:
    """The registry entry an extract's text states, and its authorised persons in full."""
    if not _TITLE.search(text):
        msg = "no registry extract title (ამონაწერი მეწარმეთა და არასამეწარმეო ...)"
        raise NotAnExtractError(msg)
    markers = _markers(text)
    code = normalize_code(_field(text, "id_code", markers) or "")
    if not code:
        msg = "the extract states no identification code"
        raise NotAnExtractError(msg)
    name = _field(text, "name", markers)
    if not name:
        msg = "the extract states no name"
        raise NotAnExtractError(msg)
    block = _section(text, "representation")
    people = persons(block) if block is not None else []
    entry = RegistryEntry(
        id_code=code,
        name=name,
        address=_field(text, "address", markers),
        directors=tuple(p.name for p in people),
        status=_status(text),
        as_of=_prepared(_field(text, "prepared", markers)),
        legal_form=_field(text, "legal_form", markers),
        source="extract",
        positions=tuple(p.position or "" for p in people),
    )
    return entry, people


def read_extract(path: Path, kind: str) -> RegistryEntry:
    """The registry entry of an uploaded extract file; ``NotAnExtractError`` when it is none."""
    return parse_extract(text_of_file(path, kind))[0]
