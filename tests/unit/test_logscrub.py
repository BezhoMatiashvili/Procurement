"""The backstops behind "logs never carry document text": structlog processor and stdlib filter."""

from __future__ import annotations

import io
import logging
import sys

import pytest
import structlog

from procurement_agent.security import logscrub
from procurement_agent.security.audit import configure_logging
from procurement_agent.security.logscrub import MAX_STRING, ScrubFilter, content_free

CANARY = "CANARY-77c1-ლარი-12 345"


def run(**event: object) -> dict[str, object]:
    return content_free(None, "info", dict(event))


# --- the structlog processor --------------------------------------------------------------------


def test_an_ordinary_event_passes_untouched() -> None:
    out = run(
        event="llm_call", model="gemma3:12b", prompt_tokens=812, total_ms=1520, ok=True, who=None
    )
    assert out == {
        "event": "llm_call",
        "model": "gemma3:12b",
        "prompt_tokens": 812,
        "total_ms": 1520,
        "ok": True,
        "who": None,
    }


@pytest.mark.parametrize(
    "event",
    [
        f"parsed {CANARY}",  # an f-string with a document in it
        f"{CANARY}",
        "has\nnewline",
        "x" * 200,
        "",
        "1leading_digit",
    ],
)
def test_an_event_that_is_not_an_identifier_is_not_logged_as_one(event: str) -> None:
    out = run(event=event)
    assert out["event"] == "log_event_withheld"
    assert out["withheld_chars"] == len(event)
    assert CANARY not in str(out)


def test_a_non_string_event_is_withheld_too() -> None:
    assert run(event=CANARY.encode())["event"] == "log_event_withheld"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "key",
    [
        "value",
        "text",
        "quote",
        "raw_text",
        "content",
        "prompt",
        "document",
        "filename",
        "name",
        "sheet",
        "question",
        "VALUE",
        "Text",
    ],
)
def test_a_field_named_for_content_is_withheld_whatever_it_holds(key: str) -> None:
    out = run(event="llm_fact_unsupported", **{key: CANARY})
    assert out[key] == "[withheld]"
    assert CANARY not in str(out)


def test_a_content_field_is_withheld_even_when_it_is_a_number() -> None:
    assert run(event="x", value=12345)["value"] == "[withheld]"


def test_a_long_string_is_withheld_by_length_not_truncated() -> None:
    out = run(event="x", error="a" * (MAX_STRING + 1), ok="a" * MAX_STRING)
    assert out["error"] == f"[withheld: {MAX_STRING + 1} chars]"
    assert out["ok"] == "a" * MAX_STRING  # at the limit is still a status word


def test_nested_containers_are_cleaned_to_a_depth() -> None:
    out = run(
        event="x",
        options=["yes", "no"],
        items=[{"text": CANARY, "n": 1}, ("a", "b" * 500)],
        deep={"a": {"b": {"c": CANARY * 50}}},
    )
    assert out["options"] == ["yes", "no"]
    items = out["items"]
    assert isinstance(items, list)
    assert items[0] == {"text": "[withheld: text]", "n": 1}
    assert items[1] == ("a", "[withheld: 500 chars]")
    assert CANARY not in str(out["items"])


def test_exc_info_becomes_the_kind_of_the_exception() -> None:
    try:
        raise ValueError(f"cannot read {CANARY}")
    except ValueError:
        out = run(event="job_crashed", exc_info=True)
        out2 = run(event="job_crashed", exc_info=sys.exc_info())
        out3 = run(event="job_crashed", exc_info=sys.exc_info()[1])
    for o in (out, out2, out3):
        assert "exc_info" not in o
        assert o["error"] == "ValueError"
        assert CANARY not in str(o)


def test_exc_info_with_no_active_exception_is_just_dropped() -> None:
    out = run(event="x", exc_info=True)
    assert "exc_info" not in out
    assert "error" not in out


# --- the stdlib filter ----------------------------------------------------------------------------


def _record(
    name: str, msg: str, args: tuple[object, ...], exc_info: object = None
) -> logging.LogRecord:
    return logging.LogRecord(name, logging.INFO, __file__, 1, msg, args, exc_info)  # type: ignore[arg-type]


def test_an_access_line_loses_its_query_string() -> None:
    record = _record(
        "uvicorn.access",
        '%s - "%s %s HTTP/%s" %d',
        (
            "203.0.113.9:5555",
            "GET",
            f"/ui/checks/aoa?advance_amount={CANARY}&contractor_type=x",
            "1.1",
            200,
        ),
    )
    assert ScrubFilter().filter(record)
    line = record.getMessage()
    assert CANARY not in line
    assert "/ui/checks/aoa?[query withheld]" in line
    assert line.endswith('HTTP/1.1" 200')


def test_an_access_line_without_a_query_is_unchanged() -> None:
    record = _record(
        "uvicorn.access", '%s - "%s %s HTTP/%s" %d', ("1.2.3.4:1", "GET", "/health", "1.1", 200)
    )
    ScrubFilter().filter(record)
    assert "/health HTTP/1.1" in record.getMessage()


def test_other_loggers_are_not_rewritten() -> None:
    record = _record("some.library", "path is %s", ("/a?b=c",))
    ScrubFilter().filter(record)
    assert record.getMessage() == "path is /a?b=c"


def test_a_traceback_is_rendered_as_frames_and_kinds() -> None:
    try:
        try:
            raise KeyError(CANARY)
        except KeyError as inner:
            raise RuntimeError(f"wrapped {CANARY}") from inner
    except RuntimeError:
        record = _record("uvicorn.error", "Exception in ASGI application", (), sys.exc_info())
    assert ScrubFilter().filter(record)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.handle(record)
    text = stream.getvalue()
    assert CANARY not in text
    assert "Traceback (most recent call last):" in text
    assert "KeyError" in text
    assert "RuntimeError (caused by KeyError)" in text
    assert "test_logscrub.py" in text  # the frames are there: where it happened is the useful part


def test_scrub_traceback_survives_a_cycle_in_the_chain() -> None:
    a = ValueError("a")
    b = ValueError("b")
    a.__context__ = b
    b.__context__ = a
    try:
        raise a
    except ValueError:
        text = logscrub.scrub_traceback(sys.exc_info())  # type: ignore[arg-type]
    assert text.count("Traceback") == 2


# --- wiring ---------------------------------------------------------------------------------------


@pytest.fixture
def restore_logging() -> object:
    structlog_config = structlog.get_config()
    root_handlers = list(logging.getLogger().handlers)
    yield None
    structlog.configure(**structlog_config)
    root = logging.getLogger()
    for h in list(root.handlers):
        if h not in root_handlers:
            root.removeHandler(h)


def test_configure_logging_attaches_the_filter_to_uvicorn_and_the_root(
    restore_logging: object,
) -> None:
    handler = logging.StreamHandler(io.StringIO())
    uvicorn_access = logging.getLogger("uvicorn.access")
    uvicorn_access.addHandler(handler)
    try:
        configure_logging(json_output=True)
        configure_logging(json_output=True)  # idempotent: one filter each, not two
        for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
            filters = [f for f in logging.getLogger(name).filters if isinstance(f, ScrubFilter)]
            assert len(filters) == 1, name
        assert sum(isinstance(f, ScrubFilter) for f in handler.filters) == 1
    finally:
        uvicorn_access.removeHandler(handler)


def test_configured_structlog_logs_through_the_processor(
    restore_logging: object, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(json_output=True)
    structlog.get_logger("t").info(
        "llm_fact_unsupported", chunk="c1", value=CANARY, error="e" * 500
    )
    structlog.get_logger("t").info(f"oops {CANARY}")
    out = capsys.readouterr().out
    assert CANARY not in out
    assert '"chunk": "c1"' in out
    assert "log_event_withheld" in out
