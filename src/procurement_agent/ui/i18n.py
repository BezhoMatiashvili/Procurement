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
    # --- the redesigned shell, verdicts and helpers ---
    "nav.skip": ("Skip to content", "მთავარ შინაარსზე გადასვლა"),
    "nav.main": ("Main", "მთავარი ნავიგაცია"),
    "login.lead": (
        "Enter the access key your administrator gave you.",
        "შეიყვანეთ წვდომის გასაღები, რომელიც ადმინისტრატორმა მოგცათ.",
    ),
    "login.submit": ("Sign in", "შესვლა"),
    "login.show": ("Show key", "გასაღების ჩვენება"),
    "login.hide": ("Hide key", "გასაღების დამალვა"),
    "login.help": (
        "The key is checked once; the browser keeps only a session that expires on its own. "
        "Documents never leave this server.",
        "გასაღები მხოლოდ ერთხელ მოწმდება; ბრაუზერი ინახავს მხოლოდ სესიას, რომელსაც ვადა "
        "თავისით გასდის. დოკუმენტები ამ სერვერს არ ტოვებს.",
    ),
    "picker.start": ("Start", "დაწყება"),
    "picker.recent": ("Recent uploads", "ბოლოს ატვირთული"),
    "picker.recent.all": ("All documents", "ყველა დოკუმენტი"),
    "steps.label": ("Progress", "ეტაპები"),
    "steps.upload": ("Choose files", "ფაილების არჩევა"),
    "steps.read": ("Reading", "წაკითხვა"),
    "steps.report": ("Report", "ანგარიში"),
    "upload.chosen": ("Chosen files", "არჩეული ფაილები"),
    "upload.remove": ("Remove", "ამოღება"),
    "upload.missing": ("Choose a file here first.", "ჯერ აქ აირჩიეთ ფაილი."),
    "upload.wrong_type": (
        "Not accepted: {name}. This slot takes {types}.",
        "არ მიიღება: {name}. აქ შეიძლება მხოლოდ {types}.",
    ),
    "upload.retry": (
        "Fix or replace the file marked above, then try again.",
        "შეასწორეთ ან შეცვალეთ ზემოთ მონიშნული ფაილი და სცადეთ ხელახლა.",
    ),
    "upload.replace": ("Choose a different file", "სხვა ფაილის არჩევა"),
    "upload.add_more": ("Add more files", "ფაილების დამატება"),
    "upload.start.compare": ("Upload and compare", "ატვირთვა და შედარება"),
    "upload.timing": (
        "Word and Excel take seconds; scanned PDFs can take a few minutes.",
        "Word-ს და Excel-ს რამდენიმე წამი სჭირდება; დასკანერებულ PDF-ს — შესაძლოა რამდენიმე წუთი.",
    ),
    "report.sections": ("Sections", "განყოფილებები"),
    "report.stat.errors": ("Errors", "შეცდომა"),
    "report.stat.warnings": ("Warnings", "გაფრთხილება"),
    "report.stat.notes": ("Notes", "შენიშვნა"),
    "report.stat.skipped": ("Could not run", "ვერ შესრულდა"),
    "report.verdict.skipped": (
        "{n} check(s) could not run — see why",
        "{n} შემოწმება ვერ შესრულდა — იხილეთ მიზეზი",
    ),
    "aoa.verdict.bad": (
        "{n} issue(s) to fix before this act is signed",
        "ხელმოწერამდე გასასწორებელია: {n}",
    ),
    "aoa.verdict.bad.lead": (
        "Each one is listed below with the value that was expected and what the act says.",
        "ქვემოთ თითოეულს ახლავს მოსალოდნელი მნიშვნელობა და ის, რაც აქტშია მითითებული.",
    ),
    "aoa.verdict.warn": (
        "No errors, {n} warning(s) to review",
        "შეცდომა არ არის, გადასახედია {n} გაფრთხილება",
    ),
    "aoa.verdict.warn.lead": (
        "Warnings are worth a look but may be acceptable.",
        "გაფრთხილებები ყურადსაღებია, მაგრამ შესაძლოა მისაღები იყოს.",
    ),
    "aoa.again": ("Check another act", "სხვა აქტის შემოწმება"),
    "aoa.rerun.lead": (
        "Tell us the contractor type and the NET/VAT wording rule runs too.",
        "მიუთითეთ შემსრულებლის ტიპი და NET/დღგ-ს ფორმულირების წესიც შესრულდება.",
    ),
    "boq.again": ("New comparison", "ახალი შედარება"),
    "boq.bidders_n": ("{n} bidders", "{n} კომპანია"),
    "boq.stat.bidders": ("Bidders", "კომპანია"),
    "boq.stat.spread": ("Highest over lowest", "სხვაობა უმაღლესსა და უდაბლესს შორის"),
    "boq.change": ("Change", "ცვლილება"),
    "boq.quantities.lead": (
        "Positions where a bidder's quantity differs from the template. Open a bidder to see them.",
        "პოზიციები, სადაც კომპანიის მოცულობა შაბლონისგან განსხვავდება. სანახავად გახსენით "
        "კომპანია.",
    ),
    "boq.quantities.changed.short": ("changed", "შეცვლილი"),
    "boq.quantities.filled.short": ("filled in", "შევსებული"),
    "boq.outliers.lead": (
        "Unit rates far from the median of all bidders for the same position.",
        "ერთეულის ფასები, რომლებიც იმავე პოზიციაზე ყველა კომპანიის მედიანას მნიშვნელოვნად სცდება.",
    ),
    "boq.per_bidder": ("Findings per bidder", "დასკვნები კომპანიების მიხედვით"),
    "compare.verdict.bad": ("{n} critical change(s)", "{n} კრიტიკული ცვლილება"),
    "compare.verdict.bad.lead": (
        "The values that changed are listed first below, each linked to its line.",
        "შეცვლილი მნიშვნელობები ქვემოთ პირველ რიგშია ჩამოთვლილი, თითოეული — თავისი "
        "სტრიქონის ბმულით.",
    ),
    "compare.verdict.unrelated": (
        "These are not two versions of one document",
        "ეს ერთი დოკუმენტის ორი ვერსია არ არის",
    ),
    "compare.verdict.noline": (
        "The line-by-line comparison could not run for this pair",
        "ამ წყვილისთვის სტრიქონობრივი შედარება ვერ შესრულდა",
    ),
    "compare.verdict.noline.lead": (
        "The field differences below are the result; the reason is listed further down.",
        "შედეგი ქვემოთ, ველების სხვაობებშია; მიზეზი უფრო ქვემოთაა მითითებული.",
    ),
    "compare.verdict.warn": (
        "{n} change(s), none of them critical",
        "{n} ცვლილება, კრიტიკული არცერთი",
    ),
    "compare.verdict.warn.lead": (
        "None of the changes is classed as critical. Read them below.",
        "არცერთი ცვლილება კრიტიკულად არ არის შეფასებული. იხილეთ ისინი ქვემოთ.",
    ),
    "compare.verdict.cosmetic": ("Only formatting changed", "შეიცვალა მხოლოდ ფორმატირება"),
    "compare.stat.other": ("Other changes", "სხვა ცვლილება"),
    "compare.stat.overlap": ("Text in common", "საერთო ტექსტი"),
    "compare.findings": ("Findings", "დასკვნები"),
    "compare.keys": (
        "In the line-by-line view, jump between changes:",
        "სტრიქონობრივ ხედში ცვლილებებს შორის გადასვლა:",
    ),
    "compare.explain.title": ("Explain with the model", "ახსნა მოდელით"),
    "docs.lead": (
        "Everything you have uploaded. Open a document to extract fields from it.",
        "ყველა ატვირთული ფაილი. ველების ამოსაღებად გახსენით დოკუმენტი.",
    ),
    "docs.upload": ("Upload", "ატვირთვა"),
    "doc.delete.lead": (
        "Documents are deleted automatically when the retention period ends. You can delete "
        "this one now.",
        "დოკუმენტები შენახვის ვადის გასვლისას ავტომატურად იშლება. ამ დოკუმენტის წაშლა ახლავე "
        "შეგიძლიათ.",
    ),
    "extract.all": ("Select all", "ყველას მონიშვნა"),
    "extract.none_selected": ("Clear", "გასუფთავება"),
    "extract.failed": ("The extraction did not finish.", "ამოღება ვერ დასრულდა."),
    "extract.again": ("Try again", "ხელახლა ცდა"),
    "extract.meta": (
        "{n} fields · {model} · {calls} model calls · {s} s",
        "{n} ველი · {model} · მოდელის {calls} გამოძახება · {s} წმ",
    ),
    # --- statuses: the enum value stays the CSS class, only the label is translated ---
    "status.uploaded": ("uploaded", "ატვირთულია"),
    "status.parsing": ("reading", "იკითხება"),
    "status.ready": ("ready", "მზადაა"),
    "status.failed": ("failed", "ვერ მოხერხდა"),
    "status.deleted": ("deleted", "წაშლილია"),
    "status.queued": ("queued", "რიგშია"),
    "status.running": ("running", "მიმდინარეობს"),
    "status.done": ("done", "დასრულდა"),
    "status.cancelled": ("cancelled", "გაუქმდა"),
    "jobkind.parse": ("Reading", "წაკითხვა"),
    "jobkind.extract": ("Extraction", "ამოღება"),
    "jobkind.compare_explain": ("Model explanation", "მოდელით ახსნა"),
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
