"""Defects the fourth red-team round found in the act check's signatory, date, fee and period
rules.

Each test states the defect's id (``samples/redteam/aoa/CONFIRMED_R4.md``) and the behaviour the
spec asks for, and every widened reading has a near miss beside it that must still not match.
Synthetic text only: the pair is ``test_aoa_check``'s, with every name, code and account invented.
"""

from __future__ import annotations

from pathlib import Path

from procurement_agent.checks.aoa import check_aoa
from procurement_agent.checks.findings import CheckReport, Finding
from tests.unit.test_aoa_check import ACT, CONTRACT


def _check(act: str = ACT, contract: str | None = CONTRACT, **kw: object) -> CheckReport:
    return check_aoa(aoa_text=act, contract_text=contract, **kw)  # type: ignore[arg-type]


def _of(report: CheckReport, rule_id: str) -> list[Finding]:
    return [f for f in report.findings if f.rule_id == rule_id]


def _substantive(report: CheckReport) -> list[Finding]:
    return [f for f in report.findings if f.severity in ("error", "warning")]


def _annotated(tmp_path: Path, act: str, findings: list[Finding], *, text: bool) -> list[str]:
    """The words each comment covers once the act (one paragraph per line) is annotated."""
    from docx import Document

    from procurement_agent.eval.score import comment_anchors
    from procurement_agent.report.docx_annotate import annotate_docx

    doc = Document()
    for line in act.splitlines():
        doc.add_paragraph(line)
    src = tmp_path / "act.docx"
    doc.save(src)
    out = tmp_path / "annotated.docx"
    annotate_docx(src, findings, out, text=act if text else None)
    return sorted(comment_anchors(out).values())


def _commented_lines(tmp_path: Path, act: str, findings: list[Finding]) -> list[str]:
    """The act's lines that carry a comment once it is annotated."""
    from docx import Document

    from procurement_agent.report.docx_annotate import annotate_docx

    doc = Document()
    for line in act.splitlines():
        doc.add_paragraph(line)
    src = tmp_path / "lines.docx"
    doc.save(src)
    out = tmp_path / "lines_annotated.docx"
    annotate_docx(src, findings, out)
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    marked: list[str] = []
    for p in Document(str(out)).paragraphs:
        if p._p.findall(f".//{w}commentRangeStart"):
            marked.append(p.text)
    return marked


# --- AOA-R4-01: a name copied into one party's signature alone -----------------------------------

#: The act with the contractor's director named in its preamble too.
_TWO_DIRECTORS = ACT.replace(
    "ჭავჭავაძის 5 (შემდგომში „შემსრულებელი“)",
    "ჭავჭავაძის 5 (შემდგომში „შემსრულებელი“) წარმოდგენილი დირექტორის დავით მელაძის სახით",
)
#: The customer's director written at the contractor's signature only; the preamble is right.
_SIGNATURE_ONLY = _TWO_DIRECTORS.replace("დირექტორი: დავით მელაძე", "დირექტორი: ნინო კაპანაძე")


class TestCopiedIntoTheSignatureOnly:
    def test_one_finding_on_the_contractor_s_signature(self) -> None:
        report = _check(_SIGNATURE_ONLY)
        found = [(f.quote, f.expected) for f in _of(report, "AOA.DIRECTOR")]
        assert found == [("ნინო კაპანაძე", "დავით მელაძე")]
        [f] = _of(report, "AOA.DIRECTOR")
        assert f.span is not None
        assert f.span[0] > _SIGNATURE_ONLY.index("„შემსრულებელი“\nშპს")

    def test_the_customer_s_own_preamble_is_not_marked(self, tmp_path: Path) -> None:
        findings = _of(_check(_SIGNATURE_ONLY), "AOA.DIRECTOR")
        for text in (False, True):
            assert _annotated(tmp_path, _SIGNATURE_ONLY, findings, text=text) == [
                "ნინო კაპანაძე"
            ], text
        assert _commented_lines(tmp_path, _SIGNATURE_ONLY, findings) == [
            "დირექტორი: ნინო კაპანაძე"
        ]

    def test_a_copy_in_the_contractor_s_preamble_alone_is_still_reported(self) -> None:
        """Near miss: the genitive copied into the contractor's preamble stands in the
        contractor's block, and is that party's mistake."""
        act = _TWO_DIRECTORS.replace(
            "„შემსრულებელი“) წარმოდგენილი დირექტორის დავით მელაძის",
            "„შემსრულებელი“) წარმოდგენილი დირექტორის ნინო კაპანაძის",
        )
        found = [(f.quote, f.expected) for f in _of(_check(act), "AOA.DIRECTOR")]
        assert found == [("ნინო კაპანაძის", "დავით მელაძის")]

    def test_a_name_nobody_signs_is_still_reported_everywhere(self) -> None:
        """Near miss: an unrelated person at the signature is in no other party's block."""
        act = _TWO_DIRECTORS.replace("დირექტორი: დავით მელაძე", "დირექტორი: ზურაბ ქობალია")
        found = [(f.quote, f.places) for f in _of(_check(act), "AOA.DIRECTOR")]
        assert found == [("ზურაბ ქობალია", None)]
