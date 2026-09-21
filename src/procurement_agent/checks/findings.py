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
    provenance: Provenance | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @property
    def is_blocking(self) -> bool:
        return self.severity == "error"


class CheckReport(BaseModel):
    document_id: str
    reference_document_id: str | None = None
    findings: list[Finding] = Field(default_factory=list)
    #: rules that could not run, and why - never silently skipped
    skipped: list[str] = Field(default_factory=list)
    summary: dict[str, str] = Field(default_factory=dict)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors
