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
| `party_name` | legal form + name, no quotes; the legal form **abbreviated** however the document writes it (`შეზღუდული პასუხისმგებლობის საზოგადოება` is `შპს`, `სააქციო საზოგადოება` is `სს`, `ინდივიდუალური მეწარმე` and `ინდ. მეწარმე` are `ი/მ`) | `შპს ნიმუში ვექტორი` |
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

How the parties are read (red-team round 4):

* **Legal forms.** One table (`normalize.LEGAL_FORMS`) serves every reader - the party reader,
  the comparison's wording fold, the contractor's kind. A form abbreviated with a full stop after
  each letter (`შ.პ.ს.`, `შ. პ. ს.`, `ს.ს.`, `ა.ა.ი.პ.`, `ს.ს.ი.პ.`, `კ.ს.`, `ს.პ.ს.`) or with the
  non-commercial entity's optional letter in brackets (`ა(ა)იპ`) is that form, and `party_name`
  carries its abbreviation without dots or brackets: `ა(ა)იპ „ნიმუში“` is `ააიპ ნიმუში` (the
  decision of round 4, so `ა(ა)იპ` and `ააიპ` name one party), `შ.პ.ს. „ნიმუში“` is `შპს ნიმუში`.
  `კს` and `სპს` are forms as well. An ID label (`ს.კ.`), one letter (`ს.`) and a word ending in
  `სს` are no form; a form with no quoted name after it names no party.
* **Where an address ends.** Also before the party's representative: a comma and
  `წარმოდგენილი`, `წარმომადგენელი`, `რომელსაც წარმოადგენს`, or a head's title with a name
  after it (`, დირექტორი: …`, `, მისი დირექტორის … სახით`); and before another address label
  (`მისამართი:`), the next column's address where a requisites row is read as one line. A
  street named after an office (`პრეზიდენტის გამზ. N5`) has no name after the title and stays.
* **Signatories in a signature's layouts.** A head's name is read after its title across a
  signature's blank and in slashes or brackets (`დირექტორი ______ /ნინო ნიმუშაძე/`), and before
  its title (`ნინო ნიმუშაძე - დირექტორი`, `ნინო ნიმუშაძე (დირექტორი)`) where the name opens its
  line or cell and the title, in the nominative, closes it. In these layouts the name's last word
  must end as a surname does, or the document must name the person again: `(უფლებამოსილი
  პირი)`, `/სახელი გვარი/` are a phrase and a blank's caption. A deputy's or a narrowed title
  after the name (`- დირექტორის მოადგილე`, `- ფინანსური დირექტორი`) is no head's.
* **An attorney at the signature.** `მინდობილი პირი: …`, `მინდობილი პირი …` and
  `წარმომადგენელი (მინდობილობით) …` name the party's attorney, as `მინდობილობით მოქმედი პირი`
  in the preamble does (decided in round 4); extraction reports it as that one is reported, a
  `party_director` noted as signing under a power of attorney. A predicate after the title
  (`მინდობილი პირი ვალდებულია …`) is no name.
* **A key-terms table keyed by item numbers.** A table of three or more columns under one header
  row whose first column holds only item numbers (`1`, `2.`, `1.1`, `ა`) and whose headers name
  no price, quantity, unit or sum (`N | პირობა | მნიშვნელობა`) is written one row to a sentence
  (`1, პირობა = …, მნიშვნელობა = …;`): a term's label types its value as in a two-column table.
  A price table keeps Docling's cells (its line amounts are read cell by cell).

How the parties are read (red-team round 5):

* **A blank is no value.** A draft's fill-in line - a value with no letter and no digit, only
  underscores, dots, ellipses, dashes and spaces (`მისამართი: ___________`) - is no
  `party_address`, `party_name`, `party_director` or `project_name`; a reader yields nothing for
  it (`normalize.is_blank`), nor is a legal form before a blank (`შპს „______“`) a party name,
  so a blank filled in is a value added (COMPARE-R5-07).
* **Where an address ends (continued).** Also at the next field's label on its line: a comma,
  one to four words with no figure, a colon (`, სახაზინო კოდი: …`, `, დღგ-ს გადამხდელის ნომერი:
  …`, `, ვებგვერდი: …`), and before another address's qualifier (`, ფაქტობრივი მისამართი:`,
  `ფაქტიური`, `საფოსტო`), which belongs to the next label, not to this address; a website
  (`ვებგვერდი`, `www.`) ends it with no colon too. A part of the address with a colon after it
  (`კორპუსი: 3, ბინა: 12`) or words with a figure before a colon (`სართ. 4:`) stay in the
  address (EXTRACTION-R5-06).
* **A double surname.** A hyphenated surname is one surname (`ნინო ბერიძე-ნიმუშაძე`); only its
  last part inflects, so `party_director` folds that part to the nominative
  (`…-ნიმუშაძის სახით` is `…-ნიმუშაძე`). A hyphen before a case ending or a postposition (`-ს`,
  `-მა`, `-სთვის`), a title (`-დირექტორი`) or a side, or a dash with spaces, is no second
  surname (EXTRACTION-R5-12).
* **`პირადი №` (policy, decided in round 5).** `პირადი` with a number sign in place of
  `ნომერი` (`პირადი №:`, `პირადი N`, `პირადი #`) labels a personal number, as `საიდ. №` labels a
  code - only with the sign and only before eleven digits. `პირადი` alone (`პირადი შემოსავალი`)
  is no label (EXTRACTION-R5-16).

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

How the rules read dates and periods (red-team round 4):

* **An act's service period, one reader.** The act check and the extraction read it in one
  place (`AoaChecker._period_hits`), so they never state two periods. It opens after a key or
  a label (`შესრულების პერიოდი`, `მომსახურების გაწევის პერიოდი:`, `მიწოდების პერიოდი:`, a bare
  `პერიოდი:` opening its line), within reach; after a past form of a performance root
  (`შესრულებულია`, `შესრულებულ იქნა`, `გაწეულ იქნა`, `განხორციელდა`, `ჩატარდა`, `მიეწოდა`,
  `გასწია`, ...) in that verb's own sentence; failing those, it is a range in a sentence about the
  work (a range written before its verb, `2026 წლის 1-30 ივნისის პერიოდში გასწია`, included).
  Not a period: a label another word qualifies (`საგარანტიო პერიოდი:`, `ხელშეკრულების
  მოქმედების პერიოდი:`), a sentence about a guarantee, an advance or the validity, and a recital
  of the work still to be done (`ვალდებულია გასწიოს …`, `შესრულდება`) with no past verb beside it.
* **A date that names the contract** is never the act's date or a day of its period: the
  contract right after it, its number in any form the number reader reads (`2026 წლის 9 მარტის
  № 318 ხელშეკრულების`, `N`, `#`, `No.`), or the contract and its signing words right before it.
  A comma before the contract word counts only before a signing verb (`… გააფორმეს
  ხელშეკრულება`); `… 30 ივნისის ჩათვლით, ხელშეკრულების პირობების შესაბამისად` ends the period.
* **An act's heading date** is read below a title that dates its contract (`მიღება-ჩაბარების
  აქტი (2026 წლის 15 მაისის N 12/2026 ხელშეკრულებაზე)` / `ქ. თბილისი 30.06.2026`): the contract
  date is passed over, and its contract word no longer makes the next date the contract's. A
  contract word no date names still leaves the heading date unread, and a date after one passed
  over must stand before the preamble ends.
* **The act's date in its other wordings**: `აქტი გაფორმებულია`, `აქტი შედგა`, `აქტს ხელი
  მოეწერა`, `აქტის გაფორმების თარიღია` (the label with its copula), each with the date in the
  same sentence; `აქტი შედგა ორ ეგზემპლარად`, `აქტი შედგება …` and `ხელმოწერის თარიღიდან` date
  nothing.
* **A validity stated as a length is a closed range** (a decision): `ძალაში შედის ხელმოწერისთანავე
  და მოქმედებს 12 (თორმეტი) თვის განმავლობაში` is `2026-03-03/2027-03-03`, the length counted
  from the clause's first day, else from the signing, the day kept and held to the month's last
  (31 August and six months is 28 February). Days and weeks are counted. The length is the
  validity's own: it follows its predicate (`მოქმედებს`, `ძალაშია`, `მოქმედების ვადა`, `valid`,
  `in force`) in the same clause, no comma between; a notice period or a report's deadline in
  the validity's sentence (`…, ხოლო შეტყობინების ვადაა 30 დღე`) is not. A length beside an
  until-phrase (`… ან შესრულებამდე`) or counted from another event (`მიწოდებიდან 12 თვის`) closes
  nothing: the range stays open. With no signing date and no first day known, the length is the
  ISO duration (`P12M`).
* **A signing date labelled anywhere is the signing date**: with no date in the preamble, a date
  after `(ხელშეკრულების) გაფორმების / ხელმოწერის / დადების თარიღი:` or `Date of signing:` is
  `contract_date`, and the validity "from signing" starts on it. Another document's label
  (`მიღება-ჩაბარების აქტის გაფორმების თარიღი:`) is not, and an unlabelled date outside the
  preamble still is no signing date.

How the rules read dates and periods (red team round 5):

* **A date that names the contract, every reader.** Each reader of the act's date - the signing
  verb, the keys, the heading - passes over a date that names the contract and takes the next
  one: `აქტი შედგენილია 2026 წლის 15 მაისის ხელშეკრულების საფუძველზე` dates nothing (the heading
  dates the act), `აქტი შედგა 2026 წლის 15 მაისის ხელშეკრულების საფუძველზე, 2026 წლის 30
  ივნისს` is dated 30 June. A numeric date names the contract with its genitive or instrumental
  on a hyphen too (`15.05.2026-ის ხელშეკრულების`, `-ისა`, `-ით`); `-დან`, `-მდე`, `-ში` and
  `-ის ჩათვლით` mark a period and name nothing.
* **An act's service month after a label, its year after the month's noun.** `საანგარიშო თვე:
  2026 წლის ივნისი` (and `ანგარიშგების თვე:`) opens the period as `საანგარიშო პერიოდი:` does;
  `ივნისის თვეში, 2026 წ.` and `ივნისის თვის განმავლობაში, 2026 წელი` are the month, as the
  postposition sits on the month whichever side its year is. A year that opens a date of its own
  (`ივნისის თვეში, 2026 წლის 20 მაისამდე`) is that date's.
* **"Not later than" marks an end.** `არაუგვიანეს` (`no later than`, `not later than`) before a
  date makes it a range's last day, the genitive with its emphatic vowel included (`არაუგვიანეს
  2026 წლის 31 დეკემბრისა`, `არაუგვიანეს 31.12.2026-ისა`), one definition with the payment
  deadline's. The state template's capped validity (`… მოქმედებს ვალდებულებების სრულ
  შესრულებამდე, მაგრამ არაუგვიანეს 2026 წლის 31 დეკემბრამდე`) is the closed range to that day.
* **The validity's "until fulfilled" is no performance.** A date in a validity clause whose only
  performance word is the noun of its until-phrase (`შესრულებამდე`, `until the full performance
  of …`) is the validity's, never an `execution_period`; a clause that sets the work's window
  and names the validity besides (`შეასრულოს სამუშაოები … 31 ივლისამდე, ხელშეკრულების
  მოქმედების ვადაში`) is still the work's.
* **The English validity clause.** `This Contract shall enter into force on the date of its
  signing and shall remain in force until …` is the validity: `into force` is a validity
  predicate as `in force` is, and `until the full performance of the Parties' obligations` is an
  open end, no performance window.
* **A performance window stated as a first day and a length is the closed range** (a decision,
  as round 4 decided for the validity): `გასწიოს მომსახურება 2026 წლის 1 ივნისიდან 12 (თორმეტი)
  თვის განმავლობაში` is `2026-06-01/2027-06-01`, the length counted from the window's first day
  by the validity's convention (the day kept, held to the month's last). The length follows the
  first day directly (`… -დან 6 თვის ვადით`, `from 1 June 2026 for 12 months`) or the window's
  label (`მომსახურების გაწევის ვადა: 12 თვე`, the first day stated in another clause); a report's
  deadline in the window's sentence (`… -დან და წარმოადგინოს ანგარიში 10 დღის ვადაში`), a count
  from another event (`მიწოდებიდან 12 თვის`) and a length beside an until-phrase close nothing,
  and with no first day the window stays a duration. The length's own words, where no keyword
  types them, are `execution_period`.
* **A staged delivery's batches** (a decision): a delivery clause written as a list, each item
  naming a batch or a stage (`პირველი პარტია - არაუგვიანეს …, მეორე პარტია - არაუგვიანეს …`,
  `batch`, `stage`, `lot`), gives each item's date a `delivery_deadline`; the extraction states
  each one, and a comparison labels each "Delivery deadline". A date in an item that names no
  batch (an invoice's, a payment's) keeps its own type.
* **An act of handover or of work done is an act.** `მიღება-გადაცემის აქტი` (also `საქონლის …`,
  `მიღება - გადაცემის აქტი`) and `შესრულებული სამუშაოების / მომსახურების აქტი` are an act's
  title on a line of their own near the top, in the nominative, with no clause number before
  them and no full stop after: so the act's own date is `act_date` and the date its recital
  names the contract by is `contract_date`. A contract naming that act in a clause
  (`მიღება-გადაცემის აქტის გაფორმებიდან 10 დღეში`, `1.1. მიღება-გადაცემის აქტი ფორმდება …`) stays
  a contract.

How the rules read money, terms and lines (red-team round 4):

* **Label lines (policy).** A `label: value` line with no full stop of its own - a short label
  of words, a colon and a value on the same line, opened by a bullet or not (`საგარანტიო ვადა:
  12 თვე`, `- ანგარიშსწორების პირობები: ... 7 სამუშაო დღის ვადაში`) - ends at its line break:
  it is a sentence of its own. A value that runs on (it ends with a comma, a dash, an open
  bracket or `და`) and an inline run (`შეადგენს` / `100` / `(ასი) ლარს`) stay one sentence, and
  a line break elsewhere is still no sentence end.
* **The contract's price (policy).** The contract's overall figure is `total_amount` even when
  its label is a price, when nothing makes it a price per unit: a total word (`საერთო ფასი`,
  `the total price`) or the contract or what it buys (`ხელშეკრულების ფასი`, `the Contract
  price`, `the price of the Goods`) names it. A price per unit (`ერთეულის ფასი`, `GEL 450 per
  chair`, `1 ცალზე`), a bare `ფასი:` and a price table's cell stay `price`.
* **Money.** A figure that restates the price on another tax basis is an `amount`, and its tax
  wording is not the price's `vat_status`, in a bracket (round 3), past a turn of the price's
  sentence (`36 000 ლარს დღგ-ს ჩათვლით, საიდანაც ღირებულება დღგ-ს გარეშე შეადგენს 30 508.47
  ლარს`, `; მათ შორის: დღგ-ს გარეშე - ...`) or in the next sentence stating the other basis; a
  net figure stated alone stays the price. A breakdown - the net figure, the tax labelled as such
  (`დღგ - 915.25 ლარი`, `დღგ (18%): 915.25 ლარი`, `VAT: GEL 180`), then the figure a total word
  names (`სულ 6 000 ლარი`, `სულ დღგ-ს ჩათვლით: 6 000.00 ლარი`) - has one `total_amount`, the
  one the total word names; the other two are `amount`. A tax label qualified by the price's
  basis (`Price including VAT: 118 GEL`, `ღირებულება + დღგ: 1 180 ლარი`) is the price's. A
  figure alone in a price table's cell whose column header names the currency (`ჯამი (ლარი)`,
  `ღირებულება (ლარი)`) is money in it, typed as a table's cells are; a currency word before a
  figure in prose stays no currency.
* **Durations.** The unit hyphenated to its count is the count's (`20-დღიან ვადაში`,
  `12-თვიანი`), the days counted are read in every case and abbreviated with their stop
  (`20 კალენდარულ დღეში`, `15 სამ. დღის`, `კალ. დღის`), and a count written in words only is
  its figure (`თხუთმეტი სამუშაო დღის` is `P15D working`, in the act check too); an ordinal
  (`20-ე დღეს`, `მე-20 დღეს`, `მეორე დღეს`) is no count, and `სამ` with no stop is the numeral
  three. A count the warranty only frames (`საგარანტიო ვადის განმავლობაში გამოვლენილ ნაკლს ...
  10 დღის ვადაში`, `საგარანტიო ვადის დასრულებიდან 15 დღეში`, `during the warranty period`) or
  that the guarantee money sets (`საგარანტიო თანხა`, `საგარანტიო უზრუნველყოფა`, `საბანკო
  გარანტია`) is no `warranty_period`.
* **Percentages and penalties.** A rate's words may stand between the figure and the sign
  (`5 (ხუთი)%-ის`, `0.1 (ნული მთელი ერთი) %-ის`) as before the word. A penalty's period is never
  read across `, ხოლო` / `, whereas` either: in `0.2% ყოველ დღეზე, ხოლო ... ჯარიმა 10%` the fine
  counts per nothing.
* **Payment terms.** `prepayment` names the advance, and so does an advance word after the
  verb that pays the share (`ღირებულების 30% გადაიხდება ავანსის სახით`, `ავანსად`, `shall be
  paid as an advance`); `დარჩენილი 75% გადაიხდება ავანსის გამოქვითვით` is the balance. A
  staged payment written as a list is one payment clause: after a lead-in ending in a colon,
  an item opening with the advance or the balance (`4.2.1. ავანსი - ...`), and after a
  semicolon an item opening with the balance (`ბ) დარჩენილი თანხა - ...`, `4.2.2. დარჩენილი 70%
  - ...`), so the balance's deadline is the term's; a sibling clause (`4.3. ...`) is none.
* **Contract numbers.** Latin letters are upper case whatever the document's case (`sp-2026/318`
  is `SP-2026/318`); Georgian letters stay as written.

How the rules read money, terms and names (red-team round 5):

* **Money.** An amount written words first with its figure in brackets (`სამი ათას (3 000)
  ლარს`) is one amount, words to currency, its value the figure's, as a duration or a rate
  written so already is; the words must read as a number, so `ღირებულება (3 000) ლარი` is none.
  In the act check its two halves are a pair like `3 000 (სამი ათასი) ლარს`. A currency's name
  may carry its adjective of origin in the noun's case (`ამერიკული დოლარი`, `ამერიკულ დოლარს`
  are USD). A figure's words after its currency (`18 400 ლარს (თვრამეტი ათას ოთხასი ლარი)`, with
  or without tetri) are its own, as before it.
* **The contract's sum.** `ხელშეკრულების თანხა`, `ხელშეკრულების საერთო თანხა` and `საერთო
  თანხა` name the contract's overall figure (`total_amount`) as `ხელშეკრულების ღირებულება`
  does, when no other figure and no rate stand between; `ავანსის თანხა` does not, nor a share's
  base (`ხელშეკრულების თანხის 10%`). `შეფასებულია` (is valued) holds no price word. A price
  of one item - a count of one before the priced item (`ერთი სკამის ღირებულება`, `1 (ერთი)
  ცალი ... ღირებულება`, `თითო`, `one`, `each`), or a per-unit word - is a `price` whichever
  money word labels it; a figure's own words (`1 000 (ერთი ათასი)`) are no count, and
  `სამუშაო` names the works only as a noun (`სამუშაოს`, `სამუშაოების`): `სამუშაო მაგიდის ფასი`
  prices a desk.
* **Parts of the price.** A figure its own clause names an advance, a guarantee, a retention or a
  deposit before it (`გადაიხდის ავანსს 5 520 ლარის ოდენობით`) is an `amount` with or without a
  turn before it; a clause is cut at a comma, and a part word that only qualifies (`ავანსის
  ჩათვლით`) names none, so `ღირებულება, ავანსის ჩათვლით, შეადგენს 36 000 ლარს` stays the
  price. Past a turn, what is left to pay (`ასანაზღაურებელი`, `გადასახდელი`, `დარჩენილი`,
  `payable`, `balance`) is an `amount` too. The advance's share is read back to the start of its
  sentence, however long the amount's words are, never across a turn (`, ხოლო`, `, მაგრამ`), a
  negation (`ავანსი არ გაიცემა`) or a security of the advance (`საბანკო გარანტია ავანსის თანხის
  100%-ის ოდენობით`).
* **A price table's closing rows.** Rows are told apart by their columns as well as their key
  (closing rows often have none). The tax's row (`დღგ`, `დღგ (18%)`) is an `amount`; a total
  row the tax's row and another total row follow (`სულ` / `დღგ` / `სულ დღგ-ს ჩათვლით`) is the
  net, an `amount`, and so is a row naming the tax's absence beside another total row (`ჯამი
  დღგ-ს გარეშე` beside `სულ ჯამი`). A table whose only closing row is `ჯამი დღგ-ს გარეშე` keeps it
  as the total.
* **VAT.** `none` also in the other word orders (`დღგ-ს გადამხდელი არ არის`, `არ არის
  რეგისტრირებული დღგ-ს გადამხდელად`, `დღგ-ის გადამხდელად რეგისტრირებული არ არის`). Wording in a
  fee's own sentence decides its status; the next sentence counts only when the fee's own states
  none, so an advance's or the unit prices' basis in the next clause is theirs. A price that lists
  the tax among its parts (`36 000 ლარს, მათ შორის: დღგ-ს გარეშე - ..., დღგ - ...`) is `incl`.
* **Penalties.** The period in the inessive right after the rate or its case ending (`0.2%
  დღეში`, `კვირაში`, `თვეში`, `წელიწადში`) and written as one word (`ყოველდღე`, `ყოველკვირა`,
  `ყოველთვე`) is the rate's; a count between them is a length (`0.2% 10 დღეში`), and `ყოველთვის`
  (always) is no month. A fine stated as an amount (`ჯარიმა 500 ლარის ოდენობით`) is a `penalty`
  beside a daily rate; a money cap (`მაგრამ არაუმეტეს 1 000 ლარისა`) and a rate restated as
  money (`ღირებულების 5%-ის, 100 ლარის ოდენობით`) are not.
* **Payment terms.** The days a deadline counts are read from the duration itself in every
  path: `ანგარიშსწორების ვადა: ... 10 (ათი) სამუშაო დღე` is `P10D working`; business and banking
  days are working days.
* **Contract numbers.** Behind a label, a colon or a dash after the number sign or the word for
  number (`ხელშეკრულების №: 45/2026`, `ხელშეკრულების ნომერი: 45/2026`, `Contract No.: 45/2026`,
  a key-terms row): after the contract's own word only. `აქტის №: 12` and `ინვოისის ნომერი: 7`
  stay unread, R3-16 being undecided.
* **Subject.** A clause number has its form (`1.1`, `1.`, `1)`); a figure that opens the subject
  (`20 ცალი ...`, `2026 წლის ...`) is its own.
* **Names.** A surname that drops a vowel before its ending (`წერეთლის` of `წერეთელი`) cannot be
  told from its genitive alone: the nominative is the one another written form attests (the
  contract's signature), and a correction in a case the contract writes is its written form
  (`… წერეთლის` in a genitive place). With no form attesting it, the lemma stays as read.

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
  in the places of the party it is wrong for; where it stands in its own party's block it is right,
  and a form of it that stands only there is no finding (red team R4). A finding about the act's
  own date (`AOA.ACT_DATE`, `AOA.ACT_DATE_IN_TERM`) marks the act's own date only - where the act
  is dated, or the date alone in its heading - never another correct copy of the same date, the
  recital's contract date or the period's end (red team R4, AOA-R4-06).
* An amount's correction (red team R2) is the fee as the act writes it, whole: its words in
  brackets wherever they stand (`100 ლარს (ასი)`, `GEL 100 (ასი)`, `100 ₾ (ასი)`), tetri written
  after the lari rewritten from the price (`100 ლარი და 50 თეთრი` against a price of 100 is
  corrected to `100 ლარი`). With several contract prices (a monthly price and a total) it is the
  one the fee is a slip of - the fewest digit edits, a slip or two at most, then the smallest
  difference - and none when no one price is meant; the message lists them. An error never
  offers the act's own words as its correction. Tetri written after a hyphen (`3 450-00`, red
  team R3) are the figure's fraction, quoted and corrected in that style (`3 540-00 (…) ლარს` ->
  `3 450-00 (…) ლარს`); two digits after a hyphen that are more than the figure before them
  (`10-15 ლარი`) are a range, never tetri. A fee in another currency than the contract's price
  (red team R4) is corrected to that price in the act's wording with the contract's currency put
  in, in the case the act writes its own (`1 200 (ათას ორასი) ლარს` against a dollar price ->
  `1 200 (ათას ორასი) აშშ დოლარს`; a code stays a code, a sign a sign), never the machine form
  `1,200.00 USD` unless the act's currency cannot be rewritten.
* A running figure of the contract the act states beside its fee (red team R4) is no fee: work
  done to date (`დღემდე შესრულებული … ღირებულება`), the contract's balance (`ხელშეკრულების
  დარჩენილი ღირებულება`, `ხელშეკრულების ნაშთი`) and what was paid before (`წინა აქტებით
  ანაზღაურებულია`, `უკვე გადახდილი`, `სულ ანაზღაურებულია`), named so by the words before the
  figure in its clause. It is not compared, and `AOA.FEE` leaves a skip note naming it - also
  when it is the act's only figure, never a "no fee" warning (red team R5). A total word alone
  (`სულ`, `ჯამი`), the contract's total, the fee's remainder after an advance and the value of
  the services `უკვე შესრულებული` (already performed: the act's own work, AOA-R5-08) are not
  running figures.
* What is left to pay once the advance is deducted (red team R5, AOA-R5-02: `ავანსის (30%)
  გამოქვითვით ანაზღაურებას ექვემდებარება 70 ლარი`, `გამოქვითული ავანსი: 30 ლარი; გადასახდელი
  თანხა: 70 ლარი`) is the fee's remainder, a part of the fee, never a second fee: the deduction
  word must name the advance (a tax deducted is the net fee), and the payable word must be the
  figure's own. It adds up with the advance's figure, or - when the act or the contract states
  the advance only as a share - as the fee less that share. A remainder that adds up is not
  compared; one that does not is compared like a fee.
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
    `ჩათვლით`, joined to it by a space or a hyphen (`დღგ-ჩათვლით`, `დ.ღ.გ.-ჩათვლით`, red team
    R5). Beside another fee the act states (red team R3), such a figure is compared with
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
  * so is a party's name slipped only in its signature block, which writes no code (red team
    R5, AOA-R5-04): it is the contract party whose name it is one slip from (a letter typed
    wrong, a final `ი` dropped), `AOA.PARTY_NAME` corrected to that name, as the same slip in
    the preamble is; where two parties' names are that close, the side its role word gives
    decides. A name no slip from any party's stays "party not found", with no correction;
  * a month name, a Latin/Cyrillic letter inside a Georgian word, or a prose typo →
    `AOA.SPELLING` with the corrected word, a warning. A prose typo whose correction neither
    the act nor the contract writes rests on the dictionary alone, which lacks words and forms:
    it is `info` with `needs_review` ("may be misspelled - check it");
  * the service period's year outside the contract term → `AOA.PERIOD_IN_TERM` (text = the
    period as written, correction = with the in-term year: every year moved when all of them
    are wrong, one end's year when only that end's is); when no single year puts the period
    inside the term, the correction is the term itself, written over the whole period in the
    act's date style (`2026 წლის 1 მარტიდან 2026 წლის 1 სექტემბრის`, `01.03.2026-დან
    01.09.2026`), never ISO unless the act wrote ISO - an end written with no year takes the
    year as the other end writes it (`25.05-30.06.2026` -> `01.06.2026-31.05.2027`, `25
    მაისიდან 30 ივნისის ჩათვლით, 2026 წ.` -> `1 ივნისიდან, 2026 წ. 31 მაისის ჩათვლით, 2027
    წ.`, red team R4); `AOA.ACT_DATE` only when the act's own
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
    correction when that still leaves a choice. A BIC and its `XXX` branch form are one code.
    An 11-character BIC with a real branch code against the contract's 8-character BIC of the
    same bank (`AAAAGE22TBS` against `AAAAGE22`) is an `AOA.BANK` warning naming the branch,
    text = the act's code, correction = the contract's (red team R5, AOA-R5-13, policy); the
    reverse (the contract's branch against the act's 8-character code) and two different
    branches are not decided and stay errors;
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
  * an attorney (`მინდობილობით მოქმედი პირი`, `მინდობილი პირი` at the signature) the contract
    names, written with a slip, is an `AOA.DIRECTOR` error corrected to the contract's attorney
    in the act's case; an attorney the contract does not name is an `AOA.DIRECTOR` warning with
    no correction (the power of attorney is to be checked); an attorney's signature line whose
    name no party is read with is a skip note, never silence (red team R4, AOA-R4-12);
  * one account whose currency sub-account the two documents name differently (`…17GEL`
    against `…17USD`) → `AOA.BANK`, a warning, text = the act's currency code, correction = the
    contract's; the account written with a sub-account in one document only is the same account;
  * a party written with its legal form in another spelling in the act (`ააიპ` against the
    contract's `ა(ა)იპ`, `შპს` against `შ.პ.ს.`) is the same party, no finding (red team R4); a
    side the contract defines (`(შემდგომში „შემკვეთი“)`) for which it reads fewer parties than
    sides - a party named in a form no reader knows - is an `AOA.PARTY_NAME` skip note naming
    that side, never silence;
  * an attorney the act names at its signature (`მინდობილი პირი: …`) is compared as one named
    in its preamble (red team R4, policy): a slip of the attorney the contract names →
    `AOA.DIRECTOR` error, correction = that attorney in the act's case (`ლია ნიმუშაშვილო` →
    `ლია ნიმუშაშვილი`); any other person - the contract names another attorney, or only a
    director - stays the power-of-attorney warning (`AOA.DIRECTOR`, no correction). The round's
    policy asked for a skip note where the contract names no attorney; that case already had
    this warning, which is kept (a test pins it), so it is never silent.
* the act's date in English (red team R5): `This Act was signed on 30 June 2026`, `the
  Certificate is executed on …` dates the act, and a late English act is an `AOA.ACT_DATE` error
  corrected in English (`5 July 2026` -> `30 June 2026`); an act not in Georgian whose date is
  not read gets an `AOA.ACT_DATE` skip note saying so, never the "date could not be found"
  warning, which stays for a Georgian act;
* **open question for the client** (red team R5, AOA-R5-12): a billing cycle across two months
  (`15.06.2026-დან 14.07.2026-ის ჩათვლით`) signed on its last day is now an `AOA.ACT_DATE` error
  corrected to the end of the second month (31 July), after the work ended. Client rule 4's
  example is a whole month; whether a cycle that crosses a month boundary is signed on its last
  day (clean, or at most an info note) or on the month's end is not decided, and the rule is
  unchanged until the client says. The two candidates: (a) the period's last day when the period
  crosses a month boundary, the month's end for a period inside one month; (b) either day
  accepted;
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
    after: NBGEGE22
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

Red team round 4 (the party readers, section 2): a party renamed behind any legal form
(`ა(ა)იპ`, `შ.პ.ს.`, `კს`, `სპს`) is `CMP.PARTY` "Party name", its value carrying the folded
abbreviation (`ააიპ …`, `შპს …`); the form restyled alone (`შპს` -> `შ.პ.ს.`) is formatting. A
director changed after an address (`…, წარმოდგენილი მისი დირექტორის … სახით`) is one "Director /
signatory" change, and an address changed in its requisites cell is one "Legal address" change.

Red team round 5 (the party readers, section 2): a draft's blank party block filled in is each
value added - `CMP.PARTY` "Legal address" before `None` after the address, never "changed" from
the underscores (COMPARE-R5-07).

Red team round 4 (compare) settled these cases:

* A figure's words dropped or added beside it, the figures of the line the same on both sides,
  are formatting (`10 (ათი) სამუშაო დღის` / `10 სამუშაო დღის`, `2 (ორი) ეგზემპლარად` / `2
  ეგზემპლარად`); words dropped with a figure that changed or went are still reported - through
  the figure: its change, or its removal (round 5 below).
* A value's own words are only those its canonical form encodes; the words a reader reaches over
  are compared as the line's own. A penalty's period is `ყოველ` (`per`, `each`, `for each`) and
  the period's noun: `ყოველ ვადაგადაცილებულ დღეზე` -> `ყოველ ვადაგადაცილებულ სამუშაო დღეზე`
  (or `ყოველ მეათე ... დღეზე`, `for each calendar day` -> `for each working day`) is
  `CMP.TEXT_CHANGED`, a warning; the day basis is no part of the penalty's value. A tax phrase
  is its verb and its tax: in `ფასი მოიცავს ტრანსპორტირების ხარჯებს და დღგ-ს` the costs are the
  line's words, and what the price covers changed is `CMP.TEXT_CHANGED`.
* A VAT inclusion's verb reaches its tax across a list of costs joined by commas, `და` and
  `ასევე` (`მოიცავს ტრანსპორტირების, მონტაჟის ხარჯებს და დღგ-ს` is `incl`; up to eight words),
  never across a turn (`, ხოლო`, `, მაგრამ`, `, თუმცა`, `, ოღონდ`), `გარდა`, a negation or a
  relative clause, and never to a tax the next words add on top (`... და დღგ ემატება ფასს` is
  `excl`). A cost added to the list is `CMP.TEXT_CHANGED` only.
* A case ending hyphenated to a value is the value's (`5%-ის`, `10%-ს`, `30%-იან`): `0.1%-ის` /
  `0.1 პროცენტის` is formatting. A word after a spaced dash (`5% - ...`) is not, nor a
  postposition (`5%-დან`, `10%-მდე`): `5%-ით` -> `5%-დან` is `CMP.TEXT_CHANGED`.
* A count of days is of calendar days unless it names working days, and `ვადაში` after a count
  is the term's own word: `20 კალენდარული დღის ვადაში`, `20 დღის ვადაში`, `20 დღეში` and `20-დღიან
  ვადაში` are one delivery deadline, formatting. A payment term keeps the days it names (`P10D
  working` -> `P10D` is `CMP.PAYMENT`). A rate's words between its figure and its sign (`5
  (ხუთი)%-ის`) are the rate's: restating them is formatting and a changed rate is one
  `CMP.PERCENT`. (Both hold where the readers read the form, red team R4-02/03.)
* A unit cell of a quantity row restyled to the same unit of the closed list (`კვ.მ` / `მ2`,
  `ცალი` / `ც.`, `კუბ.მ` / `მ3`) is formatting.
* A contract number is compared and reported with its Latin letters upper case (`sp-2026/318`
  is `SP-2026/318`); its Georgian letters stay as written.
* **Policy: a fee per period is a price.** A fee its own clause states per month or per another
  period (`ყოველთვიური მომსახურების ღირებულება`, `the monthly service fee`, `1 000 ლარი თვეში`,
  `GEL 1,000 per month`) is `CMP.AMOUNT` labelled "Monthly fee" (per month) or "Unit price" (per
  day, week, quarter or year), never a second "Total amount", whether its clause stands alone
  or beside the total's after `, ხოლო`. A total paid month by month (`გადაიხდება ყოველთვიურად`,
  `paid monthly`), the price of a number of months (`6 თვის მომსახურების ღირებულება`) and an
  advance or guarantee paid monthly are no fee per period.

Red team round 5 (compare, dates) settled these cases:

* A document's own date on one side only - a draft's blank (`„___“ ____ 2026 წ.`) filled in, or
  the preamble date deleted - is `CMP.DATE` "Contract date" ("Act date" for an act), `None` on the
  other side, when the other version writes no date of that value anywhere. Between a contract
  and its act, the act's own date is no date added.
* **Policy: a staged delivery's batch deadline is "Delivery deadline"** (`CMP.DURATION`), each
  batch's date paired with its own: the second batch's date changed is one finding, both changed
  two (section 2, red team round 5).

Red team round 5 (compare) settled these cases:

* **A penalty stated as an amount per period carries its period**, as a rate does: `50 ლარის
  ოდენობით ყოველ ვადაგადაცილებულ დღეზე` is `50.00 GEL per day`, and `დღეზე` -> `თვეზე` is one
  `CMP.PERCENT` "Penalty", `50.00 GEL per day` -> `50.00 GEL per month` (English `GEL 50 per
  day` the same). The period is the amount's own only between it and the next figure or rate:
  in `ჯარიმა 500 ლარი და 50 ლარი ყოველ დღეზე` the day is the 50's, and the 500 stays `500.00
  GEL`. An amount penalty with no period is reported as before. (The extraction's
  `penalty` keeps the amount alone; this is the comparison's form, read line by line between
  two versions. The document-wide pass reads a penalty as the extraction does, so between two
  documents that are no versions of one another an amount penalty is compared as its amount.)
* **A "label: value" line is a sentence of its own in the comparison too** (section 2, R4): terms
  written one to a line are each typed by their own label, so a delivery term changed is one
  "Delivery deadline", never a "Payment terms" from the next line's label. Requisites are still
  read as written.
* **A two-column key-terms table types each value by its row's label** (`პირობა |
  მნიშვნელობა`, one term a row), on its line and in the document-wide pass alike: the delivery
  row changed is one `CMP.DURATION` "Delivery deadline", the penalty row one `CMP.PERCENT`
  "Penalty", the price row one `CMP.AMOUNT` "Total amount". Only a table of exactly two columns
  whose first cell is a term's name (letters, no figure, no colon, short); a value cell that is
  a bare figure keeps its own reading ("Number"), and a table laid out one party per column is
  read by its columns, as before.
* **Two equal values in one sentence are two values**: `40 ცალი მაგიდა და 40 ცალი სკამი` with
  the chairs made 50 is `40 item` -> `50 item`, never a quantity added; an act delivering 38 of
  the 40 is `40 item` -> `38 item`, an error. The same for two equal prices or dates. A value
  restated in another sentence, or on another line, is still one value.
* **Policy: a value added or removed with its words is one finding, the value's.** A clause
  added or deleted whole (`ავანსი შეადგენს 300 (სამასი) ლარს`), or a draft's blank filled in
  (`___ (______) ლარს` -> `300 (სამასი) ლარს`, `___ (____) კალენდარული დღის` -> `20 (ოცი)
  ...`), is the amount, term or quantity added or removed, with no `CMP.AMOUNT_WORDS` beside it;
  a blank filled in may also be `CMP.TEXT_CHANGED` for its underscores. Words are still
  reported when they are on both sides and differ, or when they do not spell the figure beside
  them (`300 (ოთხასი)` added).

## 5. BOQ gold (per set, JSON) and the evaluation policy

* **Currency (2026-10-01).** The currency each price column's header names (a rate, an amount or
  a component, and the group label over them: "Rate in USD", "ღირებულება, ლარი", "Unit rates in
  USD") is read and folded to its code (ლარი/GEL/₾ -> GEL; დოლარი/USD/$ -> USD; ევრო/EUR/€ ->
  EUR); another column's header is not read for it. A bid whose price headers name other
  currencies than the template's is `BOQ.CURRENCY` (warning, expected the template's codes,
  actual the bid's), has no common-scope total and is neither coloured nor ranked. A header
  naming no currency takes the template's, and a template naming none holds no bid to one.
* **A negative rate (2026-10-01).** A template position with a quantity whose unit rate is
  below zero is `BOQ.NEGATIVE_RATE` (warning), status `negative`: no cost, out of the common
  scope, and the bid is not complete.
* **A discount below the total incl. VAT (2026-10-01).** A Summary row labelled as a discount
  (discount, rebate, ფასდაკლება; a total naming one is a total) with a value, below the row
  playing incl. VAT, is `BOQ.DISCOUNT` (warning, actual the discount); the bid is ranked on the
  total incl. VAT before it.

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
      "unit_filled": [{"sheet": "...", "item": "6.2", "bid_unit": "Item"}],
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
  product of two cells that points at another row (`=O23*G19`) is the position's own slip
  (`BOQ.ARITHMETIC`) only on a row with a quantity or a unit; on a row with neither it
  multiplies a sub-total by a rate - a contingency or VAT row (`=F7*E8`, the rate typed in E8) -
  and is a subtotal. A row with its own rate and amount (an amount that is a product of its own
  row, `=D6*E6`) is a position whatever its quantity formula adds: a painting area taken as two
  plastering areas (`=D2+D3`) is a linked take-off, not a parent (round 4, the lead's
  decision). On a row with no amount of its own, a quantity that adds up other rows
  (`=SUM(G13:G19)`) makes the row a parent; one that adds its
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
  lone "Total" / "ჯამი" beside the groups is the amount. The components are materials, labour,
  delivery and machinery ("Machinery", "Plant", "Equipment", "მანქანა-მექანიზმები"). A label
  the reading does not know heading its own Rate | Amount beside a known component group is a
  component too: its sub-columns are never the unit rate or the line amount; a lone unknown
  label over Rate | Amount ("Bidder's price") is not a component, and its sub-columns are the
  rate and the amount. A column takes one key only. A rate or
  amount column is read whatever currency its label names, however attached ("Rate in GEL",
  "Total amount, GEL", "Amount (USD)", "ღირებულება ლარში"); a label that goes on after the
  currency ("Unit rates in USD (excl. VAT)") is not the column itself. A label naming its column
  twice - English and Georgian side by side ("Description / აღწერა", "Unit | ერთეული"), on two
  lines in one cell, or a label and its currency ("Unit rate - USD") - is that column when every
  part read names the same one; "Description / Unit" names none. The description and the
  quantity go by rule, not one phrase at a time: "Description", "Item / Work description",
  "Description of work(s) / goods / items / services", "Name of works / goods", "სამუშაოს /
  საქონლის დასახელება"; "Quantity", "Quantity of work(s)", and რაოდენობა cut after its stem
  with or without its ending back after a hyphen ("რაოდ.", "რაოდენ.", "რ-ბა", "რაოდ-ბა"; a bare
  "რ" is none). A word hyphenated across a line break in a header cell ("რაოდე-" / "ნობა") is
  the word, and an abbreviation's dot run into the next word is that dot and a space ("ერთ.ფასი"
  is "ერთ. ფასი"; round 5). A bare "Name" is no
  description (a bid form above the bill writes it). The unit rate goes by its usual names
  ("Rate", "Unit rate", "Unit price", "Unit cost") and, in Georgian, by rule: a unit word
  (ერთეულის / ერთ.) then a price word (ფასი / ღირებულება) - "ერთეულის ფასი", "ერთ. ღირებულება".
  The line amount goes by
  its usual names ("Amount", "Total", "Cost", "Total cost", "Sum", "ღირებულება", "თანხა") and, in
  Georgian, by rule: a money word (ღირებულება / თანხა / ფასი) with a total word (სულ / საერთო /
  ჯამური / ჯამი) before or after it - "სულ ღირებულება", "საერთო თანხა", "ღირებულება სულ", the
  total-price names "ჯამური ფასი", "საერთო ფასი", "სულ ფასი" (round 5). A bare "Price" / "ფასი" is bound by
  where it stands after the quantity: the unit rate where a line amount is read after it and no
  rate is ("Qty | Price | Amount"), the line amount where none is ("Unit rate | Price"),
  otherwise no column (`BOQ.COLUMNS_NOT_READ` when it holds numbers). A priced sheet with no
  rate or amount column this reading recognises says so (`BOQ.PRICES_NOT_READ`, warning, on
  the template when it is the template's layout) - never "section left unpriced" on bidders
  who priced it: its positions are `prices_not_read`, not unpriced, and the bid is not
  complete. A template sheet whose rates are read but not its line amount says so too
  (`BOQ.AMOUNTS_NOT_READ`, info). A column inside the price block (between the quantity and the
  last rate or amount column) that holds numbers but that the reading does not recognise - a
  component with no known name, a column the bidder inserted - is named (`BOQ.COLUMNS_NOT_READ`,
  info, on the template, or on the bidder when the template has none); on that sheet the rate
  is never held against the components read, and with no rate column a line is priced at its
  amount ÷ the bidder's quantity. A header block may start anywhere in the first 40 rows (a
  bid-form block filled in above the bill pushes it down), and its own rows and the
  column-number row under it are followed wherever they fall; the same holds for the Summary,
  whose TOTAL column is looked for in its header's own rows only, never in the description
  column. A heading under a one-row header that happens to read
  like a label ("Materials" in the Description column) is content, not header. The item number
  is the first cell between the No column and the description shaped like one ("1.1", "A1"),
  so a column inserted inside that block moves nothing.
* **Numbers typed as text.** Text in a number cell that reads one way ("1,250.50",
  "12.5", "0.125") is that number: plain digits with at most one separator, or digits in
  groups of three under one grouping character (a space, "," or ".") with at most one decimal
  separator, the other character, at the end ("1.250,50", "1 250"). Anything else reads no
  clear way and stays text (`BOQ.NON_NUMERIC`, the position not priced): a separator left
  trailing or doubled ("1450.00.", "7.5,"), groups that are not threes ("14.50.0"), two
  grouping characters mixed ("1,250.000.5"), three decimals after the groups ("1 250.000").
  Text with one separator and exactly three digits after
  it ("1.250", "5,000") reads two ways - 1.25 or 1,250 - and is never guessed: it takes
  the reading its own row computes with (the cached quantity x rate = amount, a component's
  sum, the component rates added), and with neither or both readings agreeing (an unpriced
  row, nothing beside it) it stays text (`BOQ.NON_NUMERIC`). A template quantity left as text -
  so, or any other text ("2.4 t", "-"; round 5) - is evaluated for no bidder (status
  `non_numeric`, cost 0, out of the common scope, named once on the template); it is neither a
  blank quantity for the bidder to fill in nor a position the bidders left unpriced.
* **Placeholders** are optional. A placeholder is a position whose text, or whose heading's
  text, invites the bidder's own items: "any other items" ("any other undescribed items"),
  "to be listed here by the tenderer", "other costs if necessary", "აღწერაში არ შეტანილი";
  "any other item(s), work(s) or cost(s)" in any wording; the bidder addressed ("the tender
  is to list", "Bidder to list and price", "list here", "to be listed here / below / by the
  tenderer"); "Undescribed ..." and "სხვა ხარჯები" as the opening words; or a description
  that opens with "Other(s)" / "სხვა" and asks the bidder to specify or list, or says it is not
  included above / in this BOQ / elsewhere - "to be specified / listed by" anyone other than
  the bidder (the Engineer, Employer, Client, Supervisor, Consultant, Architect, Designer,
  დამკვეთი, ზედამხედველი) asks nothing of the bidder: "Other floor finishes: ceramic tiles,
  colour to be specified by the Engineer" is described work. The same words further into a
  described position are no invitation: "Paint to walls and other surfaces, colour to be
  specified by the Engineer", "texts to be listed by the Engineer", "removal of undescribed
  obstructions",
  "მობილიზაცია ... და სხვა ხარჯები". It is not "სხვადასხვა ხარჯები", which is sundry items
  the template describes. A heading's invitation reaches every position below it (at any
  quantity) up to the next row that is not a position - the group's Sub-total, the next
  heading - or the next priced section row. Left blank, a placeholder is nothing
  missing: it is neither unpriced nor unfilled. Priced, it is the bidder's own additional work. Like
  an added line (client rule 3), it is listed (`priced_optional`, `BOQ.OPTIONAL_PRICED`), kept out
  of `evaluated_cost` and the common scope, and shown in the difference breakdown and in the
  merged workbook's "Excluded from the analysis" block, which lists every status that leaves a
  position out of the analysis.
* **Section groups.** A section row that is itself a position ("A8", with its own amount formula)
  and the positions numbered under it ("8.1", "8.2") form one group, up to the first row that is
  not a position and is not itself numbered under the section: a heading with no number, or
  numbered otherwise ("2. EARTHWORKS", "2", "A11"), ends the group, and the items under it are
  their own (round 5); the section's inner heading "8.1" does not. Bidders price either the
  section or its items. The group is priced when any member is priced, and an unpriced member of a
  priced group is not listed as unpriced. Each member still costs template quantity × its own
  rate. The common scope is taken over groups: every member of a group that every evaluable bidder
  priced.
* A bid unit cell left blank where the template states a unit is a unit change (`bid_unit: ""`,
  a warning); a unit written where the template left none is listed apart (`unit_filled`,
  `BOQ.UNIT_FILLED` info, on template positions), never a unit change.
  Units are compared after folding their spellings: case, spaces and dots; Cyrillic letters
  that print as Latin ("м2"); and one measure's names, its English names singular and plural
  and its Georgian abbreviations and full words (m2/მ2/sqm/square metre(s)/კვ.მ;
  m3/მ3/cubic metre(s)/კუბ.მ; m/lm/metre(s)/meter(s)/running or linear metre(s)/გრძმ/მ.გრძ.;
  t/tn/tonnes/ტ/ტნ/ტონ./ტონა; kg/kgs/kilogram(s)/კგ; km/kms/kilometre(s)/კმ; ha/ჰა;
  l/lt/ltr/ltrs/litre(s)/ლ/ლიტრი; day/days/დღე; week/კვირა; month/months/თვე;
  set/kit/კომპლ./კომპლექტი; pcs/nr/nos/number(s)/ც/ცალ./ცალი; hr/სთ; and the lump sum,
  ls/L.S./lump sum/lump-sum - round 4, the lead's decision; and, round 5, the lead's decision,
  the usual Georgian spellings: the man-hour კაც/სთ = კაც.სთ = კაც-სთ = man-hour(s), the man-day
  კაც/დღე and the machine-hour მანქ/სთ likewise, each apart from the hour and the day; შტ = ც =
  ცალი = pcs; კვტ = kW, apart from კვტ/სთ = kWh; მთვ = month; გრ.მ = m). Plurals are listed,
  never made by dropping an "s" ("ls", a lump sum, is not a litre). "item", "each" and "pcs"
  stay apart: a lump sum is not a count, and neither is a set. A unit with a multiplier, per
  hundred or per thousand as Georgian estimates price ("100 m2", "1000 მ3"), folds like its
  measure with the multiplier kept: "100 m2" is "100 მ2", never "m2" or "1000 m2". A priced
  sheet with no
  recognisable unit column says units were not compared there
  (`BOQ.UNITS_NOT_COMPARED`, info).
* Unit rate `r` = the bidder's rate column ("Rate in USD"); when empty, the sum of the
  component rates (materials, labour, delivery, machinery); on a sheet with no rate column at
  all, or with a price column the reading does not recognise (`BOQ.COLUMNS_NOT_READ`), amount ÷
  the bidder's quantity. Text in a rate, amount or quantity cell is never read as 0: the
  position is not priced (`BOQ.NON_NUMERIC`). A line priced by its component rates alone is
  priced: a sheet priced so is never "section left unpriced". The merged sheets show every
  component's rate and, in the unit-rate column, the `r` the evaluation used (saying so where
  it was built from the components or from amount ÷ quantity).
* A position whose template quantity is blank or 0 and which the bidder also left blank is
  listed apart (`unfilled_positions`); it does not make the bid incomplete. One the bidder
  filled in (a quantity or an amount) is listed under `filled_blank`, and is priced only by its
  amount - its own, else its quantity x its rate - when that is not 0 and no quantity, rate or
  amount cell holds text: a quantity alone, or text beside it ("incl.", "n/a"), is no price
  (round 5). Such a line costs 0, stays out of the common scope and is listed unpriced too
  (`unpriced_positions`, status `unpriced` or `non_numeric`); the bid is not complete on it.
* Positions match by item number and description (after folding); a position moved within its
  sheet, or re-worded (same item number, descriptions ≥ 0.9 alike, `BOQ.DESCRIPTION_CHANGED`),
  is still the position. Round 4, the lead's decisions: a bid description that keeps the
  template's whole and adds the bidder's words after it, from where a word ends (the offered
  make or model: "Desktop monitor 24 inch IPS / Model M24-I"), is the position under the same
  item number, said by `BOQ.DESCRIPTION_CHANGED` at info - "Pipe 20" is never "Pipe 200 mm".
  An item number the bidder's spreadsheet stored as a number ("1.10" read back as 1.1) is the
  position when the number is the template's and the description is the template's word for
  word, nearest row first; typed as the text "1.1" it is a renumbering (by design, R1-23).
  Round 5: an item number written with a closing dot is the same number - "1." is "1", "1.1."
  is "1.1" - only the dot being folded, never a digit ("1.10" stays apart from "1.1").
* Position cost at template quantities = template quantity × `r`; where the template leaves the
  quantity blank or 0 (the bidder fills it, e.g. preliminaries), the bidder's own amount, and
  the position is listed under `filled_blank`.
* `evaluated_cost` = Σ position costs over the template's positions the bid carries; **added**
  lines never count; **removed** and unpriced positions contribute nothing and are listed.
* `markup_factor` = the bidder's stated total incl. VAT ÷ their stated TOTAL COST (their own
  overheads, profit and VAT); `evaluated_total = evaluated_cost × markup_factor`.
* **Common scope** = positions every bidder priced (non-zero `r`). `common_scope_cost/total`
  are computed over it. **Red/green go on `common_scope_total`**: highest red, lowest green,
  every tied bidder the same colour (compared to the cent, rounded half up once float noise
  below 1e-9 is off, the same rounding every shown total uses), no colour with a single bidder or
  when all tie. A workbook that cannot be evaluated (saved without cached values,
  `BOQ.NO_CACHED_VALUES`) takes no part in the common scope. The full evaluated total and the
  stated total are shown beside it, with the difference broken down (own quantities, added
  lines, priced non-positions, added sheets, Summary against the sheets), and an incomplete bid
  lists what it left out.
* The Summary is read as rows, never keyed by label. Its header is read with the bill's own
  vocabulary (a description column by the bill's names or "Section"; the TOTAL column a label
  opening with total / amount / ღირებულება / ჯამი / სულ / თანხა, or one the bill reads as the
  line amount - "Cost (GEL)", "Sum", "ჯამური ფასი" - or, with no unit rate on a Summary, a bare
  "Price" / "ფასი"). A Summary whose header is not recognised says so (`BOQ.NOT_RANKED`, naming
  the sheet and the header rows searched), never that a row is missing. Every row that links
  to a sheet row must
  state that row's amount (a row summing a range of a sheet, `=SUM('1. WORKS'!F2:F6)`, the
  amounts of the positions in the range; a range scaled or added to anything else, or taking in
  a subtotal, is not checked), TOTAL COST must be the sum of the top-level sections, and the
  overheads, profit, excl.-VAT, VAT and incl.-VAT rows must follow from each other
  (`BOQ.RECONCILE`, naming the rows). A chain row's rate is the one its label states
  ("Overheads 10%", "VAT 18%") or the fraction typed beside its TOTAL, either holding being
  enough - in the column right of TOTAL, else the one left of it where the header names it a
  rate column ("%", "Rate", "პროცენტი"; round 5); a cell dividing the row's own total by another
  ("% of total", `=C4/$C$8`) is a share, never a rate. A profit rate, read from the label or typed beside the total, holds on TOTAL COST or on
  TOTAL COST + overheads (the Georgian estimate's base). The stated total incl. VAT is the row labelled so; there
  is no fallback to another row. Each part of the chain (TOTAL COST, overheads, profit, excl.
  VAT, VAT, incl. VAT) is played by one row and each row plays one part at most: TOTAL COST
  ("Total cost", "Total construction / direct / works / civil works / project cost", "ჯამური
  ღირებულება", "პირდაპირი ხარჯები სულ", "სამშენებლო სამუშაოების ღირებულება"; never "Indirect
  costs", the overheads) is the last row labelled so before the chain goes on, and the rest are taken below it, top down,
  by the first row labelled for a part still open. A later "TOTAL COST INCL. OVERHEADS &
  PROFIT" or "Advance payment 20% of the price incl. VAT" plays no part. Overheads and profit
  go by their usual names too ("Indirect costs", "Margin", "გეგმიური დაგროვება",
  "არაპირდაპირი ხარჯები"). The tax goes by its names, not only the acronym ("VAT", "Value
  added tax", "დღგ", "დამატებული ღირებულების გადასახადი", and the acronyms spelt with dots,
  "V.A.T.", "დ.ღ.გ.", "დ.ღ.გ.-ს ჩათვლით" - never "V.A.Tech"): a VAT row may open with "Add",
  its rate or a word saying it is the tax's sum ("Add 18% VAT", "Value added tax @ 18%",
  "Total VAT", "Amount of VAT", "სულ დღგ"), and the total incl. VAT may say "with",
  "including", "incl. 18%" or "დღგ-ის ჩათვლით" ("GRAND TOTAL WITH VAT"), or put the tax name
  first joined by a space or a hyphen ("VAT-inclusive total"; "VAT-exclusive total" is the
  total excl. VAT). In Georgian "with VAT" goes by grammar (round 5): the tax's name in the
  instrumental, attached or hyphenated ("ჯამი დღგ-ით", "სულ დღგ-თ", "გადასახადით"), or followed
  by ჩათვლით / გათვალისწინებით / შეტანით is the total incl. VAT; followed by გარეშე /
  გამოკლებით it is the total excl. VAT. A label that says incl. or excl. VAT is never the VAT
  row. Where no row plays the VAT, the total incl. VAT is held against its base plus every
  valued row between them (a tax the chain does not name, "გადასახადი 18%"), a row restating
  the running sum not added twice. A TOTAL
  COST row that also says excl. VAT ("Total cost, excl. VAT") is TOTAL COST; a later "Total
  cost excl. VAT" row, once TOTAL COST is taken, is the total excl. VAT. A bid whose Summary
  has no row the chain can take its total incl. VAT or TOTAL COST from is not ranked, and
  says which row is missing (`BOQ.NOT_RANKED`, warning) - or, where the row is there but
  states no number in the column read as the Summary's total, names that row and column. The total excl. VAT is held against
  TOTAL COST plus every valued row between the two (a contingency, a renamed markup), a row
  restating the running sum not
  added twice. The top-level sections TOTAL COST adds up are numbered ("1", "2"), lettered
  ("A", "B") or numbered in Roman numerals ("I", "II", "III"; round 5), all the way the first
  one is. I, V and X are letters as well as numerals: the items are Roman where one of them is
  a numeral of more than one character (II, IV) and none is a letter that is not a numeral.
* `sheet_amounts` (Σ of the bidder's own position amounts) must equal `sheet_subtotals` (the
  sheet's own closing total: its SUB-TOTAL row below every position, else the sum row after
  the last position that
  the Summary links to, else the last one labelled as a total - "TOTAL CARRIED TO SUMMARY",
  "სულ ჯამი", "ჯამი"; where several rows say SUB-TOTAL / ქვეჯამი, or the one that does has
  positions after it, they are the sections' own
  and the sheet is closed by the same role order - the sum row after the last position the
  Summary links to, else the last one labelled as a total, else the last sub-total. A sum row
  is found by its role wherever its label sits: labelled across the item column (merged from
  A), or in the Unit or Rate column, with nothing in the Description column, it is still the
  row - a row whose amount adds other rows, with no quantity or unit of its own) to the
  cent. Round 5, the lead's decision: where the rows after the last position are a sum row,
  then rows built only as rates of it (a contingency, `=F7*E8`), then the closing row built from
  those ("Total", "Contingency 5%", "TOTAL CARRIED TO SUMMARY"), the positions are held against
  the first, whatever it is labelled - the contingency is part of the chain, and the carried
  total still answers for the Summary's link. The rows are told by how they are built, never by
  whether the first one's range covers the positions: a range one row short is the bidder's
  slip and is reported. A difference is either the bidder's arithmetic (reported)
  or a reader bug (a defect). Money is compared to the cent however large the sum, never with
  a tolerance that grows with it. A line amount is recomputed from quantity × the rate column,
  or × the sum of the component rates where there is no rate.
* A number typed where the template computes the cell (a formula) is `BOQ.HARDCODED_TOTAL`; a
  number typed where the template itself has a number or nothing replaced no formula.
* The Summary of a workbook is the sheet named "SUMMARY", else the one sheet named like a
  summary ("BOQ SUMMARY"), else, among several such ("Summary of unit rates" beside it), the one
  stating a TOTAL COST or incl.-VAT row and linking to the most sheets. A bid without it states no totals and is not ranked, and the
  missing sheet is named as the Summary - whatever it writes under its bill. Where the
  template has no Summary either, a single-sheet price schedule
  (the ordinary goods or simplified form) states its totals under the bill (round 4, the
  lead's decision): its closing total plays TOTAL COST, and the VAT and total incl. VAT rows
  below it ("VAT 18%", "Total incl. VAT") complete the chain, reconciled like a Summary's (the
  closing total being the total before VAT). Round 5, the lead's decision: a price list whose
  only closing row is its total incl. VAT ("სულ ჯამი (დღგ-ს ჩათვლით)") - the prices including
  the tax, the row plainly adding them, no other valued row under the bill - is ranked on that
  row, which closes the sheet and plays both TOTAL COST and the total incl. VAT (markup 1.0); a
  total incl. VAT computed onto a pre-VAT sum (`=SUM(F2:F6)*1.18`) is not read so, and a bid
  closing so where the template's own bill goes Total, VAT, Total incl. VAT is not ranked (read
  at markup 1.0, prices the template says are before VAT would rank 18% low). Without a row
  saying incl. VAT there, the bid is
  not ranked and its skip note says it has no Summary sheet stating a total incl. VAT. With
  several priced sheets and no Summary, none is read so. A hidden sheet laid out as a bill that
  the Summary does not link to (an earlier draft kept in the template) is no priced section
  (round 5, the lead's decision): it is said once on the template (`BOQ.SHEET_SKIPPED`, info),
  never "section left unpriced" on the bidders; a bidder who prices it is shown like a priced
  non-position (`BOQ.HEADING_PRICED`) and never evaluated. The rule is the template's: a
  section of the template that a bidder hid is still that section, whatever their Summary
  links. A hidden sheet with no bill on it stays a helper sheet, and sheet presence still counts
  hidden sheets. A chart sheet is an added sheet, never read as a
  table. A bid workbook that cannot be read at all is reported on that bidder alone
  (`BOQ.UNREADABLE`) and takes no part in the common scope; the other bids are compared.

## 6. Scanned documents

A scan is matched to its born-digital twin by text similarity before scoring (never by file
name or by assumption). Its gold is the twin's gold. On OCR input an extra finding that the
module marks "possible misread - check the paper original" is allowed; an extra *error* is not.
