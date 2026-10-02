"""A marker planted wherever document text arrives must come out of no log line and no audit row.

The documented rule - "log records never carry document text, extracted values, file names chosen by
users, or API keys" - was false at fourteen call sites when this test was first written; the unit
tests for ``safe_exc`` and the log filters keep the individual pieces right, and this is the check
that the *whole* is. ``tests/log_canary_flow.py`` runs the real application (uvicorn with its access
log, structlog, every library's stdlib logger) against a stub model server, in a subprocess whose
stdout and stderr are exactly what an operator would collect, and drives the paths that touch
document text - including the ones that fail: parsers that raise on a corrupt file, uploads that
are refused, a model that answers with garbage, unsupported facts or an HTTP error. Then:

* the marker (an ASCII word and a Georgian one) is searched for in everything it printed;
* every column of every table of the database is searched, except the two that are *meant* to hold
  what the user typed: ``documents.display_name`` (the file name, shown back to its owner) and
  ``jobs.params`` (the question, needed to run the job).

Run against the source this audit started from, the same flow leaks the marker eight times (pydantic
``input_value`` in LLM retry logs, a chunk id carrying a sheet name, an audit row, three access-log
query strings): that is what makes an empty result mean something.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.log_canary_flow import GEORGIAN, KEY, TOKEN

ROOT = Path(__file__).resolve().parents[2]

#: Columns that are meant to hold what a user typed or uploaded.
ALLOWED = {("documents", "display_name"), ("jobs", "params")}
#: Flows without which an empty scan would prove little.
CORE = (
    "good documents",
    "corrupt docx",
    "corrupt xlsx",
    "corrupt pdf",
    "odd extension",
    "extract with a garbage model",
    "extract with a unsupported model",
    "extract with a http500 model",
    "checks and exports",
    "bill of quantities",
)
TRANSPORT = ("ReadError", "RemoteProtocolError", "ConnectError")


@pytest.fixture(scope="module")
def flow(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    work = tmp_path_factory.mktemp("canary")
    summary = work / "summary.json"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PA_", "HF_"))}
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT)])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    done = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tests" / "log_canary_flow.py"),
            str(work / "data"),
            str(summary),
        ],
        capture_output=True,
        text=True,
        timeout=900,
        env=env,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-1500:]
    return {
        "stdout": done.stdout,
        "stderr": done.stderr,
        "flows": json.loads(summary.read_text(encoding="utf-8")),
        "db": work / "data" / "app.db",
    }


def test_the_flow_really_exercised_the_failure_paths(flow: dict[str, object]) -> None:
    flows = flow["flows"]
    assert isinstance(flows, dict)
    for name in CORE:
        assert flows[name] == "ok", (name, flows[name])
    for name, outcome in flows.items():
        assert outcome == "ok" or any(t in outcome for t in TRANSPORT), (name, outcome)
    logs = f"{flow['stdout']}\n{flow['stderr']}"
    for evidence in (
        "parse_failed",  # a corrupt file made a parser raise
        "llm_chunk_failed",  # the model answered with garbage
        "llm_invalid_json_retry",
        "[query withheld]",  # real uvicorn access lines, with the query cut off
        '"POST /api/v1/documents',  # the access log is really in the capture
        "auth.denied",  # a wrong key was refused and audited
    ):
        assert evidence in logs, (
            f"no {evidence!r} in the captured output: the scan would be vacuous"
        )


@pytest.mark.parametrize("marker", [TOKEN, GEORGIAN])
def test_the_marker_is_in_no_log_line(flow: dict[str, object], marker: str) -> None:
    for stream in ("stdout", "stderr"):
        text = str(flow[stream])
        hits = [line[:160] for line in text.splitlines() if marker in line]
        assert hits == [], f"{marker!r} reached {stream}: {hits[:3]}"


@pytest.mark.parametrize("marker", [TOKEN, GEORGIAN])
def test_the_marker_is_only_in_the_columns_meant_to_hold_it(
    flow: dict[str, object], marker: str
) -> None:
    db = sqlite3.connect(f"file:{flow['db']}?mode=ro", uri=True)
    try:
        offenders: list[tuple[str, str, int]] = []
        tables = [r[0] for r in db.execute("select name from sqlite_master where type='table'")]
        for table in tables:
            columns = [r[1] for r in db.execute(f"pragma table_info({table})")]
            for column in columns:
                if (table, column) in ALLOWED:
                    continue
                query = f"select count(*) from {table} where cast({column} as text) like ?"
                (count,) = db.execute(query, (f"%{marker}%",)).fetchone()
                if count:
                    offenders.append((table, column, count))
        assert offenders == [], f"{marker!r} was stored in: {offenders}"
        # and the places that are meant to have it do (the scan reads the right database)
        (named,) = db.execute(
            "select count(*) from documents where display_name like ?", (f"%{TOKEN}%",)
        ).fetchone()
        assert named >= 3
    finally:
        db.close()


def test_the_api_key_is_in_no_log_line_and_no_row(flow: dict[str, object]) -> None:
    for stream in ("stdout", "stderr"):
        assert KEY not in str(flow[stream])
    db = sqlite3.connect(f"file:{flow['db']}?mode=ro", uri=True)
    try:
        for table in [
            r[0] for r in db.execute("select name from sqlite_master where type='table'")
        ]:
            for column in [r[1] for r in db.execute(f"pragma table_info({table})")]:
                query = f"select count(*) from {table} where cast({column} as text) like ?"
                assert db.execute(query, (f"%{KEY}%",)).fetchone() == (0,), (table, column)
    finally:
        db.close()


def test_failed_documents_say_what_kind_of_failure_it_was(flow: dict[str, object]) -> None:
    db = sqlite3.connect(f"file:{flow['db']}?mode=ro", uri=True)
    try:
        errors = [r[0] for r in db.execute("select error from documents where status = 'failed'")]
    finally:
        db.close()
    assert errors, "no failed document: the corrupt files did not fail"
    for error in errors:
        assert error.startswith("could not be read: "), error
        assert len(error) < 200
