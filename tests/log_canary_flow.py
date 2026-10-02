"""Drives the real application through every path that touches document text, with a marker in it.

Run as a subprocess by ``tests/integration/test_log_canary.py``: its stdout and stderr are the
application's logs, exactly as an operator would collect them (real uvicorn with its access log,
structlog, the stdlib loggers of every library), so it prints nothing of its own. Usage::

    python tests/log_canary_flow.py <data_dir> <summary.json>

The marker goes into document body text, sheet names, cell values, core properties, file names, the
text of corrupt files that make the parsers raise, rejected uploads, free-form form fields, query
strings, the API key header, and the model server's replies - including replies that are garbage,
unsupported, or an HTTP error carrying the marker. The test then asserts it is in no log line and in
no database column that is not meant to hold it.
"""

from __future__ import annotations

import io
import json
import logging
import logging.config
import os
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

TOKEN = "ZQXKANARY7"
GEORGIAN = "ჯანარეიკა"  # a made-up word: the marker in the script the clients' documents use
KEY = "pa_canary_flow_key_not_a_secret_012345"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}

#: what the stub model server answers, switched between phases
MODE = {"value": "empty"}


# --- the stub model server -----------------------------------------------------------------------


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def _json(self, status: int, body: object) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        self._json(200, {"models": [{"name": "gemma3:12b"}, {"name": "gemma3:4b"}]})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length) or b"{}")
        mode = MODE["value"]
        if mode == "http500":
            self._json(500, {"error": f"{TOKEN} {GEORGIAN} internal failure"})
            return
        if mode == "garbage":
            content = f"{TOKEN} {GEORGIAN} this is not json {{{{"
        elif mode == "unsupported":
            fact = {
                "type": "subject",
                "value": f"{TOKEN} {GEORGIAN}",
                "raw_text": f"{TOKEN} {GEORGIAN} appears nowhere in the document",
                "unit": None,
                "currency": None,
            }
            content = json.dumps({"facts": [fact]})
        elif request.get("format"):
            content = json.dumps({"facts": []})
        else:
            content = "yes"
        self._json(200, {"message": {"role": "assistant", "content": content}, "eval_count": 3})


# --- documents carrying the marker ---------------------------------------------------------------


def docx_with_marker(price: str = "120,000.00") -> bytes:
    from docx import Document

    doc = Document()
    doc.core_properties.title = f"{TOKEN} {GEORGIAN}"
    doc.core_properties.author = TOKEN
    doc.add_paragraph(f"SUPPLY CONTRACT No. 17/2024 {TOKEN}")
    doc.add_paragraph(f"Concluded on 01.03.2024 between {TOKEN} LLC and {GEORGIAN} LLC.")
    doc.add_paragraph(f"1. The total price is {price} GEL including VAT. {GEORGIAN}")
    doc.add_paragraph("2. The goods shall be delivered within 30 calendar days.")
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def xlsx_with_marker() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = TOKEN  # a sheet name: whatever the bidder called it
    ws.append(["Item", "Description", "Quantity", "Unit", "Unit price (GEL)", "Total (GEL)"])
    ws.append([1, f"cable {TOKEN} {GEORGIAN}", 10, TOKEN, 12, 120])
    ws.append(["", "Total price", "", "", "Total:", "120 GEL"])
    other = wb.create_sheet(GEORGIAN[:31])
    other.append([f"{TOKEN} {GEORGIAN}"])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def corrupt_docx() -> bytes:
    """Passes the upload guard (a zip with the two required parts) and breaks the parsers."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("[Content_Types].xml", f"<Types>{TOKEN} {GEORGIAN} <<<not xml")
        z.writestr("word/document.xml", f"<w:document><w:body><w:p><w:t>{TOKEN} {GEORGIAN}")
    return out.getvalue()


def corrupt_xlsx() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.xml", f"<workbook>{TOKEN} {GEORGIAN} <sheets><sheet name=")
    return out.getvalue()


def hostile_zip(entry: str) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document/>")
        z.writestr(entry, "x")
    return out.getvalue()


# --- the flow ------------------------------------------------------------------------------------


def main(data_dir: str, summary_path: str) -> int:
    import httpx
    import uvicorn

    # The order a real deployment has: uvicorn configures its loggers, then the app is imported.
    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # this driver's own client

    os.environ.update(
        {
            "PA_ENV": "dev",
            "PA_DEV_API_KEY": KEY,
            "PA_DATA_DIR": data_dir,
            "PA_RATE_LIMIT": "100000/minute",
        }
    )
    models = ROOT / "models" / "docling"
    if models.is_dir():
        os.environ["PA_DOCLING_ARTIFACTS_PATH"] = str(models)
    stub = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    os.environ["PA_OLLAMA_BASE_URL"] = f"http://127.0.0.1:{stub.server_address[1]}"

    from procurement_agent.config import Settings
    from procurement_agent.main import create_app

    config = uvicorn.Config(
        create_app(Settings()), host="127.0.0.1", port=0, log_config=None, log_level="info"
    )
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 90
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        return 2
    base = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    api = httpx.Client(base_url=base, headers={"X-API-Key": KEY}, timeout=180, trust_env=False)
    ui = httpx.Client(base_url=base, timeout=180, trust_env=False, follow_redirects=False)
    results: dict[str, str] = {}

    def flow(name: str, fn: Any) -> Any:
        try:
            value = fn()
            results[name] = "ok"
            return value
        except Exception as e:
            where = getattr(getattr(e, "request", None), "url", None)
            results[name] = f"error: {type(e).__name__} {str(where or '')[:120]}"
            return None

    def wait(job_id: str, timeout: float = 240.0) -> str:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            status = api.get(f"/api/v1/jobs/{job_id}").json()["status"]
            if status in ("done", "failed", "cancelled"):
                return str(status)
            time.sleep(0.1)
        return "timeout"

    def upload(name: str, data: bytes) -> tuple[int, str | None, str | None]:
        r = api.post("/api/v1/documents", files={"file": (name, data)})
        if r.status_code != 201:
            return r.status_code, None, None
        status = wait(r.json()["parse_job_id"])
        return r.status_code, r.json()["document"]["id"], status

    docs: dict[str, str] = {}

    # documents that parse
    def good_docs() -> None:
        docs["docx"] = upload(f"{TOKEN}-{GEORGIAN}-contract.docx", docx_with_marker())[1] or ""
        docs["docx2"] = upload(f"{TOKEN}-v2.docx", docx_with_marker("125,000.00"))[1] or ""
        docs["xlsx"] = upload(f"{TOKEN}-bid.xlsx", xlsx_with_marker())[1] or ""

    flow("good documents", good_docs)

    # documents that make the parsers raise, and uploads that are refused
    flow("corrupt docx", lambda: upload(f"{TOKEN}.docx", corrupt_docx()))
    flow("corrupt xlsx", lambda: upload(f"{TOKEN}.xlsx", corrupt_xlsx()))
    flow(
        "corrupt pdf",
        lambda: upload(f"{TOKEN}.pdf", f"%PDF-1.4 {TOKEN} {GEORGIAN} garbage".encode()),
    )
    flow("odd extension", lambda: upload(f"report.{TOKEN}", b"x"))
    flow("odd extension ka", lambda: upload(f"report.{GEORGIAN}", b"x"))
    flow("traversal entry", lambda: upload(f"{TOKEN}.docx", hostile_zip(f"../{TOKEN}/{GEORGIAN}")))
    flow("not a zip", lambda: upload(f"{TOKEN}.xlsx", f"{TOKEN} {GEORGIAN}".encode()))
    flow("empty name", lambda: upload(f"{TOKEN}", b"x"))

    # the model server misbehaves in every way, on a question that itself carries the marker
    def extract(mode: str) -> str:
        MODE["value"] = mode
        r = api.post(
            f"/api/v1/documents/{docs['docx']}/extract",
            json={"question": f"What is {TOKEN} {GEORGIAN}?", "use_llm": True},
        )
        job = r.json()["id"]
        status = wait(job)
        if status == "done":
            api.get(f"/api/v1/jobs/{job}/result")
            api.get(f"/api/v1/jobs/{job}/result", params={"format": "xlsx"})
        return status

    for mode in ("garbage", "unsupported", "http500", "empty"):
        flow(f"extract with a {mode} model", lambda m=mode: extract(m))  # type: ignore[misc]

    def extract_xlsx() -> None:
        MODE["value"] = "garbage"
        body = {"use_llm": True}
        job = api.post(f"/api/v1/documents/{docs['xlsx']}/extract", json=body).json()["id"]
        wait(job)

    flow("extract a workbook", extract_xlsx)

    # checks and exports over the hostile documents
    def checks() -> None:
        pair = {"left_document_id": docs["docx"], "right_document_id": docs["docx2"]}
        for fmt in ("json", "xlsx", "docx"):
            api.post("/api/v1/checks/compare", json=pair, params={"format": fmt})
        act = {"aoa_document_id": docs["docx"], "contract_document_id": docs["docx2"]}
        for fmt in ("json", "docx"):
            api.post("/api/v1/checks/aoa", json=act, params={"format": fmt})
        api.post("/api/v1/checks/aoa/review", json=act)

    flow("checks and exports", checks)

    def boq() -> None:
        from tests.boq_factory import LINES, blank, build_boq

        hostile = [(i, f"{TOKEN} {GEORGIAN} {d}", u, q, m, lab) for i, d, u, q, m, lab in LINES]
        blank_template = build_boq(blank(hostile), priced=False, stated_total=None)
        template = upload(f"{TOKEN}-template.xlsx", blank_template)[1]
        bids = [
            upload(f"{TOKEN}-bid-a.xlsx", build_boq(hostile))[1],
            upload(f"{TOKEN}-bid-b.xlsx", build_boq(hostile, stated_total=2000.0))[1],
        ]
        body = {"template_document_id": template, "bid_document_ids": bids}
        for fmt in ("json", "xlsx"):
            api.post("/api/v1/checks/boq", json=body, params={"format": fmt})

    flow("bill of quantities", boq)

    # the browser interface: sign in wrongly and rightly, hostile fields, query strings
    def browser() -> None:
        ui.post("/ui/login", data={"api_key": f"pa_{TOKEN}{GEORGIAN}"}, headers=SAME_ORIGIN)
        login = {"api_key": KEY, "next": f"/ui/documents?q={TOKEN}"}
        ui.post("/ui/login", data=login, headers=SAME_ORIGIN)
        ui.get(f"/ui/documents?q={TOKEN}&advance_amount={TOKEN}&x={GEORGIAN}")
        ui.get(f"/api/v1/documents?q={TOKEN}", headers={"X-API-Key": KEY})
        ui.get(f"/ui/checks/aoa?advance_amount={TOKEN}&contractor_type={GEORGIAN}")
        ui.get(f"/ui/documents/{docs['docx']}")
        form = {"question": f"{TOKEN} {GEORGIAN}?", "use_llm": "on"}
        ui.post(f"/ui/documents/{docs['docx']}/extract", data=form, headers=SAME_ORIGIN)
        MODE["value"] = "garbage"
        both = {"left": docs["docx"], "right": docs["docx2"]}
        ui.post("/ui/checks/compare/explain", data=both, headers=SAME_ORIGIN)
        query = {**both, "aoa": docs["docx"], "contract": docs["docx2"]}
        for path in ("/ui/checks/compare", "/ui/checks/aoa", "/ui/start/boq"):
            ui.get(path, params=query)

    flow("browser interface", browser)

    # credentials that are wrong in different ways
    def credentials() -> None:
        # a header is bytes on the wire: the Georgian marker goes as UTF-8, like a browser's
        for header in (f"pa_{TOKEN}{'x' * 30}".encode(), TOKEN.encode(), GEORGIAN.encode()):
            httpx.get(
                f"{base}/api/v1/documents",
                headers={"X-API-Key": header},
                timeout=30,
                trust_env=False,
            )
        httpx.post(
            f"{base}/api/v1/documents",
            headers={"X-API-Key": f"pa_{TOKEN}{'y' * 30}"},
            files={"file": (f"{TOKEN}.docx", b"x")},
            timeout=30,
            trust_env=False,
        )

    flow("wrong credentials", credentials)

    # deletion
    flow("delete", lambda: api.delete(f"/api/v1/documents/{docs['docx2']}"))
    time.sleep(1.0)

    server.should_exit = True
    stub.shutdown()
    Path(summary_path).write_text(json.dumps(results, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
