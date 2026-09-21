# Accuracy: what is measured, what was decided, what was rejected

This records how the system's accuracy is established and why the design is what it is, so
that decisions taken against measurements are not quietly re-litigated later.

## How accuracy is measured

`scripts/evaluate.py` scores the pipeline against hand-written ground truth. Per field it
reports three numbers, not one:

* **precision** — of what we returned, how much was right;
* **recall** — of what was in the document, how much we found;
* **anchor hit rate** — how often the deterministic label anchor fired at all.

The third exists because the anchors were designed by reading one family of client documents.
A set of patterns tuned to one template can score perfectly on it and be worthless on the
next, and only a hit rate that drops makes that visible. A field that scores well *and* stops
being anchored is a field the fallback is quietly carrying.

Findings are scored separately from fields. A gold file states which rule ids must fire, which
must not, and `"*"` for a document that must be completely clean.

| gold set | location | committed |
|---|---|---|
| synthetic | `tests/fixtures/gold/` | yes |
| client | `samples/gold/` | **no** — the expected values are the confidential data itself |

## Current results

Rules and anchors only, no LLM:

| set | precision | recall | findings | time |
|---|---|---|---|---|
| client, 7 Word documents | 100% | 100% | all matched, correct act clean | 0.7 s |
| synthetic, 4 fixtures | 100% | 100% | all matched | < 0.1 s |

## The central design decision: read the label, do not ask the model

Georgian procurement documents are rigidly templated. A company name is always inside `„…“`
after a legal form; an identification code always follows `საიდენტიფიკაციო კოდი:`; an address
follows `იურიდიული მისამართი:`; a signatory follows `დირექტორი`. Reading those directly is
exact, instant and explainable.

Before this, those fields were the LLM's job: 280–577 seconds per document on CPU, with
observed mislabelling (a cadastral code returned as a contract number). They are now read by
`extraction/anchors.py`, and the LLM is asked only for targets the deterministic passes left
empty. On the client's documents that is nothing, so no model runs.

Two guards keep this honest: an anchor that does not fire yields nothing and the fallback still
runs, and anchor hit rate is reported per field.

## Decisions taken against measurements

### Free-text spell checking is out of scope — twice rejected

The client asks for `მართლწერა` checking. Two general approaches were built and measured
against the client's own documents, and both were rejected:

1. **Compare the act's vocabulary with the contract's.** Georgian is heavily inflected, so
   `დირექტორი`/`დირექტორის` and `შემკვეთის`/`შემკვეთს` are one edit apart while both are
   correct. On the *correct* act this produced 17 findings — on the one document that must
   produce none. Stemming reduced it to about three, not zero.
2. **Use the offline hunspell `ka_GE` dictionary** (`hunspell-ka`, 143k entries, MIT/CC-BY).
   Its entries carry affix flags, so a plain wordlist lookup rejects 39% of the words in a
   correct document; it needs a full affix engine (`spylls`), and its coverage of legal and
   administrative Georgian is unmeasured.

What is reliable is checking the fields where an authoritative comparison target exists — a
name, an address, a project, a month inside a date, a number written out in words. **Every typo
the client planted lives in such a field**, and each is caught with no false positives. If
free-text checking is wanted later, the honest path is option 2 plus a measured false-positive
rate on the correct act; the dictionary is packaged and installable offline.

### IBAN checksums are information, never errors

`normalize.iban_is_valid` implements the ISO 13616 mod-97 check and validates real IBANs. The
client's sample documents carry anonymised account numbers whose check digits do not validate,
so a failing checksum is reported as `info`. Its real use is to tell a likely OCR error from a
genuinely different account once the act and the contract disagree.

Bank codes are different: a BIC is 8 or 11 characters and the client's documents carry real
ones, so a malformed code is an error. That rule is what surfaced `AAAGE22` in Mistake #3.

### Scanned PDFs cannot carry Word comments

The client asked for findings marked in the document itself. `report/docx_annotate.py` writes
real Word comments, splitting runs so the comment marks the offending words rather than the
whole paragraph. For the scanned PDFs (4172–4178) this is impossible; those inputs get the
findings report plus the existing bbox-highlighted page images, and the API returns 415 rather
than an empty document, so a scan is never silently served worse than a Word file.

## LLM configuration, and why

Three settings were established by reading what the model actually receives:

* **No system message.** `ollama show gemma3:4b --template` renders `system` and `user` as two
  consecutive `<start_of_turn>user` blocks. Gemma 3 has no system role and was not trained on
  consecutive user turns, so the instructions are prepended to the single user turn.
* **`repeat_penalty = 1.0`, `repeat_last_n = 0`.** Ollama defaults to 1.1 over the last 64
  tokens and applies it even under greedy decoding. Our output is JSON with repeated field
  names containing quotes copied verbatim from the document; that penalty discourages both.
* **`num_ctx` sized per request, and a prompt that will not fit raises.** Ollama truncates an
  over-long prompt silently and answers from the remainder, which reads as a confident wrong
  answer. Sizing the window also roughly halved observed generation time (4.3 → 8.3 tok/s).

Reproducibility (`temperature 0`, fixed seed) holds *per machine configuration*: llama.cpp
results can shift with thread and batch count, so publish `PA_LLM_NUM_THREAD` alongside any
number.

Confidence weights live in one table, `extraction/weights.py`, expressing one ordering:
`anchor > classified rule > LLM > unclassified hit`. They used to be literals scattered across
three modules.

## Known gaps

* The scanned PDFs are not yet in the gold set; OCR only runs inside the Docker image, so they
  must be scored there.
* `AOA.FX_INVOICE` (NBG-rate invoice calculation) is specified but not implemented; the rates
  table format is `data/fx/nbg_rates.csv` with `date,currency,rate`.
* The registry rules run only against a snapshot the client exports (`cli load-registry`); with
  no snapshot they are reported as skipped, never as passed.
