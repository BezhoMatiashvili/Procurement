"""Remove from a Word package what could reach out, or run something, when someone opens it.

``annotate_docx`` writes the uploaded file back with python-docx, which round-trips every part and
relationship it does not understand. What comes back is a file "produced by our system", handed to
a reviewer who opens it in Word - so whatever the upload carried rides along, now with that
credibility: an image or a template fetched from a URL when the file is opened (a tracking beacon;
on Windows, a ``\\\\host\\share`` path that sends the user's credentials), a field that includes
another file or starts a DDE conversation, an embedded OLE package, an ActiveX control, an
``altChunk`` that merges foreign content. Measured before this existed: python-docx's load and save
preserved an external image relationship, an external hyperlink, an ``attachedTemplate`` pointing at
``http://...dotm`` and an ``INCLUDEPICTURE`` field.

What is removed: every external relationship that is not an ``http``, ``https`` or ``mailto``
hyperlink; embedded objects, controls, ``altChunk`` and ``subDoc``; the settings that attach a
template, a mail-merge source, or ask Word to update fields on open; and fields whose instruction
includes or links other content, sends codes to the printer, or links to anything a relationship
could not (a ``HYPERLINK`` field to ``file:``, a share or a program's scheme) - they are replaced
by the text they last showed. What is kept: text, styles, comments, tables, embedded pictures and
ordinary hyperlinks - the reviewer's view of the document is the same.

Counts, never content, are reported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from docx.document import Document as DocumentObject
from docx.opc.part import XmlPart
from lxml import etree  # type: ignore[import-untyped]

__all__ = ["Removed", "neutralise_package"]

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

#: Hyperlink targets a reviewer may follow. Anything else (``file:``, ``\\host\share``,
#: ``ms-msdt:``, ``javascript:``, a bare path) is a way to make Word or the OS act.
_SAFE_LINK_SCHEMES = frozenset({"http", "https", "mailto"})

#: Relationship types removed even when internal: what they point at is executable or foreign
#: content, and the elements that reference them are removed with them.
_DROPPED_REL_TAILS = frozenset(
    {
        "oleObject",
        "package",
        "control",
        "activeXControlBinary",
        "aFChunk",
        "vbaProject",
        "vbaData",
        "subDocument",
        "mailMergeSource",
        "recipientData",
        "attachedTemplate",
        "frame",
    }
)

#: Elements removed outright, wherever they are.
_DROPPED_ELEMENTS = frozenset(
    {"object", "altChunk", "subDoc", "control", "OLEObject", "objectEmbed", "objectLink"}
)
#: Settings that attach a template, a mail-merge source, or run field updates on open.
_DROPPED_SETTINGS = frozenset({"attachedTemplate", "mailMerge", "updateFields"})
#: Attributes that point at an external target rather than at a part we keep.
_EXTERNAL_REF_ATTRS = (f"{_R}link", f"{_R}href")
#: Field instructions that pull in other content, start a conversation with another program, run
#: a command, or send codes to the printer. Compared case-insensitively against the first word of
#: the instruction, quoted or not.
_RISKY_FIELDS = frozenset(
    {
        "INCLUDEPICTURE",
        "INCLUDETEXT",
        "INCLUDE",
        "LINK",
        "DDE",
        "DDEAUTO",
        "IMPORT",
        "DATABASE",
        "MACROBUTTON",
        "RD",
        "EMBED",
        "PRINT",
    }
)
#: A ``HYPERLINK`` field's own arguments: its target, quoted or not, and its switches, ``\l``,
#: ``\m``, ``\n``, ``\o`` and ``\t`` (some with an argument of their own).
_FIELD_WORDS = re.compile(r'"([^"]*)"|(\\[A-Za-z])(?![^\s"])|(\S+)')
_SWITCHES_WITH_AN_ARGUMENT = frozenset({"\\l", "\\o", "\\t"})


@dataclass
class Removed:
    """What was taken out of one package (counts only)."""

    relationships: int = 0
    elements: int = 0
    fields: int = 0
    settings: int = 0
    references: int = 0
    kinds: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.relationships + self.elements + self.fields + self.settings + self.references


def _local(el: etree._Element) -> str:
    tag = el.tag
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def _should_drop(rel: object) -> bool:
    reltype = str(getattr(rel, "reltype", ""))
    tail = reltype.rsplit("/", 1)[-1]
    if tail in _DROPPED_REL_TAILS:
        return True
    if not getattr(rel, "is_external", False):
        return False
    if tail != "hyperlink":
        return True
    return not _safe_link(str(getattr(rel, "target_ref", "")))


def _safe_link(target: str) -> bool:
    """Whether a link's target is one a reviewer may follow (``_SAFE_LINK_SCHEMES``)."""
    scheme = urlparse(target.strip()).scheme.lower()
    # a one-letter "scheme" is a Windows drive (C:\...); empty is a relative or UNC path
    return scheme in _SAFE_LINK_SCHEMES


def _drop_relationships(owner: object) -> set[str]:
    """Drop the relationships of one part (or of the package); return the ids that went."""
    rels = owner.rels  # type: ignore[attr-defined]
    gone: set[str] = set()
    for rid, rel in list(rels.items()):
        if _should_drop(rel):
            rels.pop(rid)
            gone.add(rid)
    return gone


def _remove(el: etree._Element) -> None:
    parent = el.getparent()
    if parent is not None:
        parent.remove(el)


def _nearest(el: etree._Element, names: frozenset[str]) -> etree._Element | None:
    node = el.getparent()
    while node is not None:
        if _local(node) in names:
            return node
        node = node.getparent()
    return None


def _strip_elements(root: etree._Element) -> int:
    count = 0
    for el in [e for e in root.iter() if _local(e) in _DROPPED_ELEMENTS]:
        if el.getparent() is None:
            continue
        # a control or OLE object inside a VML picture: the picture goes too, not an empty shell
        shell = (
            _nearest(el, frozenset({"pict"})) if _local(el) in ("control", "OLEObject") else None
        )
        _remove(shell if shell is not None else el)
        count += 1
    return count


def _strip_settings(root: etree._Element) -> int:
    hits = [e for e in root.iter() if _local(e) in _DROPPED_SETTINGS]
    for el in hits:
        _remove(el)
    return len(hits)


def _is_risky(instruction: str) -> bool:
    words = instruction.strip().split(None, 1)
    if not words:
        return False
    kind = words[0].strip('"').upper()
    if kind == "HYPERLINK":
        return not _safe_field_link(words[1] if len(words) > 1 else "")
    return kind in _RISKY_FIELDS


def _safe_field_link(arguments: str) -> bool:
    """Whether a ``HYPERLINK`` field links where a hyperlink relationship may (``_safe_link``):
    its target, or none - a link to a place in the document itself (``\\l``) goes nowhere."""
    expect_argument = False
    for quoted, switch, bare in _FIELD_WORDS.findall(arguments):
        if switch:
            expect_argument = switch.lower() in _SWITCHES_WITH_AN_ARGUMENT
            continue
        if expect_argument:
            expect_argument = False
            continue
        target = quoted if quoted or not bare else bare
        # a field writes a backslash twice: "\\\\host\\share" is \\host\share
        return _safe_link(target.replace("\\\\", "\\"))
    return True


def _unwrap_fields(root: etree._Element) -> int:
    """Turn fields that include or link other content into the text they last showed."""
    count = 0
    # simple fields: <w:fldSimple w:instr="..."> children </w:fldSimple>
    for fld in [e for e in root.iter(f"{_W}fldSimple") if _is_risky(e.get(f"{_W}instr", ""))]:
        parent = fld.getparent()
        if parent is None:
            continue
        at = parent.index(fld)
        for child in list(fld):
            parent.insert(at, child)
            at += 1
        parent.remove(fld)
        count += 1
    # complex fields: begin ... instrText ... separate ... result ... end, possibly nested
    stack: list[dict[str, list[etree._Element] | bool]] = []
    risky: list[list[etree._Element]] = []
    for el in root.iter(f"{_W}fldChar", f"{_W}instrText"):
        if el.tag == f"{_W}instrText":
            if stack and not stack[-1]["separated"]:
                stack[-1]["texts"].append(el)  # type: ignore[union-attr]
            continue
        kind = el.get(f"{_W}fldCharType")
        if kind == "begin":
            stack.append({"chars": [el], "texts": [], "separated": False})
        elif stack and kind == "separate":
            stack[-1]["chars"].append(el)  # type: ignore[union-attr]
            stack[-1]["separated"] = True
        elif stack and kind == "end":
            field_ = stack.pop()
            field_["chars"].append(el)  # type: ignore[union-attr]
            texts: list[etree._Element] = field_["texts"]  # type: ignore[assignment]
            if _is_risky("".join(t.text or "" for t in texts)):
                risky.append([*field_["chars"], *texts])  # type: ignore[misc]
    for parts in risky:
        for el in parts:
            _remove(el)
        count += 1
    return count


def _fix_dangling(root: etree._Element, gone: set[str]) -> int:
    """Remove references to relationships that were dropped, so the package stays well formed."""
    count = 0
    for el in list(root.iter()):
        if el.getparent() is None and el is not root:
            continue
        for attr, value in list(el.attrib.items()):
            if not attr.startswith(_R) or value not in gone:
                continue
            if _local(el) == "hyperlink" or attr in _EXTERNAL_REF_ATTRS:
                del el.attrib[attr]
            else:
                shell = _nearest(el, frozenset({"drawing", "pict", "object"}))
                if shell is not None:
                    _remove(shell)
                else:
                    del el.attrib[attr]
            count += 1
    return count


def _strip_external_refs(root: etree._Element) -> int:
    """``r:link`` and ``r:href`` always name an external target; none is kept."""
    count = 0
    for el in root.iter():
        for attr in _EXTERNAL_REF_ATTRS:
            if attr in el.attrib:
                del el.attrib[attr]
                count += 1
    return count


def neutralise_package(document: DocumentObject) -> Removed:
    """Remove the reaching-out and running-something content of ``document`` in place."""
    removed = Removed()
    package = document.part.package
    parts = list(package.iter_parts())
    gone_by_owner: list[tuple[object, set[str]]] = [(package, _drop_relationships(package))]
    gone_by_owner += [(part, _drop_relationships(part)) for part in parts]
    removed.relationships = sum(len(g) for _, g in gone_by_owner)
    for owner, gone in gone_by_owner:
        if not isinstance(owner, XmlPart):
            continue
        root = owner.element
        removed.fields += _unwrap_fields(root)
        removed.elements += _strip_elements(root)
        removed.references += _strip_external_refs(root)
        removed.references += _fix_dangling(root, gone)
        if str(owner.partname).endswith("/settings.xml"):
            removed.settings += _strip_settings(root)
    for name in ("relationships", "elements", "fields", "settings", "references"):
        if getattr(removed, name):
            removed.kinds.append(name)
    return removed
