"""The parser sandbox's settings: the mode, the socket, the limits, and what production refuses."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from procurement_agent.config import Settings
from procurement_agent.parser_service import protocol


def _prod(models: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "env": "prod",
        "docling_artifacts_path": models,
        "allowed_hosts": ("tasks.company.example",),
        "models_manifest_required": False,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_the_mode_is_unset_by_default_and_unset_means_inline() -> None:
    s = Settings()
    assert s.parser_mode is None
    assert s.use_parser_socket is False
    assert Settings(parser_mode="inline").use_parser_socket is False
    assert Settings(parser_mode="socket").use_parser_socket is True


def test_the_mode_reads_the_environment_and_an_empty_value_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PA_PARSER_MODE", "socket")
    assert Settings().parser_mode == "socket"
    monkeypatch.setenv("PA_PARSER_MODE", " Inline ")
    assert Settings().parser_mode == "inline"
    monkeypatch.setenv("PA_PARSER_MODE", "")
    assert Settings().parser_mode is None


def test_a_mode_that_is_neither_inline_nor_socket_is_refused() -> None:
    with pytest.raises(ValidationError):
        Settings(parser_mode="thread")  # type: ignore[arg-type]


def test_the_defaults_name_the_compose_socket_and_bound_the_result() -> None:
    s = Settings()
    assert s.parser_socket == Path("/run/parser/parser.sock")
    assert s.parser_max_result_mb == 64
    assert s.parser_max_result_bytes == 64 * 1024 * 1024
    assert s.parser_connect_wait_s == 90.0
    assert s.parser_kill_grace_s >= 1


def test_the_socket_has_to_be_an_absolute_path_that_fits_a_unix_address() -> None:
    with pytest.raises(ValidationError, match="absolute"):
        Settings(parser_socket=Path("run/parser.sock"))
    with pytest.raises(ValidationError, match="too long"):
        Settings(parser_socket=Path("/" + "d" * 120 + "/p.sock"))
    Settings(parser_socket=Path("/run/parser/parser.sock"))


def test_the_request_cap_is_the_file_limit_plus_a_megabyte() -> None:
    s = Settings(max_file_mb=7)
    assert s.parser_max_request_bytes == 7 * 1024 * 1024 + 1024 * 1024


def test_production_refuses_an_unset_mode_and_says_what_to_set(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError) as refused:
        _prod(tmp_path).validate_for_prod()
    text = str(refused.value)
    assert "PA_PARSER_MODE=socket" in text
    assert "inline" in text
    assert "degraded" in text
    assert "web process" in text


@pytest.mark.parametrize("mode", ["socket", "inline"])
def test_production_accepts_either_mode_when_it_is_chosen_on_purpose(
    tmp_path: Path, mode: str
) -> None:
    _prod(tmp_path, parser_mode=mode).validate_for_prod()


def test_development_does_not_have_to_choose() -> None:
    Settings(env="dev").validate_for_prod()


def test_the_other_production_refusals_still_stand_beside_the_new_one(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError) as refused:
        _prod(tmp_path, egress_guard=False, allowed_hosts=()).validate_for_prod()
    text = str(refused.value)
    assert "PA_EGRESS_GUARD" in text and "PA_ALLOWED_HOSTS" in text and "PA_PARSER_MODE" in text


# --- the parser's own start-up check ---------------------------------------------------------


def test_the_parser_does_not_need_a_host_list_or_any_other_web_setting(tmp_path: Path) -> None:
    s = Settings(env="prod", docling_artifacts_path=tmp_path, models_manifest_required=False)
    s.validate_for_parser()  # no allowed hosts, no mode: it is the parser


def test_the_parser_needs_its_models_checked_and_its_egress_guard_on(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="PA_DOCLING_ARTIFACTS_PATH"):
        Settings(env="prod", models_manifest_required=False).validate_for_parser()
    with pytest.raises(RuntimeError, match="PA_EGRESS_GUARD"):
        Settings(
            env="prod",
            docling_artifacts_path=tmp_path,
            models_manifest_required=False,
            egress_guard=False,
        ).validate_for_parser()
    with pytest.raises(RuntimeError, match="manifest"):
        Settings(env="prod", docling_artifacts_path=tmp_path).validate_for_parser()


def test_the_parser_is_only_checked_in_production(tmp_path: Path) -> None:
    Settings(env="dev", egress_guard=False).validate_for_parser()


# --- the deadlines are ordered ---------------------------------------------------------------


@pytest.mark.parametrize("docling", [10, 900, 7200])
@pytest.mark.parametrize("grace", [1, 30, 600])
def test_docling_gives_up_before_the_kill_and_the_kill_comes_before_the_client_does(
    docling: int, grace: int
) -> None:
    s = Settings(docling_timeout_s=docling, parser_kill_grace_s=grace)
    kill = protocol.kill_deadline_s(s.docling_timeout_s, s.parser_kill_grace_s)
    reply = protocol.reply_deadline_s(s.docling_timeout_s, s.parser_kill_grace_s)
    assert s.docling_timeout_s < kill < reply
