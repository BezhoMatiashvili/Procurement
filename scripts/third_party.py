#!/usr/bin/env python3
"""Write docs/THIRD_PARTY.md: the licences of everything that ships inside the product.

    uv run python scripts/third_party.py --out docs/THIRD_PARTY.md     # (re)generate
    uv run python scripts/third_party.py --check docs/THIRD_PARTY.md   # exit 1 when it is stale
    uv run python scripts/third_party.py                               # to standard output

For every production dependency of uv.lock (the same set scripts/sbom.py lists: no dev group, no
extras of the project, markers evaluated for the image's platform) it states the name, the version
and the licence that the installed distribution declares (scripts/licences.py: License-Expression,
then License, then the Trove classifiers, through a small reviewed table). What it cannot classify
is listed under "Unknown - review by hand", never guessed. Licence files that name a copyleft
licence (a library bundled in a wheel, a vendored package) are found by a scan and listed with the
reviewed assessment in this file's tables; one without an assessment is listed as unreviewed, and
tests/unit/test_third_party.py fails until someone reads it and adds one.

The components outside Python (base images, Tesseract, the spelling dictionary, Caddy, Ollama) and
the models are described from the Dockerfile and docker-compose.yml, so the image references on the
page are the ones the build uses. What each licence asks of someone shipping the product is the
reviewer's reading, marked "verify" where it should be confirmed against the licence text.

The output has no date or time: two runs on one lock and one environment give identical bytes.
The installed environment must be the locked one (``uv sync --frozen``); the script stops otherwise.
Exit status: 0 success (or up to date), 1 stale or an error, 2 bad usage.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections import Counter
from dataclasses import dataclass
from importlib.metadata import packages_distributions
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import licences
import uvlock

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Row:
    package: uvlock.Package
    licence: licences.Licence
    raw: licences.Raw


@dataclass(frozen=True)
class Facts:
    """What the build files say, so that the page cannot name an image the build does not use."""

    python_image: str
    uv_image: str
    apt_packages: tuple[str, ...]
    compose_images: tuple[str, ...]
    dictionary_commit: str
    llm_models: tuple[str, ...]


@dataclass(frozen=True)
class Bundled:
    component: str
    licence: str
    use: str
    consequence: str
    copyleft: bool = True


# --- what each licence asks of someone shipping the product (the reviewer's reading) --------------

#: key -> what the licence asks; the tables cite the key and section 2.0 spells it out
ASKS: dict[str, str] = {
    "MPL-2.0": (
        "File-level (weak) copyleft. Shipping the package unmodified inside a proprietary "
        "product is allowed: keep its licence text and notices with it. A changed MPL-covered "
        "file must be made available in source form, under the MPL, to whoever receives the "
        "product; the product's own files are not affected. Verify."
    ),
    "LGPL library": (
        "Allowed in a proprietary product as a separate shared library that is dynamically "
        "linked. Ship the licence text and notices (they travel in the wheel's dist-info); do "
        "not prevent the recipient from replacing the library file with a modified build; make "
        "the library's source available (upstream's, unmodified: a link or written offer). "
        "Verify."
    ),
    "GCC runtime": (
        "GPL-3.0-or-later with the GCC Runtime Library Exception: the exception lets a program "
        "that is not GPL use these runtime libraries, and no source of the product is owed for "
        "using them. Ship the licence text and notices. Verify."
    ),
    "LGPL-3.0 source": (
        "LGPL-3.0 on plain Python source, vendored unmodified. Keep the licence text and "
        "notices; the recipient can replace the files because they are source. Verify."
    ),
}
MPL_ASKS = "MPL-2.0"
LGPL_DYNAMIC_ASKS = "LGPL library"
GCC_RUNTIME_ASKS = "GCC runtime"
LGPL3_VENDORED_ASKS = "LGPL-3.0 source"

#: Python packages whose declared licence is copyleft: (name, version) -> (what it is to the
#: product, what shipping it asks). The "required by" and "imported by" parts are read from the
#: lock and the source, not written here.
PYTHON_COPYLEFT: dict[tuple[str, str], tuple[str, str, str]] = {
    ("certifi", "2026.7.22"): (
        "library with one data file, the CA bundle cacert.pem, which TLS code in the libraries "
        "that require it loads when it builds a TLS context",
        MPL_ASKS,
        "",
    ),
    ("tqdm", "4.70.1"): (
        "in-process library (progress bars) used by the libraries that require it",
        MPL_ASKS,
        "",
    ),
    ("spylls", "0.1.7"): (
        "in-process library: the act's spelling check (AOA.SPELLING). checks/aoa_spelling.py "
        "subclasses its Lookup and its _IndexedLookup._second_suffix closely follows the logic of "
        "Lookup.desuffix",
        MPL_ASKS,
        "That subclass is derived from spylls' code: treat that part of aoa_spelling.py as "
        "MPL-covered, keep the MPL notice with it (the file ships as source in the image), and "
        "have it read by whoever advises on licences.",
    ),
}

#: Reviewed assessments of what the licence files inside a wheel add to the metadata. A
#: (name, version) that has a licence file naming a copyleft licence and no entry here is
#: "unreviewed". Keyed by version because the libraries a wheel bundles change with it.
_NUMPY_LIBS = (
    Bundled(
        "libgfortran ({pkg}.libs/libgfortran*.so)",
        "GPL-3.0-or-later WITH GCC-exception-3.1",
        "shared library loaded by the package's compiled extension modules (dynamic linking)",
        GCC_RUNTIME_ASKS,
    ),
    Bundled(
        "libquadmath ({pkg}.libs/libquadmath*.so)",
        "LGPL-2.1-or-later",
        "shared library loaded by the package's compiled extension modules (dynamic linking)",
        LGPL_DYNAMIC_ASKS,
    ),
)


def _numpy_libs(pkg: str) -> tuple[Bundled, ...]:
    return tuple(
        Bundled(b.component.format(pkg=pkg), b.licence, b.use, b.consequence, b.copyleft)
        for b in _NUMPY_LIBS
    )


BUNDLED: dict[tuple[str, str], tuple[Bundled, ...]] = {
    ("numpy", "2.5.3"): _numpy_libs("numpy"),
    ("scipy", "1.18.1"): _numpy_libs("scipy"),
    ("opencv-python-headless", "5.0.0.93"): (
        Bundled(
            "FFmpeg libraries (libavcodec, libavformat, libavutil, libswscale, libswresample "
            "in opencv_python_headless.libs)",
            "LGPL-2.1 (the wheel's LICENSE-3RD-PARTY.txt: \"This license applies to the above "
            'library binaries in the directory cv2/")',
            "shared libraries loaded by cv2 for video input and output, which this application "
            "never uses; they are in the image regardless",
            LGPL_DYNAMIC_ASKS,
        ),
        *_numpy_libs("opencv_python_headless")[:2],
        Bundled(
            "OpenSSL 1.1.1k (libssl, libcrypto in opencv_python_headless.libs)",
            "OpenSSL License and SSLeay License (both reproduced in LICENSE-3RD-PARTY.txt)",
            "shared libraries FFmpeg uses for network protocols; the application opens none",
            "Not copyleft: BSD-style with an advertising clause (advertising that mentions the "
            "software's features must credit the OpenSSL Project); keep the notices. OpenSSL "
            "1.1.1 reached end of life in September 2023 (verify) and a vulnerability scanner "
            "does not see a library bundled in a wheel.",
            copyleft=False,
        ),
        Bundled(
            "a Mozilla Public License 2.0 text in LICENSE-3RD-PARTY.txt",
            "MPL-2.0",
            "the file does not say here which bundled library it belongs to: verify which one",
            MPL_ASKS,
        ),
    ),
    ("shapely", "2.1.2"): (
        Bundled(
            "GEOS (libgeos, libgeos_c in shapely.libs)",
            "LGPL-2.1 (shapely's LICENSE_GEOS)",
            "shared libraries loaded by shapely's compiled extension whenever shapely is imported",
            LGPL_DYNAMIC_ASKS,
        ),
    ),
    ("setuptools", "84.0.0"): (
        Bundled(
            "autocommand 2.2.2 (setuptools/_vendor)",
            "LGPL-3.0 (its LICENSE file)",
            "vendored Python source, unmodified",
            LGPL3_VENDORED_ASKS,
        ),
        Bundled(
            "code generated by validate-pyproject (setuptools/config/_validate_pyproject: "
            "extra_validations.py, format.py, error_reporting.py)",
            "MPL-2.0 (that directory's NOTICE)",
            "vendored Python source inside setuptools",
            MPL_ASKS,
        ),
    ),
    ("torch", "2.14.1+cpu"): (
        Bundled(
            "libgomp (torch/lib/libgomp.so.1)",
            "GPL-3.0-or-later WITH GCC-exception-3.1 (libgomp's upstream licence; torch's own "
            "licence files carry no notice for it: verify)",
            "OpenMP runtime shipped in torch/lib, used by the CPU kernels",
            GCC_RUNTIME_ASKS,
        ),
    ),
    ("pillow", "12.3.0"): (
        Bundled(
            "FreeType, zstd, xz and the other libraries in pillow.libs",
            "dual-licensed with a permissive option (FreeType: FTL or GPL-2.0-or-later; zstd: "
            "BSD-3-Clause or GPL-2.0); xz's notice says its GPL parts are not in the binaries",
            "shared libraries loaded by Pillow's compiled extension",
            "No copyleft applies while the permissive option is the one relied on. The FreeType "
            "licence asks that the product's documentation credit the FreeType Project "
            "(verify the wording in pillow's LICENSE).",
            copyleft=False,
        ),
    ),
    ("lxml", "6.1.3"): (
        Bundled(
            "libxml2, libxslt, zlib (linked into the extension); LICENSES.txt also names "
            "libiconv (LGPL-2.1) and a GPL test runner",
            "MIT / zlib; LGPL-2.1 for libiconv in builds that bundle it",
            "this Linux wheel ships no lxml.libs directory, so no libiconv file is installed; "
            "whether it is linked in statically: verify",
            "Nothing copyleft is shipped as a separate file. Verify the libiconv point.",
            copyleft=False,
        ),
    ),
    ("pypdfium2", "5.13.0"): (
        Bundled(
            "libraries built into the PDFium binary (abseil, agg, fast_float, FreeType, ICU, "
            "lcms, libjpeg-turbo, libopenjpeg, libpng, libtiff, llvm-libc, simdutf, zlib)",
            "their own licences, one text each in BUILD_LICENSES/ (the metadata names only "
            "BSD-3-Clause and Apache-2.0); CC-BY-4.0 text ships too",
            "compiled into the PDFium shared library the wheel carries",
            "No copyleft licence among them; GPL appears in the ICU and llvm-libc notices as "
            "a reference (verify). CC-BY-4.0 asks for attribution.",
            copyleft=False,
        ),
    ),
}

#: What a person decided about a package the tables cannot classify on their own.
ASSESSMENTS: dict[tuple[str, str], str] = {
    ("spylls", "0.1.7"): (
        "treat as MPL-2.0. The metadata says `License: UNKNOWN` and carries the MIT classifier; "
        "the LICENSE file the wheel ships is the full text of the Mozilla Public License 2.0, "
        "and spylls is a port of Hunspell, itself under MPL. The stricter reading is the one "
        "that matches the text actually delivered. Ask the author or confirm upstream before "
        "relying on the MIT classifier."
    ),
}

# --- the models -----------------------------------------------------------------------------------

DOCLING_MODELS = (
    ("docling-project--docling-layout-heron", "apache-2.0", "layout analysis (RT-DETR 'heron')"),
    (
        "docling-project--docling-layout-heron-onnx",
        "apache-2.0",
        "ONNX export of the same layout model",
    ),
    (
        "docling-project--docling-models",
        "cdla-permissive-2.0",
        "layout and TableFormer table-structure models",
    ),
)


# --- reading the repository -----------------------------------------------------------------------


def facts_from_repo(root: Path) -> Facts:
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (root / ".env.example").read_text(encoding="utf-8")

    def arg(name: str) -> str:
        found = re.search(rf"^ARG {name}=(\S+)", dockerfile, re.M)
        if not found:
            raise SystemExit(f"Dockerfile has no 'ARG {name}='")
        return found.group(1)

    apt = re.search(r"apt-get install([^&]*)&&", dockerfile)
    if not apt:
        raise SystemExit("Dockerfile has no apt-get install line")
    packages = tuple(w for w in apt.group(1).replace("\\", " ").split() if not w.startswith("-"))
    commit = re.search(r"ka_GE\.spell/([0-9a-f]{40})/", dockerfile)
    if not commit:
        raise SystemExit("Dockerfile does not pin the ka_GE dictionary to a commit")
    models = tuple(dict.fromkeys(re.findall(r"^PA_LLM(?:_FAST)?_MODEL=(\S+)", env_example, re.M)))
    return Facts(
        python_image=arg("PYTHON_IMAGE"),
        uv_image=arg("UV_IMAGE"),
        apt_packages=packages,
        compose_images=tuple(re.findall(r"^\s+image:\s*(\S+)", compose, re.M)),
        dictionary_commit=commit.group(1),
        llm_models=models,
    )


def src_imports(src_root: Path) -> dict[str, list[str]]:
    """Top-level module -> the files (relative to ``src_root``) that import it, absolute imports."""
    found: dict[str, set[str]] = {}
    for path in sorted(src_root.rglob("*.py")):
        rel = path.relative_to(src_root).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=rel)):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                found.setdefault(name.split(".")[0], set()).add(rel)
    return {module: sorted(files) for module, files in sorted(found.items())}


def imports_by_distribution(src_root: Path) -> dict[str, list[str]]:
    owners = packages_distributions()
    by_dist: dict[str, set[str]] = {}
    for module, files in src_imports(src_root).items():
        for dist in owners.get(module, []):
            by_dist.setdefault(_normal(dist), set()).update(files)
    return {dist: sorted(files) for dist, files in by_dist.items()}


def _normal(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def collect_rows(closure: uvlock.Closure) -> list[Row]:
    rows = []
    for pkg in closure.packages:
        raw = licences.read_raw(licences.installed(pkg.name, pkg.version))
        rows.append(Row(pkg, licences.classify(pkg.name, pkg.version, raw), raw))
    return rows


def _stray_notices(row: Row) -> list[tuple[str, str]]:
    """Copyleft notices in a package's licence files that neither its declared licence nor a
    reviewed entry for this version accounts for."""
    if (row.package.name, row.package.version) in BUNDLED:
        return []
    return [(f, fam) for f, fam in row.raw.notices if fam not in row.licence.copyleft]


def unreviewed(rows: list[Row]) -> list[str]:
    """Why a person still has to read something: an empty list means everything has been read."""
    problems = []
    for row in rows:
        key = (row.package.name, row.package.version)
        label = f"{row.package.name} {row.package.version}"
        if row.licence.status == "unknown" and key not in ASSESSMENTS:
            problems.append(f"{label}: licence unknown ({row.licence.note})")
        if row.licence.status == "conflict" and key not in ASSESSMENTS:
            problems.append(f"{label}: licence metadata and licence file disagree")
        if row.licence.copyleft and key not in PYTHON_COPYLEFT and key not in BUNDLED:
            problems.append(
                f"{label}: copyleft licence {', '.join(row.licence.copyleft)} not reviewed"
            )
        stray = _stray_notices(row)
        if stray:
            families = ", ".join(sorted({fam for _, fam in stray}))
            problems.append(f"{label}: licence files name {families}; no reviewed assessment")
    return problems


# --- the page -----------------------------------------------------------------------------------


def _cell(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


def _asks(key: str, extra: str) -> str:
    """The cell for "what shipping it asks": a legend key, or free text, plus anything extra."""
    head = f"{key} (section 2.0)" if key in ASKS else key
    return f"{head}. {extra}" if extra else head


def _names(keys: tuple[str, ...], limit: int = 6) -> str:
    shown = [k.split("@")[0] for k in keys]
    more = f" and {len(shown) - limit} more" if len(shown) > limit else ""
    return ", ".join(shown[:limit]) + more


def _usage(row: Row, imports: dict[str, list[str]]) -> str:
    files = imports.get(row.package.name, [])
    imported = (
        "imported by the application: " + ", ".join(f"`{f}`" for f in files)
        if files
        else "not imported by the application's own code"
    )
    return f"required by: {_names(row.package.required_by)}; {imported}"


def _family_table(rows: list[Row]) -> list[str]:
    counts: Counter[str] = Counter()
    for row in rows:
        if row.licence.status == "unknown":
            counts["unknown"] += 1
        for family in row.licence.families:
            counts[family] += 1
        if row.licence.status == "conflict":
            counts["conflict (see section 3)"] += 1
    lines = ["| Licence family | Packages |", "|---|---|"]
    for family in sorted(counts, key=str.lower):
        flag = " (copyleft or restrictive)" if family in licences.COPYLEFT_FAMILIES else ""
        lines.append(f"| {family}{flag} | {counts[family]} |")
    return lines


def render(
    root: uvlock.Package,
    rows: list[Row],
    *,
    lock_sha256: str,
    imports: dict[str, list[str]],
    facts: Facts,
) -> str:
    rows = sorted(rows, key=lambda r: (r.package.name.lower(), r.package.version))
    out: list[str] = []
    add = out.append

    add("# Third-party components and licences")
    add("")
    add(
        "What ships inside the product, and under which licence. Generated by "
        "`scripts/third_party.py` from `uv.lock` (SHA-256 `" + lock_sha256 + "`) and the metadata "
        "of the installed distributions; do not edit it by hand. Regenerate it with "
        "`uv run python scripts/third_party.py --out docs/THIRD_PARTY.md`; "
        "`tests/unit/test_third_party.py` fails when it no longer matches the lock or when a "
        "licence nobody has read appears. The machine-readable list of the same packages, with "
        "hashes, is `docs/sbom.cdx.json`."
    )
    add("")
    add(
        'This is an inventory for review, not legal advice. A statement marked "verify" is the '
        "reviewer's reading and should be confirmed against the licence text before it is "
        "relied on."
    )
    add("")
    add("## 1. Summary")
    add("")
    add(
        f"{len(rows)} packages are in the production closure of `{root.name}` {root.version} "
        "(`uv sync --frozen --no-dev` on linux/x86_64: no dev group, no extras), counted below by "
        "licence family; a package under several licences counts in each. "
        "The project itself is proprietary."
    )
    add("")
    out.extend(_family_table(rows))
    add("")
    copyleft_rows = [r for r in rows if r.licence.copyleft]
    add(
        f"{len(copyleft_rows)} packages declare a copyleft licence (section 2.1); wheels also "
        "bundle copyleft libraries and vendored code that the metadata does not mention "
        "(section 2.2); the models and base images have terms of their own (sections 5 and 6)."
    )
    add("")

    # -- 2
    add("## 2. Copyleft and restrictive licences")
    add("")
    add("### 2.0 What these licences ask of someone shipping the product")
    add("")
    add("The tables below cite these by name. This is the reviewer's reading, not advice.")
    add("")
    for name, asks in ASKS.items():
        add(f"* **{name}**: {asks}")
    add("")
    add("### 2.1 Declared by the package")
    add("")
    add("| Package | Licence | How it is used | Shipping it in a proprietary product |")
    add("|---|---|---|---|")
    for row in copyleft_rows:
        key = (row.package.name, row.package.version)
        use, asks_key, extra = PYTHON_COPYLEFT.get(key, ("not reviewed", "", "not reviewed"))
        lic = row.licence
        if lic.status == "conflict":
            shown = (
                f"{lic.declared} per the metadata, {', '.join(lic.copyleft)} per the licence "
                "file the package ships (section 3)"
            )
        else:
            shown = lic.declared
        add(
            f"| {row.package.name} | {_cell(shown)} | {_cell(use)}; "
            f"{_cell(_usage(row, imports))} | {_cell(_asks(asks_key, extra))} |"
        )
    add("")
    add("### 2.2 Copyleft libraries and code bundled inside a package")
    add("")
    add(
        "Found in the licence files the wheels ship, not in their metadata. The libraries are "
        "separate shared-library files (or vendored source) inside the package directories of the "
        "image's virtual environment."
    )
    add("")
    add("| Package | Component | Licence | How it is used | Shipping it in a proprietary product |")
    add("|---|---|---|---|---|")
    for row in rows:
        for item in BUNDLED.get((row.package.name, row.package.version), ()):
            if item.copyleft:
                add(
                    f"| {row.package.name} {row.package.version} | {_cell(item.component)} | "
                    f"{_cell(item.licence)} | {_cell(item.use)} | "
                    f"{_cell(_asks(item.consequence, ''))} |"
                )
    add("")
    add("### 2.3 Reviewed: bundled components that are not copyleft")
    add("")
    add("| Package | Bundled components | Licence | Note | Consequence |")
    add("|---|---|---|---|---|")
    for row in rows:
        for item in BUNDLED.get((row.package.name, row.package.version), ()):
            if not item.copyleft:
                add(
                    f"| {row.package.name} {row.package.version} | {_cell(item.component)} | "
                    f"{_cell(item.licence)} | {_cell(item.use)} | "
                    f"{_cell(_asks(item.consequence, ''))} |"
                )
    add("")

    # -- 3
    add("## 3. Unknown - review by hand")
    add("")
    add(
        "Packages whose licence the tables could not name, whose metadata and licence file "
        "disagree, or whose licence files name a copyleft licence nobody has assessed. Nothing "
        "here is guessed; the line says what was found."
    )
    add("")
    listed = False
    for row in rows:
        key = (row.package.name, row.package.version)
        stray = _stray_notices(row)
        if row.licence.status == "known" and not stray:
            continue
        listed = True
        state = {"unknown": "UNKNOWN", "conflict": "CONFLICT", "known": "UNREVIEWED NOTICE"}[
            row.licence.status
        ]
        found = _cell(
            row.licence.note or "licence files name " + ", ".join(sorted({f for _, f in stray}))
        ).rstrip(".")
        add(
            f"* **{row.package.name} {row.package.version}** - {state}: {found}."
            + (f" Assessment: {ASSESSMENTS[key]}" if key in ASSESSMENTS else "")
        )
    if not listed:
        add("None.")
    add("")

    # -- 4
    add("## 4. Python packages in the production closure")
    add("")
    add("| Package | Version | Licence | Read from |")
    add("|---|---|---|---|")
    for row in rows:
        lic = row.licence
        if lic.status == "unknown":
            shown = "UNKNOWN (section 3)"
        elif lic.status == "conflict":
            shown = f"{lic.declared} (CONFLICT, section 3)"
        else:
            shown = lic.declared
        basis = lic.basis + (f"; {lic.note}" if lic.note and lic.status == "known" else "")
        add(f"| {row.package.name} | {row.package.version} | {_cell(shown)} | {_cell(basis)} |")
    add("")

    # -- 5
    add("## 5. Components outside Python that ship with the product")
    add("")
    add("Image references are copied from `Dockerfile` and `docker-compose.yml`.")
    add("")
    add("| Component | Licence | How it is used | Obligation |")
    add("|---|---|---|---|")
    add(
        f"| Base image `{facts.python_image}` (Debian 12 and CPython 3.12) | CPython: PSF-2.0. "
        "Debian: each package under its own licence, including GPL and LGPL ones (bash, "
        "coreutils, glibc and others); the texts are in /usr/share/doc/<package>/copyright "
        "inside the image | the operating system and interpreter of the app container | "
        "Whoever hands the image to a client passes on GPL and LGPL software and owes its "
        "licence texts (present in the image) and, on request, the source (Debian publishes "
        "it). The product's own code does not become GPL by running on it. Verify. |"
    )
    add(
        f"| Debian packages installed by the Dockerfile: {', '.join(facts.apt_packages)} "
        "| Tesseract OCR engine: Apache-2.0. Language data `kat` and `eng`: Apache-2.0 "
        "(upstream tessdata; the copyright files of the Debian packages are authoritative: "
        "verify). Leptonica (a dependency apt adds): a BSD-style licence (verify). "
        "libglib2.0-0: LGPL-2.1-or-later | OCR runs as a separate `tesseract` process that "
        "Docling starts (`TesseractCliOcrOptions`, ingestion/docling_service.py); glib is "
        "a shared library | Apache-2.0: ship the licence text and keep notices. LGPL: as in "
        "section 2.2. Verify the exact package list with `dpkg -l` in the built image. |"
    )
    add(
        f"| Spelling dictionary: hunspell `ka_GE`, gamag/ka_GE.spell at commit "
        f"`{facts.dictionary_commit}` | MIT (the text is in the `.aff` header). Generated from "
        "word lists under MIT (GeoWordsDatabase) and CC-BY-4.0 (Crubadan, Kevin Scannell) "
        "(docs/ACCURACY.md). The upstream repository's GPL-3.0 covers only its generator "
        "scripts, which are not used | two data files baked into the image by the Dockerfile "
        "(`ADD --checksum`), read through spylls | MIT: keep the notice. CC-BY-4.0: credit the "
        "word-list authors, give the licence link, say what was changed (docs/ACCURACY.md "
        "carries the credit). Verify. |"
    )
    add(
        f"| Build stage only: `{facts.uv_image}` | MIT or Apache-2.0 (verify) | installs the "
        "locked packages; its binary is not copied into the runtime image | none for the "
        "runtime image |"
    )
    compose = {_ref_name(r): r for r in facts.compose_images}
    ollama = next((r for n, r in compose.items() if n.startswith("ollama/")), "ollama/ollama")
    caddy = next((r for n, r in compose.items() if n.startswith("caddy")), "caddy")
    add(
        f"| `{ollama}` | Ollama: MIT (verify against the image). The image holds a Linux "
        "userland (GPL and LGPL packages) and may hold GPU runtime libraries under their "
        "vendors' terms (verify; unused on a CPU-only server) | separate container, reached "
        "only from the app over the `llm` network | As for the base image; check the GPU "
        "libraries' terms with `docker run --rm --entrypoint ls <image> /usr/lib/ollama`. |"
    )
    add(
        f"| `{caddy}` | Caddy: Apache-2.0. Alpine userland: musl MIT; busybox and apk-tools "
        "GPL-2.0-only (verify) | stateless relay container, the only one with a published port | "
        "Apache-2.0: ship the licence text and notices. GPL: as for the base image. |"
    )
    own = next((r for r in facts.compose_images if r.startswith("procurement-agent")), None)
    if own:
        add(f"| `{own}` | the image built from this repository | the application | see section 4 |")
    add("")

    # -- 6
    add("## 6. Models")
    add("")
    add(
        "The weights are not part of the repository: the LLM comes from the Ollama library "
        "(`scripts/prefetch_models.sh` pulls it on a connected machine) and the Docling models "
        "from Hugging Face into `models/docling`. Licence names for the Docling models are "
        "quoted from the front matter of the `README.md` in each model directory, read "
        "2026-10-03; no separate LICENSE file is stored beside the weights."
    )
    add("")
    add("| Model | Licence | What it asks of someone passing it on |")
    add("|---|---|---|")
    names = " and ".join(f"`{m}`" for m in facts.llm_models)
    lookups = " and ".join(f"`ollama show --license {m}`" for m in facts.llm_models)
    add(
        f"| {names} through Ollama (Gemma 3, Google) | Gemma Terms of Use "
        "(ai.google.dev/gemma/terms) and Gemma Prohibited Use Policy "
        "(ai.google.dev/gemma/prohibited_use_policy). Not an OSI open-source licence: it "
        "restricts use | Delivering the weights to a client counts as distribution. As this "
        "reviewer reads the Terms, the recipient must be given a copy of the Terms, be bound "
        "by the use restrictions of the Prohibited Use Policy (they must be passed on in the "
        "client's agreement), and the notices the Terms require must be kept. Confirm against "
        f"the text served with each model ({lookups}) and with counsel: verify. Outputs are "
        "not claimed by Google under the Terms (verify). |"
    )
    for directory, name, what in DOCLING_MODELS:
        if name == "apache-2.0":
            asks = (
                "Apache-2.0: ship a copy of the licence, keep copyright and attribution "
                "notices, state changes if the files are modified. Verify."
            )
        else:
            asks = (
                "CDLA-Permissive-2.0 (Community Data License Agreement - Permissive, version "
                "2.0): permissive; as read here it asks that the licence (or its URL) and "
                "the credit to the data provider travel with redistributed copies, and puts "
                "no condition on results computed with it. Verify against the text."
            )
        add(
            f"| `models/docling/{directory}` ({what}) | `{name}` (model card front matter) "
            f"| {asks} |"
        )
    add("")
    add(
        "`models/docling/MANIFEST.sha256` lists the checksum of every model file; production "
        "refuses to start on a changed, missing or extra file (docs/SECURITY.md section 2)."
    )
    add("")

    # -- 7
    add("## 7. How this file is made, and what it does not cover")
    add("")
    add(
        "* Licence precedence per package: `License-Expression`, then a short `License` field, "
        "then the `License ::` classifiers (`scripts/licences.py`). A name that leaves the "
        'variant open ("BSD", "Apache") is shown as such with no SPDX id.'
    )
    add(
        "* Every licence-looking file a package ships is scanned for titles and `License:` lines "
        "of copyleft licences. Code compiled into a wheel without a licence file (Rust and C "
        "dependencies of native extensions) is not enumerated; for those the package's own "
        "licence files are the only source, and `docs/sbom.cdx.json` lists the wheels' hashes."
    )
    add(
        "* Packages are those of the lock for linux/x86_64 and Python 3.12.14; a different "
        "platform or architecture would need its own run."
    )
    add(
        "* The Debian and Alpine packages inside the images are not enumerated here; "
        "`docker run --rm --entrypoint dpkg <image> -l` (or `apk info -vv`) on the built image "
        "does."
    )
    add("")
    return "\n".join(out)


def _ref_name(ref: str) -> str:
    return ref.split("@")[0].split(":")[0]


# --- command line ---------------------------------------------------------------------------------


def generate(root: Path) -> str:
    lock_path = root / "uv.lock"
    closure = uvlock.production_closure(uvlock.read_lock(lock_path))
    return render(
        closure.root,
        collect_rows(closure),
        lock_sha256=uvlock.file_sha256(lock_path),
        imports=imports_by_distribution(root / "src"),
        facts=facts_from_repo(root),
    )


def check(root: Path, path: Path) -> list[str]:
    if not path.is_file():
        return [f"{path} does not exist"]
    committed = path.read_text(encoding="utf-8")
    fresh = generate(root)
    if committed == fresh:
        return []
    old = {m.group(1): m.group(2) for m in re.finditer(r"^\| (\S+) \| (\S+) \|", committed, re.M)}
    new = {m.group(1): m.group(2) for m in re.finditer(r"^\| (\S+) \| (\S+) \|", fresh, re.M)}
    problems = [f"{path} is stale: it differs from what the lock and the environment produce now"]
    problems += [f"  added:   {n} {new[n]}" for n in sorted(new.keys() - old.keys())]
    problems += [f"  removed: {n} {old[n]}" for n in sorted(old.keys() - new.keys())]
    problems += [
        f"  changed: {n} {old[n]} -> {new[n]}"
        for n in sorted(old.keys() & new.keys())
        if old[n] != new[n]
    ]
    problems.append(f"regenerate it: uv run python scripts/third_party.py --out {path}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Third-party licence inventory (docs/THIRD_PARTY.md)"
    )
    parser.add_argument(
        "--root", type=Path, default=ROOT, help="repository root (default: this one)"
    )
    parser.add_argument("--out", type=Path, help="write here instead of standard output")
    parser.add_argument("--check", type=Path, metavar="FILE", help="exit 1 when FILE is stale")
    args = parser.parse_args(argv)
    try:
        if args.check:
            problems = check(args.root, args.check)
            if problems:
                print("\n".join(problems), file=sys.stderr)
                return 1
            print(f"{args.check} is up to date")
            return 0
        text = generate(args.root)
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
