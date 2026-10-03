#!/usr/bin/env python3
"""Read what an installed distribution declares as its licence, and classify it without guessing.

    from licences import installed, read_raw, classify   # scripts/sbom.py, scripts/third_party.py

Not a command; it is imported by scripts/sbom.py and scripts/third_party.py and by their tests.

What is read, in this order of precedence (the first that names a licence wins):

1. ``License-Expression`` (PEP 639), checked as an SPDX expression by ``packaging.licenses``;
2. ``License``, when it is a short single line: an SPDX expression, or one of the strings in
   FREE_TEXT below. A whole licence text in this field (pandas, scipy) is not a name and is skipped;
   ``UNKNOWN`` is a placeholder and is skipped;
3. the ``License ::`` Trove classifiers, through CLASSIFIERS below.

The tables are small and reviewed on purpose. A name that leaves the variant open ("BSD" without a
clause count, "Apache" without a version) gets a family and no SPDX id; a name or an SPDX id that is
in no table is "unknown", with the text quoted, and is listed for a person to decide. A licence file
whose title names a copyleft licence the metadata does not mention (spylls: metadata says MIT, the
file shipped is the Mozilla Public License 2.0) makes the package a "conflict", also for a person.

Separately, every licence-looking file a distribution ships is scanned for licence titles and
``License:`` lines of copyleft licences (``Raw.notices``): wheels bundle shared libraries (numpy's
libgfortran, shapely's GEOS) whose terms are in those files and not in the metadata. A hit is a
tripwire, not a verdict; scripts/third_party.py lists each one with a reviewed assessment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from importlib.metadata import Distribution, PackageNotFoundError, distribution
from pathlib import Path, PurePosixPath

from packaging.licenses import InvalidLicenseExpression, canonicalize_license_expression

#: Families that are copyleft or restrict use. A weak copyleft (MPL, LGPL) is on the list: it is
#: what a person shipping the package inside a proprietary product has to read.
COPYLEFT_FAMILIES = frozenset(
    {"MPL", "EPL", "CDDL", "LGPL", "GPL", "AGPL", "CC-BY-SA", "SSPL", "non-commercial"}
)

# SPDX id -> family, for the permissive licences met so far. An id that is not here (and is not
# matched by _COPYLEFT_ID) is "unknown" until a person adds it.
_PERMISSIVE_ID = {
    "MIT": "MIT",
    "MIT-CMU": "MIT",
    "ISC": "ISC",
    "BSD-2-Clause": "BSD",
    "BSD-3-Clause": "BSD",
    "Apache-2.0": "Apache",
    "PSF-2.0": "PSF",
    "0BSD": "Other permissive",
    "Zlib": "Other permissive",
    "CC0-1.0": "Other permissive",
    "BSL-1.0": "Other permissive",
    "CNRI-Python": "Other permissive",
}
_COPYLEFT_ID = (
    (re.compile(r"^AGPL-"), "AGPL"),
    (re.compile(r"^LGPL-"), "LGPL"),
    (re.compile(r"^GPL-"), "GPL"),
    (re.compile(r"^MPL-"), "MPL"),
    (re.compile(r"^EPL-"), "EPL"),
    (re.compile(r"^CDDL-"), "CDDL"),
    (re.compile(r"^CC-BY-SA-"), "CC-BY-SA"),
    (re.compile(r"^SSPL-"), "SSPL"),
    (re.compile(r"^CC-BY-NC"), "non-commercial"),
)
#: SPDX exceptions: they widen a licence's permissions and change no family.
_EXCEPTION_ID = frozenset({"LLVM-exception", "GCC-exception-3.1", "Classpath-exception-2.0"})

#: Free-text ``License`` values that name one licence unambiguously (compared case-folded, with
#: runs of white space collapsed). ``PSFL`` is the Python Software Foundation License version 2: the
#: text defusedxml ships is titled "Python Software Foundation License Version 2".
FREE_TEXT = {
    "mit license": "MIT",
    "apache 2.0": "Apache-2.0",
    "apache 2.0 license": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "bsd 3-clause": "BSD-3-Clause",
    "bsd 3-clause license": "BSD-3-Clause",
    "isc license": "ISC",
    "psfl": "PSF-2.0",
}
#: Free-text values that name a family and leave the variant open: no SPDX id is claimed.
FREE_TEXT_FAMILY = {
    "bsd": ("BSD", "BSD (variant not stated)"),
    "bsd license": ("BSD", "BSD (variant not stated)"),
    "apache": ("Apache", "Apache (version not stated)"),
    "apache license": ("Apache", "Apache (version not stated)"),
}

#: Trove ``License ::`` classifiers (the part after "License :: "; "OSI Approved :: " dropped).
#: (SPDX id or None, family, text shown when no id is claimed)
CLASSIFIERS: dict[str, tuple[str | None, str, str]] = {
    "MIT License": ("MIT", "MIT", "MIT"),
    "ISC License (ISCL)": ("ISC", "ISC", "ISC"),
    "Python Software Foundation License": ("PSF-2.0", "PSF", "PSF-2.0"),
    "Mozilla Public License 2.0 (MPL 2.0)": ("MPL-2.0", "MPL", "MPL-2.0"),
    "BSD License": (None, "BSD", "BSD (variant not stated)"),
    "Apache Software License": (None, "Apache", "Apache (version not stated)"),
}
_CLASSIFIER_PREFIX = (
    ("GNU Affero General Public License", "AGPL"),
    ("GNU Lesser General Public License", "LGPL"),
    ("GNU Library or Lesser General Public License", "LGPL"),
    ("GNU General Public License", "GPL"),
)

#: Licences read by hand where the metadata is a list and not an SPDX expression. Keyed by name AND
#: version, and only applied while the field still reads as written here: a new release is looked at
#: again. (name, version) -> (License field as written, SPDX expression, why)
REVIEWED: dict[tuple[str, str], tuple[str, str, str]] = {
    ("pypdfium2", "5.13.0"): (
        "BSD-3-Clause, Apache-2.0, dependency licenses",
        "Apache-2.0 AND BSD-3-Clause",
        "licences/ holds Apache-2.0 and BSD-3-Clause texts (pypdfium2's own code and PDFium); the "
        "licences of the libraries built into the PDFium binary are in BUILD_LICENSES/",
    ),
}

_TITLES = (
    (re.compile(r"GNU\s+(?:Lesser|Library)\s+General\s+Public\s+License", re.I), "LGPL"),
    (re.compile(r"GNU\s+Affero\s+General\s+Public\s+License", re.I), "AGPL"),
    (re.compile(r"GNU\s+General\s+Public\s+License", re.I), "GPL"),
    (re.compile(r"Mozilla\s+Public\s+License", re.I), "MPL"),
    (re.compile(r"Eclipse\s+Public\s+License", re.I), "EPL"),
    (re.compile(r"Common\s+Development\s+and\s+Distribution\s+License", re.I), "CDDL"),
)
_LICENSE_LINE = re.compile(r"^\s*(?:SPDX-License-Identifier|Licen[sc]e)\s*:\s*(.+)$", re.I | re.M)
_LICENSE_LINE_FAMILY = (
    (re.compile(r"\bAGPL-?\d"), "AGPL"),
    (re.compile(r"\bLGPL-?\d"), "LGPL"),
    (re.compile(r"(?<![AL])GPL-?\d"), "GPL"),
    (re.compile(r"\bMPL-?\d"), "MPL"),
    (re.compile(r"\bEPL-?\d"), "EPL"),
    (re.compile(r"\bCDDL-?\d"), "CDDL"),
    (re.compile(r"\bCC-BY-SA"), "CC-BY-SA"),
    (re.compile(r"\bSSPL"), "SSPL"),
)
_NON_COMMERCIAL = re.compile(r"\bnon-?commercial\b", re.I)
_PRIMARY_NAME = re.compile(r"^(licen[sc]e|copying|unlicense)(\.[a-z0-9]+)?$", re.I)
_NOTICE_NAME = re.compile(r"^(licen[sc]e|copying|notice|copyright|unlicense)", re.I)
_NOT_TEXT = frozenset(
    {".py", ".pyc", ".pyi", ".so", ".dylib", ".dll", ".json", ".csv", ".h", ".c", ".cpp", ".pem"}
)
_PRIMARY_LINES = 15  # a title counts as the package's own only when it opens the file
_MAX_FILE_BYTES = 5_000_000
_FIELD_MAX = 100


class NotInstalledError(Exception):
    """The distribution is missing, or is not the version uv.lock pins."""


@dataclass(frozen=True)
class Raw:
    """What an installed distribution says about its licence, before any classification."""

    expression: str | None
    license_field: str | None
    classifiers: tuple[str, ...]
    #: (file, family) for a primary licence file (LICENSE, COPYING) whose opening lines are titled
    #: with a copyleft licence
    primary_titles: tuple[tuple[str, str], ...]
    #: (file, family) wherever a licence-looking file names a copyleft licence: a title line, a
    #: ``License:`` line, or non-commercial terms
    notices: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Licence:
    status: str  # "known" | "unknown" | "conflict"
    declared: str  # what to show: an SPDX expression, or the family with its open variant said
    spdx: str | None  # only when the metadata unambiguously names licences by SPDX id
    families: tuple[str, ...]
    copyleft: tuple[str, ...]
    basis: str  # "License-Expression" | "License" | "Classifier" | "reviewed: ..." | "none"
    note: str


def installed_any(name: str) -> Distribution:
    try:
        return distribution(name)
    except PackageNotFoundError:
        raise NotInstalledError(
            f"{name} is not installed in this environment, and its licence cannot be read: "
            f"run `uv sync --frozen` (or use the project's environment) and try again"
        ) from None


def installed(name: str, version: str) -> Distribution:
    dist = installed_any(name)
    if dist.version != version:
        raise NotInstalledError(
            f"{name} {dist.version} is installed but uv.lock pins {version}; the locked version "
            f"is the one whose licence is reported: run `uv sync --frozen` and try again"
        )
    return dist


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _is_licence_file(path: PurePosixPath) -> bool:
    if path.suffix.lower() in _NOT_TEXT:
        return False
    if _NOTICE_NAME.match(path.name):
        return True
    return any(
        part.lower() in {"licenses", "licences", "build_licenses"} for part in path.parts[:-1]
    )


def _is_primary(path: PurePosixPath) -> bool:
    """LICENSE / COPYING at the top of the distribution's metadata directory (or its licenses/)."""
    parts = path.parts
    if not parts or not parts[0].endswith(".dist-info"):
        return False
    inside = parts[1:]
    if len(inside) == 2 and inside[0] == "licenses":
        inside = inside[1:]
    return len(inside) == 1 and bool(_PRIMARY_NAME.match(inside[0]))


def _title_family(line: str) -> str | None:
    text = line.strip()
    for pattern, family in _TITLES:
        if pattern.match(text):
            return family
    return None


def _scan(text: str) -> set[str]:
    found: set[str] = set()
    for line in text.splitlines():
        family = _title_family(line)
        if family:
            found.add(family)
    for match in _LICENSE_LINE.finditer(text):
        for pattern, family in _LICENSE_LINE_FAMILY:
            if pattern.search(match.group(1)):
                found.add(family)
    if _NON_COMMERCIAL.search(text):
        found.add("non-commercial")
    return found


def read_raw(dist: Distribution) -> Raw:
    meta = dist.metadata
    classifiers = tuple(
        sorted(
            c.removeprefix("License :: ")
            for c in (meta.get_all("Classifier") or [])
            if c.startswith("License :: ")
        )
    )
    primary: set[tuple[str, str]] = set()
    notices: set[tuple[str, str]] = set()
    for item in dist.files or []:
        path = PurePosixPath(str(item))
        if not _is_licence_file(path):
            continue
        location = Path(str(dist.locate_file(item)))
        try:
            if not location.is_file() or location.stat().st_size > _MAX_FILE_BYTES:
                continue
            text = location.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for found in _scan(text):
            notices.add((str(path), found))
        if _is_primary(path):
            opening = [ln for ln in text.splitlines() if ln.strip()][:_PRIMARY_LINES]
            for line in opening:
                family = _title_family(line)
                if family:
                    primary.add((str(path), family))
    return Raw(
        expression=_clean(meta.get("License-Expression")),
        license_field=_clean(meta.get("License")),
        classifiers=classifiers,
        primary_titles=tuple(sorted(primary)),
        notices=tuple(sorted(notices)),
    )


# --- classification ----------------------------------------------------------------------------


def _unknown(note: str) -> Licence:
    return Licence("unknown", "", None, (), (), "none", note)


def _family_of(licence_id: str) -> str | None:
    if licence_id in _PERMISSIVE_ID:
        return _PERMISSIVE_ID[licence_id]
    for pattern, family in _COPYLEFT_ID:
        if pattern.match(licence_id):
            return family
    return None


def _from_spdx(text: str, basis: str) -> Licence | None:
    """The licence an SPDX expression names, ``None`` if ``text`` is no valid expression, and
    "unknown" when it is one but names an id the tables do not know."""
    try:
        expression = canonicalize_license_expression(text)
    except InvalidLicenseExpression:
        return None
    families: set[str] = set()
    after_with = False
    for word in re.findall(r"[()]|[^\s()]+", expression):
        if word in ("(", ")", "AND", "OR"):
            after_with = False
            continue
        if word == "WITH":
            after_with = True
            continue
        if after_with:
            after_with = False
            if word not in _EXCEPTION_ID:
                return _unknown(
                    f"SPDX exception {word} (in {expression!r}) is not in the reviewed table"
                )
            continue
        family = _family_of(word.removesuffix("+"))
        if family is None:
            return _unknown(f"SPDX id {word} (in {expression!r}) is not in the reviewed table")
        families.add(family)
    return Licence(
        "known",
        expression,
        expression,
        tuple(sorted(families)),
        tuple(sorted(families & COPYLEFT_FAMILIES)),
        basis,
        "",
    )


def _usable_field(field: str | None) -> str | None:
    if field is None or "\n" in field or len(field) > _FIELD_MAX:
        return None
    return None if field.casefold() == "unknown" else field


def _classifier_entries(
    classifiers: tuple[str, ...],
) -> tuple[list[tuple[str | None, str, str]], list[str]]:
    mapped: list[tuple[str | None, str, str]] = []
    unmapped: list[str] = []
    for full in classifiers:
        text = full.split(" :: ")[-1]
        if text == "OSI Approved":
            continue
        if text in CLASSIFIERS:
            mapped.append(CLASSIFIERS[text])
            continue
        family = next((f for prefix, f in _CLASSIFIER_PREFIX if text.startswith(prefix)), None)
        if family:
            mapped.append((None, family, text))
        else:
            unmapped.append(text)
    return mapped, unmapped


def _declared(name: str, version: str, raw: Raw) -> Licence:
    reviewed = REVIEWED.get((name, version))
    if reviewed and raw.license_field == reviewed[0]:
        got = _from_spdx(reviewed[1], f"reviewed: {reviewed[2]}")
        if got is not None:
            return replace(got, declared=raw.license_field or got.declared)
    if raw.expression:
        got = _from_spdx(raw.expression, "License-Expression")
        if got is None:
            return _unknown(f"License-Expression {raw.expression!r} is not a valid SPDX expression")
        return got
    field = _usable_field(raw.license_field)
    if field:
        got = _from_spdx(field, "License")
        if got is not None:
            return got
        folded = " ".join(field.casefold().split())
        if folded in FREE_TEXT:
            named = _from_spdx(FREE_TEXT[folded], "License")
            if named is not None:
                return replace(named, note=f'from the free text "{field}"')
        if folded in FREE_TEXT_FAMILY:
            family, shown = FREE_TEXT_FAMILY[folded]
            return Licence(
                "known", shown, None, (family,), (), "License", f'from the free text "{field}"'
            )
    mapped, unmapped = _classifier_entries(raw.classifiers)
    if unmapped:
        return _unknown("classifier not in the reviewed table: " + "; ".join(unmapped))
    if mapped:
        families = tuple(sorted({family for _, family, _ in mapped}))
        copyleft = tuple(f for f in families if f in COPYLEFT_FAMILIES)
        if len(mapped) == 1 and mapped[0][0]:
            only = mapped[0][0]
            return Licence("known", only, only, families, copyleft, "Classifier", "")
        shown = " / ".join(dict.fromkeys(text for _, _, text in mapped))
        note = "several classifiers; whether they combine as AND or OR is not stated"
        return Licence(
            "known", shown, None, families, copyleft, "Classifier", note if len(mapped) > 1 else ""
        )
    if field:
        return _unknown(
            f'License field "{field}" is not in the reviewed table, and no classifier names one'
        )
    return _unknown("no licence metadata (no License-Expression, License or License classifier)")


def classify(name: str, version: str, raw: Raw) -> Licence:
    base = _declared(name, version, raw)
    file_families = {family for _, family in raw.primary_titles}
    extra = sorted(f for f in file_families if f not in base.families)
    if base.basis in ("License-Expression", "License") or base.basis.startswith("reviewed"):
        mapped, _ = _classifier_entries(raw.classifiers)
        extra += sorted(
            {f for _, f, _ in mapped if f in COPYLEFT_FAMILIES and f not in base.families}
            - set(extra)
        )
    if not extra:
        return base
    names = ", ".join(extra)
    where = ", ".join(sorted({path for path, _ in raw.primary_titles})) or "the classifiers"
    declared = base.declared or "nothing usable"
    detail = (
        f"the licence declared in the metadata is {declared} ({base.basis}), "
        f"but {where} names {names}"
    )
    return replace(
        base,
        status="conflict" if base.status == "known" else base.status,
        copyleft=tuple(sorted(set(base.copyleft) | set(extra))),
        note="; ".join(part for part in (base.note, detail) if part),
    )
