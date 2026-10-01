"""Chunks: the unit of text handed to rules and to the LLM, always with provenance.

Docling's ``HybridChunker`` is structure-aware (headings, tables, lists) but its default tokenizer
downloads a HuggingFace model. We are air-gapped, so we plug in a heuristic tokenizer that
over-estimates token counts for Georgian text (which tokenizes poorly in most LLMs).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from docling_core.transforms.chunker.doc_chunk import DocMeta
from docling_core.transforms.chunker.hierarchical_chunker import (
    ChunkingDocSerializer,
    ChunkingSerializerProvider,
    TripletTableSerializer,
)
from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
from docling_core.transforms.chunker.tokenizer.base import BaseTokenizer
from docling_core.transforms.serializer.base import BaseDocSerializer, SerializationResult
from docling_core.transforms.serializer.common import create_ser_result
from docling_core.transforms.serializer.markdown import MarkdownParams
from docling_core.types.doc.base import BoundingBox, CoordOrigin, ImageRefMode
from docling_core.types.doc.common.content_layer import ContentLayer
from docling_core.types.doc.document import DoclingDocument
from docling_core.types.doc.items.table.table import TableItem
from docling_core.types.doc.items.text import TextItem
from docling_core.types.doc.labels import DocItemLabel

from procurement_agent.extraction.anchors import party_column
from procurement_agent.extraction.normalize import normalize_spaces
from procurement_agent.extraction.schemas import Provenance
from procurement_agent.tokens import estimate_tokens

__all__ = [
    "MAX_CHUNK_TOKENS",
    "Chunk",
    "chunk_docling_document",
    "estimate_tokens",
    "provenance_of",
]

#: Chunk size, in *estimated* tokens. The estimate deliberately over-counts (see ``tokens``),
#: so this is not the real size: measured on gemma3:4b, a full chunk at 1550 estimated tokens
#: is about 1130 real ones. That real size is the one to hold on to - it is what the chunks
#: measured before the estimate was corrected, and it sits at the boundary above which the
#: inference server was observed to reuse a prompt prefix (see ``llm.ollama_client``). When
#: the estimate was corrected, a limit left at 1200 would have cut real chunks to about 880.
MAX_CHUNK_TOKENS = 1550


class HeuristicTokenizer(BaseTokenizer):
    max_tokens: int = MAX_CHUNK_TOKENS

    def count_tokens(self, text: str) -> int:
        return estimate_tokens(text)

    def get_max_tokens(self) -> int:
        return self.max_tokens

    def get_tokenizer(self) -> Any:
        return None


# Docling's default export drops footnotes, captions and page headers/footers. Its layout model
# regularly mislabels ordinary paragraphs as footnotes, and headers/footers often carry contract
# numbers and dates — so for extraction we keep every text-bearing label.
_ALL_LABELS: set[DocItemLabel] = set(DocItemLabel)
_ALL_LAYERS: set[ContentLayer] = {ContentLayer.BODY, ContentLayer.FURNITURE}


class PartyColumnsTableSerializer(TripletTableSerializer):
    """Docling's triplet serialisation ("<row label>, <column header> = <cell>"), except for two
    layouts it misreads: a table laid out one party per column, written column by column, and a
    two-column table whose first row is data, written row by row as "label: value".

    A requisites or signature grid - the customer's column beside the contractor's, their
    names, codes, accounts and directors row by row - written row by row puts each party's
    requisite right after the other party's name: "შპს „ა“, „შემსრულებელი“ = შპს „ბ“. ს/კ: 1,
    „შემსრულებელი“ = ს/კ: 2". Every reader that takes what follows a name as that party's then
    gives the customer's code to the contractor (red team R2, AOA-R2-02), and an address runs into
    the next column's (EXTRACTION-R2-11). Written column by column, each party's cells follow
    its own name, and each cell ends with ";" - a line break alone is lost when the act check
    flattens the text, and the readers already end an address or a name at ";".

    The layout is known by a row with two or more different sides ("„შემკვეთი“") or two or more
    different party names in different columns (``anchors.party_column``). A role and a name in
    one row are one party, a table with a party per row, and stay in triplets. Columns before
    the first party's are row labels ("ს/კ", "მისამართი"): each party's cell is written after
    its row's label.

    A key-terms table or a heading laid out in two columns has no header row, yet Docling
    promotes its first row to the column header (``_first_row_is_data``). The triplets then drop
    that row's label and write its value into every other row as a column name: the price table
    lost its label (EXTRACTION-12), every row carried the first row's figures and deadline - a
    false penalty "30%" beside the penalty's own rate (EXTRACTION-R3-02) - and a title with no
    figure in its second cell was gone (EXTRACTION-R3-03). Such a table is written one row to
    a line, each cell once, as the document has it.
    """

    def serialize(
        self,
        *,
        item: TableItem,
        doc_serializer: BaseDocSerializer,
        doc: DoclingDocument,
        **kwargs: Any,
    ) -> SerializationResult:
        first = _party_columns(item)
        by_rows = first is None and _first_row_is_data(item)
        keyed = first is None and not by_rows and _keyed_by_item_number(item)
        if (
            first is None and not by_rows and not keyed
        ) or item.self_ref in doc_serializer.get_excluded_refs(**kwargs):
            return super().serialize(item=item, doc_serializer=doc_serializer, doc=doc, **kwargs)
        parts: list[SerializationResult] = []
        captions = doc_serializer.serialize_captions(item=item, **kwargs)
        if captions.text:
            parts.append(captions)
        shared = kwargs.get("visited")
        local = {**kwargs, "visited": set(shared)} if shared is not None else kwargs
        texts = [
            [
                _lines(cell._get_text(doc=doc, doc_serializer=doc_serializer, **local))
                if cell.start_row_offset_idx == r and cell.start_col_offset_idx == c
                else ""  # a merged cell is written once, where it starts
                for c, cell in enumerate(row)
            ]
            for r, row in enumerate(item.data.grid)
        ]
        if keyed:
            text = _keyed_rows(texts)
        else:
            text = _by_row(texts) if first is None else _by_column(texts, first)
        if text:
            if shared is not None:
                shared.update(local["visited"])
            parts.append(create_ser_result(text=text, span_source=item))
        return create_ser_result(text="\n\n".join(p.text for p in parts), span_source=parts)


def _lines(text: str) -> str:
    """A cell's paragraphs one to a line: the serializer puts a blank line between them."""
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _party_columns(table: TableItem) -> int | None:
    """The first party's column of a table laid out one party per column, or None."""
    for row in table.data.grid:
        found: dict[str, dict[str, int]] = {}
        for c, cell in enumerate(row):
            if cell.start_col_offset_idx != c or (column := party_column(cell.text)) is None:
                continue
            kind, value = column
            found.setdefault(kind, {}).setdefault(value, c)
        # two different sides, or two different names: a role beside a name is one party
        columns = [
            c for by_value in found.values() if len(by_value) >= 2 for c in by_value.values()
        ]
        if columns:
            return min(columns)
    return None


def _by_column(texts: list[list[str]], first: int) -> str:
    """The grid's cells column by column, each ended with ";", the row-label columns before
    ``first`` written as each cell's label."""
    blocks: list[str] = []
    for c in range(first, len(texts[0]) if texts else 0):
        lines: list[str] = []
        for row in texts:
            cell = row[c]
            if not cell:
                continue
            label = " ".join(x for x in row[:first] if x).rstrip(" :")
            line = f"{label}: {cell}" if label else cell
            lines.append(line if line.endswith(";") else f"{line};")
        if lines:
            blocks.append("\n".join(lines))
    return "\n".join(blocks)


_DIGIT = re.compile(r"\d")


def _first_row_is_data(table: TableItem) -> bool:
    """Whether a two-column table's first row, which Docling promoted to the column header, is
    data: a real header names both its columns and carries no figure ("პირობა | მნიშვნელობა").
    A first row with a digit in either cell ("ხელშეკრულება N 27/2026", "ღირებულება | 9 100
    ლარი"), or a label with no second cell (a title, alone or merged across the row), is not
    one. A first row with an empty label cell is left to Docling: a header may leave the
    corner blank."""
    grid = table.data.grid
    if table.data.num_cols != 2 or len(grid) < 2:
        return False
    first = grid[0]
    if not any(cell.column_header and cell.start_row_offset_idx == 0 for cell in first):
        return False
    label = normalize_spaces(first[0].text)
    # a cell merged across both columns is one cell, written in the first
    value = "" if first[1].start_col_offset_idx == 0 else normalize_spaces(first[1].text)
    if not label:
        return False
    return not value or bool(_DIGIT.search(label) or _DIGIT.search(value))


def _by_row(texts: list[list[str]]) -> str:
    """The grid one row to a line: "label: value", or a row's one cell alone, each ended with
    ";" as ``_by_column`` ends a cell. A row is a clause of its own: with a line break alone
    between them, the balance's deadline was read into the advance's sentence and lost."""
    lines: list[str] = []
    for row in texts:
        cells = [cell for cell in row if cell]
        if not cells:
            continue
        *labels, last = cells
        head = " ".join(label.rstrip(" :") for label in labels)
        line = f"{head}: {last}" if head else last
        lines.append(line if line.endswith((".", ";", ":", "!", "?")) else f"{line};")
    return "\n".join(lines)


#: A row key that is an item number: "1", "2.", "1.1", "3)", "ა", "ბ)".
_ITEM_NUMBER = re.compile(r"\s*(?:\d{1,3}(?:\.\d{1,3})*[.)]?|[ა-ჰ][.)]?)\s*")
#: A column header of a price table - a price, a quantity, a unit, a sum: its rows are lines
#: the rules read cell by cell ("1, ჯამი = 500"), and stay as Docling writes them.
_PRICED_HEADER = re.compile(
    r"ფას|რაოდენობ|ერთეულ|ჯამ|ღირებულ|თანხ|ლარ|დოლარ|ევრო|სულ|[₾$€]"
    r"|(?<![a-z])(?:price|rate|quantity|qty|unit|amount|total|sum|cost|gel|usd|eur)",
    re.IGNORECASE,
)


def _keyed_by_item_number(table: TableItem) -> bool:
    """Whether a table of three or more columns under one header row keys each row by an item
    number in its first column ("N | პირობა | მნიშვნელობა", red team R4, EXTRACTION-R4-07),
    and is no price table (``_PRICED_HEADER``). Docling's triplets put a full stop between one
    row's cells ("1, პირობა = ხელშეკრულების ღირებულება. 1, მნიშვნელობა = 36 000 ლარი"), so a
    term's label and its value were two sentences and every value went unread."""
    grid = table.data.grid
    if table.data.num_cols < 3 or len(grid) < 3:
        return False
    header, *rows = grid
    if not any(cell.column_header for cell in header) or any(
        cell.column_header for row in rows for cell in row
    ):
        return False
    if any(_PRICED_HEADER.search(cell.text) for cell in header[1:]):
        return False
    for r, row in enumerate(rows, start=1):
        if any(
            cell.start_row_offset_idx != r or cell.start_col_offset_idx != c
            for c, cell in enumerate(row)
        ):
            return False  # a merged cell: left to Docling
        if not _ITEM_NUMBER.fullmatch(row[0].text):
            return False
    return True


def _keyed_rows(texts: list[list[str]]) -> str:
    """A table keyed by item numbers one row to a line, Docling's triplets kept but joined by
    ", " within the row: "1, პირობა = ხელშეკრულების ღირებულება, მნიშვნელობა = 36 000 ლარი;". A
    row is one sentence, its label beside its value, and ends with ";" as ``_by_row``'s does."""
    header, *rows = texts
    names = [" ".join(cell.split()) for cell in header]
    lines: list[str] = []
    for row in rows:
        cells = [
            f"{names[c]} = {' '.join(cell.split())}" if names[c] else " ".join(cell.split())
            for c, cell in enumerate(row)
            if c and cell
        ]
        if not cells:
            continue
        line = ", ".join([" ".join(row[0].split()), *cells])
        lines.append(line if line.endswith((".", ";", ":", "!", "?")) else f"{line};")
    return "\n".join(lines)


class AllLabelsSerializerProvider(ChunkingSerializerProvider):
    def get_serializer(self, doc: DoclingDocument) -> BaseDocSerializer:
        # Plain text for extraction: no **bold** markers (they break "100 (ასი) ლარს" and
        # "კოდი: 405123456" regexes), no markdown escaping, no image placeholders.
        return ChunkingDocSerializer(
            doc=doc,
            table_serializer=PartyColumnsTableSerializer(),
            params=MarkdownParams(
                labels=_ALL_LABELS,
                layers=_ALL_LAYERS,
                include_formatting=False,
                include_hyperlinks=False,
                escape_underscores=False,
                escape_html=False,
                image_mode=ImageRefMode.PLACEHOLDER,
                image_placeholder="",
            ),
        )


@dataclass(slots=True)
class Chunk:
    id: str
    text: str  # what the LLM sees (may include heading context)
    body: str  # verbatim text used for raw_text verification
    provenance: Provenance
    headings: list[str] = field(default_factory=list)
    token_estimate: int = 0


def _to_top_left(bbox: BoundingBox, page_height: float | None) -> tuple[float, float, float, float]:
    if bbox.coord_origin == CoordOrigin.BOTTOMLEFT and page_height is not None:
        return (bbox.l, page_height - bbox.t, bbox.r, page_height - bbox.b)
    return (bbox.l, bbox.t, bbox.r, bbox.b)


def _union(boxes: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def chunk_docling_document(
    doc: DoclingDocument, *, doc_id: str, max_tokens: int = MAX_CHUNK_TOKENS
) -> list[Chunk]:
    chunker = HybridChunker(
        tokenizer=HeuristicTokenizer(max_tokens=max_tokens),
        merge_peers=True,
        serializer_provider=AllLabelsSerializerProvider(),
    )
    chunks: list[Chunk] = []
    #: every item a chunk lists, with the text of each chunk that lists it
    cover: dict[str, list[str]] = {}
    for dc in chunker.chunk(doc):
        meta = dc.meta
        if not isinstance(meta, DocMeta):  # pragma: no cover - HybridChunker always yields DocMeta
            raise TypeError("unexpected chunk metadata")
        body, contextual = dc.text, chunker.contextualize(dc)
        for listed in meta.doc_items:
            cover.setdefault(listed.self_ref, []).append(body)
        _append(chunks, doc, doc_id, contextual, body, meta.doc_items, list(meta.headings or []))

    # Safety net: Docling's serializer silently drops some items (e.g. text the layout model
    # labelled as a footnote). Anything with text that no chunk carries is emitted on its own,
    # so that no part of the document can escape extraction.
    orphans: list[TextItem] = []
    for item, _ in doc.iterate_items(included_content_layers=_ALL_LAYERS):
        if isinstance(item, TextItem) and item.self_ref not in cover and item.text.strip():
            orphans.append(item)
    orphans = _not_yet_carried(doc, orphans, cover)
    if orphans:
        batch: list[TextItem] = []
        for item in orphans:
            if batch and estimate_tokens("\n".join(t.text for t in [*batch, item])) > max_tokens:
                _flush_orphans(chunks, doc, doc_id, batch)
                batch = []
            batch.append(item)
        _flush_orphans(chunks, doc, doc_id, batch)
    return chunks


def _not_yet_carried(
    doc: DoclingDocument, orphans: list[TextItem], cover: dict[str, list[str]]
) -> list[TextItem]:
    """The orphans whose text no chunk carries yet.

    Docling's DOCX backend nests a table's content as paragraphs under the table, and a chunk
    lists only the table itself. Every such paragraph therefore looked orphaned although the
    table's serialised text already held it: on the client's acts 33 of the 39 orphans were
    copies, 29% of the text, and each mistake in them was reported twice.

    The unit of the test is the cell group directly under the nearest ancestor a chunk lists -
    never a single paragraph. The serializer drops a table's header corner cell, and that cell
    can mix a line repeated elsewhere (a company name) with one that is not (its director).
    Filtered paragraph by paragraph, the company line went, and the director was read as the
    signatory of whichever company came before it. So a group is dropped whole when its text
    is inside the covering chunks' text, and kept whole otherwise. An orphan with no covered
    ancestor has nothing to be a copy of and is always kept.
    """
    kept: list[TextItem] = []
    carried: dict[str, bool] = {}
    # each covered ancestor's text, normalised once: a table has a group per cell, and joining
    # its chunks again for every cell made a long table quadratic
    covering: dict[str, str] = {}
    for item in orphans:
        group, anchor = _cell_group(doc, item, cover)
        if anchor is None:
            kept.append(item)
            continue
        if group.self_ref not in carried:
            text = normalize_spaces(
                " ".join(
                    t.text
                    for t, _ in doc.iterate_items(root=group, included_content_layers=_ALL_LAYERS)
                    if isinstance(t, TextItem)
                )
            )
            if anchor not in covering:
                covering[anchor] = normalize_spaces(" ".join(cover[anchor]))
            # an empty group is contained in anything: never let that count as a copy
            carried[group.self_ref] = bool(text) and text in covering[anchor]
        if not carried[group.self_ref]:
            kept.append(item)
    return kept


def _cell_group(
    doc: DoclingDocument, item: TextItem, cover: dict[str, list[str]]
) -> tuple[Any, str | None]:
    """(the node directly under ``item``'s nearest covered ancestor, that ancestor's ref).

    The ref is None when no ancestor is covered; the node is then the topmost one reached.
    """
    child: Any = item
    parent = item.parent
    while parent is not None:
        node = parent.resolve(doc)
        if node.self_ref in cover:
            return child, node.self_ref
        child, parent = node, node.parent
    return child, None


def _flush_orphans(
    chunks: list[Chunk], doc: DoclingDocument, doc_id: str, batch: list[TextItem]
) -> None:
    text = "\n".join(t.text for t in batch)
    _append(chunks, doc, doc_id, text, text, list(batch), [])


def provenance_of(
    doc: DoclingDocument, items: Sequence[Any], *, chunk_id: str | None = None
) -> Provenance:
    """Page and bounding box covering ``items``, in top-left coordinates.

    DOCX carries no provenance at all (docling emits an empty ``pages`` map and no ``prov``),
    so this legitimately returns an empty ``Provenance`` for Word input. Shared with the
    segmenter so a fact and a diff hunk cannot disagree about where they are.
    """
    page: int | None = None
    boxes: list[tuple[float, float, float, float]] = []
    for item in items:
        for prov in getattr(item, "prov", []):
            if page is None:
                page = prov.page_no
            if prov.page_no != page:
                continue  # bbox only for the first page of a multi-page chunk
            height = None
            pg = doc.pages.get(prov.page_no)
            if pg is not None and pg.size is not None:
                height = pg.size.height
            boxes.append(_to_top_left(prov.bbox, height))
    return Provenance(page=page, bbox=_union(boxes) if boxes else None, chunk_id=chunk_id)


def _append(
    chunks: list[Chunk],
    doc: DoclingDocument,
    doc_id: str,
    text: str,
    body: str,
    items: list[Any],
    headings: list[str],
) -> None:
    cid = f"{doc_id}:{len(chunks)}"
    chunks.append(
        Chunk(
            id=cid,
            text=text,
            body=body,
            provenance=provenance_of(doc, items, chunk_id=cid),
            headings=headings,
            token_estimate=estimate_tokens(text),
        )
    )
