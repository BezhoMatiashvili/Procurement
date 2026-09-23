"""UI strings in Georgian and English.

The people who use this system read Georgian procurement documents all day, so Georgian is the
default and English is the fallback for anyone else. ``checks/findings.py`` already carries
``message_ka``/``message_en`` on every finding "so the same object serves the Georgian UI and an
English report" - this is that UI.

Shape deliberately mirrors ``extraction/labels.py``: one dict, ``key -> (en, ka)``, no gettext,
no catalogues, no build step. ``t()`` returns the key itself on a miss rather than raising: a
typo in a string id must not 500 a report the user is waiting on. ``tests/unit/test_i18n.py``
is what keeps the table honest.
"""

from __future__ import annotations

from typing import Literal

Lang = Literal["ka", "en"]
DEFAULT_LANG: Lang = "ka"
LANG_COOKIE = "pa_lang"

STRINGS: dict[str, tuple[str, str]] = {
    # --- chrome ---
    "app.name": ("Procurement Agent", "შესყიდვების აგენტი"),
    "nav.home": ("Tasks", "ამოცანები"),
    "nav.documents": ("Documents", "დოკუმენტები"),
    "nav.logout": ("Log out", "გასვლა"),
    "nav.back": ("Back", "უკან"),
    "nav.lang": ("Language", "ენა"),
    "footer.offline": (
        "Local & offline · documents are deleted automatically after the retention period",
        "ლოკალური და ოფლაინ · დოკუმენტები ავტომატურად წაიშლება შენახვის ვადის გასვლისას",
    ),
    "login.title": ("Sign in", "შესვლა"),
    "login.key": ("API key", "API გასაღები"),
    "login.invalid": ("Invalid API key", "API გასაღები არასწორია"),
    # every refusal renders error.html - a 415 on a download, a 409 on a report - so the title
    # cannot be about uploads
    "error.title": ("Request refused", "მოთხოვნა უარყოფილია"),
    "error.question": (
        "The question must be between 3 and 500 characters.",
        "კითხვა უნდა შეიცავდეს 3-დან 500-მდე სიმბოლოს.",
    ),
    "error.boq_inputs": (
        "Choose a template and between one and ten bids.",
        "აირჩიეთ შაბლონი და ერთიდან ათამდე წინადადება.",
    ),
    "error.llm_disabled": (
        "The model pass is disabled on this server.",
        "მოდელით ახსნა ამ სერვერზე გამორთულია.",
    ),
    # --- picker ---
    "picker.title": ("What do you want to do?", "რისი გაკეთება გსურთ?"),
    "picker.lead": (
        "Choose a task, then upload the documents it needs.",
        "აირჩიეთ ამოცანა და ატვირთეთ საჭირო დოკუმენტები.",
    ),
    "task.aoa.title": ("Check an acceptance act", "მიღება-ჩაბარების აქტის შემოწმება"),
    "task.aoa.lead": (
        "Verify an act against its contract: parties, ID codes, dates, amounts, bank details.",
        "აქტის გადამოწმება ხელშეკრულებასთან: მხარეები, ს/კ, თარიღები, თანხები, რეკვიზიტები.",
    ),
    "task.aoa.needs": ("Act + its contract", "აქტი + ხელშეკრულება"),
    "task.boq.title": ("Compare BOQ bids", "BOQ-ების შედარება"),
    "task.boq.lead": (
        "Merge every bidder into the tender template and compare quantities, rates and totals.",
        "ყველა კომპანიის BOQ გაერთიანდება საწყის შაბლონში; შედარდება მოცულობები და ფასები.",
    ),
    "task.boq.needs": ("Empty template + bids", "ცარიელი შაბლონი + წინადადებები"),
    "task.extract.title": ("Extract fields from a document", "ველების ამოღება დოკუმენტიდან"),
    "task.extract.lead": (
        "Pull prices, deadlines and terms out of a PDF, Word or Excel file, with citations.",
        "ფასების, ვადებისა და პირობების ამოღება PDF/Word/Excel ფაილიდან, წყაროს მითითებით.",
    ),
    # --- upload step ---
    "upload.aoa.title": ("Check an acceptance act", "მიღება-ჩაბარების აქტის შემოწმება"),
    "upload.boq.title": ("Compare BOQ bids", "BOQ-ების შედარება"),
    "upload.act": ("Acceptance act", "მიღება-ჩაბარების აქტი"),
    "upload.act.hint": (
        "PDF or Word. Word lets us write comments back into the file.",
        "PDF ან Word. Word-ის შემთხვევაში შენიშვნებს თავად ფაილში ჩავწერთ.",
    ),
    "upload.contract": ("Contract", "ხელშეკრულება"),
    "upload.contract.hint": (
        "Without it, six cross-document rules cannot run and are reported as skipped.",
        "მის გარეშე ექვსი შემოწმება ვერ შესრულდება და მოინიშნება როგორც გამოტოვებული.",
    ),
    "upload.template": ("Empty BOQ template", "ცარიელი BOQ შაბლონი"),
    "upload.template.hint": (
        "The workbook you sent the bidders.",
        "ის ფაილი, რომელიც კომპანიებს გაუგზავნეთ.",
    ),
    "upload.bids": ("Bidder workbooks", "კომპანიების შევსებული BOQ"),
    "upload.bids.hint": (
        "Up to 10 files. Each filename becomes the bidder's name.",
        "მაქსიმუმ 10 ფაილი. ფაილის სახელი გახდება კომპანიის სახელი.",
    ),
    "upload.required": ("required", "სავალდებულო"),
    "upload.optional": ("optional", "არასავალდებულო"),
    "upload.choose": ("Choose a file", "აირჩიეთ ფაილი"),
    "upload.choose.many": ("Choose files", "აირჩიეთ ფაილები"),
    "upload.drop": ("or drop it here", "ან ჩააგდეთ აქ"),
    "upload.drop.many": ("or drop them here", "ან ჩააგდეთ აქ"),
    "upload.contractor_type": ("Contractor type", "შემსრულებლის ტიპი"),
    "upload.contractor_type.hint": (
        "Nothing in the act states this. Leave it unset and the NET/VAT wording rule is "
        "reported as skipped rather than guessed.",
        "აქტში ეს არ წერია. თუ არ მიუთითებთ, NET/დღგ-ს ფორმულირების წესი მოინიშნება როგორც "
        "გამოტოვებული და არ გამოიცნობა.",
    ),
    "upload.contractor_type.unset": ("not stated", "არ არის მითითებული"),
    "upload.start": ("Upload and check", "ატვირთვა და შემოწმება"),
    "upload.start.boq": ("Upload and compare", "ატვირთვა და შედარება"),
    "upload.status.waiting": ("waiting", "რიგში"),
    "upload.status.uploading": ("uploading", "იტვირთება"),
    "upload.status.parsing": ("reading", "იკითხება"),
    "upload.status.ready": ("ready", "მზადაა"),
    "upload.status.failed": ("failed", "ვერ მოხერხდა"),
    "upload.busy": (
        "Too many requests — retrying in {n} s",
        "ძალიან ბევრი მოთხოვნა — ხელახლა {n} წმ-ში",
    ),
    "upload.opening": (
        "All files ready, opening the report",
        "ყველა ფაილი მზადაა, იხსნება ანგარიში",
    ),
    "upload.noscript": (
        "JavaScript is disabled. Upload each workbook on the Documents page, then start the "
        "comparison from there.",
        "JavaScript გამორთულია. ატვირთეთ ფაილები დოკუმენტების გვერდზე და იქიდან დაიწყეთ შედარება.",
    ),
    # --- reports, shared ---
    "report.severity.error": ("Error", "შეცდომა"),
    "report.severity.warning": ("Warning", "გაფრთხილება"),
    "report.severity.info": ("Note", "შენიშვნა"),
    "report.expected": ("Expected", "მოსალოდნელი"),
    "report.actual": ("In the document", "დოკუმენტში"),
    "report.rule": ("Rule", "წესი"),
    "report.message": ("Finding", "დასკვნა"),
    "report.skipped.title": ("Could not run, and why", "ვერ შესრულდა და რატომ"),
    "report.skipped.lead": (
        "These rules did not run. They are not passes.",
        "ეს წესები არ შესრულებულა. ეს არ ნიშნავს, რომ შემოწმება გაიარა.",
    ),
    "report.none": ("No discrepancies found.", "შეუსაბამობა ვერ მოიძებნა."),
    "report.info_only": (
        "Notes below are informational only.",
        "ქვემოთ მოცემული შენიშვნები მხოლოდ საინფორმაციოა.",
    ),
    "report.counts": (
        "{e} errors · {w} warnings · {i} notes",
        "{e} შეცდომა · {w} გაფრთხილება · {i} შენიშვნა",
    ),
    # --- AOA report ---
    "aoa.title": ("Acceptance act check", "აქტის შემოწმება"),
    "aoa.act": ("Act", "აქტი"),
    "aoa.contract": ("Contract", "ხელშეკრულება"),
    "aoa.contract.none": ("not supplied", "არ არის ატვირთული"),
    "aoa.download": ("Download the act with comments", "ჩამოტვირთეთ აქტი შენიშვნებით"),
    "aoa.download.unavailable": (
        "Word comments can only be attached to a .docx act; this one is a {kind}.",
        "შენიშვნების ჩაწერა შესაძლებელია მხოლოდ .docx აქტში; ეს ფაილი არის {kind}.",
    ),
    "aoa.rerun": ("Re-run with this contractor type", "ხელახლა შემოწმება ამ ტიპით"),
    # --- BOQ report ---
    "boq.title": ("BOQ comparison", "BOQ-ების შედარება"),
    "boq.template": ("Template", "შაბლონი"),
    "boq.leaderboard": ("Totals", "ჯამური ფასები"),
    "boq.bidder": ("Bidder", "კომპანია"),
    "boq.stated": ("Stated total", "მითითებული ჯამი"),
    "boq.stated.net": ("Net of VAT", "დღგ-ს გარეშე"),
    "boq.recomputed": ("Recomputed from lines", "გადათვლილი პოზიციებით"),
    "boq.lowest": ("lowest", "ყველაზე დაბალი"),
    "boq.highest": ("highest", "ყველაზე მაღალი"),
    "boq.not_comparable": ("not comparable", "არ არის შედარებადი"),
    "boq.not_comparable.why": (
        "A section was left unpriced or a template sheet removed, so this total is not "
        "like-for-like.",
        "სექცია ფასდაუდებელია ან შაბლონის გვერდი ამოღებულია, ამიტომ ეს ჯამი შედარებადი არაა.",
    ),
    "boq.no_total": ("states no total", "ჯამი მითითებული არ არის"),
    "boq.unpriced": ("left unpriced", "ფასდაუდებელი"),
    "boq.removed_sheets": ("sheets missing", "ამოღებული გვერდები"),
    "boq.added_sheets": ("sheets added", "დამატებული გვერდები"),
    "boq.excluded.title": ("Excluded from the comparison", "შედარებაში არ მონაწილეობს"),
    "boq.excluded.lead": (
        "These positions exist in only one workbook, so they are listed here and left out of "
        "the totals above.",
        "ეს პოზიციები მხოლოდ ერთ ფაილშია, ამიტომ ცალკე ჩამოთვლილია და ჯამებში არ შედის.",
    ),
    "boq.added": ("Added by the bidder", "კომპანიის მიერ დამატებული"),
    "boq.removed": ("Missing from the bid", "წინადადებაში აკლია"),
    "boq.quantities.title": ("Quantity differences", "მოცულობების სხვაობა"),
    "boq.quantities.changed": ("Changed quantities", "შეცვლილი მოცულობები"),
    "boq.quantities.filled": (
        "Blank template quantities the bidder filled in",
        "შაბლონში ცარიელი მოცულობები, რომლებიც კომპანიამ შეავსო",
    ),
    "boq.outliers.title": ("Unit-rate outliers", "ერთეულის ფასის გადახრები"),
    "boq.download": ("Download the merged workbook", "გაერთიანებული ფაილის ჩამოტვირთვა"),
    "boq.sheet": ("Sheet", "გვერდი"),
    "boq.row": ("Row", "სტრიქონი"),
    "boq.position": ("Position", "პოზიცია"),
    "boq.template_qty": ("Template", "შაბლონში"),
    "boq.bid_qty": ("Bid", "წინადადებაში"),
    "boq.findings": ("Findings for {bidder}", "დასკვნები — {bidder}"),
    # --- expired ---
    "expired.title": (
        "These documents are no longer available",
        "ეს დოკუმენტები აღარ არის ხელმისაწვდომი",
    ),
    "expired.lead": (
        "Uploads are deleted automatically after the retention period, and a result link only "
        "works for the person who uploaded the files. Upload them again to re-run the check.",
        "ატვირთული ფაილები შენახვის ვადის გასვლის შემდეგ ავტომატურად იშლება, ხოლო ბმული "
        "მუშაობს მხოლოდ იმ მომხმარებლისთვის, ვინც ისინი ატვირთა. ხელახლა ასატვირთად დაბრუნდით.",
    ),
    "expired.again": ("Start again", "თავიდან დაწყება"),
    # --- comparing two documents ---
    "task.compare.title": ("Compare two documents", "ორი დოკუმენტის შედარება"),
    "task.compare.lead": (
        "Every difference between two versions, with the prices, deadlines and bank details "
        "that changed called out first.",
        "ყველა სხვაობა ორ ვერსიას შორის — შეცვლილი ფასები, ვადები და საბანკო რეკვიზიტები "
        "პირველ რიგში.",
    ),
    "task.compare.needs": ("Two documents", "ორი დოკუმენტი"),
    "upload.compare.title": ("Compare two documents", "ორი დოკუმენტის შედარება"),
    "upload.left": ("Original", "ორიგინალი"),
    "upload.left.hint": (
        "The earlier version — Word, PDF or Excel.",
        "ადრინდელი ვერსია — Word, PDF ან Excel.",
    ),
    "upload.right": ("Revised", "შესწორებული"),
    "upload.right.hint": (
        "The version to check against it. Same file type works best.",
        "ვერსია, რომელიც უნდა შევადაროთ. სასურველია იმავე ტიპის ფაილი.",
    ),
    "compare.title": ("Comparison", "შედარება"),
    "compare.counts": (
        "{critical} critical · {other} other · {cosmetic} formatting · {moved} moved",
        "{critical} კრიტიკული · {other} სხვა · {cosmetic} ფორმატირება · {moved} გადატანილი",
    ),
    "compare.overlap": ("{pct} overlap", "{pct} დამთხვევა"),
    "compare.identical": (
        "No differences. The two documents say exactly the same thing.",
        "სხვაობა არ არის. ორივე დოკუმენტი ზუსტად ერთსა და იმავეს ამბობს.",
    ),
    "compare.unrelated": (
        "These do not look like two versions of one document — they overlap by only {pct}. "
        "The field differences below are the answer; the line-by-line diff will be noisy. "
        "To check an acceptance act against its contract, use the act check instead.",
        "ეს ერთი დოკუმენტის ორი ვერსია არ ჩანს — ისინი მხოლოდ {pct}-ით ემთხვევა. "
        "პასუხი ქვემოთ, ველების სხვაობებშია; სტრიქონობრივი შედარება ხმაურიანი იქნება. "
        "მიღება-ჩაბარების აქტის ხელშეკრულებასთან შესამოწმებლად გამოიყენეთ აქტის შემოწმება.",
    ),
    "compare.matters": ("What changed that matters", "რა შეიცვალა მნიშვნელოვანი"),
    "compare.matters.none": (
        "No prices, deadlines, bank details or party names changed.",
        "ფასები, ვადები, საბანკო რეკვიზიტები და მხარეები არ შეცვლილა.",
    ),
    "compare.field": ("Field", "ველი"),
    "compare.in_a": ("In the original", "ორიგინალში"),
    "compare.in_b": ("In the revised", "შესწორებულში"),
    "compare.change": ("Change", "ცვლილება"),
    "compare.where": ("Where", "სად"),
    "compare.diff": ("Line by line", "სტრიქონობრივად"),
    "compare.show.all": ("Everything", "ყველაფერი"),
    "compare.show.changes": ("Only changes", "მხოლოდ ცვლილებები"),
    "compare.show.critical": ("Only critical", "მხოლოდ კრიტიკული"),
    "compare.show": ("Show", "ჩვენება"),
    "compare.apply": ("Apply", "გამოყენება"),
    "compare.unchanged": ("{n} unchanged lines", "{n} უცვლელი სტრიქონი"),
    "compare.added": ("added", "დამატებული"),
    "compare.removed": ("removed", "ამოღებული"),
    "compare.changed": ("changed", "შეცვლილი"),
    "compare.moved": ("moved", "გადატანილი"),
    "compare.kind.critical": ("critical", "კრიტიკული"),
    "compare.kind.substantive": ("substantive", "არსებითი"),
    "compare.kind.cosmetic": ("formatting", "ფორმატირება"),
    "compare.nothing_here": (
        "Nothing matches this filter.",
        "ამ ფილტრს არაფერი შეესაბამება.",
    ),
    "compare.explain": (
        "Explain the {n} unclassified change(s) with the model",
        "მოდელით ახსნა: {n} დაუკლასიფიცირებელი ცვლილება",
    ),
    "compare.explain.lead": (
        "The rules found no price, date, period or bank detail in these — only different "
        "wording. The model can say what the new wording means differently.",
        "წესებმა ამათში ფასი, თარიღი, ვადა ან საბანკო რეკვიზიტი ვერ იპოვა — მხოლოდ სხვა "
        "ფორმულირება. მოდელს შეუძლია თქვას, რას ნიშნავს ახალი ფორმულირება.",
    ),
    "compare.explain.slow": (
        "On CPU this takes about a minute. The page refreshes itself when it is done.",
        "CPU-ზე ამას დაახლოებით წუთი სჭირდება. დასრულებისას გვერდი თავად განახლდება.",
    ),
    "compare.explain.running": (
        "The model is reading the changes…",
        "მოდელი კითხულობს ცვლილებებს…",
    ),
    "compare.explain.badge": ("model", "მოდელი"),
    "compare.explain.none": (
        "Every change already has a value the rules could name.",
        "ყველა ცვლილებას უკვე აქვს მნიშვნელობა, რომელიც წესებმა დაასახელა.",
    ),
    "compare.download.docx": ("Download Word with comments", "Word კომენტარებით"),
    "compare.download.xlsx": ("Download Excel", "Excel-ის ჩამოტვირთვა"),
    "compare.download.unavailable": (
        "The revised document is a .{kind}; Word comments can only be written into a .docx.",
        "შესწორებული დოკუმენტი .{kind}-ია; Word-ის კომენტარები მხოლოდ .docx-ში იწერება.",
    ),
    "compare.print": ("Print", "ბეჭდვა"),
    "compare.original": ("Original", "ორიგინალი"),
    "compare.revised": ("Revised", "შესწორებული"),
    # --- contractor types, in the client's own wording ---
    "contractor.company": ("Company", "იურიდიული პირი / კომპანია"),
    "contractor.natural_person": ("Natural person", "ფიზიკური პირი"),
    "contractor.individual_entrepreneur_vat": (
        "Individual entrepreneur (VAT payer)",
        "ინდ. მეწარმე (დღგ-ს გადამხდელი)",
    ),
    "contractor.small_business": ("Small business", "მცირე მეწარმე"),
    "contractor.foreign": ("Foreign organisation", "უცხოური ორგანიზაცია"),
    # --- document list and extraction (the pre-existing flow) ---
    "doc.name": ("Name", "დასახელება"),
    "doc.kind": ("Type", "ტიპი"),
    "doc.pages": ("Pages", "გვერდი"),
    "doc.status": ("Status", "სტატუსი"),
    "doc.uploaded": ("Uploaded", "ატვირთვის თარიღი"),
    "doc.expires": ("Expires", "წაიშლება"),
    "doc.none": ("No documents yet.", "დოკუმენტები ჯერ არ არის."),
    "doc.never": ("never", "არასდროს"),
    "extract.title": ("Extract", "ველების ამოღება"),
    "extract.fields": ("Fields", "ველები"),
    "extract.question": ("…or ask a question", "…ან დასვით შეკითხვა"),
    "extract.question.hint": (
        "e.g. What is the warranty period?",
        "მაგ. რა არის საგარანტიო ვადა?",
    ),
    "extract.fast": ("fast model (less accurate)", "სწრაფი მოდელი (ნაკლებად ზუსტი)"),
    "extract.use_llm": (
        "use the model (uncheck for rules only, instant)",
        "მოდელის გამოყენება (მოხსენით — მხოლოდ წესები, მყისიერი)",
    ),
    "extract.start": ("Start extraction", "ამოღების დაწყება"),
    "extract.parsing": (
        "Reading the document… this page refreshes automatically.",
        "დოკუმენტი იკითხება… გვერდი ავტომატურად განახლდება.",
    ),
    "extract.slow": (
        "On CPU an extraction with the model can take several minutes. "
        "This page refreshes automatically.",
        "CPU-ზე მოდელით ამოღებას შეიძლება რამდენიმე წუთი დასჭირდეს. გვერდი ავტომატურად განახლდება.",
    ),
    "extract.download": ("Download Excel", "Excel-ის ჩამოტვირთვა"),
    "extract.rules_only": ("rules only", "მხოლოდ წესები"),
    "extract.none": ("Nothing found.", "ვერაფერი მოიძებნა."),
    "job.title": ("Jobs", "დავალებები"),
    "job.one": ("Job", "დავალება"),
    "job.progress": ("Progress", "პროგრესი"),
    "job.message": ("Message", "შეტყობინება"),
    "job.none": ("No jobs.", "დავალებები არ არის."),
    "fact.field": ("Field", "ველი"),
    "fact.value": ("Value", "მნიშვნელობა"),
    "fact.confidence": ("Confidence", "სანდოობა"),
    "fact.source": ("Source", "წყარო"),
    "fact.where": ("Where", "სად"),
    "fact.quote": ("Quote", "ციტატა"),
    "fact.conflict": ("conflict", "წინააღმდეგობა"),
    "fact.page": ("page", "გვერდი"),
    "doc.delete": ("Delete document now", "დოკუმენტის წაშლა ახლავე"),
    "doc.delete.confirm": (
        "Delete this document and all its results now?",
        "წავშალოთ ეს დოკუმენტი და ყველა შედეგი ახლავე?",
    ),
}


def lang_of(raw: str | None) -> Lang:
    """Anything we do not recognise falls back to Georgian, never to an error."""
    return "en" if raw == "en" else DEFAULT_LANG


def t(key: str, lang: Lang) -> str:
    pair = STRINGS.get(key)
    if pair is None:
        return key
    en, ka = pair
    return ka if lang == "ka" else en
