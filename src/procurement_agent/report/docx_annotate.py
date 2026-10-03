"""Write findings back into the document as Word comments.

The client asked for exactly this: "გვსურს რომ ფაილშივე/დოკუმენტშივე, მოგვინიშნოს ის ველები
რომელიც საჭიროებს კორექტირებას, და გაუწეროს სწორი ვარიანტი კომენტარის სახით" — mark the fields
needing correction in the file itself and give the correct value as a comment.

Word anchors a comment to whole runs, and Word splits a paragraph into runs wherever formatting
changes, so the phrase we want to mark is usually part of a longer run or spread across several.
The run containing it is therefore split so the comment lands on the words at fault and not on
the whole paragraph - unless the run holds more than text (a picture, a field, a footnote
mark): python-docx rewrites a run's text by deleting everything else in it, so such a run is
marked whole rather than split.

Scanned PDFs cannot carry comments. They are reported through the findings table and the
existing page renders with the box highlighted instead, and ``annotate_docx`` is simply not
called for them — a caller that mixes the two up gets an explicit error, not a silent no-op.
"""

from __future__ import annotations

import copy
import re
import zipfile
from bisect import bisect_left, bisect_right
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from docx import Document
from docx.comments import Comment
from docx.document import Document as DocumentType
from docx.opc.exceptions import PackageNotFoundError
from docx.oxml.ns import qn
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph
from docx.text.run import Run

from procurement_agent.checks.findings import CheckReport, Finding
from procurement_agent.extraction.normalize import QUOTE_CHARS, fold_look_alikes, normalize_spaces
from procurement_agent.report.docx_sanitize import neutralise_package
from procurement_agent.security.audit import log
from procurement_agent.security.safe_errors import ContentFree

AUTHOR = "Procurement Agent"
INITIALS = "PA"

_SEVERITY_PREFIX = {
    "error": "შეცდომა / Error",
    "warning": "გაფრთხილება / Warning",
    "info": "ინფორმაცია / Info",
}
#: Opens the message of a note the rules could not decide (``Finding.needs_review``): the
#: reviewer is to read the marked words, not to correct them.
NEEDS_REVIEW_PREFIX = "გადასამოწმებელი / Needs review:"


class UnsupportedDocumentError(ContentFree, RuntimeError):
    """Raised for inputs Word comments cannot be attached to, e.g. a scanned PDF."""


#: Characters XML 1.0 cannot carry. A finding quotes the checked document, and text that came
#: through OCR or a PDF can hold a stray vertical tab; lxml refuses the whole comment for it.
#: A lone surrogate (half of a character, from a broken text layer or model output) cannot be
#: written in UTF-8 at all, and U+FFFE and U+FFFF are no characters (security review).
_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")

#: The most places one finding is marked in, and the most places marked in one file. An act
#: writes a value a handful of times; a file that repeats it thousands of times would make every
#: comment cost more than the last (python-docx numbers each against all before it). The places
#: past either limit are counted in the note at the top, and a finding cut short gets no tracked
#: change: accepting every change must not leave the act half corrected (security review).
MAX_PLACES_PER_FINDING = 50
MAX_PLACES = 300
#: The most places of one needle a search reads. Past it the search stops: the finding is cut
#: short (``MAX_PLACES_PER_FINDING``), and none of its places is taken for surely its own.
MAX_SEARCHED = 1000


def comment_text(finding: Finding) -> str:
    """What the reviewer reads in the margin: what is wrong, and what it should say."""
    message = finding.message_ka
    if finding.needs_review:
        message = f"{NEEDS_REVIEW_PREFIX} {message}"
    lines = [f"[{_SEVERITY_PREFIX[finding.severity]}] {finding.rule_id}", message]
    if finding.expected:
        lines.append(f"სწორი ვარიანტი / should be: {finding.expected}")
    lines.append(finding.message_en)
    return _XML_ILLEGAL.sub("", "\n".join(lines))


#: Run children ``Run.text``'s setter recreates. Anything else in a run - ``w:drawing``,
#: ``w:fldChar``, ``w:footnoteReference`` - is deleted by it, so only runs made of these split.
#: A carriage return comes back as a line break, so a run with one is not split either.
_TEXT_RUN_CHILDREN = frozenset({"rPr", "t", "tab", "br"})


def _splittable(run: Run) -> bool:
    """Whether ``Run.text`` can rewrite this run without losing anything: only text, tabs and
    line breaks - a page or column break comes back as a line break (red team R6)."""
    for child in run._r:
        tag = child.tag.split("}")[-1]
        if tag not in _TEXT_RUN_CHILDREN:
            return False
        if tag == "br" and child.get(qn("w:type")) not in (None, "textWrapping"):
            return False
    return True


#: What a run of a paragraph's own text may sit in: a link, an inline content control, a smart
#: tag, custom markup, a simple field, an earlier reviewer's insertion. A run in a deletion is
#: no text of the paragraph, and one in a text box is the box's paragraph's.
_RUN_CONTAINERS = frozenset(
    qn(tag)
    for tag in (
        "w:hyperlink",
        "w:sdt",
        "w:sdtContent",
        "w:smartTag",
        "w:customXml",
        "w:fldSimple",
        "w:ins",
    )
)


#: The run children whose text ``Run.text`` reads, in the order they stand.
_RUN_TEXT_TAGS = frozenset(
    qn(tag) for tag in ("w:br", "w:cr", "w:noBreakHyphen", "w:ptab", "w:t", "w:tab")
)


def _run_text(run: Run) -> str:
    """``run.text``, read child by child: python-docx reads it by an XPath query, which a search
    of every place in a file of many runs made the most of its cost (security review). Each
    child's own text is python-docx's, so the two are equal."""
    return "".join(str(e) for e in run._r if e.tag in _RUN_TEXT_TAGS)


def _text_runs(paragraph: Paragraph) -> list[Run]:
    """The runs of a paragraph's own text, in order, those nested in a link or an inline content
    control among them: Docling reads their text, and ``Paragraph.runs`` does not."""
    out: list[Run] = []
    p = paragraph._p
    for r in p.iter(qn("w:r")):
        parent = r.getparent()
        while parent is not None and parent is not p and parent.tag in _RUN_CONTAINERS:
            parent = parent.getparent()
        if parent is p:
            out.append(Run(r, paragraph))
    return out


def _field_runs(runs: Sequence[Run]) -> list[Any]:
    """The run elements of ``runs`` that belong to a field - a simple field's, or a complex
    field's from its ``begin`` to its ``end``: what Word computes, never to be rewritten."""
    inside: list[Any] = []
    depth = 0
    for run in runs:
        r = run._r
        kinds = [c.get(qn("w:fldCharType")) for c in r.findall(qn("w:fldChar"))]
        if "begin" in kinds:
            depth += 1
        if depth > 0 or any(a.tag == qn("w:fldSimple") for a in r.iterancestors()):
            inside.append(r)
        if "end" in kinds and depth > 0:
            depth -= 1
    return inside


def _split_run(run: Run, offset: int) -> Run:
    """Split a run in two at ``offset``, returning the new run holding the leading text."""
    head = copy.deepcopy(run._r)
    run._r.addprevious(head)
    head_run = Run(head, run._parent)
    text = _run_text(run)
    head_run.text = text[:offset]
    run.text = text[offset:]
    return head_run


def _split_run_at(run: Run, offsets: Sequence[int], text: str) -> None:
    """Split a run at every one of ``offsets`` (ascending, each inside its text) in one go, as
    ``_split_run`` at each in turn would: the leading pieces become runs of their own before it,
    the run keeps the last. One copy of the run's formatting serves every piece - a copy of the
    run itself would copy the whole of its text once a piece. ``text`` is the run's text."""
    template = copy.deepcopy(run._r)
    Run(template, run._parent).text = ""
    at = 0
    for offset in offsets:
        head = copy.deepcopy(template)
        run._r.addprevious(head)
        Run(head, run._parent).text = text[at:offset]
        at = offset
    run.text = text[at:]


def _normalised_index(text: str) -> tuple[str, list[int]]:
    """Text with quotes dropped and runs of whitespace collapsed, plus a map back to offsets.

    A finding's value is the cleaned reading of a field - "შპს ნიმუში" - while the
    document writes "შპს „ნიმუში“". Searching the raw text finds nothing, and the comment
    ends up on the document instead of on the words at fault.
    """
    out: list[str] = []
    index: list[int] = []
    prev_space = False
    for i, ch in enumerate(text):
        if ch in QUOTE_CHARS:
            continue
        if ch.isspace():
            if prev_space or not out:
                continue
            out.append(" ")
            index.append(i)
            prev_space = True
        else:
            # a check reads a text whose Cyrillic look-alikes are folded to Latin; the document
            # keeps them, and a word with a stray Cyrillic letter must still be found to be marked
            out.append(fold_look_alikes(ch))
            index.append(i)
            prev_space = False
    return "".join(out), index


def _bounded(text: str, start: int, end: int, needle: str) -> bool:
    """Whether ``text[start:end]`` stands as whole words: a needle that begins (ends) with a
    letter or a digit must not continue a word before it (after it). "ივნის" is not the start
    of "ივნისიდან", and a name in the nominative is not the start of its genitive."""
    if needle[:1].isalnum() and start > 0 and text[start - 1].isalnum():
        return False
    return not (needle[-1:].isalnum() and end < len(text) and text[end].isalnum())


class _Folded:
    """A text's folded form (``_normalised_index``), made when a search first needs it and kept
    for the searches after it - a paragraph's every place, a quote's every place in the checked
    text - by the caller that searches, and gone with it: no document's text outlives a call."""

    __slots__ = ("_text", "_value")

    def __init__(self, text: str) -> None:
        self._text = text
        self._value: tuple[str, list[int]] | None = None

    def get(self) -> tuple[str, list[int]]:
        if self._value is None:
            self._value = _normalised_index(self._text)
        return self._value


def _locate(
    text: str,
    needle: str,
    start: int = 0,
    *,
    whole: bool = False,
    folded: _Folded | None = None,
) -> tuple[int, int] | None:
    """Offsets of ``needle`` in ``text`` at or after ``start``, tolerating quoting and spacing
    differences. With ``whole``, only where it stands as whole words (``_bounded``). ``folded``
    is ``text`` folded, when the caller searches it more than once."""
    at = text.find(needle, start)
    while at >= 0:
        if not whole or _bounded(text, at, at + len(needle), needle):
            return at, at + len(needle)
        at = text.find(needle, at + 1)
    flat, index = (folded or _Folded(text)).get()
    flat_needle, _ = _normalised_index(needle)
    if not flat_needle:
        return None
    flat_start = bisect_left(index, start)  # the first folded character at or after ``start``
    at = flat.find(flat_needle, flat_start)
    while at >= 0:
        found = index[at], index[at + len(flat_needle) - 1] + 1
        if not whole or _bounded(text, *found, flat_needle):
            return found
        at = flat.find(flat_needle, at + 1)
    return None


def whole_word_spans(text: str, needle: str) -> list[tuple[int, int]]:
    """Every place ``needle`` stands as whole words in ``text``, in order - the places a comment
    for it would mark (``_locate``), so a check that names some of them (``Finding.places``)
    counts them the way annotation does."""
    needle = needle.strip()
    out: list[tuple[int, int]] = []
    at = 0
    folded = _Folded(text)
    while needle and (found := _locate(text, needle, at, whole=True, folded=folded)) is not None:
        out.append(found)
        at = found[1]
    return out


def _cover_all(
    paragraph: Paragraph, needle: str, *, whole: bool = False, first: bool = False, limit: int = 0
) -> list[tuple[list[Run], int, int]]:
    """The runs covering each occurrence of ``needle`` in a paragraph (with ``whole``, each
    standing as whole words; with ``first``, the first only), split so they cover it exactly,
    and its offsets. Splitting leaves the paragraph's text unchanged, so offsets stay valid
    between findings.

    Every occurrence is found in the text first, the runs are split at all of them in one pass,
    and each occurrence's runs are then read off: the cost is the paragraph's length, however
    many times the words stand in it - not that length once an occurrence (security review).
    With ``limit``, no more than that many occurrences are read."""
    needle = needle.strip()
    if not needle:
        return []
    runs = _text_runs(paragraph)
    texts = [_run_text(r) for r in runs]
    text = "".join(texts)
    in_fields = _simple_field_spans(runs, texts)
    field_ends = [b for _, b in in_fields]
    spans: list[tuple[int, int]] = []
    folded = _Folded(text)
    located = _locate(text, needle, 0, whole=whole, folded=folded)
    while located is not None:
        if _overlaps(in_fields, field_ends, *located):
            # a simple field holds its result inside itself: no comment can mark it there
            located = _locate(text, needle, located[1], whole=whole, folded=folded)
            continue
        spans.append(located)
        if first or len(spans) == limit:
            break
        located = _locate(text, needle, located[1], whole=whole, folded=folded)
    if not spans:
        return []

    # split the runs that contain a start or an end; a run that is more than text is left whole
    # and marked as a whole (see the module docs)
    cuts = sorted({b for span in spans for b in span})
    k = pos = 0
    for run, run_text in zip(runs, texts, strict=True):
        length = len(run_text)
        while k < len(cuts) and cuts[k] <= pos:
            k += 1
        inside: list[int] = []
        while k < len(cuts) and cuts[k] < pos + length:
            inside.append(cuts[k] - pos)
            k += 1
        if inside and _splittable(run):
            _split_run_at(run, inside, run_text)
        pos += length

    runs = _text_runs(paragraph)
    texts = [_run_text(r) for r in runs]
    starts: list[int] = []
    ends: list[int] = []
    pos = 0
    for run_text in texts:
        starts.append(pos)
        pos += len(run_text)
        ends.append(pos)
    found: list[tuple[list[Run], int, int]] = []
    for start, end in spans:
        covered: list[Run] = []
        i = bisect_right(ends, start)  # the first run that ends after ``start``
        while i < len(runs) and starts[i] < end:
            if ends[i] > starts[i]:
                covered.append(runs[i])
            i += 1
        found.append((covered, start, end))
    _drop_empty_runs(runs, texts)
    widen = _field_widener(paragraph)
    return [(widen(covered), start, end) for covered, start, end in found if covered]


def _simple_field_spans(runs: Sequence[Run], texts: Sequence[str]) -> list[tuple[int, int]]:
    """Where the paragraph's text is a simple field's result: the runs of it, in order."""
    out: list[tuple[int, int]] = []
    pos = 0
    for run, run_text in zip(runs, texts, strict=True):
        run_end = pos + len(run_text)
        if run_text and any(a.tag == qn("w:fldSimple") for a in run._r.iterancestors()):
            out.append((pos, run_end))
        pos = run_end
    return out


def _overlaps(spans: Sequence[tuple[int, int]], ends: list[int], start: int, end: int) -> bool:
    """Whether ``start:end`` overlaps any of ``spans`` (in order, apart; ``ends`` their ends)."""
    i = bisect_right(ends, start)
    return i < len(spans) and spans[i][0] < end


def _field_widener(paragraph: Paragraph) -> Any:
    """A function widening a paragraph's covered runs to the whole of any complex field they
    reach into: a comment marks a field from its ``begin`` to its ``end``, never inside the
    result Word computes - Word and LibreOffice show markers there as text, and an update of the
    field drops them. The paragraph's fields are read once, for all its places."""
    runs = _text_runs(paragraph)
    elements = [r._r for r in runs]
    groups: list[list[int]] = []
    group_of: dict[Any, int] = {}
    depth = 0
    for i, r in enumerate(elements):
        kinds = [c.get(qn("w:fldCharType")) for c in r.findall(qn("w:fldChar"))]
        if "begin" in kinds:
            if depth == 0:
                groups.append([])
            depth += 1
        if depth > 0:
            groups[-1].append(i)
            group_of[r] = len(groups) - 1
        if "end" in kinds and depth > 0:
            depth -= 1
    position = {r: i for i, r in enumerate(elements)}
    whole: dict[int, tuple[frozenset[int], list[Run]]] = {}  # a field's runs, made once

    def widen(covered: list[Run]) -> list[Run]:
        mine = {position[c._r] for c in covered if c._r in position}
        reached = {g for c in covered if (g := group_of.get(c._r)) is not None}
        if len(reached) == 1:  # within one field, as a place mostly is: the field's runs
            g = next(iter(reached))
            if g not in whole:
                whole[g] = (frozenset(groups[g]), [runs[i] for i in groups[g]])
            members, field_runs = whole[g]
            if mine <= members:
                return covered if len(mine) == len(members) else field_runs
        wanted = set(mine)
        for g in reached:
            wanted.update(groups[g])
        if wanted == mine:
            return covered
        return [runs[i] for i in sorted(wanted)]

    return widen


#: Children a run may have and still count as "empty text with nothing else in it".
_INERT_RUN_CHILDREN = frozenset({"rPr", "t"})


def _drop_empty_runs(runs: Sequence[Run], texts: Sequence[str]) -> None:
    """Remove the zero-length runs among a paragraph's ``runs`` (``texts``, their texts).

    A run holding a ``commentReference`` also has no text, and deleting it would detach a
    comment that is already anchored in this paragraph - so a run is only removed when it
    carries nothing but formatting and empty text.
    """
    for run, run_text in zip(runs, texts, strict=True):
        if run_text:
            continue
        tags = {child.tag.split("}")[-1] for child in run._r}
        if tags - _INERT_RUN_CHILDREN:
            continue
        parent = run._r.getparent()
        if parent is not None:
            parent.remove(run._r)


def _offending(finding: Finding, text: str | None = None) -> list[str]:
    """The candidates that are the finding's own words (span, quote, actual) - the ones every
    occurrence of which is wrong - as opposed to ``expected``, the value it should have been."""
    out: list[str] = []
    # the finding's own quote first: ``text[span]`` is only right when ``text`` is exactly the
    # text the span indexes, and a check may have normalised its text before reading it
    if finding.quote:
        out.append(finding.quote.strip())
    if text is not None and finding.span is not None:
        start, end = finding.span
        if 0 <= start < end <= len(text):
            out.append(text[start:end].strip())
    if finding.actual:
        for candidate in (finding.actual, finding.actual.split(" (")[0]):
            out.append(normalize_spaces(candidate).removesuffix("…"))
    return [c for c in dict.fromkeys(out) if c]


def _candidates(finding: Finding, text: str | None = None) -> list[str]:
    """Text to look for, most specific first.

    The checked text at the finding's ``span`` comes first when the caller supplies that text,
    then ``quote`` - the document's own words, in full. Both matter because ``actual`` is often
    normalised (an ISO date, "48750") or shortened for display with a trailing "…", and neither
    form occurs in the Word file.

    A finding's ``actual`` is written for a human ("ორასი (200)"), so the parenthetical gloss
    is stripped before searching: anchoring the comment on the offending word is the point.
    ``expected`` is the last resort - marking the value that should have changed is still more
    useful than marking nothing.
    """
    out: list[str] = []

    def add(candidate: str | None) -> None:
        candidate = (candidate or "").strip()
        if candidate and candidate not in out:
            out.append(candidate)

    add(finding.quote)
    if text is not None and finding.span is not None:
        start, end = finding.span
        if 0 <= start < end <= len(text):
            add(text[start:end])
    for raw in (finding.actual, finding.expected):
        if not raw:
            continue
        for candidate in (raw, raw.split(" (")[0], normalize_spaces(raw)):
            add(candidate.removesuffix("…"))
    return out


#: Markup compatibility: a text box written twice, as DrawingML (``mc:Choice``) and as VML for
#: older readers (``mc:Fallback``). Word shows the first, so only it is searched and marked.
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"


def _blocks(element: Any) -> Iterator[Any]:
    """A container's paragraphs and tables in order, a block-level content control's among them:
    a form wraps its fields in ``w:sdt``, and Docling reads what they hold."""
    for child in element:
        if child.tag in (qn("w:p"), qn("w:tbl")):
            yield child
        elif child.tag == qn("w:sdt"):
            content = child.find(qn("w:sdtContent"))
            if content is not None:
                yield from _blocks(content)


def _text_boxes(paragraph: Any) -> Iterator[Any]:
    """The text boxes a paragraph anchors, each once: not one inside another text box (that
    box's own paragraph anchors it), nor the VML copy kept for older readers."""
    for box in paragraph.iter(qn("w:txbxContent")):
        ancestors = list(box.iterancestors())
        if any(a.tag == _MC_FALLBACK for a in ancestors):
            continue
        if next((a for a in ancestors if a.tag == qn("w:p")), None) is paragraph:
            yield box


def _with_text_boxes(block: Any, parent: Any) -> Iterator[Paragraph]:
    """The paragraphs of the text boxes a paragraph anchors (and of their tables), then the
    paragraph itself: Docling reads a text box before the paragraph it stands in, and a place's
    order must be the order the check counted it in (red team R6)."""
    for box in _text_boxes(block):
        for inner in _blocks(box):
            if inner.tag == qn("w:p"):
                yield from _with_text_boxes(inner, parent)
                continue
            for cell in _cells(inner, parent):
                yield from all_paragraphs(cell)
    yield Paragraph(block, parent)


def _cells(table: Any, parent: Any) -> Iterator[_Cell]:
    """A table's cells, each once, as the file writes them, row by row. ``Row.cells`` repeats a
    cell for every grid column it spans - a number the file states, and may state as two
    billion (security review) - and reads a vertically merged cell's continuation as the cell
    above, failing when there is none; a text reader reads each cell once."""
    tbl = Table(table, parent)
    for tr in tbl._tbl.tr_lst:
        for tc in tr.tc_lst:
            yield _Cell(tc, tbl)


def all_paragraphs(container: object) -> Iterator[Paragraph]:
    """Every paragraph in the document, including the ones inside tables, content controls and
    text boxes.

    ``Document.paragraphs`` yields only top-level paragraphs. The client's acceptance acts lay
    their numbered sections out in tables, so without this the comments for the amount, the
    dates and the signatories all fell back to a document-level note - the fields most in need
    of being marked in place. A form's content controls and a text box hold text the check
    reads too. A container's paragraphs come before its tables, as they always have: a search
    that wants one place takes the first.
    """
    body: Any = getattr(container, "_body", container)
    blocks = list(_blocks(body._element))
    for block in blocks:
        if block.tag == qn("w:p"):
            yield from _with_text_boxes(block, body)
    for block in blocks:
        if block.tag == qn("w:tbl"):
            for cell in _cells(block, body):
                yield from all_paragraphs(cell)


#: One place a comment goes: the paragraph, the offsets in its text, and the runs covering them.
_Place = tuple[Paragraph, int, int, list[Run]]


class _Paragraphs:
    """A document's paragraphs (``all_paragraphs``, and ``paragraphs_in_order``), each with its
    text and its text folded for search (``_normalised_index``), read once for every finding:
    marking leaves a paragraph's text as it was - splitting runs and anchoring a comment change
    none of it. A needle the whole document does not hold is no paragraph's, and a paragraph that
    holds neither the needle nor its folded form is passed over without reading its runs."""

    def __init__(self, document: DocumentType) -> None:
        texts: dict[Any, tuple[str, str]] = {}

        def read(paragraphs: Iterator[Paragraph]) -> list[Paragraph]:
            out = list(paragraphs)
            for paragraph in out:
                if paragraph._p not in texts:
                    own = "".join(_run_text(r) for r in _text_runs(paragraph))
                    texts[paragraph._p] = (own, _normalised_index(own)[0])
            return out

        self.listed = read(all_paragraphs(document))
        self.ordered = read(paragraphs_in_order(document))
        self.texts = texts
        self.raw = "\n".join(own for own, _ in texts.values())
        self.flat = "\n".join(flat for _, flat in texts.values())

    def holding(self, needle: str, *, ordered: bool = False) -> Iterator[Paragraph]:
        """The paragraphs (in ``paragraphs_in_order`` order, or ``all_paragraphs``) that may hold
        ``needle``: those holding it as written or folded."""
        needle = needle.strip()
        flat_needle = _normalised_index(needle)[0]
        if needle not in self.raw and not (flat_needle and flat_needle in self.flat):
            return
        for paragraph in self.ordered if ordered else self.listed:
            own, flat = self.texts[paragraph._p]
            if needle in own or (flat_needle and flat_needle in flat):
                yield paragraph


def _places_for(
    document: DocumentType | _Paragraphs, finding: Finding, text: str | None = None
) -> tuple[list[_Place], bool]:
    """Every place a finding should be marked, and whether they are surely its own words - the
    places a tracked change may rewrite (``_chosen_places``).

    A wrong value written in several places is wrong in each - a misspelled director in the
    preamble, the signature block and the annex - and a reviewer who corrects the one marked
    place leaves the rest. So the finding's own words are marked wherever they occur. The value
    it *should* have been (``expected``), searched only as a last resort, marks one place: its
    other occurrences are correct.

    No more than ``MAX_SEARCHED`` places are read, and one more to tell that there are more: then
    none is surely the finding's own.
    """
    paragraphs = document if isinstance(document, _Paragraphs) else _Paragraphs(document)
    offending = _offending(finding, text)
    if finding.places is not None and finding.quote and finding.quote.strip():
        chosen, trusted = _chosen_places(paragraphs, finding.quote.strip(), finding.places, text)
        if len(chosen) > MAX_SEARCHED:
            # the places it names lie past what a search reads: marking the first ones would
            # mark words it does not mean, so it goes in the note at the top instead
            return [], False
        if chosen:
            return chosen, trusted
    for needle in _candidates(finding, text):
        # where the words stand as words first: a typo "ივნის" is not inside "ივნისიდან", and
        # the nominative of a name is not inside its genitive. A needle found only inside
        # longer words - a stem the act writes with an ending - is marked there.
        found = _places_of(paragraphs, needle, whole=True, every=needle in offending)
        if not found:
            found = _places_of(paragraphs, needle, whole=False, every=needle in offending)
        if found:
            return found, needle in offending and len(found) <= MAX_SEARCHED
    return [], False


#: How many words on each side of a place say which place it is.
_CONTEXT_WORDS = 3


#: How far from a place its context words are looked for: three words and the separators
#: between them fit in it, at the lengths the text limits allow (``bound_text``), so a long
#: line costs no more than a short one for each place on it.
_CONTEXT_REACH = 2000


def _line_breaks(text: str) -> list[int]:
    return [m.start() for m in re.finditer("\n", text)]


def _line_of(text: str, start: int, end: int, breaks: list[int]) -> tuple[int, int]:
    """Where the line holding ``text[start:end]`` starts and ends, found by bisection in the
    text's line breaks (``_line_breaks``): the text is searched for every place in it."""
    k = bisect_left(breaks, start)
    j = bisect_left(breaks, end)
    return (breaks[k - 1] + 1 if k else 0), (breaks[j] if j < len(breaks) else len(text))


def _context(
    text: str, start: int, end: int, breaks: list[int]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The words before and after ``text[start:end]`` on its own line, look-alike letters
    folded: the lines around it are what a file may order otherwise than the check read them.
    ``breaks``: the text's line breaks (``_line_breaks``)."""
    line_start, line_end = _line_of(text, start, end, breaks)
    line_start = max(line_start, start - _CONTEXT_REACH)
    line_end = min(line_end, end + _CONTEXT_REACH)
    before = _TOKEN.findall(text[line_start:start])[-_CONTEXT_WORDS:]
    after = _TOKEN.findall(text[end:line_end])[:_CONTEXT_WORDS]
    return tuple(fold_look_alikes(w) for w in before), tuple(fold_look_alikes(w) for w in after)


#: How far before and after its line ``_around`` looks for the words of the lines around it.
_AROUND_CHARS = 300


def _around(
    text: str, start: int, end: int, breaks: list[int]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The last words before the line that holds ``text[start:end]`` and the first after it,
    as many as ``_context`` takes, look-alike letters folded: what tells two places apart whose
    lines say the same - a name alone on a signature line under each party's role."""
    line_start, line_end = _line_of(text, start, end, breaks)
    before = _TOKEN.findall(text[max(0, line_start - _AROUND_CHARS) : line_start])
    after = _TOKEN.findall(text[line_end : line_end + _AROUND_CHARS])[:_CONTEXT_WORDS]
    return (
        tuple(fold_look_alikes(w) for w in before[-_CONTEXT_WORDS:]),
        tuple(fold_look_alikes(w) for w in after),
    )


def _chosen_places(
    paragraphs: _Paragraphs,
    quote: str,
    places: tuple[tuple[int, ...], int],
    text: str | None = None,
) -> tuple[list[_Place], bool]:
    """The places of ``quote`` a finding names (``Finding.places``: ordinals among its
    whole-word places in the checked text), and whether they are surely those places.

    With the checked ``text``, each chosen place is found by the words around it there, in the
    file - on its own line, and when two lines say the same, on the lines around it: the order
    the file shows its parts in is not always the order the check read them in (a header the
    parser appends, a table read by columns), and a tracked change must never rewrite another
    copy of the words, written right (red team R6). Without it, or when the words around a
    place do not single one out, the ordinals are taken when the file shows as many places as
    the checked text did - then surely those places only without the text to say otherwise -
    and every place is marked, none surely, when it shows another number."""
    wanted, total = places
    found: list[_Place] = []
    bounds: list[tuple[int, int]] = []  # each place in the file's text, its paragraphs joined
    texts: list[str] = []
    base = 0
    holding = set(map(id, paragraphs.holding(quote, ordered=True)))
    for paragraph in paragraphs.ordered:
        if id(paragraph) in holding and len(found) <= MAX_SEARCHED:
            room = MAX_SEARCHED + 1 - len(found)
            for runs, start, end in _cover_all(paragraph, quote, whole=True, limit=room):
                found.append((paragraph, start, end, runs))
                bounds.append((base + start, base + end))
        own = paragraphs.texts[paragraph._p][0]
        texts.append(own)
        base += len(own) + 1
    if len(found) > MAX_SEARCHED:
        return found, False  # past the search's limit: no place is told apart from the rest
    if text is not None:
        file_text = "\n".join(texts)
        occurrences = whole_word_spans(text, quote)
        file_breaks, breaks = _line_breaks(file_text), _line_breaks(text)
        places_by_context: dict[object, list[int]] = {}
        for k, b in enumerate(bounds):
            places_by_context.setdefault(_context(file_text, *b, file_breaks), []).append(k)
        chosen: list[_Place] = []
        for i in wanted:
            if not 0 <= i < len(occurrences):
                break
            matches = places_by_context.get(_context(text, *occurrences[i], breaks), [])
            if len(matches) > 1:
                wide = _around(text, *occurrences[i], breaks)
                matches = [
                    k for k in matches if _around(file_text, *bounds[k], file_breaks) == wide
                ]
            if len(matches) != 1:
                break
            chosen.append(found[matches[0]])
        else:
            if chosen:
                return chosen, True
    if len(found) != total:
        return found, False
    return [found[i] for i in wanted if 0 <= i < len(found)], text is None


def paragraphs_in_order(container: object) -> Iterator[Paragraph]:
    """Every paragraph in reading order - a table's between the paragraphs around it, a content
    control's where it stands, a text box's before the paragraph that anchors it - each once: a
    merged cell is read once, as a text reader reads it (``_cells``)."""

    def walk(element: Any, parent: Any) -> Iterator[Paragraph]:
        for block in _blocks(element):
            if block.tag == qn("w:p"):
                for box in _text_boxes(block):  # read before the paragraph, as Docling does
                    yield from walk(box, parent)
                yield Paragraph(block, parent)
                continue
            for cell in _cells(block, parent):
                yield from walk(cell._tc, cell)

    body: Any = getattr(container, "_body", container)
    yield from walk(body._element, body)


def _places_of(paragraphs: _Paragraphs, needle: str, *, whole: bool, every: bool) -> list[_Place]:
    """The places ``needle`` stands: every one (up to ``MAX_SEARCHED`` and one more), or only the
    first when not ``every``."""
    found: list[_Place] = []
    for paragraph in paragraphs.holding(needle):
        room = MAX_SEARCHED + 1 - len(found)
        for runs, start, end in _cover_all(
            paragraph, needle, whole=whole, first=not every, limit=room
        ):
            found.append((paragraph, start, end, runs))
            if not every or len(found) > MAX_SEARCHED:
                return found
    return found


# --- tracked changes ----------------------------------------------------------------------------

#: A word or a number: what a tracked change replaces, so the reviewer sees the word at fault
#: struck through and the right one beside it, not a letter inside a word.
_TOKEN = re.compile(r"\w+")
#: Spaces and quotes, no part of what a correction says: "შპს „ბეტა“" is "შპს ბეტა".
_SPACES_AND_QUOTES = re.compile(rf"[\s{re.escape(QUOTE_CHARS)}]+")
#: A value, not words: Latin letters and digits in groups - a code, an account, a bank code -
#: written whole or grouped by spaces, hyphens, dots or slashes.
_VALUE = re.compile(r"[0-9A-Za-z]+(?:[ .\-/]{1,3}[0-9A-Za-z]+)*")


def tracked_edits(written: str, replacement: str) -> list[tuple[int, int, str]] | None:
    """How ``written`` - the document's words at a finding's place - becomes ``replacement``,
    as (start, end, new text) on ``written``; None when the two cannot be lined up safely, and
    the place is left to its comment.

    * word for word when both have as many words: each word that differs is replaced, and the
      quotes, spaces and punctuation around it stay ("შპს „დელტა ტრთიდი“" -> only "ტრთიდი");
    * the same words with other separators ("01-02-03-456" for "01.02.03.456"): the whole;
    * one value for another, one word on either side (a code, an account): the whole;
    * otherwise only a run of words between words both share ("15 (თხუთმეტი)" inside a longer
      deadline). A correction that would rewrite the words around it, or only add some (a
      value left out), is a comment's to explain, not a change a reviewer accepts in one click.

    No edit at all when the words already read as the replacement (a place found by the
    correction itself)."""
    mine, theirs = list(_TOKEN.finditer(written)), list(_TOKEN.finditer(replacement))
    if not mine or not theirs:
        return None
    if [m.group() for m in mine] == [t.group() for t in theirs]:
        if _SPACES_AND_QUOTES.sub("", written) == _SPACES_AND_QUOTES.sub("", replacement):
            return []
        return [
            (mine[0].start(), mine[-1].end(), replacement[theirs[0].start() : theirs[-1].end()])
        ]
    if len(mine) == len(theirs):
        return [
            (m.start(), m.end(), t.group())
            for m, t in zip(mine, theirs, strict=True)
            if m.group() != t.group()
        ]
    if len(mine) == 1 or len(theirs) == 1:
        # one value for another - a code, an account, grouped or not - is replaced whole; a
        # word for a phrase is no value: "შპს" is not to be written over with a whole name
        if not (_VALUE.fullmatch(written.strip()) and _VALUE.fullmatch(replacement.strip())):
            return None
        return [
            (mine[0].start(), mine[-1].end(), replacement[theirs[0].start() : theirs[-1].end()])
        ]
    shorter = min(len(mine), len(theirs))
    head = 0
    while head < shorter and mine[head].group() == theirs[head].group():
        head += 1
    tail = 0
    while tail < shorter - head and mine[-1 - tail].group() == theirs[-1 - tail].group():
        tail += 1
    if not head or not tail or head + tail >= shorter:
        return None
    first, last = mine[head], mine[len(mine) - 1 - tail]
    new = replacement[theirs[head].start() : theirs[len(theirs) - 1 - tail].end()]
    return [(first.start(), last.end(), new)]


#: One planned tracked change: the paragraph, the offsets in its text, the text to write.
_Edit = tuple[Paragraph, int, int, str]


def _plan_edits(
    edits: list[_Edit],
    paragraph: Paragraph,
    start: int,
    end: int,
    new: str,
    own: str | None = None,
) -> None:
    """Plan the tracked change that puts ``new`` in place of the words at ``start:end``, unless
    the words cannot be lined up with it or another finding's change already holds any of them:
    the first finding to claim words writes them, the others keep their comments. ``own`` is
    the paragraph's text, when the caller has it."""
    if own is None:
        own = "".join(_run_text(r) for r in _text_runs(paragraph))
    written = own[start:end]
    # what XML cannot carry is dropped before the words are lined up, not after
    planned = tracked_edits(written, _XML_ILLEGAL.sub("", new))
    if not planned:
        return
    mine = [(paragraph, start + a, start + b, text) for a, b, text in planned]
    for _, a, b, _ in mine:
        if any(q._p is paragraph._p and a < y and x < b for q, x, y, _ in edits):
            return
    edits.extend(mine)


class _RunIndex:
    """A paragraph's text runs and where each starts and ends, kept as runs are split."""

    def __init__(self, paragraph: Paragraph) -> None:
        self.runs = _text_runs(paragraph)
        self.starts: list[int] = []
        self.ends: list[int] = []
        pos = 0
        for run in self.runs:
            self.starts.append(pos)
            pos += len(_run_text(run))
            self.ends.append(pos)

    def split_at(self, boundary: int) -> bool:
        """Split the run ``boundary`` falls inside, if any; False when it is a run that is more
        than text (``_splittable``), which is never split."""
        i = bisect_right(self.ends, boundary)  # the first run that ends after ``boundary``
        if i >= len(self.runs) or self.starts[i] >= boundary:
            return True  # between two runs already
        run = self.runs[i]
        if not _splittable(run):
            return False
        self.runs.insert(i, _split_run(run, boundary - self.starts[i]))
        self.starts.insert(i + 1, boundary)
        self.ends.insert(i, boundary)
        return True


def _edit_runs(paragraph: Paragraph, spans: Sequence[tuple[int, int]]) -> list[list[Run] | None]:
    """For each ``start:end`` of a paragraph's planned changes, in order, the runs that hold
    exactly those words, split where needed; None when a boundary falls inside a run that is
    more than text (``_splittable``), or the words cannot be rewritten where they stand. The
    paragraph's runs are indexed once for all its changes."""
    index = _RunIndex(paragraph)
    splits = [index.split_at(start) and index.split_at(end) for start, end in spans]
    runs, starts, ends = index.runs, index.starts, index.ends
    text = "".join(_run_text(r) for r in runs)
    fields = set(_field_runs(runs))
    out: list[list[Run] | None] = []
    for (start, end), split in zip(spans, splits, strict=True):
        if not split:
            out.append(None)
            continue
        covered: list[Run] = []
        i = bisect_left(starts, start)
        while i < len(runs) and starts[i] < end:
            if ends[i] <= end and ends[i] > starts[i]:
                covered.append(runs[i])
            i += 1
        out.append(_rewritable(covered, text[start:end], fields))
    return out


def _rewritable(covered: list[Run], words: str, fields: set[Any]) -> list[Run] | None:
    """``covered``, when a tracked change can move them into a deletion: they hold ``words``
    exactly, are text only, are no field's, and nothing stands between them."""
    if not covered or "".join(_run_text(r) for r in covered) != words:
        return None
    if not all(_splittable(r) for r in covered):
        return None
    if any(r._r in fields for r in covered):
        return None  # what a field shows is Word's to compute (red team R6)
    first, last = covered[0]._r, covered[-1]._r
    parent = first.getparent()
    if any(r._r.getparent() is not parent for r in covered):
        return None  # the words cross a link or a content control's edge
    element = first.getnext() if first is not last else None
    while element is not None and element is not last:
        if element.tag in _BLOCKING:
            return None  # another reviewer's change or comment between the words
        element = element.getnext()
    return covered


#: What may not stand between the runs a tracked change rewrites: an earlier reviewer's change
#: or comment, a field, a link or a content control - moving the runs past it would reorder
#: the document's own words or detach the comment (red team R6).
_BLOCKING = frozenset(
    qn(tag)
    for tag in (
        "w:del",
        "w:ins",
        "w:moveFrom",
        "w:moveTo",
        "w:commentRangeStart",
        "w:commentRangeEnd",
        "w:fldSimple",
        "w:hyperlink",
        "w:sdt",
        "w:smartTag",
        "w:customXml",
    )
)


_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_REVISION_ID = re.compile(r"[0-9]{1,9}")


def _wrap(runs: list[Run], new: str, author: str, stamp: str, revision: int) -> int:
    """Mark ``runs`` deleted and ``new`` inserted after them, as a reviewer's tracked change:
    the runs move into a ``w:del`` (their text becomes ``w:delText``), and a ``w:ins`` holds one
    run with the first run's formatting. Returns the next free revision id."""
    first = runs[0]._r
    parent = first.getparent()
    if parent is None:  # pragma: no cover - a run found in a paragraph has one
        return revision
    attributes = {qn("w:author"): author, qn("w:date"): stamp}
    deleted = parent.makeelement(qn("w:del"), {qn("w:id"): str(revision), **attributes})
    first.addprevious(deleted)
    style = first.find(qn("w:rPr"))
    for run in runs:
        for t in run._r.findall(qn("w:t")):
            t.tag = qn("w:delText")
        deleted.append(run._r)
    text = _XML_ILLEGAL.sub("", new)
    if not text:
        return revision + 1
    inserted = parent.makeelement(qn("w:ins"), {qn("w:id"): str(revision + 1), **attributes})
    r = parent.makeelement(qn("w:r"), {})
    if style is not None:
        r.append(copy.deepcopy(style))
    t = r.makeelement(qn("w:t"), {_XML_SPACE: "preserve"})
    t.text = text
    r.append(t)
    inserted.append(r)
    deleted.addnext(inserted)
    return revision + 2


def _track(document: DocumentType, edits: Sequence[_Edit], author: str, when: datetime) -> int:
    """Write every planned change as a tracked change; returns how many were written. Every
    change's runs are found first: splitting a run never changes a paragraph's text, moving
    runs into a ``w:del`` does. Revision ids start above every id the document already uses."""
    if not edits:
        return 0
    # ASCII digits only: "²" is a digit to str.isdigit and none to int (red team R6)
    used = [int(v) for v in document.element.xpath("//@w:id") if _REVISION_ID.fullmatch(str(v))]
    revision = max(used, default=0) + 1
    stamp = when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    by_paragraph: dict[Any, list[int]] = {}
    for n, (paragraph, _, _, _) in enumerate(edits):
        by_paragraph.setdefault(paragraph._p, []).append(n)
    found: list[list[Run] | None] = [None] * len(edits)
    for numbers in by_paragraph.values():
        spans = [(edits[n][1], edits[n][2]) for n in numbers]
        for n, runs in zip(numbers, _edit_runs(edits[numbers[0]][0], spans), strict=True):
            found[n] = runs
    targets = [(runs, edit[3]) for edit, runs in zip(edits, found, strict=True) if runs]
    for runs, new in targets:
        revision = _wrap(runs, new, author, stamp, revision)
    return len(targets)


def paragraph_text(paragraph: Paragraph, *, accepted: bool = False) -> str:
    """A paragraph's text with its tracked changes rejected - the words the document wrote,
    deleted text in and inserted text out - or, ``accepted``, as a reviewer who accepts them
    all reads it. ``Paragraph.text`` sees neither: it reads only a paragraph's own runs."""
    parts: list[str] = []
    p = paragraph._p
    for el in p.iter(qn("w:t"), qn("w:delText")):
        if next(el.iterancestors(qn("w:p")), None) is not p:
            continue  # a text box's words are its own paragraph's
        inserted = any(a.tag == qn("w:ins") for a in el.iterancestors())
        deleted = el.tag == qn("w:delText")
        if (deleted and accepted) or (inserted and not accepted):
            continue
        parts.append(el.text or "")
    return "".join(parts)


def summary_comment(report: CheckReport) -> str | None:
    """The note at the top of the annotated file: what the check could not run or does not
    apply to this act, so a clean margin is never read as "everything was checked"."""
    lines = [f"[შეჯამება / Summary] {AUTHOR}"]
    if report.skipped:
        lines.append("ვერ შემოწმდა / Not checked:")
        lines += [f"• {s.rule_id}: {s.reason_ka} / {s.reason_en}" for s in report.skipped]
    if not_applicable := report.summary.get("not_applicable"):
        lines.append(f"არ ეხება ამ აქტს / Not applicable: {not_applicable}")
    return _XML_ILLEGAL.sub("", "\n".join(lines)) if len(lines) > 1 else None


def _cut_short_note(finding: Finding, marked: int, places: int) -> str:
    """The note for a finding whose places ran past the limits (``MAX_PLACES_PER_FINDING``,
    ``MAX_PLACES``, ``MAX_SEARCHED``): how many are marked, and that the rest are not, nor
    changed."""
    total = f"{MAX_SEARCHED}-ზე მეტი" if places > MAX_SEARCHED else str(places)
    total_en = f"more than {MAX_SEARCHED}" if places > MAX_SEARCHED else str(places)
    return _XML_ILLEGAL.sub(
        "",
        f"{finding.rule_id}: მონიშნულია {marked} ადგილი ({total}-დან); დანარჩენი არ არის "
        f"მონიშნული და არც შესწორებული / marked at {marked} of its {total_en} places; the others "
        "are neither marked nor changed",
    )


def _add_note(document: DocumentType, text: str, author: str) -> None:
    """A comment on the document's first word: the summary and the findings no words of the
    document stand for. The body's text is left as it was; a document with no text at all gets
    a paragraph to carry it."""
    for paragraph in paragraphs_in_order(document):
        first = _TOKEN.search("".join(_run_text(r) for r in _text_runs(paragraph)))
        if first is None:
            continue
        found = _cover_all(paragraph, first.group(), first=True)
        if found:
            document.add_comment(found[0][0], text=text, author=author, initials=INITIALS)
            return
    run = document.add_paragraph().add_run("—")
    document.add_comment(run, text=text, author=author, initials=INITIALS)


def annotate_docx(
    source: Path,
    findings: Sequence[Finding],
    out_path: Path,
    *,
    author: str = AUTHOR,
    text: str | None = None,
    summary: str | None = None,
    when: datetime | None = None,
) -> tuple[Path, list[Finding]]:
    """Write ``source`` to ``out_path`` with one Word comment per finding, and a tracked change
    wherever a finding knows the words that replace the ones at fault (``Finding.replacement``)
    and they line up with them (``tracked_edits``): the reviewer accepts or rejects it in Word.

    ``text`` is the text the findings were computed over - the one their ``span`` offsets
    index into. When it is given, the words at each span are searched for first. ``summary``
    (``summary_comment``) opens the file, on its first word, with the findings that could not
    be anchored; ``when`` dates the tracked changes (now by default).

    Returns the path and the findings that could not be anchored to any text — those belong in
    the report so that nothing is silently dropped, which is the failure mode that would make
    an annotated document quietly misleading.
    """
    # .docx only. A .docm is a macro-enabled package python-docx refuses with a bare
    # ValueError, and a macro-carrying file is not one to hand back to a reviewer anyway.
    if source.suffix.lower() != ".docx":
        msg = (
            f"Word comments can only be written into a .docx file, got {source.suffix!r}. "
            "Scanned PDFs are reported through the findings table and highlighted page images."
        )
        raise UnsupportedDocumentError(msg)

    try:
        # SyntaxError: lxml's XMLSyntaxError, for a package whose document part is not well-formed
        document = Document(str(source))
    except (ValueError, KeyError, PackageNotFoundError, zipfile.BadZipFile, SyntaxError) as e:
        msg = (
            "the file is not a Word document that comments can be written into "
            f"({type(e).__name__})"
        )
        raise UnsupportedDocumentError(msg) from e
    unanchored: list[Finding] = []
    # Findings that land on exactly the same words share one comment: a bank code that is both
    # malformed and not the contract's is one place to fix, not two notes on top of each other.
    # A place is its paragraph and the offsets in the paragraph's text, which splitting runs for
    # later findings never changes.
    comments: dict[tuple[object, int, int], Comment] = {}
    edits: list[_Edit] = []
    own: dict[object, str] = {}  # each paragraph's text, read once
    cut_short: list[str] = []
    paragraphs = _Paragraphs(document)
    for finding in findings:
        places, trusted = _places_for(paragraphs, finding, text)
        total = len(places)
        # the file's limit counts the places no finding has marked yet: findings on the same
        # words share their comment
        room = MAX_PLACES - len(comments)
        kept: list[_Place] = []
        for place in places[:MAX_PLACES_PER_FINDING]:
            if (place[0]._p, place[1], place[2]) not in comments:
                if room <= 0:
                    break
                room -= 1
            kept.append(place)
        if len(kept) < total:
            cut_short.append(_cut_short_note(finding, len(kept), total))
            trusted = False
        places = kept
        if not places:
            unanchored.append(finding)
            continue
        for paragraph, start, end, runs in places:
            if finding.replacement and trusted:
                if paragraph._p not in own:
                    own[paragraph._p] = "".join(_run_text(r) for r in _text_runs(paragraph))
                _plan_edits(edits, paragraph, start, end, finding.replacement, own[paragraph._p])
            key = (paragraph._p, start, end)
            if (existing := comments.get(key)) is not None:
                existing.add_paragraph(comment_text(finding))
                continue
            comments[key] = document.add_comment(
                runs, text=comment_text(finding), author=author, initials=INITIALS
            )

    notes = [*([summary] if summary else []), *cut_short, *(comment_text(f) for f in unanchored)]
    if notes:
        _add_note(document, "\n\n".join(notes), author)
    # last: a tracked change moves runs out of the paragraph's direct children, where the
    # searches above read the text
    _track(document, edits, author, when or datetime.now(UTC))
    # The file goes back to a reviewer as something this system produced: nothing the upload used
    # to reach out or to run (see docx_sanitize) goes with it.
    removed = neutralise_package(document)
    if removed.total:
        log.info(
            "docx_sanitised",
            relationships=removed.relationships,
            elements=removed.elements,
            fields=removed.fields,
            settings=removed.settings,
            references=removed.references,
        )
    document.save(str(out_path))
    return out_path, unanchored
