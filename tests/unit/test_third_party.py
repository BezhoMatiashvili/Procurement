"""The third-party licence inventory (scripts/third_party.py -> docs/THIRD_PARTY.md): that it lists
exactly the production packages uv.lock pins, drops none, flags what needs a person, and is the same
file every time it is made."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))  # the scripts import one another by name

import licences  # noqa: E402
import third_party  # noqa: E402
import uvlock  # noqa: E402

COMMITTED = ROOT / "docs" / "THIRD_PARTY.md"


# --- synthetic inputs -----------------------------------------------------------------------------


def _licence(
    declared: str = "MIT", *, status: str = "known", copyleft: tuple[str, ...] = ()
) -> Any:
    return licences.Licence(
        status=status,
        declared=declared if status != "unknown" else "",
        spdx=declared if status == "known" and not copyleft else None,
        families=("MIT",) if status == "known" else (),
        copyleft=copyleft,
        basis="License-Expression" if status == "known" else "none",
        note="no licence metadata" if status == "unknown" else "",
    )


def _raw(notices: tuple[tuple[str, str], ...] = ()) -> licences.Raw:
    return licences.Raw(None, None, (), (), notices)


def _package(
    name: str, version: str = "1.0", required_by: tuple[str, ...] = ("proj@0.1.0",)
) -> Any:
    return uvlock.Package(name, version, "registry:x", (), (), required_by)


def _row(name: str, licence: Any = None, raw: Any = None, version: str = "1.0") -> Any:
    return third_party.Row(_package(name, version), licence or _licence(), raw or _raw())


FACTS = third_party.Facts(
    python_image="python:3.12.14-slim-bookworm@sha256:" + "a" * 64,
    uv_image="ghcr.io/astral-sh/uv:0.12.8@sha256:" + "b" * 64,
    apt_packages=("tesseract-ocr", "tesseract-ocr-kat", "tesseract-ocr-eng", "libgl1"),
    compose_images=(
        "procurement-agent:0.1.0",
        "ollama/ollama:0.34.2@sha256:" + "c" * 64,
        "caddy:2.11.4-alpine@sha256:" + "d" * 64,
    ),
    dictionary_commit="26d33614360c3784c199e3a6766eb5b095a134b8",
    llm_models=("gemma3:12b", "gemma3:4b"),
)
ROOT_PACKAGE = uvlock.Package("proj", "0.1.0", "editable:.", (), ("zeta@1.0",), ())


def _render(rows: list[Any], imports: dict[str, list[str]] | None = None) -> str:
    return third_party.render(
        ROOT_PACKAGE, rows, lock_sha256="e" * 64, imports=imports or {}, facts=FACTS
    )


def _section(text: str, heading: str) -> str:
    match = re.search(rf"^## {re.escape(heading)}.*?(?=^## |\Z)", text, re.M | re.S)
    assert match, f"no section {heading!r}"
    return match.group(0)


def _table_names(text: str) -> list[tuple[str, str]]:
    body = _section(text, "4. Python packages")
    return re.findall(r"^\| ([^|\s]+) \| ([^|\s]+) \|", body, re.M)[1:]  # [1:] drops the header


# --- the rendering --------------------------------------------------------------------------------


def test_every_package_is_listed_once_sorted_with_its_version_and_licence() -> None:
    text = _render([_row("zeta", _licence("Apache-2.0")), _row("alpha"), _row("Mid")])
    assert _table_names(text) == [("alpha", "1.0"), ("Mid", "1.0"), ("zeta", "1.0")]
    assert "| zeta | 1.0 | Apache-2.0 |" in text


def test_the_summary_counts_packages_by_licence_family() -> None:
    text = _render([_row("a"), _row("b"), _row("c", _licence("BSD-3-Clause"))])
    summary = _section(text, "1. Summary")
    assert "3 packages" in summary
    assert re.search(r"\| MIT \| 3 \|", summary) or "MIT" in summary


def test_an_unclassifiable_package_is_in_the_review_by_hand_section_with_its_reason() -> None:
    text = _render([_row("mystery", _licence(status="unknown"))])
    section = _section(text, "3. Unknown")
    assert "mystery" in section
    assert "no licence metadata" in section
    assert "review by hand" in section.lower()
    assert "mystery" in _section(text, "4. Python packages")  # and it is not dropped from the list


def test_with_nothing_unknown_the_section_says_so() -> None:
    text = _render([_row("a")])
    assert "None." in _section(text, "3. Unknown")


def test_a_copyleft_package_is_flagged_with_its_use_and_the_consequence() -> None:
    mpl = _licence("MPL-2.0", copyleft=("MPL",))
    rows = [_row("spylls", mpl, version="0.1.7"), _row("innocuous")]
    text = _render(rows, imports={"spylls": ["procurement_agent/checks/aoa_spelling.py"]})
    section = _section(text, "2. Copyleft")
    assert "spylls" in section and "innocuous" not in section
    assert "procurement_agent/checks/aoa_spelling.py" in section
    assert "MPL" in section


def test_a_copyleft_package_nobody_has_reviewed_is_reported_as_unreviewed() -> None:
    mpl = _licence("MPL-2.0", copyleft=("MPL",))
    problems = third_party.unreviewed([_row("newlib", mpl, version="9.9")])
    assert len(problems) == 1 and "newlib 9.9" in problems[0]


def test_a_copyleft_notice_in_a_licence_file_nobody_has_reviewed_is_reported() -> None:
    raw = _raw((("x-1.dist-info/LICENSE", "LGPL"),))
    problems = third_party.unreviewed([_row("bundler", raw=raw, version="3.0")])
    assert len(problems) == 1 and "bundler 3.0" in problems[0] and "LGPL" in problems[0]
    text = _render([_row("bundler", raw=raw, version="3.0")])
    assert "bundler" in _section(text, "3. Unknown")  # it is on the page for a person to read


def test_reviewed_bundled_libraries_are_listed_under_their_wheel() -> None:
    raw = _raw((("numpy-2.5.3.dist-info/licenses/LICENSE.txt", "LGPL"),))
    rows = [_row("numpy", raw=raw, version="2.5.3")]
    assert third_party.unreviewed(rows) == []
    section = _section(_render(rows), "2. Copyleft")
    assert "libquadmath" in section and "LGPL-2.1-or-later" in section
    assert "libgfortran" in section and "GCC-exception-3.1" in section


def test_a_review_of_another_version_does_not_carry_over() -> None:
    raw = _raw((("numpy-9.dist-info/licenses/LICENSE.txt", "LGPL"),))
    assert third_party.unreviewed([_row("numpy", raw=raw, version="9.0.0")])


def test_a_conflict_is_listed_for_review_with_the_assessment_made() -> None:
    conflict = licences.Licence(
        "conflict", "MIT", None, ("MIT",), ("MPL",), "Classifier", "the file names MPL"
    )
    text = _render([_row("spylls", conflict, version="0.1.7")])
    section = _section(text, "3. Unknown")
    assert "spylls" in section and "the file names MPL" in section
    assert "MPL-2.0" in section  # the reviewed reading
    assert third_party.unreviewed([_row("spylls", conflict, version="0.1.7")]) == []


def test_the_images_and_packages_of_the_build_come_from_the_files_not_from_this_script() -> None:
    text = _render([_row("a")])
    body = _section(text, "5. Components outside Python")
    for ref in (FACTS.python_image, FACTS.uv_image, *FACTS.compose_images):
        assert ref in body
    for pkg in FACTS.apt_packages:
        assert pkg in body
    assert FACTS.dictionary_commit in body


def test_the_models_are_named_with_their_licence_and_what_it_asks() -> None:
    text = _render([_row("a")])
    body = _section(text, "6. Models")
    for model in FACTS.llm_models:
        assert model in body
    assert "Gemma Terms of Use" in body and "Prohibited Use" in body
    assert "apache-2.0" in body and "cdla-permissive-2.0" in body
    assert "verify" in body.lower()


def test_the_output_is_the_same_every_time_and_has_no_date_or_time() -> None:
    rows = [_row("b"), _row("a", _licence(status="unknown"))]
    first, second = _render(rows), _render(list(reversed(rows)))
    assert first == second
    assert first.endswith("\n") and "\r" not in first
    assert not re.search(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", first)


def test_the_lock_hash_is_stated_for_the_bundle_script() -> None:
    assert "`" + "e" * 64 + "`" in _render([_row("a")])


# --- the real files -------------------------------------------------------------------------------


def _real_rows() -> tuple[uvlock.Closure, list[Any]]:
    closure = uvlock.production_closure(uvlock.read_lock(ROOT / "uv.lock"))
    return closure, third_party.collect_rows(closure)


def test_the_committed_inventory_lists_exactly_the_production_closure_of_uv_lock() -> None:
    closure = uvlock.production_closure(uvlock.read_lock(ROOT / "uv.lock"))
    text = COMMITTED.read_text(encoding="utf-8")
    listed = _table_names(text)
    assert len(listed) == len(set(listed)), "a package is listed twice"
    assert sorted(listed, key=lambda t: t[0].lower()) == [
        (p.name, p.version) for p in closure.packages
    ]


def test_no_development_package_is_in_the_committed_inventory() -> None:
    lock = uvlock.read_lock(ROOT / "uv.lock")
    root = next(p for p in lock["package"] if p["source"].get("editable"))
    dev = {d["name"] for group in root["dev-dependencies"].values() for d in group}
    assert dev
    in_closure = {p.name for p in uvlock.production_closure(lock).packages}
    listed = {name for name, _ in _table_names(COMMITTED.read_text(encoding="utf-8"))}
    # a few dev packages (packaging, pyyaml...) are production dependencies too: only the rest count
    assert not (dev - in_closure) & listed


def test_the_committed_inventory_is_current() -> None:
    """Fails when uv.lock, an installed licence or a reviewed table changed and the file was not
    regenerated: uv run python scripts/third_party.py --out docs/THIRD_PARTY.md"""
    problems = third_party.check(ROOT, COMMITTED)
    assert problems == [], "\n".join(problems)


def test_nothing_in_the_real_closure_is_unreviewed() -> None:
    """A new copyleft or unknown licence, or a new copyleft notice inside a wheel, fails here until
    someone reads it and adds a reviewed entry to scripts/third_party.py."""
    _, rows = _real_rows()
    assert third_party.unreviewed(rows) == []


def test_every_package_with_a_copyleft_licence_or_notice_is_on_the_page() -> None:
    _, rows = _real_rows()
    text = COMMITTED.read_text(encoding="utf-8")
    flagged = [r for r in rows if r.licence.copyleft or r.raw.notices]
    assert flagged, "nothing flagged? the tripwire would be asleep"
    page = _section(text, "2. Copyleft") + _section(text, "3. Unknown")
    for row in flagged:
        assert re.search(rf"\b{re.escape(row.package.name)}\b", page), row.package.name


def test_the_real_unknowns_are_the_ones_a_person_has_to_decide() -> None:
    _, rows = _real_rows()
    undecided = sorted(r.package.name for r in rows if r.licence.status != "known")
    assert undecided == ["spylls"]


def test_the_committed_inventory_records_the_hash_of_the_uv_lock_beside_it() -> None:
    text = COMMITTED.read_text(encoding="utf-8")
    assert f"`{uvlock.file_sha256(ROOT / 'uv.lock')}`" in text


def test_the_image_references_in_the_inventory_are_the_ones_the_build_uses() -> None:
    text = COMMITTED.read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for ref in re.findall(r"^\s+image:\s*(\S+)", compose, re.M):
        assert ref in text
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for ref in re.findall(r"^ARG (?:PYTHON|UV)_IMAGE=(\S+)", dockerfile, re.M):
        assert ref in text


# --- the command line -----------------------------------------------------------------------------


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "third_party.py"), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )


def test_check_passes_on_the_committed_file_and_fails_on_a_stale_one(tmp_path: Path) -> None:
    assert _run("--check", str(COMMITTED)).returncode == 0
    stale = tmp_path / "THIRD_PARTY.md"
    stale.write_text(
        COMMITTED.read_text(encoding="utf-8").replace("| fastapi |", "| fastapi-gone |", 1),
        encoding="utf-8",
    )
    done = _run("--check", str(stale))
    assert done.returncode == 1
    assert "stale" in done.stderr and "scripts/third_party.py --out" in done.stderr
    assert _run("--check", str(tmp_path / "missing.md")).returncode == 1


def test_out_writes_the_committed_bytes(tmp_path: Path) -> None:
    out = tmp_path / "t.md"
    assert _run("--out", str(out)).returncode == 0
    assert out.read_bytes() == COMMITTED.read_bytes()
    stdout = _run()
    assert stdout.returncode == 0 and stdout.stdout.encode() == COMMITTED.read_bytes()


def test_src_imports_finds_top_level_modules_in_every_form(tmp_path: Path) -> None:
    pkg = tmp_path / "app"
    pkg.mkdir()
    (pkg / "a.py").write_text(
        "import spylls.hunspell\nfrom certifi import where\n", encoding="utf-8"
    )
    (pkg / "b.py").write_text("from . import a\nimport os\n", encoding="utf-8")
    found = third_party.src_imports(tmp_path)
    assert found["spylls"] == ["app/a.py"]
    assert found["certifi"] == ["app/a.py"]
    assert "a" not in found  # a relative import is no third-party import


@pytest.mark.parametrize("name", ["certifi", "tqdm"])
def test_the_real_page_says_whether_the_application_imports_a_copyleft_package(name: str) -> None:
    text = COMMITTED.read_text(encoding="utf-8")
    line = next(ln for ln in _section(text, "2. Copyleft").splitlines() if f"| {name} |" in ln)
    assert "not imported by the application" in line


def test_the_real_page_says_where_the_application_imports_spylls() -> None:
    text = COMMITTED.read_text(encoding="utf-8")
    line = next(ln for ln in _section(text, "2. Copyleft").splitlines() if "| spylls |" in ln)
    assert "procurement_agent/checks/aoa_spelling.py" in line
