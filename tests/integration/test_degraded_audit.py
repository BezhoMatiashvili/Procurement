"""The audit row is lost: a report is not served unless the record that it was read is committed."""

from __future__ import annotations

import io
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from docx import Document as WordDocument
from fastapi.testclient import TestClient
from sqlalchemy import select

import procurement_agent.db as dbmod
from procurement_agent.config import Settings, get_settings
from procurement_agent.db import session_scope
from procurement_agent.llm.base import LLMUnavailableError
from procurement_agent.main import create_app
from procurement_agent.models import AuditLog, Job
from tests.boq_factory import LINES, blank, build_boq
from tests.degraded_support import (
    ACT_LINES,
    CONTRACT_LINES,
    KEY,
    XLSX,
    H,
    Plan,
    break_audit_commits,
    install_fake_convert,
    make_client,
    multipage_pdf,
    upload,
    wait_job,
    xlsx_bytes,
)

COMPARE_PAIRS = Path(__file__).resolve().parents[1] / "fixtures" / "compare"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

SUBJECT_CONTRACT = [
    *CONTRACT_LINES,
    "ხელშეკრულების საგანი: ვებგვერდის დიზაინის მომსახურების გაწევა (შემდგომში „მომსახურება“).",
]
SUBJECT_ACT = [
    *ACT_LINES,
    "რომლის საგანსაც წარმოადგენს საიტის ვიზუალის შექმნა (შემდგომში „მომსახურება“).",
]


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    yield from make_client(tmp_path)


def _with_model(client: TestClient, llm: object) -> None:
    client.app.state.llm = llm  # type: ignore[attr-defined]
    client.app.state.worker._llm = llm  # type: ignore[attr-defined]


def _down() -> LLMUnavailableError:
    return LLMUnavailableError("LLM service unreachable")


def _jobs(kind: str) -> list[Job]:
    with session_scope() as s:
        return list(s.scalars(select(Job).where(Job.kind == kind).order_by(Job.created_at)))


# --- finding 8: the audit row is committed before the report is served ----------------------------


@pytest.fixture
def register(monkeypatch: pytest.MonkeyPatch) -> Callable[[bytes, Plan], bytes]:
    return install_fake_convert(monkeypatch)


@pytest.fixture
def audit_commit(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> dict[str, bool]:
    return break_audit_commits(monkeypatch, request)


def _word(client: TestClient, name: str, lines: list[str]) -> str:
    doc = WordDocument()
    for line in lines:
        doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    return upload(client, name, buf.getvalue(), DOCX)[0]


def _audit_rows(action: str) -> int:
    with session_scope() as s:
        return len(list(s.scalars(select(AuditLog).where(AuditLog.action == action))))


class _Kit:
    """One of each document the report routes read."""

    def __init__(self, client: TestClient, register: Callable[[bytes, Plan], bytes]) -> None:
        self.client = client
        self.pdf, _ = upload(
            client,
            "p.pdf",
            register(multipage_pdf(1, "pg"), Plan(pages=1, text_on={1: "some text here"})),
        )
        self.act = _word(client, "act.docx", ACT_LINES)
        self.contract = _word(client, "contract.docx", CONTRACT_LINES)
        self.base, _ = upload(client, "base.docx", (COMPARE_PAIRS / "base.docx").read_bytes(), DOCX)
        self.amount, _ = upload(
            client, "amount.docx", (COMPARE_PAIRS / "amount.docx").read_bytes(), DOCX
        )
        self.template, _ = upload(
            client, "t.xlsx", build_boq(blank(), priced=False, stated_total=None), XLSX
        )
        self.bid, _ = upload(client, "b.xlsx", build_boq(LINES, stated_total=1000.0), XLSX)
        started = client.post(f"/api/v1/documents/{self.act}/extract", headers=H, json={}).json()
        self.extract_job = started["id"]
        wait_job(client, self.extract_job)
        with session_scope() as s:
            self.parse_job = (
                s.scalars(select(Job).where(Job.document_id == self.act, Job.kind == "parse"))
                .one()
                .id
            )


def _sites(k: _Kit) -> list[tuple[str, str, str, dict[str, Any] | None]]:
    """(label, method, url, json): every place a report or a read of a document is served."""
    aoa = {"aoa_document_id": k.act, "contract_document_id": k.contract}
    q = f"left={k.base}&right={k.amount}"
    return [
        ("api aoa", "POST", "/api/v1/checks/aoa", aoa),
        ("api aoa docx", "POST", "/api/v1/checks/aoa?format=docx", aoa),
        (
            "api compare",
            "POST",
            "/api/v1/checks/compare",
            {"left_document_id": k.base, "right_document_id": k.amount},
        ),
        (
            "api compare xlsx",
            "POST",
            "/api/v1/checks/compare?format=xlsx",
            {"left_document_id": k.base, "right_document_id": k.amount},
        ),
        (
            "api boq",
            "POST",
            "/api/v1/checks/boq",
            {"template_document_id": k.template, "bid_document_ids": [k.bid]},
        ),
        ("api result", "GET", f"/api/v1/jobs/{k.extract_job}/result", None),
        ("api page", "GET", f"/api/v1/documents/{k.pdf}/pages/1.png", None),
        ("ui aoa", "GET", f"/ui/checks/aoa?act={k.act}&contract={k.contract}", None),
        ("ui aoa docx", "GET", f"/ui/checks/aoa/annotated.docx?act={k.act}", None),
        ("ui compare", "GET", f"/ui/checks/compare?{q}", None),
        ("ui compare docx", "GET", f"/ui/checks/compare/annotated.docx?{q}", None),
        ("ui compare xlsx", "GET", f"/ui/checks/compare/differences.xlsx?{q}", None),
        ("ui boq", "GET", f"/ui/checks/boq?template={k.template}&bids={k.bid}", None),
        (
            "ui boq xlsx",
            "GET",
            f"/ui/checks/boq/merged.xlsx?template={k.template}&bids={k.bid}",
            None,
        ),
        ("ui result", "GET", f"/ui/jobs/{k.extract_job}", None),
        ("ui reading", "GET", f"/ui/jobs/{k.parse_job}", None),
    ]


#: What a request that serves a report or reads a document records. Each is written with
#: ``audit_committed`` - the row is stored before the body goes out - never with ``audit``,
#: whose row is committed after the response has been built.
READS = re.compile(
    r"check\.(aoa|compare|boq)\.(json|docx|pdf|xlsx|\{fmt\})|result\.read\.|document\.page_view"
    r"|document\.content_view"
)


def test_no_route_that_serves_a_read_records_it_with_the_late_commit() -> None:
    """Fails when a route that serves a report or a read of a document is written, or changed,
    to use ``audit`` (committed when the request ends) instead of ``audit_committed``."""
    import ast

    src = Path(__file__).resolve().parents[2] / "src" / "procurement_agent"
    late: list[str] = []
    for path in [*sorted((src / "api").glob("*.py")), src / "ui" / "routes.py"]:
        for node in ast.walk(ast.parse(path.read_text())):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "audit"
            ):
                continue
            action = next((k.value for k in node.keywords if k.arg == "action"), None)
            text = ast.unparse(action).strip("f'\"") if action is not None else ""
            if READS.match(text):
                late.append(f"{path.name}:{node.lineno} {text}")
    assert late == []


def test_every_report_is_served_only_after_its_audit_row_is_committed(
    client: TestClient, audit_commit: dict[str, bool], register: Callable[[bytes, Plan], bytes]
) -> None:
    kit = _Kit(client, register)
    failures: list[str] = []
    for label, method, url, body in _sites(kit):
        audit_commit["on"] = True
        r = client.request(method, url, headers=H, json=body)
        audit_commit["on"] = False
        if r.status_code != 503:
            failures.append(f"{label}: {r.status_code}")
            continue
        if url.startswith("/api/"):
            detail = r.json()["detail"]
            if detail["code"] != "AUDIT_UNAVAILABLE":
                failures.append(f"{label}: {detail}")
            if "message_en" not in detail or "message_ka" not in detail:
                failures.append(f"{label}: not bilingual")
        # nothing of the report in the refusal
        if b"findings" in r.content or r.content[:2] == b"PK":
            failures.append(f"{label}: body leaked")
    assert failures == []


def test_the_same_requests_succeed_and_are_recorded_when_the_database_is_fine(
    client: TestClient, audit_commit: dict[str, bool], register: Callable[[bytes, Plan], bytes]
) -> None:
    kit = _Kit(client, register)
    before = _audit_rows("check.aoa.json")
    r = client.post(
        "/api/v1/checks/aoa",
        headers=H,
        json={"aoa_document_id": kit.act, "contract_document_id": kit.contract},
    )
    assert r.status_code == 200
    assert _audit_rows("check.aoa.json") == before + 1


def test_a_lost_audit_row_is_said_in_the_log_content_free(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    settings = Settings(
        env="dev",
        data_dir=tmp_path / "data",
        dev_api_key=KEY,
        ollama_base_url="http://127.0.0.1:1",
        rate_limit="100000/minute",
    )
    get_settings.cache_clear()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    state = break_audit_commits(monkeypatch, request)
    try:
        with TestClient(app) as c:
            act, _ = upload(c, "act.xlsx", xlsx_bytes(ACT_LINES), XLSX)
            capsys.readouterr()
            state["on"] = True
            r = c.post("/api/v1/checks/aoa", headers=H, json={"aoa_document_id": act})
            state["on"] = False
            out = capsys.readouterr().out
    finally:
        dbmod._engine = None
        dbmod._SessionLocal = None
        get_settings.cache_clear()
    assert r.status_code == 503
    plain = re.sub(r"\x1b\[[0-9;]*m", "", out)  # the dev console renderer colours its lines
    failed = [x for x in plain.splitlines() if "audit_commit_failed" in x]
    assert failed, "the lost row is said in the log"
    assert "error" in failed[0].split("audit_commit_failed")[0]  # at error level
    assert "check.aoa.json" in failed[0]
    assert "locked" not in failed[0]  # the kind of failure, never its message
    # no line claims the read was recorded
    assert not [x for x in plain.splitlines() if "audit " in x and "check.aoa.json" in x]
