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
)
from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
from docling_core.transforms.chunker.tokenizer.base import BaseTokenizer
from docling_core.transforms.serializer.base import BaseDocSerializer
from docling_core.transforms.serializer.markdown import MarkdownParams
from docling_core.types.doc.base import BoundingBox, CoordOrigin, ImageRefMode
from docling_core.types.doc.common.content_layer import ContentLayer
from docling_core.types.doc.document import DoclingDocument
from docling_core.types.doc.items.table.table import TableItem
from docling_core.types.doc.items.text import TextItem
from docling_core.types.doc.labels import DocItemLabel

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


class AllLabelsSerializerProvider(ChunkingSerializerProvider):
    def get_serializer(self, doc: DoclingDocument) -> BaseDocSerializer:
        # Plain text for extraction: no **bold** markers (they break "100 (ასი) ლარს" and
        # "კოდი: 405123456" regexes), no markdown escaping, no image placeholders.
        return ChunkingDocSerializer(
            doc=doc,
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
    restored: set[str] = set()
    for dc in chunker.chunk(doc):
        meta = dc.meta
        if not isinstance(meta, DocMeta):  # pragma: no cover - HybridChunker always yields DocMeta
            raise TypeError("unexpected chunk metadata")
        body, contextual = dc.text, chunker.contextualize(dc)
        for listed in meta.doc_items:
            # a chunk lists its items by reference only: the table's cells are in the document
            item = listed.get_ref().resolve(doc)
            if isinstance(item, TableItem) and item.self_ref not in restored:
                # once per table: a long table split over chunks lists it in each
                restored.add(item.self_ref)
                body, contextual = _restore_first_row(item, body, contextual)
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


_DIGIT = re.compile(r"\d")


def _restore_first_row(table: TableItem, body: str, contextual: str) -> tuple[str, str]:
    """``body`` and ``contextual`` with a two-column table's first row written out as
    "label: value" when the serializer lost it.

    A key-terms table with no header row ("ხელშეკრულების ღირებულება | 9 100 ლარი") has its
    first row promoted to the column header. The serializer then drops that row's label - the
    corner cell - and writes its value into every other row as a column name, so the value is
    read with no label: the contract's price was no total. A real header names its columns
    and carries no figure ("პირობა | მნიშვნელობა"), so only a first row whose value has a
    digit is data, and only when its label is nowhere in the chunk. The line goes before the
    row that follows it, where the document has it.
    """
    grid = table.data.grid
    if table.data.num_cols != 2 or len(grid) < 2:
        return body, contextual
    label, value = (normalize_spaces(c.text) for c in grid[0][:2])
    if not label or not _DIGIT.search(value) or label in normalize_spaces(body):
        return body, contextual
    line = f"{label}: {value}"
    # the next row as the serializer writes it: its label, then the promoted value as its
    # column's name
    row = f"{normalize_spaces(grid[1][0].text)}, {value}"
    return _insert_before(body, line, row), _insert_before(contextual, line, row)


def _insert_before(text: str, line: str, row: str) -> str:
    """``text`` with ``line`` on a line of its own before the line holding ``row``, or at the
    end when ``row`` is not found as written."""
    at = text.find(row)
    if at < 0:
        return f"{text}\n{line}"
    start = text.rfind("\n", 0, at) + 1
    return f"{text[:start]}{line}\n{text[start:]}"


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
