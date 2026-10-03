"""What a report says about how much of its documents was read.

The checks themselves know nothing of this: they are given text and answer for the text. This
module sits between the stored documents and the reports the API, the downloads and the pages
build from them, and does two things:

* **adds the note** (``DOC.PARTIAL``) to a report built from a partly read document, first in its
  ``skipped`` list, so every consumer that lists what could not be checked - the pages, the Word
  and PDF summary comment, the comparison workbook - says it;
* **carries the numbers** (``read_quality``) on the report, so a client of the API sees what was
  read without parsing the note.

For a document read in full none of this changes a byte of what the checks produced.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field

from procurement_agent.checks.compare import ComparisonReport
from procurement_agent.checks.findings import CheckReport, SkipNote
from procurement_agent.extraction.schemas import ExtractionResult
from procurement_agent.ingestion import read_quality as rq
from procurement_agent.ingestion.read_quality import ReadQuality

__all__ = [
    "ReadAwareComparison",
    "ReadAwareReport",
    "StoredExtraction",
    "partial_notes",
    "read_aware_comparison",
    "read_aware_report",
    "stored_extraction",
]


def partial_notes(qualities: Mapping[str, ReadQuality | None]) -> list[SkipNote]:
    """The ``DOC.PARTIAL`` note of each side that was only partly read, in the order given."""
    notes = (rq.partial_note(q, who) for who, q in qualities.items() if q is not None)
    return [n for n in notes if n is not None]


class ReadAwareReport(CheckReport):
    """A check report plus what was read of the documents it was built from."""

    read_quality: dict[str, ReadQuality] = Field(default_factory=dict)


class ReadAwareComparison(ComparisonReport):
    """A comparison plus what was read of its two documents."""

    read_quality: dict[str, ReadQuality] = Field(default_factory=dict)


class StoredExtraction(ExtractionResult):
    """An extraction result as it is stored: the result, what was read of the document, and
    whether the model - asked for, and wanted - failed to answer."""

    read_quality: dict[str, ReadQuality] = Field(default_factory=dict)
    degraded: bool = False


def _known(qualities: Mapping[str, ReadQuality | None]) -> dict[str, ReadQuality]:
    return {who: q for who, q in qualities.items() if q is not None}


def _fields(
    report: CheckReport | ComparisonReport, known: Mapping[str, ReadQuality]
) -> dict[str, Any]:
    fields = {name: getattr(report, name) for name in type(report).model_fields}
    fields["skipped"] = [*partial_notes(known), *report.skipped]
    return fields


def read_aware_report(
    report: CheckReport, qualities: Mapping[str, ReadQuality | None]
) -> ReadAwareReport:
    """``report`` with the note of each partly read document first in its ``skipped`` list, and
    the numbers beside it. The report is not changed; a copy of the same kind is returned."""
    known = _known(qualities)
    return ReadAwareReport.model_construct(**_fields(report, known), read_quality=known)


def read_aware_comparison(
    report: ComparisonReport, qualities: Mapping[str, ReadQuality | None]
) -> ReadAwareComparison:
    """``read_aware_report`` for a comparison."""
    known = _known(qualities)
    return ReadAwareComparison.model_construct(**_fields(report, known), read_quality=known)


def stored_extraction(
    result: ExtractionResult, quality: ReadQuality, *, degraded: bool = False
) -> StoredExtraction:
    """``result`` as it is stored: with what was read of the document, and - first among its
    warnings, where the page and the workbook export both show it - the note that part of it
    was not."""
    note = rq.partial_note(quality)
    fields = {name: getattr(result, name) for name in ExtractionResult.model_fields}
    if note is not None:
        fields["warnings"] = [f"{note.rule_id}: {note.reason_en}", *result.warnings]
    return StoredExtraction.model_construct(
        **fields, read_quality={"document": quality}, degraded=degraded
    )
