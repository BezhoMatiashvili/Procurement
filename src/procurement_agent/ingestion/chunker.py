"""Chunks: the unit of text handed to rules and to the LLM, always with provenance.

Docling's ``HybridChunker`` is structure-aware (headings, tables, lists) but its default tokenizer
downloads a HuggingFace model. We are air-gapped, so we plug in a heuristic tokenizer that
over-estimates token counts for Georgian text (which tokenizes poorly in most LLMs).
"""

from __future__ import annotations

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
from docling_core.types.doc.items.text import TextItem
from docling_core.types.doc.labels import DocItemLabel

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
    covered: set[str] = set()
    for dc in chunker.chunk(doc):
        meta = dc.meta
        if not isinstance(meta, DocMeta):  # pragma: no cover - HybridChunker always yields DocMeta
            raise TypeError("unexpected chunk metadata")
        covered.update(item.self_ref for item in meta.doc_items)
        contextual = chunker.contextualize(dc)
        _append(chunks, doc, doc_id, contextual, dc.text, meta.doc_items, list(meta.headings or []))

    # Safety net: Docling's serializer silently drops some items (e.g. text the layout model
    # labelled as a footnote). Anything with text that no chunk covers is emitted on its own, so
    # that no part of the document can escape extraction.
    orphans: list[TextItem] = []
    for item, _ in doc.iterate_items(included_content_layers=_ALL_LAYERS):
        if isinstance(item, TextItem) and item.self_ref not in covered and item.text.strip():
            orphans.append(item)
    if orphans:
        batch: list[TextItem] = []
        for item in orphans:
            if batch and estimate_tokens("\n".join(t.text for t in [*batch, item])) > max_tokens:
                _flush_orphans(chunks, doc, doc_id, batch)
                batch = []
            batch.append(item)
        _flush_orphans(chunks, doc, doc_id, batch)
    return chunks


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
