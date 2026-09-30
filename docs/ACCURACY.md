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

Not yet scored: the scanned PDFs (OCR runs only inside the Docker image); `AOA.FX_INVOICE`,
because neither client set has an act in a foreign currency, so no gold file expects it; and the
contract-term, payment-term, party and scope rules (`AOA.ACT_DATE_IN_TERM`,
`AOA.PAYMENT_DEADLINE`, `AOA.PAYMENT_METHOD`, `AOA.PAYMENT_ADVANCE`, `AOA.ID_CODE_PARTY`,
`AOA.PARTY_ROLE`, `AOA.PARTY_MISSING`, `AOA.SUBJECT`, `AOA.SERVICES`), which fire on none of the
client's acts — their planted mistakes exist only as synthetic unit tests. Nor is the model
review of the subject and services: the figures above are rules-only, and so is every run of the
evaluation (see "The act review" below).

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

### Prose spelling: the third attempt, measured, on by default

The client asks for `მართლწერა` checking. Two general approaches were built and rejected first:

1. **Compare the act's vocabulary with the contract's.** Georgian is heavily inflected, so
   `დირექტორი`/`დირექტორის` and `შემკვეთის`/`შემკვეთს` are one edit apart while both are
   correct. On the *correct* act this produced 17 findings; stemming left about three.
2. **The hunspell `ka_GE` word list without its affix rules** rejects 39% of the words of a
   correct document.

The one that ships (`checks/aoa_spelling.py`, rule id `AOA.SPELLING`) reads that dictionary
through `spylls`, a full hunspell affix engine in pure Python, and reports a word only when every
gate below holds. The dictionary lacks many regular forms of words it knows (`შემსყიდველი` but
not `შემსყიდველს`, `გაწეული` but not `გაწეულია`) and many verb forms, so most gates say what is
*not* a typo:

| gate | why |
|---|---|
| Georgian letters only, four or more | three letters sit one edit from too many words; a Latin or Cyrillic letter is the stray-letter rule's |
| not a name, a date, a figure's words, a legal-form abbreviation (`სსიპ`), nor a word another rule reported | quotes (each pair matched where it stands), three words after a legal form, a name after a title, any word the anchors read as a party, director, address or project, a surname by its ending |
| not a word the contract writes | the documents' own vocabulary |
| unknown to spylls in every form | the dictionary itself |
| not a regular form of a noun the dictionary knows | the case model's exact inflection of a known nominative (a dative, an ergative, a genitive, a plural's singular); never a vowel the noun keeps dropped (`ძლის` is no form of `ძალი`), a consonant-final "lemma" (`სევრისის`), a verb-like lemma (preverb-initial, no noun ending, and no genitive or ergative the dictionary knows: `შესრულდად` is a slip, `შეცდომითაა` is `შეცდომა`'s), nor a genitive or plural that keeps a vowel the dictionary shows the noun drops (`შემსრულებელის`, `მხარები`) |
| not a full form | the short copula (`გაწეულია`, `გარეთაა`, `მომსახურებაა`), a full case ending (`ფეისბუქისა და`, `ფაილსა`), a participle's perfect (`არ შემოწმებულა`), `-ისას` (`შენიშვნისას`), an adjective whose attributive the dictionary lists (`ანტივირუსული`), the attributive of a known word, a vowel stem plus a whole postposition |
| no neighbour is the same word in another form | the other participle vowel (`შედგენულია`), a preverb-initial verb's other person (`გადაამოწმოს`/`გადაამოწმოთ`, a final plural `-თ`), the one version vowel after its preverb (`გამოიწეროს`/`გამოწეროს`); a word *without* the vowel (`შესრულა`) or with a second one (`შეეასრულა`) is a slip |
| a known word one edit away, keeping the first letter | a letter dropped, added or two swapped; a letter put for another only from a neighbouring key, its own key (shift) or the same sound series (`ბ/ფ/პ`, `დ/თ/ტ`, ...), unless the documents write the word it replaced: `კონტენტის` is a loanword, not a slip for `კონვენტის` |
| one best correction | the documents' own word first, then the one keeping the word's ending, then the one a hand explains (`აქტჯს` -> `აქტის`: `ჯ` is `ი`'s neighbour); a tie reports nothing |

**Severity follows the evidence.** A correction the act or the contract writes, in any form of its
word, is a warning; one only the dictionary proposes is `info` with `needs_review`: "may be
misspelled - check it". Every finding reaches the annotated Word file either way.

**Correct documents.** Every flag on every correct document here, the act checked against its
contract as the product runs it:

| document | flags |
|---|---|
| client correct act | `იდენტიურ` -> `იდენტურ`, `შემსრულებელის` -> `შემსრულებლის` (warnings: the contract writes both); `ეთმანეთის` -> `ერთმანეთის`, `მოიხსენებიან` -> `მოიხსენიებიან` (notes: no form of either word is written). All four are genuine; the adjudicated gold lists them |
| Test Set 2 correct act | the same four: its template carries them |
| Test Set 3 correct acts and contracts (three families) | none |
| the client's contract read on its own (no vocabulary gate) | nine words, all misspelt: `მოიხსენიერბიან`, `პრობების`, `განახორდციელებს`, `შესსრულებულ`, `სააგნარიშო`, `აუნაზღაროს`, `უპირობოთ` (twice; suggested `უპირობო` where `უპირობოდ` is meant, which the dictionary lacks), `დაუყოვნებლოვ`, `ცამხრივად` |
| the client's two instruction documents (free prose) | nine words, all misspelt; one suggestion wrong (`ინვისის` -> `ივნისის`, where `ინვოისის` is meant, a word the dictionary lacks) |

**Held out.** `samples/heldout_prose/`: 7 contracts and 7 acts in seven other trades (7,558
words, written without knowledge of the rule, no typos in them), run once with the rules frozen
(`eval_runs/heldout-prose-first-49f5827.txt`): 3 false flags on the 7 acts checked against their
contracts (two plurals and an adjective), 12 on the texts read alone (the three, verb forms and
two nouns). The plural and adjective gates and the severity by evidence came after that run:
the acts now come back clean, and the texts alone keep 8 flags - verb forms
(`გარეცხავს`, `აღჭურვოს`, `გამოიწერს`, `მომზადდება`, `მოაწერენ`) and two nouns - which are
notes unless the documents write the suggested word. Those numbers are tuned; an honest one
needs another unseen corpus.

**Recall on planted typos** (`scripts/mutate_aoa.py`, 40 per class, never the first letter):

| class | seed | detected | correction right | false positive | compare right |
|---|---|---|---|---|---|
| `prose_typo`: any letter dropped, inserted, swapped or replaced at random | 21 | 90% | 88% | 0% | 100% |
| | 5 | 90% | 85% | 0% | 100% |
| `prose_slip`: the slips of a hand (neighbouring key, shift, doubled or neighbouring extra letter, swap, drop) and of the ear (a letter of the same sound series) | 21 | 100% | 100% | 0% | 100% |
| | 5 | 95% | 95% | 0% | 100% |

The misses are ties (`სახი`: `სახით` or `სახის`), a random letter no hand types (`გვერდი` ->
`გვერდზ`, unreachable once `ზ` for `ი` is no slip) and originals the dictionary lacks
(`დარიცხვის`). The spelling agent's first version found 92.5% of the random class with 2.5%
false positives: the gates above traded a little of that recall for the held-out precision.

**A typo inside a listed service or the subject** is AOA.SPELLING's alone: the scope rules read
the part again with the reported words corrected before they call it another service.

**Speed**, this box (18-core CPU): loading the dictionary and building its indexes takes 1.1 s
once per process; the rule reads a whole act in 0.65 s from cold, then under 0.01 s for
another act of the same template; a contract read with no vocabulary, 1.6 s. spylls alone
spends 5-50 ms deciding that a string is no word, seconds per unknown word over all its edits,
so two shortcuts carry it, both held to plain spylls by a test: a string-level check that some
split of the string leaves a dictionary stem, and an index by flag of spylls' second-suffix
scan. At most 40 distinct words a document go to the correction search (acts need 8, contracts
20); past that a skip note counts the rest.

**The dictionary.** [`gamag/ka_GE.spell`](https://github.com/gamag/ka_GE.spell) at commit
`26d33614360c3784c199e3a6766eb5b095a134b8`, `dictionaries/ka_GE.aff` (sha256
`80d04afc4319209882a6e9be3bcb8d41543e79d94a8057c1e08b07d5e805352d`) and `ka_GE.dic` (143,020
entries, sha256 `5963f268e996f1e27e17163873b4d32f41fae2a1e5bef0117126caefe62f5857`). MIT
(the text is in the `.aff` header; the repository's GPL-3.0 covers only its generator scripts).
Generated from word lists by Dato Bumbeishvili (GeoWordsDatabase, MIT) and Kevin Scannell
(Crúbadán, [CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/)), the attribution that
licence asks for. `scripts/fetch_spelling_dictionary.py` fetches it into `models/hunspell/`;
the Docker build pins the same URLs and hashes (`ADD --checksum`), and a unit test fails when
the two differ. Upstream calls the list "automatically created by crawling the internet": it
lacks words (`დარიცხვა`) and may hold misspellings, which it would then accept.

**On a scanned act** a misread letter looks exactly like a typo, so the rule's warnings there
carry the scan's "possibly misread - check the paper original" and need review, the form
GOLD_SPEC section 6 allows on OCR input. A scanned contract changes nothing about the act's.

**Not measured.** Recall on real typos beyond the client's: every genuine typo seen is in the
client's two templates (four in the act, nine to eleven in the contract) and its instruction
documents. Nor how many words a scan's misreadings add: OCR runs only inside the Docker image.

### IBAN checksums are information, never errors

`normalize.iban_is_valid` implements the ISO 13616 mod-97 check and validates real IBANs. The
client's sample documents carry anonymised account numbers whose check digits do not validate,
so a failing checksum is reported as `info`. Its real use is to tell a likely OCR error from a
genuinely different account once the act and the contract disagree.

Bank codes are different: a BIC is 8 or 11 characters and the client's documents carry real
ones, so a malformed code is an error. That rule is what surfaced `AAAGE22` in Mistake #3.

### Where an IBAN ends is its country's length, not its checksum

An IBAN printed in groups of four ("GE00 NB00 ...") used to be accepted only at a length whose
checksum validated, so that the grouped form would not run on into the next capitalised word.
That made a mistyped grouped account, and every one of the client's anonymised accounts written
in groups, "no account" instead of "wrong account". The anchors now take groups, and the rest of
an IBAN a line break cut off, up to the length ISO 13616 fixes for its country
(`normalize.iban_length`, GE: 22) and stop there; a clause number after a short IBAN is never a
piece of it. The checksum still decides only for a grouped IBAN of a country the table lacks.
The act's findings quote the account as the act prints it, groups and all, so the Word comment
lands on it.

### Every quote style is a quote

The readers knew `„“`, `“”`, `«»` and the straight quotes, but not the low single quote `‚`
(which GOLD_SPEC section 1 already folds) nor two commas typed for `„` (",,შემსრულებელი“"), so an
act in either style had no parties and a false project-name error. `normalize.QUOTE_CHARS` now
holds `‚` and `‛`, and `normalize_confusables` turns two commas directly before a letter or digit
into `„` - two commas before a space stay a typo - so every reader sees one form of quote. The
act's text as written keeps the commas, and a finding's offsets are mapped back to it
(`fold_comma_quotes_mapped`), so its quote is the document's own words and the Word comment finds
them: the client's template writes `შემდგომში - ,,შემკვეთი”`, and a swapped role there is quoted
with its commas.

### Signatories: a name where its place says so

The readers after "დირექტორი" and "მინდობილობით" took any two words, so body clauses ("... დირექტორი
ან მის მიერ უფლებამოსილი პირი", "... დირექტორი პასუხს აგებს") invented signatories. A candidate is
now kept when its place says it is a name - after a colon, before "სახით", alone on its line or
cell - or when its last word ends as a Georgian surname does, or when the document names the
person again; function words never make a name. A courtesy title before the name is skipped; an
initial ("გ. ნიმუშაძე") is read and compared on the surname, and the correction keeps the
initial. A signatory listed after a party's bank lines belongs to that party, and a bank that is
itself a party (its side defined, written above it, or its name carrying an ID code elsewhere)
keeps its signatory. A two-column signature table that Docling serialises row by row
("<left cell>, <column header> = <right cell>") gives each column's director to that column's
party.

### Parties and requisites: one reader per fact, a table read by its columns

The red team's second round (AOA-R2-01 to -04 and -06, EXTRACTION-R2-11; tests in
`tests/unit/test_redteam_r2_parties.py`) found the parties' readers disagreeing, each fix below
one rule in one place:

* **One label pattern for an identification code.** The party reader knew `ს/კ` and
  `საიდენტიფიკაციო კოდი`; the rules and the act check also knew `ს.კ.` and `ID`. A slip behind
  `ს.კ.` in the act was missed, and the same label in the contract put an error on a correct
  act, corrected to the customer's code. `normalize.ID_CODE_RE` is now the only pattern, read
  by all three, and knows `ს/ნ`, `საიდ. კოდი`, `საიდენტიფიკაციო კოდით`, `ს/კ N`. An English
  `ID` needs a word boundary (`PAID 204…` is no label; the act check's own copy read it). The
  act check takes every code the contract writes behind a label, and the act's too when its
  party reader found fewer parties with codes than the contract has. A code for a party whose
  code the contract does not state is a skip note, never corrected to the other party's. An
  act not in Georgian stays a skip note even where an English label gives a code (round 1's
  decision).
* **An account ends at its country's length, a currency code after it included.** Georgian
  banks print the sub-account's currency after the IBAN (`GE…17GEL`); it read as a 25-character
  account, an error on a correct act either way. The anchors and the rules now stop at 22 and
  leave a known ISO 4217 code out; the finding still quotes the account as printed. Two
  documents naming different sub-accounts of one account is an `AOA.BANK` warning: before, it
  was an error only by accident.
* **A company with its legal form after its name** (`„ნიმუში“ შპს`) is a party. A name without
  quotes is still not read (nothing marks where it ends). Its neighbour's window then holds its
  requisites and its signatory: the signatory, right or with a slip, nearer to that unread
  party's than to the neighbour's, is a skip note, never an error corrected to the neighbour's
  director.
* **A head under another title.** A public body's chairman, a university's rector, a head, a
  president, a managing partner: `anchors._DIRECTOR` reads them as it reads a director.
  `ხელმძღვანელი` and `მმართველი` are also adjectives, so a name after them is taken only where
  its place says so, and a vowel stem's genitive (`ორგანოს`) is no first name. A deputy or an
  acting head is read apart (`Party.deputies`) and compared only with a deputy the contract
  names; one the contract does not name is a skip note, since signing in the head's place can
  be right.
* **A table with a party per column is written column by column.** Docling writes a table row
  by row, `<left cell>, <column header> = <right cell>`, so in a requisites grid every requisite
  of the customer's stood right after the contractor's name: the customer's code was read as
  the contractor's (`AOA.ID_CODE_PARTY` on a correct act) and a slip in the grid was missed.
  Round 1 gave each column's director to its party; the chunker now fixes the text at its
  source (`chunker.PartyColumnsTableSerializer`): a table whose one row holds two sides or two
  party names in different columns is written column by column, each cell ended with `;` (a
  line break alone is lost when the act check flattens the text, and the readers end an
  address and a name at `;`), with a row-label column written as each cell's label. A role and
  a name in one row are one party, so a table with a party per row keeps the triplets. An
  address also ends at a triplet's cell boundary, for a table still written row by row.

**Not measured.** A Word comment's places are counted in the file's reading order, row by row;
the text now reads a party grid column by column. A name written in both columns on different
rows is counted in another order, so a finding that marks only some of its places (a director
copied from the other party, AOA-09) could mark the other one. No client act has shown it; the
strict evaluation's occurrence counts are unchanged.

### A date's place says whose it is, its form says what it is

The red team's second round found a family of date failures with one root each, all fixed by
rule (`tests/unit/test_dates_and_periods.py`):

* **The header is a place in the document, not the first chunk.** The extraction promoted every
  unlabelled date of chunk 0 to `contract_date`; a contract short enough to be one chunk had its
  performance window's dates reported as extra, conflicting contract dates. The header now ends
  at the first party or the first numbered clause (`anchors.preamble_end`), the same boundary
  the signing-date reader uses; when neither is found in a one-chunk document nothing is
  promoted. A party is also a person or an individual entrepreneur named without quotes, so a
  contract between them gets its signing date and its validity "from signing".
* **An act's service period is known by its form.** The period was read only after an
  enumerated verb (`შესრულდა`, ...); `შესრულებულია`, `გაწეულ იქნა`, `განხორციელდა` lost it or
  split it into two single dates. A range in a sentence that dates neither the act nor an
  advance is now the period whatever the verb, and a month stated as the period ("ივნისის
  თვეში") is that whole month - a decision, since the spec was silent on it.
* **A missing first day is not the signing day.** A performance window whose first day was
  written but not read ("1-ლი ივნისიდან" before ordinals were read) started at the signing date:
  a confident wrong value. It now starts open; only a window with no first day written at all
  runs from the signing.
* **Corrections keep the act's case and day style.** The vowel-stem month's dative and genitive
  are one word (`აგვისტოს`); the word after the act's date decides it (a postposition or a noun
  makes it genitive, otherwise dative), so the correction is `30 სექტემბერს`, not
  `30 სექტემბრის`.

**Not measured.** How often a letterhead above an act's title is longer than fifteen lines, or
how often a document states its own date after the parties in a way the signing-words reader
does not know: both return no value rather than a guessed one.

Red team round 2 (2026-09-30) found six more, each fixed by rule with a unit test and a near
miss that must not match (`tests/unit/test_redteam_r2_dates.py`):

* **A day range before its month and year** (`1-15 ივნისი, 2026 წ.`, `01-15.06.2026`) was read
  as its last day alone: a correct month-end act got `AOA.ACT_DATE`, and a year slip was quoted
  without its first day. `rules.find_dates` reads the leading day as the range's first day, in
  the same month and year, only where nothing makes the number something else (a digit, stop,
  slash, dash or letter before it, `N`, a first day not before the last).
* **The year word is a whole word.** The `წ` of `წელს` was taken for the abbreviation `წ.`, so
  the act's date ended mid-word and its correction lost the rest. A two-digit year's date now
  includes its year word, and `georgian.render_date_like` has a style for every form the date
  reader reads (dd.mm.yy, English day-month-year and month-day-year, with ordinals); a unit test
  round-trips each form through its own style.
* **A numeric date's end is written with its case**: `31.12.2026-ის ჩათვლით`, `31.12.2026 წლის
  ჩათვლით`. Unread, the validity's last day became its first.
* **The future passive sets the performance window** (`შესრულდება`, `განხორციელდება`,
  `ჩატარდება`, `დაიწყება … დასრულდება`) - a decision to read these only in a sentence that names
  the work (`მომსახურება`, `სამუშაო`) and is not the validity clause, since "მოქმედებს მანამ,
  სანამ მომსახურება შესრულდება" and "მიწოდება განხორციელდება …" use the same verbs.
* **Parties named in the title** (`… შპს „…“-სა და შპს „…“-ს შორის`) ended the preamble above
  the place-and-date line, so the contract lost its date and its validity "from signing". A
  party joined to the contract word by a `-სა და … -ს შორის` phrase on the title line is part of
  the title; a party block that defines roles in brackets still ends the preamble.
* **A sentence does not end inside a short bracket or at a chained abbreviation** (`(იხ.
  დანართი N1)`, `მ.შ.`, `ე.წ.`), which cut a subject clause at `(იხ`. `ა.შ.` (and so on) still
  ends one, and a bracket left open or broken over a line holds nothing together.

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

A comment goes where the finding's words stand **as whole words** (red team R1, AOA-10): the
slip `ივნის` was marked inside the correct `ივნისიდან` and `ივნისის` too, and a director's
nominative inside the preamble's genitive, with the nominative's correction. Words found only
inside longer ones (a stem the act writes with an ending) are still marked there. A name copied
from the other party (AOA-09) is the one case where not every place of the words is wrong: the
customer's director written for the contractor is right in the customer's own block. The act
check attributes each place to the party named last before it (its name, code or role label)
and, when some places are plainly the other party's correct director, the finding carries which
of the places are its own (`Finding.places`: ordinals among the whole-word places, and their
count). The API annotates the Word file without the checked text, so the ordinals are counted in
reading order in the file, tables in place; when the file shows another number of places than
the text did (a merged cell read twice), every place is marked rather than a guessed one.

### An act not in Georgian: the rules say they cannot read it

The rules read Georgian labels and wording (`ს/კ`, `შესრულდა`, `პროექტ`). On an act written in
English (red team R1, AOA-15) they found no identification codes and reported an **error** on a
correct act, and warnings that the project and the period were missing. Reading English parties,
periods and dates is out of scope for now; instead, when fewer than a fifth of the act's letters
are Georgian, `AOA.ID_CODES`, `AOA.PROJECT_PRESENT` and `AOA.PERIOD` leave a skip note saying the
act is not in Georgian. The client's acts are over 80% Georgian letters (the Latin is bank codes
and IBANs), and an act in two languages, each clause in both, about half: its Georgian half is
read as before, and a project missing from it is still a warning. Not measured: no client act is in English, so a late or out-of-term
English act is still missed (the act date and the period are not read), and says so only
through those notes.

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
differences: they are the only layer still carrying an answer. Lines that differ only in their
clause number ("5.3." → "6.3.", the same text after it, once on each side) are paired first,
before the budget applies: an article inserted near the top of a 70-clause contract renumbered
every clause after it, made one block too big to pair, and the two versions scored 0.12 —
"unrelated", nothing reported. They now align at 0.97. Each renumbered clause is still its own
"text changed" warning; whether a clause number that moved with its clause is a change at all
is an open policy question (red team COMPARE-27).

### Red team, round 1: what the comparison now reads (2026-09-30)

Defects a red team found and a verifier reproduced, each with a unit test on invented
text (`tests/unit/test_compare_redteam.py`, `test_compare_tables.py`):

* **Values in canonical form.** A payment term, warranty, delivery deadline and quantity are
  reported as `docs/GOLD_SPEC.md` writes them (`P7D working`, `P12M`, `P30D`, `4 item`); the
  words as written stay in the finding's note. The days a term counts are part of it, so
  working → calendar days is a change. A duration in a sentence that pays ("საფასური
  გადაიხდება ... 7 სამუშაო დღის ვადაში") is a payment term, not an unclassified duration.
* **VAT is decided deterministically.** `tax_phrases`, the reader the act check uses, types a
  line's VAT status, and the document-wide pass compares it, so a contract including VAT
  against an act excluding taxes is `CMP.VAT`.
* **Quantities in prose** (a number, optionally its words, and a unit from a closed list:
  piece, set, m², m³, kg, t, l) are `CMP.QUANTITY`. A unit is the unit noun in one of its case
  forms: "ტონერი" (toner) or "ცალმხრივი" (unilateral) are not.
* **Word tables.** A cell's column header, and the paragraph after a line, say what a figure's
  words count ("800 (რვაასი)" under "ღირებულება (ლარი)" is lari). A row is aligned as a row,
  keyed by its words without its figures or its "№", so a row inserted is one change and the
  renumbered rows after it are formatting, not critical amount changes.
* **One change, one finding.** A validity end read on its line is the period the document states;
  words that retell a figure's reported change are that change; a moved list item is a move; a
  "text changed" line is dropped only when another reported change states it whole (an annex
  number "2" is no part of a date that moved from the 12th of February). A line quoted inside
  another is its own change unless it is one of the lines of a table cell reported whole.
* **Dates.** A period keeps its ends as written, so a year typed wrong in one end is one change
  to a period neither document states sorted; a period whose end has no year of its own takes
  the other end's (`rules.find_dates`).
* **Document kind** is read from folded text, so an act titled with an en dash is an act.

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

## The act review: the model reads the notes the rules leave open

`AOA.SUBJECT` and `AOA.SERVICES` compare words (stage 1, `checks/aoa_scope.py`). What they
cannot match - the act's subject in other words, a service the contract's list may or may not
hold - is an `info` note flagged `needs_review`. A user can then ask the local model to read those
notes (stage 2, `checks/aoa_llm.py`), and plain code combines the two answers (stage 3,
`decide`).

**What is asked.** Only notes whose words the contract does not clearly hold. A number that
replaces the contract's is the rules' evidence and final; a number the act only adds, in the
contract's own words, is a question about a number, which the rules already compared. Neither is
sent. It is one batched call to `PA_LLM_FAST_MODEL`, behind a button (`PA_AOA_LLM_ENABLED`); it
does not run by itself unless `PA_AOA_LLM_AUTO` is set, because the worker is a single thread.

**What an answer can do.** "Likely the same" marks the note and leaves it `info`; "likely
different" raises it to a warning. The model never clears a note and never makes an error: this
model measured as a precision regression on this corpus whenever it was allowed to assert, and
the act is the contractor's own text, so an instruction hidden in it can move a note one step at
most. An answer counts only if its id is one the model was given, both quotes are in the text it
was shown for that item (at least eight characters, or a whole line), both sentences are there
and are its own, and its confidence is from one half to 1. "Same" also does not count where the
act states a number its closest contract text does not: that may be the same work with another
count, and only the rules compare numbers. A stored review is tied to both documents
and to a digest of the notes, the prompt's version and the model; one that no longer matches is
set aside, and a review of the same notes is reused rather than asked again - a failed one too.

**Measured, 2026-09-25** - gemma3:4b (Q4_K_M) on this CPU box, synthetic items only. One
observation, not a score:

| items asked | wall clock | prompt tokens | completion tokens |
|---|---|---|---|
| 1 service, through the app (upload, queue, worker, report) | 19.8 s | 727 | 125 |
| the same prompt again, the server holding it in its cache | 10.1 s | 727 | 123 |
| 1 subject | 21.2 - 21.4 s | 787 - 794 | 210 |
| 2 services | 29.9 - 32.2 s | 820 | 237 - 302 |
| 3 services | 38.0 s | 904 | 379 |

All with the prompt as shipped; the context window was 4096 tokens each time. Output, not the
prompt, is what grows with the number of notes, which is why one review asks at most
`PA_AOA_LLM_MAX_ITEMS` (8).

* **Asked for the verdict first, the model once committed to one and then explained the
  opposite:** a service the contract never lists came back "same", with a reason describing other
  work. The schema now has it quote, then explain, then decide - its raw output follows the
  schema's order - and in the runs after that change the verdicts agreed with their reasons.
* Per item, with the prompt as shipped:
  * the contact form in other words ("უკუკავშირის ფორმის შექმნა" against "საკონტაქტო ფორმის
    აწყობა", creating against assembling): "same" in four runs - one of them refused by the quote
    check - and "different" in one;
  * a service the contract never lists (a logo and a brand book): "different" in two runs - one
    refused by the quote check; in a third it copied the previous item's reason and verdict
    ("same"), which is now refused;
  * a service written in Latin letters, construction work in place of web design, and a subject
    in other words: as expected, one run each.
* **In a batch of two, the model once gave the second item the first item's reason word for word,
  and a verdict to match.** Such a copy is now refused. In another run it corrupted a Georgian
  quote with characters of another script; the quote check refused both answers, and the rules'
  notes stood.
* **The same prompt did not give the same answer twice.** Temperature 0 and a fixed seed do not
  make CPU inference repeatable here once the server's prompt cache holds a shared prefix.

**Rules-only against rules plus review.** The evaluation and the findings harness run the rules
alone. On the client's six acts and the twelve of Test Set 2 the rules leave no subject or service
open, so the review would never be asked, and the two columns are identical by construction. A
real number needs labelled paraphrases and substitutions, which this corpus does not have.

## BOQ comparison

Measured 2026-09-30, after the second red-team round on the BOQ reader:

| harness | result |
|---|---|
| `scripts/evaluate_boq.py`, client set | 102/102 checks |
| `scripts/evaluate_boq.py`, test set 2 | 102/102 checks |
| `scripts/evaluate_boq.py`, test set 3 | 127/127 checks |
| `scripts/mutate_boq.py --seed 3 --per-class 3` | 51 mutants, 17 classes, 100% pass, 0 false positives, 0 control false positives |

The red team's round-2 probes were each workbooks laid out otherwise than the client's
template: a group header over rate and amount sub-columns, a quantity split per building, a
closing row not called SUB-TOTAL, a Summary lettered A, B, C or worded the Georgian way, a later
row that also says "TOTAL COST" or "incl. VAT", units in Cyrillic or Georgian, chart sheets, a
column inserted into the item-number block. The rules now in GOLD_SPEC 5 cover each; every probe
is a synthetic unit test in `tests/unit/test_boq_reader_variants.py`. On the three gold sets
the findings did not change, only the wording of the closing-row reconciliation. These are
fitting numbers in the same sense as above: the gold sets are this client's.

Policy questions left open:

* Profit on cost + overheads, usual in Georgian estimates, is a `BOQ.RECONCILE` error on every
  faithful bid that computes it so, because `reconcile_summary` holds overheads and profit
  against TOTAL COST x their rate. That base is the module's own documented decision, not
  GOLD_SPEC 5's (which asks only that the rows follow from each other), and the gold writer
  checks no rates at all. The markup and the ranking are right; the error is not. Accepting
  either base (TOTAL COST, or TOTAL COST + overheads) and noting which was used is the
  recommended change.

The rest follow the letter of GOLD_SPEC 5, and the gold writer agrees:

* A provisional sum the client fixes (amount typed, no rate) is unpriced for every bidder and
  makes every bid incomplete; the ranking is unaffected.
* A lump sum typed on a group row over its formula is booked as "Summary vs sheets", not as a
  priced non-position; renumbered positions after an inserted line are removed + added; an amount
  typed with every rate cell empty is unpriced.

## Known gaps

* The scanned PDFs are not yet in the gold set; OCR only runs inside the Docker image, so they
  must be scored there.
* `AOA.FX_INVOICE` (the invoice in lari for a foreign-currency act) works out the formula — the
  advance at the NBG rate of its payment day, the rest at the rate of the act's date — but no rate
  source is connected (`PA_FX_RATE_SOURCE=none`), so the note names the days whose rates to look
  up and an `AOA.FX_RATE` skip says the total in lari was not calculated. A connector implements
  `fx/provider.RateProvider`; an offline table the operator loads (`date,currency,rate`) needs no
  egress. Open with the client: their no-advance example writes 2,610.04 for 1,000 x 2.6104, which
  is 2,610.40 (the tests hold the arithmetic); whether the act states the full value or only the
  remainder (the check assumes the full value); and whether each term or only the sum is rounded
  (the check rounds each term).
* `AOA.SUBJECT` and `AOA.SERVICES` compare words, with rules only: stems, and the share of the
  act's words the contract's clause or item holds. A match needs all of them: the items of one
  list share most of their words ("… სერვისის გვერდის აღწერა GEO/ENG"), and the one that differs
  names the service. Only a number that replaces the contract's, in an item three words in four
  the same, is an error; a number only the act adds (the period, a share done) is not, and a
  month and year are not read as numbers. Wording the rules cannot match is an `info` note
  flagged `needs_review`, which the evaluation does not score — it scores errors only — so how
  often a paraphrase is flagged is not measured, nor how often the model review, when asked,
  reads such a note rightly (see "The act review"). The shares and the stemmer are fitted to the
  client's one template. A list of services is read from list lines (`- `, `•`, `ა)`) under a
  lead-in; a spreadsheet, or a table holding the list, has none, and the rule says it did not run.
* The registry rules run only against a snapshot the client exports (`cli load-registry`); with
  no snapshot they are reported as skipped, never as passed.
* Docling promotes the first row of a table with no header row to the column header, and its
  serializer drops that row's label. For a two-column table whose first row's value holds a
  figure (a key-terms table: `ხელშეკრულების ღირებულება | 48 000 ლარი`), the chunker writes the
  row back as `label: value` before the table. A table of three or more columns with no header
  row still loses its first row's first cell; the segmenter (document comparison) is unchanged.
* A prose typo inside a service line or the subject clause also leaves a scope review note
  (see "Prose spelling"); the scope rules should compare the corrected word.
