"""The string table. ``t()`` is lenient at runtime - a missing key returns the key rather than
raising, because a typo must not 500 a report someone is waiting on. These tests are the strict
half of that bargain."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from procurement_agent.checks.aoa import ContractorType
from procurement_agent.models import DocumentStatus, JobStatus
from procurement_agent.ui.i18n import DEFAULT_LANG, STRINGS, lang_of, t

TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "procurement_agent" / "ui" / "templates"
#: keys the templates build with `~`, so a source scan cannot see them whole
DYNAMIC_PREFIXES = (
    "contractor.",
    "report.severity.",
    "compare.kind.",
    "boq.diff.",
    "compare.show.",
    "task.",
    "status.",
    "jobkind.",
)


def _keys_used() -> set[str]:
    used: set[str] = set()
    for path in TEMPLATES.glob("*.html"):
        used |= set(re.findall(r"""t\(["']([a-z_.]+)["']""", path.read_text()))
    return {k for k in used if k not in DYNAMIC_PREFIXES}


def test_every_string_has_both_languages() -> None:
    missing = [k for k, (en, ka) in STRINGS.items() if not en.strip() or not ka.strip()]
    assert missing == []


def test_georgian_and_english_are_actually_different() -> None:
    """A pair that is the same string twice is almost always an untranslated placeholder."""
    same = [k for k, (en, ka) in STRINGS.items() if en == ka]
    assert same == []


def test_every_key_used_in_a_template_exists() -> None:
    assert sorted(k for k in _keys_used() if k not in STRINGS) == []


@pytest.mark.parametrize("value", ContractorType.__args__)
def test_every_contractor_type_has_a_label(value: str) -> None:
    """The <select> is built from get_args(ContractorType); a new type must not render blank."""
    assert f"contractor.{value}" in STRINGS


@pytest.mark.parametrize("severity", ["error", "warning", "info"])
def test_every_severity_has_a_label(severity: str) -> None:
    assert f"report.severity.{severity}" in STRINGS


def test_the_default_is_georgian() -> None:
    assert DEFAULT_LANG == "ka"
    assert lang_of(None) == "ka"
    assert lang_of("fr") == "ka"
    assert lang_of("en") == "en"


def test_an_unknown_key_returns_itself_rather_than_raising() -> None:
    assert t("no.such.key", "ka") == "no.such.key"


def test_the_placeholder_survives_translation() -> None:
    """upload.busy is formatted in JavaScript, so {n} must be in both languages."""
    en, ka = STRINGS["upload.busy"]
    assert "{n}" in en and "{n}" in ka


@pytest.mark.parametrize("value", [s.value for s in (*DocumentStatus, *JobStatus)])
def test_every_status_has_a_label(value: str) -> None:
    """Status pills are built as t("status." ~ value); a raw English enum in a Georgian page
    is exactly what this replaced."""
    assert f"status.{value}" in STRINGS


@pytest.mark.parametrize("kind", ["parse", "extract", "compare_explain", "aoa_review"])
def test_every_job_kind_has_a_label(kind: str) -> None:
    assert f"jobkind.{kind}" in STRINGS


@pytest.mark.parametrize("task", ["aoa", "boq", "compare"])
def test_every_picker_task_has_its_three_strings(task: str) -> None:
    for part in ("title", "lead", "needs"):
        assert f"task.{task}.{part}" in STRINGS


def test_the_not_applicable_placeholder_survives_translation() -> None:
    """The act report fills {rules} with the exempt rules' names, in either language."""
    en, ka = STRINGS["aoa.not_applicable"]
    assert "{rules}" in en and "{rules}" in ka


def test_the_upload_placeholders_survive_translation() -> None:
    """upload.wrong_type is formatted in JavaScript as well."""
    en, ka = STRINGS["upload.wrong_type"]
    for token in ("{name}", "{types}"):
        assert token in en and token in ka
