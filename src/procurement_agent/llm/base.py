"""LLM client protocol. Implementations must be local (Ollama today, vLLM later)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, TypeVar

from pydantic import BaseModel

from procurement_agent.extraction.schemas import ChoiceResult

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class LLMUnavailableError(LLMError):
    pass


class LLMClient(Protocol):
    def generate_structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        model: str | None = None,
        max_output_tokens: int | None = None,
    ) -> T:
        """``max_output_tokens`` caps this call's answer; None keeps the client's own cap. A
        caller whose answer grows with its input - one item per question asked - sizes it."""
        ...

    def generate_choice(
        self,
        *,
        document: str,
        question: str,
        options: Sequence[str],
        model: str | None = None,
    ) -> ChoiceResult:
        """Score a fixed option set against a document instead of generating free text.

        ``document`` is passed separately from ``question`` because it must be the *prefix* of
        the prompt: the inference server caches the prefix it has already processed, so several
        questions about one document cost one prefill and then a single decoded token each. Put
        the question first and that saving disappears.
        """
        ...

    def health(self) -> dict[str, object]: ...
