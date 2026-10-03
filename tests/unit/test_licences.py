"""How a distribution's licence is read and classified (scripts/licences.py): from what it declares,
by a small reviewed table, and never by a guess. Synthetic distributions only."""

from __future__ import annotations

import sys
from importlib.metadata import PathDistribution
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))  # the scripts import one another by name

import licences  # noqa: E402
from licences import Licence, Raw  # noqa: E402


def _raw(
    *,
    expression: str | None = None,
    field: str | None = None,
    classifiers: tuple[str, ...] = (),
    titles: tuple[tuple[str, str], ...] = (),
    notices: tuple[tuple[str, str], ...] = (),
) -> Raw:
    return Raw(
        expression=expression,
        license_field=field,
        classifiers=classifiers,
        primary_titles=titles,
        notices=notices,
    )


def _classify(raw: Raw, name: str = "pkg", version: str = "1.0") -> Licence:
    return licences.classify(name, version, raw)


# --- precedence: License-Expression, then License, then the classifiers -------------------------


def test_a_license_expression_wins_over_everything_else() -> None:
    got = _classify(
        _raw(
            expression="MIT",
            field="GPL-3.0-or-later",
            classifiers=("OSI Approved :: BSD License",),
        )
    )
    assert (got.status, got.spdx, got.families, got.basis) == (
        "known",
        "MIT",
        ("MIT",),
        "License-Expression",
    )


def test_a_compound_expression_keeps_every_licence_and_exception() -> None:
    got = _classify(_raw(expression="Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND MIT"))
    assert got.status == "known"
    assert got.spdx == "Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND MIT"
    assert got.families == ("Apache", "MIT")
    assert got.copyleft == ()


def test_a_license_field_that_is_an_spdx_expression_is_read_as_one() -> None:
    got = _classify(_raw(field="MPL-2.0 AND MIT"))
    assert (got.spdx, got.families, got.copyleft, got.basis) == (
        "MPL-2.0 AND MIT",
        ("MIT", "MPL"),
        ("MPL",),
        "License",
    )


@pytest.mark.parametrize(
    ("text", "spdx"),
    [
        ("MIT License", "MIT"),
        ("Apache 2.0", "Apache-2.0"),
        ("Apache 2.0 License", "Apache-2.0"),
        ("BSD 3-Clause", "BSD-3-Clause"),
        ("ISC License", "ISC"),
        ("PSFL", "PSF-2.0"),
    ],
)
def test_the_free_text_table_names_one_licence(text: str, spdx: str) -> None:
    got = _classify(_raw(field=text))
    assert (got.status, got.spdx, got.basis) == ("known", spdx, "License")


@pytest.mark.parametrize(
    ("text", "family"),
    [("BSD", "BSD"), ("Apache", "Apache"), ("bsd", "BSD")],
)
def test_a_name_that_leaves_the_variant_open_gets_a_family_and_no_spdx_id(
    text: str, family: str
) -> None:
    got = _classify(_raw(field=text))
    assert got.status == "known"
    assert got.spdx is None
    assert got.families == (family,)
    assert "not stated" in got.declared


def test_the_whole_licence_text_in_the_license_field_is_not_a_name() -> None:
    got = _classify(
        _raw(
            field="BSD 3-Clause License\n\nCopyright (c) 2008, Someone\nAll rights reserved.\n" * 5,
            classifiers=("OSI Approved :: BSD License",),
        )
    )
    assert (got.status, got.families, got.basis) == ("known", ("BSD",), "Classifier")


@pytest.mark.parametrize("junk", ["UNKNOWN", "", "  ", "unknown"])
def test_a_placeholder_license_field_is_no_declaration(junk: str) -> None:
    got = _classify(_raw(field=junk, classifiers=("OSI Approved :: MIT License",)))
    assert (got.spdx, got.basis) == ("MIT", "Classifier")


def test_classifiers_that_name_one_licence_give_its_id() -> None:
    got = _classify(_raw(classifiers=("OSI Approved :: MIT License",)))
    assert (got.spdx, got.families, got.basis) == ("MIT", ("MIT",), "Classifier")


def test_several_classifiers_are_listed_together_and_no_combination_is_assumed() -> None:
    got = _classify(
        _raw(
            field="Dual License",
            classifiers=(
                "OSI Approved :: Apache Software License",
                "OSI Approved :: BSD License",
            ),
        )
    )
    assert got.status == "known"
    assert got.spdx is None  # "and" or "or" is not in the metadata
    assert got.families == ("Apache", "BSD")
    assert "Apache" in got.declared and "BSD" in got.declared


def test_the_bare_osi_approved_classifier_says_nothing() -> None:
    got = _classify(_raw(classifiers=("OSI Approved", "OSI Approved :: MIT License")))
    assert got.spdx == "MIT"


# --- unknown, never guessed ------------------------------------------------------------------


def test_nothing_declared_is_unknown() -> None:
    got = _classify(_raw())
    assert got.status == "unknown"
    assert got.spdx is None and got.families == ()
    assert "no licence" in got.note


def test_a_license_name_outside_the_table_with_no_classifier_is_unknown_and_quoted() -> None:
    got = _classify(_raw(field="Some Custom Terms 4"))
    assert got.status == "unknown"
    assert "Some Custom Terms 4" in got.note


def test_a_classifier_outside_the_table_is_unknown_and_quoted() -> None:
    got = _classify(_raw(classifiers=("Other/Proprietary License",)))
    assert got.status == "unknown"
    assert "Other/Proprietary License" in got.note


def test_a_valid_spdx_id_the_table_has_no_family_for_is_unknown() -> None:
    got = _classify(_raw(expression="Artistic-2.0"))
    assert got.status == "unknown"
    assert "Artistic-2.0" in got.note


def test_an_invalid_expression_is_unknown() -> None:
    got = _classify(_raw(expression="MIT or maybe BSD"))
    assert got.status == "unknown"


# --- copyleft and restrictive ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "copyleft"),
    [
        ("MPL-2.0", ("MPL",)),
        ("LGPL-2.1-or-later", ("LGPL",)),
        ("GPL-3.0-or-later WITH GCC-exception-3.1", ("GPL",)),
        ("AGPL-3.0-only", ("AGPL",)),
        ("EPL-2.0", ("EPL",)),
        ("CC-BY-SA-4.0", ("CC-BY-SA",)),
        ("SSPL-1.0", ("SSPL",)),
        ("CC-BY-NC-4.0", ("non-commercial",)),
        ("MIT OR Apache-2.0", ()),
    ],
)
def test_copyleft_and_restrictive_families_are_named(
    expression: str, copyleft: tuple[str, ...]
) -> None:
    got = _classify(_raw(expression=expression))
    assert got.status == "known"
    assert got.copyleft == copyleft


def test_a_licence_file_that_names_a_stricter_licence_than_the_metadata_is_a_conflict() -> None:
    got = _classify(
        _raw(
            field="UNKNOWN",
            classifiers=("OSI Approved :: MIT License",),
            titles=(("LICENSE", "MPL"),),
        )
    )
    assert got.status == "conflict"
    assert "MIT" in got.note and "MPL" in got.note
    assert "MPL" in got.copyleft  # reported as copyleft until a person decides


def test_a_licence_file_that_agrees_with_the_metadata_is_no_conflict() -> None:
    got = _classify(_raw(expression="MPL-2.0", titles=(("LICENSE", "MPL"),)))
    assert got.status == "known"


def test_a_reviewed_entry_applies_to_its_version_only() -> None:
    raw = _raw(field="BSD-3-Clause, Apache-2.0, dependency licenses")
    assert _classify(raw, "pypdfium2", "5.13.0").status == "known"
    assert _classify(raw, "pypdfium2", "5.13.0").basis.startswith("reviewed")
    moved = _classify(raw, "pypdfium2", "5.14.0")
    assert moved.status == "unknown"  # a new version is looked at again


# --- reading a distribution --------------------------------------------------------------------


def _dist(tmp_path: Path, metadata: list[str], files: dict[str, str]) -> PathDistribution:
    info = tmp_path / "pkg-1.0.dist-info"
    info.mkdir()
    header = ["Metadata-Version: 2.4", "Name: pkg", "Version: 1.0", *metadata]
    (info / "METADATA").write_text("\n".join(header) + "\n", encoding="utf-8")
    record = []
    for rel, text in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        record.append(f"{rel},,")
    (info / "RECORD").write_text("".join(f"{line}\n" for line in record), encoding="utf-8")
    return PathDistribution(info)


def test_read_raw_takes_the_three_fields_and_only_license_classifiers(tmp_path: Path) -> None:
    dist = _dist(
        tmp_path,
        [
            "License-Expression: MIT",
            "License: Something",
            "Classifier: Programming Language :: Python :: 3",
            "Classifier: License :: OSI Approved :: MIT License",
        ],
        {},
    )
    raw = licences.read_raw(dist)
    assert raw.expression == "MIT"
    assert raw.license_field == "Something"
    assert raw.classifiers == ("OSI Approved :: MIT License",)


def test_read_raw_sees_the_title_of_the_primary_licence_file(tmp_path: Path) -> None:
    dist = _dist(
        tmp_path,
        ["License: UNKNOWN"],
        {
            "pkg-1.0.dist-info/LICENSE": "Mozilla Public License Version 2.0\n==========\n\n1. x\n",
            "pkg-1.0.dist-info/licenses/NOTICE.txt": "unrelated\n",
        },
    )
    raw = licences.read_raw(dist)
    assert raw.primary_titles == (("pkg-1.0.dist-info/LICENSE", "MPL"),)


def test_a_title_far_down_a_bundled_notice_is_not_a_primary_title(tmp_path: Path) -> None:
    body = "Copyright (c) the authors\n" + "text\n" * 40 + "GNU GENERAL PUBLIC LICENSE\nVersion 3\n"
    dist = _dist(
        tmp_path, ["License-Expression: BSD-3-Clause"], {"pkg-1.0.dist-info/LICENSE": body}
    )
    raw = licences.read_raw(dist)
    assert raw.primary_titles == ()
    assert raw.notices == (("pkg-1.0.dist-info/LICENSE", "GPL"),)  # but the tripwire sees it


def test_bundled_notices_name_the_copyleft_libraries_they_ship(tmp_path: Path) -> None:
    notice = (
        "Name: libquadmath\n"
        "Files: pkg.libs/libquadmath*.so\n"
        "License: LGPL-2.1-or-later\n"
        "\n"
        "Name: libgfortran\n"
        "License: GPL-3.0-or-later WITH GCC-exception-3.1\n"
    )
    dist = _dist(
        tmp_path, ["License-Expression: BSD-3-Clause"], {"pkg-1.0.dist-info/LICENSE.txt": notice}
    )
    assert licences.read_raw(dist).notices == (
        ("pkg-1.0.dist-info/LICENSE.txt", "GPL"),
        ("pkg-1.0.dist-info/LICENSE.txt", "LGPL"),
    )


def test_a_word_in_passing_is_not_a_notice(tmp_path: Path) -> None:
    prose = (
        "PSF LICENSE AGREEMENT\n"
        "Python is GPL-compatible. Programs may also be non-GPL, and the GNU General Public\n"
        "License is mentioned inline here only.\n"
    )
    dist = _dist(tmp_path, ["License-Expression: PSF-2.0"], {"pkg-1.0.dist-info/LICENSE": prose})
    assert licences.read_raw(dist).notices == ()


def test_python_files_named_after_licences_are_not_licence_files(tmp_path: Path) -> None:
    dist = _dist(
        tmp_path,
        ["License-Expression: MIT"],
        {"pkg/licenses/_spdx.py": "AGPL-3.0\nGNU Affero General Public License\n"},
    )
    assert licences.read_raw(dist).notices == ()


def test_non_commercial_terms_are_noticed(tmp_path: Path) -> None:
    dist = _dist(
        tmp_path,
        ["License-Expression: MIT"],
        {"pkg-1.0.dist-info/LICENSE": "MIT\nOnly for non-commercial purposes.\n"},
    )
    assert licences.read_raw(dist).notices == (("pkg-1.0.dist-info/LICENSE", "non-commercial"),)


# --- the installed distribution must be the locked one ---------------------------------------


def test_a_package_that_is_not_installed_is_an_error_not_an_empty_licence() -> None:
    with pytest.raises(licences.NotInstalledError, match="uv sync"):
        licences.installed("definitely-not-a-real-distribution", "1.0")


def test_an_installed_package_at_another_version_is_an_error() -> None:
    with pytest.raises(licences.NotInstalledError, match="locked"):
        licences.installed("pytest", "0.0.1")


# --- the real environment ----------------------------------------------------------------------


def test_the_shipped_licence_of_spylls_overrules_its_classifier() -> None:
    """spylls 0.1.7 declares `License: UNKNOWN` and the MIT classifier, and ships the text of
    the Mozilla Public License 2.0. The conflict must be reported, not resolved by a guess."""
    dist = licences.installed_any("spylls")
    got = licences.classify("spylls", dist.version, licences.read_raw(dist))
    assert got.status == "conflict"
    assert "MPL" in got.copyleft
