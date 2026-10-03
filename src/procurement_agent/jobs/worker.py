"""Single background worker: parses documents, runs extractions, enforces retention.

One thread is deliberate — on a CPU-only box, Docling and the LLM would only fight each other.
All state lives in the database so the API can report progress and a restart loses nothing but
the currently running job (which is marked failed on startup).

Deletion and the worker race by design: a user may delete a document while its job runs. The
rule that keeps a deleted document deleted is an ordering on both sides:

* DELETE (and the retention sweep) commit ``deleted`` + ``cancelled`` *first*, then remove files.
* The worker writes its output *first*, then settles the job with a conditional
  ``UPDATE ... WHERE status = 'running'`` that also re-reads the documents under the same write
  lock. If the job was cancelled or a document deleted meanwhile, nothing is flipped and the
  output is removed.

Whichever side commits second cleans up after the first, so no interleaving leaves a result
file, a ``done`` job or a ``ready``/``failed`` status behind a deleted document. Plain
read-modify-write through the ORM is not enough on SQLite: pysqlite runs a SELECT outside any
transaction, so the read and the write are two separate moments.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, object_session

from procurement_agent.checks.aoa import scope_review_items
from procurement_agent.checks.aoa_llm import request_review
from procurement_agent.checks.compare_llm import request_explanations
from procurement_agent.config import Settings
from procurement_agent.db import checkpoint, session_scope
from procurement_agent.extraction.pipeline import (
    StoredDocument,
    compare_stored,
    load_chunks,
    load_document_text,
    result_dir,
    run_extraction,
    upload_path,
)
from procurement_agent.extraction.read_status import LLM_UNAVAILABLE, stored_extraction
from procurement_agent.extraction.schemas import FactType
from procurement_agent.history import forget_runs
from procurement_agent.ingestion import docling_service, read_quality
from procurement_agent.jobs.retention import delete_document_files, reconcile, remove_file
from procurement_agent.jobs.stuck import fail_stuck_parses, fail_unreadable
from procurement_agent.llm.base import LLMClient
from procurement_agent.models import Document, DocumentStatus, Job, JobStatus, UiSession
from procurement_agent.security.audit import audit, log
from procurement_agent.security.caches import forget_cached_text
from procurement_agent.security.safe_errors import safe_exc

_SWEEP_EVERY_S = 600
#: How long the loop waits for work before it beats the heartbeat and checks the sweep timer.
_IDLE_POLL_S = 30.0
#: An idle worker beats at least every ``_IDLE_POLL_S``; four missed beats means it is stuck.
IDLE_STALL_S = 120.0
#: LLM calls that can happen for one chunk between two progress callbacks (see ``healthy``).
_LLM_CALLS_PER_BEAT = 3
_STOP = object()
_ACTIVE = (JobStatus.queued, JobStatus.running)
_DELETED_MESSAGE = "document was deleted while the job ran"
#: How a job whose whole purpose is the model's answer ends when there was none.
_NO_ANSWER = f"{LLM_UNAVAILABLE}: the model did not answer"


class Worker:
    def __init__(self, settings: Settings, llm: LLMClient | None) -> None:
        self._s = settings
        self._llm = llm
        self._q: queue.Queue[object] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="pa-worker", daemon=True)
        self._last_sweep = 0.0
        self._stopping = threading.Event()
        self._heartbeat = time.monotonic()
        self._busy_since: float | None = None
        #: the document whose parse is running now, so a job that cannot be settled fails it too
        self._parsing_doc: str | None = None

    # --- lifecycle -----------------------------------------------------------------------------

    def start(self) -> None:
        self._recover()
        self._beat()
        self._thread.start()

    def stop(self, timeout: float | None = None) -> bool:
        """Ask the loop to exit and wait for it. Returns True once the thread has stopped.

        The event is what makes this prompt: the ``_STOP`` sentinel alone queues behind every
        pending job, so a busy queue used to outlive the join. Jobs still queued stay ``queued``
        in the database and are picked up again by ``_recover`` on the next start. The job that
        is running is allowed to finish, up to ``timeout``.
        """
        self._stopping.set()
        self._q.put(_STOP)
        if self._thread.is_alive():
            self._thread.join(self._s.worker_stop_timeout_s if timeout is None else timeout)
        return not self._thread.is_alive()

    def submit(self, job_id: str) -> None:
        self._q.put(job_id)

    @property
    def queue_size(self) -> int:
        return self._q.qsize()

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    @property
    def heartbeat_age(self) -> float:
        """Seconds since the loop (or the running job's progress callback) last showed life."""
        return time.monotonic() - self._heartbeat

    @property
    def busy(self) -> bool:
        return self._busy_since is not None

    def healthy(self) -> bool:
        """Alive and not stuck.

        A Docling conversion may legitimately run for its whole timeout without a progress
        callback, and so may the LLM work for one chunk - which is up to
        ``_LLM_CALLS_PER_BEAT`` calls (the answer, one retry after invalid JSON, a verifier
        question), each allowed the full LLM timeout. A busy worker gets the longer of the two
        plus the idle allowance before it counts as stalled. Anything tighter would make
        ``/ready`` flap during ordinary long jobs.
        """
        if not self.is_alive:
            return False
        limit = IDLE_STALL_S
        if self.busy:
            limit += max(_LLM_CALLS_PER_BEAT * self._s.llm_timeout_s, self._s.docling_timeout_s)
        return self.heartbeat_age < limit

    # --- internals -----------------------------------------------------------------------------

    def _beat(self) -> None:
        self._heartbeat = time.monotonic()

    def _recover(self) -> None:
        """Jobs left running by a previous process cannot finish: fail them, re-queue queued.

        A document the dead job was parsing would otherwise sit in ``parsing`` for ever, and
        the UI polls a parsing document until it changes.
        """
        now = datetime.now(UTC)
        with session_scope() as s:
            for job in s.scalars(select(Job).where(Job.status == JobStatus.running)):
                job.status = JobStatus.failed
                job.message = "interrupted by restart"
                job.finished_at = now
            for doc in s.scalars(select(Document).where(Document.status == DocumentStatus.parsing)):
                doc.status = DocumentStatus.failed
                doc.error = "interrupted by restart"
            queued = [j.id for j in s.scalars(select(Job).where(Job.status == JobStatus.queued))]
        for jid in queued:
            self._q.put(jid)

    def _run(self) -> None:
        while True:
            self._beat()
            try:
                item = self._q.get(timeout=_IDLE_POLL_S)
            except queue.Empty:
                item = None
            if item is _STOP or self._stopping.is_set():
                return
            if isinstance(item, str):
                self._run_one(item)
            if time.monotonic() - self._last_sweep > _SWEEP_EVERY_S:
                self._last_sweep = time.monotonic()
                try:
                    self.sweep_expired()
                except Exception as e:
                    log.error("sweep_failed", error=safe_exc(e))

    def _run_one(self, job_id: str) -> None:
        """One job, and nothing it raises - including the failure bookkeeping - kills the loop."""
        self._busy_since = time.monotonic()
        self._beat()
        try:
            try:
                self._process(job_id)
            except Exception as e:
                log.error("job_crashed", job_id=job_id, error=safe_exc(e))
                self._finish(job_id, JobStatus.failed, f"internal error: {type(e).__name__}")
        except Exception as e:  # e.g. "database is locked" while recording the failure
            log.error("job_finish_failed", job_id=job_id, error=safe_exc(e))
        finally:
            self._busy_since = None
            self._parsing_doc = None
            self._beat()

    def _set_progress(self, job_id: str, pct: int, msg: str) -> None:
        self._beat()
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job is not None and job.status == JobStatus.running:
                job.progress = pct
                job.message = msg

    def _settle(
        self,
        job_id: str,
        status: JobStatus,
        msg: str | None,
        *,
        documents: Sequence[str] = (),
        apply: Callable[[Session], None] | None = None,
    ) -> bool:
        """Move an active job to its final state, atomically with its documents' state.

        The conditional UPDATE is the claim: it refuses a job that is no longer active (so a
        ``cancelled`` is never overwritten) and, once it has run, this transaction holds
        SQLite's write lock - the documents read next cannot be deleted before we commit.
        Returns False when the job had been cancelled or one of ``documents`` deleted; the
        caller then owns removing whatever it wrote.
        """
        with session_scope() as s:
            claimed = s.execute(
                update(Job)
                .where(Job.id == job_id, Job.status.in_(_ACTIVE))
                .values(status=status, message=msg, finished_at=datetime.now(UTC))
            )
            if getattr(claimed, "rowcount", 0) != 1:
                return False
            job = s.get(Job, job_id)
            if job is None:  # pragma: no cover - the UPDATE just matched it
                return False
            docs = [s.get(Document, d) for d in documents]
            live = all(d is not None and d.status != DocumentStatus.deleted for d in docs)
            if not live:
                job.status = JobStatus.cancelled
                job.message = _DELETED_MESSAGE
            else:
                if status == JobStatus.done:
                    job.progress = 100
                if apply is not None:
                    apply(s)
            audit(
                s,
                actor="worker",
                action=f"job.{job.kind}",
                outcome="ok" if job.status == JobStatus.done else "error",
                document_id=job.document_id,
                job_id=job.id,
                detail=job.message,
            )
            return live

    def _finish(self, job_id: str, status: JobStatus, msg: str | None) -> None:
        document = self._parsing_doc
        if status != JobStatus.failed or document is None:
            self._settle(job_id, status, msg)
            return
        # A parse that cannot be settled must not leave its document "parsing" for ever: the
        # page polls it, and nothing else would ever change it (only a restart's ``_recover``).
        log.warning("parse_job_failed_unsettled", job_id=job_id, document_id=document)

        def fail_document(s: Session) -> None:
            doc = s.get(Document, document)
            if doc is not None and doc.status == DocumentStatus.parsing:
                doc.status = DocumentStatus.failed
                doc.error = (msg or "could not be read")[:500]

        self._settle(job_id, status, msg, documents=(document,), apply=fail_document)

    def _discard(self, documents: Sequence[tuple[str, str]], output: Path | None = None) -> None:
        """Remove what a job wrote after it lost the race with a deletion.

        A deleted document's files all go (its DELETE may have run before this job's write);
        a live one only loses this job's own output file.
        """
        if output is not None:
            remove_file(output)
        with session_scope() as s:
            gone = [
                (doc_id, kind)
                for doc_id, kind in documents
                if (d := s.get(Document, doc_id)) is None or d.status == DocumentStatus.deleted
            ]
        for doc_id, kind in gone:
            delete_document_files(self._s, doc_id, kind)
        log.info("job_output_discarded", documents=[d for d, _ in documents])

    def _write_output(self, doc_id: str, job_id: str, payload: str) -> Path:
        out_dir = result_dir(self._s, doc_id) / "jobs"
        out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        out = out_dir / f"{job_id}.json"
        out.write_text(payload)
        out.chmod(0o600)
        return out

    def _process(self, job_id: str) -> None:
        now = datetime.now(UTC)
        with session_scope() as s:
            # Claim first (see _settle): after this UPDATE the reads below cannot go stale.
            claimed = s.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == JobStatus.queued)
                .values(status=JobStatus.running, started_at=now)
            )
            if getattr(claimed, "rowcount", 0) != 1:
                return
            job = s.get(Job, job_id)
            if job is None:  # pragma: no cover - the UPDATE just matched it
                return
            kind, doc_id, params = job.kind, job.document_id, dict(job.params)
            doc = s.get(Document, doc_id)
            if doc is None or doc.status == DocumentStatus.deleted:
                job.status = JobStatus.cancelled
                job.message = "document no longer exists"
                job.finished_at = now
                return
            if kind == "parse":
                doc.status = DocumentStatus.parsing
                self._parsing_doc = doc_id
            doc_kind = doc.kind

        log.info("job_start", job_id=job_id, kind=kind, document_id=doc_id)
        if kind == "parse":
            self._parse(job_id, doc_id, doc_kind)
        elif kind == "extract":
            self._extract(job_id, doc_id, doc_kind, params)
        elif kind == "compare_explain":
            self._compare_explain(job_id, doc_id, params)
        elif kind == "aoa_review":
            self._aoa_review(job_id, doc_id, params)
        else:
            self._finish(job_id, JobStatus.failed, f"unknown job kind {kind!r}")

    def _parse(self, job_id: str, doc_id: str, doc_kind: str) -> None:
        path = upload_path(self._s, doc_id, doc_kind)
        try:
            if doc_kind == "xlsx":
                chunks = load_chunks(self._s, doc_id, doc_kind)  # validates it is readable
                read_quality.settle_workbook(self._s, doc_id, chunks)  # refuses one with no text
                page_count = None
                self._set_progress(job_id, 90, f"{len(chunks)} table chunks")
            else:
                self._set_progress(job_id, 10, "converting")
                result = docling_service.convert(path, self._s)
                docling_service.save_document(result.document, result_dir(self._s, doc_id))
                quality = read_quality.settle_conversion(
                    self._s, doc_id, doc_kind, result, upload=path
                )
                page_count = quality.pages_total
                self._set_progress(job_id, 90, "saved")
        except Exception as e:
            # The exception's own text may quote the document; only its kind is kept, in the log
            # and in the rows the user and the audit trail read (see ``security.safe_errors``).
            error = read_quality.parse_error(e)
            log.warning("parse_failed", document_id=doc_id, error=error)

            def mark_failed(s: Session) -> None:
                doc = s.get(Document, doc_id)
                if doc is not None:
                    doc.status = DocumentStatus.failed
                    doc.error = error[:500]

            if not self._settle(
                job_id, JobStatus.failed, error[:200], documents=(doc_id,), apply=mark_failed
            ):
                self._discard([(doc_id, doc_kind)])
            return

        def mark_ready(s: Session) -> None:
            doc = s.get(Document, doc_id)
            if doc is not None:
                doc.status = DocumentStatus.ready
                doc.parsed_at = datetime.now(UTC)
                if page_count:
                    doc.page_count = page_count

        if not self._settle(job_id, JobStatus.done, "ready", documents=(doc_id,), apply=mark_ready):
            self._discard([(doc_id, doc_kind)])

    def _extract(self, job_id: str, doc_id: str, doc_kind: str, params: dict[str, object]) -> None:
        targets_raw = params.get("targets")
        targets = [FactType(t) for t in targets_raw] if isinstance(targets_raw, list) else None
        q = params.get("question")
        question = q if isinstance(q, str) else None
        fast = bool(params.get("fast", False))
        # A job queued without the flag runs rules-only: the model is opt-in (api/extraction.py)
        use_llm = bool(params.get("use_llm", False)) or question is not None

        notes: list[str] = []
        try:
            chunks = load_chunks(self._s, doc_id, doc_kind, notes=notes)
            quality = read_quality.read_quality(
                self._s, StoredDocument(id=doc_id, kind=doc_kind, name="")
            )
        except Exception as e:  # reported to the user, never swallowed
            failure = safe_exc(e)
            log.warning("chunking_failed", job=job_id, document=doc_id, error=failure)
            if isinstance(e, read_quality.StoredParseError):
                fail_unreadable(doc_id)  # its reading is gone: the document is not 'ready' any more
            self._finish(job_id, JobStatus.failed, f"could not read the document: {failure}"[:200])
            return
        result = run_extraction(
            document_id=doc_id,
            chunks=chunks,
            settings=self._s,
            llm=self._llm if use_llm else None,
            targets=targets,
            question=question,
            fast=fast,
            progress=lambda pct, msg: self._set_progress(job_id, pct, msg),
        )
        # What the workbook reader left out belongs in front of the user, not only in the log.
        result.warnings[:0] = [f"not read (size limit): {n}" for n in notes]
        # Each passage sent to the model is one call; one that raised is not counted. A model
        # that was asked and did not answer leaves the rules' facts - kept, and said to be so.
        unanswered = result.chunks_selected - result.llm_calls if result.model is not None else 0
        out = self._write_output(
            doc_id,
            job_id,
            stored_extraction(result, quality, unanswered=max(unanswered, 0)).model_dump_json(),
        )
        msg = f"{len(result.facts)} facts, {result.llm_calls} LLM calls"
        apply: Callable[[Session], None] | None = None
        if unanswered > 0:
            msg += f"; {LLM_UNAVAILABLE}: the model was unavailable for {unanswered} passage(s)"

            def mark_degraded(s: Session) -> None:
                job = s.get(Job, job_id)
                if job is not None:
                    job.params = {**job.params, "degraded": True}

            apply = mark_degraded
        if not self._settle(job_id, JobStatus.done, msg, documents=(doc_id,), apply=apply):
            self._discard([(doc_id, doc_kind)], out)

    def _compare_explain(self, job_id: str, doc_id: str, params: dict[str, object]) -> None:
        """Ask the model about the changes the rules could not classify in one comparison.

        The job is keyed on the right-hand document because ``Job.document_id`` is a single
        foreign key - the same reason a BOQ run has no job row at all - and the left-hand id
        travels in ``params``. Only the model's answers are stored: the comparison itself is
        recomputed whenever the page is opened, so a stored run can never pin the report to an
        older version of the rules. Deleting the *left* document does not cancel this job (it
        hangs off the right one), which is why both are re-checked when it settles.
        """
        left_id = params.get("left")
        if not isinstance(left_id, str):
            self._finish(job_id, JobStatus.failed, "no left document recorded on the job")
            return
        if self._llm is None:
            self._finish(job_id, JobStatus.failed, "no LLM backend is configured")
            return

        with session_scope() as s:
            left = s.get(Document, left_id)
            right = s.get(Document, doc_id)
            pair = (
                None
                if left is None
                or right is None
                or DocumentStatus.deleted in (left.status, right.status)
                else [
                    (left.id, left.kind, left.display_name),
                    (right.id, right.kind, right.display_name),
                ]
            )
        if pair is None:
            self._finish(job_id, JobStatus.cancelled, "a document no longer exists")
            return

        self._set_progress(job_id, 10, "reading both documents")
        try:
            report = compare_stored(
                self._s,
                StoredDocument(id=pair[0][0], kind=pair[0][1], name=pair[0][2]),
                StoredDocument(id=pair[1][0], kind=pair[1][1], name=pair[1][2]),
            )
        except Exception as e:
            failure = safe_exc(e)
            log.warning("compare_failed", job=job_id, error=failure)
            self._finish(job_id, JobStatus.failed, f"could not compare: {failure}"[:200])
            return

        pending = len(report.unexplained)
        self._set_progress(job_id, 30, f"asking the model about {pending} change(s)")
        stored = request_explanations(
            report,
            self._llm,
            model=self._s.llm_fast_model,
            max_hunks=self._s.compare_llm_max_hunks,
        )
        if stored.error is not None:  # the model's answer was the whole job, and it did not come
            log.warning("compare_explain_unanswered", job=job_id, error=stored.error)
            self._finish(job_id, JobStatus.failed, _NO_ANSWER)
            return
        out = self._write_output(doc_id, job_id, stored.model_dump_json())
        msg = f"{len(stored.items)} change(s) described"
        if not self._settle(job_id, JobStatus.done, msg, documents=(pair[0][0], pair[1][0])):
            self._discard([(pair[0][0], pair[0][1]), (pair[1][0], pair[1][1])], out)

    def _aoa_review(self, job_id: str, doc_id: str, params: dict[str, object]) -> None:
        """Ask the model about the notes an act check's scope rules could not decide.

        Keyed on the act, with the contract in ``params``, for the reason ``_compare_explain``
        gives - so deleting the contract does not cancel this job, and both documents are
        re-checked when it settles. The notes are read here exactly as the report reads them
        (``aoa.scope_review_items``); only the model's answers are stored, and the report
        applies them to the rules it recomputes on every view.
        """
        contract_id = params.get("contract")
        if not isinstance(contract_id, str):
            self._finish(job_id, JobStatus.failed, "no contract recorded on the job")
            return
        if self._llm is None:
            self._finish(job_id, JobStatus.failed, "no LLM backend is configured")
            return

        with session_scope() as s:
            act = s.get(Document, doc_id)
            contract = s.get(Document, contract_id)
            pair = (
                None
                if act is None
                or contract is None
                or DocumentStatus.deleted in (act.status, contract.status)
                else [(act.id, act.kind), (contract.id, contract.kind)]
            )
        if pair is None:
            self._finish(job_id, JobStatus.cancelled, "a document no longer exists")
            return

        self._set_progress(job_id, 10, "reading both documents")
        try:
            items = scope_review_items(
                aoa_text=load_document_text(self._s, *pair[0]),
                contract_text=load_document_text(self._s, *pair[1]),
            )
        except Exception as e:
            failure = safe_exc(e)
            log.warning("aoa_review_read_failed", job=job_id, error=failure)
            self._finish(job_id, JobStatus.failed, f"could not read the documents: {failure}"[:200])
            return

        self._set_progress(job_id, 30, f"asking the model about {len(items)} note(s)")
        stored = request_review(
            items,
            self._llm,
            aoa_document_id=pair[0][0],
            contract_document_id=pair[1][0],
            model=self._s.llm_fast_model,
            max_items=self._s.aoa_llm_max_items,
            max_chars=self._s.aoa_llm_max_chars,
        )
        if stored.error is not None:  # the model's answer was the whole job, and it did not come
            log.warning("aoa_review_unanswered", job=job_id, error=stored.error)
            self._finish(job_id, JobStatus.failed, _NO_ANSWER)
            return
        out = self._write_output(doc_id, job_id, stored.model_dump_json())
        msg = f"{len(stored.items)} answer(s) about {len(stored.asked)} note(s)"
        if not self._settle(job_id, JobStatus.done, msg, documents=(pair[0][0], pair[1][0])):
            self._discard(pair, out)

    # --- retention -----------------------------------------------------------------------------

    def sweep_expired(self) -> int:
        """Delete documents past retention, forget expired sign-in sessions, and remove what an
        earlier deletion left behind.

        Status first, files second - the same order as DELETE, for the reason in the module
        docstring. A file that will not go does not stop the others, and is retried: the
        reconciler removes the files of every deleted document, not only of those deleted now.
        """
        now = datetime.now(UTC)
        with session_scope() as s:
            expired = list(
                s.scalars(
                    select(Document).where(
                        Document.expires_at.is_not(None),
                        Document.expires_at < now,
                        Document.status != DocumentStatus.deleted,
                    )
                )
            )
            ids = [(d.id, d.kind) for d in expired]
            for d in expired:
                mark_deleted(d)
                audit(s, actor="worker", action="document.retention_delete", document_id=d.id)
            s.execute(delete(UiSession).where(UiSession.expires_at < now))
        incomplete = 0
        for doc_id, kind in ids:
            # Zeroing a large file takes a while and the sweep runs outside a job, so beat per
            # document: a big first sweep after downtime must not read as a stalled worker.
            self._beat()
            if not delete_document_files(self._s, doc_id, kind):
                incomplete += 1
        if ids:
            log.info("retention_sweep", deleted=len(ids), incomplete=incomplete)
        self._beat()
        reconcile(self._s)
        fail_stuck_parses(self._s, running_too=not self.busy)
        checkpoint()  # a deleted row must not stay readable in the write-ahead log
        return len(ids)


def mark_deleted(doc: Document) -> None:
    """The database half of a deletion; commit it before calling ``delete_document_files``."""
    doc.status = DocumentStatus.deleted
    doc.display_name = "[deleted]"
    for j in doc.jobs:
        if j.status in _ACTIVE:
            j.status = JobStatus.cancelled
            j.message = "document deleted"
            j.finished_at = datetime.now(UTC)
        j.params = {}  # a question the user typed about a document that no longer exists
    forget_cached_text()
    # The checks it took part in cannot be reproduced without it, so their records go too.
    session = object_session(doc)
    if session is not None:
        forget_runs(session, doc.id)


def retention_expiry(settings: Settings) -> datetime | None:
    if settings.retention_days <= 0:
        return None
    return datetime.now(UTC) + timedelta(days=settings.retention_days)
