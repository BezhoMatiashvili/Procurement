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
| The relay applies the same table first and answers the same 413 itself, from the announced length (a chunked body is cut off by `request_body`); the oversize body is never forwarded. Without it the app's early 413 and close showed through the relay as 502 (measured on the three-container stack: 29 of 30 requests with a 1.2 MB body; with it, 30 of 30 are 413) | [x] | `docker-compose.yml` (`proxy`), `tests/unit/test_relay_policy.py` fails when the two tables drift |
| Hostile-input bounds: no exponential or quadratic reading of document text (patterns fixed, runs and tokens bounded, text/cell budgets), bounded rendering, workbook cell count refused before load, one lock around the app's own pdfium use | [x] | `ingestion/text_limits.py`, `ingestion/xlsx_size.py`, `ingestion/render.py`, `ingestion/pdfium_lock.py`; `tests/regex_budget.py`, `scripts/audit_resources.py` |
| Bounded report work: at most `PA_MAX_CONCURRENT_REPORTS` computed at once, the rest 503 + `Retry-After` | [x] | `security/capacity.py` |
| Annotated Word output drops external relationships, embedded objects, risky fields | [x] | `report/docx_sanitize.py` |
| Formula-injection escaping in every generated workbook | [x] | `report/xlsx_safe.py`, used by all four writers: BOQ merged workbook, comparison export, extraction export, `scripts/boq_compare.py` |
| Append-only audit trail (DB table with triggers that abort UPDATE/DELETE + JSON log line); reads, key operations, denied probes and refused outbound attempts included | [x] | `db.py`, `security/audit.py`, `cli audit`, `cli audit-prune`; see SECURITY.md §4 for the actions |
| Content-free logs and stored errors, proven by a canary test through every input path | [x] | `security/safe_errors.py`, `security/logscrub.py`, `tests/integration/test_log_canary.py` |
| Retention: row first, files second, a reconciler that retries what failed, derived outputs and caches removed, overwrite + fsync, SQLite `secure_delete` | [x] | `jobs/retention.py`, `jobs/worker.py` sweep every 10 min, `cli sweep`, `PA_RETENTION_DAYS` |
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
  keep read timeouts long enough for a 100 MB upload on the slowest client link. (The relay
  behind your proxy refuses anything larger with the app's 413; it reads `PA_MAX_FILE_MB` from
  the same `.env` and will not start on a value that is not a whole number.)
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

### 5.6 Upgrade procedure

1. Back up `app.db` (5.5).
2. Build the new image on a connected machine, `docker save` it, move it, `docker load`.
3. `docker compose up -d` — the worker stops within its timeout, the new process recovers
   interrupted jobs, `create_all` adds new tables. There is no migration framework: a release
   that changes an existing table must ship its own migration step (none has so far).
4. `docker compose ps` shows all three services healthy; sign in once.
5. Re-run `scripts/evaluate.py` if Ollama, Docling or the models changed.

**Upgrading to the release with server-side sessions signs every UI user out once**: the old
cookie held the API key itself and is no longer accepted. API clients using `X-API-Key` are
unaffected.

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
