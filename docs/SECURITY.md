# Security & data-handling policy

This system processes confidential procurement documents. This page states what it does with
them, what it never does, how that is enforced, and - in §10 - what it does **not** protect
against. It is written to be handed to the client's IT/security team as-is. Every claim below is
either a mechanism with a test that fails when it is removed, or is listed as a limit.

## 1. Where data goes

| Data | Where it lives | Leaves the server? |
|---|---|---|
| Uploaded documents | `/data/uploads/<uuid>.<ext>`, mode `0600`, owned by the unprivileged `app` user | **Never** |
| Parsed structure (Docling JSON), extraction results, and the outputs of explain/review jobs | `/data/results/<uuid>/`, mode `0600` | **Never** |
| Metadata (owner, size, SHA-256, status, timestamps), the audit trail, and the list of checks run (which check, over which document ids, with which options — never document content; removed with the documents) | SQLite `/data/app.db` (and its `-wal`/`-shm` files), mode `0600`; the directory is `0700` | **Never** |
| Copies of the database taken before a schema upgrade, with file names, typed questions and options and document hashes blanked (§8) | `/data/db-backups/`, files `0600`, directory `0700`; removed `PA_DB_BACKUP_KEEP_DAYS` (default 3) days after they were made | **Never** |
| LLM prompts | HTTP to the Ollama container over a Docker **internal** network (no route to the internet) | **Never** |
| Logs | stdout of the container (JSON), content-free — see §5 | Only if the operator ships them |

The `/data` volume should be placed on an encrypted disk (LUKS or equivalent). The application
overwrites files before deleting them, but on journaling, copy-on-write and flash storage that
cannot be guaranteed: disk encryption is the real protection against forensic recovery (§8, §10).

## 2. Network isolation (enforced, not just configured)

Three independent layers, then a proof. Each has a test (§11).

**The kernel / deployment.**

* `docker-compose.yml` has two networks with `internal: true`: `internal` (relay ↔ app) and `llm`
  (app ↔ Ollama). Containers on such a network have **no default route**; an outbound connection
  fails in the kernel. The relay cannot reach Ollama and Ollama cannot reach the relay.
* No container publishes a port except the small relay (`proxy`, Caddy, plain HTTP, no state),
  bound to `127.0.0.1:8000`; the company's TLS reverse proxy sits in front of that. The relay's
  second network (`edge`) has outbound NAT disabled, so its TCP connections to the outside time
  out (measured: `nc 1.1.1.1:80` fails after the 4 s allowed).
* The relay cannot resolve public names either. Docker's embedded resolver answers the names of
  other containers itself and forwards the rest to the host's resolvers, and on `edge`, which is
  routable, that worked: `nslookup example.com` answered from the relay while every TCP
  connection timed out. A lookup can carry data out in the name it asks for, so the relay has
  `dns: [127.0.0.1]` - an upstream where nothing listens. Measured on Docker 29.7: from the
  relay, `app` still resolves and a public name ends in SERVFAIL in 0.1 s; from the app and from
  Ollama, which sit on internal networks only, a public name gives `Temporary failure in name
  resolution`, as before. `tests/unit/test_relay_policy.py` checks the setting. Docker refuses
  `gateway_mode_ipv4=isolated` (no route at all) on a network that is not internal, and a
  published port needs one, so the relay keeps a default route; what stops it is the missing NAT.
* The relay's own logs hold no header and no query string (§5): Caddy would otherwise have
  written the caller's `X-Api-Key` in clear into its error log.
* Ollama runs with `OLLAMA_NO_CLOUD=1`: its hosted-model features (remote inference, web search)
  are off, so a `...-cloud` model cannot forward a prompt to ollama.com through the local server.
* The app answers only to the host names in `PA_ALLOWED_HOSTS` (production refuses to start
  without it; development allows loopback). A request that names another host - a DNS-rebinding
  page, a forgotten tunnel - is refused with 400, before anything else runs.

**The process.**

* Every ML library is pinned to offline mode by environment (`HF_HUB_OFFLINE`,
  `TRANSFORMERS_OFFLINE`, `HF_HUB_DISABLE_IMPLICIT_TOKEN`, telemetry opt-outs) by
  `procurement_agent/__init__.py` **assigning** them, before any of those libraries can be
  imported through any entry point - so neither import order nor a `.env`/`env_file` value can
  turn them off (`tests/unit/test_offline_pinning.py` checks every import order in fresh
  interpreters, including a pre-set `HF_HUB_OFFLINE=0`).
* An egress guard (`security/egress.py`) refuses every Python-level outbound connection
  (`connect`, `connect_ex`, `sendto`) and every name lookup except loopback, Unix sockets and the
  configured model server; each refusal is audited (`egress.blocked`, content-free). It is on by
  default, cannot be turned off in production, and is active in every test.
  *It cannot see* code that opens sockets outside Python's `socket` module (uvloop, native
  libraries): for those the kernel is the boundary, and §11 proves it there.
* Docling runs with remote services disabled and Tesseract as the only OCR engine (the other
  engines would try to download models). Model files must be pre-downloaded (production requires
  `PA_DOCLING_ARTIFACTS_PATH`) and are verified against `MANIFEST.sha256` at every production
  start (`cli models-manifest write|verify`); a changed, missing or extra file stops the start, and
  so does one the app user cannot read (named by its path under the models directory, with the
  `chmod -R a+rX` that fixes it).

**The model server is the only thing the application talks to.**

* The Ollama URL must name this machine or the compose service (`localhost`, `127.0.0.1`,
  `ollama`, `host.docker.internal`); another private host (a GPU box on the company network) only
  with `PA_LLM_ALLOW_LAN=true`. Anything public is refused at startup, and the client re-checks
  the URL it is given.
* A model whose name says `cloud` is refused, and the client asks the server which models it
  runs: a model with `remote_host`/`remote_model` is not used.
* The HTTP client ignores proxy environment variables (`trust_env=False`), follows no redirects,
  and sends exactly two requests - `POST /api/chat` and `GET /api/tags` - to exactly the
  configured origin; anything else (Ollama can also pull, copy, delete and push models) is
  refused before it leaves.
* Models are downloaded **once, on a connected machine**, by `scripts/prefetch_models.sh`, and
  copied to the server. The server never needs internet access.
* So are the National Bank's exchange rates for the foreign-currency invoice (rule 9):
  `scripts/fetch_nbg_rates.py` runs on a connected machine - standard library only, never
  imported by the application, HTTPS to the one host `nbg.gov.ge`, redirects refused, every
  answer size-capped and checked field by field - and sends nothing but dates and currency
  codes. The server imports its file (`load-fx-rates`), checking every row again and refusing
  the whole file for one bad row; an audit row records the counts, never the rates.

**Proof.** `scripts/verify_offline.sh` runs the whole product - upload, parse, extract, compare,
bill of quantities, reports, UI, delete, plus live-socket checks of the refusals below - inside a
Linux network namespace that has loopback and nothing else, where an attempt to leave the machine
fails in the kernel whatever opened the socket. It runs in CI. On a deployed stack:
`docker compose exec app python -c "import urllib.request; urllib.request.urlopen('https://example.com', timeout=3)"`
must fail with a network error (measured: `Temporary failure in name resolution`), and so
must the relay's resolver: `docker compose exec proxy nslookup example.com` answers SERVFAIL
(measured), not an address.

## 3. Untrusted input

Every upload is treated as hostile (`security/upload_guard.py`):

* extension allow-list (`.pdf .docx .xlsx`); legacy `.doc/.xls` and macro-enabled
  `.docm/.xlsm/.xlsb/.dotm` are rejected outright;
* magic-byte check must match the claimed type; a `.docx` that is really a `.xlsx` is rejected;
* size limit per file (default 50 MB) and page limit (default 300); password-protected PDFs
  rejected;
* **the request body is bounded per route and counted on the bytes that actually arrive**
  (chunked bodies included). Every route that takes no file is cut off at 1 MiB; only the routes
  that take a file - found from the routes themselves, so a new one cannot be forgotten - may
  carry two files plus framing (101 MB at the defaults);
* **a caller is identified before an upload body is read.** FastAPI parses a body before it runs
  authentication; the pre-auth layer (`security/preauth.py`) runs the same authentication for
  those routes first and answers 401/403 itself. Measured over a real socket: an anonymous
  upload declaring 90 MB is answered 401 and the connection closed while its body is still
  unsent; a 5 MB JSON post to a route that takes 1 MiB is answered 413 the same way;
* **where an upload's bytes are while it is handled.** Starlette keeps a file part in memory up
  to 1 MiB and moves anything larger to an unnamed temporary file in `/tmp` - the container's
  2 GB `noexec` tmpfs (§7), so those pages count against its memory - which the request closes
  when it ends, accepted or refused (`tests/integration/test_hardening_web.py`). The route
  then copies the part into memory (up to the file limit plus a 1 MiB chunk, and once more as
  `bytes`), and only an accepted upload is written to the data directory;
* OOXML archives: at most 10,000 entries, 512 MiB inflated in total and 256 MiB per entry, a
  200:1 compression ratio, `[Content_Types].xml` at most 1 MiB; duplicate and overlapping
  entries, path-traversal names, encrypted entries, any VBA part or macro content type, and any
  archive the parser cannot read are rejected - as a refusal, never as a server error;
* XML is parsed with entity resolution off (python-docx's parser; `defusedxml` for openpyxl);
* the user's file name is used for display only (NFC-normalised; control, format and other
  invisible characters removed); on disk files are named by UUID; a refusal message never
  repeats text the caller chose;
* spreadsheets are read with `openpyxl` in `data_only` mode: formulas are **not evaluated**,
  macros cannot run. Every string written to any generated workbook (comparison, bill of
  quantities, extraction export) is neutralised against formula injection;
* the annotated Word file returned by a comparison is a re-save of the uploaded file, so what it
  carried would come back as something "produced by our system": external relationships other
  than ordinary hyperlinks, embedded objects and ActiveX controls, `altChunk`, attached
  templates and mail-merge sources, and fields that include or link other content are removed
  (`report/docx_sanitize.py`; counts, never content, are logged).

What the text of a parsed document can cost is bounded where the text enters
(`ingestion/text_limits.py`). Several hundred regular expressions read every document, and a
pattern that starts with optional whitespace costs the square of a run of whitespace when it fails
after it. So any run of non-letter, non-digit characters longer than 200, and any unbroken run of
non-space characters longer than 256, is cut to that length when a stored parse is loaded or a
cell is read (the longest real ones measured in the client's documents: 116 and 33); a parsed
document may hold at most `PA_MAX_TEXT_CHARS` (6 M) characters, a workbook 1 M cells and 8 M
characters, and a bill-of-quantities workbook is counted without being loaded and refused above
`PA_BOQ_MAX_CELLS` (300,000). The patterns themselves were fixed where they were exponential (the
contract-number reader froze the whole process on a 62-character string).
A page image is rendered with finite, clamped highlight boxes (at most 20) and at most 25 M
pixels, and the application's own uses of pdfium are serialised by one lock.

### Input that was not read

Untrusted input also fails quietly: a scan with no OCR, a conversion that ran out of time, a
database that would not commit. The rule is that **a document, or part of one, that was not read
never yields a result that looks like "all clear", and the user always sees what was not read.**

* Every parse is assessed once (`ingestion/read_quality.py`) and the answer is stored beside the
  parse as `results/<id>/read_quality.json`: the file kind, pages in the file, pages with
  readable text, characters of text, whether the conversion reported errors or only part of the
  work, and whether OCR could run. Numbers and flags only; it goes with the document when the
  document is deleted or expires. A document stored before this existed gets the same answer
  worked out from its parse.
* A document with **no readable text at all** - a PDF of page images on a host without
  Tesseract, a workbook with no cell text - does not become `ready`. The parse job and the
  document are `failed` with the code **`NO_TEXT`**, and with **`OCR_UNAVAILABLE`** or
  **`IMAGE_ONLY`** when the cause is known; the API returns them as `error_code` and
  `error_hint`, the pages say them in Georgian and English. Every check and extraction refuses a
  document that is not ready with 409.
* A document with **some pages unread** (the conversion ended partial, reported errors, or pages
  carry no text - a picture among text pages, a timeout that cut the page table) stays `ready`
  and is **partial**. The act check, the comparison and the extraction then carry
  `read_quality` and a **`DOC.PARTIAL`** note first among what could not be checked: "N of M
  pages had no readable text (a scan without OCR, or a page the reader could not process);
  findings may be incomplete". The pages put it at the top; a comparison of two documents that
  match in what could be read is not called identical.
* An act checked against a contract that holds no text says so (`AOA.CONTRACT_UNREADABLE`); it
  is not reported as "no contract supplied". A workbook cut at the reader's caps adds an
  `AOA.TRUNCATED` note to the act report, as `CMP.TRUNCATED` does to a comparison.
* A stored parse that is missing or damaged is **409 `DOCUMENT_UNREADABLE`** ("upload it again"),
  and the document becomes `failed`; it is never a bare 500 with the document still `ready`.
* A job that exists to get the model's answer (the act review, the comparison explanation) ends
  `failed` with **`LLM_UNAVAILABLE`** when the model does not answer, and is asked again rather
  than reused. An extraction keeps the rules' and labels' facts but is `done` with
  `degraded: true`, a warning, and a banner on the page.
* A database failure while a parse is settled fails the document as well as the job, and the
  periodic sweep fails a document left `parsing` with no job to account for it for longer than
  `PA_DOCLING_TIMEOUT_S` plus five minutes.
* The audit row of a report or a read is committed **before** the body is returned. If it
  cannot be stored the answer is **503 `AUDIT_UNAVAILABLE`** (bilingual, with `Retry-After`,
  nothing of the report in it) and the log says the row was lost; no log line says it was
  recorded.
* A write-ahead-log checkpoint that fails or is blocked is logged (`checkpoint_failed`,
  `checkpoint_busy`); it never stops the caller.

## 4. Authentication, authorisation, audit

* API keys: 256-bit random, shown once, stored as SHA-256; constant-time comparison; revocable
  (`python -m procurement_agent.cli`). Creating and revoking a key is audited.
* Browser sessions are server-side. Signing in to the UI checks the key once and issues a random
  session token; the `HttpOnly`, `SameSite=Strict` cookie carries only that token and the
  database only its SHA-256. In production the cookie is `__Host-pa_session` (`Secure`, no
  `Domain`, path `/`), which a sibling sub-domain cannot overwrite. A session expires on the
  server after `PA_UI_SESSION_HOURS` (default 8) or after `PA_UI_SESSION_IDLE_MINUTES` without a
  request (default 60), ends on logout, and stops working when its API key is revoked.
  Cookie-authenticated state changes from other origins are refused (`Sec-Fetch-Site` /
  `Origin` check; the language switch included).
* Every document and job is owner-scoped. Foreign or unknown ids return **404**, so ids cannot
  be enumerated; a probe of another user's id is itself audited (`access.denied`).
  `tests/integration/test_security_matrix.py` walks every route in the application and asserts it
  is either on the short public list or answers 401 without a credential, 404 to a second user,
  and refuses a cross-origin state change.
* Every response - including a refusal written before the application runs - carries
  `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, a
  `Permissions-Policy` that disables the sensor/payment APIs, `Cross-Origin-Opener-Policy` and
  `Cross-Origin-Resource-Policy: same-origin`. Pages carry a CSP of `default-src 'self'` with no
  inline script or style; production adds `Strict-Transport-Security` (one year,
  sub-domains); the image's server command omits uvicorn's `Server` banner.
  Note: `Referrer-Policy: no-referrer` makes browsers send `Origin: null`, so the CSRF check
  rests on `Sec-Fetch-Site`, which browsers send over HTTPS and to localhost only; a browser
  reaching the UI over plain HTTP from another machine is refused rather than trusted.
* Rate limiting: fixed window, default 120 requests/minute, keyed by **client address** — not
  by the key or cookie presented, which the caller chooses (an IPv6 client by its /64). `/health`,
  `/ready` and `/static` are exempt. The address is the one uvicorn derives from
  `X-Forwarded-For`, which it accepts only from `FORWARDED_ALLOW_IPS`; production refuses `*`.
  If the reverse proxy does not forward the client address, every user shares one bucket.
* Each user may hold at most `PA_MAX_ACTIVE_JOBS_PER_USER` (default 20) queued or running
  jobs, and at most `PA_MAX_CONCURRENT_REPORTS` (default 4) reports are computed at once: a fifth
  caller is answered 503 with `Retry-After` after `PA_REPORT_WAIT_S` seconds instead of queuing
  - reports are recomputed on request, on a thread pool every other request shares. Writing a
  report into the act's Word or PDF file, or into the later version of a comparison, is bounded
  by the same slots; a request takes one for the check and, once it has given that back, one for
  the file.
  `/api/v1/status` shows a caller only their own jobs.
* Audit trail: an append-only table plus a JSON log line for upload (accepted/denied),
  extraction start, a result read (API and UI), a document's content shown as text or as a page
  image (these are committed before the body is returned), check results and exports, delete and retention delete, sign-in (ok/denied) and logout,
  refused cross-origin requests, probes of other users' ids, key creation and revocation, the
  retention sweep, the model-manifest write, outbound attempts the egress guard refused, a schema
  upgrade of the database (`db.migrate`: the versions and the migrations' names), and shortening
  the trail itself. Fields: actor, action, outcome, document id, job id, client IP,
  timestamp, and a content-free detail. Read it with `python -m procurement_agent.cli audit`.
  The table is append-only **in the database**: triggers abort any `UPDATE` or `DELETE`, so a bug
  or an injection through the application cannot rewrite history; the one deliberate way to
  shorten it is `cli audit-prune --older-than-days N --yes`, which is itself audited. Someone
  with write access to the database file can drop the triggers - this protects against mistakes
  and application-level tampering, not against the owner of the disk.
* A dev-only bypass key (`PA_DEV_API_KEY`) exists for local testing; `PA_ENV=prod` **refuses to
  start** if it is set. Production also refuses to start with the egress guard off, no host
  allow-list, a trusted-proxy list of `*`, or missing or unverified models.

## 5. Logging

Log lines never contain document text, extracted values, user-chosen file names or API keys, and
neither do the places an error is stored (a document's error, a job's message, an audit detail).
This is kept at three levels:

* at the call sites: nothing logs `str(e)`. `security/safe_errors.py::safe_exc` says what *kind*
  of failure it was and, for the few families whose parts are structural, how many and which
  kinds (a validation error is "3 errors: missing, int_parsing", never the value). Only
  exception classes that this project writes from its own words and numbers (`ContentFree`) keep
  their message;
* behind it: a structlog processor withholds fields whose name says they hold content and any
  string too long to be a status word, and a filter on the standard `logging` module cuts query
  strings off access-log lines and renders a traceback as frames and exception kinds, without
  messages;
* the guarantee is a test, not a promise: `tests/integration/test_log_canary.py` runs the real
  server through every path that touches document text with a marker in the body, a sheet name,
  a cell, a file name, form fields, a query string, and in model replies that are garbage or an
  error, and asserts the marker is in no log line and no database column.
  A library's own message text cannot be inspected; the test is what notices when one leaks.

The relay (Caddy) has logs of its own, and they are held to the same rule. Caddy redacts
`Authorization` and `Cookie` by itself and writes every other header as it came, so an error it
logged - an app that is down, a connection reset - put the caller's `X-Api-Key` on stdout in
clear (measured: the key in one line per failed request). Both its loggers, the default one
(the error log) and the site's access log, now pass through a `filter` encoder that drops every
request and response header, and the whole query string (the report pages carry document and job
ids, amounts and dates there). What stays is time, client address, method, host, path, status,
size and duration. The container's health probes are not logged. Measured on the stack with a
marker in `X-Api-Key`, `Cookie`, `Authorization`, `Proxy-Authorization`, a made-up
`X-Auth-Token` and the query string, on requests the app answered, refused, and could not be
reached for: no marker in the relay's, the app's or Ollama's log. `tests/unit/test_relay_policy.py`
fails when a log loses the filter or the filter loses a field.

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
`no-new-privileges`, tmpfs `/tmp` (`noexec,nosuid,nodev`; library caches are pointed there),
memory, PID and (`PA_APP_CPUS`) CPU limits, `ulimits.core: 0`, an init process. The relay runs as
`nobody` with the same restrictions (it keeps only `NET_BIND_SERVICE`, which its binary's file
capability needs to execute at all). The process itself sets `umask(0o077)` and `RLIMIT_CORE=0`
(a crash in a native parser must not leave a core file of document text), creates `/data` as
`0700`, and opens SQLite with `secure_delete`, `trusted_schema=OFF` and `cell_size_check`. The
database's schema version is checked at start: one written by a newer release is refused, not
opened (`migrations.py`, PRODUCTION_READINESS.md 5.6).
One worker thread processes jobs, so a malicious document cannot fan out CPU/memory consumption.
Docling conversion has a timeout.

Health: `/health` is liveness only. `/ready` returns 503 when the database does not answer or
the job worker is dead or has not shown progress for longer than its longest legitimate step;
the container healthcheck uses `/ready`. Neither endpoint needs a key or reveals configuration.
On shutdown the worker is given `PA_WORKER_STOP_TIMEOUT_S` to finish its job; a job cut off
is failed on the next start instead of hanging in `running`.

**A hung server is restarted.** A request that makes the process spin inside a regular
expression or a native parser holds the interpreter's lock: from inside, nothing - a watchdog
thread included - can run to notice, and Docker restarts on exit, not on a failed healthcheck.
The compose command is therefore `scripts/supervise.py`: a small standard-library process that
probes `/ready` over loopback every 15 s, and after three consecutive failures (once the
180 s model-loading start-up is over) sends SIGTERM, then SIGKILL after 50 s, and starts the
server again; after five restarts in ten minutes it exits non-zero, and Docker's restart policy
then starts the container again, as after any exit (the log shows `giving_up`). A restart fails
the job that was running and loses nothing else. Every wait of the supervisor is on an event that
the SIGTERM handler sets, so `docker compose stop` reaches the server at once (measured: 0.7 s
on an idle app); a probe to a server that does not answer is abandoned when the stop arrives.
`tests/integration/test_supervisor_real_app.py` freezes the real server (`SIGSTOP`, which is what
a wedged interpreter looks like from outside) and checks it is killed and comes back; on the
three-container stack the same drill measured one restart, 107.6 s after the stop, the relay
answering again at 124.2 s. A restart is Docker's only on an exit by itself: a container stopped
by hand (`docker kill`, `docker compose stop`) stays stopped until `docker compose start` or `up`.
The supervisor cannot tell a hang from any other reason `/ready` does not answer 200: a
`database` or `worker_stalled` answer restarts the server too, and so would a thread pool held
for a minute by other requests (`/ready` runs on it), which also fails the running job. The
report limit in §4 keeps ordinary load from doing that; a restart loop shows in the container's
log as `restarting` lines and ends in a non-zero exit after five.

## 8. Retention

Documents expire `PA_RETENTION_DAYS` (default 7) after upload. A sweep runs every 10 minutes
and on `python -m procurement_agent.cli sweep`. Users can delete immediately from the UI or via
`DELETE /api/v1/documents/{id}`. Deletion:

* marks the document deleted in the database first (and cancels pending jobs), then removes the
  upload, the parsed structure and every result file; each document is handled on its own, so
  one failure does not stop the rest;
* **reconciles**: on every sweep, files whose document is deleted - or is not in the database
  at all and is older than 15 minutes - are removed again, so a removal that failed (a
  permission error, a crash between the commit and the unlink) is retried until it works, not
  forgotten;
* removes what is *derived* from the document: explain/review outputs that live in another
  document's directory and quote it, the record of the checks it took part in, the question a
  user typed (a job's parameters), and the in-process caches that hold document text (the
  acceptance-act check's two text-keyed caches and the words the spelling dictionary was asked
  about; the dictionary itself stays loaded);
* overwrites a file and syncs it before unlinking it - every file of the document, and every
  document-derived file the application drops under the data directory (the output of a job
  that lost the race with a deletion, an upload whose database commit failed, a half-written
  parse) - and SQLite zeroes deleted cells (`secure_delete`) and the write-ahead log is
  truncated after every sweep. The annotated and merged files built for a download live in
  `/tmp` (a tmpfs, §7) and are only unlinked.

The metadata row is kept (name replaced by `[deleted]`) so the audit trail stays consistent.
**Copies of the database follow the same promise.** Before a schema upgrade changes a database that
holds rows, the release copies it into `/data/db-backups/` (`0600`, directory `0700`). The copy is read
into memory and blanked there, and only the blanked database is written, so the text never reaches
the directory - a process killed half-way leaves nothing that retention would have removed. Blanked:
`documents.display_name` (the file name; set to `[scrubbed]`), `documents.sha256` (a hash of the
document), `documents.error`, `jobs.params` (the question and the options a person typed),
`jobs.message` and `check_runs.params` (the options of a check). A test fails until every column of
every table is either on that list or on the test's own list of columns that are kept, so a column
added later gets a decision. Kept, because a copy is useless for going back without them: key and
session-token hashes, account names, ids, statuses, sizes and times, the audit trail.

The lifetime of what is kept is bounded by age, like documents: the retention sweep (every 10 minutes,
and `cli sweep`) overwrites and removes a copy taken more than `PA_DB_BACKUP_KEEP_DAYS` days ago
(default 3; 0 removes it at the first sweep after the upgrade, at the cost of having nothing to go
back to but the operator's own backup), and at most the three newest are kept. Worst case, then: a
file name, a typed question, an option or a document hash is in a copy for **0 days**; an audit row
that `audit-prune` has removed, or a key that has been revoked, is in a copy for at most
`PA_DB_BACKUP_KEEP_DAYS` days plus a sweep interval (and, while the stack is stopped, until its first
sweep after it starts). The same sweep removes a half-written copy a crash left, once it is an
hour old. A copy is not a backup (PRODUCTION_READINESS.md 5.5).

## 9. Supply chain

* `uv.lock` pins every dependency, and CI refuses a lock that disagrees with `pyproject.toml`.
  `bandit` (0 findings, 0 `nosec`), ruff (including its `S` security rules) and mypy (strict) run
  in pre-commit, `scripts/check.sh` and CI; `pip-audit` (against exactly what the lock pins) and
  `gitleaks` (full history) run in CI and in `scripts/check.sh` when it has network access.
  Container base images, Ollama and the relay are pinned by version **and digest**, the CI
  actions by commit and the gitleaks image by digest (resolved 2026-10-02; the commands to move
  a pin are in the file headers). The image build context is an allow-list (`.dockerignore`):
  only `pyproject.toml`, `uv.lock`, `README.md`, `src/` and the supervisor can enter an image.
  `torch` and `torchvision` come from the PyTorch CPU index and OpenCV is the headless build, so
  the lock holds no CUDA wheels and no GUI libraries - 19 fewer packages that would be unpacked
  into the image and could not be used on a server without a GPU.
* What ships is listed in `docs/sbom.cdx.json`, a CycloneDX 1.5 SBOM of the production closure of
  `uv.lock` (for linux/x86_64: no dev group, no extras; each package with its purl, the
  SHA-256 of every file the lock records, its licence and its place in the dependency graph).
  `scripts/sbom.py` writes it without timestamps or random ids, so two runs give the same bytes;
  `tests/unit/test_sbom.py` fails when it no longer matches `uv.lock`, and the fix is
  `uv run python scripts/sbom.py --out docs/sbom.cdx.json`. It lists what the lock pins; it does
  not see the Debian packages in the image (Tesseract and its libraries) or the native libraries
  bundled inside wheels (OpenSSL 1.1.1k inside `opencv-python-headless`, for one), which
  `pip-audit` cannot see either.
* The licences of those packages, of the components outside Python (base images, Tesseract, the
  spelling dictionary, Caddy, Ollama) and of the models are in `docs/THIRD_PARTY.md`, written by
  `scripts/third_party.py` from the lock and the installed metadata. It lists what it cannot
  classify as unknown instead of guessing, names the copyleft libraries bundled inside wheels
  (GEOS in `shapely`, FFmpeg in `opencv-python-headless`, `libquadmath` and `libgfortran` in
  `numpy` and `scipy`, for example) and says what each licence asks of someone shipping the
  product. `tests/unit/test_third_party.py` fails when it no longer lists exactly the production
  packages of `uv.lock`, and when a copyleft or unknown licence appears that nobody has read;
  the fix is `uv run python scripts/third_party.py --out docs/THIRD_PARTY.md` plus, for a new
  licence, a reviewed entry in the script.
* A release leaves the repository as a bundle (`scripts/make_release_bundle.sh`,
  docs/PRODUCTION_READINESS.md 5.8): built from a clean commit, from a fixed list of files, with
  `SHA256SUMS` over every file and a `RELEASE.txt` that records the commit and the hashes of
  `uv.lock` and of the SBOM. The checksums show the bundle arrived as built; they do not show who
  built it, so hand over the hash of `RELEASE.txt` or of the bundle by another route.
* Documents cannot reach the repository: `scripts/check_no_documents.sh` blocks any document,
  archive, database, key, `.env` or client directory (a link to one included, which is how a git
  worktree holds them) outside `tests/fixtures/` - as a CI job (`repo-guard`, which runs after a
  push), in the opt-in `pre-commit` configuration, and from the local commit hook below, which
  runs before the commit exists. `scripts/client_canary.py install` adds that commit hook (it
  refuses staged documents, runs the script when the checkout has it, and refuses the client's own
  identifiers: tax IDs, IBANs, names, and six-word runs of their text) and a push hook
  (identifiers only); `--no-verify` is the deliberate override. The tool knows the identifiers
  only as salted hashes in a file outside the repository, so it is not itself a copy of them.
  All test fixtures are **synthetic** (`scripts/gen_synthetic_samples.py`).
* Dependencies with native code (Docling, PyTorch, pypdfium2, Pillow) are the main attack
  surface for malformed documents; they are kept current via the lock file and run inside the
  hardened container described above - but in the same process as the web server (§10).
* The annotated PDF (`report/pdf_annotate.py`) calls PDFium's C API through pypdfium2's raw
  bindings, for the highlights and notes its Python wrapper does not offer: fixed annotation
  setters on annotations the module creates, under the process-wide PDFium lock. It is the one
  module allowed that import (`tests/unit/test_egress_surface.py`, `FFI_IMPORTS`); `ctypes`
  itself stays forbidden everywhere, and the output carries nothing but annotations - no
  action, link or script (`tests/unit/test_pdf_annotate.py`). Nor does it carry the act's own
  (red team R6, R6-08): the copy is a new document the act's pages are imported into, so what
  the file runs or carries when it opens - an open action, a document script, an attached file,
  a form - stays behind; each page's own annotations (links that launch a program or run a
  script, form fields) are drawn into the page and their actions dropped, and the pages are
  imported once more so that no object they no longer use is saved. What importing keeps - every
  entry of a page's own dictionary, and whatever its content draws with - is then checked in the
  saved copy, read as PDF reads it (outside strings and stream data, escaped names decoded): a
  name that only runs something, reaches out or carries a file or a form (`/JS`, `/AA`, `/AF`,
  `/Launch`, `/EmbeddedFile` ...) refuses the copy (415) - a page's associated file, annotations
  held in no array, actions written as an array, an attached file of a form the page draws. The
  check fails closed (security review): a stream's data is skipped by its own dictionary's
  direct length and must end where `endstream` stands, and a length it cannot read, a stream that
  does not end there, a string left open and an object stream refuse the copy too. A page's own
  metadata goes with it: it runs nothing. A page that runs an action itself when opened or closed
  is refused as well. (Drawing each page anew as a form of its content, to leave a page's
  dictionary behind, was tried and reverted: PDFium copies a page's resources into each form, so
  thirty pages sharing a font carried thirty copies of it, and a file built for it gigabytes.)
  Six mutations of `scripts/mutate_security.py` stand for these controls
  (`tests/unit/test_pdf_annotate.py`, `tests/unit/test_security_review_aoa.py`).
* What a hostile file can cost the annotators is bounded: a Word table's cells are read once
  each, whatever number of columns a cell says it spans (python-docx's `Row.cells` made an object
  for each - gigabytes for one cell); every place of a value in a paragraph is found in one pass,
  a run's text read without python-docx's query; a search reads at most 1,000 places of a value,
  a finding is marked in at most 50 and a file in at most 300 distinct places - a finding cut
  short is named in the note at the top and gets no tracked change, so accepting every change
  never leaves it half corrected, and a finding that names a place past the search is left in
  the note rather than marked on words it does not mean. A PDF page's touching text cells are
  mended by an indexed search of its words, and not at all past 5,000 cells, 30,000 words or two
  million words read. The registry extract reader's text is held to the same limits as every
  other document's (`bound_text`, line by line), its patterns are measured on raw text as well
  (the regex budget harness now builds its inputs from each pattern's own words too, which is
  how the page-footer pattern's quadratic blanks, and a contract title's, were found), and it
  reads at most 200 persons. Word fields that link out are held to the same schemes as links
  (`http`, `https`, `mailto`); a link field whose target another field computes is unwrapped.
* Model files are checked against a manifest at start (§2); the formats in use (safetensors, ONNX)
  cannot carry code the way a pickle can, so this detects a swapped or truncated weight file, not
  a code-execution attempt.

## 10. Known gaps and limits

What this does not do, so nobody has to discover it:

* **Parsing runs in the web process.** Docling, pdfium, Tesseract and the XML parsers run in
  the same process as the web server and the database connection, on one daemon thread. There is
  no way to stop a runaway native call from inside the process; the supervisor (§7) restarts it
  after a minute or two instead. A memory-corruption bug in a parser would run with the
  process's access to every stored document. The structural fix is a parser sandbox - parsing in
  a child process with its own memory and CPU limits, a hard kill, no database or secrets, and
  pdfium moved out - which is **designed but not built**. Until then pdfium has one more
  exposure: the lock in §3 serialises the application's own uses of it, but not Docling's.
* **The input limits are a bound, not a proof.** They stop the long-run shapes that made
  patterns quadratic (and the fixed patterns were exponential); the regular-expression scan
  (`tests/regex_budget.py`) searches for super-linear patterns with strings built from each
  pattern's own pieces, and finds none after the limits. A string that looks slow is timed
  again (three times in all) and judged on its shortest times, so a loaded machine does not
  report a linear pattern; a quadratic one is slow on every run. A search that finds nothing has
  not covered every input.
* **No application-level encryption at rest**: files, the database and the audit trail are
  plaintext to anyone with the disk; rely on disk encryption (LUKS), and keep `/data` and backups
  on it.
* **Secure deletion is best-effort** (§8): journaling, copy-on-write, snapshots and SSD wear
  levelling can keep old copies; encryption at rest is the control.
* **The audit trail is protected against the application, not the disk's owner** (§4).
* **Pre-upgrade database copies stay on the data volume** for `PA_DB_BACKUP_KEEP_DAYS` days. They
  hold no file name, typed text or document hash (§8), but they do hold key hashes, ids and the
  audit trail as they were, and a copy restored later brings a revoked key back.
* **AI coding tools can read whatever they are allowed to.** On a developer machine, an
  assistant that reads client files sends them to its provider. On the machine this was
  developed on, `.claude/settings.json` deny rules and a `.cursorignore` name the client's
  paths. They are local files, **not part of the repository** (a fresh clone has neither), and
  they are a guardrail, not a boundary: a script the assistant runs can still read and print
  the files. Keep real documents out of any folder an assistant is pointed at.
* API-key auth only; integration with the company directory (LDAP/AD or OIDC) is planned.
* The rate limiter keeps its counters in process memory (they reset on restart). The app runs
  one uvicorn worker by design; running several would multiply the effective limit.
* GET requests write bookkeeping rows (check history); `SameSite=Strict` is what keeps another
  site from causing them.
* OCR (Tesseract) runs only inside the container image. On a host without it a PDF of page
  images yields no text, and the document is `failed` with `NO_TEXT` and the hint
  `OCR_UNAVAILABLE`; a PDF with some text pages and some pictures is `ready` but partial
  (`DOC.PARTIAL`, "Input that was not read" above).
* Git history: see `PRODUCTION_READINESS.md` §7.

## 11. Verification and measured results

`scripts/security_audit.sh` re-measures everything below from scratch, offline, and prints one
scorecard (`--quick` skips the long steps, `--online` adds `pip-audit`, which sends package names
and versions to PyPI/OSV and nothing else). A step that cannot run says SKIPPED and is listed
at the end; it is never counted as a pass.

| Step | What it measures |
|---|---|
| `static_*` | ruff (with the `S` rules), mypy `--strict`, bandit, count of `nosec` markers |
| `repo_*`, `machine_*` | no document/database/key/`.env` in git; no credential-shaped string in any commit or the tree (a small stand-in for gitleaks, which CI runs); the client-identifier canary over the tree; hooks installed; client directories not readable by group/others; the AI-tool deny rules |
| `egress_policy` | the single outbound-capable code path is pinned; the guard, URL, model and Host policies |
| `offline_kernel` | the whole product flow with only loopback up (`unshare --net`) |
| `security_suite` | authz/IDOR/CSRF/header/cookie matrix over every route, hostile ids and hostile request bodies (19 kinds, on every route that reads one: always a 4xx, never a 500); pre-auth; hostile uploads; retention; schema upgrades (the copies' modes, the refusal of a newer database); hardening; the XML, DOCX, manifest, text-limit and capacity tests |
| `log_canary` | a marker planted in every input reaches 0 log lines and 0 database columns |
| `deploy_policy` | compose, Dockerfile, build context and supervisor; the supervisor restarts a hung real server |
| `regex_budget`, `regex_controls` | directed search for super-linear patterns through the input limits; the same search still finds the pre-fix pattern |
| `resources` | worst-case inputs (bounding boxes, page-size and zip bombs, 20 MB of separators, a 54,000-character paragraph, 400,000-cell workbooks) finish inside time and memory ceilings, each in a fresh process |
| `mutation` | one security property of a copy of `src/` is broken at a time (ownership not checked, a header dropped, an outbound lookup allowed, error text unscrubbed, ...); a test must fail each time |

Change one of these properties and a test fails - that is what `mutation` demonstrates, and CI
runs it. A mutation counts as caught only when a test *fails* (one that stops the tests from
running does not count), and the unmodified copy must pass the same tests first. Its blind spot,
stated in the harness: a test that starts its own interpreter on the real source (the log-canary
flow, the offline smoke, the real-server supervisor test) cannot see a mutation, so it can only
make one survive, never be caught.

### Results of the 2026-10-02 audit

Measured on this working tree (commit b6f7e63 plus the uncommitted changes of this work); "start"
is the same measurement before any change. `scripts/security_audit.sh --online` ended with 19
steps passed, 0 failed, 0 skipped.

| | Start | Now |
|---|---|---|
| One string of about 60 characters in a document froze the whole server (exponential pattern in the contract-number reader) | 0.84 s at 46 characters, 3.6 min at 62, about 31 h at 80 | 0.00 s at 1,216 characters |
| Super-linear patterns found by the directed scan | 166 findings in 56 patterns on unbounded text (461 patterns, 49,863 strings; measured after the exponential pattern above was fixed, before the input limits) | 0 (464 patterns, 88,789 strings, 21 s, 0.25 s budget) |
| One highlight box in a page-image request (20 allowed per request) | 5.45 s with the interpreter's lock held | 0.02 s for 21 extreme boxes |
| A 400,000-cell bill-of-quantities workbook | read twice in full, about 421 bytes a cell, no cap | refused before loading: 0.05 s, 197 MB peak for the process |
| Request-body bytes read before the caller was known | up to about 101 MB, any content type | none of an upload (answered 401 while the body is unsent); 1 MiB on every other route |
| Places that carried document text: log call sites / stored errors | 14 / 3 | 0 / 0; a marker in every input reaches 0 log lines and 0 database columns (56 tests) |
| A request naming another Host | served (200) | 400 |
| Offline flags when `eval.runner`, `docling_service` or `extraction.pipeline` was imported first | False / False / False | True under every import order |
| In-process egress guard / the whole flow with only loopback up | none / never run | on in every test and at runtime / 17 steps pass |
| Routes tested for authentication, ownership, CSRF, headers, hostile ids and bodies | 0 | every route; 1,063 tests in the security step |
| Security headers | 5; no HSTS, COOP, CORP; `Server: uvicorn` | all, refusals included; banner removed in the image command |
| Data directory / database / WAL modes | 0755 / 0600 / 0644 | 0700 / 0600 / 0600 |
| Git hooks | none | commit hook (documents, the generic rule, identifiers) and push hook (identifiers); CI `repo-guard` |
| The client's own name in the tree | on 2 lines (and in 3 early commits) | on none; the canary reports 0 hard hits and 9 soft warnings, all one public bank's name in bank-account test strings (reviewed, left as a warning) |
| Security tests shown able to fail | - | 37 of 37 broken controls caught |
| bandit findings, `nosec` markers, ruff `S` findings | 0, 0, 0 over 30,507 lines | 0, 0, 0 over 32,622 lines |
| Vulnerable dependencies (pip-audit) | not run | none, in the 154 packages that apply on Linux |
| CUDA wheels in the lock | 19 (about 2.5 GB) | 0 |
| Worst-case inputs inside their time and memory ceilings | - | 13 of 13 |
| Development Ollama container | default network, internet reachable, all capabilities | prepared, **not switched**: `scripts/switch_dev_ollama.sh --yes` |

### The accuracy gate

The extraction and checking code is held to the accuracy gate of `docs/ACCURACY.md`, and every
change in this work - the pattern rewrites, the input limits, the lock - went through it against the
unchanged b6f7e63 baseline: **0 differences** in the three strict evaluation sets (reports and every
scored value), in the output snapshot (every module over every set), and in the act mutation
harness (seed 1, six per class: the date and number classes); the bill-of-quantities mutation
harness (seed 1, six per class, two bases) passes 102 of 102 mutants. These sets were looked at while
the product was being fixed, so this shows that nothing moved, not that it is accurate
(`docs/ACCURACY.md` says what it is).

The CPU-only torch lock (2.14.0 with CUDA 13 → 2.14.1 CPU): Docling parsed all 54 evaluated
documents again in that environment from an empty cache, and every parse is identical as JSON to the
cached one (53 Word files and 1 PDF - the layout model ran once, so this is thin evidence about its
numerics beyond the strict evaluations and the snapshot, which are again 0 differences, and the
`docling` tests, which give the same result in both environments).
