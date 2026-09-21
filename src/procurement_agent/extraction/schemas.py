"""Pydantic models shared by the extraction pipeline and the API."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FactType(StrEnum):
    # procurement-specific
    price = "price"
    total_amount = "total_amount"
    vat_status = "vat_status"
    quantity = "quantity"
    delivery_deadline = "delivery_deadline"
    execution_period = "execution_period"
    payment_terms = "payment_terms"
    warranty_period = "warranty_period"
    validity_period = "validity_period"
    penalty = "penalty"
    contract_date = "contract_date"
    contract_number = "contract_number"
    party_name = "party_name"
    party_id_code = "party_id_code"
    party_director = "party_director"
    party_address = "party_address"
    bank_account = "bank_account"
    cadastral_code = "cadastral_code"
    project_name = "project_name"
    subject = "subject"
    # generic (mostly produced by rules; the LLM refines them into the specific types above)
    amount = "amount"
    date = "date"
    percentage = "percentage"
    duration = "duration"
    id_code = "id_code"
    other = "other"


DEFAULT_TARGETS: tuple[FactType, ...] = (
    FactType.price,
    FactType.total_amount,
    FactType.vat_status,
    FactType.quantity,
    FactType.delivery_deadline,
    FactType.execution_period,
    FactType.payment_terms,
    FactType.warranty_period,
    FactType.validity_period,
    FactType.penalty,
    FactType.contract_date,
    FactType.contract_number,
    FactType.party_name,
    FactType.party_id_code,
    FactType.party_director,
    FactType.party_address,
    FactType.bank_account,
    FactType.cadastral_code,
    FactType.project_name,
    FactType.subject,
)


class Provenance(BaseModel):
    """Where a fact was found. At least one locator is always present."""

    model_config = ConfigDict(frozen=True)

    page: int | None = Field(default=None, description="1-based page number (PDF/DOCX)")
    bbox: tuple[float, float, float, float] | None = Field(
        default=None, description="(left, top, right, bottom) in PDF points, top-left origin"
    )
    sheet: str | None = Field(default=None, description="worksheet name (XLSX)")
    cell: str | None = Field(default=None, description="cell reference, e.g. C12 (XLSX)")
    chunk_id: str | None = None


class ExtractedFact(BaseModel):
    type: FactType
    label: str = Field(description="human-readable label, e.g. 'Warranty period'")
    value: str = Field(description="value as it should be shown to the user")
    normalized: str | float | None = Field(
        default=None, description="ISO date, decimal number, or canonical string when derivable"
    )
    unit: str | None = None
    currency: str | None = None
    raw_text: str = Field(description="verbatim snippet from the document that supports the fact")
    provenance: Provenance
    source: Literal["anchor", "rules", "llm", "both"]
    confidence: float = Field(ge=0.0, le=1.0)
    conflict: bool = Field(
        default=False, description="another fact of the same type has a different value"
    )
    note: str | None = None


class ExtractionResult(BaseModel):
    document_id: str
    facts: list[ExtractedFact]
    targets: list[FactType]
    question: str | None = None
    warnings: list[str] = Field(default_factory=list)
    model: str | None = None
    llm_calls: int = 0
    seconds: float = 0.0
    # Provenance of the configuration that produced this result, so an evaluation row can
    # be attributed to a prompt/sampling configuration rather than just to a model name.
    prompt_version: str | None = None
    llm_options: dict[str, int | float | str] = Field(default_factory=dict)
    llm_fallback_fields: list[FactType] = Field(
        default_factory=list,
        description="targets no anchor or rule produced, i.e. left for the LLM to decide",
    )


# --- what the LLM is asked to produce (kept deliberately small and flat) ------------------------


class LLMFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: FactType
    value: str = Field(description="the extracted value, concise")
    raw_text: str = Field(description="exact quote from the text that contains the value")
    unit: str | None = None
    currency: str | None = None


class LLMExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facts: list[LLMFact] = Field(default_factory=list)


class LLMAnswer(BaseModel):
    """Free-form question mode."""

    model_config = ConfigDict(extra="forbid")

    found: bool
    answer: str = Field(description="short answer; empty if not found")
    raw_text: str = Field(description="exact quote supporting the answer; empty if not found")
