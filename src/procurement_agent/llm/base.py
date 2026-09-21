"""LLM client protocol. Implementations must be local (Ollama today, vLLM later)."""

from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class LLMUnavailableError(LLMError):
    pass


class LLMClient(Protocol):
    def generate_structured(
        self, *, system: str, user: str, schema: type[T], model: str | None = None
    ) -> T: ...

    def health(self) -> dict[str, object]: ...
