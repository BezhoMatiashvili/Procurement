"""The model is down: a job whose only purpose is the model's answer must end failed, never
'done, 0 answers', and must not be handed back later as if it were the answer."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from procurement_agent.config import Settings
from procurement_agent.db import session_scope
from procurement_agent.extraction.schemas import ChoiceResult, LLMExtraction
from procurement_agent.llm.base import LLMUnavailableError
from procurement_agent.models import Job
from tests.degraded_support import (
    ACT_LINES,
    CONTRACT_LINES,
    SAME_ORIGIN,
    XLSX,
    H,
    make_client,
    upload,
    wait_job,
    xlsx_bytes,
)
from tests.unit.test_aoa_llm import ReadingLLM

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


# --- the model is down ----------------------------------------------------------------------


def test_an_act_review_with_the_model_down_fails_with_a_code(client: TestClient) -> None:
    _with_model(client, ReadingLLM(error=_down()))
    act, _ = upload(client, "act.xlsx", xlsx_bytes(SUBJECT_ACT), XLSX)
    contract, _ = upload(client, "contract.xlsx", xlsx_bytes(SUBJECT_CONTRACT), XLSX)
    ids = {"aoa_document_id": act, "contract_document_id": contract}

    job = client.post("/api/v1/checks/aoa/review", headers=H, json=ids).json()["job"]
    done = wait_job(client, job["id"])
    assert done["status"] == "failed"
    assert str(done["message"]).startswith("LLM_UNAVAILABLE")
    assert "answer(s)" not in str(done["message"])


def test_an_act_review_that_failed_for_want_of_the_model_is_asked_again_when_it_is_back(
    client: TestClient,
) -> None:
    _with_model(client, ReadingLLM(error=_down()))
    act, _ = upload(client, "act.xlsx", xlsx_bytes(SUBJECT_ACT), XLSX)
    contract, _ = upload(client, "contract.xlsx", xlsx_bytes(SUBJECT_CONTRACT), XLSX)
    ids = {"aoa_document_id": act, "contract_document_id": contract}
    first = client.post("/api/v1/checks/aoa/review", headers=H, json=ids).json()["job"]
    assert wait_job(client, first["id"])["status"] == "failed"

    _with_model(client, ReadingLLM())  # the model is back
    again = client.post("/api/v1/checks/aoa/review", headers=H, json=ids)
    assert again.status_code == 202, "a failed-for-outage review must not be reused"
    assert again.json()["job"]["id"] != first["id"]
    assert wait_job(client, again.json()["job"]["id"])["status"] == "done"

    # and now that one is the answer asked about next time
    third = client.post("/api/v1/checks/aoa/review", headers=H, json=ids)
    assert (third.status_code, third.json()["job"]["id"]) == (200, again.json()["job"]["id"])


def test_a_review_that_ended_done_with_an_error_before_this_fix_is_not_reused(
    client: TestClient,
) -> None:
    """Jobs stored by an older version ended 'done, 0 answers' on an outage; the stored run
    names the error, and that is enough to ask again."""
    _with_model(client, ReadingLLM())
    act, _ = upload(client, "act.xlsx", xlsx_bytes(SUBJECT_ACT), XLSX)
    contract, _ = upload(client, "contract.xlsx", xlsx_bytes(SUBJECT_CONTRACT), XLSX)
    ids = {"aoa_document_id": act, "contract_document_id": contract}
    first = client.post("/api/v1/checks/aoa/review", headers=H, json=ids).json()["job"]
    assert wait_job(client, first["id"])["status"] == "done"
    settings: Settings = client.app.state.settings  # type: ignore[attr-defined]
    stored = settings.results_dir / act / "jobs" / f"{first['id']}.json"
    data = json.loads(stored.read_text())
    data.update(error="LLMUnavailableError", items=[])
    stored.write_text(json.dumps(data))

    again = client.post("/api/v1/checks/aoa/review", headers=H, json=ids)
    assert again.status_code == 202
    assert again.json()["job"]["id"] != first["id"]


def test_a_compare_explanation_with_the_model_down_fails_and_is_asked_again(
    client: TestClient,
) -> None:
    from tests.unit.test_compare_llm import FakeLLM

    _with_model(client, FakeLLM(error=_down()))
    base = (COMPARE_PAIRS / "base.docx").read_bytes()
    reworded = (COMPARE_PAIRS / "reworded.docx").read_bytes()
    left, _ = upload(client, "base.docx", base, DOCX)
    right, _ = upload(client, "reworded.docx", reworded, DOCX)
    url = f"/ui/checks/compare/explain?left={left}&right={right}"

    first = client.post(url, headers=SAME_ORIGIN, follow_redirects=False)
    assert first.status_code == 303
    [job] = _jobs("compare_explain")
    done = wait_job(client, job.id)
    assert done["status"] == "failed"
    assert str(done["message"]).startswith("LLM_UNAVAILABLE")

    _with_model(client, FakeLLM())
    client.post(url, headers=SAME_ORIGIN, follow_redirects=False)
    assert len(_jobs("compare_explain")) == 2, "the failed run must not be handed back"


def _extraction_llm(error: Exception | None) -> object:
    class _Model:
        def __init__(self) -> None:
            self.calls = 0

        def generate_structured(self, *, schema: type[Any], **kw: object) -> Any:
            self.calls += 1
            if error is not None:
                raise error
            assert schema is LLMExtraction
            return LLMExtraction(facts=[])

        def generate_choice(self, **kw: object) -> ChoiceResult:  # pragma: no cover - unused
            raise NotImplementedError

        def health(self) -> dict[str, object]:  # pragma: no cover - unused
            return {}

        def options(self) -> dict[str, object]:
            return {}

    return _Model()


def test_extraction_with_the_model_down_keeps_the_rules_result_and_says_it_is_degraded(
    client: TestClient,
) -> None:
    _with_model(client, _extraction_llm(_down()))
    doc, _ = upload(client, "act.xlsx", xlsx_bytes(ACT_LINES), XLSX)
    started = client.post(
        f"/api/v1/documents/{doc}/extract", headers=H, json={"use_llm": True}
    ).json()
    job = wait_job(client, started["id"])
    assert job["status"] == "done", "the rules' facts are kept"
    assert job["degraded"] is True
    assert "model was unavailable" in str(job["message"])
    assert "0 LLM calls" in str(job["message"])

    result = client.get(f"/api/v1/jobs/{started['id']}/result", headers=H).json()
    assert result["degraded"] is True
    assert result["facts"], "the rules and anchors still found the act's facts"
    assert result["warnings"][0].startswith("LLM_UNAVAILABLE")

    client.post("/ui/lang", data={"lang": "en", "next": "/ui/"}, headers=SAME_ORIGIN)
    page = client.get(f"/ui/jobs/{started['id']}").text
    assert 'id="degraded"' in page
    assert "model was unavailable" in page


def test_extraction_with_the_model_answering_is_not_degraded(client: TestClient) -> None:
    _with_model(client, _extraction_llm(None))
    doc, _ = upload(client, "act.xlsx", xlsx_bytes(ACT_LINES), XLSX)
    started = client.post(
        f"/api/v1/documents/{doc}/extract", headers=H, json={"use_llm": True}
    ).json()
    job = wait_job(client, started["id"])
    assert job["degraded"] is False
    result = client.get(f"/api/v1/jobs/{started['id']}/result", headers=H).json()
    assert result["degraded"] is False
    assert not any(w.startswith("LLM_UNAVAILABLE") for w in result["warnings"])


def test_extraction_without_the_model_asked_for_is_not_called_degraded(client: TestClient) -> None:
    doc, _ = upload(client, "act.xlsx", xlsx_bytes(ACT_LINES), XLSX)
    started = client.post(f"/api/v1/documents/{doc}/extract", headers=H, json={}).json()
    assert wait_job(client, started["id"])["degraded"] is False
