"""The network policy through the real application: who may reach it, and where it may reach."""

from __future__ import annotations

import socket
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import procurement_agent.db as dbmod
from procurement_agent.config import Settings, get_settings
from procurement_agent.db import session_scope
from procurement_agent.main import create_app
from procurement_agent.models import AuditLog
from procurement_agent.security import audit as audit_mod
from procurement_agent.security import egress
from procurement_agent.security.egress import EgressBlockedError

KEY = "pa_devkey_for_tests_only"


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "env": "dev",
        "data_dir": tmp_path / "data",
        "dev_api_key": KEY,
        "ollama_base_url": "http://127.0.0.1:1",
        "rate_limit": "10000/minute",
    }
    base.update(overrides)
    return Settings(**base)


def _client(tmp_path: Path, **overrides: Any) -> Iterator[TestClient]:
    settings = _settings(tmp_path, **overrides)
    get_settings.cache_clear()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as c:
        yield c
    dbmod._engine = None
    dbmod._SessionLocal = None
    get_settings.cache_clear()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    yield from _client(tmp_path)


# --- who may reach the server --------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/health", "/ready", "/ui/login", "/static/app.js", "/nonexistent"]
)
def test_a_foreign_host_is_refused_on_every_kind_of_path(client: TestClient, path: str) -> None:
    ok = client.get(path)
    assert ok.status_code != 400  # the same path with the test client's own host is served
    r = client.get(path, headers={"host": "attacker.example"})
    assert r.status_code == 400
    assert r.text == "Invalid host header"


def test_the_names_this_machine_is_reached_by_are_served(client: TestClient) -> None:
    for host in ("127.0.0.1:8000", "localhost:8000", "[::1]:8000", "testserver"):
        assert client.get("/health", headers={"host": host}).status_code == 200, host


def test_a_listed_name_is_served_and_the_test_alias_is_not_in_production_style_lists(
    tmp_path: Path,
) -> None:
    for c in _client(tmp_path, allowed_hosts=("tasks.company.example",)):
        assert c.get("/health", headers={"host": "tasks.company.example"}).status_code == 200
        assert c.get("/health", headers={"host": "other.company.example"}).status_code == 400


def test_the_post_of_a_foreign_host_never_reaches_a_handler(client: TestClient) -> None:
    r = client.post(
        "/api/v1/documents",
        headers={"host": "attacker.example", "X-API-Key": KEY},
        files={"file": ("a.pdf", b"%PDF-1.4")},
    )
    assert r.status_code == 400


def test_the_interactive_docs_page_that_loads_a_cdn_is_gone(client: TestClient) -> None:
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert (
        client.get("/openapi.json").status_code == 200
    )  # the schema itself is local and useful in dev


def test_no_response_names_an_external_origin(client: TestClient) -> None:
    for path in ("/ui/login", "/static/app.js", "/openapi.json"):
        body = client.get(path).text
        for needle in ("cdn.jsdelivr", "unpkg.com", "googleapis", "cdnjs", "fonts.gstatic"):
            assert needle not in body, (path, needle)


# --- where the server may reach ------------------------------------------------------------


def test_the_application_installs_the_egress_guard_with_its_model_server_allowed(
    tmp_path: Path,
) -> None:
    for _ in _client(tmp_path, ollama_base_url="http://localhost:11434"):
        guard = egress.installed()
        assert guard is not None
        guard.check_name("localhost")
        with pytest.raises(EgressBlockedError):
            guard.check_name("huggingface.co")


def _audited(kind_and_destination: str, timeout: float = 5.0) -> list[AuditLog]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with session_scope() as s:
            rows = list(
                s.scalars(
                    select(AuditLog).where(
                        AuditLog.action == "egress.blocked",
                        AuditLog.detail == kind_and_destination,
                    )
                )
            )
            s.expunge_all()
        if rows:
            return rows
        time.sleep(0.05)
    return []


def test_a_blocked_lookup_and_a_blocked_connect_are_audited_without_content(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(audit_mod, "_egress_seen", {})
    monkeypatch.setattr(audit_mod, "_egress_window", [0.0, 0])
    with pytest.raises(EgressBlockedError):
        socket.create_connection(("203.0.113.77", 443), timeout=1)  # refused at the lookup
    with socket.socket() as raw, pytest.raises(EgressBlockedError):
        raw.connect(("203.0.113.78", 443))  # and a socket that skips the lookup, at connect
    (lookup,) = _audited("lookup 203.0.113.77")
    (connect,) = _audited("connect 203.0.113.78")
    assert (lookup.actor, lookup.outcome) == ("system", "denied")
    assert (connect.actor, connect.outcome) == ("system", "denied")


def test_the_guard_can_be_switched_off_in_development_only(tmp_path: Path) -> None:
    Settings(env="dev", egress_guard=False).validate_for_prod()
    with pytest.raises(RuntimeError, match="PA_EGRESS_GUARD"):
        Settings(
            env="prod",
            egress_guard=False,
            docling_artifacts_path=tmp_path,
            allowed_hosts=("a.example",),
            models_manifest_required=False,
        ).validate_for_prod()
