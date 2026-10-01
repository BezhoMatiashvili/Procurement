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
| `party_name` | legal form + name, no quotes; the legal form **abbreviated** however the document writes it (`შეზღუდული პასუხისმგებლობის საზოგადოება` is `შპს`, `სააქციო საზოგადოება` is `სს`, `ინდივიდუალური მეწარმე` and `ინდ. მეწარმე` are `ი/მ`) | `შპს ნიმუში დეველოპმენტ` |
| `party_director` | full name, **nominative**, without a courtesy title (`ბატონი`, `ქალბატონ`, `ბ-ნ`) and without a patronymic (`გიორგის ძე`, `ზურაბის ასული` between the first name and the surname); a name written with an initial keeps it (`გ. ნიმუშაძე`); the act check compares it with the contract's director whose first name starts with that letter, on the surname | `გიორგი ნიმუშაძე` (not `…ნიმუშაძის`) |
| `party_address` | as written, no quotes, no trailing punctuation; it ends where another requisite (code, account, bank) begins | `ქ. თბილისი, ნიმუშის ქ. N5` |
| `party_id_code` | digits only (9 or 11); a code printed in groups of three by one separator (`123 456 789`, `123-456-789`, `123.456.789`) is the same code; a labelled run of another length is no code fact | `123456789` |
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
| `payment_terms` | ISO duration + ` working`/` calendar`, then ` advance N%` when an advance is stated; the share alone when no deadline is; never a bare number. A staged payment states the deadline of the balance (the payment after the advance) | `P5D working`, `P10D working advance 30%`, `advance 30%` |
| `vat_status` | one of `incl`, `excl`, `none`, `net` | `incl` for "გადასახადების ჩათვლით" |
| `total_amount`, `price` | number, `.` decimal, no separators (compared numerically); thousands are grouped by a space of any kind (no-break, thin, narrow no-break), an apostrophe (`36'000`), a dot or a comma | `2400` |
| `penalty` | number with `%` (per period after it if stated: `0.5% per day`) or an amount | `0.5% per day` |
| `quantity` | number + unit | `4 item` |
| `contract_number` | as written, upper case, no spaces | `PR-2026/017` |
| `subject` | the subject clause as written (see scoring) | |

Dates are read as Georgian documents write them: a day in quotes (`„30“ ივნისი, 2026 წ.`,
both quotes required), an ordinal first day (`2026 წლის 1-ლი მაისი`), and a two-digit year
only where the word for year follows it (`12.05.26 წ.` is 2026-05-12). The word for year is
part of the date and is read whole (`5 ივლისს, 2026 წელს`, `05.07.2026 წ.`, never `… 2026 წ`),
and a date's postposition is on its month whether the year comes before or after it (`30
ნოემბრამდე, 2026 წ.` and `30 ნოემბრის ჩათვლით, 2026 წ.` are an end). A day range before
one month and year (`1-15 ივნისი, 2026 წ.`, `01-15.06.2026`) is one period, first day to last,
and so is a day and month before a full date, the year written once for both (`01.06-30.06.2026`,
`1 ივნისი - 30 ივნისი, 2026 წ.`); a month in figures written with one digit (`1.2-3.04.2026`)
is no such day and month. An
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

How the parties and their requisites are read (red-team round 2):

* **Identification codes.** One label pattern serves every reader (`normalize.ID_LABEL`): `ს/კ`,
  `ს.კ.`, `ს.კ`, `ს/ნ`, `პ/ნ`, `საიდენტიფიკაციო` alone or with `კოდი`/`ნომერი`/`№` in any case
  (`კოდით`, `ნომრის`), `საიდ. კოდი`, `საიდ. №`, `პირადი ნომერი`, English `ID`, `ID code`,
  `identification number`, `tax ID`, with an optional `N`/`№`/`#`, colon or dash before the
  digits. An abbreviation is a word of its own (`ბს/კ` is none) and `საიდან` is no label.
* **Accounts.** A currency code written straight after an account (`GE…17GEL`, `GE…17 USD`) is
  a sub-account's, no part of the IBAN: the account is its first 22 characters. Letters that
  are no currency code (`…17GELA`) or a digit too many stay part of it.
* **Party names.** A company is also named with its legal form after the quoted name
  (`„ნიმუში“ შპს` is `შპს ნიმუში`). A name with no quotes at all is not read: nothing marks
  where it ends.
* **Signatories.** A party's head signs as `დირექტორი` (also `გენერალური`/`აღმასრულებელი`),
  `თავმჯდომარე`, `ხელმძღვანელი`, `რექტორი`, `პრეზიდენტი` or `მმართველი`, in any case;
  `პრორექტორი`, `ვიცე-პრეზიდენტი`, a title after another body's genitive (`პროექტის
  ხელმძღვანელი`) and a director narrowed by a function (`ფინანსური`, `კომერციული`,
  `ტექნიკური`, `იურიდიული`, `საოპერაციო`, … `დირექტორი`: red team R3) are not the party's
  head; such a co-signer is not compared. `ხელმძღვანელი` and `მმართველი` are adjectives too
  (`ხელმძღვანელი პირი`, `მმართველი ორგანო`): a name after them needs its place (a colon,
  `სახით`, the end of its line) or a second mention. A deputy or an acting head (`დირექტორის
  მოადგილე`, `… მოვალეობის შემსრულებელი`) is not `party_director`.
* **Requisites tables.** A table with one party per column - two sides or two party names side
  by side in one row - is read column by column, each cell its party's; a first column of row
  labels (`ს/კ`, `მისამართი`) labels each cell. An address ends at its cell, also where Docling
  writes a row as `<left cell>, <column header> = <right cell>`.

How the rules read them (red-team round 2):

* **Money.** A magnitude written as a word multiplies its figure (`48 ათას ლარს` -> `48000`,
  `1,2 მილიონ ლარს`, `1.2 მლნ ლარს`, `GEL 2.5 million` -> `1200000`/`2500000`); `ათასობით`
  (thousands of) and a bare `m` are no magnitude. An amount stated as a share of the value right
  before it (`ღირებულების 5%-ის, 100 ლარის ოდენობით`, `5% of the Contract price, GEL 100`) or as
  the tax the price holds (`მათ შორის დღგ 18% - 305.08 ლარი`, `საიდანაც დღგ - ...`, `of which VAT`)
  is an `amount`, never `total_amount` or `price`. In a price table, a cell under a total column
  is a line amount also when its row has no unit price or quantity - a lump-sum table - if the
  row is keyed by an item number (`1`, `1.1`) or the table has a total row of its own; a table
  with neither keeps the figure as the total.
* **Percentages and penalties.** A rate written in words before its figure (`ნული მთელი ორი (0.2)
  პროცენტის`, `zero point two (0.2) percent`) is the same rate; so is a rate in brackets
  (`(18%)`). A cap on a penalty (`მაგრამ არაუმეტეს ღირებულების 10%-ისა`, `არ უნდა აღემატებოდეს`,
  `not exceeding 10%`, `up to a maximum of 10%`) is a `percentage`, never a second penalty; a
  cap word before a rate that carries its own period (`up to 0.5% per day`) leaves it the
  penalty. A period is never read across another rate.
* **VAT.** `none`: the contractor is no VAT payer or the supply is exempt (`არ არის დღგ-ს
  გადამხდელი`, `არ იბეგრება დღგ-ით`, `დღგ-ით დაუბეგრავი`, `გათავისუფლებულია დღგ-სგან`, `არ
  ექვემდებარება დღგ-ით დაბეგვრას`, `not registered as a VAT payer`, `exempt from VAT`, `not
  subject to VAT`); `არის დღგ-ს გადამხდელი` and `დღგ-ს გადამხდელის ნომერი` say nothing about the
  price. `excl` also when the tax is added with a verb (`რასაც დაემატება დღგ`, `ემატება 18% დღგ`,
  `დღგ-ს დამატებით`, `plus 18% VAT`, `VAT to be added`). A rate inside the wording keeps its
  meaning (`inclusive of 18% VAT`, `VAT 18% is included`, `დღგ-ს (18%) ჩათვლით` are `incl`).
  The rate itself is a `percentage`; in a comparison, a rate one version writes beside the tax
  and the other leaves out is no change of its own (the wording's `CMP.VAT` is), and a rate
  changed (`18%` -> `20%`) is `CMP.PERCENT`.
* **Payment terms.** A share is the advance's when an advance word names it (`30% ავანსი`,
  `ავანსის სახით ღირებულების 30%`, `an advance payment of 30%`) with no remainder word or other
  rate between; `დარჩენილი 70%` / `the remaining 70%` is the balance, a `percentage`. A share of
  a payment with no advance word (`ღირებულების 50%-ის გადახდა`) is a `percentage`. In a
  comparison, an advance changed is `advance 20%` -> `advance 30%`.
* **Durations.** The event a count starts from is not what it measures: in `მონტაჟი -
  მიწოდებიდან 10 დღის ვადაში` and `within 10 days after delivery` the 10 days are not a
  `delivery_deadline`; `, ხოლო` / `, whereas` starts another clause, whose count the first
  clause's keyword does not type.
* **Contract numbers.** One Georgian letter between separators is part of the reference
  (`N 12/2026-ა`, `N ს-45/2026`); two letters or more after a hyphen, and `-ს` or `-ი` at its
  end, are a case ending and end it (`N 15/2026-ის` -> `15/2026`). Upper case applies to Latin
  letters only.

How the rules read money, terms and names (red-team round 3):

* **Money.** A figure's words may stand between the figure and its magnitude (`36 (ოცდათექვსმეტი)
  ათას ლარს` -> `36000`). A figure in a bracket that names a tax before it, right after a price
  (`1 180 ლარს დღგ-ს ჩათვლით (დღგ-ს გარეშე 1 000 ლარი)`, `(excl. VAT GEL 1,000)`), is that price
  on another tax basis: an `amount`, and its tax wording is not the price's `vat_status`; a
  bracket with no price before it (`ფასი (დღგ-ს ჩათვლით 1 180 ლარი)`) is the price. Past a turn
  of the price's sentence (`, საიდანაც`, `, ხოლო`, `, მათ შორის`, `, of which`, `, whereas`), a
  figure its own clause names as an advance, a guarantee, a retention or a deposit is an
  `amount`; a periodic price (`ყოველთვიური`, `monthly`) beside a clause naming the total
  (`საერთო`, `total`) is an `amount`, the total is `total_amount`. Without a turn the price
  stays whole (`ღირებულება, ავანსის ჩათვლით, შეადგენს 36 000 ლარს`).
* **Payment terms.** A payment clause is one whatever verb pays: every preverb and version of
  the roots pay (`გადაიხდის`, `გადაუხდის`, `გადაუხადოს`, `იხდის`), transfer (`გადაურიცხავს`,
  `ჩაირიცხება`), reimburse (`აუნაზღაურებს`, `აანაზღაურებს`) and settle, and English `pays`,
  `paying`. Not a payer (`დღგ-ს გადამხდელი`), not damages reimbursed (`ზიანს აუნაზღაურებს`; damage
  named in passing, `თუ საქონელი არ არის დაზიანებული`, is), not English
  `transfer`. A clause after a semicolon that opens with the balance (`; the balance within 10
  working days`, `; დარჩენილი თანხა - ...`) pays with the verb before it.
* **VAT.** `net` is a status only where it qualifies money: not `net of VAT` (`excl`), `net 30
  days`, `net weight`, `ნეტო წონა`; before a money figure it is (`NET 900 ლარი`). Tax wording in a conditional clause (`თუ ... არ არის დღგ-ს
  გადამხდელი, ...`, `If the Contractor is not registered as a VAT payer, ...`) decides the
  status only when nothing else states one.
* **Penalties.** A rate is a `penalty` only with a penalty word in its own sentence (a heading's
  number is no sentence end: `5. პირგასამტეხლო 5.1. ... 0.3%`), no guarantee, retention or
  advance word nearer to it, and never the VAT's rate. English words are whole: `defined` holds
  no `fine`.
* **Names.** A case ending hyphenated to a Latin word, a figure or a bracket inside the quotes
  (`„Green Valley-სთვის“`, `„ვაკე 2-ისთვის“`, `„კორპუსი N3-ის“`) is dropped: only a whole ending
  of the declension; one letter after a hyphen (`ვაკე-ა`) and a nominative `-ი` stay.
* **Contract numbers.** The number may stand before the contract's word, as the genitive puts it
  (`N 27/2026 ხელშეკრულების საფუძველზე`, `№27/2026 ხელშეკრულებით`); the number sign stays
  required, and before any other word (`N 5 პუნქტის`, `N 14 აქტის`) it is no contract number.

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
* An amount's correction (red team R2) is the fee as the act writes it, whole: its words in
  brackets wherever they stand (`100 ლარს (ასი)`, `GEL 100 (ასი)`, `100 ₾ (ასი)`), tetri written
  after the lari rewritten from the price (`100 ლარი და 50 თეთრი` against a price of 100 is
  corrected to `100 ლარი`). With several contract prices (a monthly price and a total) it is the
  one the fee is a slip of - the fewest digit edits, a slip or two at most, then the smallest
  difference - and none when no one price is meant; the message lists them. An error never
  offers the act's own words as its correction. Tetri written after a hyphen (`3 450-00`, red
  team R3) are the figure's fraction, quoted and corrected in that style (`3 540-00 (…) ლარს` ->
  `3 450-00 (…) ლარს`); two digits after a hyphen that are more than the figure before them
  (`10-15 ლარი`) are a range, never tetri.
* Which rule owns which mistake:
  * wrong digits and/or words of the fee → `AOA.FEE` (+ `AOA.AMOUNT_WORDS` when digits and words
    disagree); words misspelled (`ასოი`) → `AOA.AMOUNT_WORDS`, an error, with the correct words
    (words that read as no number and as no misspelling of the figure are a warning: the reader
    may be at fault). Which half of a pair that disagrees is wrong is the contract's to say: a
    figure it states - a price, or a count it writes in digits and words that agree
    (`12 (თორმეტი) პოსტი`), or a number the contract's own item states, in digits alone too,
    for a pair inside a listed service or the subject (`- 12 პოსტი` for the act's `- 15
    (თორმეტი) პოსტი`) - is right, and the other half is corrected; with no such figure the
    digits are taken as written. A number written elsewhere in the contract decides nothing;
  * a figure the act labels as before VAT (`… ლარს დღგ-ს ჩათვლით (დღგ-ს გარეშე 500 ლარი)`) is
    no second fee when another fee the act states is it with VAT added at 18%, or at a rate the
    documents write beside VAT; with the VAT amount written out, net + VAT must make the fee.
    VAT is `დღგ`, `დ.ღ.გ.` or `დ.ღ.გ`, with or without its case ending, before or after
    `ჩათვლით`. Beside another fee the act states (red team R3), such a figure is compared with
    the price before VAT (the price less VAT at that rate) and corrected to it, never to the
    price; a breakdown whose fee and net are right but do not add up with the VAT amount is the
    VAT amount's mistake (`AOA.FEE`, correction = the fee less the net). A figure before VAT the
    act states alone is its fee, compared with the price;
  * a fee the act's price table states in a row a total word names (`სულ`, `ჯამი`), under a
    column whose header names the currency (`ღირებულება (ლარი)`), is a fee like one in the prose
    (`AOA.FEE`, text = the cell's figure, correction in its style); a table with such figures
    and no total row leaves an `AOA.FEE` skip note saying they were not compared;
  * a party's name is written whole: a word's final `ი` dropped (`შპს „ბეტა სერვის“`) is
    `AOA.PARTY_NAME`, corrected to the contract's name, unless an ending is attached to the stem
    (`„ბეტა სერვის“-ის`). A case ending inside the quotes (`„ბეტა სერვისის“`) is still the name;
  * a month name, a Latin/Cyrillic letter inside a Georgian word, or a prose typo →
    `AOA.SPELLING` with the corrected word, a warning. A prose typo whose correction neither
    the act nor the contract writes rests on the dictionary alone, which lacks words and forms:
    it is `info` with `needs_review` ("may be misspelled - check it");
  * the service period's year outside the contract term → `AOA.PERIOD_IN_TERM` (text = the
    period as written, correction = with the in-term year: every year moved when all of them
    are wrong, one end's year when only that end's is); when no single year puts the period
    inside the term, the correction is the term itself, written over the whole period in the
    act's date style (`2026 წლის 1 მარტიდან 2026 წლის 1 სექტემბრის`, `01.03.2026-დან
    01.09.2026`), never ISO unless the act wrote ISO; `AOA.ACT_DATE` only when the act's own
    date is the odd one out. The act's date is read after its signing verb with a one-letter
    slip too (`„აქტი“ გაორმდა …`): the slip is `AOA.SPELLING`'s, the date still the act's;
  * a signatory with the first name as an initial (`ო. ფუტკარაძე`) is the contract's director
    whose first name starts with that letter, and its surname is held to the rule for a full
    name: a final-vowel slip (`ო. ფუტკარაძი`) is an `AOA.DIRECTOR` error, corrected with the
    initial kept (`ო. ფუტკარაძე`);
  * a missing project name / cadastral code → `AOA.PROJECT_PRESENT` / `AOA.CADASTRAL_PRESENT`;
  * a bank code or plot code not the contract's → one `AOA.BANK` / `AOA.CADASTRAL` per value,
    correction = the contract value it is a slip or two from, the contractor's own on a tie;
    a value near none is corrected to the contractor's own, or to the only contract value; no
    correction when that still leaves a choice. A BIC and its `XXX` branch form are one code;
  * an act not written in Georgian: what the rules read off Georgian wording (the parties'
    codes, the project, the period) is a skip note saying so, never a finding - an English
    label's code included;
  * a code the act gives a party whose code the contract does not state is a skip note, never
    corrected to the other party's code;
  * a labelled code of the wrong length (7 to 13 digits) or grouped by hyphens or dots is read
    as the code the act states (red team R3): not the contract's → `AOA.ID_CODES`, text = the
    code as printed, correction = the nearest contract code, written in the act's grouping
    (`987-654-312` → `987-654-321`). A code of the wrong length is corrected only to a contract
    code two digit slips or fewer from it (a digit dropped, doubled or changed), and otherwise
    has none; the message gives its length. Whether every party the contract codes must state its code in the act is not
    decided: a code left out is silent;
  * a signatory written with a patronymic (`დავით გიორგის ძე მელაძე`) is compared on the first
    name and the surname; a finding quotes the whole name as written and its correction keeps
    the patronymic, in the act's case; a signatory nearest to the one the contract names for
    a party the act does not name in a readable form (no quotes) is a skip note, never
    corrected to the neighbour's; a deputy or an acting head is compared only with a deputy the
    contract names, and otherwise leaves a skip note;
  * one account whose currency sub-account the two documents name differently (`…17GEL`
    against `…17USD`) → `AOA.BANK`, a warning, text = the act's currency code, correction = the
    contract's; the account written with a sub-account in one document only is the same account.
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

Red team round 2 (compare) settled these cases:

* A line whose value changed **and** whose other words changed is two findings: the value, and
  `CMP.TEXT_CHANGED` (warning). Examples: a penalty's rate and the party it falls on; a price and
  what it newly leaves out. The words of the reported value do not count as other words, in any
  case form, and neither do a figure's own words. A line removed or added whole is its values
  only, one finding each. The exception is a value on that line that the other document restates:
  then the line is also `CMP.TEXT_REMOVED`/`CMP.TEXT_ADDED`.
* A range is always a period. That holds whatever words stand near it, "contract" or "delivery"
  included, so it is never a `Contract date` or a `Delivery deadline`. Deleting a range is one
  finding, never the range plus each of its ends. A period stated as a month ("2026 წლის მაისის
  თვეში") is that month's first to last day.
* An advance is `advance N%` (`CMP.PAYMENT`). A BIC and its `XXX` branch form are one code.
* The same values written another way are formatting, never a text change. This covers a date
  with its postposition ("2026 წლის 31 დეკემბრამდე" / "31.12.2026-მდე"), "0.1%" / "0.1
  პროცენტის", "ლარს" / "ლ." and "12 600 ლარი და 50 თეთრი" / "12 600.50 ლარი".
* A Word table row that stands elsewhere in the other document is one `CMP.MOVED` (info), never a
  row removed plus a row added. This applies only when the row's words are carried by that one
  row in each document. Each cell changed on the way is classified as an aligned row's cell is;
  a price changed in a moved row is `CMP.AMOUNT`.
* A figure under a quantity header (რაოდენობა, quantity, qty) beside a unit cell naming a unit of
  the closed list is the row's quantity. Changing the figure, the unit, or both gives one
  `CMP.QUANTITY`, e.g. `20 item` -> `20 set`.
* Between a contract and its act, two parties' directors, codes or names that both differ are
  listed as the values removed and added (warnings). Nothing pairs them by role yet.

Red team round 3 (compare) settled these cases:

* A value's words are its own in every form the readers accept, and no value ends inside a
  word: a penalty's period in any case (`დღეზე` -> `კვირაზე` is one `CMP.PERCENT`), a figure's
  words in brackets after it (a table cell `8 450 (...)` -> `8 950 (...)` is one `CMP.AMOUNT`),
  a rate's words before its figure (`ნული მთელი ორი (0.2) პროცენტის` is `0.2%`, formatting), and a
  party's legal form on either side of the quoted name (`„ნიმუში“ შპს` is `შპს „ნიმუში“`).
* A count of time written only in words (`თხუთმეტი სამუშაო დღის`) is its figure: restating
  `15 (თხუთმეტი)` so is formatting, and changing it so is the term's change (`P15D working` ->
  `P20D working`). A count written in words on both sides that changed stays `CMP.AMOUNT_WORDS`
  ("Period in words"), one finding.
* A cell of a table laid out one party per column is read as its column's party's, and a short
  signature line as the party named on the line above it: a director replaced there is
  `CMP.PARTY` "Director / signatory", before and after.
* A figure in a price table's total row (the row a total word names: `სულ`, `ჯამი`, `Total`)
  under a column of money is `CMP.AMOUNT` "Total amount"; a line total stays "Number".
* A day range before one month and year (`01-31.05.2026`, `1-31 მაისი, 2026 წ.`) is one period.
* A line whose one change is a letter slipped (typed wrong, dropped or added) in one word of four
  letters or more that is no value, month or number word is read as meant: no value changes
  with it, and the line is `CMP.TEXT_CHANGED` (or `CMP.SPELLING`).
* A version pair stays two versions however many consecutive lines were edited: a block too
  large to score pairwise is paired in order, and only lines with nothing alike are a removal
  and an addition.

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
  own row's cells (`=D7+E7`, a quantity split per building) is the position's quantity, and so
  is one that adds cells of another sheet (`=SUM('Take-off'!D2:D5)`, a take-off or measurement
  sheet) or plain numbers (`=SUM(30,30,30,30)`). A quantity over a defined name makes a parent
  only where the name stands for other rows of the same sheet.
* **Reading the sheet.** A row with an item number and a quantity, a unit or an own-row amount
  is a line whatever its description reads ("Ø12", "600×300"); a repeated header block or
  column-number row further down is not. A header label with sub-labels beneath it ("Unit rate"
  over Materials | Labour | Delivery | Total, "Quantity" over Block A | Block B | Total) is a
  group: its key is the sub-column labelled Total, the components take the component keys, and
  with no Total the key stays unbound. A component heading its own sub-columns ("Materials"
  over Rate | Amount, "მასალა" / "ხელფასი" over "ერთ. ფასი | სულ") is a group too: its rate
  and amount are that component's rate and sum, never the unit rate or the line amount, and a
  lone "Total" / "ჯამი" beside the groups is the amount. A column takes one key only. A rate or
  amount column is read whatever currency its label names, however attached ("Rate in GEL",
  "Total amount, GEL", "Amount (USD)", "ღირებულება ლარში"); a label that goes on after the
  currency ("Unit rates in USD (excl. VAT)") is not the column itself. A priced sheet with no
  rate or amount column this reading recognises says so (`BOQ.PRICES_NOT_READ`, warning, on
  the template when it is the template's layout) - never "section left unpriced" on bidders
  who priced it. A heading under a one-row header that happens to read
  like a label ("Materials" in the Description column) is content, not header. The item number
  is the first cell between the No column and the description shaped like one ("1.1", "A1"),
  so a column inserted inside that block moves nothing.
* **Numbers typed as text.** Text in a number cell that reads one way ("1,250.50",
  "12.5", "0.125") is that number. Text with one separator and exactly three digits after
  it ("1.250", "5,000") reads two ways - 1.25 or 1,250 - and is never guessed: it takes
  the reading its own row computes with (the cached quantity x rate = amount, a component's
  sum, the component rates added), and with neither or both readings agreeing (an unpriced
  row, nothing beside it) it stays text (`BOQ.NON_NUMERIC`). A template quantity left so is
  evaluated for no bidder (status `non_numeric`, cost 0, out of the common scope, named once
  on the template); it is neither a blank quantity for the bidder to fill in nor a position
  the bidders left unpriced.
* **Placeholders** are optional. A placeholder is a position whose text, or whose heading's
  text, invites the bidder's own items: "any other items" ("any other undescribed items"),
  "to be listed here by the tenderer", "other costs if necessary", "აღწერაში არ შეტანილი";
  "any other item(s), work(s) or cost(s)" in any wording; the bidder addressed ("the tender
  is to list", "Bidder to list and price", "list here", "to be listed here / below / by the
  tenderer"); "Undescribed ..." and "სხვა ხარჯები" as the opening words; or a description
  that opens with "Other(s)" / "სხვა" and asks the bidder to specify or list, or says it is not
  included above / in this BOQ / elsewhere. The same words further into a described position
  are no invitation: "Paint to walls and other surfaces, colour to be specified by the
  Engineer", "texts to be listed by the Engineer", "removal of undescribed obstructions",
  "მობილიზაცია ... და სხვა ხარჯები". It is not "სხვადასხვა ხარჯები", which is sundry items
  the template describes. A heading's invitation reaches every position below it (at any
  quantity) up to the next row that is not a position - the group's Sub-total, the next
  heading - or the next priced section row. Left blank, a placeholder is nothing
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
  that print as Latin ("м2"); and one measure's names, English plurals among them (m2/მ2/sqm;
  m3/მ3; m/lm/გრძმ; t/tn/ტ/ტნ/ტონა; kg/კგ; km/კმ; ha/ჰა; l/ltr/ლ/ლიტრი; day/days/დღე;
  week/კვირა; month/months/თვე; set/kit/კომპლ./კომპლექტი; pcs/nr/ც/ცალი; hr/სთ). Plurals
  are listed, never made by dropping an "s" ("ls", a lump sum, is not a litre). "item" and
  "pcs" stay apart: a lump sum is not a count, and neither is a set. A priced sheet with no
  recognisable unit column says units were not compared there
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
  "არაპირდაპირი ხარჯები"). The tax goes by its names, not only the acronym ("VAT", "Value
  added tax", "დღგ", "დამატებული ღირებულების გადასახადი"): a VAT row may open with "Add"
  or its rate ("Add 18% VAT", "Value added tax @ 18%"), and the total incl. VAT may say
  "with", "including", "incl. 18%" or "დღგ-ის ჩათვლით" ("GRAND TOTAL WITH VAT"). A TOTAL
  COST row that also says excl. VAT ("Total cost, excl. VAT") is TOTAL COST; a later "Total
  cost excl. VAT" row, once TOTAL COST is taken, is the total excl. VAT. A bid whose Summary
  has no row the chain can take its total incl. VAT or TOTAL COST from is not ranked, and
  says which row is missing (`BOQ.NOT_RANKED`, warning). The total excl. VAT is held against
  TOTAL COST plus every valued row between the two (a contingency, a renamed markup), a row
  restating the running sum not
  added twice. The top-level sections TOTAL COST adds up are numbered ("1", "2") or lettered
  ("A", "B"), all the way the first one is.
* `sheet_amounts` (Σ of the bidder's own position amounts) must equal `sheet_subtotals` (the
  sheet's own closing total: its SUB-TOTAL row, else the sum row after the last position that
  the Summary links to, else the last one labelled as a total - "TOTAL CARRIED TO SUMMARY",
  "სულ ჯამი", "ჯამი"; where several rows say SUB-TOTAL / ქვეჯამი, they are the sections' own
  and the sheet is closed by the same role order - the sum row after the last position the
  Summary links to, else the last one labelled as a total, else the last sub-total) to the
  cent; a difference is either the bidder's arithmetic (reported)
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
