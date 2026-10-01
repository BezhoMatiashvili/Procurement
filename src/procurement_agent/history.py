"""The checks a person has run, so a file can lead back to their reports.

A report is recomputed from the stored files every time it is opened: its URL carries the document
ids and the options and nothing else. What has to be remembered to open one again is exactly that,
and this module remembers it - which check, over which documents, with which options - and nothing
the documents say.

One row per check, not per view: a refresh, the language toggle and a filter on the page are the
same check and update the row they already have.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from procurement_agent.models import CheckRun, CheckRunDocument, DocumentStatus
from procurement_agent.security.audit import log

__all__ = [
    "LISTED",
    "MAX_RUNS_PER_OWNER",
    "CheckKind",
    "find_run",
    "forget_runs",
    "record_run",
    "run_key",
    "runs_for_document",
]

CheckKind = Literal["aoa", "boq", "compare"]

#: A person's newest checks that are kept; each new one past this drops the oldest.
MAX_RUNS_PER_OWNER = 200
#: The checks the page of one file lists.
LISTED = 20


def run_key(kind: str, documents: Sequence[tuple[str, str]]) -> str:
    """What makes two views one check: its kind and its documents, each in its role.

    Sorted, so the bidders of a BOQ run in another order are the same comparison; the role keeps
    the original and the revised of a comparison apart.
    """
    parts = sorted(f"{role}:{doc_id}" for doc_id, role in documents)
    return hashlib.sha256("|".join([kind, *parts]).encode()).hexdigest()


def find_run(
    session: Session, owner: str, kind: CheckKind, documents: Sequence[tuple[str, str]]
) -> CheckRun | None:
    """The check a person already has over these documents, if they have one."""
    return session.scalar(
        select(CheckRun).where(CheckRun.owner == owner, CheckRun.key == run_key(kind, documents))
    )


def record_run(
    session: Session,
    *,
    owner: str,
    kind: CheckKind,
    documents: Sequence[tuple[str, str]],
    params: dict[str, object],
    keep: Sequence[str] = (),
) -> None:
    """Remember that this check was opened, or update the row that says it was.

    ``documents`` are (id, role) pairs and ``params`` the options its URL carries. An option named
    in ``keep`` survives a view that carries none: a model's explanation of a comparison or review
    of an act is the answer to minutes of work, and opening the plain link again from a browser's
    history must not forget which job holds it. ``params`` is always replaced, never mutated - a
    plain JSON column does not see ``run.params[name] = value``.

    The whole write is one savepoint. A failure in it - two tabs inserting the same check, a
    locked database - rolls back that and only that: the report the person is looking at, and the
    audit row of looking at it, are in the transaction around it and are not touched.
    """
    try:
        with session.begin_nested():
            run = find_run(session, owner, kind, documents)
            now = datetime.now(UTC)
            if run is None:
                run = CheckRun(
                    owner=owner,
                    kind=kind,
                    key=run_key(kind, documents),
                    params=dict(params),
                    created_at=now,
                    last_opened_at=now,
                )
                seen: set[str] = set()
                for doc_id, role in documents:
                    if doc_id not in seen:
                        seen.add(doc_id)
                        run.documents.append(CheckRunDocument(document_id=doc_id, role=role))
                session.add(run)
            else:
                merged = dict(params)
                for name in keep:
                    if name not in merged and name in run.params:
                        merged[name] = run.params[name]
                run.params = merged
                run.last_opened_at = now
            session.flush()
            _trim(session, owner)
    except SQLAlchemyError as e:
        log.warning("check_run_not_recorded", kind=kind, error=type(e).__name__)


def _trim(session: Session, owner: str) -> None:
    stale = session.scalars(
        select(CheckRun.id)
        .where(CheckRun.owner == owner)
        .order_by(CheckRun.last_opened_at.desc(), CheckRun.id)
        .offset(MAX_RUNS_PER_OWNER)
    ).all()
    if stale:
        session.execute(delete(CheckRun).where(CheckRun.id.in_(stale)))


def runs_for_document(
    session: Session, owner: str, doc_id: str, *, limit: int = LISTED
) -> list[CheckRun]:
    """The checks a person has run that this document took part in, most recent first.

    A check one of whose documents has since been deleted is left out: it cannot be reproduced.
    (Deleting a document removes its checks as well; this is for a row that outlived that.)
    """
    runs = session.scalars(
        select(CheckRun)
        .join(CheckRunDocument, CheckRunDocument.run_id == CheckRun.id)
        .where(CheckRun.owner == owner, CheckRunDocument.document_id == doc_id)
        .order_by(CheckRun.last_opened_at.desc())
        .limit(limit)
        .options(selectinload(CheckRun.documents).selectinload(CheckRunDocument.document))
    ).all()
    return [
        run
        for run in runs
        if all(part.document.status != DocumentStatus.deleted for part in run.documents)
    ]


def forget_runs(session: Session, doc_id: str) -> None:
    """Drop every check the document took part in.

    One missing a file cannot be reproduced, and what was left of it would only point at a page
    that says so. The rows that tie the other documents to it go with it (ON DELETE CASCADE).
    """
    runs = select(CheckRunDocument.run_id).where(CheckRunDocument.document_id == doc_id)
    session.execute(delete(CheckRun).where(CheckRun.id.in_(runs)))
