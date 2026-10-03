"""The CycloneDX SBOM (scripts/sbom.py) and the production closure it is made from
(scripts/uvlock.py). Synthetic locks for the rules; the real uv.lock for the committed file."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))  # the scripts import one another by name

import licences  # noqa: E402
import sbom  # noqa: E402
import uvlock  # noqa: E402

COMMITTED = ROOT / "docs" / "sbom.cdx.json"


# --- a synthetic lock ----------------------------------------------------------------------------


def _pkg(
    name: str,
    version: str,
    deps: tuple[Any, ...] = (),
    *,
    optional: dict[str, list[Any]] | None = None,
    dev: dict[str, list[Any]] | None = None,
    source: dict[str, str] | None = None,
    digest: str = "0",
) -> dict[str, Any]:
    p: dict[str, Any] = {
        "name": name,
        "version": version,
        "source": source or {"registry": "https://pypi.org/simple"},
    }
    if deps:
        p["dependencies"] = [d if isinstance(d, dict) else {"name": d} for d in deps]
    if optional:
        p["optional-dependencies"] = {
            k: [d if isinstance(d, dict) else {"name": d} for d in v] for k, v in optional.items()
        }
    if dev:
        p["dev-dependencies"] = {
            k: [d if isinstance(d, dict) else {"name": d} for d in v] for k, v in dev.items()
        }
    if p["source"].get("registry"):
        h = digest * 64
        p["sdist"] = {
            "url": f"https://files.example/{name}-{version}.tar.gz",
            "hash": f"sha256:{h}",
        }
        p["wheels"] = [
            {
                "url": f"https://files.example/{name}-{version}-py3-none-any.whl",
                "hash": f"sha256:{h[::-1]}",
            }
        ]
    return p


def _lock(*packages: dict[str, Any]) -> dict[str, Any]:
    return {"version": 1, "requires-python": "==3.12.*", "package": list(packages)}


def _root(*deps: Any, **kwargs: Any) -> dict[str, Any]:
    return _pkg("proj", "0.1.0", deps, source={"editable": "."}, **kwargs)


def _names(closure: uvlock.Closure) -> list[str]:
    return [p.key for p in closure.packages]


# --- the production closure ----------------------------------------------------------------------


def test_the_dev_group_and_the_optional_extras_are_not_in_the_closure() -> None:
    lock = _lock(
        _root("a", dev={"dev": ["pytest"]}, optional={"embeddings": ["st"]}),
        _pkg("a", "1", ("b",)),
        _pkg("b", "2"),
        _pkg("pytest", "9"),
        _pkg("st", "3"),
    )
    closure = uvlock.production_closure(lock)
    assert _names(closure) == ["a@1", "b@2"]
    assert closure.root.name == "proj"


def test_markers_are_evaluated_for_the_image_platform() -> None:
    lock = _lock(
        _root(
            {"name": "torch", "version": "2.1", "marker": "sys_platform == 'darwin'"},
            {"name": "torch", "version": "2.1+cpu", "marker": "sys_platform != 'darwin'"},
            {"name": "colorama", "marker": "sys_platform == 'win32'"},
            {"name": "uvloop", "marker": "sys_platform != 'win32'"},
        ),
        _pkg("torch", "2.1"),
        _pkg("torch", "2.1+cpu"),
        _pkg("colorama", "0.4"),
        _pkg("uvloop", "0.22"),
    )
    assert _names(uvlock.production_closure(lock)) == ["torch@2.1+cpu", "uvloop@0.22"]


def test_an_extra_named_by_a_dependency_pulls_in_that_extras_packages_only() -> None:
    with_extra = _lock(
        _root({"name": "uvicorn", "extra": ["standard"]}),
        _pkg("uvicorn", "0.5", optional={"standard": ["httptools"], "other": ["nope"]}),
        _pkg("httptools", "1"),
        _pkg("nope", "1"),
    )
    assert _names(uvlock.production_closure(with_extra)) == ["httptools@1", "uvicorn@0.5"]
    without = _lock(
        _root("uvicorn"),
        _pkg("uvicorn", "0.5", optional={"standard": ["httptools"]}),
        _pkg("httptools", "1"),
    )
    assert _names(uvlock.production_closure(without)) == ["uvicorn@0.5"]


def test_an_extra_reached_late_still_adds_its_packages() -> None:
    """a needs b[x]; c needs b plain: visiting b plain first must not hide b's extra."""
    lock = _lock(
        _root("c", "a"),
        _pkg("c", "1", ("b",)),
        _pkg("a", "1", ({"name": "b", "extra": ["x"]},)),
        _pkg("b", "1", optional={"x": ["d"]}),
        _pkg("d", "1"),
    )
    assert _names(uvlock.production_closure(lock)) == ["a@1", "b@1", "c@1", "d@1"]


def test_one_name_at_two_versions_is_told_apart_by_the_versions_the_lock_names() -> None:
    lock = _lock(
        _root({"name": "x", "version": "2"}),
        _pkg("x", "1"),
        _pkg("x", "2"),
    )
    assert _names(uvlock.production_closure(lock)) == ["x@2"]


def test_required_by_is_the_reverse_of_the_edges_and_cycles_end() -> None:
    lock = _lock(
        _root("a"),
        _pkg("a", "1", ("b",)),
        _pkg("b", "1", ("a", "c")),
        _pkg("c", "1"),
    )
    closure = uvlock.production_closure(lock)
    by_key = {p.key: p for p in closure.packages}
    assert by_key["c@1"].required_by == ("b@1",)
    assert by_key["a@1"].required_by == ("b@1", "proj@0.1.0")
    assert by_key["a@1"].depends_on == ("b@1",)


def test_a_dependency_the_lock_does_not_hold_is_an_error() -> None:
    lock = _lock(_root("missing"))
    with pytest.raises(uvlock.LockError, match="missing"):
        uvlock.production_closure(lock)


def test_the_files_the_lock_records_come_through_with_their_hashes() -> None:
    lock = _lock(_root("a"), _pkg("a", "1", digest="a"))
    (pkg,) = uvlock.production_closure(lock).packages
    kinds = {f.kind: f for f in pkg.files}
    assert kinds["sdist"].sha256 == "a" * 64
    assert kinds["wheel"].sha256 == ("a" * 64)[::-1]
    assert kinds["wheel"].url.endswith(".whl")


def test_the_lock_must_have_exactly_one_project_root() -> None:
    with pytest.raises(uvlock.LockError, match="root"):
        uvlock.production_closure(_lock(_pkg("a", "1")))


def test_the_target_environment_is_the_pythons_of_the_dockerfile() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    image = re.search(r"^ARG PYTHON_IMAGE=python:(\d+\.\d+\.\d+)-", dockerfile, re.M)
    assert image is not None
    assert uvlock.TARGET_ENVIRONMENT["python_full_version"] == image.group(1)


# --- the SBOM document ---------------------------------------------------------------------------


def _known(spdx: str | None = "MIT") -> licences.Licence:
    return licences.Licence(
        status="known",
        declared=spdx or "x",
        spdx=spdx,
        families=("MIT",),
        copyleft=(),
        basis="License-Expression",
        note="",
    )


def _build(lock: dict[str, Any], lookup: Any = None, **kwargs: Any) -> dict[str, Any]:
    closure = uvlock.production_closure(lock)
    return sbom.build_sbom(
        closure,
        lock_sha256="f" * 64,
        root_licence=sbom.PROPRIETARY,
        licence_of=lookup or (lambda name, version: _known()),
        **kwargs,
    )


def _small_lock() -> dict[str, Any]:
    return _lock(
        _root("a", "torch"),
        _pkg("a", "1.0", ("b",), digest="a"),
        _pkg("b", "2.0"),
        _pkg("torch", "2.14.1+cpu"),
    )


def test_each_component_has_name_version_purl_hashes_and_a_licence() -> None:
    doc = _build(_small_lock())
    comp = {c["name"]: c for c in doc["components"]}
    a = comp["a"]
    assert (a["type"], a["version"], a["purl"]) == ("library", "1.0", "pkg:pypi/a@1.0")
    assert a["licenses"] == [{"license": {"id": "MIT"}}]
    refs = {r["url"]: r["hashes"] for r in a["externalReferences"]}
    assert refs["https://files.example/a-1.0.tar.gz"] == [{"alg": "SHA-256", "content": "a" * 64}]
    assert all(r["type"] == "distribution" for r in a["externalReferences"])


def test_a_local_version_is_percent_encoded_in_the_purl_and_is_the_bom_ref() -> None:
    doc = _build(_small_lock())
    torch = next(c for c in doc["components"] if c["name"] == "torch")
    assert torch["version"] == "2.14.1+cpu"
    assert torch["purl"] == "pkg:pypi/torch@2.14.1%2Bcpu"
    assert torch["bom-ref"] == torch["purl"]


def test_the_project_is_the_root_component_without_a_purl() -> None:
    doc = _build(_small_lock())
    root = doc["metadata"]["component"]
    assert (root["type"], root["name"], root["version"]) == ("application", "proj", "0.1.0")
    assert "purl" not in root  # a PyPI purl would match a package that is not this one
    assert root["licenses"] == [{"license": {"name": "Proprietary"}}]
    assert root["bom-ref"] == "proj@0.1.0"
    assert all(c["bom-ref"] != root["bom-ref"] for c in doc["components"])


def test_the_graph_has_an_entry_for_the_root_and_every_component_sorted() -> None:
    doc = _build(_small_lock())
    graph = {d["ref"]: d.get("dependsOn", []) for d in doc["dependencies"]}
    assert graph["proj@0.1.0"] == ["pkg:pypi/a@1.0", "pkg:pypi/torch@2.14.1%2Bcpu"]
    assert graph["pkg:pypi/a@1.0"] == ["pkg:pypi/b@2.0"]
    assert graph["pkg:pypi/b@2.0"] == []
    assert [d["ref"] for d in doc["dependencies"]] == sorted(graph)
    assert {c["bom-ref"] for c in doc["components"]} | {"proj@0.1.0"} == set(graph)


def test_components_are_sorted_and_two_builds_are_identical_bytes() -> None:
    lock = _small_lock()
    first = sbom.render(_build(lock))
    second = sbom.render(_build(lock))
    assert first == second
    assert first.endswith("\n")
    refs = [c["bom-ref"] for c in json.loads(first)["components"]]
    assert refs == sorted(refs)


def test_no_timestamp_unless_asked_for() -> None:
    assert "timestamp" not in _build(_small_lock())["metadata"]
    stamped = _build(_small_lock(), timestamp="2026-10-03T00:00:00Z")
    assert stamped["metadata"]["timestamp"] == "2026-10-03T00:00:00Z"


def test_the_serial_number_is_derived_from_the_lock_hash() -> None:
    lock = _small_lock()
    closure = uvlock.production_closure(lock)

    def serial(digest: str) -> str:
        doc = sbom.build_sbom(
            closure,
            lock_sha256=digest,
            root_licence=sbom.PROPRIETARY,
            licence_of=lambda n, v: _known(),
        )
        return str(doc["serialNumber"])

    assert serial("1" * 64) == serial("1" * 64)
    assert serial("1" * 64) != serial("2" * 64)
    assert uuid.UUID(serial("1" * 64).removeprefix("urn:uuid:")).version == 5


def test_the_lock_hash_and_the_scope_are_recorded_for_the_bundle_script() -> None:
    props = {p["name"]: p["value"] for p in _build(_small_lock())["metadata"]["properties"]}
    assert props["pa:uv-lock-sha256"] == "f" * 64
    assert "linux" in props["pa:target-environment"] and "x86_64" in props["pa:target-environment"]
    assert "no dependency groups" in props["pa:scope"]


@pytest.mark.parametrize(
    ("licence", "expected"),
    [
        (_known("MIT"), [{"license": {"id": "MIT"}}]),
        (_known("MIT AND PSF-2.0"), [{"expression": "MIT AND PSF-2.0"}]),
        (_known("MIT OR Apache-2.0"), [{"expression": "MIT OR Apache-2.0"}]),
        (
            licences.Licence(
                "known", "BSD (variant not stated)", None, ("BSD",), (), "License", ""
            ),
            [{"license": {"name": "BSD (variant not stated)"}}],
        ),
        (
            licences.Licence("conflict", "x", None, (), ("MPL",), "License", "metadata says MIT"),
            [{"license": {"name": "UNRESOLVED: metadata says MIT (see docs/THIRD_PARTY.md)"}}],
        ),
    ],
)
def test_licences_are_encoded_as_id_expression_or_name(
    licence: licences.Licence, expected: list[dict[str, Any]]
) -> None:
    assert sbom.cyclonedx_licences(licence) == expected


def test_an_unknown_licence_is_left_out_and_marked_rather_than_invented() -> None:
    unknown = licences.Licence("unknown", "", None, (), (), "none", "no licence metadata")
    doc = _build(_small_lock(), lambda n, v: unknown)
    comp = doc["components"][0]
    assert "licenses" not in comp
    assert {"name": "pa:licence", "value": "unknown"} in comp["properties"]


def test_a_package_the_environment_lacks_stops_the_build() -> None:
    def lookup(name: str, version: str) -> licences.Licence:
        raise licences.NotInstalledError(f"{name} {version} is not installed: run uv sync")

    with pytest.raises(licences.NotInstalledError, match="uv sync"):
        _build(_small_lock(), lookup)


# --- the committed file ------------------------------------------------------------------------


def _real() -> tuple[dict[str, Any], uvlock.Closure]:
    lock = uvlock.read_lock(ROOT / "uv.lock")
    return lock, uvlock.production_closure(lock)


def test_the_committed_sbom_is_current_with_uv_lock() -> None:
    """Fails when uv.lock changed and docs/sbom.cdx.json was not regenerated:
    uv run python scripts/sbom.py --out docs/sbom.cdx.json"""
    problems = sbom.check(ROOT / "uv.lock", COMMITTED)
    assert problems == [], "\n".join(problems)


def test_the_committed_sbom_lists_the_production_closure_and_no_dev_package() -> None:
    lock, closure = _real()
    doc = json.loads(COMMITTED.read_text(encoding="utf-8"))
    listed = {(c["name"], c["version"]) for c in doc["components"]}
    assert listed == {(p.name, p.version) for p in closure.packages}
    root = next(p for p in lock["package"] if p["source"].get("editable"))
    dev_names = {d["name"] for group in root["dev-dependencies"].values() for d in group}
    assert dev_names, "the lock names no dev packages? the test would pass vacuously"
    assert not dev_names & {c["name"] for c in doc["components"]}
    assert doc["metadata"]["component"]["name"] == "procurement-agent"
    assert "timestamp" not in doc["metadata"]


def test_every_locked_hash_of_every_listed_package_is_in_the_committed_sbom() -> None:
    _, closure = _real()
    doc = json.loads(COMMITTED.read_text(encoding="utf-8"))
    in_sbom = {
        h["content"]
        for c in doc["components"]
        for r in c["externalReferences"]
        for h in r["hashes"]
    }
    wanted = {f.sha256 for p in closure.packages for f in p.files}
    assert wanted and wanted == in_sbom


def test_the_committed_sbom_records_the_hash_of_the_uv_lock_beside_it() -> None:
    doc = json.loads(COMMITTED.read_text(encoding="utf-8"))
    props = {p["name"]: p["value"] for p in doc["metadata"]["properties"]}
    assert props["pa:uv-lock-sha256"] == uvlock.file_sha256(ROOT / "uv.lock")


def test_the_committed_sbom_is_valid_cyclonedx_1_5() -> None:
    pytest.importorskip("cyclonedx")
    from cyclonedx.schema import SchemaVersion
    from cyclonedx.validation.json import JsonStrictValidator

    error = JsonStrictValidator(SchemaVersion.V1_5).validate_str(
        COMMITTED.read_text(encoding="utf-8")
    )
    assert error is None, str(error)


# --- the command line ----------------------------------------------------------------------------


def _run(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "sbom.py"), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_check_passes_on_the_committed_file_and_names_a_stale_one(tmp_path: Path) -> None:
    assert _run("--check", str(COMMITTED)).returncode == 0
    stale = json.loads(COMMITTED.read_text(encoding="utf-8"))
    stale["components"].pop()
    path = tmp_path / "stale.json"
    path.write_text(json.dumps(stale, indent=2) + "\n", encoding="utf-8")
    done = _run("--check", str(path))
    assert done.returncode == 1
    assert "stale" in done.stderr and "scripts/sbom.py --out" in done.stderr
    assert _run("--check", str(tmp_path / "missing.json")).returncode == 1


def test_check_ignores_a_timestamp_and_out_writes_the_same_bytes(tmp_path: Path) -> None:
    out = tmp_path / "bom.json"
    assert _run("--out", str(out)).returncode == 0
    assert out.read_bytes() == COMMITTED.read_bytes()
    stamped = tmp_path / "stamped.json"
    assert _run("--out", str(stamped), "--timestamp", "2026-10-03T00:00:00Z").returncode == 0
    assert "2026-10-03T00:00:00Z" in stamped.read_text(encoding="utf-8")
    assert _run("--check", str(stamped)).returncode == 0
