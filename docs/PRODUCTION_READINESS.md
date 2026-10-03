# Production readiness

What is in place, what the deployer must do, and what is still open. Data handling and the
threat model are in [SECURITY.md](SECURITY.md); accuracy claims and their limits in
[ACCURACY.md](ACCURACY.md). This page does not repeat them, it points at them.

Status markers: **[x]** implemented and covered by tests or a measured check, **[ ]** a
deployer duty or an open item.

## 1. Security controls

| Control | Status | Where / how |
|---|---|---|
| API-key authentication (256-bit, stored as SHA-256, constant-time compare, revocable) | [x] | `security/auth.py`, `cli create-key / list-keys / revoke-key` |
| Server-side UI sessions (token cookie, server expiry `PA_UI_SESSION_HOURS`, idle expiry `PA_UI_SESSION_IDLE_MINUTES`, logout revokes, key revocation kills its sessions; `__Host-` cookie prefix in prod) | [x] | `security/auth.py`, `ui_sessions` table |
| CSRF: cookie-authenticated state changes refused from other origins (`Sec-Fetch-Site` / `Origin`), `SameSite=Strict` | [x] | `security/auth.py` |
| Security headers on every answer, refusals included: CSP (`default-src 'self'`, no inline script), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, COOP/CORP `same-origin`, `no-store` on the API; no `Server` banner | [x] | `security/headers.py`, `main.py`, `--no-server-header` in the image command |
| HSTS | [x] | sent by the app in prod (one year, sub-domains); the TLS proxy may add its own |
| Host allow-list: a request naming any other host is refused (DNS rebinding, tunnels); prod refuses to start without `PA_ALLOWED_HOSTS` | [x] | `security/hosts.py` |
| Rate limiting, fixed window per client address, `/health` `/ready` `/static` exempt | [x] | `security/ratelimit.py`, `PA_RATE_LIMIT` |
| Per-user cap on queued + running jobs | [x] | `PA_MAX_ACTIVE_JOBS_PER_USER` |
| Upload validation: extension allow-list, magic bytes, zip-bomb limits, macro/VBA refusal, page limit, encrypted PDFs refused | [x] | `security/upload_guard.py` |
| Request-body cap per route, counted on streamed bytes (chunked bodies included): 1 MiB on every route that takes no file, 2 × `PA_MAX_FILE_MB` + 1 MB on the upload routes, where the caller is authenticated *before* the body is read | [x] | `security/body_limit.py`, `security/preauth.py`; live-socket check in `scripts/offline_smoke.py` |
| Hostile-input bounds: no exponential or quadratic reading of document text (patterns fixed, runs and tokens bounded, text/cell budgets), bounded rendering, workbook cell count refused before load, one lock around the app's own pdfium use | [x] | `ingestion/text_limits.py`, `ingestion/xlsx_size.py`, `ingestion/render.py`, `ingestion/pdfium_lock.py`; `tests/regex_budget.py`, `scripts/audit_resources.py` |
| Bounded report work: at most `PA_MAX_CONCURRENT_REPORTS` computed at once - the checks and the Word/PDF files written from them - the rest 503 + `Retry-After` | [x] | `security/capacity.py`, `api/checks.py` (`reported`) |
| Annotated Word output drops external relationships, embedded objects, risky fields | [x] | `report/docx_sanitize.py` |
| Formula-injection escaping in every generated workbook | [x] | `report/xlsx_safe.py`, used by all four writers: BOQ merged workbook, comparison export, extraction export, `scripts/boq_compare.py` |
| Append-only audit trail (DB table with triggers that abort UPDATE/DELETE + JSON log line); reads, key operations, denied probes and refused outbound attempts included | [x] | `db.py`, `security/audit.py`, `cli audit`, `cli audit-prune`; see SECURITY.md §4 for the actions |
| Content-free logs and stored errors, proven by a canary test through every input path | [x] | `security/safe_errors.py`, `security/logscrub.py`, `tests/integration/test_log_canary.py` |
| Retention: row first, files second, a reconciler that retries what failed, derived outputs and caches removed, overwrite + fsync, SQLite `secure_delete` | [x] | `jobs/retention.py`, `jobs/worker.py` sweep every 10 min, `cli sweep`, `PA_RETENTION_DAYS` |
| Versioned schema: the version lives in the database (`PRAGMA user_version`); a start applies pending migrations in order, each in its own transaction, after a consistent copy of a database that holds rows, with file names and typed text blanked and removed after `PA_DB_BACKUP_KEEP_DAYS`; a database newer than the release is refused; the upgrade is audited (`db.migrate`) | [x] | `migrations.py`, `cli db-status`, `tests/unit/test_migrations.py`; 5.6 |
| Files created owner-only (umask 077, data dir 0700), no core dumps | [x] | `security/hardening.py`, `db.py`, `config.py`, compose `ulimits` |
| Air-gap, four layers: internal Docker networks (no route out), offline flags force-set before any ML import, an in-process egress guard, Ollama URL = this machine / the compose service (LAN only by opt-in), cloud models refused, only two model-server requests possible, `trust_env=False`, no redirects | [x] | `docker-compose.yml`, `procurement_agent/__init__.py`, `security/egress.py`, `config.py`, `llm/ollama_client.py` |
| Proof of the air-gap: the whole product flow in a loopback-only network namespace | [x] | `scripts/verify_offline.sh` (CI job `offline`) |
| Model files verified against `MANIFEST.sha256` at every prod start | [x] | `security/models_manifest.py`, `cli models-manifest` |
| Container hardening: non-root, read-only rootfs, `cap_drop: ALL`, `no-new-privileges`, tmpfs `/tmp` (noexec), memory/PID/CPU limits, no core dumps, init | [x] | `Dockerfile`, `docker-compose.yml`; `tests/unit/test_deployment_policy.py` |
| Trusted-proxy handling for `X-Forwarded-For` (never `*`) | [x] | `FORWARDED_ALLOW_IPS` (image default loopback; compose trusts only its relay) |
| Prod start-up refuses: a dev key, missing or unverified Docling models, the egress guard off, no host allow-list, `FORWARDED_ALLOW_IPS=*` | [x] | `Settings.validate_for_prod` |
| Authorisation matrix over every route (401 / 404-for-others / CSRF / headers / cookie flags), and a harness that breaks each guard to prove a test fails | [x] | `tests/integration/test_security_matrix.py`, `scripts/mutate_security.py` (CI job `mutation`) |
| Client documents kept out of the repository (generic block in the hook and CI) and the client's identifiers out of commits (salted-hash canary, local hook) | [x] | `scripts/check_no_documents.sh`, `scripts/client_canary.py` |

## 2. Reliability

- [x] **Readiness vs liveness.** `/health` answers while the process runs. `/ready` returns 503
  with a reason (`database`, `worker_down`, `worker_stalled`) and is what the container
  healthcheck calls. A busy worker counts as stalled only after
  `max(PA_LLM_TIMEOUT_S, PA_DOCLING_TIMEOUT_S)` plus two minutes without progress, so a long
  Docling or LLM step does not flap it.
- [x] **Worker recovery.** On start, jobs left `running` by a dead process are failed and
  queued jobs are re-queued; a document stuck in `parsing` is released.
- [x] **Graceful shutdown.** The worker gets `PA_WORKER_STOP_TIMEOUT_S` (30 s) to finish its
  job; compose gives the container `stop_grace_period: 60s` and runs an init process, so
  SIGTERM reaches uvicorn. Measured: `docker compose stop app` completes in about 0.5 s when
  idle, with a clean "Application shutdown complete".
- [x] **Upgrades fail safe.** A schema change runs one migration at a time, in a transaction that
  also records the new version (SQLite rolls DDL back with it), under the database's write lock:
  a failed upgrade leaves the last version that worked, a process killed in the middle leaves the
  version before, and two processes starting together migrate once. Tested by killing a process
  inside a migration and by concurrent starts on a new, an old and a behind database
  (`tests/unit/test_migrations.py`).
- [x] **Start order.** The app waits for Ollama's healthcheck (`ollama list`); the relay starts
  after the app.
- [x] **Restart when hung.** Compose marks an unready container `unhealthy` but never restarts
  it, and a request that spins in a regular expression or a native parser freezes the process
  from inside. The compose command is `scripts/supervise.py`, a standard-library process that
  probes `/ready` over loopback and restarts the server after three failed probes in a row (see
  SECURITY.md §7). The running job is failed; nothing else is lost. Still wire `/ready` into
  whatever monitoring the client runs, so a restart loop is noticed.

## 3. Observability

- [x] JSON logs on stdout in prod (`PA_ENV=prod`), human-readable in dev. Log lines carry no
  document text, extracted values, file names or keys.
- [x] Audit events in the `audit` table and as log lines, with actor, action, outcome, ids,
  client IP and timestamp.
- [ ] Ship container stdout to the client's log store if they want retention beyond Docker's
  log driver. Nothing leaves the host unless the operator configures it.
- [ ] No metrics endpoint. `/api/v1/status` (authenticated) reports the caller's own active jobs,
  model and OCR availability for a human (server-wide queue depth is deliberately not shown);
  there is no Prometheus exporter.

## 4. Testing and quality gate

| Layer | Command | Where it runs |
|---|---|---|
| Lock in sync with `pyproject.toml` | `uv lock --check` | pre-commit, `scripts/check.sh`, CI |
| Lint + format | `ruff check .`, `ruff format --check .` | pre-commit, `scripts/check.sh`, CI |
| Types | `mypy` (strict) | pre-commit, `scripts/check.sh`, CI |
| Static security | `bandit -c pyproject.toml -r src` | pre-commit, `scripts/check.sh`, CI |
| Unit + integration | `pytest -m "not e2e and not docling"` with a branch-coverage floor (`fail_under` in `pyproject.toml`, currently 85 against 88 measured) | `scripts/check.sh`, CI |
| Docling integration | `pytest -m docling` with `PA_DOCLING_ARTIFACTS_PATH` set | locally (needs the models) |
| End-to-end against a live Ollama | `scripts/check.sh --e2e` | locally / on the server |
| Dependency vulnerabilities | `pip-audit --strict` over `uv export` of the lock | CI; `scripts/check.sh` without `--offline` |
| Secrets in history | gitleaks (full history) | CI; pre-commit on staged changes |
| Real documents never committed | `block-documents` pre-commit hook (pdf/doc(x)/xls(x)/archives outside `tests/fixtures/`) | pre-commit |
| Accuracy regression | `scripts/evaluate.py --set all` (synthetic gold committed; client gold git-ignored) | locally, before any extraction/check change |
| Security scorecard (static analysis, repo and machine hygiene, egress policy, the no-network run, authorisation matrix, log canary, deployment policy, regex budget, worst-case resource ceilings, security mutation) | `scripts/security_audit.sh` (`--quick`, `--online` adds pip-audit) | locally, before a release |
| The product runs with no network at all | `scripts/verify_offline.sh` | CI job `offline`; locally |
| Security tests fail when their guard is removed | `scripts/mutate_security.py` | CI job `mutation`; locally |

Warnings raised by `procurement_agent` fail the test run; third-party warnings are silenced
one message at a time in `pyproject.toml`, never by category.

## 5. Deployment

### 5.1 Topology

```
browser ──TLS──► company reverse proxy (host) ──► 127.0.0.1:8000 ──► proxy (Caddy relay)
                                                                        │ internal network,
                                                                        ▼ no default route
                                                          app:8000 ◄──► ollama:11434
```

Docker does not publish ports of a container whose only network is `internal: true` (checked
on Docker 29.7: the mapping is dropped without an error). That is why the relay exists: it is
the only container on a second network (`edge`), holds no state, and forwards to the app.
`edge` has outbound NAT disabled, so the published port answers but the relay cannot reach the
internet (measured: outbound `wget` times out). The app and Ollama have no route at all, and
they sit on two different internal networks (`internal` with the relay, `llm` with each other) so
the relay cannot reach Ollama; verify with the command in SECURITY.md §2.

### 5.2 Reverse proxy duties (deployer)

- [ ] Terminate TLS with the company certificate and redirect HTTP to HTTPS. (The app sends
  `Strict-Transport-Security` itself in production; a proxy-level header is optional.)
- [ ] Set `X-Forwarded-For` to the client address (nginx: `proxy_set_header X-Forwarded-For
  $remote_addr;`, which also overwrites anything the client sent). The relay reads the
  address right-to-left and passes one value on; the app trusts only the relay
  (`FORWARDED_ALLOW_IPS`). Without this header every user shares one rate-limit bucket and the
  audit log records the Docker gateway instead of the user.
- [ ] Pass the original `Host` header (nginx: `proxy_set_header Host $host;`; nginx otherwise
  sends `127.0.0.1:8000`), and list the names users type in `PA_ALLOWED_HOSTS`: the app answers
  400 to any other name, and compose refuses to start without the variable. Current browsers send `Sec-Fetch-Site` over HTTPS and the CSRF check
  uses that; for a browser that does not, the check falls back to comparing `Origin` with
  `Host`, and a rewritten `Host` would refuse every UI form post. The relay passes `Host`
  through unchanged.
- [ ] Allow request bodies of at least 2 × `PA_MAX_FILE_MB` + 1 MB (101 MB at the default) and
  keep read timeouts long enough for a 100 MB upload on the slowest client link.
- [ ] Do not cache `/api/` responses (the app already sends `Cache-Control: no-store`).

### 5.3 Host and volumes

- [ ] `/data` (the `app-data` volume) on an encrypted disk (LUKS or equivalent).
- [ ] `.env` readable only by the operator (`chmod 600`); `PA_ENV=prod`, `PA_DEV_API_KEY`
  empty, `PA_ALLOWED_HOSTS` set. `PA_APP_CPUS` / `PA_OLLAMA_CPUS` below the host's core count keep
  one runaway parse from starving the host (no default: Docker refuses a number above the cores).
- [ ] If `172.30.231.0/24` or `172.30.232.0/24` collides with a host network, set `PA_INTERNAL_SUBNET`/`PA_EDGE_SUBNET`/`PA_EDGE_GATEWAY` and
  `PA_PROXY_IP` in `.env` together.
- [ ] Copy `./models` and the `ollama` volume from the connected machine that ran
  `scripts/prefetch_models.sh`, after writing the checksum manifest there
  (`python -m procurement_agent.cli models-manifest write`): production verifies it at every
  start and refuses to run on a changed, missing or extra model file. The spelling dictionary is not among them: the image carries
  it, fetched and hash-checked by `docker compose build`, which needs the network anyway.
- [ ] Reference data, loaded the same way - prepared on a connected machine, copied in, imported,
  the copy removed:
  - NBG exchange rates (rule 9): `python3 scripts/fetch_nbg_rates.py --from <first day needed>
    --out nbg.json` (repeat as acts need newer days; a day's rate can also be typed on the report),
    then `docker compose cp nbg.json app:/data/nbg.json`, `docker compose exec app python -m
    procurement_agent.cli load-fx-rates /data/nbg.json`, `docker compose exec app rm
    /data/nbg.json`; `fx-rates-status` lists the days held.
  - The business registry (rule 1): the client's export as CSV (`id_code,name,address,directors,
    status,as_of[,legal_form]`), imported with `load-registry <csv> --as-of <export date>` and
    `PA_REGISTRY_DB` set; or a party's NAPR extract uploaded with the act.

### 5.4 Keys

```bash
docker compose exec app python -m procurement_agent.cli create-key <user>   # shown once
docker compose exec app python -m procurement_agent.cli list-keys           # id, prefix, state
docker compose exec app python -m procurement_agent.cli revoke-key <key-id> # also ends its UI sessions
```

Rotation is create-new, hand over, revoke-old; there is no in-place rotation. One key per
person, so revocation and the audit trail stay per person.

### 5.5 Backups

`app.db` holds keys (hashed), sessions and the audit trail; SQLite runs in WAL mode, so copy it
with the backup API, never with `cp` of a live file. The image has no `sqlite3` binary; use
Python:

```bash
docker compose exec -T app python -c "import os,sqlite3,sys; p='/tmp/app.db.bak'; s=sqlite3.connect('/data/app.db'); d=sqlite3.connect(p); s.backup(d); d.close(); s.close(); sys.stdout.buffer.write(open(p,'rb').read()); os.remove(p)" > ./backup/app-$(date +%F).db
```

The copy is streamed out because `/tmp` is a tmpfs and `docker compose cp` cannot read tmpfs
mounts. Check it with `sqlite3 ./backup/app-<date>.db 'pragma integrity_check'`.

Uploaded documents and results are deliberately short-lived (`PA_RETENTION_DAYS`); backing
them up extends their lifetime past the retention promise, so decide that with the client
rather than by default. Store backups encrypted.

Restore (the stale `-wal`/`-shm` files must go, or SQLite replays them onto the restored file):

```bash
docker compose stop proxy app
docker compose run --rm --no-deps -T --entrypoint sh app -c \
  'cat > /data/app.db.restore && rm -f /data/app.db-wal /data/app.db-shm && mv /data/app.db.restore /data/app.db && chmod 600 /data/app.db' \
  < ./backup/app-<date>.db
docker compose up -d
```

Both procedures were exercised against the current image with a WAL-mode database on a
read-only container with tmpfs `/tmp`.

A release that changes the schema takes one more copy by itself before it does (5.6). It sits on
the same disk as the database and has file names and typed text blanked, so it does not replace
this backup.

### 5.6 Upgrade procedure

1. Back up `app.db` (5.5). The release takes a copy of its own before it changes a database that
   holds data (below), but that copy is on the same disk: it covers an upgrade that goes wrong,
   not a lost disk.
2. Build the new image on a connected machine, `docker save` it, move it, `docker load`.
3. See what the start will do before it does it: `docker compose run --rm --no-deps app python -m
   procurement_agent.cli db-status`. It prints the database's schema version, the image's, and the
   migrations still pending, from a read-only look that changes nothing and prints no row. Exit
   status 1 means the database is **newer** than this image (a later release wrote it: do not start
   this one) or damaged.
4. `docker compose up -d` - the worker stops within its timeout, the new process recovers
   interrupted jobs and brings the database to the image's schema version (below).
5. `docker compose ps` shows all three services healthy; sign in once. `db-status` now says
   `up to date`, and `cli audit --action db.migrate` shows what ran.
6. Re-run `scripts/evaluate.py` if Ollama, Docling or the models changed.

**Schema versions.** The version is stored in the database file (`PRAGMA user_version`, no table of
its own). At start:

| The database | The start |
|---|---|
| does not exist, or has no tables | creates it at the image's version; nothing to migrate, no copy |
| written before versions existed (version 0) | checks the tables and columns against the version-1 schema, adds tables that are missing, records version 1 |
| older than the image | takes a copy (if it holds rows), then applies each pending migration in order, each in a transaction of its own that also records the new version |
| the same version | nothing to migrate: the version is read, no migration lock is taken |
| newer than the image | refuses to start ("database schema version N is newer than this release") and touches nothing, so an older image cannot damage it |

A migration that fails is undone with its own transaction: the database stays at the last version
that succeeded, the start fails with the migration's number and name and the *kind* of failure
(never a value), and the next start resumes from there. A version-0 database that is not shaped as
expected - a table without a column the image needs, a column it does not know - is refused with
the table named (an unknown column is counted, not named: the name came from the file) and left as
it was. The check is of names, not of types or indexes, and nothing is ever altered by guesswork:
restore a backup from a release that wrote the expected tables, or export what is needed and start
a new database. Two processes starting together (the app and a `cli` command) take the write lock
in turn; the second finds the work done, and waits at most 30 s for the first.

**The copy.** Before the first migration of a database that holds rows (a schema with no rows in it
is not copied), SQLite's online backup API reads a consistent snapshot into memory, and what is
written to `/data/db-backups/app-v<version>-<UTC time>.db` is that snapshot with every column that
holds text a person typed, a file name or a value derived from a document **blanked**:
`documents.display_name` (becomes `[scrubbed]`), `documents.sha256`, `documents.error`,
`jobs.params` (the question and options typed; becomes `{}`), `jobs.message` and
`check_runs.params`. The blanking happens before anything is written, so the text is never in the
directory. What stays is what a copy needs to be worth restoring - key hashes, session-token hashes,
account names, ids, statuses, sizes, times and the audit trail. The directory is `0700`, the file
`0600`, the version in the name the one the database stood at. The copy is checked (`PRAGMA
quick_check`) before anything changes; if it cannot be made or fails the check, the upgrade does not
start.

A copy is kept only when the upgrade succeeded, and then the three newest; a start that failed
before changing anything removes the copy it took (it is the file as it stands), so a start that is
retried again and again does not push out the copy from before the upgrade, which is the one to go
back to. Older ones are overwritten, then deleted.

**How long a copy lives: `PA_DB_BACKUP_KEEP_DAYS`** (default 3; 0 to 365). The retention sweep (every
10 minutes and `cli sweep`) overwrites and deletes a copy taken more than that many days ago, so
what a copy keeps is bounded like the documents are. In the worst case: a file name, typed text or a
document hash is in a copy for 0 days; an audit row removed by `audit-prune`, or a key revoked since
the copy, is in it for at most `PA_DB_BACKUP_KEEP_DAYS` days plus the sweep interval (and while the
stack is stopped, until its first sweep after it starts). **0 trades the way back for the shortest
lifetime:** the first sweep after the new release is up removes the copy, so if the release turns
out wrong the only way back is your own backup (5.5). A start that fails does not bring the
application up, so no sweep runs: the copy of an upgrade that was partly applied is still there. To
remove copies at once:
`docker compose run --rm --no-deps --entrypoint sh app -c 'rm -rf /data/db-backups'`.

**Going back.** There are no down-migrations. An older image will not start on a newer database
(`db-status` says so first). To return, stop the stack, restore the copy taken before the upgrade
(`<N>` is the version you are returning to) and start the older image; what was written after the
upgrade is lost with it. The restored database has the blanked columns as above: documents show
`[scrubbed]` for their names, the parameters of jobs that were queued or running when the copy was
taken are gone (they fail on the restart), and the key list is the one of the copy - **a key revoked
since is active again until it is revoked once more** (`cli revoke-key`):

```bash
docker compose stop proxy app
docker compose run --rm --no-deps -T --entrypoint sh app -c \
  'rm -f /data/app.db-wal /data/app.db-shm && cp /data/db-backups/app-v<N>-<time>.db /data/app.db && chmod 600 /data/app.db'
docker compose up -d                  # with the older image loaded
```

**Upgrading to the release with server-side sessions signs every UI user out once**: the old
cookie held the API key itself and is no longer accepted. API clients using `X-API-Key` are
unaffected.

#### Adding a migration (developers)

1. Change the model in `models.py` as usual.
2. Write the step in `migrations.py` and append it to `MIGRATIONS` with the next number. DDL by
   hand - not `create_all`, not the live models, which move on with every release:

   ```python
   def _add_note(conn: Connection) -> None:
       conn.exec_driver_sql("ALTER TABLE documents ADD COLUMN note VARCHAR(255)")


   MIGRATIONS = (Migration(2, "document_note", _add_note),)
   ```

   `LATEST_VERSION` follows from the list; a name is a lower-case identifier (it is audited).
3. `python scripts/freeze_schema.py tests/fixtures/schema_v2.json`, and commit the file.
4. `pytest tests/unit/test_migrations.py`: a new database must have exactly the recorded schema,
   and the version-1 record upgraded through every migration must have what a new one has - so a
   migration that disagrees with the model fails, and so does a model change without a migration.

A column that will hold text a person typed, a file name or a value derived from a document's
content also goes into `SCRUBBED_COLUMNS` in `migrations.py` - never remove an entry, a copy is of
the schema before the migration - and `tests/unit/test_migrations.py` fails until every column is
either there or on its list of kept ones.

The rules for a step: the connection is the transaction's, so it does not commit, roll back or
begin one. Foreign keys are off on it (a table can be rebuilt - create, copy, drop, rename -
without deleting what references it) and `PRAGMA foreign_key_check` must be empty afterwards, or
the step is undone. `audit_log` is guarded by triggers that abort `UPDATE` and `DELETE`: a step
that has to change audit rows drops the trigger and puts it back in the same transaction, as
`prune_audit` does. Nothing a step raises is quoted (the start shows its class name), and it must
not log a row's content. `BASELINE_SCHEMA` is the frozen version-1 shape that a version-0 file is
held to: leave it. `create_all` still runs after the migrations on every start, as it did before
versions, and creates a table that is missing; do not rely on it - the test above is what notices
a table that was added to the models without a migration.

### 5.7 Pinning

The Dockerfile pins its two base images by tag **and digest** (`python:3.12.14-slim-bookworm`,
`uv:0.12.8`, as resolved on 2026-10-02), compose pins Ollama `0.34.2` and Caddy `2.11.4` by tag and
digest, and CI pins every action by commit (the tag stays in a comment) and the gitleaks image by
digest. To move a pin on purpose, use the commands in the file headers, and re-run
`scripts/evaluate.py` when it is Ollama, Docling or the models. Debian packages installed at build
time (Tesseract and its language data) are not pinned: the build needs the network anyway, and a
rebuild takes the distribution's current security fixes.

`torch` and `torchvision` are direct dependencies from the PyTorch **CPU** index, so `uv.lock`
holds no CUDA wheels (the 19 NVIDIA/CUDA/triton packages, about 2.5 GB, are gone), and OpenCV is
the headless build (no Qt or window-system libraries); `rapidocr`'s request for the GUI build is
overridden in `pyproject.toml`. The re-lock moved torch and torchvision one patch release (2.14.0 →
2.14.1, 0.29.0 → 0.29.1) and nothing else. Docling was run again over every evaluated document in
that environment; the comparison is in `docs/SECURITY.md` §11.

## 6. Known limitations

- API keys only; no directory (LDAP/AD/OIDC) integration yet.
- No application-level encryption at rest; relies on disk encryption.
- Parsing runs inside the web process (no sandbox), so a native-parser bug runs with the
  process's access to the stored documents, and a runaway parse is stopped by the supervisor's
  restart, not by a hard limit; the input limits are a bound, not a proof. SECURITY.md §10 has
  the full list of what the security work does not cover.
- One uvicorn worker and one job thread by design; the rate limiter's counters live in memory.
- Migrations only go forward. Going back is restoring the copy taken before the upgrade (5.6), and
  what was written since is lost. That copy is on the volume it protects, blanked of typed text, and
  removed after `PA_DB_BACKUP_KEEP_DAYS`. A database from before schema versions is held to the
  version-1 table and column names, not to types, indexes or constraints.
- OCR runs only inside the image (no host Tesseract), so scanned PDFs are not in the host eval.
- The accuracy numbers are fitted to the client's own set, not held-out (ACCURACY.md).
- pip-audit (needs network access) and gitleaks run in CI. pip-audit was also run by hand on
  2026-10-02 over the re-locked `uv.lock` (the 154 packages that apply on Linux): no known
  vulnerabilities. gitleaks itself has not been run on this machine (no binary); an offline scan
  of every commit of every ref and of the working tree for credential-shaped strings found none
  (`scripts/security_audit.sh`, step `repo_secrets` - a smaller rule set than gitleaks', so CI
  stays the gate). Uncommitted work is not covered by CI until it is committed.

## 7. Deferred / open questions

Measured or decided during the 2026-09-23 hardening pass; each is a known limitation, not an
unknown one.

- **Compare worst case at `PA_COMPARE_MAX_SEGMENTS` (20 000 segments per side):** measured on
  two synthetic 4 000 × 5 workbooks (one deleted row, ~100 edited cells): 1.4 s and 959 MB
  peak RSS for the whole process (the ML libraries account for most of it). The cap can stay.
- **Numbers-only spreadsheet rows are aligned by position.** A row with no text to identify
  it is matched to the row at the same place, so deleting one row near the top of a
  10 000-row numeric sheet reports ~20 000 changes. Needs a content-based row identity.
- **Very large text documents with no line unique to both sides** are reported as a wholesale
  replacement rather than a line diff.
- **AOA act-date rule intent (client decision needed):** instruction 8 of the client's rules
  says the act date should equal the execution date, and its example says that for a period
  service (1–31 August) the act is signed on the last day of the month. The rule implements
  the example (month-end). The readings differ only for a partial-month period such as
  1–15 August (instruction: the 15th; rule: the 31st). Unchanged until the client confirms.
- **Token estimate:** re-derived from `gemma3:4b` measurements on synthetic text (Georgian
  0.41–0.52 tokens/char → counted as 0.6; Mtavruli capitals 1.44–1.54 → 1.7). It is now a
  safe over-estimate; `MAX_CHUNK_TOKENS` (1550) and `PA_LLM_CHUNK_TOKEN_BUDGET` (7800) were
  raised with it so the same real amount of text is chunked and selected as before.
  Re-measure if the model family changes.
- **Ambiguous numbers:** "1.125" is read as 1125 (Georgian thousands grouping); a separator
  followed by four or more digits is always decimal, and spreadsheet values are written so
  they cannot be misread. Where context says otherwise (a unit price of 1.125) the text alone
  cannot decide.
- **VAT fact disagreement:** a model's VAT verdict ("incl.") and a rule's VAT rate (18%) have
  different keys and are shown as a conflict. Needs a product decision on one VAT field.
- **Dev-key sessions** survive a rotation of `PA_DEV_API_KEY` until they expire (dev only;
  prod refuses a dev key at start-up).
- **httpx2 for the test client:** adopt `httpx2>=2.12` once it can be fetched (2.11.0, the
  only offline-cached version, has PYSEC-2026-3846); until then one Starlette warning is
  filtered by message.
- **Release pinning:** done on 2026-10-02 for the Dockerfile's base images (digests), the CI
  actions (commit SHAs) and the gitleaks image (digest); the pre-commit gitleaks hook is pinned by
  tag. The image has been built on this machine before, but **not since** the lock went CPU-only
  and the Dockerfile took the supervisor, the digests and the offline environment: build it once
  on a connected machine (`docker compose build`), check its size and `docker compose up`, before
  relying on it. The policy tests (`tests/unit/test_deployment_policy.py`) check the Dockerfile's
  and compose file's content, not that they build; the compose stack's relay, networks and
  healthchecks were verified on a probe stack.
- **Git history:** before this pass, a client party name appeared in a docstring and in
  `docs/TEST_REPORT_2026-09-21.md`; both are replaced in the working tree but remain in the
  already-committed history. Rewrite history (e.g. `git filter-repo --replace-text`) before
  the repository is shared outside the team.
- **Audit claims that did not reproduce** (kept as guard tests, not counted as fixes): a rule
  span fully containing an earlier one (unreachable with the current patterns); the BOQ item
  number being read from the column after "No" (correct for the real template); cache writes
  failing on the read-only container filesystem (a read-only probe container parsed and
  extracted without errors; the cache variables are set as a precaution).
- **Test provenance:** most hardening tests were confirmed to fail on the old code first. The
  session, audit, UI-error, `/ready`, threadpool and throttle tests were written after their
  fixes; three of them guard behaviour rather than reproduce an old bug.
- **Stale evaluation cache:** `.eval_cache/` folders named by file hash alone predate the new
  cache key and are unused; they hold parsed text of client documents and should be deleted by
  the operator (they are git- and docker-ignored).
