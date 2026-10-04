"""A BOQ template nobody could read is an error that leads the report, not an `info` line among
the sheets skipped."""

from __future__ import annotations

from pathlib import Path

from procurement_agent.checks.boq import read_boq, template_findings
from tests.degraded_support import xlsx_bytes


def _template(tmp_path: Path, lines: list[str]) -> object:
    path = tmp_path / "template.xlsx"
    path.write_bytes(xlsx_bytes(lines))
    return read_boq(path, name="template")


def test_an_unrecognised_template_is_an_error_finding_first_in_the_list(tmp_path: Path) -> None:
    findings = template_findings(_template(tmp_path, ["just some words", "and more words"]))  # type: ignore[arg-type]
    assert findings, "the template problem must be said"
    first = findings[0]
    assert first.rule_id == "BOQ.TEMPLATE_UNRECOGNISED"
    assert first.severity == "error"
    assert first.message_ka and first.message_ka != first.message_en
    assert "template" in first.message_en.lower()
    assert [f.rule_id for f in findings].count("BOQ.TEMPLATE_UNRECOGNISED") == 1


def test_a_recognised_template_gets_no_such_finding(tmp_path: Path) -> None:
    from tests.boq_factory import blank, build_boq

    path = tmp_path / "t.xlsx"
    path.write_bytes(build_boq(blank(), priced=False, stated_total=None))
    template = read_boq(path, name="template")
    assert "BOQ.TEMPLATE_UNRECOGNISED" not in [f.rule_id for f in template_findings(template)]
