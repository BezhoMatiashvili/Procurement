"""``python -m procurement_agent.parser_service``: the start-up a parser container performs, and
the health probe the container runs. The service itself is faked: what is asserted is the order and
the arguments - that the checks run before anything is loaded, that the process is hardened, that
the egress guard is up before Docling is imported, and that the deadline handed to ``serve`` is the
one the settings imply (``protocol.kill_deadline_s``)."""

from __future__ import annotations

import runpy
import signal
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from procurement_agent.config import Settings
from procurement_agent.parser_service import cli, docling_converter, server
from procurement_agent.parser_service import protocol as p
from procurement_agent.security import egress


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in list(__import__("os").environ):
        if name.startswith("PA_"):
            monkeypatch.delenv(name)
    sock = tmp_path / "p.sock"
    monkeypatch.setenv("PA_PARSER_SOCKET", str(sock))
    monkeypatch.setenv("PA_DOCLING_TIMEOUT_S", "100")
    monkeypatch.setenv("PA_PARSER_KILL_GRACE_S", "20")
    monkeypatch.setenv("PA_DOCLING_NUM_THREADS", "3")
    return sock


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    record: dict[str, Any] = {"order": []}

    def fake_serve(**kwargs: Any) -> None:
        record["order"].append("serve")
        record["serve"] = kwargs

    def fake_load(settings: Settings) -> server.Converter:
        record["order"].append("load")
        record["loaded_with"] = settings
        return lambda path, kind: server.Converted(1, "success", b"{}")

    def fake_harden() -> Any:
        record["order"].append("harden")
        return lambda: record["order"].append("restore")

    monkeypatch.setattr(server, "serve", fake_serve)
    monkeypatch.setattr(docling_converter, "load", fake_load)
    monkeypatch.setattr(cli, "harden_process", fake_harden)
    monkeypatch.setattr(
        server.KillSwitch, "install", lambda self: record["order"].append("install")
    )
    return record


def test_the_parser_starts_hardened_and_serves_with_the_deadline_the_settings_imply(
    env: Path, calls: dict[str, Any]
) -> None:
    assert cli.main([]) == 0
    assert calls["order"] == ["harden", "install", "load", "serve", "restore"]
    kwargs = calls["serve"]
    assert kwargs["socket_path"] == env
    limits = kwargs["limits"]
    assert limits.deadline_s == p.kill_deadline_s(100, 20) == 120
    assert limits.cpu_threads == 3
    assert isinstance(kwargs["guard"], server.KillSwitch)
    assert callable(kwargs["convert_fn"])


def test_serve_is_never_given_an_unarmed_deadline(env: Path, calls: dict[str, Any]) -> None:
    cli.main([])
    assert calls["serve"]["limits"].deadline_s is not None


def test_the_egress_guard_is_up_before_docling_is_loaded(
    env: Path, calls: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    installed: list[str] = []
    real = egress.ensure_installed

    def spy(*args: Any, **kwargs: Any) -> Any:
        installed.append("guard")
        return real(*args, **kwargs)

    monkeypatch.setattr(egress, "ensure_installed", spy)
    monkeypatch.setattr(
        docling_converter,
        "load",
        lambda s: (
            installed.append("load"),
            lambda path, kind: server.Converted(1, "success", b""),
        )[1],
    )
    cli.main([])
    assert installed == ["guard", "load"]


def test_production_start_up_checks_come_before_anything_is_loaded(
    env: Path, calls: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PA_ENV", "prod")  # no models path: the parser refuses to start
    with pytest.raises(RuntimeError, match="PA_DOCLING_ARTIFACTS_PATH"):
        cli.main([])
    assert calls["order"] == []


def test_the_process_is_restored_when_serving_ends_with_an_error(
    env: Path, calls: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(**kwargs: Any) -> None:
        raise OSError("socket")

    monkeypatch.setattr(server, "serve", failing)
    with pytest.raises(OSError, match="socket"):
        cli.main([])
    assert calls["order"][-1] == "restore"


def test_a_blocked_connection_is_logged_without_content(
    env: Path, calls: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    real = egress.ensure_installed

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(egress, "ensure_installed", spy)
    cli.main([])
    assert callable(seen["on_block"])
    seen["on_block"]("connect", "203.0.113.9")  # logs; raises nothing


def test_the_guard_is_not_installed_when_the_setting_is_off_in_development(
    env: Path, calls: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PA_EGRESS_GUARD", "false")
    spy: list[int] = []
    monkeypatch.setattr(egress, "ensure_installed", lambda *a, **k: spy.append(1))
    cli.main([])
    assert spy == []


# --- the health probe ------------------------------------------------------------------------


@pytest.mark.parametrize(("alive", "code"), [(True, 0), (False, 1)])
def test_the_ping_exits_zero_for_a_live_parser_and_one_otherwise(
    env: Path, monkeypatch: pytest.MonkeyPatch, alive: bool, code: int
) -> None:
    asked: list[Path] = []
    monkeypatch.setattr(server, "ping", lambda path, timeout_s: asked.append(path) or alive)
    assert cli.main(["--ping"]) == code
    assert asked == [env]
    guard = egress.installed()
    assert guard is not None and str(env) in guard.unix_paths, "the probe's one allowed address"


def test_the_ping_does_not_load_docling_or_the_models(
    env: Path, calls: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "ping", lambda path, timeout_s: True)
    cli.main(["--ping"])
    assert calls["order"] == []


def test_running_the_package_runs_main(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "main", lambda argv=None: 7)
    with pytest.raises(SystemExit) as stop:
        runpy.run_module("procurement_agent.parser_service", run_name="__main__")
    assert stop.value.code == 7


def test_importing_the_package_for_the_probe_does_not_import_docling() -> None:
    import subprocess
    import sys

    script = (
        "import sys\n"
        "import procurement_agent.parser_service.cli\n"
        "bad = [m for m in ('docling', 'torch', 'docling_core') if m in sys.modules]\n"
        "print(','.join(bad))\n"
    )
    import os

    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": f"{root / 'src'}"}
    done = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-500:]
    assert done.stdout.strip() == ""


# --- the converter the parser serves ---------------------------------------------------------


class FakeDocument:
    def __init__(self, pages: int, body: str) -> None:
        self.pages = {n: object() for n in range(1, pages + 1)}
        self._body = body

    def save_as_json(self, filename: Path, **_: object) -> None:
        Path(filename).write_text(self._body, encoding="utf-8")


def test_the_converter_loads_both_pipelines_before_it_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from docling.datamodel.base_models import InputFormat

    from procurement_agent.ingestion import docling_service

    initialised: list[Any] = []
    fake = SimpleNamespace(initialize_pipeline=initialised.append)
    monkeypatch.setattr(docling_service, "get_converter", lambda s: fake)
    docling_converter.load(Settings())
    assert initialised == [InputFormat.PDF, InputFormat.DOCX]


def test_the_converter_returns_what_save_document_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from procurement_agent.ingestion import docling_service

    doc = FakeDocument(3, '{"schema_name": "DoclingDocument", "texts": ["ბარამიძე"]}')
    result = SimpleNamespace(document=doc, status=SimpleNamespace(value="partial_success"))
    seen: list[tuple[Path, Settings]] = []

    def fake_convert(path: Path, settings: Settings) -> Any:
        seen.append((path, settings))
        return result

    monkeypatch.setattr(
        docling_service,
        "get_converter",
        lambda s: SimpleNamespace(initialize_pipeline=lambda f: None),
    )
    monkeypatch.setattr(docling_service, "convert", fake_convert)
    settings = Settings()
    convert = docling_converter.load(settings)
    source = tmp_path / "abc.pdf"
    source.write_bytes(b"%PDF")
    converted = convert(source, "pdf")
    assert seen == [(source, settings)]
    assert converted == server.Converted(
        pages=3,
        status="partial_success",
        document='{"schema_name": "DoclingDocument", "texts": ["ბარამიძე"]}'.encode(),
    )
    assert not list(tmp_path.glob("**/docling.json")) or all(
        tmp_path in f.parents for f in tmp_path.glob("**/docling.json")
    )


def test_a_failure_in_the_converter_propagates_as_the_services_to_turn_into_a_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from procurement_agent.ingestion import docling_service

    def failing(path: Path, settings: Settings) -> Any:
        raise docling_service.ConversionError("conversion failed: failure (0 error(s))")

    monkeypatch.setattr(
        docling_service,
        "get_converter",
        lambda s: SimpleNamespace(initialize_pipeline=lambda f: None),
    )
    monkeypatch.setattr(docling_service, "convert", failing)
    convert = docling_converter.load(Settings())
    with pytest.raises(docling_service.ConversionError):
        convert(tmp_path / "x.pdf", "pdf")


def test_nothing_in_the_test_changed_the_alarm_signal() -> None:
    assert signal.getsignal(signal.SIGALRM) == signal.SIG_DFL
    assert threading.current_thread() is threading.main_thread()
