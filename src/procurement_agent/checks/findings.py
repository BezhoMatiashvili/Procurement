"""The result model shared by every check (AOA today, contract review and BOQ next).

A finding is machine-readable on purpose: the rule id lets the evaluation harness score each
client rule separately, the provenance lets the DOCX writer attach a comment to the right run,
and the bilingual messages let the same object serve the Georgian UI and an English report.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from procurement_agent.extraction.schemas import Provenance

Severity = Literal["error", "warning", "info"]


class Finding(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule_id: str = Field(description="stable id, e.g. 'AOA.ACT_DATE'")
    severity: Severity
    message_ka: str
    message_en: str
    expected: str | None = None
    actual: str | None = None
    #: character span in the checked document's text, for in-document annotation
    span: tuple[int, int] | None = None
    #: the document's own words for what was found, in full. ``expected``/``actual`` are for
    #: reading: normalised (an ISO date, "48750"), and in the comparison shortened for display.
    #: Neither can be found again in the Word file, so annotation looks for this first.
    quote: str | None = None
    #: Which of the places the document writes ``quote`` are the finding's, when not every one
    #: is: their ordinals among the quote's whole-word places in reading order (from 0), and how
    #: many places there are. A name copied from the other party is wrong in this party's block
    #: and right in that party's own. None: every place is (``report/docx_annotate``).
    places: tuple[tuple[int, ...], int] | None = None
    #: What the document's words at the finding's place should read instead, as the correction
    #: a reviewer accepts as a tracked change (``report/docx_annotate``): set only where the rule
    #: knows ``expected`` is those words written right in the act's own form - a code, a name in
    #: its case, a date in the act's style, a misspelt word. None for a note to check
    #: (``needs_review``), a value the act leaves out, or a correction that is advice.
    replacement: str | None = None
    provenance: Provenance | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    #: A note the rules could not decide either way - the same clause in other words, a service
    #: the contract's list may or may not hold. It is marked for a person to read, and the rules
    #: never make it an error or a warning: a correct act written in other words must not fail.
    #: Only the model review, when a user asks for it, can raise one to a warning (``aoa_llm``).
    needs_review: bool = False
    #: Who read a ``needs_review`` note after the rules: "llm" when the local model's answer was
    #: applied to it (``checks/aoa_llm``). The model can mark a note, never clear it.
    reviewed_by: Literal["llm"] | None = None

    @property
    def is_blocking(self) -> bool:
        return self.severity == "error"


class SkipNote(BaseModel):
    """A rule that could not run, and why - in both languages, like a finding.

    ``Finding`` has carried ``message_ka``/``message_en`` from the start; skips were plain English
    strings, which meant a Georgian report explained its own gaps in English. Same contract now:
    whatever the UI can show as a finding, it can show as a skip.
    """

    model_config = ConfigDict(frozen=True)

    rule_id: str
    reason_ka: str
    reason_en: str


class CheckReport(BaseModel):
    document_id: str
    reference_document_id: str | None = None
    findings: list[Finding] = Field(default_factory=list)
    #: rules that could not run, and why - never silently skipped
    skipped: list[SkipNote] = Field(default_factory=list)
    summary: dict[str, str] = Field(default_factory=dict)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors
