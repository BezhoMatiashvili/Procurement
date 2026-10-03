"""What one caller can do to everyone else's service, through the real application."""

from __future__ import annotations

import io
import json
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from docx import Document as WordDocument
from fastapi.testclient import TestClient

import procurement_agent.db as dbmod
from procurement_agent.api import checks as checks_api
from procurement_agent.config import Settings, get_settings
from procurement_agent.db import session_scope
from procurement_agent.main import create_app
from procurement_agent.models import Job, JobStatus
from procurement_agent.security.auth import create_api_key
from procurement_agent.security.capacity import report_capacity
from procurement_agent.ui import routes as ui_routes

DEV_KEY = "pa_devkey_for_tests_only"
H = {"X-API-Key": DEV_KEY}
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
CANARY = "ZQX-CANARY-VALUE-5521"


def _make(tmp_path: Path, **overrides: Any) -> Iterator[TestClient]:
    base: dict[str, Any] = {
        "env": "dev",
        "data_dir": tmp_path / "data",
        "dev_api_key": DEV_KEY,
        "ollama_base_url": "http://127.0.0.1:1",
        "rate_limit": "10000/minute",
    }
    base.update(overrides)
    settings = Settings(**base)
    get_settings.cache_clear()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as client:
        yield client
    dbmod._engine = None
    dbmod._SessionLocal = None
    get_settings.cache_clear()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    yield from _make(tmp_path)


def _docx(price: str) -> bytes:
    doc = WordDocument()
    doc.add_paragraph("SUPPLY CONTRACT No. 17/2024")
    doc.add_paragraph(f"1. The total price is {price} GEL including VAT.")
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _upload(client: TestClient, price: str, headers: dict[str, str] = H) -> str:
    r = client.post("/api/v1/documents", headers=headers, files={"file": ("a.docx", _docx(price))})
    assert r.status_code == 201, r.text
    for _ in range(600):
        job = client.get(f"/api/v1/jobs/{r.json()['parse_job_id']}", headers=headers).json()
        if job["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    return str(r.json()["document"]["id"])


# --- reports are computed a few at a time ------------------------------------------------------


@pytest.fixture
def one_slot(tmp_path: Path) -> Iterator[TestClient]:
    yield from _make(tmp_path, max_concurrent_reports=1, report_wait_s=0.05)


def test_a_report_asked_for_while_every_slot_is_taken_gets_503_and_retry_after(
    one_slot: TestClient,
) -> None:
    left, right = _upload(one_slot, "100"), _upload(one_slot, "200")
    body = {"left_document_id": left, "right_document_id": right}
    assert one_slot.post("/api/v1/checks/compare", headers=H, json=body).status_code == 200
    with report_capacity(1, 0.05).slot():  # somebody else's report is running
        r = one_slot.post("/api/v1/checks/compare", headers=H, json=body)
        assert r.status_code == 503
        assert r.headers["retry-after"] == "5"
        r = one_slot.post(
            "/api/v1/checks/aoa",
            headers=H,
            json={"aoa_document_id": left, "contract_document_id": right},
        )
        assert r.status_code == 503
        r = one_slot.post("/api/v1/checks/compare", headers=H, params={"format": "xlsx"}, json=body)
        assert r.status_code == 503  # downloads recompute too: same limit
    assert one_slot.post("/api/v1/checks/compare", headers=H, json=body).status_code == 200


def test_the_browser_pages_obey_the_same_limit_and_answer_with_a_page_not_a_stack_trace(
    one_slot: TestClient,
) -> None:
    left, right = _upload(one_slot, "100"), _upload(one_slot, "200")
    one_slot.post(
        "/ui/login", data={"api_key": DEV_KEY}, headers=SAME_ORIGIN, follow_redirects=False
    )
    with report_capacity(1, 0.05).slot():
        r = one_slot.get("/ui/checks/compare", params={"left": left, "right": right})
    assert r.status_code in (200, 503)
    assert "Traceback" not in r.text and "Internal Server Error" not in r.text
    assert (
        one_slot.get("/ui/checks/compare", params={"left": left, "right": right}).status_code == 200
    )


def test_the_probes_and_the_other_routes_answer_while_the_slots_are_full(
    one_slot: TestClient,
) -> None:
    with report_capacity(1, 0.05).slot():
        assert one_slot.get("/health").status_code == 200
        assert one_slot.get("/ready").status_code == 200
        assert one_slot.get("/api/v1/documents", headers=H).status_code == 200
        assert one_slot.get("/api/v1/status", headers=H).status_code == 200


def test_a_report_that_fails_gives_its_slot_back(one_slot: TestClient) -> None:
    for _ in range(4):  # more failures than there are slots
        r = one_slot.post(
            "/api/v1/checks/compare",
            headers=H,
            json={"left_document_id": "0" * 32, "right_document_id": "1" * 32},
        )
        assert r.status_code == 404
    left, right = _upload(one_slot, "100"), _upload(one_slot, "200")
    body = {"left_document_id": left, "right_document_id": right}
    assert one_slot.post("/api/v1/checks/compare", headers=H, json=body).status_code == 200


@contextmanager
def _another_report_starts_after(name: str, module: Any = checks_api) -> Iterator[None]:
    """For the requests made inside: every slot is taken at the moment a request has its check
    and wants to write the file - the check ran and gave its slot back, then somebody else's
    report took it. The slot is given back, and the check restored, on leaving."""
    real: Callable[..., Any] = getattr(module, name)
    with ExitStack() as stack, pytest.MonkeyPatch.context() as patch:

        def run_then_fill(*args: Any, **kwargs: Any) -> Any:
            result = real(*args, **kwargs)
            stack.enter_context(report_capacity(1, 0.05).slot())
            return result

        patch.setattr(module, name, run_then_fill)
        yield


def test_the_act_with_notes_is_refused_503_while_every_slot_is_taken_and_served_after(
    one_slot: TestClient,
) -> None:
    act, contract = _upload(one_slot, "100"), _upload(one_slot, "200")
    body = {"aoa_document_id": act, "contract_document_id": contract}
    url = "/api/v1/checks/aoa"
    # one slot, and the check inside the request uses it: the file must not wait for itself
    free = one_slot.post(url, headers=H, params={"format": "docx"}, json=body)
    assert free.status_code == 200, free.text
    assert free.headers["content-disposition"].endswith('.docx"')
    with _another_report_starts_after("run_aoa"):
        r = one_slot.post(url, headers=H, params={"format": "docx"}, json=body)
        assert r.status_code == 503
        assert r.headers["retry-after"] == "5"
    assert one_slot.post(url, headers=H, params={"format": "docx"}, json=body).status_code == 200


def test_the_comparison_with_notes_is_refused_503_while_every_slot_is_taken_and_served_after(
    one_slot: TestClient,
) -> None:
    left, right = _upload(one_slot, "100"), _upload(one_slot, "200")
    body = {"left_document_id": left, "right_document_id": right}
    url = "/api/v1/checks/compare"
    free = one_slot.post(url, headers=H, params={"format": "docx"}, json=body)
    assert free.status_code == 200, free.text
    with _another_report_starts_after("run_compare"):
        r = one_slot.post(url, headers=H, params={"format": "docx"}, json=body)
        assert r.status_code == 503
        assert r.headers["retry-after"] == "5"
    assert one_slot.post(url, headers=H, params={"format": "docx"}, json=body).status_code == 200


def test_the_browser_links_to_the_files_obey_the_same_limit(one_slot: TestClient) -> None:
    act, contract = _upload(one_slot, "100"), _upload(one_slot, "200")
    one_slot.post(
        "/ui/login", data={"api_key": DEV_KEY}, headers=SAME_ORIGIN, follow_redirects=False
    )
    link = "/ui/checks/aoa/annotated.docx"
    assert one_slot.get(link, params={"act": act, "contract": contract}).status_code == 200
    with _another_report_starts_after("run_aoa", ui_routes):  # the page's own import of it
        r = one_slot.get(link, params={"act": act, "contract": contract})
    # a page that says so, not the file and not a stack trace
    assert r.headers["content-type"].startswith("text/html")
    assert "busy with other reports" in r.text
    assert "Traceback" not in r.text and "Internal Server Error" not in r.text


def test_the_limits_are_configurable_and_bounded() -> None:
    assert Settings().max_concurrent_reports == 4 and Settings().report_wait_s == 2.0
    for bad in (0, 65):
        with pytest.raises(ValueError, match="max_concurrent_reports"):
            Settings(max_concurrent_reports=bad)
    with pytest.raises(ValueError, match="report_wait_s"):
        Settings(report_wait_s=-1)


# --- the status endpoint -----------------------------------------------------------------------


def _key(user: str) -> dict[str, str]:
    with session_scope() as s:
        key, _ = create_api_key(s, user)
    return {"X-API-Key": key}


def test_status_shows_the_callers_own_work_and_nobody_elses(client: TestClient) -> None:
    alice, bob = _key("alice"), _key("bob")
    doc = _upload(client, "100", alice)
    with session_scope() as s:
        for _ in range(3):
            s.add(
                Job(
                    document_id=doc,
                    owner="alice",
                    kind="extract",
                    params={},
                    status=JobStatus.queued,
                )
            )
        s.add(Job(document_id=doc, owner="alice", kind="extract", params={}, status=JobStatus.done))
    mine = client.get("/api/v1/status", headers=alice).json()
    theirs = client.get("/api/v1/status", headers=bob).json()
    assert mine["my_active_jobs"] == 3
    assert theirs["my_active_jobs"] == 0
    assert "queue" not in mine and "queue" not in theirs  # the server-wide depth is not shown


def test_polling_status_does_not_make_the_server_poll_the_model(tmp_path: Path) -> None:
    for client in _make(tmp_path):
        probes: list[str] = []

        def handler(request: httpx.Request, seen: list[str] = probes) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200, json={"models": [{"name": "gemma3:12b"}]})

        llm = client.app.state.llm  # type: ignore[attr-defined]
        llm._http = httpx.Client(
            base_url="http://127.0.0.1:1",
            transport=httpx.MockTransport(handler),
            event_hooks={"request": [llm._only_ollama]},
        )
        llm._health_cache = None
        for _ in range(20):
            assert client.get("/api/v1/status", headers=H).json()["llm"]["ok"] is True
        assert probes == ["/api/tags"]  # one probe for twenty polls


def test_the_model_probe_cache_expires_and_can_be_bypassed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from procurement_agent.llm import ollama_client

    now = [1000.0]
    monkeypatch.setattr(ollama_client.time, "monotonic", lambda: now[0])
    probes = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        probes[0] += 1
        return httpx.Response(200, json={"models": [{"name": "gemma3:12b"}]})

    llm = client.app.state.llm  # type: ignore[attr-defined]
    llm._http = httpx.Client(
        base_url="http://127.0.0.1:1",
        transport=httpx.MockTransport(handler),
        event_hooks={"request": [llm._only_ollama]},
    )
    llm._health_cache = None
    llm.health()
    llm.health()
    assert probes[0] == 1
    now[0] += 11
    llm.health()
    assert probes[0] == 2
    llm.health(max_age_s=0)
    assert probes[0] == 3
    answer = llm.health()
    answer["ok"] = "tampered"  # a caller changing its copy must not change the cache
    assert llm.health()["ok"] is True


# --- validation errors do not echo what was sent -----------------------------------------------


def test_an_over_long_key_at_sign_in_is_refused_without_being_echoed(client: TestClient) -> None:
    typed = "pa_" + CANARY * 20
    r = client.post("/ui/login", data={"api_key": typed}, headers=SAME_ORIGIN)
    assert r.status_code == 422
    assert CANARY not in r.text
    (error,) = r.json()["detail"]
    assert error == {
        "loc": ["body", "api_key"],
        "msg": "String should have at most 200 characters",
        "type": "string_too_long",
    }


def test_a_wrong_type_in_a_json_body_does_not_come_back(client: TestClient) -> None:
    r = client.post(
        "/api/v1/checks/compare",
        headers=H,
        json={"left_document_id": {"nested": CANARY}, "right_document_id": 12345},
    )
    assert r.status_code == 422
    assert CANARY not in r.text and "12345" not in r.text
    assert {e["type"] for e in r.json()["detail"]} >= {"string_type"}
    assert all(set(e) == {"loc", "msg", "type"} for e in r.json()["detail"])


def test_the_error_list_and_its_pieces_are_bounded(client: TestClient) -> None:
    body = {"targets": [CANARY] * 40, "question": "x" * 600}
    r = client.post("/api/v1/documents/" + "0" * 32 + "/extract", headers=H, json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert 1 <= len(detail) <= 20
    assert all(len(str(p)) <= 64 for e in detail for p in e["loc"])
    assert all(len(e["msg"]) <= 200 for e in detail)
    assert CANARY not in r.text


def test_a_validation_error_in_a_json_api_is_still_a_422_with_the_standard_shape(
    client: TestClient,
) -> None:
    r = client.post("/api/v1/checks/compare", headers=H, json={})
    assert r.status_code == 422
    assert [e["loc"] for e in r.json()["detail"]] == [
        ["body", "left_document_id"],
        ["body", "right_document_id"],
    ]
    assert json.loads(r.text)["detail"][0]["type"] == "missing"


# --- free-form fields are bounded --------------------------------------------------------------


def test_the_free_form_fields_of_the_act_check_have_limits(client: TestClient) -> None:
    login = {"api_key": DEV_KEY}
    client.post("/ui/login", data=login, headers=SAME_ORIGIN, follow_redirects=False)
    for field, limit in (("contractor_type", 32), ("advance_amount", 40), ("advance_date", 40)):
        ok = client.get("/ui/checks/aoa", params={"act": "x", field: "a" * limit})
        too_long = client.get("/ui/checks/aoa", params={"act": "x", field: "a" * (limit + 1)})
        assert ok.status_code != 422, field
        assert too_long.status_code == 422, field
        assert CANARY not in too_long.text


def test_a_malformed_id_still_gets_a_page_not_a_validation_error(client: TestClient) -> None:
    login = {"api_key": DEV_KEY}
    client.post("/ui/login", data=login, headers=SAME_ORIGIN, follow_redirects=False)
    r = client.get("/ui/checks/aoa", params={"act": "a" * 200})
    assert r.status_code != 422
    assert "text/html" in r.headers["content-type"]


# --- the model pass is asked for once per pair -------------------------------------------------


def _explain(client: TestClient, left: str, right: str) -> str:
    r = client.post(
        f"/ui/checks/compare/explain?left={left}&right={right}",
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text[:200]
    return str(r.headers["location"].split("job=")[1].split("&")[0])


def _explain_jobs() -> list[Job]:
    from sqlalchemy import select

    with session_scope() as s:
        jobs = list(s.scalars(select(Job).where(Job.kind == "compare_explain")))
        s.expunge_all()
    return jobs


def test_asking_the_model_about_the_same_pair_twice_is_one_job(client: TestClient) -> None:
    left, right = _upload(client, "100"), _upload(client, "200")
    # jobs stay queued: nothing races the test
    client.app.state.worker.stop()  # type: ignore[attr-defined]
    login = {"api_key": DEV_KEY}
    client.post("/ui/login", data=login, headers=SAME_ORIGIN, follow_redirects=False)
    first = _explain(client, left, right)
    again = _explain(client, left, right)
    third = _explain(client, left, right)
    assert first == again == third
    assert len(_explain_jobs()) == 1


def test_another_pair_or_another_user_gets_a_job_of_their_own(client: TestClient) -> None:
    alice, bob = _key("alice"), _key("bob")
    a_left, a_right = _upload(client, "100", alice), _upload(client, "200", alice)
    b_left, b_right = _upload(client, "100", bob), _upload(client, "200", bob)
    client.app.state.worker.stop()  # type: ignore[attr-defined]
    for headers, pair in ((alice, (a_left, a_right)), (bob, (b_left, b_right))):
        client.cookies.clear()
        client.post(
            "/ui/login",
            data={"api_key": headers["X-API-Key"]},
            headers=SAME_ORIGIN,
            follow_redirects=False,
        )
        _explain(client, *pair)
        _explain(client, *pair)
        _explain(client, pair[1], pair[0])  # the reverse order is another question
    jobs = _explain_jobs()
    assert sorted(j.owner for j in jobs) == ["alice", "alice", "bob", "bob"]


def test_a_failed_attempt_may_be_asked_again(client: TestClient) -> None:
    left, right = _upload(client, "100"), _upload(client, "200")
    client.app.state.worker.stop()  # type: ignore[attr-defined]
    client.post("/ui/login", data={"api_key": DEV_KEY}, headers=SAME_ORIGIN, follow_redirects=False)
    first = _explain(client, left, right)
    with session_scope() as s:
        job = s.get(Job, first)
        assert job is not None
        job.status = JobStatus.failed
    second = _explain(client, left, right)
    assert second != first  # a failure is not an answer: the retry is a new job
    assert len(_explain_jobs()) == 2


def test_the_reused_job_does_not_use_up_the_users_job_capacity(tmp_path: Path) -> None:
    for client in _make(tmp_path, max_active_jobs_per_user=1):
        left, right = _upload(client, "100"), _upload(client, "200")
        client.app.state.worker.stop()  # type: ignore[attr-defined]
        client.post(
            "/ui/login", data={"api_key": DEV_KEY}, headers=SAME_ORIGIN, follow_redirects=False
        )
        first = _explain(client, left, right)  # now one active job: the cap
        assert _explain(client, left, right) == first  # reused, not refused


def test_a_query_of_thousands_of_ids_is_cut_off_without_quadratic_work() -> None:
    """``?bids=`` takes ids from the query string, which can be as long as the server's request
    line allows; checking each against the list kept so far made that quadratic."""
    from procurement_agent.ui.routes import _ids

    raw = ",".join(f"{i:x}" for i in range(30_000))
    started = time.perf_counter()
    got = _ids(raw, 10)
    assert got == [f"{i:x}" for i in range(10)]
    assert time.perf_counter() - started < 0.5
    assert _ids("a,,b, a ,!bad,c-d,b", 10) == ["a", "b"]  # malformed and repeated ids drop out
