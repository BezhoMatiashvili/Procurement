"""A checksum manifest for the model files, verified when production starts.

The models are downloaded once, on a machine with internet access (``scripts/prefetch_models.sh``),
and copied to the server that never has any. Between the download and every start there is a copy,
a disk, a volume: nothing said the files that load are the files that were downloaded. The formats
in use are safetensors and ONNX, which cannot carry code the way a pickle can - so this is not a
defence against remote code execution through a model, it is the check that the models are the ones
that were evaluated: a swapped or truncated weight file changes every extraction, silently.

``MANIFEST.sha256`` is written next to the models in coreutils format (``sha256sum -c`` reads it)::

    <64 hex digits>  <path relative to the models directory>

``write`` after the download, ``verify`` at every production start; a missing, changed or extra
file, a symlink, or a manifest line that reaches outside the directory is a problem, and a problem
stops the start. Standard library only.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["MANIFEST_NAME", "Verification", "verify", "write"]

MANIFEST_NAME = "MANIFEST.sha256"
_LINE = re.compile(r"([0-9a-f]{64})  (.+)")
_BLOCK = 1024 * 1024


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(_BLOCK):
            h.update(block)
    return h.hexdigest()


def _files(root: Path) -> tuple[list[Path], list[Path]]:
    """``(regular files, symlinks)`` under ``root``, the manifest itself excluded."""
    files: list[Path] = []
    links: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in [*dirnames, *filenames]:
            path = Path(dirpath, name)
            if path.is_symlink():
                links.append(path)
            elif name in filenames and not (path.parent == root and name == MANIFEST_NAME):
                files.append(path)
    return sorted(files), sorted(links)


def write(root: Path) -> int:
    """Write ``MANIFEST.sha256`` for everything under ``root``; the number of files listed."""
    files, links = _files(root)
    if links:
        msg = (
            f"{len(links)} symbolic link(s) under {root}: a manifest would not cover their targets"
        )
        raise ValueError(msg)
    lines = [f"{_digest(f)}  {f.relative_to(root).as_posix()}" for f in files]
    (root / MANIFEST_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


@dataclass
class Verification:
    checked: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def verify(root: Path) -> Verification:
    """Compare ``root`` with its manifest. Reports every difference, not only the first."""
    result = Verification()
    manifest = root / MANIFEST_NAME
    if not manifest.is_file():
        result.problems.append(f"{MANIFEST_NAME} is missing")
        return result
    expected: dict[str, str] = {}
    for number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        parsed = _LINE.fullmatch(line)
        if parsed is None:
            result.problems.append(f"{MANIFEST_NAME} line {number} is not '<sha256>  <path>'")
            continue
        relative = parsed.group(2)
        target = Path(relative)
        if target.is_absolute() or ".." in target.parts or relative.startswith("./"):
            result.problems.append(
                f"{MANIFEST_NAME} line {number} names a path outside the directory"
            )
            continue
        expected[relative] = parsed.group(1)
    files, links = _files(root)
    for link in links:
        result.problems.append(f"symbolic link {link.relative_to(root).as_posix()}")
    present = {f.relative_to(root).as_posix(): f for f in files}
    for relative, want in sorted(expected.items()):
        path = present.get(relative)
        if path is None:
            result.problems.append(f"missing {relative}")
            continue
        result.checked += 1
        if _digest(path) != want:
            result.problems.append(f"changed {relative}")
    for relative in sorted(set(present) - set(expected)):
        result.problems.append(f"not in the manifest: {relative}")
    return result
