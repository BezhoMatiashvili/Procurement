# Procurement Agent

Local, air-gapped document intelligence for a procurement department. Phase 1 extracts prices,
totals, VAT, quantities, deadlines, payment/warranty/validity terms, penalties, contract numbers
and dates, party names, ID codes and signatories from **PDF / DOCX / XLSX** documents in
**Georgian and English**, each fact with a page/bbox (or sheet/cell) citation and a verbatim
quote. Everything runs on-premise on CPU: [Docling](https://github.com/docling-project/docling)
for parsing, Tesseract for OCR, a local LLM via Ollama (Gemma 3), FastAPI + a small web UI.

Security and data handling: **[docs/SECURITY.md](docs/SECURITY.md)**.
Deploying and operating it: **[docs/PRODUCTION_READINESS.md](docs/PRODUCTION_READINESS.md)**.
Accuracy, measurements and design decisions: **[docs/ACCURACY.md](docs/ACCURACY.md)**.
What we need from the client to test: [docs/CLIENT_REQUEST_ka.md](docs/CLIENT_REQUEST_ka.md).

## How extraction works

```
upload ─► validate (type, size, magic bytes, zip-bomb, macros)
       ─► Docling (layout + TableFormer + Tesseract kat+eng)  |  openpyxl for XLSX
       ─► chunks with provenance
       ─► 1. anchors: read each field from the label in front of it — „…“ company names,
             საიდენტიფიკაციო კოდი, იურიდიული მისამართი, დირექტორი, ს/კ, ა/ა  (no LLM)
       ─► 2. rules: amounts, dates, %, durations, reference № (no LLM, all chunks)
       ─► 3. LLM: only for targets 1 and 2 left empty; JSON-schema, quote must exist verbatim
       ─► 4. merge: dedupe, cross-source confidence, conflict flags
       ─► JSON / Excel / UI table with "show source" page images
```

Georgian procurement documents are rigidly templated, so step 1 reads the fields exactly and
instantly: parties, codes, directors, accounts, bank codes, projects and plots by their labels,
and amounts, dates, periods, payment terms, VAT status, penalties and the subject by rules that
know their Georgian wordings - every field in its canonical form (names in the nominative, ISO
dates and ranges). The acceptance-act check never calls the LLM, which is why a check that used
to take about ten minutes takes under a second. The model is off by default; asked, it only
suggests fields the rules left empty, and its values are validated (see docs/ACCURACY.md).

The LLM never sees the whole document and can never act — it only returns JSON that is
validated and quote-checked against the text.

## The interface

The UI asks what you want to do, then collects the files that task needs. It is
**Georgian by default** with a ka/EN toggle in the header — the findings have carried
`message_ka`/`message_en` from the start, and so now do the "could not run" notes.

```
/ui/  ─►  Check an acceptance act   ─►  act + contract      ─►  findings, skips, act with comments
      ─►  Compare BOQ bids          ─►  template + bidders  ─►  leaderboard, diffs, merged workbook
      ─►  Compare two documents     ─►  two versions        ─►  changed values first, then the diff
      ─►  Extract fields            ─►  one document        ─►  facts with citations  (phase 1)
```

Uploads happen from the task screen (drag and drop, one request per file, a progress row each),
and the report URL carries the document ids — so a result is bookmarkable and re-runnable.
Every report is recomputed from the stored files whenever it is opened, and the database keeps
only enough to open it again: one small `check_runs` row per check — which check, over which
documents, with which options, never what the documents say. The page of each uploaded file
lists the checks it took part in and reopens them; the row goes with its documents. The same
page lists the file's jobs, and every job opens its result: the reading job shows what was read
from the file (text, tables, sheets with their cell positions), an extraction its fields, and a
model job the report it belongs to. Checks opened before `check_runs` existed were never
recorded, so they appear once they are opened again. When retention has swept the documents the
page says so instead of rendering a raw 404.

## Checks

```bash
# acceptance act against its contract: 32 rule ids, fully deterministic, no LLM
curl -s -H "$K" -X POST localhost:8000/api/v1/checks/aoa \
     -H 'content-type: application/json' \
     -d '{"aoa_document_id":"<act>","contract_document_id":"<contract>"}'
# ?format=docx returns the act with one Word comment per finding. For an act in a foreign
# currency, "advance_amount" (in the act's currency; 0 = none) and "advance_date" (ISO) give the
# advance paid; left out, the act's own is used
# optional: ask the local model about the notes the rules could not decide (subject, services);
# 202 queues a job, 200 reuses the one that asked the same thing, "job": null means none open
curl -s -H "$K" -X POST localhost:8000/api/v1/checks/aoa/review \
     -H 'content-type: application/json' \
     -d '{"aoa_document_id":"<act>","contract_document_id":"<contract>"}'
# once that job is done, add "review_job_id":"<job>" to the check above to apply its reading
# BOQ: all bidders merged into the template, stacked vertically, highest red / lowest green
curl -s -H "$K" -X POST localhost:8000/api/v1/checks/boq \
     -H 'content-type: application/json' \
     -d '{"template_document_id":"<tpl>","bid_document_ids":["<a>","<b>"]}'
# ?format=xlsx returns the merged workbook; the CLI does the same thing offline
uv run python -m procurement_agent.checks.boq --template template.xlsx \
    --bids Company-*.xlsx --out merged.xlsx
# two versions of one document: a line-by-line diff plus the values that changed
curl -s -H "$K" -X POST localhost:8000/api/v1/checks/compare \
     -H 'content-type: application/json' \
     -d '{"left_document_id":"<old>","right_document_id":"<new>"}'
# ?format=docx returns the newer file with a Word comment per difference; ?format=xlsx a table
```

## Comparing two documents

```
segment both files (paragraphs, list items, table cells, spreadsheet cells)
  ─► align  (difflib over normalised keys — the same primitive checks/boq.py uses on BOQ lines)
  ─► 1. positional diff: added / removed / changed / moved, word-level inside a changed line
  ─► 2. entity diff:     run_rules + find_anchors on EACH SIDE of a change, paired by fact type
  ─► 3. field diff:      the whole document, keyed by fact type, independent of alignment
  ─► 4. optional: ask the model about the changes the rules could not name a value for
```

Classification is per **entity**, never per hunk. A prototype that judged whole paragraphs filed
a bank code naming a different bank as "text changed", because the digits in that paragraph were
untouched; reading the typed values out of both sides is what makes it a banking difference.
That is also why a paragraph holding two amounts reports only the one that moved.

**The positional diff is not always the answer, and the page says so.** Two revisions of one
acceptance act align at 0.81–0.98; an act against its own contract draft aligns at 0.08. Below
`PA_COMPARE_MIN_SIMILARITY` the report leads with the field differences and states that these
are not two versions of one document, instead of drawing a diff that is pure noise.

Three differences a reader cannot see are reported on their own: a Cyrillic lookalike inside a
bank code (`info` rather than `warning` on scanned input, where Tesseract is the likelier
author), zero-width and bidirectional control characters, and a spreadsheet formula that changed
behind an unchanged cached value. Quotes, dashes, spacing and `1 000,00` against `1000.00` are
reported as formatting and never as a change.

**The model is off unless asked.** The deterministic report renders in milliseconds; an
"explain with the model" button queues one batched call about the rewritten passages that carry
no price, date, period or bank detail — the only thing the rules cannot name. It may explain a
change and mark it immaterial; it can never introduce one, and every quote it returns is checked
against the passage it claims to come from. Set `PA_COMPARE_LLM_ENABLED=false` to remove the
button.

A rule that cannot run says so in `skipped` — bilingually, and in its own "could not run, and
why" section of the report, never styled as a pass — rather than passing quietly — the contractor type
for the NET/VAT wording rules, or a business-registry snapshot that has not been loaded
(`cli load-registry`). A rule the client's instructions do not apply to a contractor type (the
NET/VAT wording for a company) is named in the report as not applicable instead, since nothing
failed to run. Findings carry a stable rule id, a bilingual message, the expected value
and a character span, so the same object serves the API, the Excel export and the Word comments.

An act written in a foreign currency gets one note with the invoice it needs in lari, at the
NBG rate: the advance at the rate of the day it was paid, the rest at the rate of the act's
date. No rate source is connected yet (`PA_FX_RATE_SOURCE=none`, `fx/provider.py`), so the note
gives the formula and the days to look up, and a skip note says the total was not calculated.
The advance is read from the act, or entered on the upload page or the report's re-run form,
where the one read from the act comes back to be confirmed; a contract's advance is never taken
for a payment, since a contract only plans one. A small business or a foreign organisation needs
no such invoice, and the report says the rule does not apply.

The parties are matched by identification code, and by name where the act gives none. A party
written with the other party's code is reported as a code on the wrong party
(`AOA.ID_CODE_PARTY`), not as a party with a wrong name, address and director. Each party must be
on the same side in the act as in the contract — the customer who pays, the contractor who is
paid (`AOA.PARTY_ROLE`, read only from the documents' own "(შემდგომში „…“)" definitions) — and
both of the contract's parties must be in the act (`AOA.PARTY_MISSING`). The act's restated
subject and its list of services are compared with the contract's word by word (`AOA.SUBJECT`,
`AOA.SERVICES`): a number that differs in an item that otherwise reads the same is an error, and
wording the rules cannot match is a note marked "needs review", in the report and in the Word
comment — which the rules never make an error or a warning, since other words are not a mistake.
On request, the local model reads those notes: "review with the model" queues one batched call
(`PA_AOA_LLM_ENABLED`; it never runs by itself unless `PA_AOA_LLM_AUTO` is set). Its reading is
marked on each note, "likely the same" or "likely different" — the second raises the note to a
warning — but it never clears a note or makes an error, a number that differs is never put to
it, and every quote it gives is checked against the text it was shown (`docs/ACCURACY.md`).

## Development setup

Requirements: Python 3.12 via [uv](https://docs.astral.sh/uv/), Docker (for Ollama),
optionally Tesseract with `kat` + `eng` data for OCR on the host.

```bash
uv sync --all-groups                      # deps (CPU-only torch wheels)
cp .env.example .env                      # set PA_DEV_API_KEY=pa_something for local use
./scripts/prefetch_models.sh              # Docling + spelling dictionary (once, online) + Ollama
docker run -d --name ollama -p 127.0.0.1:11434:11434 -v ollama:/root/.ollama ollama/ollama:0.34.2
docker exec ollama ollama pull gemma3:12b && docker exec ollama ollama pull gemma3:4b
```

That `docker run` container sits on Docker's default network and can reach the internet. To run
the same pinned Ollama with no route out (what production does), use `docker-compose.dev.yml`
through `scripts/switch_dev_ollama.sh` (a dry run by default; pulling models then needs
`--pull`, which uses a temporary connected container).

```bash
PA_OLLAMA_BASE_URL=http://127.0.0.1:11434 PA_DOCLING_ARTIFACTS_PATH=./models/docling \
  uv run uvicorn procurement_agent.main:app --reload
```

Open <http://127.0.0.1:8000/ui/> (sign in with the dev key); the API schema is at `/openapi.json`
(there is no interactive docs page: it loaded its script from a public CDN).

### API in 30 seconds

```bash
K="X-API-Key: $PA_DEV_API_KEY"
curl -s -H "$K" -F file=@tests/fixtures/contract_ka.docx localhost:8000/api/v1/documents
# {"document":{"id":"<doc>","status":"uploaded",...},"parse_job_id":"<job>"}
curl -s -H "$K" localhost:8000/api/v1/jobs/<job>                    # wait for "done"
curl -s -H "$K" -X POST localhost:8000/api/v1/documents/<doc>/extract \
     -H 'content-type: application/json' -d '{"fast": true}'         # or {"question": "..."}
curl -s -H "$K" localhost:8000/api/v1/jobs/<job2>/result             # ?format=xlsx for Excel
```

`use_llm: false` gives an instant rules-only result; `fast: true` uses `gemma3:4b`.

### Tests and checks

```bash
scripts/check.sh --offline                       # the CI gate locally: lock, ruff, format,
                                                 # mypy, bandit, tests + coverage floor
scripts/check.sh                                 # same, plus pip-audit (needs network)
PA_OLLAMA_BASE_URL=http://127.0.0.1:11434 scripts/check.sh --e2e   # plus e2e (slow, CPU)
uv run pytest -q -m "not e2e"                    # just the tests (no LLM needed)
uv run python scripts/gen_synthetic_samples.py   # regenerate synthetic fixtures
uv run python scripts/benchmark.py [--llm]       # timings on this machine
uv run python scripts/evaluate.py --set all      # accuracy against the gold sets
scripts/security_audit.sh [--quick] [--online]   # one scorecard: static analysis, no-network run,
                                                 # authorisation matrix, log canary, resource ceilings,
                                                 # security mutation (docs/SECURITY.md section 11)
scripts/verify_offline.sh                        # the whole product with only loopback up
```

### Measuring accuracy

`scripts/evaluate.py` scores extraction and checks against hand-written ground truth and
reports precision, recall and **anchor hit rate** per field — the last one separately, because
label anchors designed around one family of documents can score well on it and be useless on
the next, and only a hit-rate that drops makes that visible.

```bash
uv run python scripts/evaluate.py --set client --out eval_runs/baseline.json
uv run python scripts/evaluate.py --set client --compare eval_runs/baseline.json
```

Gold files live in `tests/fixtures/gold/` (synthetic, committed) and `samples/gold/`
(the client's documents, git-ignored — their expected values *are* the confidential data).

All fixtures are synthetic. Real documents must never be committed (a pre-commit hook and a CI
job block them; `scripts/client_canary.py install` adds a local hook that also refuses the
client's own names, tax IDs and bank accounts, which it knows only as salted hashes); keep client
samples in `samples/` (git-ignored). If an AI coding assistant works in this repository, it must
not be able to read them: the `.claude/settings.json` deny rules and `.cursorignore` do that,
as a guardrail rather than a boundary (docs/SECURITY.md section 10).

## Production deployment

```bash
./scripts/prefetch_models.sh          # on a connected machine; copy ./models + ollama volume
cp .env.example .env                  # PA_ENV=prod, PA_ALLOWED_HOSTS (required), retention, limits
python -m procurement_agent.cli models-manifest write   # on the connected machine, once, before copying ./models
docker compose build && docker compose up -d
docker compose ps                     # app, ollama and proxy should all turn "healthy"
docker compose exec app python -m procurement_agent.cli create-key alice
```

`docker-compose.yml` runs the app and Ollama on an `internal` network with no internet route,
non-root, read-only filesystem, dropped capabilities. Docker will not publish a port from an
internal-only network, so a stateless relay (`proxy`) is the one container listening on the
host, at `127.0.0.1:8000`. Put the TLS reverse proxy in front of that, have it set
`X-Forwarded-For` (audit IPs and rate limiting depend on it), and mount `/data` on an
encrypted disk. `/health` is liveness, `/ready` readiness (database + job worker). The full
checklist — proxy duties, backups, key rotation, upgrades — is in
[docs/PRODUCTION_READINESS.md](docs/PRODUCTION_READINESS.md).

The app runs under `scripts/supervise.py`, which restarts the server when `/ready` stops
answering (a request that wedges the process cannot be caught from inside it). CI
(`.github/workflows/ci.yml`) runs the same gate as `scripts/check.sh` plus pip-audit, a gitleaks
scan of the full history, the whole product in a network namespace with loopback only, and a
harness that removes each security control in turn and requires a test to fail.

## Accuracy and performance (this dev box: 18-core CPU, 30 GB RAM, no GPU)

Strict scoring against docs/GOLD_SPEC.md (canonical values compared exactly, every fact of an
unlisted type a false positive, every finding scored with its correction and the places its
Word comment marks), rules and anchors only:

| measure | result |
|---|---|
| client set, Test Set 2, Test Set 3 (strict `scripts/evaluate.py`) | every field and every finding matched; nothing missing, nothing unexpected |
| BOQ against an independent reference reader (`scripts/evaluate_boq.py`) | 106/106, 106/106, 132/132 checks: totals to the cent, common scope, colours, merged workbook |
| act check, mutation harness (20 error classes; 21,000 planted mistakes over four large runs) | the first three found 2 missed and 5 corrected in a wrong form, each fixed as a general rule; the final run on the final code: 6,000 of 6,000 detected, corrected in the act's own form and marked |
| harmless edits (14 classes: styles, layouts, formats; 14,700 cases) | 0 false findings |
| comparison of the same mutated pairs | 100% per class, but a director slip that breaks the surname's declension (99%) |
| BOQ mutation harness (17 classes, 7 seeds, the last 25 a class) | 100% of valid mutants |
| scanned acts (OCR) against the Word contract | every planted mistake reported, no false error |
| five adversarial red-team rounds | 285 confirmed defects, fixed test-first except the known limits in docs/ACCURACY.md; the rate fell from 17% to 8% of deliberately unusual variants |

**The sets above were looked at while fixing, so these are tuned numbers.** The honest ones are
the first runs on unseen data (Test Set 3: extraction precision 88.8% / recall 87.6% before any
fix) and the red-team rate: about one deliberately unusual wording or layout in thirteen still
finds a defect. Measurements, the held-out runs, the decisions and the known limits are in
**[docs/ACCURACY.md](docs/ACCURACY.md)**.

| step | measured |
|---|---|
| Docling, 1-page text PDF with a table | ~4 s |
| Docling, DOCX contract | ~0.1 s |
| anchors + rules extraction (any fixture) | < 0.1 s |
| AOA check, act against contract | ~0.02 s |
| BOQ: template + 4 bidders, 235 lines, merged workbook | ~2 s |
| compare two Word revisions (59 segments each) | ~0.01 s |
| compare two 13-sheet bid workbooks (2600 cells each) | ~0.8 s incl. reading both files |
| `gemma3:4b` (CPU, 16 threads), one ~1 250-token chunk | ~75 s (≈ 8.3 tok/s; 632 output tokens) |

The LLM figures improved from ~280 s because `num_ctx` is now sized per request instead of
being pinned at 8192, and the output is capped. But the bigger change is that on templated
documents the LLM is not called at all: it is asked only for targets the anchors and rules left
empty. Reproducibility (`temperature 0`, fixed seed) holds per machine configuration —
llama.cpp results can shift with thread and batch count, so record `PA_LLM_NUM_THREAD` with any
published number.

## Roadmap

Done: Phase 1 (document intelligence core), Phase 2a (BOQ comparison and merged workbook,
`checks/boq.py`), Phase 2b/3 (comparing two documents, `checks/compare.py`, with an optional
model pass in `checks/compare_llm.py`), Phase 4 (AOA verification against the contract,
`checks/aoa.py`, with Word comment output and an optional business-registry snapshot).

The BOQ leaderboard ranks on the total each bidder states on their own Summary sheet, shows the
recomputed line-item total beside it, and carries a badge wherever the two are not comparable —
on the client's own set every bidder left `5. MEP` unpriced, which is the kind of fact a colour
alone would hide.

On the client's six acceptance acts the comparison finds every planted error against the correct
version, including the two a value-blind diff would miss: `100 (ორასი)`, where only the Georgian
number words changed and every digit stayed put, and the bank code naming a different bank.

Next: Phase 3 proper — a contract draft reviewed against *itself* for internal inconsistency
(the client's own draft uses "შემკვეთი" and "დამკვეთი" for the same party), which the
comparison's entity layer does not cover because it compares two files rather than one; Phase 5
power-of-attorney register lookup; and an NBG rate source for the foreign-currency invoice,
which today stops at the formula (`fx/provider.py`).

How accuracy is measured, and which approaches were tried and rejected:
**[docs/ACCURACY.md](docs/ACCURACY.md)**.
