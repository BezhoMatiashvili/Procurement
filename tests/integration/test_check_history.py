"""A file leads back to the checks it took part in.

The reports are recomputed from the stored files whenever they are opened, so what is recorded is
only which check, over which documents, with which options. The tests open reports the way a
person does - through the pages - and read what the page of each file then lists.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import selectinload

from procurement_agent import history
from procurement_agent.db import session_scope
from procurement_agent.models import (
    AuditLog,
    CheckRun,
    CheckRunDocument,
    Document,
    DocumentStatus,
    Job,
    JobStatus,
)
from procurement_agent.ui.i18n import STRINGS
from procurement_agent.ui.routes import _report_url
from tests.integration.test_ui_flows import (
    SAME_ORIGIN,
    H,
    _boq_run,
    _compare_run,
    _make_ui_client,
    _open_pair,
)

LINK = re.compile(r'<tr data-href="(/ui/checks/[^"]+)"')


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    yield from _make_ui_client(tmp_path)


def ka(key: str) -> str:
    return STRINGS[key][1]


def _listed(client: TestClient, doc_id: str) -> list[str]:
    """The report links the page of a file lists, in page order, as a browser would read them."""
    page = client.get(f"/ui/documents/{doc_id}").text
    return [html.unescape(link) for link in LINK.findall(page)]


def _runs() -> list[CheckRun]:
    with session_scope() as s:
        found = s.scalars(select(CheckRun).options(selectinload(CheckRun.documents)))
        return list(found.unique())


def _options(link: str) -> dict[str, str]:
    return {name: values[0] for name, values in parse_qs(urlsplit(link).query).items()}


def _boq_url(template: str, bids: list[str]) -> str:
    return f"/ui/checks/boq?template={template}&bids={','.join(bids)}"


def _explain_job(right: str, left: str) -> str:
    with session_scope() as s:
        job = Job(
            document_id=right,
            owner="dev",
            kind="compare_explain",
            params={"left": left, "right": right},
            status=JobStatus.done,
        )
        s.add(job)
        s.flush()
        return job.id


def _documents(n: int) -> list[str]:
    with session_scope() as s:
        docs = [
            Document(
                owner="dev", display_name=f"d{i}.xlsx", kind="xlsx", size_bytes=1, sha256="0" * 64
            )
            for i in range(n)
        ]
        s.add_all(docs)
        s.flush()
        return [d.id for d in docs]


# --- the page of a file lists the checks it took part in -----------------------------------


def test_a_boq_comparison_is_listed_on_the_page_of_every_file_it_used(client: TestClient) -> None:
    template, bids = _boq_run(client)
    assert client.get(_boq_url(template, bids)).status_code == 200
    expected = f"/ui/checks/boq?template={template}&bids={bids[0]}%2C{bids[1]}"
    page = client.get(f"/ui/documents/{template}").text
    assert ka("task.boq.title") in page and ka("checks.role.template") in page
    for doc_id in (template, *bids):
        assert _listed(client, doc_id) == [expected]
    assert ka("checks.role.bid") in client.get(f"/ui/documents/{bids[0]}").text
    # the link is the report: pressing it from a file's page shows the comparison again
    again = client.get(_listed(client, bids[1])[0])
    assert again.status_code == 200 and "Company-1" in again.text and "Company-2" in again.text


def test_an_act_check_remembers_the_options_it_was_opened_with(client: TestClient) -> None:
    act, contract = _open_pair(client)
    url = f"/ui/checks/aoa?act={act}&contract={contract}"
    opened = f"{url}&contractor_type=company&advance_amount=1000&advance_date=2026-03-20"
    assert client.get(opened).status_code == 200
    [link] = _listed(client, act)
    assert _options(link) == {
        "act": act,
        "contract": contract,
        "contractor_type": "company",
        "advance_amount": "1000",
        "advance_date": "2026-03-20",
    }
    assert _listed(client, contract) == [link]  # the contract's page lists it too
    assert ka("checks.role.contract") in client.get(f"/ui/documents/{contract}").text

    # opened again with other options it is the same check, now as it was last opened
    assert client.get(f"{url}&contractor_type=small_business").status_code == 200
    [link] = _listed(client, act)
    assert _options(link) == {"act": act, "contract": contract, "contractor_type": "small_business"}
    assert len(_runs()) == 1


def test_a_comparison_is_listed_with_its_original_and_its_revision(client: TestClient) -> None:
    left, right = _compare_run(client)
    assert client.get(f"/ui/checks/compare?left={left}&right={right}").status_code == 200
    [link] = _listed(client, left)
    assert _options(link) == {"left": left, "right": right}
    assert ka("checks.role.left") in client.get(f"/ui/documents/{left}").text
    assert ka("checks.role.right") in client.get(f"/ui/documents/{right}").text
    assert client.get(link).status_code == 200


def test_the_checks_come_first_on_the_page_and_an_empty_list_says_so(client: TestClient) -> None:
    template, bids = _boq_run(client)
    before = client.get(f"/ui/documents/{bids[0]}").text
    assert ka("checks.none") in before and not LINK.findall(before)
    client.get(_boq_url(template, bids))
    after = client.get(f"/ui/documents/{bids[0]}").text
    assert ka("checks.none") not in after and ka("checks.lead") in after
    # above the extraction form: coming back to a file shows its results before anything else
    assert after.index(ka("checks.title")) < after.index('id="extract"')


# --- one row per check, however often it is opened -----------------------------------------


def test_the_same_check_is_one_row_however_often_it_is_opened(client: TestClient) -> None:
    template, bids = _boq_run(client)
    left, right = _compare_run(client)
    for _ in range(3):  # a refresh
        client.get(_boq_url(template, bids))
    client.get(_boq_url(template, list(reversed(bids))))  # the same bids, the other way round
    client.post(  # the language toggle sends you back to the same report
        "/ui/lang",
        data={"lang": "en", "next": _boq_url(template, bids)},
        headers=SAME_ORIGIN,
    )
    client.get(_boq_url(template, bids))
    for show in ("all", "changes", "critical"):  # a filter on the page is not another check
        client.get(f"/ui/checks/compare?left={left}&right={right}&show={show}")
    assert sorted(run.kind for run in _runs()) == ["boq", "compare"]
    assert len(_listed(client, template)) == 1


def test_a_comparison_keeps_the_model_job_that_a_plain_link_would_drop(client: TestClient) -> None:
    left, right = _compare_run(client)
    job = _explain_job(right, left)
    client.get(f"/ui/checks/compare?left={left}&right={right}&job={job}")
    assert _options(_listed(client, left)[0]).get("job") == job
    # opened again from a browser's history, without the job: the answer is not forgotten
    client.get(f"/ui/checks/compare?left={left}&right={right}")
    assert _options(_listed(client, left)[0]).get("job") == job
    # and an id that is nobody's job is never stored
    client.get(f"/ui/checks/compare?left={left}&right={right}&job=deadbeef")
    assert _options(_listed(client, left)[0]).get("job") == job


def test_a_model_review_row_reopens_the_act_with_the_options_it_was_recorded_with(
    client: TestClient,
) -> None:
    act, contract = _open_pair(client)
    client.get(f"/ui/checks/aoa?act={act}&contract={contract}&contractor_type=small_business")
    with session_scope() as s:
        job = Job(document_id=act, owner="dev", kind="aoa_review", params={"contract": contract})
        s.add(job)
        s.flush()
        job_id = job.id
    r = client.get(f"/ui/jobs/{job_id}", follow_redirects=False)
    assert r.status_code == 303
    assert _options(r.headers["location"]) == {
        "act": act,
        "contract": contract,
        "contractor_type": "small_business",
        "review": job_id,
    }


# --- what removes a check ------------------------------------------------------------------


def test_deleting_a_file_removes_its_checks_from_every_page(client: TestClient) -> None:
    template, bids = _boq_run(client)
    left, right = _compare_run(client)
    client.get(_boq_url(template, bids))
    client.get(f"/ui/checks/compare?left={left}&right={right}")
    assert client.delete(f"/api/v1/documents/{bids[0]}", headers=H).status_code == 204
    assert _listed(client, template) == [] and _listed(client, bids[1]) == []
    assert len(_listed(client, left)) == 1  # a check the file was not in is untouched
    assert [run.kind for run in _runs()] == ["compare"]
    with session_scope() as s:  # and the rows that tied the documents to it went with it
        assert {p.run_id for p in s.scalars(select(CheckRunDocument))} == {_runs()[0].id}


def test_a_check_that_outlived_its_document_is_not_listed(client: TestClient) -> None:
    """A row the deletion missed (a crash between the two steps) must not point at a dead page."""
    template, bids = _boq_run(client)
    client.get(_boq_url(template, bids))
    with session_scope() as s:
        doc = s.get(Document, bids[0])
        assert doc is not None
        doc.status = DocumentStatus.deleted  # status only: the run is deliberately kept
    assert len(_runs()) == 1
    assert _listed(client, template) == []


# --- who sees it ---------------------------------------------------------------------------


def test_a_check_belongs_to_the_person_who_ran_it(client: TestClient) -> None:
    template, bids = _boq_run(client)
    client.get(_boq_url(template, bids))
    with session_scope() as s:
        assert [r.kind for r in history.runs_for_document(s, "dev", template)] == ["boq"]
        assert history.runs_for_document(s, "bob", template) == []
        # a row somebody else owns, naming this file, is not listed on its page either
        s.add(
            CheckRun(
                owner="bob",
                kind="compare",
                key="k",
                params={"left": template, "right": bids[0]},
                documents=[
                    CheckRunDocument(document_id=template, role="left"),
                    CheckRunDocument(document_id=bids[0], role="right"),
                ],
            )
        )
    assert len(_listed(client, template)) == 1


# --- the bookkeeping -----------------------------------------------------------------------


def test_the_history_keeps_a_persons_newest_checks_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            clock[0] += timedelta(seconds=1)
            return clock[0]

    monkeypatch.setattr(history, "datetime", Clock)
    n = history.MAX_RUNS_PER_OWNER + 5
    ids = _documents(n + 1)
    with session_scope() as s:
        for i in range(n):
            pair = [(ids[i], "left"), (ids[i + 1], "right")]
            history.record_run(s, owner="dev", kind="compare", documents=pair, params={})
    assert len(_runs()) == history.MAX_RUNS_PER_OWNER
    with session_scope() as s:
        assert history.find_run(s, "dev", "compare", [(ids[0], "left"), (ids[1], "right")]) is None
        newest = [(ids[n - 1], "left"), (ids[n], "right")]
        assert history.find_run(s, "dev", "compare", newest) is not None


def test_a_history_write_that_fails_never_breaks_the_report(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def locked(*_: object, **__: object) -> None:
        raise OperationalError("INSERT", {}, Exception("database is locked"))

    monkeypatch.setattr(history, "_trim", locked)
    template, bids = _boq_run(client)
    r = client.get(_boq_url(template, bids))
    assert r.status_code == 200 and "Company-1" in r.text
    assert _runs() == []  # the savepoint rolled the half-written row back
    with session_scope() as s:  # and the audit row of looking at the report is intact
        rows = s.scalars(select(AuditLog).where(AuditLog.action == "check.boq.json")).all()
        assert len(rows) == 1


def test_a_file_that_is_both_sides_of_a_comparison_is_one_row(client: TestClient) -> None:
    [doc_id] = _documents(1)
    with session_scope() as s:
        history.record_run(
            s,
            owner="dev",
            kind="compare",
            documents=[(doc_id, "left"), (doc_id, "right")],
            params={"left": doc_id, "right": doc_id},
        )
    [run] = _runs()
    assert [p.document_id for p in run.documents] == [doc_id]


def test_options_are_replaced_as_a_whole_and_the_kept_ones_survive_a_reload(
    client: TestClient,
) -> None:
    a, b = _documents(2)
    pair = [(a, "left"), (b, "right")]
    with session_scope() as s:
        history.record_run(
            s, owner="dev", kind="compare", documents=pair, params={"job": "j1"}, keep=("job",)
        )
    with session_scope() as s:
        history.record_run(
            s, owner="dev", kind="compare", documents=pair, params={"left": a}, keep=("job",)
        )
    with session_scope() as s:  # a fresh session: what was written is what was kept
        run = history.find_run(s, "dev", "compare", pair)
        assert run is not None and run.params == {"left": a, "job": "j1"}
    with session_scope() as s:
        history.record_run(s, owner="dev", kind="compare", documents=pair, params={"job": "j2"})
    with session_scope() as s:
        run = history.find_run(s, "dev", "compare", pair)
        assert run is not None and run.params == {"job": "j2"}


def test_what_makes_two_views_one_check() -> None:
    bids = [("b1", "bid"), ("b2", "bid")]
    boq = history.run_key("boq", [("t", "template"), *bids])
    assert boq == history.run_key("boq", [("t", "template"), *reversed(bids)])
    assert boq != history.run_key("boq", [("t", "template"), ("b1", "bid")])
    swapped = history.run_key("compare", [("y", "left"), ("x", "right")])
    assert history.run_key("compare", [("x", "left"), ("y", "right")]) != swapped
    assert history.run_key("aoa", [("x", "act")]) != history.run_key("compare", [("x", "act")])


def test_a_stored_check_cannot_put_a_stray_parameter_in_its_link() -> None:
    run = CheckRun(
        kind="compare", params={"left": "a", "right": "b", "job": "j", "evil": "x", "next": "//e"}
    )
    assert _report_url(run) == "/ui/checks/compare?left=a&right=b&job=j"
    boq = CheckRun(kind="boq", params={"template": "t", "bids": ["b1", "b2"], "evil": "x"})
    assert _report_url(boq) == "/ui/checks/boq?template=t&bids=b1%2Cb2"
