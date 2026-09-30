# Gold specification: what "correct" means, per module

Every accuracy number in this project is a comparison between what a module returned and a
gold file. This document fixes the form both sides are compared in, so a number can only move
because an answer changed, never because a label was spelled differently. Write gold against
this document; change this document before changing a scorer.

Three rules apply everywhere:

1. **Derive when you can, label when you must.** A value that follows from something exact - a
   correct-vs-mistake diff, the contract's own text, a generator's substitution table - is
   derived and is exact. A value a person (or agent) read off the page is *labelled*, and every
   labelled type is listed under `provisional:` in its gold file until the user confirms it.
2. **Gold states what the document says, never what the extractor says.** A gold value is
   never copied from tool output.
3. **Client gold never enters git.** Anything describing the client's documents lives under
   `samples/` (git-ignored). Only synthetic gold is committed (`tests/fixtures/gold/`).

## 1. Folding applied before any comparison

`fold(s)`: Unicode NFC; every quote character (`„ “ ” " ' ‘ ’ « » ‚`) removed; every dash
(`– — ‑ −`) turned into `-`; runs of whitespace (incl. NBSP) turned into one space; trimmed; Latin
letters upper-cased. Nothing else - no stemming, no substring match, no case-ending tolerance.

## 2. Canonical form per fact type (extraction)

| type | canonical form | example |
|---|---|---|
| `party_name` | legal form + name, no quotes; the legal form **abbreviated** however the document writes it (`შეზღუდული პასუხისმგებლობის საზოგადოება` is `შპს`, `სააქციო საზოგადოება` is `სს`) | `შპს ნიმუში დეველოპმენტ` |
| `party_director` | full name, **nominative**, without a courtesy title (`ბატონი`, `ქალბატონ`, `ბ-ნ`); a name written with an initial keeps it (`გ. ნიმუშაძე`); the act check compares it with the contract's director whose first name starts with that letter, on the surname | `გიორგი ნიმუშაძე` (not `…ნიმუშაძის`) |
| `party_address` | as written, no quotes, no trailing punctuation; it ends where another requisite (code, account, bank) begins | `ქ. თბილისი, ნიმუშის ქ. N5` |
| `party_id_code` | digits only (9 or 11); a code printed in groups (`123 456 789`) is the same code | `123456789` |
| `bank_account` | IBAN, no spaces, upper case; printed in groups or broken over a line it is read whole, to its country's fixed length (GE: 22), whether or not its checksum holds | `GE00NB0000000000000000` |
| `bank_code` | BIC/SWIFT, upper case | `NBGEGE22` |
| `cadastral_code` | dotted, as written | `01.02.03.456` |
| `project_name` | **nominative**, no quotes, no postposition; also read behind a label (`პროექტის დასახელება: „…“`) and before `პროექტი` with the case ending after the quote (`„…“-ს პროექტი`) | `ზღვის ხედი` (not `ზღვის ხედისთვის`) |
| `contract_date` | ISO date of the *contract's* signing | `2026-08-01` |
| `act_date` | ISO date of the act's *own* signing | `2026-08-31` |
| `execution_period` | the period the work is performed in - an act's service period, a contract's performance window ("შეასრულოს მომსახურება … -დან … ჩათვლით"): ISO range `start/end` (inclusive), or ISO duration when only a length is stated | `2026-08-01/2026-08-31`, `P6M` |
| `validity_period` | the contract's own validity ("ძალაში შედის … და მოქმედებს …", usually under "ხელშეკრულების მოქმედების ვადა"): ISO range, `..` for an open end ("until the obligations are fulfilled"), or ISO duration | `2026-08-01/..`, `2026-08-01/2027-02-01` |
| `delivery_deadline` | ISO date, or ISO duration | `2026-04-30`, `P30D` |
| `warranty_period` | ISO duration | `P24M` |
| `payment_terms` | ISO duration + ` working`/` calendar`, plus `advance N%` when stated | `P5D working` |
| `vat_status` | one of `incl`, `excl`, `none`, `net` | `incl` for "გადასახადების ჩათვლით" |
| `total_amount`, `price` | number, `.` decimal, no separators (compared numerically) | `2400` |
| `penalty` | number with `%` (per period after it if stated: `0.5% per day`) or an amount | `0.5% per day` |
| `quantity` | number + unit | `4 item` |
| `contract_number` | as written, upper case, no spaces | `PR-2026/017` |
| `subject` | the subject clause as written (see scoring) | |

Dates are read as Georgian documents write them: a day in quotes (`„30“ ივნისი, 2026 წ.`,
both quotes required), an ordinal first day (`2026 წლის 1-ლი მაისი`), and a two-digit year
only where the word for year follows it (`12.05.26 წ.` is 2026-05-12). The word for year is
part of the date and is read whole (`5 ივლისს, 2026 წელს`, never `… 2026 წ`). A day range before
one month and year (`1-15 ივნისი, 2026 წ.`, `01-15.06.2026`) is one period, first day to last. An
unlabelled date is the document's own (`contract_date`, or `act_date` for an act) only when it
stands in the preamble, before the first party or the first numbered clause; a date in a clause
is not, and with neither boundary no date is promoted. Parties the title names
(`ხელშეკრულება N 7 შპს „…“-სა და შპს „…“-ს შორის`) are part of the title: the preamble ends at
the party block. A contract's party may be a person or an individual
entrepreneur named without quotes (`ფ/პ …`, `ი/მ …`). An act's service period is read by its
form whatever the verb (`შესრულდა`, `შესრულებულია`, `გაწეულ იქნა`, `განხორციელდა`, `ჩატარდა`,
`… პერიოდი: 01.06.2026 - 30.06.2026`): one range, never its two dates apart; a period stated
as a month (`2026 წლის ივნისის თვეში`) is that month's first to last day. A performance window
whose first day is written but cannot be read starts open (`../2026-09-30`), never at the
signing. A contract's performance window is set by a performance verb, also in the future
passive (`შესრულდება`, `განხორციელდება`, `ჩატარდება`, `დაიწყება … დასრულდება`) where the
sentence names the work and is not the validity clause; a numeric date's end is written with
its case too (`31.12.2026-ის ჩათვლით`, `31.12.2026 წლის ჩათვლით`). An act is an act when its
title stands near the top or on a line of its own below a letterhead, before the first clause.

How the rules read what the canonical forms need (red-team round 1):

* **Money.** The lari is read in every case (`ლარი`, `ლარის`, `ლარს`, `ლარით`, `ლარად`), as
  `ქართული ლარი`, as English `Lari`, and abbreviated `ლ.`. `ლ.` is also a litre, so an amount in
  it is money only where a word about money types it (`ღირებულება`, `თანხა`, …); "20 ლ." alone
  is no amount. Tetri written in figures after a lari amount are its fraction:
  `4 800 ლარი და 50 თეთრი` is `4800.5`. In a price table, a cell under a total column (`ჯამი`)
  in a row that has a unit price or quantity is a line total, not `total_amount`; the document's
  total is the row a total word names, or the figure in the prose.
* **Percentages and penalties.** A rate written with the word (`0.3 პროცენტის`, `0.2 percent`)
  is read as `0.3%`. A penalty's period is read after or before its rate, within its sentence,
  in days, weeks, months or years (`ყოველ ვადაგადაცილებულ კვირაზე` -> `0.3% per week`); the
  period is part of the value, so a period changed alone is a penalty change. A rate beside
  `დღგ` (`+ 18% დღგ`) is never a `vat_status`: that type holds only the four values above.
* **Durations.** A count written in words before its figure (`ათი (10) სამუშაო დღე`,
  `ten (10) banking days`, `24 (twenty-four) months`) is the same duration as `10 (ათი)`.
* **Names.** A quoted project name in apposition after an inflected `პროექტის`/`პროექტი` is a
  nominative unless its ending can only be an oblique case (`-ისთვის`, `-ის` after a consonant);
  a loanword in `-უს`/`-უმ`/`-უდ` (`პრემიუმ`, `ფორუმ`) is a nominative. Two forms of one
  project whose nominative is ambiguous between `-ა` and `-ი` (`ტერასისთვის` / `ტერასა`) are one
  name; when neither document writes the nominative, the reported form is a guess (`-ი`).
* **Contract numbers.** A slash with a space either side is part of the reference
  (`N 15 / 2026` -> `15/2026`); a bare space ends it.

`vat_status` meanings: `incl` = the price includes VAT/taxes; `excl` = VAT is added on top;
`none` = no VAT applies / exempt; `net` = a net amount, taxes withheld or not mentioned by rule
(natural person, foreign organisation).

**Strict scoring** (`scripts/evaluate.py --strict`): for every document every target type is
scored. `fields:` lists the expected canonical values; a target type not listed is expected to
be **absent**, so any fact of that type is a false positive. Generic leftovers (`amount`,
`date`, `percentage`, `duration`, `id_code`, `other`) are not scored but are counted and
reported as "unclassified". A type the gold cannot yet state canonically goes under `ignore:`
with a reason in `notes:`. A fact matches when `fold(canonical(fact)) == fold(expected)`; money
and percentages compare numerically (0.005 tolerance); `subject` matches when, after folding and
per-word stemming, at least 80% of the gold's words are in the fact and at least 50% of the
fact's words are in the gold.

## 3. AOA gold (per act)

```yaml
name: ts2_09_director
document: samples/<set>/AOA and Contract/<act>.docx
reference: samples/<set>/AOA and Contract/<contract>.docx
check: aoa
contractor_type: company          # optional: company | natural_person |
                                  # individual_entrepreneur_vat | small_business | foreign
fields: {...}                     # extraction gold of the act, section 2
provisional: [subject]            # labelled, not derived
findings:                         # every finding that must be reported
  - rule: AOA.DIRECTOR
    severity: error
    text: ნიკოლოზ ბერიშვილი          # the offending words exactly as the act writes them
    correction: ნიკოლოზ ბერიძე       # the correct variant, in the act's own grammatical form
    occurrences: 4                  # places in the act a Word comment must mark
allowed_findings: [AOA.IBAN_CHECKSUM]   # may appear without being expected (info only)
strict_findings: true             # anything else reported (any severity) is a false positive
```

* A reported finding **matches** an entry when the rule id is equal, `fold(text)` occurs in
  `fold(quote or actual)`, and `fold(expected) == fold(correction)` when `correction` is given.
* `correction` is written in the act's form: a name in the case the act uses at that place, a
  date in the act's date style (`2026 წლის 31 აგვისტო`), an amount as the act writes amounts
  (`100 (ასი) ლარს`). A date's style includes its month's case and its day's form: an act
  signed "2026 წლის 31 აგვისტოს" (the dative; the vowel stem's dative and genitive are one
  word, and a postposition or a noun after the date makes it the genitive) is corrected to
  `2026 წლის 30 სექტემბერს`, a quoted or zero-padded day keeps its quotes or its zero
  (`„30“ ივნისი, 2026 წ.`), and its year word and year digits (`30 ივნისს, 2026 წელს`,
  `30.06.26 წ.`); an English date stays English (`30 June 2026`). A correction is ISO only
  where the act wrote ISO.
* `occurrences` is checked by annotating the act (`report/docx_annotate.py`) and counting the
  comments whose anchored text contains `text`. A place is where `text` stands as whole words:
  `ივნის` is not a place inside `ივნისიდან`, nor a name's nominative inside its genitive. A name
  copied from another party (the customer's director written for the contractor) is marked only
  in the places of the party it is wrong for; where it stands in its own party's block it is right.
* Which rule owns which mistake:
  * wrong digits and/or words of the fee → `AOA.FEE` (+ `AOA.AMOUNT_WORDS` when digits and words
    disagree); words misspelled (`ასოი`) → `AOA.AMOUNT_WORDS`, an error, with the correct words
    (words that read as no number and as no misspelling of the figure are a warning: the reader
    may be at fault);
  * a month name, a Latin/Cyrillic letter inside a Georgian word, or a prose typo →
    `AOA.SPELLING` with the corrected word, a warning. A prose typo whose correction neither
    the act nor the contract writes rests on the dictionary alone, which lacks words and forms:
    it is `info` with `needs_review` ("may be misspelled - check it");
  * the service period's year outside the contract term → `AOA.PERIOD_IN_TERM` (text = the
    period as written, correction = with the in-term year); `AOA.ACT_DATE` only when the act's
    own date is the odd one out;
  * a missing project name / cadastral code → `AOA.PROJECT_PRESENT` / `AOA.CADASTRAL_PRESENT`;
  * a bank code or plot code not the contract's → one `AOA.BANK` / `AOA.CADASTRAL` per value,
    correction = the contract value it is a slip or two from, the contractor's own on a tie;
    a value near none is corrected to the contractor's own, or to the only contract value; no
    correction when that still leaves a choice. A BIC and its `XXX` branch form are one code;
  * an act not written in Georgian: what the rules read off Georgian wording (the parties'
    codes, the project, the period) is a skip note saying so, never a finding.
* The legacy `expected_findings:` / `forbidden_findings:` (rule ids only, `"*"` = must be
  completely clean) keep working and are checked as well.

## 4. Compare gold (per pair)

```yaml
name: ts2_01_vs_05
document: <right / newer>.docx
reference: <left / older>.docx
check: compare
findings:
  - rule: CMP.BANK
    label: Bank code              # the label the finding must carry (English)
    before: TBCBGE22
    after: BAGAGE22
    severity: error
strict_findings: true             # any other substantive finding is a false positive
allowed_findings: [CMP.FORMATTING]
```

A value both documents carry must never be reported "added" or "removed"; one entity change is
one finding. An amount's words misspelled ("(ასი)" -> "(ასოი)") are `CMP.AMOUNT_WORDS`, an error,
as the act check grades the same slip; a count's or a period's words misspelled are
`CMP.SPELLING`, a warning. Two dates written as a range are one period (`CMP.DURATION`, label
"Execution period", or "Validity period" when validity words precede it), its ends as written -
a year typed wrong in one end gives `2026-05-01/2025-05-31`, never the ends sorted - and a
period whose year is written once ("2026 წლის 1-დან 31 მაისის ჩათვლით") is the same period.
A document's own date changed where no word names it (the heading's "ქ. თბილისი, 2026 წლის
12 თებერვალი") is `CMP.DATE` labelled "Contract date" ("Act date" for an act), as on a labelled line.


`before`/`after` are the section 2 canonical forms: a payment term `P7D working` (its days
counted are part of it: `P7D working` -> `P7D calendar` is a `CMP.PAYMENT` change, whatever verb
the sentence pays with), a warranty `P12M`, a delivery deadline `P30D`, a quantity `4 item`
(`CMP.QUANTITY`), VAT `incl`/`excl` (`CMP.VAT`, also between a contract and its act). A number in
words is an amount's (`CMP.AMOUNT_WORDS`, error) when the currency is beside it, in the header
of its table column, or at the start of the next paragraph. A table cell holding a figure and its
words ("800 (რვაასი)") is that figure. A Word table row added or removed is one
`CMP.TEXT_ADDED`/`CMP.TEXT_REMOVED` (its cells joined with " | "); the row numbers after it that
moved are `CMP.FORMATTING`, never `CMP.AMOUNT`. A "text changed" line is dropped only when a
change reported for another line states it whole, both values word for word, or when it is one
of the lines of a table cell reported whole.

## 5. BOQ gold (per set, JSON) and the evaluation policy

`samples/gold_boq/<set>.json`:

```json
{
  "template": "samples/<set>/BOQ/<template>.xlsx",
  "positions": {"<sheet>": [{"row": 12, "item": "1.1", "qty": 4.0, "unit": "item"}]},
  "bids": {
    "<bidder>": {
      "path": "samples/<set>/BOQ/<bidder>.xlsx",
      "quantity_changed": [{"sheet": "...", "item": "1.3", "template_qty": 98, "bid_qty": 110}],
      "unit_changed": [{"sheet": "...", "item": "...", "template_unit": "pers.", "bid_unit": "man-month"}],
      "added": [{"sheet": "...", "item": "1.4.1", "row": 17}],
      "removed": [{"sheet": "...", "item": "1.4", "row": 16}],
      "filled_blank": [{"sheet": "...", "item": "...", "bid_qty": 12, "amount": 3388.54}],
      "sheets_added": [], "sheets_removed": [],
      "unpriced_positions": [{"sheet": "...", "item": "..."}],
      "sheet_amounts": {"<sheet>": 40933.44},
      "sheet_subtotals": {"<sheet>": 40933.44},
      "stated": {"total_cost": 0.0, "excl_vat": 0.0, "incl_vat": 0.0},
      "markup_factor": 1.44,
      "evaluated_cost": 0.0,
      "evaluated_total": 0.0,
      "common_scope_cost": 0.0,
      "common_scope_total": 0.0
    }
  },
  "common_scope": [{"sheet": "...", "item": "..."}],
  "colours": {"red": ["<bidder>"], "green": ["<bidder>"]}
}
```

Policy (client BOQ rules 2, 3, 5, 6 and the user's decision of 2026-09-28):

* A **position** is a row of a priced sheet whose template amount cell holds a product of that
  row (`=O12*G12`) or a number - a leaf item. Rows whose amount is a sum of other rows are
  subtotals; rows with no amount cell are headings or parents - unless they carry a quantity or
  a unit: the client's template has a position whose amount formula is missing, everyone
  priced it, and it is a position. A bidder's price on a row that is not a position is listed
  (`BOQ.HEADING_PRICED`) and never evaluated. An amount formula must refer to a cell of its own
  row: one over a defined name (`=SUM(Block1_Amounts)`) or over other rows is a subtotal. A
  quantity that adds up other rows (`=SUM(G13:G19)`) makes the row a parent; one that adds its
  own row's cells (`=D7+E7`, a quantity split per building) is the position's quantity.
* **Reading the sheet.** A row with an item number and a quantity, a unit or an own-row amount
  is a line whatever its description reads ("Ø12", "600×300"); a repeated header block or
  column-number row further down is not. A header label with sub-labels beneath it ("Unit rate"
  over Materials | Labour | Delivery | Total, "Quantity" over Block A | Block B | Total) is a
  group: its key is the sub-column labelled Total, the components take the component keys, and
  with no Total the key stays unbound. A heading under a one-row header that happens to read
  like a label ("Materials" in the Description column) is content, not header. The item number
  is the first cell between the No column and the description shaped like one ("1.1", "A1"),
  so a column inserted inside that block moves nothing.
* **Placeholders** are optional. A placeholder is a position whose text, or whose heading's
  text, invites the bidder's own items: "any other items", "to be listed here by the tenderer",
  "other costs if necessary", "აღწერაში არ შეტანილი", "სხვა ხარჯები"; "any other item(s),
  work(s) or cost(s)" in any wording; or a description that opens with "Other(s)" / "სხვა" and
  asks the bidder to specify or list, or says it is not included above / in this BOQ /
  elsewhere. "Other" further into a described position ("Paint to walls and other surfaces,
  colour to be specified by the Engineer") is no invitation. It is not "სხვადასხვა
  ხარჯები", which is sundry items the template describes. Left blank, a placeholder is nothing
  missing: it is neither unpriced nor unfilled. Priced, it is the bidder's own additional work. Like
  an added line (client rule 3), it is listed (`priced_optional`, `BOQ.OPTIONAL_PRICED`), kept out
  of `evaluated_cost` and the common scope, and shown in the difference breakdown.
* **Section groups.** A section row that is itself a position ("A8", with its own amount formula)
  and the positions numbered under it ("8.1", "8.2") form one group. Bidders price either the
  section or its items. The group is priced when any member is priced, and an unpriced member of a
  priced group is not listed as unpriced. Each member still costs template quantity × its own
  rate. The common scope is taken over groups: every member of a group that every evaluable bidder
  priced.
* A bid unit cell left blank where the template states a unit is a unit change (`bid_unit: ""`,
  a warning); a unit written where the template left none is listed apart (`unit_filled`).
  Units are compared after folding their spellings: case, spaces and dots; Cyrillic letters
  that print as Latin ("м2"); and one measure's names (m2/მ2/sqm; m3/მ3; m/lm/გრძმ; t/tn/ტ/ტონა;
  kg/კგ; month/თვე; pcs/nr/ც/ცალი; hr/სთ). "item" and "pcs" stay apart: a lump sum is not a
  count. A priced sheet with no recognisable unit column says units were not compared there
  (`BOQ.UNITS_NOT_COMPARED`, info).
* Unit rate `r` = the bidder's rate column ("Rate in USD"); when empty, the sum of the
  materials, labour and delivery rates; on a sheet with no rate column at all, amount ÷ the
  bidder's quantity. Text in a rate, amount or quantity cell is never read as 0: the position
  is not priced (`BOQ.NON_NUMERIC`).
* A position whose template quantity is blank or 0 and which the bidder also left blank is
  listed apart (`unfilled_positions`); it does not make the bid incomplete.
* Positions match by item number and description (after folding); a position moved within its
  sheet, or re-worded (same item number, descriptions ≥ 0.9 alike, `BOQ.DESCRIPTION_CHANGED`),
  is still the position.
* Position cost at template quantities = template quantity × `r`; where the template leaves the
  quantity blank or 0 (the bidder fills it, e.g. preliminaries), the bidder's own amount, and
  the position is listed under `filled_blank`.
* `evaluated_cost` = Σ position costs over the template's positions the bid carries; **added**
  lines never count; **removed** and unpriced positions contribute nothing and are listed.
* `markup_factor` = the bidder's stated total incl. VAT ÷ their stated TOTAL COST (their own
  overheads, profit and VAT); `evaluated_total = evaluated_cost × markup_factor`.
* **Common scope** = positions every bidder priced (non-zero `r`). `common_scope_cost/total`
  are computed over it. **Red/green go on `common_scope_total`**: highest red, lowest green,
  every tied bidder the same colour (compared to the cent), no colour with a single bidder or
  when all tie. A workbook that cannot be evaluated (saved without cached values,
  `BOQ.NO_CACHED_VALUES`) takes no part in the common scope. The full evaluated total and the
  stated total are shown beside it, with the difference broken down (own quantities, added
  lines, priced non-positions, added sheets, Summary against the sheets), and an incomplete bid
  lists what it left out.
* The Summary is read as rows, never keyed by label. Every row that links to a sheet row must
  state that row's amount, TOTAL COST must be the sum of the top-level sections, and the
  overheads, profit, excl.-VAT, VAT and incl.-VAT rows must follow from each other
  (`BOQ.RECONCILE`, naming the rows). The stated total incl. VAT is the row labelled so; there
  is no fallback to another row. Each part of the chain (TOTAL COST, overheads, profit, excl.
  VAT, VAT, incl. VAT) is played by one row and each row plays one part at most: TOTAL COST is
  the last row labelled so before the chain goes on, and the rest are taken below it, top down,
  by the first row labelled for a part still open. A later "TOTAL COST INCL. OVERHEADS &
  PROFIT" or "Advance payment 20% of the price incl. VAT" plays no part. Overheads and profit
  go by their usual names too ("Indirect costs", "Margin", "გეგმიური დაგროვება",
  "არაპირდაპირი ხარჯები"). The total excl. VAT is held against TOTAL COST plus every valued
  row between the two (a contingency, a renamed markup), a row restating the running sum not
  added twice. The top-level sections TOTAL COST adds up are numbered ("1", "2") or lettered
  ("A", "B"), all the way the first one is.
* `sheet_amounts` (Σ of the bidder's own position amounts) must equal `sheet_subtotals` (the
  sheet's own closing total: its SUB-TOTAL row, else the sum row after the last position that
  the Summary links to, else the last one labelled as a total - "TOTAL CARRIED TO SUMMARY",
  "სულ ჯამი", "ჯამი") to the cent; a difference is either the bidder's arithmetic (reported)
  or a reader bug (a defect). Money is compared to the cent however large the sum, never with
  a tolerance that grows with it. A line amount is recomputed from quantity × the rate column,
  or × the sum of the component rates where there is no rate.
* A number typed where the template computes the cell (a formula) is `BOQ.HARDCODED_TOTAL`; a
  number typed where the template itself has a number or nothing replaced no formula.
* The Summary of a workbook is the sheet named "SUMMARY", else the one sheet named like a
  summary ("BOQ SUMMARY"), else, among several such ("Summary of unit rates" beside it), the one
  stating a TOTAL COST or incl.-VAT row and linking to the most sheets. A bid without it states no totals and is not ranked, and the
  missing sheet is named as the Summary. A chart sheet is an added sheet, never read as a
  table. A bid workbook that cannot be read at all is reported on that bidder alone
  (`BOQ.UNREADABLE`) and takes no part in the common scope; the other bids are compared.

## 6. Scanned documents

A scan is matched to its born-digital twin by text similarity before scoring (never by file
name or by assumption). Its gold is the twin's gold. On OCR input an extra finding that the
module marks "possible misread - check the paper original" is allowed; an extra *error* is not.
