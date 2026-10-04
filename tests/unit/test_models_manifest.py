"""The model checksum manifest: written after the download, verified at every production start."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
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
        "parser_mode": "inline",  # the parser's own settings have their tests
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


# --- files the app user cannot read ------------------------------------------------------------
# A models directory copied with `cp -a` keeps the owner and the mode of the source. The
# huggingface cache files under it are mode 600: the host user reads them, the image's user
# (uid 999) does not, and a production start died with a raw PermissionError traceback (exit 3)
# instead of a refusal.

needs_a_normal_user = pytest.mark.skipif(
    os.geteuid() == 0, reason="root reads files whatever their mode"
)


@pytest.fixture
def locked(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    """A manifest-ed tree with one file no one can read; ``(root, that file)``."""
    root = _models(tmp_path)
    mm.write(root)
    victim = root / "layout" / "config.json"
    victim.chmod(0)
    try:
        yield root, victim
    finally:
        victim.chmod(0o644)


@needs_a_normal_user
def test_an_unreadable_file_is_a_named_problem_and_not_a_traceback(
    locked: tuple[Path, Path],
) -> None:
    root, _ = locked
    result = mm.verify(root)
    assert result.problems == ["unreadable layout/config.json"]
    assert result.unreadable == ["layout/config.json"]
    assert result.checked == 2, "the other files are still checked"
    assert not result.ok


@needs_a_normal_user
def test_every_unreadable_file_is_named_and_the_readable_ones_are_still_compared(
    locked: tuple[Path, Path],
) -> None:
    root, _ = locked
    other = root / "layout" / "model.safetensors"
    other.chmod(0)
    try:
        (root / "tableformer" / "fast" / "tm.safetensors").write_bytes(b"swapped")
        result = mm.verify(root)
    finally:
        other.chmod(0o644)
    assert result.unreadable == ["layout/config.json", "layout/model.safetensors"]
    assert "changed tableformer/fast/tm.safetensors" in result.problems


@needs_a_normal_user
def test_an_unreadable_directory_is_one_problem_not_a_missing_file_per_entry(
    tmp_path: Path,
) -> None:
    root = _models(tmp_path)
    mm.write(root)
    (root / "tableformer").chmod(0)
    try:
        result = mm.verify(root)
    finally:
        (root / "tableformer").chmod(0o755)
    assert result.problems == ["unreadable directory tableformer"]
    assert result.unreadable == ["tableformer"]


@needs_a_normal_user
def test_an_unreadable_manifest_is_a_problem_and_not_a_traceback(tmp_path: Path) -> None:
    root = _models(tmp_path)
    mm.write(root)
    (root / mm.MANIFEST_NAME).chmod(0)
    try:
        result = mm.verify(root)
    finally:
        (root / mm.MANIFEST_NAME).chmod(0o644)
    assert result.problems == [f"unreadable {mm.MANIFEST_NAME}"]
    assert result.unreadable == [mm.MANIFEST_NAME]


@needs_a_normal_user
def test_an_unreadable_models_directory_is_one_problem(tmp_path: Path) -> None:
    root = _models(tmp_path)
    mm.write(root)
    root.chmod(0)
    try:
        result = mm.verify(root)
    finally:
        root.chmod(0o755)
    assert result.unreadable == ["the models directory"]
    assert len(result.problems) == 1


def test_a_file_that_vanishes_between_the_listing_and_the_read_is_missing_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _models(tmp_path)
    mm.write(root)
    real = mm._digest

    def vanishing(path: Path) -> str:
        if path.name == "config.json":
            raise FileNotFoundError(path)
        return real(path)

    monkeypatch.setattr(mm, "_digest", vanishing)
    result = mm.verify(root)
    assert result.problems == ["missing layout/config.json"]
    assert result.unreadable == []


def test_a_read_error_other_than_permission_is_also_a_named_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _models(tmp_path)
    mm.write(root)

    def failing(path: Path) -> str:
        raise OSError(5, "Input/output error", str(path))

    monkeypatch.setattr(mm, "_digest", failing)
    assert mm.verify(root).problems == [
        "unreadable layout/config.json",
        "unreadable layout/model.safetensors",
        "unreadable tableformer/fast/tm.safetensors",
    ]


@needs_a_normal_user
def test_production_refuses_with_one_clear_cause_when_a_model_file_is_unreadable(
    locked: tuple[Path, Path],
) -> None:
    root, _ = locked
    with pytest.raises(RuntimeError) as raised:
        _prod(root).validate_for_prod()
    message = str(raised.value)
    assert message.startswith("Refusing to start: ")
    assert "layout/config.json" in message
    assert "readable by the app user" in message and "chmod -R a+rX" in message
    assert "do not match their manifest" not in message, "one cause, and it is not a mismatch"
    assert str(root) not in message, "relative model paths only"


@needs_a_normal_user
def test_the_refusal_names_a_few_files_and_counts_the_rest(tmp_path: Path) -> None:
    root = _models(tmp_path)
    for i in range(10):
        (root / f"part{i}.bin").write_bytes(b"x")
    mm.write(root)
    locked_files = sorted(root.glob("part*.bin"))
    for f in locked_files:
        f.chmod(0)
    try:
        with pytest.raises(RuntimeError) as raised:
            _prod(root).validate_for_prod()
    finally:
        for f in locked_files:
            f.chmod(0o644)
    message = str(raised.value)
    assert "10 model file(s)" in message and "part0.bin" in message
    assert "part9.bin" not in message and "and 7 more" in message


@needs_a_normal_user
def test_the_command_prints_the_unreadable_files_and_the_fix(
    locked: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    root, _ = locked
    monkeypatch.setenv("PA_DOCLING_ARTIFACTS_PATH", str(root))
    monkeypatch.setenv("PA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PA_OLLAMA_BASE_URL", "http://127.0.0.1:1")
    get_settings.cache_clear()
    try:
        assert cli.main(["models-manifest", "verify"]) == 1
        out = capsys.readouterr().out
        assert "unreadable layout/config.json" in out
        assert "chmod -R a+rX" in out
        assert "2 file(s) checked, 1 problem(s)" in out
    finally:
        _reset_cli_state()


# --- writing a manifest for files the app user will not be able to read ---------------------------


@needs_a_normal_user
def test_write_refuses_clearly_when_it_cannot_read_a_file_itself(tmp_path: Path) -> None:
    root = _models(tmp_path)
    (root / "layout" / "config.json").chmod(0)
    try:
        with pytest.raises(ValueError, match=r"cannot read layout/config\.json"):
            mm.write(root)
    finally:
        (root / "layout" / "config.json").chmod(0o644)
    assert not (root / mm.MANIFEST_NAME).exists(), "no half-written manifest"


def test_files_other_users_cannot_read_are_listed(tmp_path: Path) -> None:
    root = _models(tmp_path)
    assert mm.not_world_readable(root) == []
    (root / "layout" / "config.json").chmod(0o600)  # what a huggingface cache file is
    (root / "tableformer").chmod(0o700)
    try:
        assert mm.not_world_readable(root) == ["layout/config.json", "tableformer"]
    finally:
        (root / "tableformer").chmod(0o755)
        (root / "layout" / "config.json").chmod(0o644)


def test_the_write_command_warns_about_files_the_app_user_cannot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _models(tmp_path)
    (root / "layout" / "config.json").chmod(0o600)
    monkeypatch.setenv("PA_DOCLING_ARTIFACTS_PATH", str(root))
    monkeypatch.setenv("PA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PA_OLLAMA_BASE_URL", "http://127.0.0.1:1")
    get_settings.cache_clear()
    try:
        assert cli.main(["models-manifest", "write"]) == 0, "a warning, not a failure"
        captured = capsys.readouterr()
        assert "3 file(s)" in captured.out
        assert "warning" in captured.err and "layout/config.json" in captured.err
        assert "chmod -R a+rX" in captured.err and "uid 999" in captured.err
        (root / "layout" / "config.json").chmod(0o644)
        assert cli.main(["models-manifest", "write"]) == 0
        assert "warning" not in capsys.readouterr().err
    finally:
        _reset_cli_state()


@needs_a_normal_user
def test_the_write_command_says_why_it_could_not_write_instead_of_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _models(tmp_path)
    (root / "layout" / "config.json").chmod(0)
    monkeypatch.setenv("PA_DOCLING_ARTIFACTS_PATH", str(root))
    monkeypatch.setenv("PA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PA_OLLAMA_BASE_URL", "http://127.0.0.1:1")
    get_settings.cache_clear()
    try:
        assert cli.main(["models-manifest", "write"]) == 1
        assert "cannot read layout/config.json" in capsys.readouterr().err
    finally:
        (root / "layout" / "config.json").chmod(0o644)
        _reset_cli_state()


def _reset_cli_state() -> None:
    import procurement_agent.db as dbmod

    dbmod._engine = None
    dbmod._SessionLocal = None
    get_settings.cache_clear()
