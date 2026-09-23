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

Rules and anchors only, no LLM (`scripts/evaluate.py --set all`, re-measured 2026-09-23):

| set | fields scored | precision | recall | findings | time |
|---|---|---|---|---|---|
| client, 7 Word documents | 13 types, 109 values | 84.38% | 74.31% | all matched, correct act clean | 0.6 s |
| client, the 7 anchor-friendly types¹ | 7 types, 80 values | 100% | 100% | — | — |
| synthetic, 4 fixtures | 9 types, 18 values | 100% | 100% | all matched | < 0.1 s |

¹ `bank_account`, `cadastral_code`, `party_director`, `party_id_code`, `party_name`,
`project_name`, `total_amount`.

The overall client figure is below 100% because the gold set now also labels fields that the
rules path does not attempt and the LLM path is meant to answer: `vat_status`,
`payment_terms`, `subject`, `execution_period` and `contract_date` score 0% rules-only, and
`penalty` 50% precision (one false positive). A number for these fields needs an LLM run
(`--llm`); see "LLM-dependent fields" below.

These are **fitting** numbers. The anchors were designed by reading this client's documents and
the rules were tuned while watching this score; the gold files were themselves corrected against
extractor output where the document, not the expectation, turned out to be right. So the result
says the system handles these documents, not that it generalises — and anchor hit rate, the
measure meant to expose overfitting, is necessarily 100% on the corpus the anchors came from.
It becomes informative on the next document family, which is when to look at it.

Not yet scored: the scanned PDFs (OCR runs only inside the Docker image), and `AOA.FX_INVOICE`,
which has no implementation and therefore no gold expectation.

The harness prints a `fragile` list: fields scoring well with **no** anchor coverage, carried by
keyword proximity alone. `total_amount` is on it — it is found by the nearness of `საფასურ` or
`ღირებულებ`, so a document that says `ანაზღაურება` instead would drop a field that currently
reads as perfect.

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
whole paragraph. Two details decide whether that works on real files: the acts lay their
numbered sections out in **tables**, which `Document.paragraphs` does not reach, and a finding
states a cleaned value (`შპს ნიმუში`) where the document writes `შპს „ნიმუში“`. With both
handled, 18 of 20 findings across five acts are marked in place rather than appended as a
general note. For the scanned PDFs (4172–4178) this is impossible; the API returns 415 rather than an empty
document, and the UI offers no download at all for a non-DOCX act, saying why instead of hiding
the button. Those inputs get the findings list and nothing more: `AoaChecker._add` never sets
`provenance`, only `span`, so there is no bbox for the page-image highlighter to draw — the
highlighting path exists for extraction facts, not for act findings.

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

## Closed-answer-space calls (`PA_LLM_CHOICES`, off by default)

Instead of letting the model write JSON, a *choice* call fixes the answer set before asking and
reads the probability the model put on each option. It is used for two things: deciding
`vat_status` (include / exclude / not stated — a rule finds a percentage near `დღგ`, but nothing
deterministic decides which case applies), and asking, after generation, whether the document
really states a value the model produced. The second is a different check from
`quote_in_chunk`: a quote proves the words are on the page, not that they mean what was claimed,
and a figure lifted out of a penalty clause is a genuine quote and a wrong total.

**Measured here on `gemma3:4b`, 18 CPU threads.** Prefill runs at roughly 45 tok/s, and the
server reuses a cached prompt prefix — but only above a size threshold:

| prompt tokens | 401 | 520 | 640 | 760 | 1000 | ~1050 | 1193 | 1241 |
|---|---|---|---|---|---|---|---|---|
| second question reuses the prefill? | no | no | no | no | no | no | **yes** | **yes** |

Below the boundary every question re-prefills the whole prompt; above it the second and later
questions cost ~0.75 s instead of ~27 s. The cut-off was not pinned down further and the reason
for it is not established — the table is an observation, not a mechanism. Chunks are about 1250
tokens, so real chunks sit in the reusable regime; a short chunk quietly does not, which is why
no code assumes the saving.

Three consequences are load-bearing, and each is asserted in `tests/e2e/test_choice_e2e.py`:

* **The document must be the prompt prefix and the question the suffix.** Reversed, reuse is
  lost outright. This is why `generate_choice` takes `document` and `question` separately.
* **All questions about one chunk must be asked together.** The cache holds one prefix, so
  interleaving another document evicts it.
* **A choice prompt shares no prefix with the generation prompt**, which leads with its
  instructions. So the *first* choice question against a chunk always pays a full prefill of its
  own — about 27 s against the generation call's ~75 s. That is the whole reason the pass is
  opt-in rather than on by default, and why a comparison run must report seconds as well as F1.

**The reported probability is not calibrated and is not used as a confidence.** On gemma3:4b it
is saturated: p = 1.0000 on questions with an obvious answer, even at `temperature 1`. It is
used only to *withhold* an answer — below `weights.CHOICE_MIN_CONFIDENCE`, or when the allowed
options hold less than `MIN_COVERAGE` of the mass, nothing is recorded — never to promote one.
Calibrating it properly would need far more labelled judgements than the seven documents here;
a temperature fitted on seven documents would be fitted to noise.

### Measured result: it does not pay, and the LLM path itself is a regression

Client set, 7 documents, 109 expected values, `gemma3:4b`:

| configuration | precision | recall | F1 | wall clock |
|---|---|---|---|---|
| **rules + anchors only** | **84.4%** | 74.3% | **0.790** | **0.8 s** |
| + LLM | 66.2% | 80.7% | 0.727 | 27.9 min |
| + LLM + choices | 71.0% | 80.7% | 0.755 | 44.9 min |

Two conclusions, and the second is the important one.

**The choice pass does what it was built to do.** Precision rises 66.2% → 71.0% with recall
unchanged to the decimal: the verifier removed values the model could not stand behind, and
removed nothing correct. That is the mechanism working as designed.

**It is still the wrong thing to run here, because the LLM path is a regression.** Rules and
anchors alone score the best F1 of the three. The LLM buys 6.4 points of recall for 18 points of
precision and roughly 2000× the wall clock; the choice pass only makes a losing configuration
lose by less. `PA_LLM_CHOICES` and the LLM pass both stay off, and these numbers are why.

**The prefix-reuse saving did not materialise at real chunk sizes.** Over 111 choice calls the
prefill was min 284 ms, **median 8669 ms**, max 21263 ms — most questions paid a substantial
prefill rather than the ~0.8 s the boundary table above promised. Real chunks measure ~1103
tokens and the choice envelope adds ~90, landing right on the threshold where reuse was observed
at 1193 and never at 1050. The cost is therefore roughly **one prefill per question**, not per
chunk, which is what the extra 17 minutes buys. The e2e reuse test passes because its fixture is
~2000 tokens; it proves the mechanism and the prompt ordering, not that production benefits.

**`vat_status` scored 0% even with choices, for two compounding reasons.** The model answered
"not stated" to the VAT question almost every time and chose `incl` exactly once — though the
acts say `გადასახადების ჩათვლით` and the contract says `მოიცავს გადასახადებს მათ შორის დღგ-ს`.
And the once it did commit, the emitted value is the English gloss from `VAT_MEANING` while the
gold label is the Georgian phrase, so it could not have matched regardless. The second of those
is a fault in `_decide_vat`, not a model limitation: a choice answer needs to carry a canonical
token the scorer can compare, which is what `normalized` is for.

**Caveat on all of it.** Seven documents, and the labels for the newly scored fields are
candidates written in one sitting (see `notes:` in each gold file). The direction is clear and
the wall-clock figures are solid, but a 5-point precision difference on this corpus is not a
precise quantity.

**`anchor_hit_rate` is not comparable across these rows.** `total_amount` reads 0% anchored in
the rules-only run, 42.9% with the LLM and 14.3% with choices. The field has no anchor in any of
them: `score.py` counts a fact as anchored when its source is `anchor` **or `both`**, and `both`
is what a rule and an LLM agreeing produce. The metric therefore measures different things per
configuration, and only the rules-only column is a true anchor rate.

## LLM-dependent fields

`DEFAULT_TARGETS` has twenty fact types. The client gold set labels thirteen of them; the
rules-and-anchors path reads seven of those exactly (the table above) and leaves
`vat_status`, `payment_terms`, `subject`, `execution_period`, `contract_date` and `penalty` to
the model. Those six are therefore the fields any claim about the LLM path has to be measured
on (`scripts/evaluate.py --set client --llm`). The remaining seven targets — `price`,
`quantity`, `delivery_deadline`, `warranty_period`, `validity_period`, `contract_number` and
`party_address` — still have no client gold labels, and `eval/gold.py` treats an absent type
as not scored rather than as a miss, so silence there does not mean success.

Not re-measured since the 2026-09-23 hardening: the LLM path. Three changes touch it — the
corrected token estimate (with chunk size and selection budget raised to match; workbook
selection is now within 11-17% of the characters it selected before, and closer to honest in
real tokens), the rule that drops a model's number or date its own quote does not contain, and
the escaping of delimiter tags in untrusted text. The ten e2e tests against the real model pass;
an `--llm` eval run is the next measurement to take.

### Scoring rule, tightened 2026-09-23

The scorer no longer credits a match by substring for numbers, percentages or dates (it had
counted '5%' as matching '15%'), and free text now matches inside a longer value only when it
is at least half its length and ends on a word boundary. Re-scoring both gold sets with the
stricter rule changed no number: synthetic 100%/100%, client overall precision 84.38% and
recall 74.31%, identical per field. The published figures were therefore not inflated by the
substring fallback.

## Selector precision (unmeasured, now instrumented)

`selector.py` decides which chunks are worth a call using purely lexical scoring. Nobody has
measured how often it is right. A run now records `chunks_selected` and `chunks_with_facts` per
document, which is the number a semantic gate would have to beat.

A gate is *not* implemented, deliberately. It would run over the chunk × target cross product,
and by the table above every new chunk is a fresh ~27 s prefill — so at K chunks and 13 open
targets it costs roughly `K × (27 + 12×0.8)` seconds, against the ~75 s the pipeline currently
pays only on chunks the lexical selector already picked. It is a net loss on any document with
five or more chunks, and it loses hardest when the selector is doing its job. If the recorded
gap turns out to be large, the version to build batches all open targets into a single
schema-constrained call — one prefill, ~13 decoded tokens — rather than asking serially.

## Document comparison

### The measurement that shaped the design

Paragraph-level alignment of the client's acceptance acts, correct version against each
revision, before any of the comparison code was written:

| pair | similarity | hunks | opcodes |
|---|---|---|---|
| correct vs each of the five planted-mistake revisions | 0.81–0.98 | 1–7 | `replace` only |
| correct act vs its own contract draft | **0.08** | one block | `replace` |

Two conclusions, and both are in the code.

**A positional diff is the right tool for two versions and the wrong tool for two documents.**
`compare_min_similarity` (0.35) sits between the two regimes. Below it the report leads with the
field differences, says these are not two versions of one document, and records `CMP.LINE_DIFF`
as a rule that could not run — rather than drawing a hundred-hunk diff that means nothing.

**The client's corpus cannot measure most of the feature.** Every real pair is `replace` only:
not one added, removed or moved paragraph in the entire set. The added / removed / moved /
formatting / homoglyph / reworded paths are therefore covered by committed synthetic pairs in
`tests/fixtures/compare/`, one deviation each, generated by `scripts/gen_synthetic_samples.py`.
Without them those paths would ship with no coverage at all.

### Classify per entity, not per hunk

A first prototype judged whole changed paragraphs. It filed the client's Mistake #3 — a bank
code naming a different bank — as "text changed", because every digit in that paragraph was
untouched and the paragraph-level heuristics had nothing to fire on.

The shipped version runs `rules.run_rules` and `anchors.find_anchors` over **each side** of a
change and pairs the typed values by fact type, comparing them through the same
`extraction/normalize` primitives the extractor and the AOA checker use. That is what turns the
same paragraph into a critical banking difference, and what stops a paragraph holding two
amounts from reporting both when only one moved.

Two rules exist because nothing else could reach them:

* **`CMP.AMOUNT_WORDS`.** Georgian contracts restate an amount in words. A revision that edits
  only the words leaves every digit in place, so the amount rules see no difference whatsoever —
  and on the client's "Minor mistake" revision that is the *entire* planted error.
* **`CMP.AMOUNT` on a bare number.** `find_amounts` needs a currency beside the figure, and a
  table or spreadsheet keeps the currency in another cell. The one place a price is most likely
  to change is the one place the amount rules cannot see it.

### Both passes must fold their text the same way

`hits_of` applies `normalize_confusables` before extracting, exactly as `AoaChecker`'s
`DocumentView.build` does. It did not at first, and a revision that replaced every hyphen with
an en dash reported the contract number changing from `PR-2026/017` to `PR` — the number pattern
stopped matching at the dash, while the line itself was correctly called formatting-only. A
hunk-level check and a document-level check that disagree about what the document says will
manufacture differences out of typography.

### Characters nobody can see

`CMP.HOMOGLYPH` fires when two strings fold equal under `normalize_confusables` but are not
equal — a Cyrillic lookalike inside a bank code. It is a `warning` between two Word files and
**`info` when either side came from a PDF**, because `normalize.py` documents Tesseract as a
*source* of exactly these characters: on a scan this is OCR, not tampering. `CMP.INVISIBLE_CHARS`
covers zero-width and bidirectional controls separately.

**These rules are reported in addition to whatever else changed on the line, never instead of
it.** They were not, at first: `cosmetic_kind` only speaks when a change is *nothing but*
invisible, and `hits_of` folds confusables away before it compares values. Between them, a
Cyrillic letter swapped into a bank code **in the same paragraph as a plausible wording change**
was reported as neither a banking difference nor a substitution — only as "text changed". That
is precisely the case where a swap would be deliberate, so `hidden_character_rules` now checks
the raw text of both sides on its own, independently of the value comparison.

The same hazard applies to our own source. bandit's B613 caught the first version of the
detector carrying the very characters it looks for, so both it and its tests now build them from
codepoints. A test for invisible characters whose source contains invisible characters is a test
nobody can review.

### The model pass, and why it is behind a button

Scope: changes that are substantive and carry **no** typed value — a reworded clause with no
price, date, period or bank detail in either version. On the client's revisions that is a
handful; on four of the six it is nothing at all.

It is **one batched call**, not one per change. The prefix-reuse table above is why: reuse never
materialised at real sizes (median prefill 8669 ms over 111 calls), and these passages are far
shorter than the ~1193 tokens at which it was ever observed. Serial calls would cost one prefill
each.

It can only ever narrow the picture. It cannot introduce a change; an id it was not given is
dropped; both quotes are checked against the passage they claim to come from with the same gate
`llm_extractor.quote_in_chunk` applies to extraction; and `material: false` moves a change *down*
only. Given that this model measured as a precision regression on this corpus when allowed to
assert, withholding is the only thing it is asked to do.

It is off until someone presses the button. The deterministic report is milliseconds; one
batched call on CPU is tens of seconds. `PA_COMPARE_LLM_ENABLED=false` removes the button
entirely.

### Two limits worth knowing before quoting a result

**Rows of nothing but numbers align positionally.** A spreadsheet row is keyed by its text
cells, the same identity `BoqLine.key` uses; a row with no text falls back to its column
signature, so every all-numeric row of the same shape shares a key. `SequenceMatcher` tolerates
duplicates, but within a run of such rows the alignment is effectively positional — inserting
one numeric row mid-block shifts the rest. Inserting a row that carries *any* text does not.

**A very large replace block is reported wholesale.** Scoring every left segment against every
right segment is quadratic, so past `_PAIR_BUDGET` (2500 candidate pairs) a block becomes an
all-removed plus an all-added rather than a set of line edits. The act-vs-contract pair is
59 × 68 = 4012 and lands there — which is the other reason that report leads with the field
differences: they are the only layer still carrying an answer.

### The findings list is a list, not a dump

Two different bid workbooks differ in over a thousand cells. Listing every one buried the
twenty-one that mattered, so **critical findings are never truncated** and everything else is
capped at 60, with a `SkipNote` stating how many were left out and that they are all in the diff
below. A bare figure changing in a *spreadsheet* is substantive rather than critical for the
same reason: two bids disagreeing in hundreds of numeric cells is what a bid *is*, and the word
has to keep meaning something.

### What is not measured

The comparison is scored by rule id through the same `score_findings` the AOA check uses —
`eval/gold.py` gained a `check:` field and `tests/fixtures/gold/compare_*.yaml` holds six pairs,
including the one that matters most: an identical revision must produce nothing at all. What
that scores is *which rules fire*, not whether a reported difference is the one a buyer cared
about, and certainly not whether the model's explanations are any good — that needs labelled
judgements this corpus does not have. The client pairs are not in the committed set for the
usual reason: their expected values are the confidential data.

## Known gaps

* The scanned PDFs are not yet in the gold set; OCR only runs inside the Docker image, so they
  must be scored there.
* `AOA.FX_INVOICE` (NBG-rate invoice calculation) is specified but not implemented; the rates
  table format is `data/fx/nbg_rates.csv` with `date,currency,rate`.
* The registry rules run only against a snapshot the client exports (`cli load-registry`); with
  no snapshot they are reported as skipped, never as passed.
