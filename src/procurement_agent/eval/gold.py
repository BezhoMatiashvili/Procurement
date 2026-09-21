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

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from procurement_agent.checks.aoa import ContractorType
from procurement_agent.extraction.schemas import FactType

#: Marks a document that must produce no findings at all.
NO_FINDINGS = "*"


class GoldDocument(BaseModel):
    """What a document should yield, written by hand from reading it."""

    model_config = ConfigDict(extra="forbid")

    name: str
    document: Path
    #: The contract an acceptance act is checked against, if any.
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
