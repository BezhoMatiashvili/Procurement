"""The audit trail: what is recorded, that it cannot be rewritten, and how an operator reads it."""

from __future__ import annotations

import io
import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from docx import Document as WordDocument
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError

import procurement_agent.db as dbmod
from procurement_agent import cli
from procurement_agent.config import Settings, get_settings
from procurement_agent.db import prune_audit, session_scope
from procurement_agent.main import create_app
from procurement_agent.models import AuditLog
from procurement_agent.security.audit import audit_denied_access
from procurement_agent.security.auth import create_api_key

SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        env="dev",
        data_dir=tmp_path / "data",
        ollama_base_url="http://127.0.0.1:1",
        rate_limit="10000/minute",
    )
    get_settings.cache_clear()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as c:
        yield c
    dbmod._engine = None
    dbmod._SessionLocal = None
    get_settings.cache_clear()


def _key(user: str) -> dict[str, str]:
    with session_scope() as s:
        key, _ = create_api_key(s, user)
    return {"X-API-Key": key}


def _docx() -> bytes:
    doc = WordDocument()
    doc.add_paragraph("SUPPLY CONTRACT No. 17/2024. The total price is 120,000.00 GEL.")
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _upload(client: TestClient, headers: dict[str, str]) -> str:
    r = client.post("/api/v1/documents", headers=headers, files={"file": ("a.docx", _docx())})
    assert r.status_code == 201
    job = r.json()["parse_job_id"]
    for _ in range(600):
        if client.get(f"/api/v1/jobs/{job}", headers=headers).json()["status"] in (
            "done",
            "failed",
        ):
            break
        time.sleep(0.05)
    return str(r.json()["document"]["id"])


def _rows(action: str) -> list[AuditLog]:
    with session_scope() as s:
        rows = list(
            s.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id))
        )
        s.expunge_all()
    return rows


# --- the trail cannot be rewritten -------------------------------------------------------------


def test_the_application_can_still_add_rows(client: TestClient) -> None:
    with session_scope() as s:
        s.add(AuditLog(actor="a", action="x.y", outcome="ok"))
    assert [r.action for r in _rows("x.y")] == ["x.y"]


def test_a_row_cannot_be_changed(client: TestClient) -> None:
    with session_scope() as s:
        s.add(AuditLog(actor="a", action="x.y", outcome="ok", detail="original"))
    with pytest.raises(IntegrityError, match="append-only"), session_scope() as s:
        s.execute(update(AuditLog).where(AuditLog.action == "x.y").values(detail="rewritten"))
    assert _rows("x.y")[0].detail == "original"


def test_a_row_cannot_be_deleted(client: TestClient) -> None:
    with session_scope() as s:
        s.add(AuditLog(actor="a", action="x.y", outcome="ok"))
    with pytest.raises(IntegrityError, match="append-only"), session_scope() as s:
        s.execute(delete(AuditLog))
    with pytest.raises(IntegrityError, match="append-only"), session_scope() as s:
        s.execute(text("DELETE FROM audit_log WHERE action = 'x.y'"))
    assert len(_rows("x.y")) == 1


def test_the_triggers_are_there_after_every_start(tmp_path: Path) -> None:
    for _ in range(2):  # init_db twice: idempotent
        settings = Settings(
            env="dev", data_dir=tmp_path / "data", ollama_base_url="http://127.0.0.1:1"
        )
        settings.ensure_dirs()
        dbmod.init_db(settings.db_path)
        with session_scope() as s:
            names = set(
                s.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'")).scalars()
            )
        assert {"audit_log_no_update", "audit_log_no_delete"} <= names
    dbmod._engine = None
    dbmod._SessionLocal = None


def test_the_sanctioned_prune_removes_old_rows_only_and_restores_the_trigger(
    client: TestClient,
) -> None:
    now = datetime.now(UTC)
    with session_scope() as s:
        s.add(AuditLog(actor="a", action="old.one", outcome="ok", at=now - timedelta(days=400)))
        s.add(AuditLog(actor="a", action="old.two", outcome="ok", at=now - timedelta(days=366)))
        s.add(AuditLog(actor="a", action="new.one", outcome="ok", at=now - timedelta(days=3)))
    assert prune_audit(now - timedelta(days=365)) == 2
    assert _rows("old.one") == [] and _rows("old.two") == []
    assert len(_rows("new.one")) == 1
    with pytest.raises(IntegrityError, match="append-only"), session_scope() as s:
        s.execute(delete(AuditLog))  # the trigger is back


def test_a_failed_prune_leaves_the_trail_and_the_trigger_alone(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    with session_scope() as s:
        s.add(
            AuditLog(
                actor="a",
                action="old.one",
                outcome="ok",
                at=datetime.now(UTC) - timedelta(days=900),
            )
        )
    original = dbmod._AUDIT_TRIGGERS["audit_log_no_delete"]
    monkeypatch.setitem(dbmod._AUDIT_TRIGGERS, "audit_log_no_delete", "CREATE TRIGGER nonsense (((")
    with pytest.raises(Exception):  # noqa: B017 - any database error
        prune_audit(datetime.now(UTC))
    monkeypatch.setitem(dbmod._AUDIT_TRIGGERS, "audit_log_no_delete", original)
    assert len(_rows("old.one")) == 1  # rolled back: nothing was deleted
    with pytest.raises(IntegrityError, match="append-only"), session_scope() as s:
        s.execute(delete(AuditLog))  # and the trigger was never lost


# --- what is recorded --------------------------------------------------------------------------


def test_asking_for_a_document_that_is_not_yours_is_recorded_against_the_asker(
    client: TestClient,
) -> None:
    alice, mallory = _key("alice"), _key("mallory")
    doc = _upload(client, alice)
    assert client.get(f"/api/v1/documents/{doc}", headers=mallory).status_code == 404
    assert client.get(f"/api/v1/documents/{'0' * 32}", headers=mallory).status_code == 404
    assert client.get(f"/api/v1/documents/{doc}", headers=alice).status_code == 200
    rows = _rows("access.denied")
    assert [(r.actor, r.outcome, r.detail) for r in rows] == [
        ("mallory", "denied", f"document {doc}"),
        ("mallory", "denied", f"document {'0' * 32}"),
    ]
    assert all(r.ip == "testclient" for r in rows)  # the address of the caller, from the request


def test_jobs_are_recorded_the_same_way(client: TestClient) -> None:
    alice, mallory = _key("alice"), _key("mallory")
    doc = _upload(client, alice)
    job = client.post(f"/api/v1/documents/{doc}/extract", headers=alice, json={}).json()["id"]
    assert client.get(f"/api/v1/jobs/{job}", headers=mallory).status_code == 404
    assert [(r.actor, r.detail) for r in _rows("access.denied")] == [("mallory", f"job {job}")]


def test_what_the_caller_typed_is_not_recorded_as_if_it_were_an_id(client: TestClient) -> None:
    headers = _key("mallory")
    hostile = "a;b c%20d" + "A" * 300  # no dot-segments: those are folded away before routing
    client.get(f"/api/v1/documents/{hostile}", headers=headers)
    client.get("/api/v1/documents/ჯანარეიკა", headers=headers)
    assert [r.detail for r in _rows("access.denied")] == ["document malformed id"] * 2


def test_a_failure_to_record_a_refusal_does_not_become_a_server_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from procurement_agent.security import audit as audit_mod

    def broken(**_: Any) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(audit_mod, "audit_now", broken)
    assert client.get(f"/api/v1/documents/{'1' * 32}", headers=_key("mallory")).status_code == 404
    audit_denied_access("x", "document", "abc")  # no exception either


def test_reading_extracted_values_in_the_browser_is_recorded_like_through_the_api(
    client: TestClient,
) -> None:
    headers = _key("alice")
    doc = _upload(client, headers)
    job = client.post(f"/api/v1/documents/{doc}/extract", headers=headers, json={}).json()["id"]
    for _ in range(600):
        if client.get(f"/api/v1/jobs/{job}", headers=headers).json()["status"] == "done":
            break
        time.sleep(0.05)
    client.get(f"/api/v1/jobs/{job}/result", headers=headers)  # through the API
    key = headers["X-API-Key"]
    client.post("/ui/login", data={"api_key": key}, headers=SAME_ORIGIN, follow_redirects=False)
    assert client.get(f"/ui/jobs/{job}").status_code == 200  # through the browser interface
    assert [r.actor for r in _rows("result.read.json")] == ["alice"]
    ui = _rows("result.read.ui")
    assert [(r.actor, r.document_id, r.job_id) for r in ui] == [("alice", doc, job)]


def test_a_cross_site_language_post_is_recorded(client: TestClient) -> None:
    r = client.post("/ui/lang", data={"lang": "ka"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    rows = _rows("auth.csrf")
    assert [(r.actor, r.detail) for r in rows] == [("anonymous", "POST /ui/lang")]
    ok = client.post("/ui/lang", data={"lang": "ka"}, headers=SAME_ORIGIN, follow_redirects=False)
    assert ok.status_code == 303
    assert len(_rows("auth.csrf")) == 1  # a same-origin post is not an event


def test_a_run_of_rate_limited_requests_is_logged_once_not_each(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(
        env="dev",
        data_dir=tmp_path / "data",
        ollama_base_url="http://127.0.0.1:1",
        rate_limit="3/minute",
    )
    get_settings.cache_clear()
    app = create_app(settings)
    try:
        with TestClient(app) as c:
            statuses = [c.get("/ui/login").status_code for _ in range(12)]
            out = capsys.readouterr().out
    finally:
        dbmod._engine = None
        dbmod._SessionLocal = None
        get_settings.cache_clear()
    assert statuses[:3] == [200, 200, 200] and set(statuses[3:]) == {429}
    assert out.count("rate_limited") == 1
    assert "ip:testclient" in out


# --- the operator's view -----------------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("PA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PA_OLLAMA_BASE_URL", "http://127.0.0.1:1")
    get_settings.cache_clear()
    yield tmp_path
    dbmod._engine = None
    dbmod._SessionLocal = None
    get_settings.cache_clear()


def test_creating_and_revoking_a_key_are_recorded(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["create-key", "alice"]) == 0
    key_id = capsys.readouterr().out.split("id=")[1].split()[0]
    assert cli.main(["revoke-key", key_id]) == 0
    assert cli.main(["revoke-key", key_id]) == 1  # already revoked
    assert cli.main(["revoke-key", "x" * 80]) == 1
    rows = [(r.actor, r.action, r.outcome) for r in _all()]
    assert ("cli", "key.create", "ok") in rows
    assert rows.count(("cli", "key.revoke", "ok")) == 1
    assert rows.count(("cli", "key.revoke", "denied")) == 2
    detail = next(r.detail for r in _all() if r.action == "key.create")
    assert detail == f"user alice id {key_id}"


def _all() -> list[AuditLog]:
    with session_scope() as s:
        rows = list(s.scalars(select(AuditLog).order_by(AuditLog.id)))
        s.expunge_all()
    return rows


def test_the_audit_command_lists_and_filters(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["create-key", "alice"])
    cli.main(["create-key", "bob"])
    audit_denied_access("mallory", "document", "a" * 32)
    capsys.readouterr()
    assert cli.main(["audit"]) == 0
    listing = capsys.readouterr().out
    assert listing.count("key.create") == 2 and "access.denied" in listing and "mallory" in listing
    assert cli.main(["audit", "--action", "access.denied", "--json"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert [(r["actor"], r["detail"]) for r in lines] == [("mallory", f"document {'a' * 32}")]
    assert cli.main(["audit", "--actor", "cli", "--last", "1", "--json"]) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 1


def test_audit_prune_is_a_dry_run_until_told_otherwise(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["create-key", "alice"])
    with session_scope() as s:
        s.add(
            AuditLog(
                actor="a",
                action="ancient",
                outcome="ok",
                at=datetime.now(UTC) - timedelta(days=500),
            )
        )
    capsys.readouterr()
    assert cli.main(["audit-prune", "--older-than-days", "365"]) == 0
    assert "dry run: 1 audit row(s)" in capsys.readouterr().out
    assert len(_rows("ancient")) == 1
    assert cli.main(["audit-prune", "--older-than-days", "365", "--yes"]) == 0
    assert "deleted 1 audit row(s)" in capsys.readouterr().out
    assert _rows("ancient") == []
    prune = _rows("audit.prune")
    assert [(r.actor, r.detail) for r in prune] == [("cli", "1 row(s) older than 365 day(s)")]
    assert cli.main(["audit-prune", "--older-than-days", "0"]) == 2
