"""The registry an act check consults: the parties' uploaded extracts, then the operator's snapshot.

A party's extract (``registry.extract``) is the agency's own word on that party, as of the day it
was printed; the snapshot (``registry.snapshot``) is the client's export of many, as of its date.
For a code both know, the extract wins. A file uploaded as an extract that is none is kept with
the reason, so the report can say which file did not count instead of passing over it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from procurement_agent.registry.snapshot import RegistryEntry, RegistrySnapshot, normalize_code

__all__ = ["RefusedExtract", "RegistrySource"]


@dataclass(frozen=True, slots=True)
class RefusedExtract:
    """A file uploaded as a registry extract that could not be read as one, and why."""

    name: str
    reason: str


@dataclass
class RegistrySource:
    """Lookups by identification code over the uploaded extracts and the snapshot."""

    snapshot: RegistrySnapshot | None = None
    extracts: dict[str, RegistryEntry] = field(default_factory=dict)
    refused: list[RefusedExtract] = field(default_factory=list)

    @classmethod
    def of(
        cls,
        snapshot: RegistrySnapshot | None,
        extracts: Iterable[RegistryEntry] = (),
        refused: Iterable[RefusedExtract] = (),
    ) -> RegistrySource:
        return cls(
            snapshot=snapshot,
            extracts={normalize_code(e.id_code): e for e in extracts},
            refused=list(refused),
        )

    @property
    def has_snapshot(self) -> bool:
        return self.snapshot is not None

    @property
    def as_of(self) -> str | None:
        return self.snapshot.as_of if self.snapshot is not None else None

    def lookup(self, id_code: str) -> RegistryEntry | None:
        code = normalize_code(id_code)
        found = self.extracts.get(code)
        if found is None and self.snapshot is not None:
            found = self.snapshot.lookup(code)
        return found

    def close(self) -> None:
        if self.snapshot is not None:
            self.snapshot.close()
