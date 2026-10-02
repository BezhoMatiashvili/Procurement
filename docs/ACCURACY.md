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

Measured 2026-10-01 on branch `accuracy-hardening` (rules and anchors only; the LLM is off by
default and plays no part in any number below). Commands are in "How accuracy is measured".

### Strict evaluation against the gold sets (`scripts/evaluate.py --strict`, docs/GOLD_SPEC.md)

Strict means canonical values compared exactly (names in the nominative, ISO dates and ranges,
numbers as numbers), every fact of a type the gold does not list counted as a false positive,
and every finding scored by rule, text, correction in the act's own form, severity and the
number of places its Word comment marks.

| set | documents | extraction (derived / labelled) | findings expected | missing | unexpected |
|---|---|---|---|---|---|
| client (`client_v2`) | 6 acts, the contract, 6 compare pairs | 99/99 and 37/37 | all | 0 | 0 |
| Test Set 2 (`ts2`) | 12 acts, the contract, compare pairs | 198/198 and 54/54 | all | 0 | 0 |
| Test Set 3 (`ts3`) | 3 families, 23 acts, 3 contracts, compare pairs | 373/373 and 107/107 | all | 0 | 0 |

These are **tuned** numbers: every set was looked at while fixing. The honest numbers are the
first runs on data nobody had looked at, kept in `eval_runs/` (git-ignored):

* Test Set 3, first run before any fix (`heldout-ts3-first-9f704c8.txt`): extraction strict
  precision 88.8%, recall 87.6%; findings 50 matched, 9 missing, 20 unexpected.
* Test Set 3 BOQ, first run (`heldout-ts3-boq-first-f65db38.txt`): 66 of 127 checks.
* Prose spelling on 14 unseen texts (`heldout-prose-first-49f5827.txt`): 3 false flags on 7
  acts checked against their contracts.

### BOQ: an independent reference reader (`scripts/evaluate_boq.py`)

Totals to the cent at the template's quantities, common scope, colours, every finding, and
every template position for every bidder in the merged workbook:

| set | checks |
|---|---|
| client (`AI Test`, 4 bidders, 13 sheets) | 106/106 |
| Test Set 2 | 106/106 |
| Test Set 3 (a second template: Georgian headers, a lettered Summary) | 132/132 |

### Mutation harnesses (fresh seeds; mutated Word files parsed by Docling, as an upload is)

`scripts/mutate_aoa.py` plants one change into a correct act (the client's and Test Set 2's)
and scores the act check and the comparison of the pair: 20 error classes (codes, IBAN, BIC,
plot, fee digits/words, act date, period year, director, party, address, project, month and
number-word typos, a stray Latin/Cyrillic letter, removed project/plot, swapped roles), 14
harmless classes (quote/dash/space styles, amount and date styles, run splits, table vs
paragraphs, unquoted names, tetri, an equivalent in USD, a relabelled price, ...) and two prose
typo classes. A case passes only with the finding, the correction in the act's own form, a Word
comment on every place, and nothing else reported.

Six small seeds (71-199, 12 cases a class) and then four large runs, 300, 300, 150 and 300 cases
a class (36,450 cases in all; about one in two is distinct, a repeat being scored once):

| run | error classes (15,000 planted mistakes over the three runs) | harmless classes | what it found, each fixed as a general rule |
|---|---|---|---|
| seed 211, 300 a class | 1 address slip missed, 1 party name corrected into the wrong case | 0 false findings in 4,200 | a genitive written with and without the vowel some nouns drop is two words; a slip tied between two cases takes the case the documents write |
| seed 223, 300 a class | 1 address missed, 1 month corrected into a form no date takes, 1 month read as another month, 2 stray letters in the signing verb hid the act's date | 0 in 4,200 | a phrase declines on its last word only; a month tie is decided by the whole word, or no month is read; a date's form wins a tie; the act's date is read past a stray letter |
| seed 229, 150 a class, after those fixes | detection 100%, 1 stray letter corrected by a guessed letter (1 in 3,000) | 0 in 2,100 | a stray letter dropped beats a letter guessed in its place |
| **seed 241, 300 a class, the final code** | **all 6,000: detected, corrected in the act's own form, every place marked, nothing else reported (100%)** | **0 in 4,200** | nothing new; the comparison missed 1 director slip in 300 (the known declension limit) |

Every failing case of the three runs passes on the final code. A targeted rerun of the classes
touched, 300 cases each: party name, address and month typos 100% on every measure with no
false finding on seeds 229 and 233; the stray letter 100% on seeds 229 and 233 (after two more
fixes it found: a dropped stray letter beats a guessed one, and a reading written only in
another form is settled by the dictionary) and 299 of 300 corrections on a fresh seed 239. The
one left is a true ambiguity: a Latin "i" in "ეგზემპლ-i-არად" is the "ი" key, and that reading,
"ეგზემპლიარად", is how the contract spells the word, so the correction follows the contract
where the harness, which only inserts letters, expected the letter dropped; the stray letter
itself is reported and marked. Over the 15,000 planted mistakes the code as each run found it
missed 2 and wrote 5 corrections in a wrong form; none of those remain.

What stays below 100%:

* **The comparison of a director slip that breaks a surname's declension** (99-99.3% of the
  director class): "ლომძიე" for "ლომიძე" is reported, but its value is read off its genitive
  ("ლომძიი"); see the limits.
* **Prose typos** (`prose_typo`, random one-letter edits: 87-91% detected; `prose_slip`, typing
  slips: 91-93%). The misses are words the dictionary lacks, a slip that makes another real
  word or a real form of one ("გვედრის", the genitive of a word the dictionary holds), and
  ties between two corrections; a prose slip gave an unrelated note 1-2 times in 300.

`scripts/mutate_boq.py` (17 classes: quantity, unit, added/removed/reordered lines, description
edits, hard-coded or wrong arithmetic, shifted Summary, renamed headers, text in numeric cells,
merged cells, hidden rows, missing cached values, reconciliation, filled blanks), seeds 11, 17,
23, 31, 41, 43 and 47 (the last 25 cases a class, 422 valid): every valid mutant 100% (a mutant whose deletion breaks a Summary formula into
`#REF!` is reported invalid, not scored).

### Rule 9: the invoice in lari, end to end (`scripts/fx_accuracy.py`, 2026-10-02)

Random acts written from scratch - 14 currencies the National Bank quotes (four of them per 10,
100 or 1,000 units), each written as its code, its sign or its Georgian or English name, before
or after the figure; four ways of grouping digits; three date styles; no advance, or an advance
in one of three phrasings with or without the remainder spelled out - run through the whole act
check against a real imported rates table, and the invoice compared with one computed
independently from the case's numbers.

* **First run (seed 1, 600 acts): 522 exact (87.0%).** Every miss was "no invoice worked out",
  for one general reason with two faces: the keyword search behind every amount's role cut a
  sentence at any dot, so the date in "ავანსის სახით 30.08.2026 ჩარიცხულია 2 000 USD" or the
  "წ." of "1 იანვარს, 2026 წ. ჩარიცხულია" took the advance's keyword away from its figure. A dot
  between two digits, and the stop of a one-letter abbreviation after a figure, now end no
  sentence (`rules._SENTENCE_BOUNDARY`). On the gold sets the only effect was one redundant
  untyped date fewer in Test Set 3's dollar acts; every finding is unchanged.
* **After that fix: seed 1 600/600; fresh seeds 2 and 3, 3,000 acts each: 6,000/6,000 exact to
  the tetri (100%).**
* The client's own example, from a file `scripts/fetch_nbg_rates.py` fetched from the NBG on
  2026-10-02 (USD 2.6104 on 10 September 2026, 2.6081 on the 17th) and imported with
  `load-fx-rates`: 28,691.40 GEL with the advance and 2,610.40 GEL without (the client's text
  says 2,610.04 for the second - a typo for 1,000 x 2.6104).

What the harness does not vary: the act's layout (one template), and acts in two foreign
currencies (a skip by design). Several layouts are measured with the synthetic families
(`scripts/gen_aoa_families.py`) and the held-out set.

### Rule 1: the parties against the business registry (`scripts/registry_accuracy.py`)

Random cases, written from scratch: two invented companies, each one's registry extract written
in the 2011 or the 2025 layout and read back with the application's own reader, and an act in an
act's style (form abbreviated, name quoted, address without country or district, the director in
the genitive and the nominative). Ten classes: clean, signature surname first, the second
authorised person signing (no finding allowed); a name slip, another legal form, another house
number, a street slip, the registry's flat left out (a warning), a director slip in every place,
a liquidation in the registry (a warning).

* First run (seed 1): a harness mistake - the act never wrote the flat the registry lists, so
  "clean" acts were short. Fixed in the harness, not the checker.
* **First honest runs (fresh seeds 2 and 3, 300 a class): 2,989 and 2,991 of 3,000 right
  (99.63%, 99.70%).** Three general causes: a vowel changed in a street's last syllable passed as
  a case form ("რუსთაველას" for "რუსთაველის"), so a place name now matches only its real
  alternations (X/X-ს, -ე/-ის, -ი/-ში); two directors one letter apart in the two parties ("ეკა
  მელაშვილი", "ეკა გელაშვილი") had one "corrected" into the other - a false error on a correct
  act - so a place that is another party's own director, written right, is never this party's
  slip, with or without a contract; and the same false error behind the few name misses.
* **After the fixes: fresh seeds 4 and 5, 3,000 cases each: 6,000 of 6,000 right (100%).**
* The reader on the two public extracts: code, name, legal form, full address (its wrapped
  line too), the director and position, sole representation, preparation date, no status - and
  neither the founders' nor the partners' persons taken for representatives.
* **Red team R6:** the page footer ("… http://public.reestri.gov.ge გვერდი: 1(2)") was read as a
  section heading, so the persons and the status on the next page were lost (R6-04): a footer
  and a page number alone on its line now end nothing. A street's namesake written with an
  initial ("ა. წერეთლის" for "აკაკი წერეთლის") and a house number marked "#" were a different
  address (R6-11): an initial now matches a word with its letter, another initial does not, and
  "#" marks a number as "№" and "N" do. The harness then planted one declined name as a slip
  ("სერვისს", the dative of "სერვისი", which the check rightly accepts) on seed 21; it no longer
  does. **Fresh seeds 21 and 23, 3,000 cases each: 6,000 of 6,000 (100%).**

### Rule 2: a signatory other than the director (out of scope, as the client says)

The client's rule 2 - check that a signatory who is not the director has the authority to sign
and that the power of attorney has not expired - is marked by the client "ამ ეტაპზე
არარელევანტურია" (not relevant at this stage). Nothing reads a power-of-attorney register, so
neither authority nor expiry is checked. What the act check does with such a signatory: an
attorney the act names (`მინდობილობით მოქმედი პირი`, `მინდობილი პირი` at the signature) is
compared with the one the contract names - a slip of that name is an `AOA.DIRECTOR` error with
its correction - and an attorney the contract does not name is an `AOA.DIRECTOR` warning that
asks for the power of attorney to be checked, with no correction offered.

### Rules 3 and 4: the project, the cadastral code, the act's date (`scripts/act_rules_accuracy.py`)

Acts written from scratch in three layouts (prose, "label: value" lines, numbered sections), three
date styles and five ways of saying the work was done (შესრულდა, დასრულდა, გაიწია, ჩაბარდა,
გაწეულია); fourteen classes: the project quoted, labelled without quotes, missing; the cadastral
code dotted, dashed, with a digit group wrong, missing; one completion date with the act on it or
off it; a whole month with the act on its end or off it; part of a month with the act on the
period's end, on the month's end, or off both.

* **First run (seed 1, 60 a class): 576 of 840 (68.6%).** Every miss was one general gap: the
  label "აქტის თარიღი:" (the act's date) did not date the act, so the check said the date could
  not be found. It now does, like "გაფორმების თარიღი:" (a bare "თარიღი:" still does not).
* **After the fix: seed 1 840/840; fresh seeds 2 and 3, 200 a class: 2,800 of 2,800 each
  (100%)**, with every correction read back as the expected date in the act's own style, and the
  gold sets unchanged.
* **Red team R6, what dates the work (R6-06):** a date anywhere within 160 characters after
  "შესრულდა" was read as the period's end, so a payment's deadline ("ანგარიშსწორება
  განხორციელდება 05.04.2026-მდე") or a signature's day made a right act wrong, with a
  "correction" to the month's end, and a bare date after the act's own hid a wrong act date. A
  verb of work done now dates the work in its own sentence only; a date after a payment, an
  advance, a signature or the act's own signing words is never the work's; a heading's or a
  label's dates still run onto the next lines. **Rules 3 and 4 also (R6-10):** "ს/კ" abbreviates
  both codes, and a company's code in groups ("ს/კ 405.987.612") was a malformed plot code; a
  year after a dashed plot code ("03-04-05-678 2026 წლის") was read into it. A party's code, or
  nine or eleven digits in three groups or fewer, is no plot code, and a part with four digits in
  a row is no part of one. **Fresh seed 21, 200 a class: 2,800 of 2,800**; the gold sets and the
  snapshot of every output unchanged.

### Rules 5-8: the tax wording by contractor type (`scripts/tax_rules_accuracy.py`)

Acts with an invented contractor of each type (natural person, VAT-paying or small-business
entrepreneur, foreign organisation, company), the type given or left to be read from the act, the
fee worded right, wrongly or not at all in Georgian and English phrasings, an advance line worded
too; eleven classes, among them the type read from the act and the cases where it must not be
(an entrepreneur with no status, or with both).

* First run (seed 1, 60 a class): 660 of 660. The phrasings were then widened (English "including
  VAT", "VAT included", "NET amount", "gross, taxes included"; "დამატებული ღირებულების
  გადასახადის ჩათვლით").
* **Fresh seeds 2 and 3, 200 a class: 2,200 of 2,200 each (100%).** On the gold sets the type is
  now read for 39 acts (a company for 24, whose wording rules are then not applicable) and no
  finding changed but the invoice note's "a small business needs none" sentence, now that the
  natural-person family's type is known.
* **Red team R6 (R6-05):** an entrepreneur's status was read anywhere in the act - a status
  denied ("დღგ-ს გადამხდელი არ არის", "მცირე მეწარმის სტატუსის გარეშე") or the customer's
  ("შემკვეთი … დღგ-ს გადამხდელი") typed the contractor. A status now counts only when said in the
  contractor's own block and denied nowhere there, a denial read in the status's clause ("მცირე
  მეწარმეა და არ არის დღგ-ს გადამხდელი" is a small business). **Fresh seed 21, 200 a class:
  2,200 of 2,200.**

### Rule 10: the act against its contract (`scripts/contract_rules_accuracy.py`)

A contract and its act written from scratch for each case - two invented companies with codes,
addresses and directors (the contract declining a director's first name with the surname, as many
contracts do), a project and its cadastral code, a subject and services with numbers in them, a
term, a fee in digits and words, an advance, a payment deadline and method, a bank code and an
IBAN - and one difference planted in the act for each of the client's bullets: either party's
code, name or director, a code given to the wrong party, the subject, the cadastral code, an act
after the contract's validity, a service, the fee, the payment deadline or method, the IBAN or the
bank code, a party's code or the account left out; and two harmless restyles. A case passes when
the act's errors and warnings are exactly the expected rule ids and each correction is the
contract's value in the act's own form (a director's name in the case the act writes it in).

* **First run (seed 1, 30 a class): 428 of 570** - every miss the harness's own: it built the
  months' oblique forms wrongly, and the check rightly flagged them as misspellings. Fixed: 570
  of 570. Every planted difference was found in that first run too.
* **Fresh seeds 2 and 3, 100 a class: 1,899 and 1,900 of 1,900.** The one miss was the harness's
  again: a slip of a name's last letter, "ჯგუფი" written "ჯგუფს", is the same name declined,
  which the check accepts; the harness no longer plants a declined form as a slip.
* **Gaps closed, general rules:** a party named without its code was silent, now a warning with
  the contract's code (not when the act states no code at all: that stays the one error); an act
  without its account named no account to write, now the contractor's when exactly one is the
  contractor's; an account without its bank code was silent, now a note with the code.
* **Corrections checked, a real defect found:** with each correction compared to the contract's
  value, every director slip failed: where the contract declined the first name ("ლევანის
  მელაშვილის") and the act wrote the name in the nominative ("ლევან მეჩაშვილი"), the
  correction kept the contract's genitive first name ("ლევანის მელაშვილი"). A correction now
  keeps the words the act writes as forms of the right ones and puts only a misspelled word
  right, in the case the act writes the name in; a word the documents never decline (the first
  word of a company's name, the fixed genitive of "ზღვის ბანკი", an invented bank) is given as the contract
  writes it - the first version of the fix changed one such word in the held-out set into a
  case form, which the strict gold caught. A name whose ending the act writes right is no longer
  read as meant in another case.
* **After the fixes, on the final code: fresh seeds 10 and 11, 100 a class: 2,100 of 2,100
  each (100%)**, the corrections included (seeds 4 to 9, each 2,100 of 2,100, ran on the
  versions in between); the gold sets unchanged and the snapshot of every output identical.
* **Services in a Word table (found after the harness, which writes plain text):** invented
  contracts and acts as Word files through Docling, the services as lines, as a table in the act,
  and as a table in both: with a table the rule said the act lists no services and compared
  none. It now reads a table's rows in the forms the chunker and Docling write them (one row to
  a line, `1: …;`; a header's columns, `1, მომსახურება = …`; an unbulleted list's `…;` lines),
  each row an item without its item number, a money column left to the fee rules and a quantity
  compared. A changed number in a table is now the same error as in a list; the gold sets and
  the snapshot of every output are unchanged.
* **A code under a misspelt label is not left out:** the spelling harness (rule 11) planted
  slips in the label "საიდენტიფიკაციო კოდი", and the code beside it, no longer read as the
  party's, was then reported as left out. A code is only left out when its digits are nowhere
  in the act; the misspelt label is AOA.SPELLING's.
* **Where a correction goes (red team R6, R6-03):** the customer's own name, address or account
  written for the contractor is right where the customer's block states it, and a tracked
  change there made a right act wrong. Each party's value is now corrected only at the places
  that are its own: a place written in another party's block, as that party's own value, is
  left alone - a name told by where the party reader read it, a place by the party named last
  before it, and a name two parties are written with by the code or role named last that only
  one of them has. A finding whose places cannot be told apart is a comment and changes
  nothing. The harness gained four classes (the contractor's address, and the customer's
  address, name and account written for the contractor) and checks every case end to end: the
  act as a Word file, annotated, every tracked change accepted, must leave each line it wrote
  right as it was, and each line it wrote wrong as it was or as it should be. **Fresh seeds 12
  and 13, 100 a class: 2,500 of 2,500 each**, no line written right ever changed. The check
  also showed where a reviewer can accept a correction in one click: every code, name,
  director, address, cadastral code, service, payment deadline, account and bank code, and now
  a code given to the wrong party and a number in the subject (the subject's number was marked
  on the whole clause, whose words a tracked change would have replaced by the number; it is
  now the number alone, at its own place). A fee, a payment method and a value left out stay
  comments: a fee in figures and words, or a sentence, is for a person to rewrite.
* **A services table's columns (red team R6, R6-09):** a money column headed "ანაზღაურება" or
  "Fee" was read as part of the service, and its cells were joined to the quantity's with a
  space - "2" and "500" made the number 2 500, a false "number differs". Those headings are money
  columns now, and a row's cells are joined with "; ", each figure a number of its own. **Fresh
  seed 21: 2,500 of 2,500.**
* **Not compared:** the bank's name (its code and the account name the bank); the subject or
  services where only one document states them, a one-column table of services (Docling reads
  its first row as a header), and a contract whose parties are named without quotes (each a skip
  note, as before).

### Rule 11: spelling, measured again at 300 a class (2026-10-02)

The prose rule (`checks/aoa_spelling.py`, unchanged since the attempt described under "Decisions
taken against measurements") on fresh seeds, every case a mutated act parsed by Docling:

| class | seed | detected | correction right | false findings |
|---|---|---|---|---|
| `prose_typo` (any letter, at random) | 101 | 87% | 84% | 1% |
| | 102 | 87% | 85% | 2% |
| | 103 | 91% | 87% | 0% |
| | 104 | 89% | 87% | 0% |
| `prose_slip` (the slips of a hand and an ear) | 101 | 95% | 93% | 1% |
| | 102 | 92% | 89% | 1% |
| | 103 | 93% | 91% | 0% |
| | 104 | 91% | 90% | 0% |

* **The false findings of seeds 101 and 102 were rule 10's:** a slip planted in the label
  "საიდენტიფიკაციო კოდი" left the code beside it unread, and the new "code left out" warning
  fired; a code is now left out only when its digits are nowhere in the act (seeds 103 and 104:
  0%).
* **Why a slip is missed, on the dictionary's own words** (`ka_GE`, five letters or more, one
  hand slip each, never the first letter): when the word the slip was made in is written
  elsewhere in the documents, 96.5% are corrected and none wrongly; when it is not, 89.9% - the
  rest are ties between two dictionary words one letter away (4.3%), a wrong pick among them
  (2.8%), a slip that is a form the dictionary lacks and the rule must accept (2%: a final "ი"
  dropped reads as an attributive), and another form of the same verb or participle (1%).
* **Tried and not taken:** reporting a tie as "check this word" (the words the dictionary lacks
  that are one letter from two others are the verb forms that made the false flags of the
  held-out prose run), and ranking a correction by any form of it the documents write (worse:
  1,115 right against 1,119). Random replacements no hand makes stay unreported unless the
  documents write the word: the rule is built not to call a word it does not know a misspelling.
  The limit is the dictionary's; a wrong pick (2.8% of slips in words the documents do not
  write elsewhere) is a correction a reviewer rejects, and the finding still marks the slip.

### Marking: comments and tracked changes in the act (`report/docx_annotate.py`)

The client asks for each field that needs correcting to be marked in the file itself, "with the
correct variant as a comment or Track Change". Every finding is a Word comment, as before; where
the rule knows the exact words that replace the ones at fault (`Finding.replacement`: a code, an
account, a bank code, a cadastral code, a name or a director in the act's case, a date in the
act's style, a misspelt word, a fee or an amount in words, a service's or the subject's number,
a payment deadline, the registry's legal form), the file also carries a tracked change, word by
word: "შპს „დელტა ტრთიდი“" keeps its quotes and only "ტრთიდი" is struck and "ტრეიდი" inserted.
A change is written only when the words line up safely (as many words on both sides, one value
for another, or a run of words between words both share); a value left out, a period or term
that is advice, the tax wording, a role swap and a note to check stay comments. The first
finding to claim words writes them; revision ids start above every id the file uses; a run that
is more than text (a field) is never rewritten. A note on the act's first word lists what could
not be checked and what does not apply, and any finding no words of the act stand for - the
body's text is left as it was.

* **Mutation harness, tracked changes scored** (`scripts/mutate_aoa.py`, "track": after accept
  all, every wrong place reads the correction): seed 201, 20 a class, every class - 100% wherever a
  change is offered (codes, accounts, bank codes, the cadastral code, the act's date, director,
  address, project, month and number-word typos, Latin or Cyrillic letters, prose typos); party
  names, which the first version left without a change, 100% on seed 202. Detection, correction
  and comment places unchanged, 0% false findings on the 14 harmless classes.
* **The gold sets' acts, annotated as the product annotates them:** client 6 acts / 27 findings
  with a replacement / 31 tracked insertions, Test Set 2 12 / 38 / 44, Test Set 3 23 / 19 / 22;
  on every act the text with all changes rejected is the act's own, paragraph for paragraph, the
  text with all changes accepted holds every insertion, and LibreOffice opens every file. The
  strict gold's comment places are unchanged (0 occurrence errors).
* **Where a Word file keeps text outside its body:** a form's content controls (`w:sdt`) and text
  boxes are text Docling reads, so a finding can stand there; both are now searched and marked
  in place (a text box Word keeps twice, as DrawingML and as a VML copy for older readers, is
  marked once, in the copy Word shows). Headers and footers cannot carry a Word comment: a
  finding that stands only there goes in the note at the top. On the gold sets nothing moved
  (strict findings, comment places and the snapshot of every output unchanged).
* **An act that arrives as a PDF** (`report/pdf_annotate.py`, `?format=pdf`, the report's PDF
  download) is returned with a highlight on the words at fault and the comment as its note,
  found as in a Word file - quotes, spaces and look-alike letters set aside, since a PDF's text
  often lacks the quote marks its font draws and breaks a phrase across lines (one quad a line).
  A scanned page has no characters: the line or table cell Docling's OCR read the words in is
  highlighted, and the note says so. The summary and the findings no words stand for are a note
  on the first page; nothing but annotations is added (tests check for no script, action or
  link). Measured on the synthetic fixtures and an upload through the API end to end, and on
  the client's 7 scanned acts inside the image (Tesseract), each checked against its Word
  contract: 45 findings, 41 marked on the line OCR read them in (32 highlights, some lines
  holding two findings), 4 in the first page's note.
* **A page cropped or turned (red team R6, R6-13 and R6-14):** a page shows its crop box,
  turned as it says. The note was placed by the page's full size, unturned, and could fall
  outside what is shown; a scanned line's box, which Docling gives on the page as shown, was
  used as if in the page's own space. Both now go through the crop box and the turn - checked
  against PDFium's own page-to-screen transform for every quarter turn. The annotated copy is
  also rebuilt from the act's pages (R6-08, `docs/SECURITY.md`); on Test Set 4's PDF acts every
  mark is where it was.

### Scanned PDFs (OCR inside the Docker image, Tesseract `kat+eng`, 216 dpi)

The client's 7 scans, each paired with its Word twin by content: character error rate 0.16-0.40
against the twin (an upper bound: layout order differs). Each scanned act checked against the
**Word** contract: every planted mistake reported, no false error, the correct act clean. Against
the **scanned** contract, the contract's own OCR is the limit: its IBAN and bank code read 0 of
1, so Mistake #3's bank change cannot be compared and Mistake #4's wrong period year is reported
as an act-date error; the correct act stays clean.

### Five red-team rounds

Each round, one agent per module wrote new adversarial documents (synthetic, never client data)
- other wordings, layouts, number and date formats, combinations - and an independent verifier
reproduced every claim. Every confirmed defect was fixed test-first, and each round re-ran every
earlier round's repros as regressions (none regressed, but for two partial fixes found and
completed in the next round):

| round | variants tried | confirmed defects | share of variants | policy questions |
|---|---|---|---|---|
| 1 | 506 | 88 | 17.4% | 13 |
| 2 | 546 | 58 | 10.6% | 8 |
| 3 | 450 | 46 | 10.2% | 7 |
| 4 | 584 | 45 | 7.7% | 13 |
| 5 | 614 | 48 | 7.8% | 12 |

The rate fell and flattened: about **one deliberately unusual variant in thirteen** still found a
defect in round 5. Of the 285 confirmed, all are fixed except those under "Known limits" below
(English documents, a bare "თარიღი:" label, and the open policy questions); a few fixed only in
part in their round were completed in a later one, and their repros pass. Round 5's fixes were
not themselves red-teamed. After the rounds, four BOQ questions left open were decided and
built (2026-10-01): a bid in another currency (`BOQ.CURRENCY`, not ranked), a negative rate
(`BOQ.NEGATIVE_RATE`, no price), a discount below the total incl. VAT (`BOQ.DISCOUNT`), TOTAL
COST by its usual names, and a profit rate typed beside its total held on cost + overheads. That is the honest measure of what an unseen wording or layout may still do; the
client's own documents and the three test sets pass completely. The decisions taken on the
policy questions are in docs/GOLD_SPEC.md; the ones left open are listed below.

### Repeatability

`scripts/snapshot_outputs.py --include-held-out` (every module's output on every set, the
held-out one too) run three times on the same code: three byte-identical files (418 KB, one
SHA-256). Rules and anchors only, no model in the path, so the same documents give the same
report on every run.

### Tests

4,106 unit and integration tests, 93.8% line coverage (`scripts/check.sh --offline`: ruff,
format, mypy strict, bandit, pytest).

### The deployable image

`docker build` of this branch, run as compose runs it (read-only root, tmpfs, every capability
dropped, models mounted read-only, no extra mounts): `/health` and `/ready` 200; the client's
acts checked against the Word contract give the same findings as the dev box, prose spelling
from the dictionary baked into the image; scan 4174 parsed through the image's OCR in about 18
s, its planted fee and amount-in-words errors reported, no false error, and its OCR misreads
(`მოედაწი` for `მოედანი`) reported as "possibly misread from the scan - check the paper
original" warnings. The image still carries CUDA wheels, as `main`'s did: `uv.lock` resolves torch
2.14.0 from PyPI (the CUDA build, which pulls the `nvidia-*` packages) although `pyproject.toml`
names the CPU-only index for it. Re-locking against that index should drop several GB - an
image-size issue, not an accuracy one, left for its own change.

### Known limits

* **Unseen wording.** See the red-team table: new wordings still break about one variant in
  thirteen of a deliberately adversarial set; most fail loudly (a skip note or "could not be
  read"), not with a wrong answer.
* **English documents.** An act or contract in English gets skip notes, not findings; English
  party blocks and amounts in words are not read.
* **A scanned contract** is only as good as its OCR: codes it misreads cannot be compared.
* **Prose spelling** rests on the hunspell `ka_GE` dictionary, which lacks words and forms:
  83-92% of random typos are found, and a correction no document writes is a note to check, not
  a warning.
* **A misspelled surname that no longer declines regularly** ("მერგანძიე" for "მერგანიძე", an invented name) is
  reported in the comparison with its value read off its genitive ("მერგანძიი").
* **A bare "თარიღი:" label** is not read as the act's date (it may be the contract's), and
  English act and contract wording is out of scope.
* **Open policy questions** for the client: resource-based estimates in BOQs (a work row
  whose resource rows carry the amounts), a sweep-up "any other works" line every bidder prices,
  a fixed coefficient cell in every amount formula, which total a bid offers when it gives a
  discount below its total incl. VAT, pairing a contract's and an act's parties by role in the
  comparison, and whether a one-sided date counts as carried when the other document states it
  in another role.

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

Red team round 3 (2026-10-01) found five more, each fixed by rule with a unit test and a near
miss that must not match (`tests/unit/test_redteam_r3_dates.py`):

* **A day and a month before a full date share its year** (`01.06-30.06.2026`, `25.05-30.06.2026`,
  `1 ივნისი - 30 ივნისი, 2026 წ.`). Read as its last day alone, a correct act's period was one
  day, a year slip was quoted without its first half, and a period starting before the term
  went unreported. `rules.find_dates` now reads the leading day and month as the first day, in
  the full date's year, where the month in figures has two digits as the full date's does
  (`1.2-3.04.2026` reads as a clause or version number), nothing before it makes it part of
  another number (a digit, also one word before, a stop, slash, dash, letter or `N`), and the
  first day comes before the last. The round-2 near miss `01.06-15.06.2026` is now a range.
* **A range's first day ends before its dash.** The first day of `01-31.05.2026`, `1-31 მაისი,
  2026 წ.` and `2026 წლის 1-31 მაისი` took the dash into its span, so the comparison, which joins
  two dates with a dash between them, saw two dates: a restyled window was "Execution period
  removed", a moved end a delivery deadline added. The span now ends at the day (a "from" form,
  `1-დან`, keeps its postposition), and a range's first day takes the type of the date that
  closes it, so the two are one period to every reader.
* **A four-digit numeric date keeps its year word** (`05.07.2026 წ.`, `05.07.2026წ.`,
  `05.07.2026 წელს`), as the two-digit one already did: the act's date is quoted whole and its
  correction keeps the word. `წლიდან` and other words that only start like it stay out.
* **A date's role is on its month, before a year written after it.** `30 ნოემბრამდე, 2026 წ.`
  was read as a validity start (the "until" was lost behind the year), and `30 ნოემბრის
  ჩათვლით, 2026 წ.` not at all. `rules.date_role` reads the postposition off the date's own
  words whatever follows; the contract-term reader and the comparison's value masking use it,
  and the date reader reads the inclusive form whole, except where its year opens the next date
  (`30 ნოემბრის ჩათვლით, 2027 წლის 1 იანვრიდან`).
* **An act's heading date is the act's.** The extraction promoted every unlabelled preamble date
  to `contract_date`; in an act that dated its contract in a wording the act reader did not know
  (`N 11/2026 ხელშეკრულება, დადებული …`, `ხელშეკრულების (თარიღი: …)`, `03.03.2026 №11/2026
  ხელშეკრულების`), the act's own date stood as a second, conflicting contract date. The preamble
  date of an act is now `act_date`, and the act reader knows those wordings (the contract's
  number read as the contract-number reader reads it). A date after the contract named for
  another purpose (`ხელშეკრულების შესაბამისად, … 10 ივნისს გადაიხადა`) is not its signing.

Red team round 4 (2026-10-01) found six more, each fixed by rule with a unit test and a near
miss that must not match (`tests/unit/test_redteam_r4_dates.py`):

* **One reader for an act's service period** (AOA-R4-03). The act check read the period after a
  closed list of words; the extraction read it there and then by its form. An act whose period
  followed `შესრულებულია`, `გაწეულ იქნა`, `განხორციელდა`, `ჩატარდა` or a bare `პერიოდი:` got a
  warning when correct and passed when dated a day early, while the extraction stated the period.
  `AoaChecker._period_hits` now reads both ways and `document_facts` states what it read. A past
  verb opens the period only in its own sentence (`მომსახურება შესრულებულია სრულად.` dates
  nothing after it); the form reader skips a guarantee's, an advance's or the validity's
  sentence and a recital of the work still to be done, and ranks a sentence with a past verb or
  a period label above one that only names the service. Two near misses the extraction used to
  read as the period (`საგარანტიო პერიოდი: …`, a recital of the contract's window) are no period
  now. Measured on the mutation harness (seed 149): the prose-typo class's false positive (a slip
  that turned `შესრულდა` into `შეასრულა` lost the period) went from 17% to 0%.
* **A date that names the contract, one predicate** (COMPARE-R4-01). The period reader knew a
  contract reference only with `N`; after `№ 318` the contract's date became the period's first
  day, so a contract-vs-act comparison stated a wrong period and a revision of the contract's
  date was two errors. `aoa.contract_named_by` reads the number as the number reader does and
  serves the act's date, its period and the extraction's contract date.
* **An act's heading date under a title that dates its contract** (AOA-R4-05): only the first
  header date was tried. Now a date its own reference names as the contract's is passed over,
  its contract word no longer disqualifies the next date, and a later date must stand before
  the preamble ends.
* **The act's date in its other wordings** (AOA-R4-07): one verb alternation (`გაფორმებულია`,
  `შედგა`, `ხელი მოეწერა`, the label's copula `თარიღია`) for every reader of the act's date,
  whole words only.
* **A validity stated as a length** (EXTRACTION-R4-08) was an open range from the signing. The
  lead decided it is the closed range: the length counted from the clause's first day or the
  signing, the day kept, held to the month's last. Only the validity's own length closes it - one
  that follows `მოქმედებს` / `ძალაშია` / `valid` in its clause - so a notice period or a
  report's deadline in the same sentence leaves the range open. The red team's probe asks for
  `P12M`; it reports the range now, by that decision.
* **A signing date labelled at the foot** (EXTRACTION-R4-11) was read as `contract_date` by the
  chunk rule but not by the term reader, so the validity "from signing" was lost. The term
  reader now takes a labelled signing date anywhere when the preamble has none.

Red team round 5 (2026-10-01) found these in the date and term readers, each fixed by rule with a
unit test and a near miss beside it (`tests/unit/test_r5_dates_and_terms.py`):

* **Every act-date reader passes over the contract's date** (AOA-R5-01). The signing-verb branch
  of `AoaChecker._act_date` took the first date in its sentence, so `აქტი შედგენილია 2026 წლის 15
  მაისის ხელშეკრულების საფუძველზე` dated a correct June act 15 May (an error). All three readers
  now drop the dates `contract_named_by` claims, and that predicate reads a numeric date's case
  on a hyphen (`15.05.2026-ის ხელშეკრულების`).
* **A service month after a label, its year after the month's noun** (AOA-R5-07): `საანგარიშო
  თვე: …` opens the period, and `ივნისის თვეში, 2026 წ.` is the month - a correct act got a
  warning and a late one passed.
* **An English act** (AOA-R5-11, a regression of AOA-15): round 4 taught the period reader English
  service words, so an English act's period was read and its date was not, and a correct act got
  the "date not found" warning. The act's date is read in English (`This Act was signed on …`)
  and, unread on an act not in Georgian, is a skip note.
* **"Not later than" ends a range** (EXTRACTION-R5-01): `არაუგვიანეს 2026 წლის 31 დეკემბრისა` was
  read as the validity's first day. `contract_terms._NOT_LATER_THAN` serves the range reader and
  the payment deadline.
* **The validity's "until fulfilled"** (EXTRACTION-R5-02): `შესრულებამდე` in the state
  template's validity clause made the chunk classifier type its cap a bare `execution_period`.
  A date in a validity clause whose only performance word is that until-phrase is the validity's.
* **The English validity clause** (EXTRACTION-R5-13): `shall enter into force … and shall remain
  in force until …` was no validity (`into force` was not a predicate), and `until the full
  performance` was read as a performance window.
* **An act of handover or of work done** (EXTRACTION-R5-03) was read as a contract, so the act's
  own date replaced the contract's. `document_facts._ACT_FAMILY_TITLE` accepts those titles on a
  line of their own only - in the nominative, no clause number, no full stop - so a contract that
  names the act in a clause stays a contract.
* **A performance window of a first day and a length** (AOA-R5-06, policy decided by the lead):
  it was open-ended, and a late act passed silently. It is the closed range now, as the validity
  is since round 4; `contract_terms._performance_length` takes only a length right after the
  window's first day or its label, so a report's deadline in the same sentence closes nothing.
  A comparison of such a clause whose length changed reports the period and, as round 4's
  validity length already did, the length's own duration beside it: two findings for one change,
  left to the comparison's de-duplication.
* **A staged delivery** (COMPARE-R5-11, policy): each batch's date is a delivery deadline.
* **A preamble date on one side** (COMPARE-R5-10): a draft's date filled in or deleted was "Date
  (unclassified)"; it is "Contract date", when the other version writes no date of that value.
* **A billing cycle across two months** (AOA-R5-12): decided 2026-10-02 - an act on the period's
  last day or on the month's end is right (GOLD_SPEC 3).

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
inside longer ones (a stem the act writes with an ending) are still marked there. Two cases mark
only some places of the words, because the same words are right elsewhere. A name copied from
the other party (AOA-09): the customer's director written for the contractor is right in the
customer's own block. The act check attributes each place to the party named last before it
(its name, code or role label) and, when some places are plainly the other party's correct
director, the finding carries which of the places are its own (`Finding.places`: ordinals among
the whole-word places, and their count); a form whose every place is the other party's own
director is no finding at all (red team R4, AOA-R4-01). And the act's own date (red team R4,
AOA-R4-06): an `AOA.ACT_DATE` or `AOA.ACT_DATE_IN_TERM` finding marks where the act is dated -
where the date reader read it, after the words that date the act, or alone in the heading -
never the same date written elsewhere as the recital's contract date or the period's end. The
check counts the places with the annotator's own reader (`docx_annotate.whole_word_spans`). The API annotates the Word file without the checked text, so the ordinals are counted in
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

### Red team, round 2: what a correction may say (2026-09-30)

The second round found the act's findings right but their corrections wrong, or findings on
correct acts, where the rules read less than the act wrote (`tests/unit/test_aoa_redteam_r2.py`):

* **A correction is one value, written as the act writes it.** A fee slip against a contract
  stating a monthly price and a total was "corrected" to both, in ISO form; it is now the price
  the fee is a slip of (fewest digit edits, then the smallest difference) or none, as round 1
  did for accounts and plots. Words in brackets after the currency (`3 540.00 ლარს (…)`, `GEL
  3,540.00 (…)`, `3 540 ₾ (…)`) were left out of the quote and the correction, so typing the
  correction over the mark left a second wrong amount; the digits-and-words reader now allows
  the currency between the two, when the bracket reads as a numeral. Tetri written after the
  lari were copied, so the correction was the act's own text; they are rewritten from the
  price, and an error whose correction equals its quote now carries none.
* **The contract says which half is wrong.** A service count whose digits changed
  (`15 (თორმეტი)` for `12 (თორმეტი)`) had its words corrected towards the wrong digits,
  against the services rule. Every figure the contract writes in agreeing digits and words now
  decides, as its prices did, and so does any number the contract's item aligned with the act's
  service states, in digits alone too (`- 12 პოსტი`). A bare number anywhere else in the
  contract (a clause number, a day count) is not the pair's counterpart. Still open: a contract
  item that writes the count in words only (`თორმეტი პოსტი`) is not read as a number, so the
  act's `15 (თორმეტი)` there has its words corrected; the services rule only notes that item.
* **One VAT token.** `დ.ღ.გ.-ს ჩათვლით` and `… ლარს ჩათვლით დღგ-ს` said "VAT included" and
  were errors for a VAT-registered entrepreneur. The dotted abbreviation is one token every tax
  pattern shares (the tax amount labels and the VAT-number label too), and `ჩათვლით` before
  the tax may follow the amount's currency word; after any other word it still closes a date.
* **The fee before VAT at the rate.** An act restating its fee before VAT in brackets, with no
  VAT amount, had that figure compared as a second fee. It is set aside when another fee the
  act states is it plus VAT (18%, or a rate either document writes beside VAT); a net figure
  the act states alone is still compared, since that act states its fee without VAT.
* **A name is written whole.** `შპს „ბეტა სერვის“` passed as `ბეტა სერვისი`: the word matcher
  takes an attribute's bare stem as a form of the word. For a party's name a bare stem is a
  misspelling unless an ending is attached to it (`„ბეტა სერვის“-ის`). Whether a case ending
  may stand inside the quotes (`„ბეტა სერვისის“`) stays open; it is accepted.
* **A grouped code is quoted as printed**, so its Word comment marks it rather than landing at
  the end of the document; and a period whose start year alone is wrong is corrected in that
  end, never to the contract's term in ISO.

Round-2 AOA-R2-17 (the customer's own account in its signature block, against a contract that
states only the contractor's) is left as it is by design.

### Red team, round 3: parties, signatories and the Word text (2026-10-01)

Fixed in the shared readers (`extraction/anchors.py`, `normalize.py`, the Word conversion), each
with a unit test and a near miss (`tests/unit/test_redteam_r3_parties.py`,
`test_redteam_r3_ingestion.py`):

* **A code of the wrong length is a code to compare (AOA-R3-01).** Behind an ID label a run of
  7 to 13 digits, or groups of three split by one space, hyphen or dot, is the code the act
  states; a digit dropped or doubled left the contractor's code unread and the rule silent.
  The correction is the contract code within two digit slips (an edit distance that counts a
  dropped or doubled digit), in the act's grouping (`987-654-312` → `987-654-321`; a round-2
  test now expects the grouped `987 654 321`), or none. Two readers share the label: the code
  facts keep 9 or 11 digits (`normalize.ID_CODE_RE`, GOLD_SPEC 2), the party reader and the act
  check also read a slip's length (`ID_CODE_WRITTEN_RE`). Left open then, as the verifier asked:
  a party whose code the act leaves out was not reported. It is now (rule 10, 2026-10-02): a
  warning whose correction is the contract's code.
* **`ინდივიდუალური მეწარმე` is `ი/მ` (AOA-R3-04).** The long forms are one table
  (`_SPELLED_OUT_FORMS`) the legal-form pattern is built from, so a form read is always written
  short.
* **A functional director is not the head (AOA-R3-05).** A `დირექტორი` narrowed by a named
  function adjective (`ფინანსური`, `კომერციული`, `ტექნიკური`, `საოპერაციო`, …) co-signs beside
  the head and is not compared. The adjectives are listed, not guessed from an ending: a
  surname ends in `-ური` or `სა-…-ო` too, and in a flattened act it stands right before the next
  title.
* **A patronymic is no part of the name (AOA-R3-06).** `<first name> <father's name>ის ძე|ასული
  <surname>` is read as the first name and the surname; a finding quotes the whole name and
  keeps the patronymic in its correction.
* **Formatting inside a word is no break (AOA-R3-13).** Docling split a Word paragraph at each
  bold, italic, underline or hyperlink run and stripped the pieces, so `3 4` + `50` read as two
  figures and `ბარამ` + `იძე` as a misspelled name. The Word backend keeps one text per
  paragraph (`docling_service.WholeParagraphWordBackend`); the conversion fingerprint changed,
  so cached parses are made again.

Measured after the fixes: the three strict gold sets unchanged at 100%/100%, and
`scripts/mutate_aoa.py --seed 103 --per-class 6` the same as before them, class by class: every
error class detected except one `prose_typo` mutation (83%; one `prose_slip` correction is off
too), both in the spelling reader these fixes do not touch, and 0% false positives on the
harmless classes.

### Red team, round 4: the party readers (2026-10-01)

Fixed in the shared readers (`extraction/normalize.py`, `anchors.py`, the chunker), each with a
unit test and a near miss (`tests/unit/test_r4_party_readers.py`, `test_r4_keyterms_table.py`):

* **One legal-form table (AOA-R4-02, COMPARE-R4-05, EXTRACTION-R4-06).** The party reader knew
  `შპს|სს|ააიპ|სსიპ|ი/მ` and the long forms; the contractor's kind knew `კს`, `სპს`, `შ.პ.ს.`;
  a municipal buyer written `ა(ა)იპ` - the usual spelling - or any dotted form was no party.
  Its name, address and signatory were then compared with nothing and nothing was said; an
  act writing `ააიპ` for the contract's `ა(ა)იპ` was "party not found", and a renamed party was
  only text changed in a comparison. `normalize.LEGAL_FORMS` is now the one table (long,
  dotted, bracketed and plain spellings, each with its abbreviation); the party reader, the
  form after a quoted name, the address's next-party stop, the comparison's wording fold and
  `georgian.legal_form_kind` are built from it. `ა(ა)იპ` folds to `ააიპ`, the abbreviation the
  code already used (the verifier's recommendation; the extraction probe's `ა(ა)იპ …` value is
  therefore `ააიპ …`). A side the contract defines with fewer parties read than sides is an
  `AOA.PARTY_NAME` skip note. `aoa._LEGAL_FORM_WORDS` gained `კს`, `სპს`.
* **An address ends where the representative is named (COMPARE-R4-04) and where another
  address label starts (COMPARE-R4-08).** `, წარმოდგენილი მისი დირექტორის … სახით` was part of
  the address, so one director change was two errors. A head's title ends an address only with
  a name after it, so a street named after an office stays whole. The comparison reads a Word
  requisites row as one flattened line, where the line-based stops never fire: the address
  label is the stop that holds there. A cell with no label of its own still runs on into the
  next one - the joining is in the segmenter, outside these readers.
* **A signatory before its title or in slashes (AOA-R4-04).** `დირექტორი ______ /X/`, `X -
  დირექტორი` and `X (დირექტორი)` gave no signatory, so a copied or unrelated name at the
  contractor's signature passed; only a one-letter slip was caught, as a near form of the
  contract's name. The title reader skips a signature's blank and a slash or bracket (shared by
  the director, deputy and attorney readers); a name before a head's nominative title is read
  where the name opens its line or cell and the title closes it. Each layout now reports what
  `დირექტორი: X` reports; a slip is still reported once.
* **An attorney at the signature (AOA-R4-12, policy).** `მინდობილი პირი: …` and
  `წარმომადგენელი (მინდობილობით) …` are the attorney's titles, read as `მინდობილობით …`
  is. A slip of the attorney the contract names is an `AOA.DIRECTOR` error with the correction,
  as a director's slip is; anyone else keeps the power-of-attorney warning. The lead's policy
  asked for a skip note where the contract names no attorney: that case already had the
  warning (pinned by `test_layout_variants`), so the warning stays and it is never silent.
* **A key-terms table keyed by item numbers (EXTRACTION-R4-07).** Docling's triplets put a full
  stop between a row's label cell and its value cell (`1, პირობა = X. 1, მნიშვნელობა = Y`), so
  the five terms of the table were untyped. Such a table (three or more columns, one header
  row, item numbers in the first column, no price/quantity/unit/sum header) is written one row
  to a sentence, `1, პირობა = X, მნიშვნელობა = Y;`. A price table keeps the triplets: the rules
  read its line amounts cell by cell (`rules._TABLE_CELL`), and a lump-sum line keyed by an item
  number must stay a line, never the total.

Measured after the fixes: the three strict gold sets unchanged at 100%/100%, and
`scripts/mutate_aoa.py --seed 137 --per-class 6` detects every error class at 100% except
`prose_typo` and `prose_slip` (83% each, one mutation each; one more `prose_slip` correction is
off), the same cases with the same messages as on the code before these fixes - the spelling
reader, which they do not touch - and 0% false positives on every harmless class.

### Red team, round 5: the party readers (2026-10-01)

Fixed in the shared readers (`extraction/normalize.py`, `anchors.py`, `georgian.lemma_word`) and
in the act check's party matching, each with a unit test and a near miss
(`tests/unit/test_redteam_r5_readers.py`):

* **A party's name slipped in its signature block alone (AOA-R5-04).** The signature block
  writes no code, so a slipped name matched no contract party and was "party not found in the
  contract" with no correction, though the same slip in the preamble was corrected. A party
  with no code whose name matches none is now matched to the contract party whose name it is a
  slip of (`classify_difference` judges it a typo, or a word's final `ი` is dropped); where two
  are that close, the side its role word gives decides. A name nothing like either party's
  stays "not found", with no correction.
* **A draft's blank is no value (COMPARE-R5-07).** `მისამართი: ______` was read as the address,
  so filling it in was "changed" from underscores. A value with no letter and no digit
  (`normalize.is_blank`) yields no fact, and so does a party name whose quotes after the legal
  form hold only a blank (`შპს „______“`); the act check's own party record is unchanged.
* **Where an address ends (EXTRACTION-R5-06).** The round-4 stop before `მისამართი:` left the
  next address's qualifier (`, ფაქტობრივი`) in this one, and a treasury code, a VAT payer's
  number or a website ran on into it. An address now also ends at a comma before one to four
  words with no figure and a colon, before a qualified address label whatever the separator,
  and before a website; a part of the address with its colon (`კორპუსი: 3`, `სართ. 4:`) stays.
* **A double surname (EXTRACTION-R5-12).** The name readers stopped at a hyphen. A second part
  of three letters or more that is no case ending, postposition, title or side is now part of
  the surname, and only that last part takes the nominative. The act check compares the whole
  name: a correct pair is clean and a slip in the second part is `AOA.DIRECTOR` (pinned).
* **`პირადი №` (EXTRACTION-R5-16, the lead's policy).** `პირადი` with a number sign before
  eleven digits labels a personal number; `პირადი` alone, or before nine digits, does not.

Measured after the fixes: the three strict gold sets unchanged at 100%/100% (client_v2, ts2,
ts3), and `scripts/mutate_aoa.py --seed 167 --per-class 6` detects every error class at 100%
with its correction and place, and 0% false positives on every harmless class.

### Red team, round 3: what the act check reads, and what a correction says (2026-10-01)

Round 3 found figures written in forms the readers did not know, and corrections that were
right in value but wrong in place or style (`tests/unit/test_aoa_redteam_r3.py`):

* **Tetri after a hyphen.** `3 450-00 ლარს`, the bookkeeping style, was read as a fee of `00`:
  a correct act had two errors, and a slip was never quoted. The money reader (`amounts.py`) now
  joins the two digits after a figure's hyphen to it as tetri, and the digits-and-words reader
  starts no figure there. Two digits more than the figure before them (`10-15`, `90-95`) are
  a range, not tetri; three digits after the hyphen, a figure with a fraction of its own, and a
  date's digits are none either. The correction keeps the style (`3 450-00`).
* **An initial and a slip in the surname.** `ო. ფუტკარაძი` for `ოთარ ფუტკარაძე` passed: the
  party reader took it for the full name, and the forms the rule compares were only full ones. The
  rule now also finds each initialled form of a signatory, and compares an initial with the
  first letter of the contract's first name and the surname by the full name's rule. A correct
  initialled signature that was the act's only form of the name had been an error with no
  correction; it is clean now.
* **Before VAT is its own role.** A figure labelled `დღგ-ს გარეშე` beside the act's fee was
  compared with the price and corrected to it - writing the gross into a net slot. It is now
  compared with the price less VAT (18%, or the one other rate the documents write) and
  corrected to that. In a breakdown whose fee and net are right, the VAT amount that does not
  add up is the one reported (`562.27` -> `526.27`); round 1 and 2 put that finding on the
  correct net, and their tests now pin it on the VAT. A net stated alone stays the act's fee
  (round 2). An exclusion phrase labels the figure it follows directly, else the one after it
  in its sentence, so `… ლარს დღგ-ს ჩათვლით (დღგ-ს გარეშე 500 ლარი)` labels the 500.
* **A price table's total.** A table whose column header alone names the currency (`ღირებულება
  (ლარი)`, `თანხა, ლარი`) held figures no reader took for money, so a slip in its `სულ:` row
  passed with no note. The act's table total - the last row a total word names, not one before
  tax, not a column of rates - is now compared as a fee. Only the act's: the contract's prices
  are read as before. A table with such figures and no total row leaves a skip note.
* **The term as a correction.** When no single year puts the period inside the term, the
  finding quoted only the date outside and gave the term in ISO. It now quotes the whole period
  and writes the term over it in the period's own style, each end keeping its case and
  postposition; a month written with no day gets the term written out in the long style.
* **A slip in the signing verb** (the mutation harness): `„აქტი“ გაორმდა 2026 წლის 30 სექტემბერი`
  made `AOA.ACT_DATE` say the act was undated. The act-date reader accepts one edit in `გაფორმდა`
  after the act's name; `გაფორმდება`, `გააფორმეს` and a slip after `ხელშეკრულება` date nothing.

Round-3 AOA-R3-12 (a services annex written as a Word table is not read as the act's services;
the skip note names the list form it looks for) is left as it is by design.

### Red team, round 4: what is the fee, and corrections in the act's words (2026-10-01)

Round 4's act-check defects owned here (`tests/unit/test_aoa_redteam_r4.py`):

* **A name copied into one signature** (AOA-R4-01). The customer's director written at the
  contractor's signature, with the contractor's preamble right, also gave an error on the
  customer's own, correct preamble genitive, and a Word comment there. A form of the name whose
  every place stands in the block of a party it is that party's director for is no mistake of
  the other party's, and is not reported; a form with a place in the wrong party's block is, and
  marked there only (AOA-09).
* **The act's date, marked once** (AOA-R4-06, policy decided by the lead): the comment marks
  the act's own date only, never another correct copy of the same date. See the comment
  placement above. An act that writes its own date twice (the heading and the signing line) is
  marked at both.
* **Running figures are no fee** (AOA-R4-08). A monthly act that also states the work done to
  date, the contract's balance or what was paid before had an `AOA.FEE` error, the balance even
  "corrected" to the monthly price. The amount roles (`contract_terms.amount_roles`) have a new
  one, `running`, for a figure whose own clause - the words before it, back to the sentence's
  start, the amount before it or a ";" - names it so: `დღემდე` (not `-დან დღემდე`, a period
  ending today), `წინა აქტ…`, `წინა პერიოდ…`, `უკვე / ადრე / სულ` before `გადახდილ…` or
  `ანაზღაურებულ…`, `ხელშეკრულების დარჩენილი / ნაშთი / აუთვისებელი`, `კუმულაციურ…`, and the
  English `to date`, `cumulative`, `already paid`, `contract balance`. A total word alone
  (`სულ`, `ჯამი`, `ჯამური ღირებულება`), `სულ ასანაზღაურებელია` (to be paid), the fee's
  remainder after an advance (`დარჩენილი თანხა`, `ღირებულების დარჩენილი ნაწილი`) and an
  advance said to be paid keep their roles, and so does a figure whose fee or fee part (`საფასური`, `ავანსი`, `დარჩენილი თანხა`) is named after the running words (`წინა აქტით შესრულებული სამუშაოს შემდეგ, მიმდინარე თვის საფასური …`). `AOA.FEE` does not compare a running figure and
  leaves a skip note naming it. `fx_invoice` is unchanged: its total takes a price, an amount
  with no role or a remainder, never a running figure.
* **The term over a period that writes its year once** (AOA-R4-09). `25.05-30.06.2026` got
  the correction `2026-06-01-31.05.2027`: the start, written with no year, had no style to
  copy, and fell back to ISO. Such an end now borrows the year as the other end writes it - its
  joiner, digits and year word (`25 მაისიდან` beside `30 ივნისის ჩათვლით, 2026 წ.` reads
  `1 ივნისიდან, 2026 წ.`), or failing that the other end's whole style - keeping its own day,
  month and `-დან`. A correction is ISO only where the act wrote ISO.
* **Another currency's correction** (AOA-R4-11). A lari fee against a dollar price was
  corrected to `1,200.00 USD`. The correction is now the fee as the act writes it with the
  contract's currency put in (`amounts.with_currency`): a word in its case, consonant stems
  (`ლარს` -> `აშშ დოლარს`) and the vowel stem `ევრო` (`ლარით` -> `ევროთი`) alike, a code as a
  code, a sign as a sign, tetri and cents swapped with it. The machine form is left only when
  the act's currency cannot be rewritten (a word only starting like one, `ევროპული`). The
  message still names the contract's price. `test_aoa_check`'s currency test pinned the
  machine form; it now pins the act's wording.
* **An attorney at the signature** (AOA-R4-12, the rule part; the reader that takes
  `მინდობილი პირი: …` in a signature block into `Party.proxies` is another change this round).
  The act's attorney who is the contract's attorney with a slip (`… გოგიჩაიშვილო` for `…
  გოგიჩაიშვილი`) is an `AOA.DIRECTOR` error corrected to the contract's attorney in the act's
  case, by the director's rule; another person stays the warning that asks for the power of
  attorney, with no correction, as does any attorney when the contract names only the director
  (ts3 `aoa_f2_08`). A signature line that names an attorney (`მინდობილი პირი`, `მინდობილობით
  მოქმედი პირი`, `წარმომადგენელი (მინდობილობით)`, `რწმუნებული`, then two or three words and
  nothing else) whom no party of either document is read with leaves an `AOA.DIRECTOR` skip
  note: not compared, never silent.

### Red team, round 5: the fee in parts, VAT joined by a hyphen, a branch BIC (2026-10-01)

Round 5's act-check defects owned here (`tests/unit/test_aoa_r5_fee_tax_bank.py`):

* **What is paid once the advance is deducted** (AOA-R5-02). A correct act that writes its fee
  and then what is left to pay once the contract's advance is deducted (`ავანსის (30%)
  გამოქვითვით ანაზღაურებას ექვემდებარება 70 ლარი`, `გამოქვითული ავანსი: 30 ლარი; გადასახდელი
  თანხა: 70 ლარი`, `აქედან გამოიქვითება ავანსი 30 ლარი, ხოლო ასანაზღაურებელია 70 ლარი`) had an
  `AOA.FEE` error "correcting" the 70 to the full price. `amount_roles` now calls such a figure
  the fee's `remainder` when the words before it deduct the advance (a `გამოქვით…` /
  `გამოიქვით…` / `გამოკლ…` form beside `ავანს…`, or `less / net of / after deducting the
  advance`) and the figure's own clause, after the amount before it, then names what is to be
  paid (`ანაზღაურებას ექვემდებარება`, `ასანაზღაურებ…`, `გადასახდელ…`, `გადაიხდ…`, `დარჩენ…`,
  `payable`, `due`). The deduction is looked for in the figure's sentence, and back across a
  `;` only past the advance's own figure (case `გამოქვითული ავანსი: 30 ლარი; გადასახდელი
  თანხა: …`) - never across a full stop or into a clause that opens with its number, and the
  noun `ანაზღაურება` alone is the fee itself: a contract's `ავანსი გამოიქვითება;` followed by
  `4.3. ყოველთვიური ანაზღაურება შეადგენს 100 ლარს` states its price. The deduction must be the
  advance's: `საშემოსავლო გადასახადის გამოქვითვით ასანაზღაურებელია 900 ლარი` (a natural
  person's net fee) stays the fee, as do `სულ ასანაზღაურებელია` and an advance named after the
  payable word (`გადასახდელი ავანსი: 30 ლარი`). When the act gives the advance only as a share
  (in the act or the contract, `30%`), the remainder adds up when it is the fee less that share
  (`_fee_parts_add_up`); an advance figure that is that share of the price is the contract's own,
  not a slip, when the parts do not add up. A remainder that is neither (75 of 100 after 30%) is
  still compared and reported.
* **"Already performed" is this act's work** (AOA-R5-08). `უკვე შესრულებული მომსახურების
  ღირებულება` was read as a running figure, so a correct act got a "no fee could be read"
  warning and a wrong fee passed. `უკვე` marks a running figure only before `გადახდილ…` or
  `ანაზღაურებულ…`, as the documented list always said; `დღემდე შესრულებული` (done to date) is
  still one. And an act whose every figure is a running one now gets the `AOA.FEE` skip note
  naming them, never the "no fee" warning, which stays for an act with no figure at all. An
  override for "this act's" words (`ამ აქტით`, `საანგარიშო თვეში`) before a running word was
  not added: with `შესრულებულ` gone no probe needs it, and `საანგარიშო თვის ჩათვლით დღემდე
  შესრულებული` (to date, this month included) is a running figure such an override would take
  for the fee.
* **VAT joined to "including" by a hyphen** (AOA-R5-09). `დღგ-ჩათვლით` was one word to the tax
  reader, whose word tail swallowed `-ჩათვლით`, so a VAT-registered entrepreneur's act was
  told it does not say VAT is included; `დ.ღ.გ.-ჩათვლით` also made the fee read as the tax
  amount (the tax-after pattern stopped short of the abbreviation's own stop), leaving the
  finding with no quote. A tax word's tail now never runs over a hyphen into `ჩათვლით`,
  `გათვალისწინებით`, `გარეშე`, `გამოკლებით` or `ჩაუთვლელად`, and a hyphen joins a tax to them as
  a space does (`დღგ-გარეშე` is an exclusion). `18 ლარი დ.ღ.გ.` closing its clause is still the
  tax, and `დღგ-ს გადამხდელის ნომერი` still a label.
* **A branch BIC** (AOA-R5-13, policy decided by the lead). An 11-character BIC with a real
  branch code (`AAAAGE22TBS`) against the contract's 8-character BIC of the same bank (or its
  `XXX` form) is an `AOA.BANK` warning naming the branch, text = the act's code, correction =
  the contract's code - not an error: the first eight characters name the institution (ISO
  9362) and the account is the IBAN's, compared on its own. The bank-pairing check takes such a
  branch for its bank. Another bank with a branch, the contract's branch against the act's
  8-character code, and two different real branches are not covered by the decision and stay
  errors.

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

*Historical (2026-09-23). Since the accuracy-hardening work these fields - `vat_status`, `payment_terms`, `subject`, `execution_period`, `contract_date`, `penalty` - are read by rules; see "Current results". The section is kept for the measurements behind the decision to keep the model optional.*

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

**A very large replace block is paired in order.** Scoring every left segment against every
right segment is quadratic, so past `_PAIR_BUDGET` (2500 candidate pairs) a block is paired in
order instead (`_pair_in_order`): each line looks for its counterpart among the next eight on
the other side, and a line that finds none is a removal or an addition. Until red team round 3
such a block became an all-removed plus an all-added, and one word inserted into each of 51
consecutive clauses turned two versions of one contract "unrelated", with every change dropped
(COMPARE-R3-05). The act-vs-contract pair is 59 × 68 = 4012 and lands there; its lines have
little in common, so it still leads with the field differences. Lines that differ only in their
clause number ("5.3." → "6.3.", the same text after it, once on each side) are paired first,
before the budget applies: an article inserted near the top of a 70-clause contract renumbered
every clause after it, made one block too big to pair, and the two versions scored 0.12 —
"unrelated", nothing reported. They now align at 0.97. Each renumbered clause is still its own
"text changed" warning; whether a clause number that moved with its clause is a change at all
is an open policy question (red team COMPARE-27).

### Extraction, red team round 2 (2026-09-30)

Defects a red team found and a verifier reproduced, each with a unit test on invented text
(`tests/unit/test_extraction_redteam_r2.py`); the readers are in `docs/GOLD_SPEC.md` section 2.
The decisions that are policy rather than reading:

* **`payment_terms` carries the advance.** Canonical form: the deadline, then `advance N%`
  (`P10D working advance 30%`), or the share alone (`advance 30%`); the chunk rule, the
  whole-document reader and the comparison share one builder (`rules.payment_terms_value`),
  and the merger demotes a value not in that form (a bare `30.0`) to a generic type, as it
  does a `vat_status` that is not one of its four words. A staged payment states the balance's
  deadline: it is the one that settles the price, and the advance's day is often counted from
  signing. Which deadline to report was left open by the red team; this is the choice.
* **A cap is no penalty.** Only the rate per period is a `penalty`; the cap stays a
  `percentage`, so adding or changing it in a comparison is one `CMP.PERCENT` finding.
* **An amount that restates a share** (a guarantee's `5%, 100 ლარი`, the VAT inside the price)
  is only read as such when the share or the tax word stands right before it (80 characters,
  no other figure between); `ღირებულება (18% დღგ-ს ჩათვლით) შეადგენს 1 000 ლარს` is the price.
* **VAT rate in comparisons.** A rate is the VAT's only when written right beside the tax word
  (`18% დღგ`, `დღგ-ს (18%)`, `VAT at 18%`); a discount in the same sentence is not. Its
  appearance or removal alone is not reported.

Measured after the fixes: the three strict gold sets unchanged at 100%/100%, and
`scripts/mutate_aoa.py --seed 79 --per-class 6` detected every error class with 0% false
positives on the harmless ones.

Round 3 (2026-10-01), in the readers every module shares (`tests/unit/test_redteam_r3_numbers.py`,
`test_redteam_r3_ingestion.py`):

* **A headerless two-column table is written row by row (EXTRACTION-R3-02, -03).** Docling
  promotes such a table's first row to the column header and writes its value into every other
  row as a column name, so each key-terms row carried the first row's figures (a false penalty
  `30%`, the advance's deadline for the balance), and a title row with no figure in its second
  cell was dropped. When the first row is data - a digit in either cell, or a label with no
  second cell - the chunker's table serializer writes the table as `label: value;` lines, each
  cell once. This replaces round 1's restore of the lost first line (EXTRACTION-12).
* **Every space groups thousands (EXTRACTION-R3-11).** One figure pattern
  (`normalize.FIGURE`) serves the extraction rules and the act check: a thin space, a narrow
  no-break space, a figure space or an apostrophe between the groups is one figure, never its
  last group (`36 000` was a total of 0); a figure never starts right after a digit and one of
  these separators.

### Extraction, red team round 3: money, terms and names (2026-10-01)

EXTRACTION-R3-01, 04, 06-10, 12-14 and COMPARE-R3-04, each with a unit test on invented text
and a near miss beside every widened pattern (`tests/unit/test_extraction_r3_money_terms.py`,
`tests/unit/test_compare_units_r3.py`); the readers are in `docs/GOLD_SPEC.md` section 2. What
was decided, and where one reader now serves several:

* **One payment reader.** The words that make a clause a payment clause are built from verb
  roots (`rules.PAYMENT_WORDS`), which `contract_terms` reads for the act check and the
  extraction alike; the act check therefore also reads a deadline written with `გადაუხდის`. A
  damages clause (`ზიანს აუნაზღაურებს`, `damages`) is no payment clause, as a penalty's is not;
  damage named only in passing (`თუ საქონელი არ არის დაზიანებული`, `free of damage`) leaves a
  payment clause one. English
  `transfer` was left out: it moves goods as often as money.
* **The VAT status is the price's.** The whole-document reader no longer lets wording decide the
  status that belongs to something else: a restated net figure's bracket
  (`rules.restating_bracket`, which the amount typing uses too, so the bracket's figure is an
  `amount` and the two never disagree), "net" measuring days or a weight, "net" inside `net of
  VAT`, and wording under a condition (used only when nothing else states a status). The kind
  precedence among what is left (net, exempt, exclusion, inclusion) is unchanged. The act
  check's own tax-wording rule reads `tax_phrases` unchanged: these filters are the
  extraction's.
* **A penalty's rate** is decided by one function (`rules.is_penalty_rate`) that the
  whole-document reader calls: the penalty word in the rate's own sentence, where the stop of a
  clause or heading number ends no sentence, so a heading `5. პირგასამტეხლო` still names its
  clauses' rates.
* **Other amounts in the price clause** are split off only past an explicit turn word; a
  monthly fee beside a named total is an `amount` rather than a `price`, because strict gold
  that states no `price` would count one as a false positive.
* **Contract numbers before the word** are read only before the contract's own words
  (`ხელშეკრულებ`, `შეთანხმებ`, `contract`, `agreement`): an act's or an invoice's number written
  the same way stays unread, so this reading adds nothing to the open policy question of other
  documents' numbers (EXTRACTION-R3-16, R2-16).
* **Units in comparisons.** `მ2`, `მ3` and `ც.` joined the comparison's closed unit list, in
  place, where the BOQ fold (`boq._UNIT_ALIASES`) already had them.

Measured after the fixes: the three strict gold sets unchanged at 100%/100%.
`scripts/mutate_aoa.py --seed 107 --per-class 6` detected every error class at 100% except
`prose_typo` (83%, one planted typo the speller misses and one wrong correction), with 0% false
positives on the harmless ones; the unchanged code at `ad72a3a` gives the same table, row for
row, so the spelling miss predates these fixes and is outside them.

### Extraction, red team round 4: lines, prices and the readers the checks share (2026-10-01)

EXTRACTION-R4-01 to -05, -09, -10, -12, -13, COMPARE-R4-02, -03, -13 and AOA-R4-10, each with a
unit test on invented text and a near miss beside every widened reading
(`tests/unit/test_extraction_redteam_r4.py`, `tests/unit/test_redteam_r4_rules.py`); the readers
are in `docs/GOLD_SPEC.md` section 2. What was decided, and where one reader now serves several:

* **A label line is a sentence (policy, EXTRACTION-R4-12).** Round 1 made line breaks no
  sentence end, for Docling's inline runs; since round 3 the Word backend keeps one text per
  paragraph, so a `label: value` paragraph's end is a real break. One detector
  (`rules.label_line_spans`) serves the keyword search, which stops at such a line, and the
  whole-document readers, which get a stop after it (`pipeline.document_level_facts`); the
  patterns themselves still match across a line break. The act check and the comparison flatten
  line breaks as before: the policy is the extraction's.
* **The contract's price is its total (policy, EXTRACTION-R4-13).** `rules.is_contract_price`;
  the LLM prompt's descriptions of `price` and `total_amount` say the same, so the prompt version
  moved. Round 2's test that read `The Contract price is GEL 2,000.00` as `price` now expects
  `total_amount`; its point, that a share of it is an `amount`, stands.
* **One restatement reader.** `rules.restatement` (which `document_facts` imports by its round-3
  name `restating_bracket`) decides when a money figure restates the price on another tax basis
  - bracket, turn, next sentence, breakdown - for the money typing and the VAT status alike.
  `; მათ შორის: დღგ-ს გარეშე - X, დღგ - Y` no longer gives `excl`, but gives no status: the
  inclusion wording reader (`contract_terms.tax_phrases`) reads none there.
* **One typing for prose and tables.** `rules._money_type` types a figure in prose and in a
  table cell whose header names the currency (`rules.find_header_amounts`, called from
  `run_rules` only). The act check reads those cells by its own reader
  (`amounts.header_money`), which `find_amounts` does not feed, so its fee is not read twice.
* **One duration reader, words included.** `rules.find_durations` reads a count written in words
  only; the act check's payment deadline gets it through `contract_terms.read_payment_terms`.
  The comparison still shows the figure first (`compare._with_figures`), a second reader of the
  same words; it now finds an already-read count and changes nothing, and is left for the
  comparison's owner to fold into the rules' reader.
* **Staged payments.** `rules.continues_payment` reads list items under a payment lead-in; the
  act check's paying sentences use it too.

Not fixed here, outside these readers: a rate's words added with the figure unchanged
(`5%-ის` -> `5 (ხუთი)%-ის`) still gives a `Number in words` warning from the comparison's words
reader, and a deadline's frame word changed with its value (`20 კალენდარული დღის ვადაში` ->
`20-დღიან ვადაში`) a `CMP.TEXT_CHANGED` warning; both are the comparison's masking. A
performance window written as a label line right after the preamble, before any numbered
clause, starts at the signing (`contract_terms.read_term`).

Measured after the fixes: the three strict gold sets unchanged at 100%/100%, and
`scripts/mutate_aoa.py --seed 139 --per-class 6` gives the table of the unchanged code at
`4138994` row for row: every error class detected at 100%, `party_name` and `prose_typo`
corrections at 83% and `script_letter` 17% false positives, all three already there before.

### Extraction, red team round 5: the readers the three modules share (2026-10-01)

EXTRACTION-R5-04, -05, -07, -08, -09, -10, -11, -14, -15, -17, COMPARE-R5-05, -06, -08, -09 and
AOA-R5-03, -05, -10, each with a failing unit test on invented text first and a near miss beside
every widened reading (`tests/unit/test_r5_extraction_fields.py`,
`tests/unit/test_r5_shared_readers.py`); the readings are in `docs/GOLD_SPEC.md` section 2. What
was decided, and where one reader now serves several:

* **One money typing.** `rules._money_type` now also types the contract's sum by its wordings
  (`_sum_named_whole`), a price of one item as a price (`_priced_per_unit`, on the `ღირებულება`
  path too, which never reached `is_contract_price`), and a figure its clause names an advance
  without a turn (`_named_a_part`). The comparison, the act check and the extraction read the
  same types, so `ავანსს 5 520 ლარის ოდენობით, ხელშეკრულების ღირებულების 30%` is an `amount` and
  `advance 30%` in all three.
* **One words-first reader.** `rules._words_before` reads the number words before a bracketed
  figure for the amount reader, the act check's digits-and-words pairs
  (`amounts._words_then_digits`) and the act check's fee correction (`aoa._fee_like`), so a
  words-first fee is read, compared and corrected in its own order.
* **Rows by their columns.** `rules._table_rows` splits a serialised price table into rows by
  key and by a column coming again; closing rows with no key were one row, and the tax's row
  took the total's name.
* **The penalty kind decides what supersedes.** `pipeline._superseded` lets the document-level
  penalty rates replace the chunk-level rates only; a fine stated as money is the chunk rule's.
  To keep that from reading a money cap as a second penalty, `_money_type` gives a penalty
  figure the rate's own cap and share tests.
* **The payment deadline's days.** `rules.payment_deadline_of` builds `P10D working` from the
  duration hit for the chunk path (`merger.rule_hits_to_facts`); the comparison's own payment
  values are unchanged.
* **The attested nominative.** `georgian.attested_nominatives` gives each written form of a person
  the nominative another form attests; the act check's signatory correction
  (`aoa._person_findings`) and the extraction's merge of a director's forms use it, and
  `in_case_of(..., written=...)` returns the contract's own form in a case the contract writes.
  Plain `lemma` is unchanged: project names go through it, and a place name such as `ქართლი` is
  a nominative in `-ლი`.
* **The price's own sentence first.** `document_facts._status_of` ranks tax wording by
  attachment before kind (`own sentence`, then the fee's scope, then anywhere); the kinds'
  precedence holds among wording of one attachment.

A test of round 1 (`test_rules.test_percentages`) read `ჯარიმა 0.1% დღეში` as a bare `0.1%`; that
is the behaviour COMPARE-R5-05 confirms as a defect, and the test now expects `0.1% per day`.

Changed outside the extraction's files, each the smallest change inside one function:
`contract_terms._EXEMPT` (two alternatives), `document_facts._CLAUSE_NUMBER`, `_status_of` and
`_vat_status`, `amounts.digits_and_words` and `DigitsAndWords.digits_raw` (a figure may follow its
words), `aoa._fee_like` and `aoa._person_findings` (one call each), and
`compare._with_own_words` (a figure's words after its currency).

Measured after the fixes: the three strict gold sets (`client_v2`, `ts2`, `ts3`) pass unchanged,
and `scripts/mutate_aoa.py --seed 173 --per-class 6` gives the base's table row for row: every
error class detected and corrected at 100%, every harmless class at 0% false positives.

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

### Red team, round 2: one reader for a value, and what a value leaves unsaid (2026-09-30)

The tests are in `tests/unit/test_compare_redteam2.py` and `test_compare_tables.py`, on invented
text.

* **Words beside a value are a change of their own.** Before, once a line's value change was
  reported, its text change was dropped. A penalty whose rate changed could therefore move onto
  the other party with no finding saying so. Now each side is read with its values cut out,
  including their words, currency, unit and postpositions. If a word changed outside them, and it
  is not one of the reported values' own words in whatever case the line writes it, the line is
  also "text changed". A line removed whole stays one finding per value; the round-1 "one change,
  one finding" still holds there. It says more only when a value on it went unreported because
  the other document restates it. The value finding is now anchored on the line it was read on.
* **One value reader for both passes.** A range of two dates is a period, whatever words stand
  near it. A month named as a period is read on the line as the whole-document pass reads it.
  A BIC and its `XXX` form are one code, as the act check reads them. An advance is `advance N%`.
  A line whose values are the same values written another way is formatting: a postposition fused
  into the month, the percent word, the lari abbreviated, tetri as a fraction.
* **Nothing is suppressed by a change that is not reported.** Between a contract and its act, no
  line's change is reported. Their positional diff still paired the party lines, though, and its
  "director X → Y" silently dropped the document-wide "X removed" and "Y added". So both parties'
  directors replaced in an act were listed as nothing. They are now listed as warnings. Pairing
  them by role (the customer's director against the customer's) is a policy question the spec
  does not answer yet.
* **Table rows that move.** A Word table row found elsewhere by its words is a move, paired cell
  by cell. Two rows swapped are one `CMP.MOVED`, not two warnings, and a price changed in the
  moved row is the price's change. Two kinds of row never pair as a move:
  * rows of nothing but numbers, which share a key (the "two limits" note above);
  * rows whose words another row repeats.
* **Quantity columns.** A figure under "რაოდენობა" beside its row's unit is read like the prose
  quantity "20 ცალი". A changed count or unit is `CMP.QUANTITY`, not a bare number or a text
  change.
* **Left as it is.** English party names, directors and addresses are still only "text changed"
  (COMPARE-R2-12). This follows the English-scope decision recorded under "An act not in
  Georgian".

### Red team, round 3: a value's words, wherever and however they are written (2026-10-01)

The tests are in `tests/unit/test_compare_redteam3.py`, on invented text, each with a near miss.

* **No mask stops inside a word, and a value's own words are masked with it.** The round-2
  masks left a penalty period's case ending behind ("per day" → "per week" was also a text
  change); they now end at a word's end. A figure's words in brackets after it, a rate's words
  before its figure (decimals included: `ნული მთელი ორი (0.2)`), and a company's legal form on
  either side of its quoted name are part of the value, so restating them is formatting.
* **A count of time written only in words is read as its figure** (`_with_figures`, one reader
  for the line values and the masks). A term restated so is no change, and changed so is the
  term's change. A count in words on both sides that changed is still reported once, as its
  words; a figure's words "removed" while the line still writes them are not reported.
* **A requisites table is read by its columns.** A cell of a table with two party names side by
  side is read under its column's party, and a short signature line under the party-name line
  above it, so a director replaced there is a party change. A total row's figure under a money
  column is the total amount.
* **A day range before one month and year is one period** on the compare side (`_range_end`),
  whatever the date reader makes of its first day.
* **A slipped letter is a spelling change.** A line whose one change is one letter in one word
  (four letters or more, no value, month or number word) is read as meant, on the line and in
  the document-wide pass: `გადასახადების` typed `გძდასახადების` no longer turned "VAT included"
  into "none". Found by `scripts/mutate_aoa.py`, not the red team.
* **Left open.** An address restyled (`N17` → `№17`, `ქ.` → `ქუჩა`) is still a party change
  (COMPARE-R3-10, a spec gap: `party_address` is compared as written), and a contract date
  moved still moves a validity that runs from signing, two findings (COMPARE-R3-11, by the
  round-1 decision). A validity end written with its year after the postposition (`30
  ნოემბრამდე, 2026 წ.`) is read by `checks/document_facts` (COMPARE-R3-02); the compare side
  keeps the date's "until" once the date reader reads it.

### Red team, round 4: what a value says, and what it only stands beside (2026-10-01)

The tests are in `tests/unit/test_compare_redteam4.py`, on invented text, each with a near miss.

* **A reader's reach is not its mask.** Round 3 masked a value with every word its reader read
  over, so words that changed inside the reach were never compared: a penalty "per overdue day"
  made "per overdue working day" or "per tenth overdue day" with no finding, and the costs a
  price covers before "და დღგ-ს" could change freely (COMPARE-R4-07, R4-10). The mask is now the
  words the canonical value encodes: `ყოველ` and the period's noun (`_per_period_words`), the
  tax phrase's verb and its tax (`_tax_wording`). The words between are the line's own, and a
  change in them is "text changed".
* **The VAT inclusion reads across a list of costs** (R4-09). `contract_terms._KA_REACH` lets the
  verb reach its tax over up to eight words joined by commas, `და` and `ასევე`, never past a
  turn (`, ხოლო`, `, მაგრამ`, `, თუმცა`, `, ოღონდ`), `გარდა`, a negation or a relative clause,
  and never to a tax the next words add on top (`... და დღგ ემატება`). One reader: the act check
  and the extraction read the same wording the same way. Adding a cost to the list was a
  critical "VAT removed". Known limit: a comma followed by a new subject and verb (`..., შემსყიდველი
  იხდის დღგ-ს`) is not told from a list, and still reads as the price including the tax.
* **A figure's words follow their figure** (R4-06). Words dropped from beside a figure, or added
  beside it, while both versions write the same figures, are formatting (`_words_restyled`);
  before, "10 (ათი) სამუშაო დღის" → "10 სამუშაო დღის" was a critical "Period in words removed"
  for a term both versions state. Words dropped with a figure that changed are still reported.
* **Restyles that are formatting**: a case ending hyphenated to a value (`5%-ის`, `30%-იან`,
  R4-11), a quantity row's unit cell written another way (`კვ.მ` / `მ2`, R4-12), a count of days
  that drops "calendar" or writes "ვადაში" (`20 კალენდარული დღის ვადაში` / `20-დღიან ვადაში`), a
  contract number retyped in lower case (R4-13, compared and reported upper case).
* **A fee per period is a price, labelled "Monthly fee" or "Unit price"** (R4-14, decided by the
  lead as policy). A service contract's monthly fee in a clause of its own was read as a second
  total: a fee raised was "Total amount" changed, and against another contract it was paired
  with that contract's total while the real total was "removed". Read in the figure's own
  clause (`_fee_per_period`), past the sentence's turns: the period's adjective before a word
  for the price, or "per month" / "თვეში" after the figure. Not the adverb ("გადაიხდება
  ყოველთვიურად", "paid monthly"), not the price of a number of months, not an advance.
* **Waiting on the readers.** "20-დღიან ვადაში", "20 კალენდარულ დღეში" (R4-02) and "5 (ხუთი)%-ის"
  (R4-03) are not read by the duration and percentage readers yet; that is fixed apart, in the
  rules. The comparison's side is in place and was checked against those readers widened in a
  scratch copy: every pair gives the expected result. The tests for those pairs skip until the
  reader reads the form, then hold the comparison to it.
* **Left as policy.** A fee per period changes the comparison's label only: the extraction still
  types a monthly fee in a clause of its own as `total_amount` (its strict gold is unchanged).

Measured after the fixes: the three strict gold sets unchanged at 100%/100%;
`scripts/mutate_aoa.py --seed 157 --per-class 6` compares every class at 100% (`cmp`), and its
two misses outside the comparison - `script_letter` 17% false positives (an extra `AOA.PERIOD`)
and `prose_typo` 83% corrections - are the same row for row at `4138994`, before these fixes.

### Red team, round 5: one entity, one reading, one finding (2026-10-01)

COMPARE-R5-01 to -04 and -12, each with a unit test on invented text and a near miss beside
every widened reading (`tests/unit/test_compare_redteam5.py`); the cases are in
`docs/GOLD_SPEC.md` section 4.

* **A fine per day is a value with its period** (R5-01). A rate's period was part of its value,
  an amount's was not, and the mask took the period's words as the value's: a fine of 50 lari
  per overdue day made per month was formatting, no finding. The comparison now states it
  `50.00 GEL per day` (`_amount_penalty_period`, the rules' `penalty_period` within the
  amount's own reach), and the mask keys the period's words by the period they name, so a
  period changed is never the same value written another way.
* **The comparison reads label lines as the extraction does** (R5-02). The extraction's round-4
  policy closed a "label: value" line for its own readers only; the comparison flattened the
  line breaks, and each term written one to a line took the next line's label: one changed
  delivery term was two errors, one of them a "Payment terms". `hits_of` now gives the values'
  readers the text with label lines closed (`rules.close_label_lines`), and the document-wide
  readers too. The anchors read the text as before, so a requisite on a label line keeps its
  value as written. So the round-4 note above that "the act check and the comparison flatten
  line breaks as before" no longer holds for the comparison.
* **A key-terms table is read by its rows** (R5-03). A Word table `პირობა | მნიშვნელობა` gave
  each value cell to the line pass alone, unclassified, and to the document-wide pass with the
  next row's label beside it: one changed penalty was "Percentage (unclassified)" and "Penalty".
  A value cell of a two-column table whose first cell is a term's name is now read as "label:
  value" on its line (`_row_labels`, `_Context.label`) and written so in the text the
  document-wide pass reads (`_reading_text`), as the chunker writes such a table for the
  extraction. The values each document states, which decide that a value "added" is written
  in the other document after all, are read from that text too; finding spans still index the
  document's own text. A table of one party per column is no key-terms table: its cells stay
  their column's party's.
* **Equal values in one sentence are counted** (R5-04). Values were one per type and value, so
  "40 tables and 40 chairs" was one quantity, and a chair count changed was a quantity added;
  between a contract and its act a shortfall was only a warning. A value now counts as many
  times as a reader finds it in one sentence (a line break ends one too); restated elsewhere it
  is still one, so a restatement deleted is still no value removed.
* **A value and its words come and go together** (R5-12, decided by the lead as policy). A clause
  added or deleted whole, or a draft's blank filled in, was the value and its words, two errors
  for one value; the words that spell their figure now go with it (`_without_own_words`). The
  round-4 rule that words dropped with their figure are still reported now means: reported
  through the figure.
* **The shared readers' side.** COMPARE-R5-05 (`დღეში` as a period), -07 (a blank read as an
  address), -08 (an advance clause's share and amount), -09 (one item's price) and -11 (a staged
  delivery's batches) are fixed in the readers, apart. Their whole pairs are tests that skip
  until the reader reads the form, then hold the comparison to the expected result; the R5-05
  pair was checked against the period reader widened in a scratch copy, and passes.
* **Left open.** The fine's period is the line comparison's: the document-wide pass reads a
  penalty as the extraction does, so between an unrelated pair an amount penalty is its amount.

Measured after the fixes: the three strict gold sets unchanged at 100%/100%;
`scripts/mutate_aoa.py --seed 191 --per-class 6` compares every class at 100% (`cmp`), its
table the same row for row as the code before these fixes (`prose_typo` corrections at 67%/50%
are the act check's, there before). The 68 red-team repro scripts of rounds 1-4 give the same
output before and after.

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

Measured 2026-10-01, after the fifth red-team round on the BOQ reader:

| harness | result |
|---|---|
| `scripts/evaluate_boq.py`, client set | 106/106 checks |
| `scripts/evaluate_boq.py`, test set 2 | 106/106 checks |
| `scripts/evaluate_boq.py`, test set 3 | 132/132 checks |
| `scripts/mutate_boq.py --seed 37 --per-class 3` | 50 valid mutants, 17 classes, 100% pass, 0 false positives, 0 control false positives |

The red team's round-2 probes were each workbooks laid out otherwise than the client's
template: a group header over rate and amount sub-columns, a quantity split per building, a
closing row not called SUB-TOTAL, a Summary lettered A, B, C or worded the Georgian way, a later
row that also says "TOTAL COST" or "incl. VAT", units in Cyrillic or Georgian, chart sheets, a
column inserted into the item-number block. The rules now in GOLD_SPEC 5 cover each; every probe
is a synthetic unit test in `tests/unit/test_boq_reader_variants.py`. On the three gold sets
the findings did not change, only the wording of the closing-row reconciliation. These are
fitting numbers in the same sense as above: the gold sets are this client's.

Policy questions left open:

* Profit on cost + overheads, usual in Georgian estimates, was a `BOQ.RECONCILE` error on every
  faithful bid that computes it so. Decided (rounds 4 and 5, then for a typed rate on
  2026-10-01): the profit rate, from its label or typed beside the total, holds on either base.

The rest follow the letter of GOLD_SPEC 5, and the gold writer agrees:

* A provisional sum the client fixes (amount typed, no rate) is unpriced for every bidder and
  makes every bid incomplete; the ranking is unaffected.
* A lump sum typed on a group row over its formula is booked as "Summary vs sheets", not as a
  priced non-position; renumbered positions after an inserted line are removed + added; an amount
  typed with every rate cell empty is unpriced.

### Red team, round 2 (BOQ-R2-01 to BOQ-R2-16)

The same harnesses after the fixes: `evaluate_boq.py` 102/102, 102/102 and 127/127;
`mutate_boq.py --seed 13 --per-class 3` 50 valid mutants, 17 classes, 100% pass, 0 false
positives, 0 control false positives (one `swap` mutant is set aside by the harness itself as
invalid, before and after these changes). On the gold sets nothing moved: none of their
workbooks has a number typed as text with three decimals, a second SUB-TOTAL on a sheet, or a
placeholder only its heading marks. Each defect is a synthetic test in
`tests/unit/test_boq_reader_round2.py`; the rules are in GOLD_SPEC 5.

* A number typed as text with one separator and three digits after it ("1.250") was read as
  thousands by the prose number parser: 1.25 t of rebar priced as 1,250 t, and the colours
  flipped. Such text is now read the way its own row computes it (quantity x rate = amount),
  and stays text where the row cannot tell (an unpriced template row). A template quantity
  left so is evaluated for no bidder rather than guessed; the gold writer does the same.
* A heading that invites the bidder's own items ("Undescribed items (Bidder to list and
  price)") now makes the lines under it placeholders, up to the group's Sub-total. The
  invitation's words count only where they address the bidder or open the text: "texts to be
  listed by the Engineer" and "removal of undescribed obstructions" are described work.
* A quantity from a take-off sheet or added up from numbers is the position's own; component
  column groups (Materials: Rate | Amount) bind the component's rate and sum; "Rate in GEL" and
  "ღირებულება ლარში" are the rate and amount columns; Summary rows worded "GRAND TOTAL WITH
  VAT", "Value added tax @ 18%", "Total cost, excl. VAT" play their parts; units such as
  t/ტნ, day/days, set/კომპლ. fold; sections closed by their own sub-totals under a grand total
  reconcile against the grand total. A priced sheet whose price columns are not recognised
  (`BOQ.PRICES_NOT_READ`) and a bid left unranked for want of a Summary row (`BOQ.NOT_RANKED`)
  now say so.

The gold writer does not yet choose a sheet's closing row by role: it reads up to the last
SUB-TOTAL row, which it also uses to bound the rows it reads. On a sheet with section
sub-totals under a grand total its `sheet_subtotals` is the last section's; no gold set has such
a sheet.

Policy questions round 2 left open, with the recommended answer:

* **Discount rows on the Summary (BOQ-R2-12).** A "Discount 5%" row and a "TOTAL ... AFTER
  DISCOUNT" row after the total incl. VAT play no part today, so the pre-discount total is
  ranked, silently. Recommended: a discount row (discount, rebate, ფასდაკლება) is part of the
  chain, and a restated total "after discount" replaces the part it restates - the discounted
  incl.-VAT total sets the markup, and VAT is held against the discounted excl.-VAT total. At
  the least, a discount below the total incl. VAT is a warning naming both totals.
  **The minimum done 2026-10-01** (`BOQ.DISCOUNT`, warning, `tests/unit/test_boq_discount.py`):
  the bid is still ranked before the discount, and says so; which total it offers stays the
  client's call. A discount inside the chain was already part of it.
* **A negative rate on a position (BOQ-R2-13).** It counts as a price today (non-zero r) and
  lowers the common-scope total with no finding. Recommended: a negative rate or amount on a
  template position is not a price for the work - `BOQ.NEGATIVE_RATE` (warning), the position
  out of the common scope, still shown in the full evaluated total and the breakdown.
  **Done 2026-10-01** (`tests/unit/test_boq_negative_rate.py`), one step stricter: the position
  (status `negative`) is left out of the full evaluated total too, as text in a number cell
  is, so a negative rate can lower no total, and the bid is incomplete. A negative amount on a
  line the template leaves blank is not covered.
* **A partly priced section group (BOQ-R2-14, by design).** Pricing one item of a group prices
  the group, so its other items cost nothing in the common scope. Recommended if revisited: the
  section row and its items are alternatives, the items among themselves are not - the group
  is priced when its section row is priced or every item is; otherwise the unpriced items are
  listed and the group leaves the common scope.
* **A sheet tab renamed with a trailing space (BOQ-R2-15).** It is a removed and an added sheet
  today, and every bidder's common scope loses its positions. Recommended: match sheets by exact
  name, else by folded name (trimmed, whitespace collapsed, case) where the match is one to
  one, reported as `BOQ.SHEET_RENAMED` (info).
* **Sections closed by their own totals with no grand total (BOQ-R2-16).** The last section's
  total is the closing row by the letter of GOLD_SPEC 5, so a faithful bid gets two
  `BOQ.RECONCILE` errors. Recommended: where a sheet has several top-level sum rows (none summed
  by a later row) and no single closing total, the sheet's total is their sum, and a Summary row
  that adds several sheet rows is held against their sum.

### Red team, round 3 (BOQ-R3-01 to BOQ-R3-12)

The same harnesses after the fixes: `evaluate_boq.py` 106/106, 106/106 and 132/132 - four, four
and five checks more than before, because `unit_filled` is now scored against the gold (the
gold writer always recorded it; the module had no such list). `mutate_boq.py --seed 19
--per-class 3`: 50 valid mutants, 17 classes, 100% pass, 0 false positives, 0 control false
positives - the same table as before the fixes (one `swap` mutant set aside as invalid by the
harness itself, before and after). On the gold sets the only change in any output is the new
`BOQ.UNIT_FILLED` note on the bidders who wrote "Item" against the preliminaries' position 6.2,
whose unit the template leaves blank: no column map, helper or unreadable sheet, total or colour
moved. Each defect is a synthetic test in `tests/unit/test_boq_reader_round3.py`; the rules are
in GOLD_SPEC 5.

* **Cost components come from one list.** A machinery group (მანქანა-მექანიზმები, Plant,
  Equipment) over Rate | Amount had its Rate bound as the unit rate: every line priced at the
  machines' share alone, the colours flipped. Machinery is now a component beside materials,
  labour and delivery, and every component key list is built from one tuple - a component named
  in one list and not another was the defect. A label the reader does not know over Rate |
  Amount beside a known component group is a component too, and never binds the rate or the
  amount. A price column holding numbers that the reader did not recognise
  (`BOQ.COLUMNS_NOT_READ`, info) stops the rate being held against the components it did read
  - which was "total rate does not match" on every faithful line - and a line without a rate is
  then priced at its amount over its quantity.
* **A header pushed down by a bid-form block.** The 14-row window bounded both where the header
  starts and how far it runs, so eight rows of bidder details cut it at its second row: the
  prices were not read and the bidder was told every position was left unpriced. The window
  (40 rows, as the gold writer's) now bounds only the start; the block and its column-number
  row are followed where they fall, on the Summary too, whose TOTAL column is looked for in its
  header's own rows only. Positions on a sheet whose prices are not read are `prices_not_read`,
  never unpriced.
* **Number text reads one way or not at all.** "1450.00." was read as 145,000 by stripping every
  separator; a strict number grammar now decides, and anything else stays text
  (`BOQ.NON_NUMERIC`). The grammar is in `boq._num`, not in the shared `parse_number`, so the
  other modules are unchanged.
* Units: plurals and full names (metres, kgs, nos, ltrs, kms, square/cubic/running metre, ცალ.,
  ტონ., მ.გრძ.) fold with their measure. The Summary: "Total VAT" is the VAT row and
  "VAT-inclusive total" the total incl. VAT. The line amount headed "Cost", "Sum", "თანხა",
  "სულ ღირებულება" is read, and a sheet whose amount is not says so (`BOQ.AMOUNTS_NOT_READ`).
  Money is rounded to the cent half up in one place (`boq.cents`), so bidders tied to the cent
  share a colour in the module, the merged workbook and the API alike. A unit written where the
  template left none is listed (`unit_filled`, `BOQ.UNIT_FILLED`), never a unit change.

Policy questions round 3 left open, with the recommended answer:

* **A bid priced in another currency (BOQ-R3-03).** Since R2-10 a rate or amount column is read
  whatever currency it names, so a bid headed "Rate in GEL" against a template in USD is ranked
  as if in dollars, silently. Recommended: keep the currency each rate and amount header names
  (the part the label reader strips), folded to one code (ლარი/GEL/₾ -> GEL; აშშ დოლარი/USD/$
  -> USD; ევრო/EUR/€ -> EUR), a header naming none taking the template's. A bid whose currency
  differs from the template's is `BOQ.CURRENCY` (warning, naming both) and is left out of the
  colours unless a rate for the day is supplied through the existing `fx` rate provider.
  **Done 2026-10-01** (`boq._header_currencies`, `tests/unit/test_boq_currency.py`): the
  warning, and the bid left out of the colours and the ranking with its reason; converting at
  a rate for the day is not built, so such a bid is compared by hand.
* **TOTAL COST by its usual names (BOQ-R3-12).** "Total construction cost" and "Total direct
  cost" are not read as TOTAL COST today, by the letter of GOLD_SPEC 5, and the bid is loudly
  unranked. Recommended: TOTAL COST goes by its usual names as overheads and profit do - "total"
  then at most one qualifier among construction / direct / works / civil works / project, then
  "cost(s)", word-bounded so "Indirect costs" (an overheads name) never plays it, plus
  "პირდაპირი ხარჯები სულ" and "სამშენებლო სამუშაოების ღირებულება" - with the role order kept
  (the last such row before the chain goes on). **Done 2026-10-01** as recommended
  (`boq._TOTAL_COST`, `tests/unit/test_boq_total_cost_names.py`).

### Red team, round 4 (BOQ-R4-01 to BOQ-R4-19)

The same harnesses after the fixes: `evaluate_boq.py` 106/106, 106/106 and 132/132;
`mutate_boq.py --seed 29 --per-class 3`: 51 valid mutants, 17 classes, 100% pass, 0 false
positives, 0 control false positives. On the gold sets no column map, position, total or colour
moved, and the 49 repro scripts of rounds 1-3 print what they printed before. Each defect is a synthetic test in `tests/unit/test_boq_reader_round4.py`, the gold
writer's side of the lead's policies in `tests/unit/test_boq_reference_round4.py`; the rules
are in GOLD_SPEC 5.

* **One header reader for the bill and the Summary (R4-01, 02, 08).** A label naming its column
  in English and Georgian side by side ("Description / აღწერა", or on two lines) made the
  template sheet unreadable and nothing was evaluated; the parts are now read and must agree.
  The description and quantity are read by rule (Description of works / goods, Name of works,
  Quantity of work, რაოდ., რ-ბა), the Georgian total-price names (ჯამური / საერთო / სულ ფასი)
  are the line amount, and a bare "Price" is bound by its place - the unit rate before a line
  amount, the amount where none follows - so an amount slip under "Qty | Price | Amount" is
  reported instead of priced in. The Summary used a separate, shorter vocabulary: "Description
  of works", "Cost (GEL)", "Price", "Sum" left every bidder unranked under "no row states the
  total incl. VAT"; it now reads with the bill's names, and an unrecognised Summary header says
  so.
* **The rows that close a sheet (R4-04, 05, 07).** A contingency or VAT row computed from the
  sub-total (`=F7*E8`, no quantity or unit) was read as a position - the tax in the cost and a
  false gap on every faithful bid; a product over another row is the position's slip only on a
  row with a measure. A single SUB-TOTAL that positions follow is a section's, not the sheet's
  closing total. A closing total labelled across the item column or in the Unit or Rate column
  is read by its role, so a SUM range one row short is reported wherever the label sits.
* **The Summary chain (R4-03, 06, 11).** A section row summing a range (`=SUM('1. WORKS'!F2:F6)`)
  was held against the range's first row; it is held against the range's positions, and a
  range scaled or mixed is not checked. "V.A.T." and "დ.ღ.გ." are the tax. A chain row's rate
  is the one its label states or the one typed beside it; a "% of total" share column is no
  rate. A profit rate, from the label or typed beside it, holds on TOTAL COST or on TOTAL COST +
  overheads.
* Units: "100 m2" is "100 მ2" (a multiplier kept, the measure folded; R4-09). An "Other(s)"
  position "to be specified by the Engineer" is described work, not a placeholder (R4-10). A
  sheet priced by its component rates alone is priced, and the merged sheets show every
  component (machinery too) and the rate the evaluation used (R4-12); their columns are looked
  up by key (`boq.MERGED_COLUMNS`), the machinery column shifting the later ones by one.

The lead's policies of round 4, each in GOLD_SPEC 5 and the gold writer:

* **The offered make or model after the template's description (R4-13).** Same item number,
  the template's text kept whole and the bidder's words after it from a word's end: the
  position, `BOQ.DESCRIPTION_CHANGED` at info. "Pipe 20" against "Pipe 200 mm" stays two
  positions. The 70-character truncation of the matching key is left as it is (it feeds the
  sequence alignment and the scorer's keys); a long description re-worded past its 70th
  character still matches without the 0.9 similarity check, which is the length dependence the
  verifier named.
* **A row with its own rate and amount (R4-14)** is a position whatever its quantity formula
  adds; a quantity adding other rows makes a parent only on a row with no amount of its own.
  The round-2 tests of parents were reworded to rows without an amount, and assert the policy
  for rows with one.
* **A single-sheet schedule with no Summary (R4-16)** is ranked on its Total, VAT and Total incl.
  VAT rows, a contingency between the total and the VAT taxed with it - only where the template
  has no Summary either: a bid dropping the template's Summary is not ranked. Without such rows the
  skip note says the workbook has no Summary sheet; no per-bidder finding is added, as fifteen
  earlier tests of Summary-less bills hold that such bids carry no findings.
* **LS, L.S., lump sum and lump-sum are one unit (R4-18)**, apart from item, set, pcs and the
  litre.
* **An item number stored as a number (R4-19)**, "1.10" read back as 1.1, is the position when
  the description is the template's word for word, nearest row first; typed as the text "1.1"
  it remains a renumbering.

Spec gaps round 4 left open, with the recommended answer:

* **"Allow for any other works necessary to complete ... as per drawings" (BOQ-R4-15).** GOLD_SPEC
  5 lists "any other item(s), work(s) or cost(s)" in any wording, and also says the same words
  further into a described position are no invitation. Recommended: "any other item / work /
  cost" invites the bidder only where it opens the description (after an optional "Any" or
  "Provide") or where the bidder is addressed (list, specify, by the bidder / tenderer); after
  "Allow for", "Include for", or with a described scope ("necessary to complete ... as per
  drawings / specification") it is a described sweep-up position and is evaluated.
* **A coefficient cell in every amount formula (BOQ-R4-17,** `=D2*E2*$H$1`). Read by the letter,
  every position is a subtotal, nothing is evaluated and every bidder gets a false RECONCILE.
  Recommended: a product of the row's own quantity and rate is the position's amount whatever
  constant factors multiply it - plain numbers or an absolute reference to one cell outside the
  bill's lines; and, separately, a priced template sheet that ends with no position while its
  rows carry quantities says so once on the template (`BOQ.PRICES_NOT_READ` or a new
  `BOQ.NO_POSITIONS`), with the per-bidder RECONCILE on that sheet suppressed.

### Red team, round 5 (BOQ-R5-01 to BOQ-R5-18)

The same harnesses after the fixes: `evaluate_boq.py` 106/106, 106/106 and 132/132;
`mutate_boq.py --seed 37 --per-class 3`: 50 valid mutants, 17 classes, 100% pass, 0 false
positives, 0 control false positives (one `removed` mutant set aside by the harness itself as
invalid, before and after these changes). On the gold sets no column map, line kind,
position status, group, finding, total or colour moved (a snapshot of everything the module reads
and decides on the gold workbooks, before and after, is identical), the gold writer rewrites
the client set and test set 2 identically (test set 3's gold is derived from its generator, not
written by it), and the 66 repro scripts of rounds 1-4 print, byte for byte, what they printed
before. Each defect is a synthetic test in `tests/unit/test_boq_reader_round5.py`, the gold
writer's side in `tests/unit/test_boq_reference_round5.py`; the rules are in GOLD_SPEC 5.

* **A price needs a price (R5-01, 06, 07).** On a line the template leaves blank, a quantity the
  bidder filled in with no rate or amount was "filled_blank" at 0 and in the common scope, so the
  bidder who priced it paid for it and the one who did not paid nothing: the colours flipped. Such
  a line, and one with text in its rate or amount, is still listed as filled in but costs 0,
  is unpriced and leaves the common scope. A template quantity typed as any text ("2.4 t"), not
  only the two-way "1.250", is evaluated for no bidder - it was read as a blank to fill in, at
  each bidder's own figure.
* **A heading ends a section group (R5-02).** "A2" and the items "2.1"-"2.3" under a heading
  "2. EARTHWORKS" were one group, so an unpriced 2.3 was hidden and stayed in the common scope.
  A row that is no position now ends the group unless its own number is under the section.
* **The header and the Summary in their other usual spellings (R5-03, 04, 05, 12).** "რაოდ-ბა"
  and a word hyphenated over two lines made the template unreadable; "ერთ.ფასი" and
  "ერთ. ღირებულება" were not the rate, so an amount slip was priced in; "საერთო თანხა" and
  "ღირებულება სულ" were not the amount. The quantity, the Georgian rate and the Georgian
  amount are read by rule (stem and ending, unit word + price word, money word + total word).
  "ჯამი დღგ-ით" and the other Georgian "with VAT" forms left every bidder unranked, or played
  the VAT row; they are the total incl. VAT by grammar (the instrumental), and with no VAT row
  named the total incl. VAT is held against its base plus the rows between, as the total excl.
  VAT already was.
* **Item numbers, Roman chapters, a rate left of TOTAL, the excluded block (R5-08 to 11).** "1."
  is "1": a bid writing every number with a dot was five removed and five added positions and
  nobody was ranked. Summary chapters I, II, III are top-level sections (TOTAL COST against "I"
  alone was an error on every faithful bid). A rate typed in a "%" column left of TOTAL is
  read as one typed right of it. The merged workbook's excluded block now lists a priced
  placeholder; its statuses come from the analysis texts, so no excluded status can be missed.

The lead's policies of round 5, each in GOLD_SPEC 5 and the gold writer:

* **A price list closed by its total incl. VAT (R5-13)** - the prices including the tax, one
  closing row plainly adding them - is ranked on that row as both TOTAL COST and the total incl.
  VAT (markup 1.0). A total incl. VAT scaled onto a pre-VAT sum, or followed by other valued
  rows, is not read so, and such a bid stays unranked as before; so does a bid closing on its
  total incl. VAT alone where the template's bill goes Total, VAT, Total incl. VAT, which at
  markup 1.0 would rank its pre-VAT prices 18% low among bidders marked up 1.18.
* **A contingency between the bill's Total and its carried total (R5-16)** is part of the
  chain: the positions are held against the first sum row whatever its label, the rows after it
  being rates of it and the carried total built from those. The rows are told by how they are
  built, not by the first one's range, so a range one row short is still reported.
* **A hidden template sheet the Summary does not link (R5-17)** is no section: one info note on
  the template (`BOQ.SHEET_SKIPPED`), no SECTION_UNPRICED on every bidder; a bidder who prices it
  is shown like a priced non-position. The client templates' hidden calculation sheet, which
  has no bill on it, stays a helper sheet. The rule is the template's: a bidder who hides one of
  the template's sections (their Summary typed as values, linking nothing) still returns it.
* **The usual Georgian unit spellings are one unit (R5-18):** the man-hour კაც/სთ = კაც.სთ =
  კაც-სთ (and the man-day and machine-hour likewise, each apart from the hour), შტ = ც = ცალი,
  კვტ = kW (apart from kWh), მთვ = month, გრ.მ = m.

Left as they are:

* **Price headers with a VAT note and no currency (R5-14, by design).** "ერთეულის ფასი დღგ-ს
  ჩათვლით" and "Unit price (excl. VAT)" are not read, as R4 decided for a label that runs on
  after what it names; the failure is loud (`BOQ.PRICES_NOT_READ` on the template), never a
  silent wrong ranking.
* **Resource-based estimates (R5-15, spec gap).** In the Georgian resource-based estimate each
  work row (number, unit, quantity, no amount) is the parent of the item-less labour, machine
  and material rows priced under it; by the letter of GOLD_SPEC 5 ("unless they carry a quantity
  or a unit") the work row is a position, so every faithful bid is "incomplete" with
  `BOQ.POSITIONS_UNPRICED` on every work row (the totals and colours are right). Recommended: a
  numbered row with a quantity and/or unit but no amount cell, directly followed by one or more
  item-less rows each with an amount of its own row, up to the next numbered row or sum row, is
  the parent of those rows, not a position; its completeness is that of its children. The
  client's lone position with a missing amount formula has no such rows under it and stays a
  position.

## Known gaps

* The scanned PDFs are not yet in the gold set; OCR only runs inside the Docker image, so they
  must be scored there.
* `AOA.FX_INVOICE` (the invoice in lari for a foreign-currency act) works the invoice out — the
  advance at the NBG rate of its payment day, the rest at the rate of the act's date — from the
  NBG rates imported (`load-fx-rates`, a file `scripts/fetch_nbg_rates.py` writes on a connected
  machine) or typed in on the report's form; a day with neither is named in an `AOA.FX_RATE`
  skip, never priced at another day's rate. Open with the client: their no-advance example writes 2,610.04 for 1,000 x 2.6104, which
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
* The registry rules run against the parties' uploaded extracts and the snapshot the client
  exports (`cli load-registry`); a party with neither is reported as skipped, never as passed.
  The extract reader was built from two public extracts (2011 and 2025 layouts); an extract of a
  third layout would fail loudly ("not an extract", or fields left out), not quietly.
* Docling promotes the first row of a table with no header row to the column header. A
  two-column table whose first row is data - a digit in either cell, or a label alone (a
  title) - is written row by row as `label: value;` (red team R3). A first row of two cells
  with no digit (`მიმწოდებელი | შპს „…“`) cannot be told from a real header (`პირობა |
  მნიშვნელობა`) and is still written as one, its value repeated into every row as a column
  name; a table of three or more columns with no header row still loses its first row's first
  cell; the segmenter (document comparison) is unchanged.
* A prose typo inside a service line or the subject clause also leaves a scope review note
  (see "Prose spelling"); the scope rules should compare the corrected word.
