"""``safe_exc`` and the exceptions allowed to keep their messages."""

from __future__ import annotations

import importlib
import pkgutil

import pytest
from pydantic import BaseModel, ValidationError

import procurement_agent
from procurement_agent.security.safe_errors import ContentFree, safe_exc

CANARY = "CANARY-9f3a-ლარი-12 345"


def _all_modules() -> None:
    for module in pkgutil.walk_packages(procurement_agent.__path__, "procurement_agent."):
        if ".eval." in module.name or module.name.endswith(".cli"):
            continue
        importlib.import_module(module.name)


def _subclasses(base: type) -> set[type]:
    found: set[type] = set()
    for sub in base.__subclasses__():
        found.add(sub)
        found |= _subclasses(sub)
    return found


@pytest.mark.parametrize(
    "error",
    [
        ValueError(CANARY),
        KeyError(CANARY),
        RuntimeError(f"could not parse {CANARY!r}"),
        UnicodeDecodeError("utf-8", CANARY.encode(), 0, 1, CANARY),
        AssertionError(CANARY),
        Exception(CANARY, CANARY),
    ],
)
def test_an_exception_is_described_by_its_kind_alone(error: BaseException) -> None:
    text = safe_exc(error)
    assert CANARY not in text
    assert "CANARY" not in text
    assert text == type(error).__name__


def test_a_validation_error_loses_its_input_and_keeps_its_shape() -> None:
    class Fact(BaseModel):
        value: int
        unit: str

    with pytest.raises(ValidationError) as raised:
        Fact.model_validate({"value": CANARY, "unit": 5, CANARY: CANARY})
    text = safe_exc(raised.value)
    assert "CANARY" not in text
    assert "ლარი" not in text
    assert text == "ValidationError(2 error(s): int_parsing, string_type)"


def test_the_error_kinds_of_a_validation_error_are_capped() -> None:
    class Wide(BaseModel):
        a: int
        b: str
        c: list[int]
        d: dict[str, int]
        e: bool
        f: float
        g: set[int]

    with pytest.raises(ValidationError) as raised:
        Wide.model_validate({"a": "x", "b": 1, "c": 1, "d": 1, "e": "maybe", "f": "x", "g": 1})
    text = safe_exc(raised.value)
    assert text.startswith("ValidationError(7 error(s): ")
    assert text.count(",") <= 5  # five kinds shown, then an ellipsis


def test_an_os_error_loses_the_path_it_names() -> None:
    error = FileNotFoundError(2, "No such file or directory", f"/data/uploads/{CANARY}.docx")
    assert safe_exc(error) == "FileNotFoundError(errno=ENOENT)"
    assert safe_exc(PermissionError(13, "denied", CANARY)) == "PermissionError(errno=EACCES)"
    assert safe_exc(OSError(CANARY)) == "OSError"  # no errno, no detail


def test_an_http_status_error_keeps_the_status_and_nothing_else() -> None:
    class Response:
        status_code = 503
        text = CANARY

    class HTTPStatusError(Exception):
        response = Response()

    assert safe_exc(HTTPStatusError(CANARY)) == "HTTPStatusError(503)"


def test_the_chain_of_causes_is_named_by_kind() -> None:
    try:
        try:
            raise OSError(5, "io", CANARY)
        except OSError as inner:
            raise ValueError(CANARY) from inner
    except ValueError as outer:
        assert safe_exc(outer) == "ValueError (caused by OSError)"
    # at most three causes, however long the chain
    error: BaseException = ValueError("a")
    for _ in range(10):
        try:
            raise RuntimeError(CANARY) from error
        except RuntimeError as e:
            error = e
    assert safe_exc(error).count("RuntimeError") == 4  # itself and three causes


def test_a_content_free_class_keeps_its_message_cut_short() -> None:
    class OurError(ContentFree, RuntimeError):
        pass

    assert safe_exc(OurError("the file has 9,000 segments;\n more than 5,000")) == (
        "OurError: the file has 9,000 segments; more than 5,000"
    )
    assert len(safe_exc(OurError("x" * 1000))) == 200
    assert len(safe_exc(OurError("x" * 1000), limit=50)) == 50


def test_the_limit_applies_to_everything() -> None:
    assert len(safe_exc(ValueError("x"), limit=5)) == 5


def test_a_hostile_str_does_not_matter_for_ordinary_exceptions() -> None:
    class HostileError(Exception):
        def __str__(self) -> str:
            raise RuntimeError("str() was called")

    assert safe_exc(HostileError()) == "HostileError"


def test_the_set_of_classes_that_keep_their_messages_is_this_one() -> None:
    """Adding the marker is a decision about every message that class will ever carry: it is
    made here, in review, not by a ``class X(ContentFree, ...)`` somewhere nobody looks."""
    _all_modules()
    names = {f"{c.__module__}.{c.__qualname__}" for c in _subclasses(ContentFree)}
    names = {n for n in names if "test" not in n}
    assert names == {
        "procurement_agent.llm.base.LLMError",
        "procurement_agent.llm.base.LLMUnavailableError",
        "procurement_agent.llm.ollama_client.PromptTooLongError",
        "procurement_agent.checks.compare.DocumentTooLargeError",
        "procurement_agent.ingestion.docling_service.ConversionError",
        # a fixed sentence under a fixed code (ParserCode), nothing interpolated
        "procurement_agent.ingestion.parser_client.ParserError",
        "procurement_agent.ingestion.xlsx_size.WorkbookTooLargeError",
        "procurement_agent.ingestion.render.PageTooLargeError",
        "procurement_agent.security.upload_guard.UploadRejectedError",
        "procurement_agent.report.docx_annotate.UnsupportedDocumentError",
        "procurement_agent.migrations.MigrationError",
        # a code and a hint from our own constants; a stored parse that will not load
        "procurement_agent.ingestion.read_quality.NoTextError",
        "procurement_agent.ingestion.read_quality.StoredParseError",
        # one fixed sentence: the record of a read could not be stored (security/audit.py)
        "procurement_agent.security.audit.AuditUnavailableError",
    }


#: Every expression that may be interpolated into the message of a ``ContentFree`` exception, with
#: the reason it carries nothing of a document. A new entry is a review decision.
SAFE_INTERPOLATIONS = {
    "limit // (1024 * 1024)",  # a configured limit, in MB
    "limit",  # a configured limit
    "label",  # "first" / "second": which of the two compared documents
    "len(segments)",  # a count
    "max_segments",  # a configured limit
    "', '.join(parts[:3])",  # Docling component and module names (class names, not text)
    "len(result.errors)",  # a count
    "result.status.value",  # an enum of Docling's: "failure", "partial_success", ...
    "type(e).__name__",  # the kind of an exception
    "model",  # a model name, validated by ``check_model_name``
    "payload['model']",  # the same
    "needed",  # a token count
    "r.status_code",  # an HTTP status
    "request.method",  # GET or POST, from our own endpoint table
    "self._s.llm_num_ctx",  # a configured limit
    "url.host",  # the configured model server, from our own base URL
    "url.path",  # one of our endpoint paths
    "source.suffix",  # ".docx" and its like: the stored name is "<uuid>.<kind>"
    "_shown_extension(ext)",  # reduced to a plausible extension or "(unrecognised)"
    "allowed",  # the configured document kinds
    "kind",  # one of pdf / docx / xlsx
    "len(data) // mb",  # a size in MB
    "max_bytes // mb",  # a configured limit, in MB
    "max_pages",  # a configured limit
    "n",  # a page count
    "other",  # "docx" or "xlsx"
    "found",  # a schema version read from the database header: an integer
    "latest",  # the newest schema version this release knows: an integer
    "problem",  # one of OUR tables or columns the file lacks, or a count of the extra ones it has
    "version",  # a registered migration's number
    "step",  # a registered migration's name: an identifier ``check_registry`` has checked
    "error_kind",  # the class name of the exception a migration or a backup raised
    "before",  # the schema version the database stood at: an integer
    "count",  # a number of rows
}


def test_the_messages_of_the_marked_classes_only_interpolate_what_is_known_to_be_safe() -> None:
    """Every ``raise`` of a marked class is read for what goes into its message - directly, or
    through the ``msg = f"..."`` variable the project's style puts it in."""
    import ast
    from collections import defaultdict
    from pathlib import Path

    src = Path(procurement_agent.__file__).parent
    marked = {
        "LLMError",
        "LLMUnavailableError",
        "PromptTooLongError",
        "DocumentTooLargeError",
        "ConversionError",
        "ParserError",
        "WorkbookTooLargeError",
        "PageTooLargeError",
        "UploadRejectedError",
        "UnsupportedDocumentError",
        "MigrationError",
        "NoTextError",
        "StoredParseError",
        "AuditUnavailableError",
    }
    interpolated: dict[str, set[str]] = defaultdict(set)
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            assigned: dict[str, list[ast.expr]] = defaultdict(list)
            for st in ast.walk(fn):
                if isinstance(st, ast.Assign) and len(st.targets) == 1:
                    target = st.targets[0]
                    if isinstance(target, ast.Name):
                        assigned[target.id].append(st.value)
            for st in ast.walk(fn):
                if not (isinstance(st, ast.Raise) and isinstance(st.exc, ast.Call)):
                    continue
                func = st.exc.func
                called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if called not in marked:
                    continue
                for arg in st.exc.args:
                    values = assigned.get(arg.id, []) if isinstance(arg, ast.Name) else [arg]
                    for value in values:
                        for node in ast.walk(value):
                            if isinstance(node, ast.FormattedValue):
                                interpolated[str(path.relative_to(src))].add(
                                    ast.unparse(node.value)
                                )
    unknown = {
        f"{f}: {e}" for f, exprs in interpolated.items() for e in exprs - SAFE_INTERPOLATIONS
    }
    assert unknown == set(), (
        "a ContentFree exception now interpolates something not on SAFE_INTERPOLATIONS - if it can "
        "hold document text, take the mixin off that class; if not, add it there with the reason"
    )
    assert interpolated, (
        "the scan found no raise of a marked class: it is looking in the wrong place"
    )
