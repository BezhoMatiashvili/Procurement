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
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

from docx import Document
from docx.comments import Comment
from docx.document import Document as DocumentType
from docx.opc.exceptions import PackageNotFoundError
from docx.oxml.ns import qn
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
_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


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
_TEXT_RUN_CHILDREN = frozenset({"rPr", "t", "tab", "br", "cr"})


def _splittable(run: Run) -> bool:
    return {child.tag.split("}")[-1] for child in run._r} <= _TEXT_RUN_CHILDREN


def _split_run(run: Run, offset: int) -> Run:
    """Split a run in two at ``offset``, returning the new run holding the leading text."""
    head = copy.deepcopy(run._r)
    run._r.addprevious(head)
    head_run = Run(head, run._parent)
    head_run.text = run.text[:offset]
    run.text = run.text[offset:]
    return head_run


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


def _locate(
    text: str, needle: str, start: int = 0, *, whole: bool = False
) -> tuple[int, int] | None:
    """Offsets of ``needle`` in ``text`` at or after ``start``, tolerating quoting and spacing
    differences. With ``whole``, only where it stands as whole words (``_bounded``)."""
    at = text.find(needle, start)
    while at >= 0:
        if not whole or _bounded(text, at, at + len(needle), needle):
            return at, at + len(needle)
        at = text.find(needle, at + 1)
    flat, index = _normalised_index(text)
    flat_needle, _ = _normalised_index(needle)
    if not flat_needle:
        return None
    flat_start = next((i for i, orig in enumerate(index) if orig >= start), len(flat))
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
    while needle and (found := _locate(text, needle, at, whole=True)) is not None:
        out.append(found)
        at = found[1]
    return out


def _runs_covering(paragraph: Paragraph, needle: str, start_at: int = 0) -> list[Run] | None:
    """Split runs as needed so ``needle`` is covered by a whole number of runs."""
    found = _cover_next(paragraph, needle, start_at, whole=True) or _cover_next(
        paragraph, needle, start_at
    )
    return found[0] if found else None


def _cover_next(
    paragraph: Paragraph, needle: str, start_at: int, *, whole: bool = False
) -> tuple[list[Run], int, int] | None:
    """The runs covering the first occurrence of ``needle`` at or after ``start_at`` (with
    ``whole``, the first standing as whole words), split so they cover it exactly, and its
    offsets. Splitting leaves the paragraph's text unchanged, so offsets stay valid from one
    occurrence to the next and between findings."""
    needle = needle.strip()
    if not needle:
        return None
    text = "".join(r.text for r in paragraph.runs)
    located = _locate(text, needle, start_at, whole=whole)
    if located is None:
        return None
    start, end = located

    # walk runs, splitting the one that contains the start and the one containing the end;
    # a run that is more than text is left whole and marked as a whole (see the module docs)
    for boundary in (start, end):
        pos = 0
        for run in list(paragraph.runs):
            length = len(run.text)
            if pos < boundary < pos + length:
                if _splittable(run):
                    _split_run(run, boundary - pos)
                break
            pos += length

    covered: list[Run] = []
    pos = 0
    for run in paragraph.runs:
        run_end = pos + len(run.text)
        if pos < end and run_end > start and run.text:
            covered.append(run)
        pos = run_end
    _drop_empty_runs(paragraph)
    return (covered, start, end) if covered else None


#: Children a run may have and still count as "empty text with nothing else in it".
_INERT_RUN_CHILDREN = frozenset({"rPr", "t"})


def _drop_empty_runs(paragraph: Paragraph) -> None:
    """Remove zero-length runs left behind by splitting.

    A run holding a ``commentReference`` also has no text, and deleting it would detach a
    comment that is already anchored in this paragraph - so a run is only removed when it
    carries nothing but formatting and empty text.
    """
    for run in list(paragraph.runs):
        if run.text:
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


def all_paragraphs(container: object) -> Iterator[Paragraph]:
    """Every paragraph in the document, including the ones inside tables.

    ``Document.paragraphs`` yields only top-level paragraphs. The client's acceptance acts lay
    their numbered sections out in tables, so without this the comments for the amount, the
    dates and the signatories all fell back to a document-level note - the fields most in need
    of being marked in place.
    """
    yield from container.paragraphs  # type: ignore[attr-defined]
    for table in getattr(container, "tables", []):
        for row in table.rows:
            for cell in row.cells:
                yield from all_paragraphs(cell)


#: One place a comment goes: the paragraph, the offsets in its text, and the runs covering them.
_Place = tuple[Paragraph, int, int, list[Run]]


def _anchor_for(
    document: DocumentType, finding: Finding, text: str | None = None
) -> list[Run] | None:
    """Find the runs a finding should be attached to, most specific candidate first."""
    places = _places_for(document, finding, text)
    return places[0][3] if places else None


def _places_for(document: DocumentType, finding: Finding, text: str | None = None) -> list[_Place]:
    """Every place a finding should be marked.

    A wrong value written in several places is wrong in each - a misspelled director in the
    preamble, the signature block and the annex - and a reviewer who corrects the one marked
    place leaves the rest. So the finding's own words are marked wherever they occur. The value
    it *should* have been (``expected``), searched only as a last resort, marks one place: its
    other occurrences are correct.
    """
    offending = _offending(finding, text)
    if finding.places is not None and finding.quote and finding.quote.strip():
        chosen = _chosen_places(document, finding.quote.strip(), finding.places)
        if chosen:
            return chosen
    for needle in _candidates(finding, text):
        # where the words stand as words first: a typo "ივნის" is not inside "ივნისიდან", and
        # the nominative of a name is not inside its genitive. A needle found only inside
        # longer words - a stem the act writes with an ending - is marked there.
        found = _places_of(document, needle, whole=True, every=needle in offending)
        if not found:
            found = _places_of(document, needle, whole=False, every=needle in offending)
        if found:
            return found
    return []


def _chosen_places(
    document: DocumentType, quote: str, places: tuple[tuple[int, ...], int]
) -> list[_Place]:
    """The places of ``quote`` a finding names (``Finding.places``): its whole-word places in
    reading order, the chosen ordinals of them. The document must show as many places as the
    checked text did; when it shows another number - a table cell the parser read twice, a
    header - the ordinals cannot be trusted, and every place is marked."""
    wanted, total = places
    found: list[_Place] = []
    for paragraph in paragraphs_in_order(document):
        at = 0
        while (hit := _cover_next(paragraph, quote, at, whole=True)) is not None:
            runs, start, end = hit
            found.append((paragraph, start, end, runs))
            at = end
    if len(found) != total:
        return found
    return [found[i] for i in wanted if 0 <= i < len(found)]


def paragraphs_in_order(container: object) -> Iterator[Paragraph]:
    """Every paragraph in reading order - a table's between the paragraphs around it - each
    once: a merged cell, which python-docx returns for every grid column it spans, is read
    once, as a text reader reads it."""
    seen: set[object] = set()  # the cells' elements, held so that each keeps its identity

    def walk(inner: object) -> Iterator[Paragraph]:
        for block in inner.iter_inner_content():  # type: ignore[attr-defined]
            if isinstance(block, Paragraph):
                yield block
                continue
            for row in block.rows:
                for cell in row.cells:
                    if cell._tc in seen:
                        continue
                    seen.add(cell._tc)
                    yield from walk(cell)

    yield from walk(container)


def _places_of(document: DocumentType, needle: str, *, whole: bool, every: bool) -> list[_Place]:
    """The places ``needle`` stands: every one, or only the first when not ``every``."""
    found: list[_Place] = []
    for paragraph in all_paragraphs(document):
        at = 0
        while (hit := _cover_next(paragraph, needle, at, whole=whole)) is not None:
            runs, start, end = hit
            found.append((paragraph, start, end, runs))
            if not every:
                return found
            at = end
    return found


# --- tracked changes ----------------------------------------------------------------------------

#: A word or a number: what a tracked change replaces, so the reviewer sees the word at fault
#: struck through and the right one beside it, not a letter inside a word.
_TOKEN = re.compile(r"\w+")
#: Spaces and quotes, no part of what a correction says: "შპს „ბეტა“" is "შპს ბეტა".
_SPACES_AND_QUOTES = re.compile(rf"[\s{re.escape(QUOTE_CHARS)}]+")


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


def _plan_edits(edits: list[_Edit], paragraph: Paragraph, start: int, end: int, new: str) -> None:
    """Plan the tracked change that puts ``new`` in place of the words at ``start:end``, unless
    the words cannot be lined up with it or another finding's change already holds any of them:
    the first finding to claim words writes them, the others keep their comments."""
    written = "".join(r.text for r in paragraph.runs)[start:end]
    planned = tracked_edits(written, new)
    if not planned:
        return
    mine = [(paragraph, start + a, start + b, text) for a, b, text in planned]
    for _, a, b, _ in mine:
        if any(q._p is paragraph._p and a < y and x < b for q, x, y, _ in edits):
            return
    edits.extend(mine)


def _runs_between(paragraph: Paragraph, start: int, end: int) -> list[Run] | None:
    """The runs that hold exactly ``start:end`` of the paragraph's text, split where needed;
    None when a boundary falls inside a run that is more than text (``_splittable``)."""
    for boundary in (start, end):
        pos = 0
        for run in list(paragraph.runs):
            length = len(run.text)
            if pos < boundary < pos + length:
                if not _splittable(run):
                    return None
                _split_run(run, boundary - pos)
                break
            pos += length
    covered: list[Run] = []
    pos = 0
    for run in paragraph.runs:
        run_end = pos + len(run.text)
        if pos >= start and run_end <= end and run.text:
            covered.append(run)
        pos = run_end
    whole = "".join(r.text for r in paragraph.runs)[start:end]
    if not covered or "".join(r.text for r in covered) != whole:
        return None
    if not all(_splittable(r) for r in covered):
        return None
    return covered


_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


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
    used = [int(v) for v in document.element.xpath("//@w:id") if str(v).isdigit()]
    revision = max(used, default=0) + 1
    stamp = when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    targets = [
        (runs, new)
        for paragraph, start, end, new in edits
        if (runs := _runs_between(paragraph, start, end)) is not None
    ]
    for runs, new in targets:
        revision = _wrap(runs, new, author, stamp, revision)
    return len(targets)


def paragraph_text(paragraph: Paragraph, *, accepted: bool = False) -> str:
    """A paragraph's text with its tracked changes rejected - the words the document wrote,
    deleted text in and inserted text out - or, ``accepted``, as a reviewer who accepts them
    all reads it. ``Paragraph.text`` sees neither: it reads only a paragraph's own runs."""
    parts: list[str] = []
    for el in paragraph._p.iter(qn("w:t"), qn("w:delText")):
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


def _add_note(document: DocumentType, text: str, author: str) -> None:
    """A comment on the document's first word: the summary and the findings no words of the
    document stand for. The body's text is left as it was; a document with no text at all gets
    a paragraph to carry it."""
    for paragraph in paragraphs_in_order(document):
        first = _TOKEN.search("".join(r.text for r in paragraph.runs))
        if first is None:
            continue
        found = _cover_next(paragraph, first.group(), 0)
        if found is not None:
            document.add_comment(found[0], text=text, author=author, initials=INITIALS)
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
    for finding in findings:
        places = _places_for(document, finding, text)
        if not places:
            unanchored.append(finding)
            continue
        for paragraph, start, end, runs in places:
            if finding.replacement:
                _plan_edits(edits, paragraph, start, end, finding.replacement)
            key = (paragraph._p, start, end)
            if (existing := comments.get(key)) is not None:
                existing.add_paragraph(comment_text(finding))
                continue
            comments[key] = document.add_comment(
                runs, text=comment_text(finding), author=author, initials=INITIALS
            )

    notes = [*([summary] if summary else []), *(comment_text(f) for f in unanchored)]
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
