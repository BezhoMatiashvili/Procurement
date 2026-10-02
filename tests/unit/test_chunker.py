"""The chunker's orphan safety net, on synthetic documents built the way Docling builds them.

Docling's DOCX backend writes a table whose cells hold paragraphs as "rich" cells: each cell is
a group of paragraphs nested under the table, and a chunk lists only the table. The serializer
also drops the table's header corner cell. The safety net has to recover that corner cell -
it holds a signature block - without re-emitting every other cell a second time.
"""

from __future__ import annotations

from docling_core.types.doc.document import DoclingDocument, RichTableCell, TableCell, TableData
from docling_core.types.doc.labels import DocItemLabel, GroupLabel

from procurement_agent.checks.aoa import DocumentView
from procurement_agent.extraction.pipeline import chunks_text
from procurement_agent.ingestion.chunker import chunk_docling_document

PREAMBLE = [
    "შპს „ალფა ჯგუფი“, საიდენტიფიკაციო კოდი: 123456789 (შემდგომში „შემკვეთი“)",
    "შპს „ბეტა სერვისი“, საიდენტიფიკაციო კოდი: 987654321 (შემდგომში „შემსრულებელი“)",
]
#: A two-by-two signature table. The corner cell mixes a line repeated in the preamble (the
#: company name) with one found nowhere else (its director).
SIGNATURES = [
    [
        ["„შემკვეთი“", "შპს „ალფა ჯგუფი“", "დირექტორი ნინო კაპანაძე"],
        ["„შემსრულებელი“", "შპს „ბეტა სერვისი“", "დირექტორი: დავით მელაძე"],
    ],
    [["ხელმოწერა ____"], ["ხელმოწერა ____"]],
]
AFTER = "ბოლო აბზაცი ცხრილის შემდეგ"


def _signature_document() -> DoclingDocument:
    doc = DoclingDocument(name="synthetic")
    for line in PREAMBLE:
        doc.add_text(label=DocItemLabel.TEXT, text=line)
    table = doc.add_table(data=TableData(num_rows=2, num_cols=2))
    for r, row in enumerate(SIGNATURES):
        for c, lines in enumerate(row):
            group = doc.add_group(
                label=GroupLabel.UNSPECIFIED, name=f"rich_cell_group_1_{c}_{r}", parent=table
            )
            for line in lines:
                doc.add_text(label=DocItemLabel.TEXT, text=line, parent=group)
            doc.add_table_cell(
                table_item=table,
                cell=RichTableCell(
                    text="\n".join(lines),
                    start_row_offset_idx=r,
                    end_row_offset_idx=r + 1,
                    start_col_offset_idx=c,
                    end_col_offset_idx=c + 1,
                    column_header=r == 0,
                    ref=group.get_ref(),
                ),
            )
    doc.add_text(label=DocItemLabel.TEXT, text=AFTER)
    return doc


def _text(doc: DoclingDocument) -> str:
    return chunks_text(chunk_docling_document(doc, doc_id="t"))


def test_a_paragraph_nested_in_a_table_appears_once() -> None:
    text = _text(_signature_document())
    assert text.count("დირექტორი: დავით მელაძე") == 1
    assert text.count("შპს „ბეტა სერვისი“") == 2  # the preamble and the signature table


def test_the_corner_cell_the_serializer_drops_survives_whole_and_in_order() -> None:
    text = _text(_signature_document())
    assert text.count("დირექტორი ნინო კაპანაძე") == 1
    # its company line is also in the preamble, yet it is kept, right above its director
    assert "„შემკვეთი“\nშპს „ალფა ჯგუფი“\nდირექტორი ნინო კაპანაძე" in text


def test_a_paragraph_outside_the_table_appears_once() -> None:
    assert _text(_signature_document()).count(AFTER) == 1


def test_the_corner_cell_keeps_each_signatory_with_their_own_company() -> None:
    """Dropped line by line, the corner cell would lose its company name, and its director
    would be read as the signatory of the company named before it - the contractor."""
    parties = DocumentView.build("t", _text(_signature_document())).parties
    assert [(p.name, p.role, p.id_code, p.directors) for p in parties] == [
        ("შპს ალფა ჯგუფი", "შემკვეთი", "123456789", ["ნინო კაპანაძე"]),
        ("შპს ბეტა სერვისი", "შემსრულებელი", "987654321", ["დავით მელაძე"]),
    ]


def _plain_table_document(footnote: str) -> DoclingDocument:
    """A PDF-style table: plain cells, with a paragraph the layout model took for the table's
    footnote. Docling nests it under the table and lists it among the table's footnotes; the
    serializer then consumes it with the table and writes none of its text."""
    doc = DoclingDocument(name="synthetic")
    doc.add_heading(text="კომერციული წინადადება")
    cells = ["დასახელება", "ფასი", "კაბელი", "100"]
    data = TableData(
        num_rows=2,
        num_cols=2,
        table_cells=[
            TableCell(
                text=t,
                start_row_offset_idx=i // 2,
                end_row_offset_idx=i // 2 + 1,
                start_col_offset_idx=i % 2,
                end_col_offset_idx=i % 2 + 1,
                column_header=i < 2,
            )
            for i, t in enumerate(cells)
        ],
    )
    table = doc.add_table(data=data)
    note = doc.add_text(label=DocItemLabel.FOOTNOTE, text=footnote, parent=table)
    table.footnotes.append(note.get_ref())
    return doc


def test_a_note_under_a_table_that_its_text_lacks_is_kept() -> None:
    """The client's PDF offer: its price clause was read as a footnote under the table."""
    note = "1. ფასი და ღირებულება: 48 750,00 ლარს."
    assert _text(_plain_table_document(note)).count(note) == 1


def test_an_orphan_with_no_covered_ancestor_is_kept() -> None:
    """A heading lives in the chunk's headings, not in its body: nothing else carries it."""
    assert "კომერციული წინადადება" in _text(_plain_table_document("შენიშვნა"))


#: A key-terms table with no header row: Docling promotes its first row to the column header,
#: and the serializer drops that row's label - the corner cell - and writes its value into
#: every other row as a column name. (EXTRACTION-12)
KEY_TERMS = [
    ["ხელშეკრულების ღირებულება", "9 100 (ცხრა ათას ასი) ლარი, გადასახადების ჩათვლით"],
    ["საგარანტიო ვადა", "12 (თორმეტი) თვე"],
    ["ანგარიშსწორების ვადა", "5 (ხუთი) სამუშაო დღე"],
]


def _key_terms_document(
    rows: list[list[str]], *, rich: bool = False, header: bool = True
) -> DoclingDocument:
    """``header``: the first row marked as the column header, as Docling marks a Word table's
    and not always a PDF's."""
    doc = DoclingDocument(name="synthetic")
    doc.add_text(label=DocItemLabel.TEXT, text="4. ძირითადი პირობები:")
    table = doc.add_table(data=TableData(num_rows=len(rows), num_cols=2))
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            where = {
                "start_row_offset_idx": r,
                "end_row_offset_idx": r + 1,
                "start_col_offset_idx": c,
                "end_col_offset_idx": c + 1,
                "column_header": header and r == 0,
            }
            if rich:
                group = doc.add_group(
                    label=GroupLabel.UNSPECIFIED, name=f"rich_cell_group_1_{c}_{r}", parent=table
                )
                doc.add_text(label=DocItemLabel.TEXT, text=text, parent=group)
                cell: TableCell = RichTableCell(text=text, ref=group.get_ref(), **where)
            else:
                cell = TableCell(text=text, **where)
            doc.add_table_cell(table_item=table, cell=cell)
    doc.add_text(label=DocItemLabel.TEXT, text=AFTER)
    return doc


def test_a_first_row_taken_for_a_header_keeps_its_label_beside_its_value() -> None:
    text = _text(_key_terms_document(KEY_TERMS))
    line = "ხელშეკრულების ღირებულება: 9 100 (ცხრა ათას ასი) ლარი, გადასახადების ჩათვლით"
    assert text.count(line) == 1
    # in order: after the heading above the table, before the table's other rows
    assert text.index("4. ძირითადი პირობები:") < text.index(line) < text.index("საგარანტიო ვადა")


def test_a_first_row_taken_for_a_header_in_rich_cells_appears_once() -> None:
    text = _text(_key_terms_document(KEY_TERMS, rich=True))
    assert text.count("ხელშეკრულების ღირებულება") == 1


def test_a_real_header_row_adds_nothing() -> None:
    text = _text(_key_terms_document([["პირობა", "მნიშვნელობა"], *KEY_TERMS]))
    assert "პირობა:" not in text
    assert text.count("ხელშეკრულების ღირებულება") == 1


#: An act's requisites as a table: labels and their values, under a header that names the values
#: only as such (Test Set 4).
REQUISITES = [
    ["დასახელება", "მონაცემი"],
    ["დამკვეთის საიდენტიფიკაციო კოდი", "405284716"],
    ["პროექტის დასახელება", "„ვერხვების ბაღი“"],
]


def test_a_table_of_labels_and_values_is_written_one_row_to_a_line() -> None:
    """Docling's triplets put the header's word between each label and its value ("…კოდი,
    მონაცემი = 405284716"), which no reader reads: each row is a "label: value" line, and the
    header is no line of its own."""
    text = _text(_key_terms_document(REQUISITES))
    assert "დამკვეთის საიდენტიფიკაციო კოდი: 405284716" in text
    assert "პროექტის დასახელება: „ვერხვების ბაღი“" in text
    assert "მონაცემი" not in text


def test_a_table_of_labels_and_values_no_row_of_which_is_marked_the_header() -> None:
    """A PDF's table: Docling marked no row the header, and the header's words are a row like
    any other."""
    text = _text(_key_terms_document(REQUISITES, header=False))
    assert "დამკვეთის საიდენტიფიკაციო კოდი: 405284716" in text
    assert "მონაცემი" not in text


def test_a_two_column_table_with_no_header_is_written_one_row_to_a_line() -> None:
    text = _text(_key_terms_document(KEY_TERMS, header=False))
    assert "საგარანტიო ვადა: 12 (თორმეტი) თვე" in text


def test_a_two_column_table_under_a_real_header_keeps_its_column_names() -> None:
    """Near miss: a header that names what its column holds ("რაოდენობა") is no list of labels
    and values: the rows keep the column's name, as the price and service readers read them."""
    rows = [["მომსახურება", "რაოდენობა"], ["ფასადის ესკიზი", "2"], ["ფანჯრის ნახაზი", "4"]]
    text = _text(_key_terms_document(rows))
    assert "ფასადის ესკიზი, რაოდენობა = 2" in text


def test_the_contract_value_in_a_headerless_key_terms_table_is_the_total() -> None:
    from procurement_agent.config import Settings
    from procurement_agent.extraction.pipeline import run_extraction
    from procurement_agent.extraction.schemas import FactType

    chunks = chunk_docling_document(_key_terms_document(KEY_TERMS), doc_id="t")
    result = run_extraction(
        document_id="t",
        chunks=chunks,
        settings=Settings(ollama_base_url="http://localhost:11434"),
        llm=None,
    )
    totals = [f.normalized for f in result.facts if f.type == FactType.total_amount]
    assert totals == [9100.0]


# --- one paragraph longer than the budget ------------------------------------------------------


def _long_paragraph_document(text: str) -> DoclingDocument:
    doc = DoclingDocument(name="synthetic")
    doc.add_text(label=DocItemLabel.TEXT, text="შესავალი აბზაცი.")
    doc.add_text(label=DocItemLabel.TEXT, text=text)
    doc.add_text(label=DocItemLabel.TEXT, text="ბოლო აბზაცი.")
    return doc


def test_a_paragraph_longer_than_the_budget_is_split_not_a_crash() -> None:
    """``get_tokenizer()`` used to return None, which ``semchunk`` rejects with a TypeError the
    moment Docling had to split a single item: one long clause failed extraction and every check
    that reads the document's text."""
    from procurement_agent.ingestion.chunker import MAX_CHUNK_TOKENS
    from procurement_agent.tokens import estimate_tokens

    sentence = (
        "ხელშეკრულების მხარეები თანხმდებიან, რომ შესრულებული სამუშაოს ღირებულება გადახდილი იქნება. "
    )
    long = sentence * 400  # a single paragraph of about 36,000 characters
    chunks = chunk_docling_document(_long_paragraph_document(long), doc_id="d" * 32)
    assert len(chunks) >= 3
    assert all(estimate_tokens(c.body) <= MAX_CHUNK_TOKENS * 1.3 for c in chunks)
    body = " ".join(chunks_text(chunks).split())
    assert body.count("ღირებულება გადახდილი იქნება") == 400  # nothing lost, nothing repeated
    assert body.startswith("შესავალი აბზაცი.") and body.endswith("ბოლო აბზაცი.")


def test_a_long_run_of_whitespace_inside_a_paragraph_does_not_crash_either() -> None:
    text = "ღირებულება 100" + "\n" * 8000 + "თეთრი და სხვა ტექსტი"
    chunks = chunk_docling_document(_long_paragraph_document(text), doc_id="d" * 32)
    assert chunks
    assert "თეთრი და სხვა ტექსტი" in chunks_text(chunks)


def test_the_heuristic_tokenizer_hands_semchunk_a_callable_counter() -> None:
    from procurement_agent.ingestion.chunker import HeuristicTokenizer

    counter = HeuristicTokenizer().get_tokenizer()
    assert callable(counter)
    assert counter("ორი სიტყვა") == HeuristicTokenizer().count_tokens("ორი სიტყვა")
