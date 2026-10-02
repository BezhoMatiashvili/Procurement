"""The model checksum manifest: written after the download, verified at every production start."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from procurement_agent import cli
from procurement_agent.config import Settings, get_settings
from procurement_agent.security import models_manifest as mm


def _models(tmp_path: Path) -> Path:
    root = tmp_path / "models"
    (root / "layout").mkdir(parents=True)
    (root / "tableformer" / "fast").mkdir(parents=True)
    (root / "layout" / "model.safetensors").write_bytes(os.urandom(5000))
    (root / "layout" / "config.json").write_text('{"a": 1}')
    (root / "tableformer" / "fast" / "tm.safetensors").write_bytes(os.urandom(3 * 1024 * 1024 + 7))
    return root


# --- write and verify --------------------------------------------------------------------------


def test_a_written_manifest_verifies_and_is_in_sha256sum_format(tmp_path: Path) -> None:
    root = _models(tmp_path)
    assert mm.write(root) == 3
    lines = (root / mm.MANIFEST_NAME).read_text().splitlines()
    assert len(lines) == 3
    for line in lines:
        digest, path = line.split("  ", 1)
        assert digest == hashlib.sha256((root / path).read_bytes()).hexdigest()
    assert lines == sorted(lines, key=lambda line: line.split("  ", 1)[1])
    result = mm.verify(root)
    assert result.ok and result.checked == 3 and result.problems == []


def test_the_manifest_does_not_list_itself_and_can_be_rewritten(tmp_path: Path) -> None:
    root = _models(tmp_path)
    mm.write(root)
    assert mm.write(root) == 3  # the first manifest is not one of the files
    assert mm.MANIFEST_NAME not in (root / mm.MANIFEST_NAME).read_text()


def test_a_changed_file_is_named(tmp_path: Path) -> None:
    root = _models(tmp_path)
    mm.write(root)
    weights = root / "layout" / "model.safetensors"
    data = bytearray(weights.read_bytes())
    data[100] ^= 0x01  # one bit
    weights.write_bytes(bytes(data))
    result = mm.verify(root)
    assert result.problems == ["changed layout/model.safetensors"]


def test_a_truncated_a_deleted_and_an_extra_file_are_all_reported(tmp_path: Path) -> None:
    root = _models(tmp_path)
    mm.write(root)
    (root / "tableformer" / "fast" / "tm.safetensors").write_bytes(b"short")
    (root / "layout" / "config.json").unlink()
    (root / "extra.bin").write_bytes(b"x")
    result = mm.verify(root)
    assert sorted(result.problems) == [
        "changed tableformer/fast/tm.safetensors",
        "missing layout/config.json",
        "not in the manifest: extra.bin",
    ]


def test_a_missing_manifest_is_a_problem(tmp_path: Path) -> None:
    result = mm.verify(_models(tmp_path))
    assert result.problems == [f"{mm.MANIFEST_NAME} is missing"]


@pytest.mark.parametrize(
    "line",
    [
        "not a manifest line",
        "abc  layout/model.safetensors",
        "0" * 64 + " layout/x",
        "0" * 64 + "  ",
    ],
)
def test_a_malformed_line_is_reported_not_skipped(tmp_path: Path, line: str) -> None:
    root = _models(tmp_path)
    mm.write(root)
    with (root / mm.MANIFEST_NAME).open("a") as fh:
        fh.write(line + "\n")
    assert any("is not '<sha256>  <path>'" in p for p in mm.verify(root).problems)


@pytest.mark.parametrize("path", ["../outside.bin", "/etc/passwd", "a/../../b", "./layout/x"])
def test_a_line_that_reaches_outside_the_directory_is_refused(tmp_path: Path, path: str) -> None:
    root = _models(tmp_path)
    mm.write(root)
    with (root / mm.MANIFEST_NAME).open("a") as fh:
        fh.write("0" * 64 + "  " + path + "\n")
    assert any("outside the directory" in p for p in mm.verify(root).problems)


def test_symbolic_links_are_neither_followed_nor_accepted(tmp_path: Path) -> None:
    root = _models(tmp_path)
    outside = tmp_path / "elsewhere.bin"
    outside.write_bytes(b"not a model")
    (root / "layout" / "link.bin").symlink_to(outside)
    with pytest.raises(ValueError, match="symbolic link"):
        mm.write(root)
    (root / "layout" / "link.bin").unlink()
    mm.write(root)
    (root / "layout" / "link.bin").symlink_to(outside)
    assert any(p.startswith("symbolic link layout/link.bin") for p in mm.verify(root).problems)


def test_a_large_file_is_hashed_without_being_read_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _models(tmp_path)
    sizes: list[int] = []
    real_open = Path.open

    class Spy:
        def __init__(self, fh: object) -> None:
            self.fh = fh

        def __enter__(self) -> Spy:
            self.fh.__enter__()  # type: ignore[attr-defined]
            return self

        def __exit__(self, *exc: object) -> None:
            self.fh.__exit__(*exc)  # type: ignore[attr-defined]

        def read(self, n: int = -1) -> bytes:
            data: bytes = self.fh.read(n)  # type: ignore[attr-defined]
            sizes.append(len(data))
            return data

    monkeypatch.setattr(
        Path,
        "open",
        lambda self, *a, **k: (
            Spy(real_open(self, *a, **k))
            if "b" in (a[0] if a else k.get("mode", ""))
            else real_open(self, *a, **k)
        ),
    )
    mm.write(root)
    assert max(sizes) <= 1024 * 1024


# --- production start-up -----------------------------------------------------------------------


def _prod(models: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "env": "prod",
        "docling_artifacts_path": models,
        "allowed_hosts": ("tasks.company.test",),
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_production_refuses_to_start_when_the_models_do_not_match_their_manifest(
    tmp_path: Path,
) -> None:
    root = _models(tmp_path)
    with pytest.raises(
        RuntimeError, match=r"do not match their manifest.*MANIFEST\.sha256 is missing"
    ):
        _prod(root).validate_for_prod()
    mm.write(root)
    _prod(root).validate_for_prod()  # now it starts
    (root / "layout" / "model.safetensors").write_bytes(b"swapped")
    with pytest.raises(RuntimeError, match=r"changed layout/model\.safetensors"):
        _prod(root).validate_for_prod()


def test_the_message_is_bounded_when_many_files_differ(tmp_path: Path) -> None:
    root = _models(tmp_path)
    mm.write(root)
    for i in range(20):
        (root / f"extra{i}.bin").write_bytes(b"x")
    with pytest.raises(RuntimeError) as raised:
        _prod(root).validate_for_prod()
    assert "and 15 more" in str(raised.value)


def test_the_check_can_be_switched_off_and_never_applies_in_development(
    tmp_path: Path,
) -> None:
    root = _models(tmp_path)  # no manifest
    _prod(root, models_manifest_required=False).validate_for_prod()
    Settings(env="dev", docling_artifacts_path=root).validate_for_prod()
    assert Settings().models_manifest_required is True


# --- the command line --------------------------------------------------------------------------


def test_the_command_writes_and_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _models(tmp_path)
    monkeypatch.setenv("PA_DOCLING_ARTIFACTS_PATH", str(root))
    monkeypatch.setenv("PA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PA_OLLAMA_BASE_URL", "http://127.0.0.1:1")
    get_settings.cache_clear()
    try:
        assert cli.main(["models-manifest", "verify"]) == 1
        assert f"{mm.MANIFEST_NAME} is missing" in capsys.readouterr().out
        assert cli.main(["models-manifest", "write"]) == 0
        assert "3 file(s)" in capsys.readouterr().out
        assert cli.main(["models-manifest", "verify"]) == 0
        assert "3 file(s) checked, 0 problem(s)" in capsys.readouterr().out
        (root / "layout" / "config.json").write_text("{}")
        assert cli.main(["models-manifest", "verify"]) == 1
        assert "changed layout/config.json" in capsys.readouterr().out
    finally:
        import procurement_agent.db as dbmod

        dbmod._engine = None
        dbmod._SessionLocal = None
        get_settings.cache_clear()


def test_the_command_needs_a_models_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("PA_DOCLING_ARTIFACTS_PATH", raising=False)
    monkeypatch.setenv("PA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PA_OLLAMA_BASE_URL", "http://127.0.0.1:1")
    get_settings.cache_clear()
    try:
        assert cli.main(["models-manifest", "write"]) == 2
        assert "PA_DOCLING_ARTIFACTS_PATH" in capsys.readouterr().err
    finally:
        import procurement_agent.db as dbmod

        dbmod._engine = None
        dbmod._SessionLocal = None
        get_settings.cache_clear()
