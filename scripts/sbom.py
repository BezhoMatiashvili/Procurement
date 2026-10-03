#!/usr/bin/env python3
"""Write a CycloneDX 1.5 SBOM of what uv.lock pins for production, byte for byte reproducible.

    uv run python scripts/sbom.py --out docs/sbom.cdx.json     # (re)generate the committed file
    uv run python scripts/sbom.py --check docs/sbom.cdx.json   # exit 1 when it is stale
    uv run python scripts/sbom.py                              # to standard output
    uv run python scripts/sbom.py --out X --timestamp [ISO-8601]   # stamp it (default: now)

Scope: the packages ``uv sync --frozen --no-dev`` puts in the image (scripts/uvlock.py): the project
as the root component, its production dependencies and theirs, for linux / x86_64 / CPython 3.12.
No dependency group, no optional extra; a package that applies to another platform only is out.
Each component carries its name, version, ``pkg:pypi`` purl (a local version such as ``+cpu`` is
percent-encoded, as the purl specification asks), every file the lock records for it with its
SHA-256 (``externalReferences`` of type ``distribution``: a package has one source archive and
many wheels, and each file has its own hash), and its licence as scripts/licences.py reads it from
the installed distribution - which must be the locked version, or the build stops.

Why not ``uv export --format cyclonedx1.5``: it is experimental ("may change without warning"),
puts a random serial number and a timestamp in every run, numbers the bom-refs by position
(``accelerate-2@1.15.0``), carries no licence, and attaches no hash to a component. It also lists
six packages this image never installs: ``colorama``, ``pywin32`` and ``tzdata`` (Windows only),
``torch`` and ``torchvision`` for macOS beside the CPU builds, and ``opencv-python``, the GUI
OpenCV that pyproject.toml overrides away (marker ``sys_platform == 'never'``). The file here is
the same lock read with ``tomllib``, with the markers evaluated.

Determinism: sorted components and graph, no timestamp unless asked for, and a serial number
derived from the SHA-256 of uv.lock, so two runs on one lock give identical files. The lock's
hash is also recorded in ``metadata.properties`` (``pa:uv-lock-sha256``), which is how
scripts/make_release_bundle.sh refuses a stale SBOM without running Python.
Exit status: 0 success (or up to date), 1 stale or an error, 2 bad usage.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

import licences
import uvlock

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = ROOT / "uv.lock"

#: The project's own licence, as pyproject.toml declares it: no one else's terms apply to it.
PROPRIETARY = licences.Licence(
    "known", "Proprietary", None, ("Proprietary",), (), "pyproject.toml", ""
)

_SCOPE = (
    "production: [project].dependencies and the extras they name, as `uv sync --frozen --no-dev` "
    "installs them; no dependency groups, no optional extras of the project"
)

LicenceLookup = Callable[[str, str], licences.Licence]


def purl(name: str, version: str) -> str:
    """``pkg:pypi/<name>@<version>``: the name already lower-case with dashes in the lock; the
    version percent-encoded (``2.14.1+cpu`` -> ``2.14.1%2Bcpu``)."""
    return f"pkg:pypi/{name}@{quote(version, safe='')}"


def cyclonedx_licences(licence: licences.Licence) -> list[dict[str, Any]] | None:
    """A CycloneDX ``licenses`` array: ``license.id`` for one SPDX id, ``expression`` (alone) for a
    compound expression, ``license.name`` for a declared name that is no SPDX id. ``None`` for a
    licence nobody could name; the component then carries none rather than an invented one."""
    if licence.status == "unknown":
        return None
    if licence.status == "conflict":
        return [{"license": {"name": f"UNRESOLVED: {licence.note} (see docs/THIRD_PARTY.md)"}}]
    if licence.spdx:
        if any(op in licence.spdx.split() for op in ("AND", "OR", "WITH")) or "(" in licence.spdx:
            return [{"expression": licence.spdx}]
        return [{"license": {"id": licence.spdx}}]
    return [{"license": {"name": licence.declared}}]


def build_sbom(
    closure: uvlock.Closure,
    *,
    lock_sha256: str,
    root_licence: licences.Licence,
    licence_of: LicenceLookup,
    timestamp: str | None = None,
) -> dict[str, Any]:
    root = closure.root
    root_ref = f"{root.name}@{root.version}"
    environment = closure.environment
    root_component: dict[str, Any] = {
        "type": "application",
        "bom-ref": root_ref,
        "name": root.name,
        "version": root.version,
    }
    root_licences = cyclonedx_licences(root_licence)
    if root_licences:
        root_component["licenses"] = root_licences

    components: list[dict[str, Any]] = []
    ref_of: dict[str, str] = {}
    for pkg in closure.packages:
        ref = purl(pkg.name, pkg.version)
        ref_of[pkg.key] = ref
        component: dict[str, Any] = {
            "type": "library",
            "bom-ref": ref,
            "name": pkg.name,
            "version": pkg.version,
            "purl": ref,
        }
        licence = licence_of(pkg.name, pkg.version)
        encoded = cyclonedx_licences(licence)
        if encoded:
            component["licenses"] = encoded
        else:
            component["properties"] = [{"name": "pa:licence", "value": "unknown"}]
        component["externalReferences"] = [
            {
                "type": "distribution",
                "url": f.url,
                "hashes": [{"alg": "SHA-256", "content": f.sha256}],
            }
            for f in pkg.files
        ]
        components.append(component)
    components.sort(key=lambda c: str(c["bom-ref"]))

    graph = [
        {
            "ref": root_ref,
            "dependsOn": sorted(ref_of[k] for k in root.depends_on),
        }
    ] + [
        {"ref": ref_of[p.key], "dependsOn": sorted(ref_of[k] for k in p.depends_on)}
        for p in closure.packages
    ]
    graph.sort(key=lambda d: str(d["ref"]))

    metadata: dict[str, Any] = {}
    if timestamp:
        metadata["timestamp"] = timestamp
    metadata["tools"] = {
        "components": [{"type": "application", "name": "scripts/sbom.py", "group": root.name}]
    }
    metadata["component"] = root_component
    metadata["properties"] = [
        {"name": "pa:uv-lock-sha256", "value": lock_sha256},
        {"name": "pa:scope", "value": _SCOPE},
        {
            "name": "pa:target-environment",
            "value": (
                f"sys_platform={environment['sys_platform']} "
                f"platform_machine={environment['platform_machine']} "
                f"{environment['platform_python_implementation']} "
                f"{environment['python_full_version']}"
            ),
        },
    ]
    serial = uuid.uuid5(uuid.NAMESPACE_URL, f"urn:procurement-agent:uv.lock:sha256:{lock_sha256}")
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": metadata,
        "components": components,
        "dependencies": graph,
    }


def render(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=True) + "\n"


def _project_licence(lock_path: Path) -> licences.Licence:
    pyproject = lock_path.parent / "pyproject.toml"
    with pyproject.open("rb") as fh:
        declared = tomllib.load(fh).get("project", {}).get("license")
    text = declared.get("text") if isinstance(declared, dict) else declared
    if not text:
        return licences.Licence(
            "unknown", "", None, (), (), "none", "pyproject.toml declares no licence"
        )
    if text == "Proprietary":
        return PROPRIETARY
    raise SystemExit(f"{pyproject}: licence {text!r} is not one this script knows how to state")


def _installed_licence(name: str, version: str) -> licences.Licence:
    return licences.classify(name, version, licences.read_raw(licences.installed(name, version)))


def generate(lock_path: Path, timestamp: str | None = None) -> str:
    lock = uvlock.read_lock(lock_path)
    return render(
        build_sbom(
            uvlock.production_closure(lock),
            lock_sha256=uvlock.file_sha256(lock_path),
            root_licence=_project_licence(lock_path),
            licence_of=_installed_licence,
            timestamp=timestamp,
        )
    )


def _without_timestamp(text: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(text)
    document.get("metadata", {}).pop("timestamp", None)
    return document


def _summary(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    def listed(doc: dict[str, Any]) -> dict[str, str]:
        return {str(c["name"]): str(c["version"]) for c in doc.get("components", [])}

    before, after = listed(old), listed(new)
    lines = [f"  added:   {n} {after[n]}" for n in sorted(after.keys() - before.keys())]
    lines += [f"  removed: {n} {before[n]}" for n in sorted(before.keys() - after.keys())]
    lines += [
        f"  changed: {n} {before[n]} -> {after[n]}"
        for n in sorted(before.keys() & after.keys())
        if before[n] != after[n]
    ]
    return lines


def check(lock_path: Path, sbom_path: Path) -> list[str]:
    """Why ``sbom_path`` is not what ``lock_path`` produces now (an empty list: it is current)."""
    if not sbom_path.is_file():
        return [f"{sbom_path} does not exist"]
    try:
        committed = _without_timestamp(sbom_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"{sbom_path} cannot be read as JSON: {exc}"]
    fresh = _without_timestamp(generate(lock_path))
    if committed == fresh:
        return []
    problems = [f"{sbom_path} is stale: it differs from what {lock_path.name} produces now"]
    problems += _summary(committed, fresh)
    problems.append(f"regenerate it: uv run python scripts/sbom.py --out {sbom_path}")
    return problems


def _timestamp(value: str) -> str:
    if value != "now":
        return value
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    moment = datetime.fromtimestamp(int(epoch), UTC) if epoch else datetime.now(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="CycloneDX 1.5 SBOM of the production dependencies"
    )
    parser.add_argument(
        "--lock", type=Path, default=DEFAULT_LOCK, help="uv.lock (default: the repo's)"
    )
    parser.add_argument("--out", type=Path, help="write here instead of standard output")
    parser.add_argument("--check", type=Path, metavar="FILE", help="exit 1 when FILE is stale")
    parser.add_argument(
        "--timestamp",
        nargs="?",
        const="now",
        metavar="ISO-8601",
        help="add metadata.timestamp (no value: SOURCE_DATE_EPOCH, else now); off by default",
    )
    args = parser.parse_args(argv)
    try:
        if args.check:
            problems = check(args.lock, args.check)
            if problems:
                print("\n".join(problems), file=sys.stderr)
                return 1
            print(f"{args.check} is up to date with {args.lock.name}")
            return 0
        text = generate(args.lock, _timestamp(args.timestamp) if args.timestamp else None)
    except (uvlock.LockError, licences.NotInstalledError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.out:
        args.out.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
