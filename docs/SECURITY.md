# Security & data-handling policy

This system processes confidential procurement documents. This page states what it does with
them, what it never does, and how that is enforced. It is written to be handed to the client's
IT/security team as-is.

## 1. Where data goes

| Data | Where it lives | Leaves the server? |
|---|---|---|
| Uploaded documents | `/data/uploads/<uuid>.<ext>`, mode `0600`, owned by the unprivileged `app` user | **Never** |
| Parsed structure (Docling JSON) and extraction results | `/data/results/<uuid>/`, mode `0600` | **Never** |
| Metadata (owner, size, SHA-256, status, timestamps), the audit trail, and the list of checks run (which check, over which document ids, with which options — never document content; removed with the documents) | SQLite `/data/app.db`, mode `0600` | **Never** |
| LLM prompts | HTTP to the Ollama container over a Docker **internal** network (no route to the internet) | **Never** |
| Logs | stdout of the container (JSON), content-free — see §5 | Only if the operator ships them |

The `/data` volume should be placed on an encrypted disk (LUKS or equivalent). The application
does its own best-effort zeroing before deleting files, but disk encryption is the real
protection against forensic recovery.

## 2. Network isolation (enforced, not just configured)

* `docker-compose.yml` puts `app` and `ollama` on a network with `internal: true`. Containers on
  such a network have **no default route**; an outbound connection fails at the kernel level.
* Neither Ollama nor the app publishes a port. Docker does not publish ports from a container
  whose only network is `internal: true`, so a small relay container (`proxy`, Caddy, plain
  HTTP, no state) joins the internal network and a second one and is the only container with a
  published port, bound to `127.0.0.1:8000`. The company's TLS reverse proxy sits in front of
  that. The relay's second network has outbound NAT disabled, so it cannot reach the internet
  either; the app and Ollama have no route at all.
* Every ML library is pinned to offline mode by environment (`HF_HUB_OFFLINE=1`,
  `TRANSFORMERS_OFFLINE=1`, telemetry disabled) — set in the image and again in `config.py`
  before any of those libraries is imported.
* The Ollama URL must resolve to a private/internal host; the settings validator refuses anything
  else at startup.
* The HTTP client used for the LLM ignores proxy environment variables (`trust_env=False`).
* Models are downloaded **once, on a connected machine**, by `scripts/prefetch_models.sh`, and
  copied to the server. The server never needs internet access.

Verification: `docker compose exec app python -c "import urllib.request; urllib.request.urlopen('https://example.com', timeout=3)"`
must fail with a network error (measured: `Temporary failure in name resolution`).

## 3. Untrusted input

Every upload is treated as hostile (`security/upload_guard.py`):

* extension allow-list (`.pdf .docx .xlsx`); legacy `.doc/.xls` and macro-enabled
  `.docm/.xlsm/.xlsb/.dotm` are rejected outright;
* magic-byte check must match the claimed type; a `.docx` that is really a `.xlsx` is rejected;
* size limit per file (default 50 MB, enforced while streaming) and a request-body cap of two
  files plus 1 MB, counted on the bytes that actually arrive, so a chunked body without
  `Content-Length` is cut off too; a multipart body carrying no credential at all is refused
  before it is read. Page limit (default 300); password-protected PDFs rejected;
* OOXML archives: entry count, total inflated size and per-entry compression ratio limits
  (zip-bomb), path-traversal names rejected, encrypted entries rejected, any VBA part or
  macro-enabled content type rejected;
* the user's file name is used for display only (sanitised, NFC-normalised, control characters
  stripped); on disk files are named by UUID;
* spreadsheets are read with `openpyxl` in `data_only` mode: formulas are **not evaluated**,
  macros cannot run. Values exported to Excel are prefixed to neutralise formula injection.

## 4. Authentication, authorisation, audit

* API keys: 256-bit random, shown once, stored as SHA-256; constant-time comparison; revocable
  (`python -m procurement_agent.cli`).
* Browser sessions are server-side. Signing in to the UI checks the key once and issues a random
  session token; the `HttpOnly`, `SameSite=Strict` cookie (`Secure` in prod) carries only that
  token and the database only its SHA-256. A session expires on the server after
  `PA_UI_SESSION_HOURS` (default 8), ends on logout, and stops working when its API key is
  revoked. (Earlier builds put the API key itself in the cookie; upgrading signs every user
  out once.) Cookie-authenticated state changes from other origins are refused
  (`Sec-Fetch-Site` / `Origin` check).
* Every document and job is owner-scoped. Foreign or unknown ids return **404**, so ids cannot
  be enumerated.
* Rate limiting: fixed window, default 120 requests/minute, keyed by **client address** — not
  by the key or cookie presented, which the caller chooses. `/health`, `/ready` and `/static`
  are exempt. The address is the one uvicorn derives from `X-Forwarded-For`, which it accepts
  only from `FORWARDED_ALLOW_IPS` (the image default is loopback only; compose sets it to the
  relay). If the reverse proxy does not forward the client address, every user shares one
  bucket.
* Each user may hold at most `PA_MAX_ACTIVE_JOBS_PER_USER` (default 20) queued or running
  jobs, so one script cannot starve the single worker for everybody else.
* Append-only audit table + JSON log line for: upload (accepted/denied), extraction start,
  result read (JSON/XLSX), a document's content shown as text (`document.content_view`, the
  "what was read" page) or as a page image, check results and exports, delete, retention
  delete, UI login
  (ok/denied) and logout, refused cross-origin requests. Fields: actor, action, outcome,
  document id, job id, client IP, timestamp.
* A dev-only bypass key (`PA_DEV_API_KEY`) exists for local testing; `PA_ENV=prod` **refuses to
  start** if it is set.

## 5. Logging

Log lines never contain document text, extracted values, user-chosen file names or API keys.
A structlog processor redacts anything that looks like a key. Docling/LLM errors are truncated
and typed (`ConversionError`, `LLMError`) before they reach logs or API responses.

## 6. The LLM is a tool, not an agent

* The model receives document text **only as data**, inside `<document>` delimiters, with a
  system prompt that says instructions inside the document must be ignored.
* It has **no tools**, no network, no file access; it can only return JSON.
* Output is grammar-constrained to a JSON schema by Ollama and validated again with Pydantic.
* A fact is accepted **only if its supporting quote occurs verbatim in the document chunk**.
  Hallucinated values are dropped and counted in the result's warnings.
* Generation is deterministic (`temperature=0`, fixed seed).
* Facts that should be unique (contract date, number, total, …) but come back with several
  different values are flagged `conflict=true` rather than silently picked.
* Everything that can be extracted deterministically (amounts, dates, ID codes, percentages,
  durations, contract numbers) is extracted by regular expressions first; the LLM sees only the
  chunks that scored as relevant, never the whole document.

## 7. Process hardening

Runtime container: non-root user, read-only root filesystem, `cap_drop: ALL`,
`no-new-privileges`, tmpfs `/tmp` (library caches are pointed there), memory and PID limits,
an init process. The relay runs as `nobody` with the same restrictions (it keeps only
`NET_BIND_SERVICE`, which its binary's file capability needs to execute at all). One worker
thread processes jobs, so a malicious document cannot fan out CPU/memory consumption. Docling
conversion has a timeout.

Health: `/health` is liveness only. `/ready` returns 503 when the database does not answer or
the job worker is dead or has not shown progress for longer than its longest legitimate step;
the container healthcheck uses `/ready`. Neither endpoint needs a key or reveals configuration.
On shutdown the worker is given `PA_WORKER_STOP_TIMEOUT_S` to finish its job; a job cut off
is failed on the next start instead of hanging in `running`.

## 8. Retention

Documents expire `PA_RETENTION_DAYS` (default 7) after upload. A sweep runs every 10 minutes
and on `python -m procurement_agent.cli sweep`. Users can delete immediately from the UI or via
`DELETE /api/v1/documents/{id}`. Deletion removes the upload, the parsed structure and every
result, and cancels pending jobs; the metadata row is kept (name replaced by `[deleted]`) so the
audit trail stays consistent.

## 9. Supply chain

* `uv.lock` pins every dependency, and CI refuses a lock that disagrees with `pyproject.toml`.
  `bandit`, ruff and mypy (strict) run in pre-commit, `scripts/check.sh` and CI; `pip-audit`
  (against exactly what the lock pins) and `gitleaks` (full history) run in CI and in
  `scripts/check.sh` when it has network access. Container base images, Ollama and the relay
  are pinned by version; Ollama and the relay also by digest.
* A pre-commit hook blocks any `.pdf/.docx/.xlsx` outside `tests/fixtures/` from being
  committed; all fixtures are **synthetic** (generated by `scripts/gen_synthetic_samples.py`).
  No client document ever enters the repository.
* Dependencies with native code (Docling, PyTorch CPU, pypdfium2, Pillow) are the main attack
  surface for malformed documents; they are kept current via the lock file and run inside the
  hardened container described above.

## 10. Known limitations / roadmap

* API-key auth is phase 1; integration with the company directory (LDAP/AD or OIDC) is planned.
* The rate limiter keeps its counters in process memory (they reset on restart). The app runs
  one uvicorn worker by design; running several would multiply the effective limit.
* Application-level encryption of files at rest is not implemented; rely on disk encryption.
* OCR (Tesseract) runs only inside the container image; scanned PDFs processed on a host
  without Tesseract yield no text and the API reports that in the document status.
