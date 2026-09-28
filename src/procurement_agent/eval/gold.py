"""Ground truth for the evaluation harness.

One YAML file per document. The client's own documents are confidential — their expected
values *are* the real identification codes, account numbers and director names — so their gold
files live in ``samples/gold/`` next to the documents they describe and are git-ignored. Only
the synthetic set under ``tests/fixtures/gold/`` is committed.

Both sets load through this module and are reported separately, so a published accuracy number
can always say which corpus produced it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from procurement_agent.checks.aoa import ContractorType
from procurement_agent.extraction.schemas import FactType

#: Marks a document that must produce no findings at all.
NO_FINDINGS = "*"


class ExpectedFinding(BaseModel):
    """One finding a check must report, described the way ``docs/GOLD_SPEC.md`` sections 3-4
    describe it: by rule id and, when known, the words it must point at and the fix it must
    propose. Only ``rule`` is required, so an entry can be as specific as the gold can be.
    """

    model_config = ConfigDict(extra="forbid")

    rule: str
    severity: Literal["error", "warning", "info"] | None = None
    #: the offending words exactly as the document writes them (AOA)
    text: str | None = None
    #: the correct variant, in the document's own form; compared with ``Finding.expected``
    correction: str | None = None
    #: how many places in the act a Word comment must mark
    occurrences: int | None = Field(default=None, ge=0)
    #: the value in the earlier document and in the revision (compare)
    before: str | None = None
    after: str | None = None
    #: the English label the comparison's finding must carry, e.g. "Bank code"
    label: str | None = None


class GoldDocument(BaseModel):
    """What a document should yield, written by hand from reading it."""

    model_config = ConfigDict(extra="forbid")

    name: str
    document: Path
    #: Which check to score. ``aoa`` reads the document against its contract; ``compare`` diffs
    #: the two documents against each other and scores the CMP.* rules that fire. The model
    #: forbids extra keys, so a new check has to be named here rather than smuggled in.
    check: Literal["aoa", "compare"] | None = "aoa"
    #: The other document: the contract an act is checked against, or the earlier version a
    #: revision is compared with.
    reference: Path | None = None
    contractor_type: ContractorType | None = None
    #: Expected values per fact type. A type that is absent is not scored, so a partial gold
    #: file is honest rather than penalising fields nobody has labelled yet.
    fields: dict[FactType, list[str]] = Field(default_factory=dict)
    #: Rule ids the AOA check must report.
    expected_findings: list[str] = Field(default_factory=list)
    #: Rule ids that must NOT be reported. "*" means the document must be completely clean,
    #: which is the constraint that matters most: a checker that fires on a correct document
    #: is worse than no checker.
    forbidden_findings: list[str] = Field(default_factory=list)
    #: Target types the gold cannot yet state canonically: skipped by strict scoring (the
    #: reason goes in ``notes``).
    ignore: list[FactType] = Field(default_factory=list)
    #: Types whose values were read off the page by a person rather than derived; strict
    #: scoring reports them apart from the derived ones until the user confirms them.
    provisional: list[FactType] = Field(default_factory=list)
    #: Every finding the check must report, with its words and correction (GOLD_SPEC 3-4).
    findings: list[ExpectedFinding] = Field(default_factory=list)
    #: Rule ids that may be reported without being expected.
    allowed_findings: list[str] = Field(default_factory=list)
    #: Whether anything reported beyond ``findings`` and ``allowed_findings`` is a false positive.
    strict_findings: bool = False
    notes: str | None = None

    @field_validator("document", "reference")
    @classmethod
    def _must_exist(cls, v: Path | None) -> Path | None:
        if v is not None and not v.exists():
            msg = f"gold file points at a missing document: {v}"
            raise ValueError(msg)
        return v

    @property
    def must_be_clean(self) -> bool:
        return NO_FINDINGS in self.forbidden_findings

    @model_validator(mode="after")
    def _compare_needs_two_documents(self) -> GoldDocument:
        if self.check == "compare" and self.reference is None:
            msg = f"gold file {self.name!r} asks for a comparison but names only one document"
            raise ValueError(msg)
        return self


class GoldSet(BaseModel):
    name: str
    documents: list[GoldDocument]

    def __iter__(self) -> Iterator[GoldDocument]:  # type: ignore[override]
        return iter(self.documents)

    def __len__(self) -> int:
        return len(self.documents)


def load_gold_set(directory: Path, *, name: str | None = None, root: Path | None = None) -> GoldSet:
    """Load every ``*.yaml`` in a directory. Relative document paths resolve against ``root``."""
    root = root or Path.cwd()
    documents: list[GoldDocument] = []
    for path in sorted(directory.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        raw.setdefault("name", path.stem)
        for key in ("document", "reference"):
            if raw.get(key):
                candidate = Path(raw[key])
                raw[key] = str(candidate if candidate.is_absolute() else root / candidate)
        documents.append(GoldDocument.model_validate(raw))
    return GoldSet(name=name or directory.name, documents=documents)
