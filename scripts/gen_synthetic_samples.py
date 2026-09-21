"""Generate synthetic Georgian/English procurement documents for tests and benchmarks.

Nothing here is real: company names, codes, amounts and people are invented. Output goes to
tests/fixtures/ (committed) so integration tests never depend on client material.

    uv run python scripts/gen_synthetic_samples.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from docx import Document as DocxDocument
from openpyxl import Workbook
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

OFFER_TITLE = "კომერციული წინადადება / Commercial Offer № OFF-2026/031"
OFFER_PARAS = [
    'შპს "სინთეზ ენერჯი" (ს/კ 405123456), წარმოდგენილი დირექტორის ნინო კაპანაძის სახელით, '
    "წარმოგიდგენთ წინადადებას ელექტრო მასალების მიწოდებაზე.",
    "Synthez Energy LLC (ID code 405123456), represented by its Director Nino Kapanadze, "
    "submits this offer for the supply of electrical materials.",
    "თარიღი / Date: 2026 წლის 12 სექტემბერი (12 September 2026).",
    "1. ფასი და ღირებულება. წინადადების საერთო ღირებულება შეადგენს 48 750,00 ლარს დღგ-ს ჩათვლით. "
    "The total value of the offer is 48,750.00 GEL including VAT (18%).",
    "2. მიწოდების ვადა. საქონლის მიწოდება განხორციელდება ხელშეკრულების გაფორმებიდან "
    "30 კალენდარული დღის განმავლობაში. Delivery within 30 calendar days after signing.",
    "3. საგარანტიო ვადა. საგარანტიო ვადა შეადგენს 24 თვეს მიწოდების დღიდან. "
    "Warranty period: 24 months from the date of delivery.",
    "4. გადახდის პირობები. ავანსი 30%, დარჩენილი 70% მიღება-ჩაბარების აქტის გაფორმებიდან "
    "10 საბანკო დღის განმავლობაში. Payment: 30% advance, 70% within 10 banking days after the AOA.",
    "5. პირგასამტეხლო. ვადის დარღვევის შემთხვევაში მიმწოდებელი იხდის პირგასამტეხლოს "
    "ყოველ ვადაგადაცილებულ დღეზე ღირებულების 0,1%-ის ოდენობით. Penalty 0.1% per day of delay.",
    "6. წინადადება ძალაშია 2026 წლის 31 ოქტომბრამდე / valid until 31 October 2026.",
]
OFFER_TABLE = [
    ["#", "დასახელება / Description", "რაოდენობა / Qty", "ერთ. ფასი (GEL)", "ჯამი (GEL)"],
    ["1", "კაბელი NYM 3x2.5 / Cable NYM 3x2.5", "1 200 მ", "4,50", "5 400,00"],
    ["2", "ავტომატური ამომრთველი 16A / MCB 16A", "150 ცალი", "12,00", "1 800,00"],
    ["3", "განაწილების ფარი / Distribution board", "5 ცალი", "1 250,00", "6 250,00"],
    ["", "სულ დღგ-ს გარეშე / Subtotal excl. VAT", "", "", "41 313,56"],
    ["", "დღგ 18% / VAT 18%", "", "", "7 436,44"],
    ["", "სულ / Total", "", "", "48 750,00"],
]

CONTRACT_PARAS = [
    "ხელშეკრულება № PR-2026/017 ნასყიდობის შესახებ",
    "ქ. თბილისი, 2026 წლის 15 მარტი",
    'ერთის მხრივ, სს "მაგნოლია ჰოლდინგი" (ს/კ 204567890), წარმოდგენილი გენერალური დირექტორის '
    "გიორგი ბერიძის სახელით (შემდგომში „შემსყიდველი“), და მეორეს მხრივ, "
    'შპს "სინთეზ ენერჯი" (ს/კ 405123456), წარმოდგენილი დირექტორის ნინო კაპანაძის სახელით '
    "(შემდგომში „მიმწოდებელი“), დებენ წინამდებარე ხელშეკრულებას შემდეგზე:",
    "1. ხელშეკრულების საგანი. მიმწოდებელი კისრულობს ვალდებულებას მიაწოდოს შემსყიდველს "
    "ელექტრო მასალები დანართი №1-ის შესაბამისად.",
    "2. ხელშეკრულების ღირებულება. ხელშეკრულების საერთო ღირებულება შეადგენს 48 750,00 "
    "(ორმოცდარვა ათას შვიდას ორმოცდაათი) ლარს დღგ-ს ჩათვლით.",
    "3. მიწოდების ვადა. მიწოდება უნდა განხორციელდეს არაუგვიანეს 2026 წლის 30 აპრილისა.",
    "4. გადახდის პირობები. ავანსი 30% ხელშეკრულების გაფორმებიდან 5 საბანკო დღეში; "
    "დარჩენილი თანხა მიღება-ჩაბარების აქტის ხელმოწერიდან 10 საბანკო დღის განმავლობაში.",
    "5. საგარანტიო ვადა. საგარანტიო ვადა შეადგენს 24 თვეს.",
    "6. პასუხისმგებლობა. ვადის დარღვევისას მიმწოდებელი იხდის პირგასამტეხლოს 0,1% ყოველ დღეზე, "
    "მაგრამ არაუმეტეს ღირებულების 10%-ისა.",
    "7. ხელშეკრულება ძალაშია ხელმოწერიდან 2026 წლის 31 დეკემბრამდე.",
    # deliberate inconsistency for the phase-3 consistency checks:
    "მხარეთა ხელმოწერები: შემსყიდველი — გენერალური დირექტორი გიორგი ბერიძე; "
    "მიმწოდებელი — დირექტორი ნინო კაპანიძე.",
]


def _fonts() -> tuple[str, str]:
    """Register Noto fonts (Latin + Georgian). Returns (latin_font, georgian_font)."""

    def find(pattern: str) -> str | None:
        try:
            out = subprocess.run(
                ["fc-list", pattern, "file"], capture_output=True, text=True, check=False
            ).stdout
        except FileNotFoundError:
            return None
        for line in out.splitlines():
            path = line.split(":")[0].strip()
            if path.endswith(".ttf") and "Regular" in path:
                return path
        return None

    ka = find(":family=Noto Sans Georgian") or find(":lang=ka")
    la = find(":family=Noto Sans") or find(":family=DejaVu Sans") or find(":lang=en")
    if not ka or not la:
        sys.exit("need Noto Sans + Noto Sans Georgian (or DejaVu) fonts installed")
    pdfmetrics.registerFont(TTFont("Latin", la))
    pdfmetrics.registerFont(TTFont("Georgian", ka))
    return "Latin", "Georgian"


def _mixed(text: str, la: str, ka: str) -> str:
    """Wrap Georgian runs in <font name=...> so reportlab picks glyphs from the right font."""
    out: list[str] = []
    cur_ka: bool | None = None
    buf: list[str] = []

    def flush() -> None:
        if buf:
            s = "".join(buf).replace("&", "&amp;").replace("<", "&lt;")
            out.append(f'<font name="{ka if cur_ka else la}">{s}</font>')
            buf.clear()

    for ch in text:
        # Only Georgian letters use the Georgian font; digits, punctuation and Latin go to the
        # Latin font (Noto Sans Georgian has no digit glyphs, they would silently vanish).
        is_ka = "\u10a0" <= ch <= "\u10ff"
        if ch.isspace():
            buf.append(ch)
            continue
        if cur_ka is None or is_ka != cur_ka:
            flush()
            cur_ka = is_ka
        buf.append(ch)
    flush()
    return "".join(out)


def make_offer_pdf(path: Path) -> None:
    la, ka = _fonts()
    body = ParagraphStyle("body", fontName=la, fontSize=10.5, leading=15, spaceAfter=6)
    title = ParagraphStyle("title", fontName=la, fontSize=14, leading=18, spaceAfter=12)
    doc = SimpleDocTemplate(str(path), pagesize=A4, title="Commercial Offer OFF-2026/031")
    story = [Paragraph(_mixed(OFFER_TITLE, la, ka), title)]
    for p in OFFER_PARAS[:3]:
        story.append(Paragraph(_mixed(p, la, ka), body))
    data = [[Paragraph(_mixed(c, la, ka), body) for c in row] for row in OFFER_TABLE]
    t = Table(data, colWidths=[22, 210, 95, 85, 85])
    t.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ]
        )
    )
    story += [Spacer(1, 8), t, Spacer(1, 12)]
    for p in OFFER_PARAS[3:]:
        story.append(Paragraph(_mixed(p, la, ka), body))
    doc.build(story)


def make_scanned_pdf(src: Path, path: Path) -> None:
    """Rasterise the offer so it has no text layer — exercises OCR."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(src)
    images = [pdf[i].render(scale=2.0).to_pil().convert("RGB") for i in range(len(pdf))]
    pdf.close()
    images[0].save(path, "PDF", resolution=144.0, save_all=True, append_images=images[1:])


def make_contract_docx(path: Path) -> None:
    d = DocxDocument()
    d.add_heading(CONTRACT_PARAS[0], level=1)
    for p in CONTRACT_PARAS[1:]:
        if "48 750,00" in p:  # real contracts bold the key figures; formatting must not hide them
            para = d.add_paragraph()
            before, after = p.split("48 750,00", 1)
            para.add_run(before)
            para.add_run("48 750,00").bold = True
            para.add_run(after)
        else:
            d.add_paragraph(p)
    d.save(path)


def make_boq_xlsx(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "BOQ"
    ws.append(["ხარჯთაღრიცხვა / Bill of Quantities — ტენდერი T-2026-09", "", "", "", "", ""])
    ws.append(["#", "დასახელება", "ერთეული", "რაოდენობა", "ერთ. ფასი (GEL)", "ჯამი (GEL)"])
    rows = [
        (1, "კაბელი NYM 3x2.5", "მ", 1200, 4.5),
        (2, "ავტომატური ამომრთველი 16A", "ცალი", 150, 12.0),
        (3, "განაწილების ფარი", "ცალი", 5, 1250.0),
    ]
    for n, name, unit, qty, price in rows:
        ws.append([n, name, unit, qty, price, qty * price])
    ws.append(["", "სულ დღგ-ს გარეშე", "", "", "", 13050.0])
    ws.append(["", "დღგ 18%", "", "", "", 2349.0])
    ws.append(["", "სულ ჯამი / Total", "", "", "", 15399.0])
    ws2 = wb.create_sheet("პირობები")
    ws2.append(["მიწოდების ვადა", "30 კალენდარული დღე"])
    ws2.append(["საგარანტიო ვადა", "24 თვე"])
    ws2.append(["გადახდა", "ავანსი 30%, დარჩენილი 10 საბანკო დღეში"])
    ws2.append(["წინადადება ძალაშია", "31.10.2026"])
    wb.save(path)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    make_offer_pdf(OUT / "offer_ka_en.pdf")
    make_scanned_pdf(OUT / "offer_ka_en.pdf", OUT / "offer_ka_en_scanned.pdf")
    make_contract_docx(OUT / "contract_ka.docx")
    make_boq_xlsx(OUT / "boq_ka.xlsx")
    for p in sorted(OUT.iterdir()):
        print(f"{p.name:32} {p.stat().st_size // 1024:5} KB")


if __name__ == "__main__":
    main()
