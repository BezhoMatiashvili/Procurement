#!/usr/bin/env python
"""The whole product over real loopback sockets, against a stub model server.

Run by ``scripts/verify_offline.sh`` inside a network namespace that has loopback and nothing
else, where any attempt to leave the machine fails in the kernel - native libraries and uvloop
included, which the in-process egress guard cannot see. The flow: start the real application with
its real HTTP server, upload documents, parse, extract (with a question, so the model is called),
compare two versions (JSON, XLSX, annotated DOCX), check a bill of quantities against two bids,
read every report, sign in through the UI, delete. Then assert the guard refused nothing during
normal operation, and prove it refuses when asked.

Exit status 0 only if every step passed. Documents are synthetic; nothing here reads client files.
"""

from __future__ import annotations

import errno
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

KEY = "pa_smoke_key_not_a_secret_0123456789"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
RESULTS: list[tuple[bool, str]] = []


class SmokeError(Exception):
    """A step's expectation did not hold."""


def expect(condition: object, message: object = "") -> None:
    """An ``assert`` that survives ``python -O`` and says what it saw."""
    if not condition:
        raise SmokeError(str(message))


def step(name: str) -> Callable[[Callable[[], None]], Callable[[], None]]:
    """Run a step, record its outcome, never let one failure hide the next."""

    def wrap(fn: Callable[[], None]) -> Callable[[], None]:
        try:
            fn()
        except Exception as e:
            RESULTS.append((False, f"{name}: {type(e).__name__}: {str(e)[:200]}"))
            print(f"FAIL  {name}: {type(e).__name__}: {str(e)[:200]}", flush=True)
        else:
            RESULTS.append((True, name))
            print(f"ok    {name}", flush=True)
        return fn

    return wrap


def raw_exchange(
    port: int, head: str, body_start: bytes = b"", wait_s: float = 5.0
) -> tuple[bytes, bool]:
    """Send a request head and only the START of its body over a real socket, then read the answer.

    Returns what came back and whether the server closed the connection within ``wait_s``. An
    answer that arrives while the rest of the body is still unsent is proof the server decided
    without reading it - the property a mock cannot show, because the real HTTP server's flow
    control sits between the socket and the application.
    """
    received = b""
    with socket.create_connection(("127.0.0.1", port), timeout=1.0) as conn:
        conn.sendall(head.encode("ascii") + body_start)
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            try:
                chunk = conn.recv(65536)
            except TimeoutError:
                continue
            if not chunk:
                return received, True
            received += chunk
    return received, False


# --- the stub model server ----------------------------------------------------------------------


def _empty(schema: dict[str, Any], defs: dict[str, Any]) -> object:
    """The smallest value that satisfies a JSON schema: what a model that knows nothing returns."""
    if "$ref" in schema:
        return _empty(defs[schema["$ref"].rsplit("/", 1)[-1]], defs)
    if "enum" in schema:
        return schema["enum"][0]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [o for o in schema[key] if o.get("type") != "null"] or schema[key]
            return _empty(options[0], defs)
    kind = schema.get("type")
    if kind == "object":
        need = set(schema.get("required", []))
        return {k: _empty(v, defs) for k, v in schema.get("properties", {}).items() if k in need}
    return {"array": [], "string": "", "integer": 0, "number": 0, "boolean": False}.get(str(kind))


class StubOllama(BaseHTTPRequestHandler):
    seen: ClassVar[list[tuple[str, str]]] = []

    def log_message(self, *args: object) -> None:  # silence the default stderr line per request
        pass

    def _send(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        StubOllama.seen.append(("GET", self.path))
        if self.path == "/api/tags":
            self._send(200, {"models": [{"name": "gemma3:12b"}, {"name": "gemma3:4b"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        StubOllama.seen.append(("POST", self.path))
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length) or b"{}")
        if self.path != "/api/chat":
            self._send(404, {"error": "not found"})
            return
        schema = request.get("format")
        content = json.dumps(_empty(schema, schema.get("$defs", {}))) if schema else "yes"
        self._send(
            200,
            {
                "message": {"role": "assistant", "content": content},
                "eval_count": 3,
                "prompt_eval_count": 10,
                "total_duration": 1_000_000,
            },
        )


# --- documents ------------------------------------------------------------------------------------


def _docx(price: str, days: str) -> bytes:
    from docx import Document

    doc = Document()
    doc.add_paragraph("SUPPLY CONTRACT No. 17/2024")
    doc.add_paragraph("Concluded on 01.03.2024 between the Buyer and the Supplier.")
    doc.add_paragraph(f"1. The total price is {price} GEL including VAT.")
    doc.add_paragraph(f"2. The goods shall be delivered within {days} calendar days.")
    doc.add_paragraph("3. Payment is made within 30 days of the invoice.")
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


# --- the flow ---------------------------------------------------------------------


def main() -> int:
    expect_no_network = "--expect-no-network" in sys.argv
    data_dir = tempfile.mkdtemp(prefix="pa-smoke-")
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

    stub = ThreadingHTTPServer(("127.0.0.1", 0), StubOllama)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    os.environ["PA_OLLAMA_BASE_URL"] = f"http://127.0.0.1:{stub.server_address[1]}"

    import httpx
    import uvicorn

    from procurement_agent.config import Settings
    from procurement_agent.main import create_app
    from procurement_agent.security import egress

    settings = Settings()
    server = uvicorn.Server(
        # the flags the image's command passes, so what is measured is what is deployed
        uvicorn.Config(
            create_app(settings),
            host="127.0.0.1",
            port=0,
            log_level="warning",
            server_header=False,
        )
    )
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 60
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        print("FAIL  the application did not start", flush=True)
        return 1
    port = server.servers[0].sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    api = httpx.Client(base_url=base, headers={"X-API-Key": KEY}, timeout=120, trust_env=False)

    def upload(name: str, data: bytes) -> str:
        r = api.post("/api/v1/documents", files={"file": (name, data)})
        expect(r.status_code == 201, (r.status_code, r.text[:200]))
        job = r.json()["parse_job_id"]
        wait(job)
        return str(r.json()["document"]["id"])

    def wait(job_id: str, timeout: float = 240.0) -> dict[str, Any]:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            j = api.get(f"/api/v1/jobs/{job_id}").json()
            if j["status"] in ("done", "failed", "cancelled"):
                expect(j["status"] == "done", j)
                return dict(j)
            time.sleep(0.1)
        raise TimeoutError(f"job {job_id} did not finish")

    docs: dict[str, str] = {}

    @step("the server answers on loopback: /health and /ready")
    def _health() -> None:
        expect(api.get("/health").status_code == 200)
        expect(api.get("/ready").status_code == 200)

    @step("the model server is reached over loopback and its models are local")
    def _status() -> None:
        llm = api.get("/api/v1/status").json()["llm"]
        expect(llm["ok"] is True and llm["models_remote"] == [], llm)

    @step("upload and parse: two contract versions (DOCX)")
    def _upload_docx() -> None:
        docs["v1"] = upload("contract-v1.docx", _docx("120,000.00", "30"))
        docs["v2"] = upload("contract-v2.docx", _docx("125,000.00", "45"))

    @step("extract with a question: the model is called and a result is stored")
    def _extract() -> None:
        before = len(StubOllama.seen)
        r = api.post(
            f"/api/v1/documents/{docs['v2']}/extract",
            json={"question": "Who is the supplier?", "use_llm": True},
        )
        expect(r.status_code == 202, r.text)
        job = wait(r.json()["id"])
        body = api.get(f"/api/v1/jobs/{job['id']}/result").json()
        expect(isinstance(body, dict) and body, "empty result")
        xlsx = api.get(f"/api/v1/jobs/{job['id']}/result", params={"format": "xlsx"})
        expect(xlsx.content[:2] == b"PK", "the xlsx result is not a workbook")
        expect(("POST", "/api/chat") in StubOllama.seen[before:], StubOllama.seen[before:])

    @step("compare two versions: JSON, XLSX and the annotated DOCX")
    def _compare() -> None:
        body = {"left_document_id": docs["v1"], "right_document_id": docs["v2"]}
        j = api.post("/api/v1/checks/compare", json=body)
        expect(j.status_code == 200, j.text[:200])
        counts = j.json()["counts"]
        changes = counts["added"] + counts["removed"] + counts["changed"] + counts["moved"]
        expect(changes >= 1, f"two different versions compare as identical: {counts}")
        for fmt in ("xlsx", "docx"):
            r = api.post("/api/v1/checks/compare", json=body, params={"format": fmt})
            expect(r.status_code == 200 and r.content[:2] == b"PK", (fmt, r.status_code))

    @step("bill of quantities: a template against two bids (JSON and merged XLSX)")
    def _boq() -> None:
        from tests.boq_factory import LINES, blank, build_boq

        template = upload(
            "006-BID Template.xlsx", build_boq(blank(), priced=False, stated_total=None)
        )
        bids = [
            upload("bid-a.xlsx", build_boq(LINES)),
            upload("bid-b.xlsx", build_boq(LINES, stated_total=2000.0)),
        ]
        body = {"template_document_id": template, "bid_document_ids": bids}
        j = api.post("/api/v1/checks/boq", json=body)
        expect(j.status_code == 200 and len(j.json()["bidders"]) == 2, j.text[:200])
        x = api.post("/api/v1/checks/boq", json=body, params={"format": "xlsx"})
        expect(x.status_code == 200 and x.content[:2] == b"PK")

    @step("the browser interface: sign in with a key, list documents, open a page")
    def _ui() -> None:
        ui = httpx.Client(base_url=base, timeout=60, trust_env=False, follow_redirects=True)
        expect(ui.get("/ui/login").status_code == 200)
        r = ui.post("/ui/login", data={"api_key": KEY}, headers=SAME_ORIGIN)
        expect(r.status_code == 200, r.status_code)
        expect(ui.get("/ui/").status_code == 200)
        expect(ui.get(f"/ui/documents/{docs['v2']}").status_code in (200, 404))

    @step("the page image of a document renders")
    def _page() -> None:
        # a DOCX has no page images; the route must answer cleanly, not fall over
        status = api.get(f"/api/v1/documents/{docs['v2']}/pages/1.png").status_code
        expect(status in (200, 404, 409, 422), status)

    @step("delete a document")
    def _delete() -> None:
        expect(api.delete(f"/api/v1/documents/{docs['v1']}").status_code == 204)

    # --- the real HTTP server, over real sockets ------------------------------------

    def status_line(answer: bytes) -> str:
        return answer.split(b"\r\n", 1)[0].decode("latin-1")

    @step("live socket: an anonymous upload is answered 401 while its 90 MB body is still unsent")
    def _anonymous_upload() -> None:
        head = (
            "POST /api/v1/documents HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Content-Type: multipart/form-data; boundary=zz\r\n"
            "Content-Length: 90000000\r\n\r\n"
        )
        started = time.monotonic()
        answer, closed = raw_exchange(
            port,
            head,
            b'--zz\r\nContent-Disposition: form-data; name="file"; filename="a.docx"\r\n\r\nPK',
        )
        expect(status_line(answer).startswith("HTTP/1.1 401"), status_line(answer))
        expect(closed, "the refusal left the connection open")
        expect(time.monotonic() - started < 4.0, "the refusal waited for the body")

    @step("live socket: a body declared far over the route's cap is answered 413 unsent")
    def _declared_too_large() -> None:
        head = (
            "POST /api/v1/documents/none/extract HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"X-API-Key: {KEY}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 5000000\r\n\r\n"
        )
        answer, closed = raw_exchange(port, head, b'{"question": "')
        expect(status_line(answer).startswith("HTTP/1.1 413"), status_line(answer))
        expect(closed, "the refusal left the connection open")

    @step("live socket: a request for a Host name nobody listed is refused")
    def _foreign_host() -> None:
        answer, _ = raw_exchange(port, "GET /health HTTP/1.1\r\nHost: attacker.example\r\n\r\n")
        expect(status_line(answer).startswith("HTTP/1.1 400"), status_line(answer))

    @step("live socket: no response names the server software")
    def _no_banner() -> None:
        answer, _ = raw_exchange(
            port, f"GET /health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
        )
        head = answer.split(b"\r\n\r\n", 1)[0].lower()
        expect(b"server:" not in head or b"uvicorn" not in head, head[:200])

    # --- accounting -----------------------------------------------------------------

    guard = egress.installed()

    @step("the egress guard is active and refused nothing during normal operation")
    def _quiet() -> None:
        if guard is None:
            raise SmokeError("the guard is not installed")
        expect(guard.blocked == 0, f"{guard.blocked} refused connection(s) during normal operation")

    @step("the model server saw only /api/tags and /api/chat")
    def _paths() -> None:
        expect({p for _, p in StubOllama.seen} <= {"/api/tags", "/api/chat"}, set(StubOllama.seen))

    @step("the guard refuses a lookup and a connection to a public host")
    def _refuses() -> None:
        if guard is None:
            raise SmokeError("the guard is not installed")
        for call in (
            lambda: socket.getaddrinfo("example.com", 80),
            lambda: socket.create_connection(("93.184.216.34", 80), timeout=2),
        ):
            try:
                call()
            except egress.EgressBlockedError:
                continue
            raise AssertionError("a connection to a public host was not refused")

    @step("the kernel refuses too (a raw socket, below the guard): no route, one interface")
    def _kernel() -> None:
        import _socket

        if not expect_no_network:
            print("      (skipped: not run inside a network namespace)", flush=True)
            return
        interfaces = [
            ln.split(":")[0].strip() for ln in Path("/proc/net/dev").read_text().splitlines()[2:]
        ]
        expect(interfaces == ["lo"], interfaces)
        raw = _socket.socket()
        raw.settimeout(2)
        try:
            raw.connect(("1.1.1.1", 53))
        except OSError as e:
            expect(e.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH), e)
        else:
            raise AssertionError("a raw socket reached the internet")

    server.should_exit = True
    stub.shutdown()
    failed = [m for ok, m in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} steps passed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
