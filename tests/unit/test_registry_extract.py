"""Reading a party's business-registry extract (NAPR „ამონაწერი“), in both known layouts.

Every name, number and address here is invented; the layouts follow the agency's extracts of 2011
and 2025 (labels, their order and wrapping, how authorised persons are listed).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from docx import Document

from procurement_agent.registry.extract import (
    NotAnExtractError,
    parse_extract,
    persons,
    read_extract,
)
from procurement_agent.registry.snapshot import load_snapshot, normalize_code, open_snapshot

TITLE = "ამონაწერი მეწარმეთა და არასამეწარმეო\n(არაკომერციული) იურიდიული პირების\nრეესტრიდან\n"

LAYOUT_2011 = (
    "*B90000001 *\n"
    + TITLE
    + """განაცხადის ნომერი: 123456
განაცხადის რეგისტრაციის ნომერი: B90000001
ამონაწერის მომზადების თარიღი: 14/03/2026 10:15:00
სუბიექტი
საფირმო სახელწოდება: შპს ნიმუშის ჯგუფი
იურიდიული მისამართი: საქართველო, ქ. თბილისი, მთაწმინდის რაიონი,
ტესტის ქ. №7
საიდენტიფიკაციო კოდი: 400000017
სამართლებრივი ფორმა: შეზღუდული პასუხისმგებლობის საზოგადოება
სახელმწიფო რეგისტრაციის
თარიღი: 01/02/2015
მარეგისტრირებელი ორგანო: საჯარო რეესტრის ეროვნული სააგენტო
ხელმძღვანელობაზე/წარმომადგენლობაზე უფლებამოსილი პირები
- 01000000011, ნიკა სამხარაძე,
დირექტორი, ,
- 01000000022, ანა ჯაფარიძე,
წარმომადგენელი, ,
დამფუძნებლები
- 01000000033, ლევან ქავთარაძე,
ყადაღა/აკრძალვა:
რეგისტრირებული არ არის
საჯარო რეესტრის ეროვნული სააგენტო. http://public.reestri.gov.ge გვერდი: 1(1)
"""
)

LAYOUT_2025 = (
    "*B90000002 *\nსაქართველოს იუსტიციის\nსამინისტრო\nსსიპ საჯარო რეესტრის ეროვნული\nსააგენტო\n"
    + TITLE
    + """განაცხადის რეგისტრაციის ნომერი, მომზადების თარიღი: B90000002,
02/09/2026 16:07:35
სუბიექტი
საფირმო სახელწოდება: შპს გზის მშენებელი
სამართლებრივი
ფორმა: შეზღუდული პასუხისმგებლობის საზოგადოება
საიდენტიფიკაციო
ნომერი: 400000029
რეგისტრაციის ნომერი,
თარიღი: 01/04-2073; 03/02/2005
მარეგისტრირებელი
ორგანო: ბათუმის რ-ნის სასამართლო
იურიდიული მისამართი: საქართველო, ქალაქი ბათუმი, ტესტის ქუჩა, N51, სართული 2,
ბინა 4
რეორგანიზაციის ისტორია
http://public.reestri.gov.ge 1(2)
ინფორმაცია ლიკვიდაციის/ რეორგანიზაციის/ გადახდისუუნარობის
პროცესის მიმდინარეობის შესახებ
რეგისტრირებული არ არის
მმართველობის ორგანო
საერთო კრება
დირექტორი
ხელმძღვანელობა/წარმომადგენლობა
დირექტორი
დავით ბერიძე, 61000000044 ,ერთპიროვნული
კაპიტალი
ნებადართული კაპიტალი არ არის განსაზღვრული
პარტნიორები
მესაკუთრე რაოდენობა წილი
გიორგი ჭავჭავაძე,
61000000055 100 100%
ვალდებულება
რეგისტრირებული არ არის
"""
)


def test_the_2011_layout() -> None:
    entry, people = parse_extract(LAYOUT_2011)
    assert entry.id_code == "400000017"
    assert entry.name == "შპს ნიმუშის ჯგუფი"
    assert entry.legal_form == "შეზღუდული პასუხისმგებლობის საზოგადოება"
    # the address goes on on the next line, and stops at the next label
    assert entry.address == "საქართველო, ქ. თბილისი, მთაწმინდის რაიონი, ტესტის ქ. №7"
    assert entry.as_of == "2026-03-14"
    assert entry.source == "extract"
    assert entry.status is None
    # the founders are not authorised persons; each person's position follows them
    assert [(p.name, p.personal_number, p.position) for p in people] == [
        ("ნიკა სამხარაძე", "01000000011", "დირექტორი"),
        ("ანა ჯაფარიძე", "01000000022", "წარმომადგენელი"),
    ]
    assert entry.directors == ("ნიკა სამხარაძე", "ანა ჯაფარიძე")


def test_the_2025_layout() -> None:
    entry, people = parse_extract(LAYOUT_2025)
    # wrapped labels, "ნომერი" for the code, the date on the line after its label
    assert (entry.id_code, entry.name, entry.as_of) == (
        "400000029",
        "შპს გზის მშენებელი",
        "2026-09-02",
    )
    assert entry.legal_form == "შეზღუდული პასუხისმგებლობის საზოგადოება"
    assert entry.address == "საქართველო, ქალაქი ბათუმი, ტესტის ქუჩა, N51, სართული 2, ბინა 4"
    assert entry.status is None  # "რეგისტრირებული არ არის": nothing is under way
    # the position heads the list; the partners' list after it is not read
    assert [(p.name, p.position, p.representation) for p in people] == [
        ("დავით ბერიძე", "დირექტორი", "ერთპიროვნული")
    ]


def test_two_persons_in_the_2025_layout_take_the_position_above_each() -> None:
    block = """
დირექტორი
დავით ბერიძე, 61000000044 ,ერთობლივი
ფინანსური დირექტორი
ეკა ლომიძე, 61000000066 ,ერთობლივი
"""
    assert [(p.name, p.position) for p in persons(block)] == [
        ("დავით ბერიძე", "დირექტორი"),
        ("ეკა ლომიძე", "ფინანსური დირექტორი"),
    ]


def test_a_liquidation_under_way_is_the_status() -> None:
    text = LAYOUT_2025.replace(
        "პროცესის მიმდინარეობის შესახებ\nრეგისტრირებული არ არის",
        "პროცესის მიმდინარეობის შესახებ\nლიკვიდაციის პროცესი დაწყებულია 01/08/2026",
    )
    entry, _ = parse_extract(text)
    assert entry.status == "ლიკვიდაციის პროცესი დაწყებულია 01/08/2026"


@pytest.mark.parametrize(
    "text",
    [
        "ხელშეკრულება N5\nსაიდენტიფიკაციო კოდი: 400000017",
        # a contract that names a party by its labels is still no extract: no extract's title
        "ხელშეკრულება N5\nსაფირმო სახელწოდება: შპს ნიმუში\nსაიდენტიფიკაციო კოდი: 400000017",
        TITLE + "საფირმო სახელწოდება: შპს ნიმუში\n",  # no code
        TITLE + "საიდენტიფიკაციო კოდი: 400000017\n",  # no name
    ],
)
def test_what_is_not_an_extract_is_refused(text: str) -> None:
    with pytest.raises(NotAnExtractError):
        parse_extract(text)


def test_a_word_file_is_read_by_its_paragraphs(tmp_path: Path) -> None:
    doc = Document()
    for line in LAYOUT_2011.split("\n"):
        doc.add_paragraph(line)
    path = tmp_path / "extract.docx"
    doc.save(str(path))
    entry = read_extract(path, "docx")
    assert (entry.id_code, entry.directors) == ("400000017", ("ნიკა სამხარაძე", "ანა ჯაფარიძე"))
    with pytest.raises(NotAnExtractError):
        read_extract(tmp_path / "extract.docx", "xlsx")
    (tmp_path / "broken.docx").write_bytes(b"not a zip")
    with pytest.raises(NotAnExtractError):
        read_extract(tmp_path / "broken.docx", "docx")


# --- the snapshot's codes --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "restore", "code"),
    [
        ("406-444-789", False, "406444789"),
        ("406 444 789", False, "406444789"),
        ("1000000011", True, "01000000011"),  # a personal number Excel took for a number
        ("40000001", True, "040000001"),
        ("1000000011", False, "1000000011"),  # a document's code is kept as written
    ],
)
def test_a_code_is_compared_as_its_digits(written: str, restore: bool, code: str) -> None:
    assert normalize_code(written, restore_zero=restore) == code


def test_the_snapshot_finds_a_code_however_the_export_wrote_it(tmp_path: Path) -> None:
    csv_path = tmp_path / "r.csv"
    csv_path.write_text(
        "id_code,name,legal_form\n406-444-789,ტესტი,შეზღუდული პასუხისმგებლობის საზოგადოება\n"
        "1000000011,ი/მ ნიკა ტესტაძე,\n",
        encoding="utf-8",
    )
    db = tmp_path / "r.db"
    assert load_snapshot(csv_path, db, as_of="2026-09-01") == 2
    snap = open_snapshot(db)
    assert snap is not None
    with snap:
        found = snap.lookup("406 444 789")
        assert found is not None
        assert found.legal_form == "შეზღუდული პასუხისმგებლობის საზოგადოება"
        assert snap.lookup("01000000011") is not None


def test_a_snapshot_loaded_before_the_legal_form_column_still_reads(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            "CREATE TABLE registry_company (id_code TEXT PRIMARY KEY, name TEXT NOT NULL,"
            " address TEXT, directors TEXT, status TEXT, as_of TEXT);"
            "CREATE TABLE registry_meta (key TEXT PRIMARY KEY, value TEXT);"
            "INSERT INTO registry_company VALUES ('400000017','შპს ტესტი',NULL,NULL,NULL,NULL);"
        )
    snap = open_snapshot(db)
    assert snap is not None
    with snap:
        found = snap.lookup("400000017")
        assert found is not None and found.legal_form is None
