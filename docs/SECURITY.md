# Security & data-handling policy

This system processes confidential procurement documents. This page states what it does with
them, what it never does, and how that is enforced. It is written to be handed to the client's
IT/security team as-is.

## 1. Where data goes

| Data | Where it lives | Leaves the server? |
|---|---|---|
| Uploaded documents | `/data/uploads/<uuid>.<ext>`, mode `0600`, owned by the unprivileged `app` user | **Never** |
| Parsed structure (Docling JSON) and extraction results | `/data/results/<uuid>/`, mode `0600` | **Never** |
| Metadata (owner, size, SHA-256, status, timestamps) and the audit trail | SQLite `/data/app.db`, mode `0600` | **Never** |
| LLM prompts | HTTP to the Ollama container over a Docker **internal** network (no route to the internet) | **Never** |
| Logs | stdout of the container (JSON), content-free — see §5 | Only if the operator ships them |

The `/data` volume should be placed on an encrypted disk (LUKS or equivalent). The application
does its own best-effort zeroing before deleting files, but disk encryption is the real
protection against forensic recovery.

## 2. Network isolation (enforced, not just configured)

* `docker-compose.yml` puts `app` and `ollama` on a network with `internal: true`. Containers on
  such a network have **no default route**; an outbound connection fails at the kernel level.
* Ollama publishes no port. Only the app port is published, and only on `127.0.0.1`; a reverse
  proxy with the company's TLS certificate sits in front of it.
* Every ML library is pinned to offline mode by environment (`HF_HUB_OFFLINE=1`,
  `TRANSFORMERS_OFFLINE=1`, telemetry disabled) — set in the image and again in `config.py`
  before any of those libraries is imported.
* The Ollama URL must resolve to a private/internal host; the settings validator refuses anything
  else at startup.
* The HTTP client used for the LLM ignores proxy environment variables (`trust_env=False`).
* Models are downloaded **once, on a connected machine**, by `scripts/prefetch_models.sh`, and
  copied to the server. The server never needs internet access.

Verification: `docker compose exec app python -c "import urllib.request; urllib.request.urlopen('https://example.com', timeout=3)"`
must fail with a network error.

## 3. Untrusted input

Every upload is treated as hostile (`security/upload_guard.py`):

* extension allow-list (`.pdf .docx .xlsx`); legacy `.doc/.xls` and macro-enabled
  `.docm/.xlsm/.xlsb/.dotm` are rejected outright;
* magic-byte check must match the claimed type; a `.docx` that is really a `.xlsx` is rejected;
* size limit (default 50 MB, enforced while streaming, and again by `Content-Length`), page limit
  (default 300), password-protected PDFs rejected;
* OOXML archives: entry count, total inflated size and per-entry compression ratio limits
  (zip-bomb), path-traversal names rejected, encrypted entries rejected, any VBA part or
  macro-enabled content type rejected;
* the user's file name is used for display only (sanitised, NFC-normalised, control characters
  stripped); on disk files are named by UUID;
* spreadsheets are read with `openpyxl` in `data_only` mode: formulas are **not evaluated**,
  macros cannot run. Values exported to Excel are prefixed to neutralise formula injection.

## 4. Authentication, authorisation, audit

* API keys: 256-bit random, shown once, stored as SHA-256; constant-time comparison; revocable
  (`python -m procurement_agent.cli`). The built-in UI stores the key in an `HttpOnly`,
  `SameSite=Strict` cookie (`Secure` in prod) and refuses cookie-authenticated state changes from
  other origins (`Sec-Fetch-Site` / `Origin` check).
* Every document and job is owner-scoped. Foreign or unknown ids return **404**, so ids cannot
  be enumerated.
* Rate limiting (120 requests/minute per client address).
* Append-only audit table + JSON log line for: upload (accepted/denied), extraction start,
  result read (JSON/XLSX), delete, retention delete, login (ok/denied). Fields: actor, action,
  outcome, document id, job id, client IP, timestamp.
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
`no-new-privileges`, tmpfs `/tmp`, memory and PID limits. One worker thread processes jobs, so
a malicious document cannot fan out CPU/memory consumption. Docling conversion has a timeout.

## 8. Retention

Documents expire `PA_RETENTION_DAYS` (default 7) after upload. A sweep runs every 10 minutes
and on `python -m procurement_agent.cli sweep`. Users can delete immediately from the UI or via
`DELETE /api/v1/documents/{id}`. Deletion removes the upload, the parsed structure and every
result, and cancels pending jobs; the metadata row is kept (name replaced by `[deleted]`) so the
audit trail stays consistent.

## 9. Supply chain

* `uv.lock` pins every dependency; `pip-audit` and `bandit` run in pre-commit; ruff + mypy
  (strict) on all code.
* A pre-commit hook blocks any `.pdf/.docx/.xlsx` outside `tests/fixtures/` from being
  committed; all fixtures are **synthetic** (generated by `scripts/gen_synthetic_samples.py`).
  No client document ever enters the repository.
* Dependencies with native code (Docling, PyTorch CPU, pypdfium2, Pillow) are the main attack
  surface for malformed documents; they are kept current via the lock file and run inside the
  hardened container described above.

## 10. Known limitations / roadmap

* API-key auth is phase 1; integration with the company directory (LDAP/AD or OIDC) is planned.
* Application-level encryption of files at rest is not implemented; rely on disk encryption.
* OCR (Tesseract) runs only inside the container image; scanned PDFs processed on a host
  without Tesseract yield no text and the API reports that in the document status.
