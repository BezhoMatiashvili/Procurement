#!/usr/bin/env python3
"""The production dependency closure that uv.lock pins: the one definition of "what ships".

    from uvlock import read_lock, production_closure   # scripts/sbom.py, scripts/third_party.py

Not a command; it is imported by scripts/sbom.py and scripts/third_party.py, and by their tests, so
that the SBOM, the licence inventory and the tests that keep them current all list the same set.

The closure is what ``uv sync --frozen --no-dev`` installs in the Dockerfile, on the platform the
image is built for: the project's ``[project].dependencies`` and everything they need, following the
extras a dependency names (``uvicorn[standard]``), but no dependency group (``dev``) and no optional
extra of the project itself (``embeddings``). A dependency whose marker is false on that platform is
left out: the lock holds both torch builds (the macOS one and the CPU build for everything else),
and the Windows-only ``colorama``; the image has one of them and not the other.

Only the standard library and ``packaging`` (itself in the production closure) are used.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packaging.markers import Marker

#: The platform the image is built for. python_full_version is the Dockerfile's PYTHON_IMAGE tag;
#: tests/unit/test_sbom.py fails when the two differ.
TARGET_ENVIRONMENT: dict[str, str] = {
    "implementation_name": "cpython",
    "implementation_version": "3.12.14",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "platform_python_implementation": "CPython",
    "platform_release": "",
    "platform_system": "Linux",
    "platform_version": "",
    "python_full_version": "3.12.14",
    "python_version": "3.12",
    "sys_platform": "linux",
    "extra": "",
}

SUPPORTED_LOCK_VERSION = 1


class LockError(Exception):
    """uv.lock is not what this script understands, or is not consistent."""


@dataclass(frozen=True)
class LockedFile:
    """One distribution file the lock records: a source archive or a wheel, with its hash."""

    kind: str  # "sdist" | "wheel"
    url: str
    sha256: str


@dataclass(frozen=True)
class Package:
    name: str
    version: str
    source: str
    files: tuple[LockedFile, ...]
    depends_on: tuple[str, ...]  # keys ("name@version") of the packages it needs here
    required_by: tuple[str, ...]  # keys of the packages that need it here, the project included

    @property
    def key(self) -> str:
        return f"{self.name}@{self.version}"


@dataclass(frozen=True)
class Closure:
    root: Package
    packages: tuple[Package, ...]  # sorted by name, then version; the root is not among them
    environment: dict[str, str]


def read_lock(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        lock: dict[str, Any] = tomllib.load(fh)
    if lock.get("version") != SUPPORTED_LOCK_VERSION:
        raise LockError(
            f"{path} has lock format version {lock.get('version')!r}; this script reads "
            f"version {SUPPORTED_LOCK_VERSION}. Check what changed before trusting its output."
        )
    return lock


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_text(source: dict[str, Any]) -> str:
    for kind in ("registry", "editable", "virtual", "url", "git", "path", "directory"):
        if kind in source:
            return f"{kind}:{source[kind]}"
    return "unknown"


def _files(entry: dict[str, Any]) -> tuple[LockedFile, ...]:
    found: list[LockedFile] = []
    sdist = entry.get("sdist")
    if sdist:
        found.append(_locked_file("sdist", sdist))
    found.extend(_locked_file("wheel", w) for w in entry.get("wheels", []))
    return tuple(sorted(found, key=lambda f: (f.url, f.sha256)))


def _locked_file(kind: str, item: dict[str, Any]) -> LockedFile:
    digest = str(item.get("hash", ""))
    if not digest.startswith("sha256:"):
        raise LockError(f"a {kind} of the lock has no sha256 hash: {item.get('url')}")
    return LockedFile(
        kind=kind, url=str(item.get("url", "")), sha256=digest.removeprefix("sha256:")
    )


def _find_root(entries: list[dict[str, Any]]) -> dict[str, Any]:
    roots = [e for e in entries if "editable" in e["source"] or "virtual" in e["source"]]
    if len(roots) != 1:
        raise LockError(f"the lock must name exactly one project root, it names {len(roots)}")
    return roots[0]


def _resolve(entries: list[dict[str, Any]], dep: dict[str, Any], needed_by: str) -> dict[str, Any]:
    candidates = [e for e in entries if e["name"] == dep["name"]]
    if "version" in dep:
        candidates = [e for e in candidates if e.get("version") == dep["version"]]
    if "source" in dep:
        candidates = [e for e in candidates if e["source"] == dep["source"]]
    if len(candidates) != 1:
        raise LockError(
            f"{needed_by} needs {dep['name']!r}, which matches {len(candidates)} packages "
            f"in the lock (expected exactly one)"
        )
    return candidates[0]


def _applies(dep: dict[str, Any], environment: dict[str, str]) -> bool:
    marker = dep.get("marker")
    return True if not marker else bool(Marker(marker).evaluate(environment))


def _key(entry: dict[str, Any]) -> str:
    return f"{entry['name']}@{entry.get('version', '')}"


def production_closure(lock: dict[str, Any], environment: dict[str, str] | None = None) -> Closure:
    env = dict(TARGET_ENVIRONMENT if environment is None else environment)
    entries: list[dict[str, Any]] = lock["package"]
    root = _find_root(entries)

    extras_seen: dict[str, set[str]] = {}
    edges: dict[str, set[str]] = {}
    by_key: dict[str, dict[str, Any]] = {_key(root): root}
    pending: list[tuple[dict[str, Any], tuple[str, ...]]] = [(root, ())]
    while pending:
        entry, extras = pending.pop()
        key = _key(entry)
        known = extras_seen.setdefault(key, set())
        if key in edges and set(extras) <= known:
            continue
        known.update(extras)
        edges.setdefault(key, set())
        wanted = list(entry.get("dependencies", []))
        for extra in sorted(known):
            wanted.extend(entry.get("optional-dependencies", {}).get(extra, []))
        for dep in wanted:
            if not _applies(dep, env):
                continue
            target = _resolve(entries, dep, key)
            by_key[_key(target)] = target
            edges[key].add(_key(target))
            pending.append((target, tuple(dep.get("extra", ()))))

    reverse: dict[str, set[str]] = {k: set() for k in edges}
    for source, targets in edges.items():
        for needed in targets:
            reverse[needed].add(source)

    def build(key: str) -> Package:
        entry = by_key[key]
        return Package(
            name=entry["name"],
            version=str(entry.get("version", "")),
            source=_source_text(entry["source"]),
            files=_files(entry),
            depends_on=tuple(sorted(edges[key])),
            required_by=tuple(sorted(reverse[key])),
        )

    root_key = _key(root)
    packages = sorted((build(k) for k in edges if k != root_key), key=lambda p: (p.name, p.version))
    return Closure(root=build(root_key), packages=tuple(packages), environment=env)
