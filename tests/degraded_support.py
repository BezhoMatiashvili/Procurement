"""Helpers for the tests of degraded input: documents that were not (fully) read.

Everything here is invented. A conversion is replaced by a plan - how many pages, which of them
carry text, whether Docling called it a partial success - so a test can say "an 80-page file of
which 4 pages were read" without 80 pages of real parsing; the real parser has its own tests
(``tests/integration/test_degraded_docling.py``).
"""

from __future__ import annotations

import hashlib
import io
import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
from docling.datamodel.base_models import ConversionStatus
from docling_core.types.doc.base import BoundingBox, Size
from docling_core.types.doc.document import DoclingDocument, ProvenanceItem
from docling_core.types.doc.labels import DocItemLabel
from fastapi.testclient import TestClient
from openpyxl import Workbook

import procurement_agent.db as dbmod
from procurement_agent.config import Settings, get_settings
from procurement_agent.ingestion import docling_service
from procurement_agent.main import create_app

KEY = "pa_devkey_for_tests_only"
H = {"X-API-Key": KEY}
#: A real browser always sends this on a form post; the CSRF guard fails closed without it.
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF = "application/pdf"

ACT_LINES = [
    "მიღება-ჩაბარების აქტი",
    "შპს „ალფა ჯგუფი“. საიდენტიფიკაციო კოდი: 123456789; იურიდიული მისამართი: ქ. თბილისი",
    "მომსახურება შესრულდა 2026 წლის 1 აპრილიდან 2026 წლის 30 აპრილის ჩათვლით.",
    "საფასური შეადგენს 100 (ასი) ლარს.",
]
CONTRACT_LINES = [
    "ხელშეკრულება",
    "შპს „ალფა ჯგუფი“. საიდენტიფიკაციო კოდი: 123456789; იურიდიული მისამართი: ქ. თბილისი",
    "შეასრულოს მომსახურება 2026 წლის 1 მარტიდან - 2026 წლის 1 სექტემბრის ჩათვლით.",
    "ღირებულება შეადგენს 100 (ასი) ლარს.",
]


def multipage_pdf(pages: int, tag: str = "") -> bytes:
    """A blank PDF of ``pages`` pages with a correct cross-reference table. ``tag`` makes two
    files with the same page count different files."""
    kids = " ".join(f"{3 + i} 0 R" for i in range(pages))
    bodies = [
        "<</Type/Catalog/Pages 2 0 R>>",
        f"<</Type/Pages/Kids[{kids}]/Count {pages}>>",
        *("<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>" for _ in range(pages)),
    ]
    out = bytearray(b"%PDF-1.4\n" + (f"%{tag}\n".encode() if tag else b""))
    offsets = []
    for number, body in enumerate(bodies, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj{body}endobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(bodies) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer<</Size {len(bodies) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def xlsx_bytes(lines: list[str]) -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    for line in lines:
        ws.append([line])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def empty_xlsx() -> bytes:
    """A workbook with sheets and no cell text at all."""
    wb = Workbook()
    wb.create_sheet("Second")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@dataclass
class Plan:
    """What a faked conversion of one PDF yields."""

    pages: int = 1
    text_on: dict[int, str] = field(default_factory=dict)
    status: ConversionStatus = ConversionStatus.SUCCESS
    errors: int = 0


def plan_document(plan: Plan) -> DoclingDocument:
    doc = DoclingDocument(name="synthetic")
    for n in range(1, plan.pages + 1):
        doc.add_page(page_no=n, size=Size(width=200, height=200))
    box = BoundingBox(l=0, t=0, r=10, b=10)
    for n, text in plan.text_on.items():
        doc.add_text(
            label=DocItemLabel.TEXT,
            text=text,
            prov=ProvenanceItem(page_no=n, bbox=box, charspan=(0, len(text))),
        )
    return doc


def install_fake_convert(
    monkeypatch: pytest.MonkeyPatch, *, ocr: bool | None = None
) -> Callable[[bytes, Plan], bytes]:
    """Replace the conversion of PDFs by plans. Returns ``register(pdf_bytes, plan)``, which
    gives the bytes back for uploading. Files nobody planned (Word documents) convert for real.

    ``ocr`` pins what the host says about Tesseract; None leaves it alone."""
    plans: dict[str, Plan] = {}
    real = docling_service.convert

    def convert(path: Path, settings: Settings) -> object:
        plan = plans.get(hashlib.sha256(path.read_bytes()).hexdigest())
        if plan is None:
            return real(path, settings)
        return SimpleNamespace(
            document=plan_document(plan),
            status=plan.status,
            errors=[SimpleNamespace(component_type=SimpleNamespace(value="x"), module_name="y")]
            * plan.errors,
        )

    monkeypatch.setattr(docling_service, "convert", convert)
    if ocr is not None:
        monkeypatch.setattr(docling_service, "ocr_available", lambda settings: ocr)

    def register(data: bytes, plan: Plan) -> bytes:
        plans[hashlib.sha256(data).hexdigest()] = plan
        return data

    return register


def make_client(tmp_path: Path, **overrides: object) -> Iterator[TestClient]:
    """The signed-in client of the whole app (API key header and UI cookie both work)."""
    settings = Settings(
        env="dev",
        data_dir=tmp_path / "data",
        dev_api_key=KEY,
        ollama_base_url="http://127.0.0.1:1",
        retention_days=1,
        rate_limit="100000/minute",
        **overrides,  # type: ignore[arg-type]
    )
    get_settings.cache_clear()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as c:
        c.post("/ui/login", data={"api_key": KEY}, headers=SAME_ORIGIN)
        yield c
    dbmod._engine = None
    dbmod._SessionLocal = None
    get_settings.cache_clear()


def settings_of(client: TestClient) -> Settings:
    s: Settings = client.app.state.settings  # type: ignore[attr-defined]
    return s


def wait_job(client: TestClient, job_id: str, timeout: float = 30.0) -> dict[str, object]:
    deadline = time.time() + timeout
    job: dict[str, object] = {}
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}", headers=H).json()
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    return job


def upload(client: TestClient, name: str, data: bytes, media: str = PDF) -> tuple[str, str]:
    """Upload, wait for the parse job to settle, return ``(document_id, parse_job_id)``."""
    r = client.post("/api/v1/documents", headers=H, files={"file": (name, data, media)})
    assert r.status_code == 201, r.text
    body = r.json()
    wait_job(client, body["parse_job_id"])
    return str(body["document"]["id"]), str(body["parse_job_id"])


def document(client: TestClient, doc_id: str) -> dict[str, object]:
    out: dict[str, object] = client.get(f"/api/v1/documents/{doc_id}", headers=H).json()
    return out


def make_legacy(client: TestClient, doc_id: str, kind: str, *, empty: bool = True) -> None:
    """Turn a parsed document into one stored before read quality existed: no sidecar, and
    (``empty``) a parse that holds no text at all."""
    s = settings_of(client)
    out = s.results_dir / doc_id
    (out / "read_quality.json").unlink(missing_ok=True)
    if not empty:
        return
    if kind == "xlsx":
        (s.upload_dir / f"{doc_id}.xlsx").write_bytes(empty_xlsx())
    else:
        docling_service.save_document(DoclingDocument(name="legacy"), out)


def damage_parse(client: TestClient, doc_id: str, *, delete: bool) -> None:
    """The stored parse is gone (``delete``) or is not a parse any more."""
    path = settings_of(client).results_dir / doc_id / "docling.json"
    if delete:
        path.unlink()
    else:
        path.write_text('{"not": "a docling document"')


def sidecar(client: TestClient, doc_id: str) -> dict[str, object]:
    out: dict[str, object] = json.loads(
        (settings_of(client).results_dir / doc_id / "read_quality.json").read_text()
    )
    return out


def skip_ids(report: dict[str, object]) -> list[str]:
    return [s["rule_id"] for s in report["skipped"]]  # type: ignore[index, union-attr]


def break_audit_commits(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> dict[str, bool]:
    """While ``state["on"]``, a commit that would store an audit row fails the way a locked or
    full database does; every other commit works. Returns the switch.

    The audit row is noticed when it is flushed (the session holds it only weakly after), by an
    event on the session class that is removed again when the test ends.
    """
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    from procurement_agent.models import AuditLog

    state = {"on": False}
    real = Session.commit

    def noticing(session: Session, *_: object) -> None:
        if any(isinstance(o, AuditLog) for o in session.new):
            session.info["audit_pending"] = True

    def forgetting(session: Session) -> None:
        session.info.pop("audit_pending", None)

    def commit(self: Session) -> None:
        if state["on"]:
            self.flush()
            if self.info.get("audit_pending"):
                raise OperationalError("COMMIT", {}, Exception("database is locked"))
        self.info.pop("audit_pending", None)
        real(self)

    event.listen(Session, "before_flush", noticing)
    event.listen(Session, "after_rollback", forgetting)
    request.addfinalizer(lambda: event.remove(Session, "before_flush", noticing))
    request.addfinalizer(lambda: event.remove(Session, "after_rollback", forgetting))
    monkeypatch.setattr(Session, "commit", commit)
    return state
