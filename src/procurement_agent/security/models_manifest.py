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
stops the start. A file or directory the reading user cannot open is a problem too, named by its
path relative to the models directory (``unreadable ...``): a copy made with ``cp -a`` keeps the
owner and mode of the source, the huggingface cache files in it are mode 600, and the app runs as
another user than the one who copied them. Standard library only.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["MANIFEST_NAME", "Verification", "not_world_readable", "verify", "write"]

MANIFEST_NAME = "MANIFEST.sha256"
_LINE = re.compile(r"([0-9a-f]{64})  (.+)")
_BLOCK = 1024 * 1024


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(_BLOCK):
            h.update(block)
    return h.hexdigest()


def _files(root: Path) -> tuple[list[Path], list[Path], list[Path]]:
    """``(regular files, symlinks, directories that could not be listed)`` under ``root``, the
    manifest itself excluded."""
    files: list[Path] = []
    links: list[Path] = []
    blocked: list[Path] = []

    def cannot_list(error: OSError) -> None:
        if error.filename is not None:
            blocked.append(Path(os.fsdecode(error.filename)))

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=cannot_list):
        for name in [*dirnames, *filenames]:
            path = Path(dirpath, name)
            if path.is_symlink():
                links.append(path)
            elif name in filenames and not (path.parent == root and name == MANIFEST_NAME):
                files.append(path)
    return sorted(files), sorted(links), sorted(blocked)


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def write(root: Path) -> int:
    """Write ``MANIFEST.sha256`` for everything under ``root``; the number of files listed.

    Refuses (``ValueError``, nothing written) when a file cannot be read by the user running this,
    since its digest cannot be known, or when a link would put a file outside the manifest."""
    files, links, blocked = _files(root)
    if links:
        msg = (
            f"{len(links)} symbolic link(s) under {root}: a manifest would not cover their targets"
        )
        raise ValueError(msg)
    if blocked:
        raise ValueError(f"cannot read directory {_relative(blocked[0], root)}")
    lines: list[str] = []
    unreadable: list[str] = []
    for f in files:
        try:
            lines.append(f"{_digest(f)}  {_relative(f, root)}")
        except OSError:
            unreadable.append(_relative(f, root))
    if unreadable:
        more = f" and {len(unreadable) - 1} more" if len(unreadable) > 1 else ""
        raise ValueError(f"cannot read {unreadable[0]}{more}")
    (root / MANIFEST_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def not_world_readable(root: Path) -> list[str]:
    """Paths (relative to ``root``) another user - the app, in its container - could not read:
    a file without the ``other`` read bit, a directory without ``other`` read and search. The
    answer to both is ``chmod -R a+rX`` on the models directory, or owning it as the app user."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames:
            path = Path(dirpath, name)
            if not path.is_symlink() and not _other_can(path, stat.S_IROTH | stat.S_IXOTH):
                found.append(_relative(path, root))
        for name in filenames:
            path = Path(dirpath, name)
            if not path.is_symlink() and not _other_can(path, stat.S_IROTH):
                found.append(_relative(path, root))
    return sorted(found)


def _other_can(path: Path, bits: int) -> bool:
    try:
        return path.stat().st_mode & bits == bits
    except OSError:
        return False


@dataclass
class Verification:
    checked: int = 0
    problems: list[str] = field(default_factory=list)
    #: paths (relative to the models directory) the reading user could not open; each one is also
    #: an ``unreadable ...`` entry in ``problems``
    unreadable: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def verify(root: Path) -> Verification:
    """Compare ``root`` with its manifest. Reports every difference, not only the first."""
    result = Verification()
    if not os.access(root, os.R_OK | os.X_OK):
        result.problems.append("unreadable the models directory")
        result.unreadable.append("the models directory")
        return result
    manifest = root / MANIFEST_NAME
    if not manifest.is_file():
        result.problems.append(f"{MANIFEST_NAME} is missing")
        return result
    try:
        manifest_text = manifest.read_text(encoding="utf-8")
    except OSError:
        result.problems.append(f"unreadable {MANIFEST_NAME}")
        result.unreadable.append(MANIFEST_NAME)
        return result
    expected: dict[str, str] = {}
    for number, line in enumerate(manifest_text.splitlines(), 1):
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
    files, links, blocked = _files(root)
    for link in links:
        result.problems.append(f"symbolic link {_relative(link, root)}")
    hidden = [_relative(d, root) for d in blocked]
    for name in hidden:
        result.problems.append(f"unreadable directory {name}")
        result.unreadable.append(name)
    present = {_relative(f, root): f for f in files}
    for relative, want in sorted(expected.items()):
        path = present.get(relative)
        if path is None:
            if not any(relative.startswith(name + "/") for name in hidden):
                result.problems.append(f"missing {relative}")
            continue
        try:
            digest = _digest(path)
        except FileNotFoundError:  # gone between the listing and the read
            result.problems.append(f"missing {relative}")
            continue
        except OSError:
            result.problems.append(f"unreadable {relative}")
            result.unreadable.append(relative)
            continue
        result.checked += 1
        if digest != want:
            result.problems.append(f"changed {relative}")
    for relative in sorted(set(present) - set(expected)):
        result.problems.append(f"not in the manifest: {relative}")
    return result
