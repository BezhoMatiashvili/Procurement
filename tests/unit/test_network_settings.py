"""The settings that keep the server on this machine: host list, egress guard, prod refusals."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from procurement_agent.config import Settings


def _prod(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "env": "prod",
        "docling_artifacts_path": Path("/models/docling"),
        "allowed_hosts": ("tasks.company.example",),
        # these tests are about hosts, the egress guard and the proxy; the models have their own
        # (tests/unit/test_models_manifest.py)
        "models_manifest_required": False,
        # parsing in the web process, chosen on purpose: production refuses to guess
        # (tests/unit/test_parser_config.py)
        "parser_mode": "inline",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# --- PA_ALLOWED_HOSTS --------------------------------------------------------------------------


def test_the_host_list_reads_csv_and_json_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PA_ALLOWED_HOSTS", "Tasks.Company.Example, *.corp.example ,")
    assert Settings().allowed_hosts == ("tasks.company.example", "*.corp.example")
    monkeypatch.setenv("PA_ALLOWED_HOSTS", '["tasks.company.example", "[::1]"]')
    assert Settings().allowed_hosts == ("tasks.company.example", "[::1]")


def test_the_host_list_defaults_to_empty() -> None:
    assert Settings().allowed_hosts == ()


@pytest.mark.parametrize(
    "entry",
    [
        "*",
        "**.example.com",
        "http://tasks.company.example",
        "tasks.company.example:8443",
        "tasks.company.example/",
        "user@tasks.company.example",
        "tasks company",
        "-leading.example",
        "[not-ipv6]",
        "[::1",
    ],
)
def test_an_entry_that_is_not_a_host_name_is_refused(entry: str) -> None:
    with pytest.raises(ValidationError, match="PA_ALLOWED_HOSTS"):
        Settings(allowed_hosts=(entry,))


# --- production refusals -------------------------------------------------------------------------


def test_a_complete_production_configuration_passes() -> None:
    _prod().validate_for_prod()


def test_development_is_never_refused() -> None:
    Settings(env="dev", egress_guard=False).validate_for_prod()


def test_prod_refuses_to_start_without_a_host_list() -> None:
    with pytest.raises(RuntimeError, match="PA_ALLOWED_HOSTS"):
        _prod(allowed_hosts=()).validate_for_prod()


def test_prod_refuses_to_run_with_the_egress_guard_off() -> None:
    with pytest.raises(RuntimeError, match="PA_EGRESS_GUARD"):
        _prod(egress_guard=False).validate_for_prod()


def test_prod_refuses_a_wildcard_proxy_trust(monkeypatch: pytest.MonkeyPatch) -> None:
    """With ``*`` any caller may set X-Forwarded-For and choose the address the audit log and the
    rate limiter record."""
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    with pytest.raises(RuntimeError, match="FORWARDED_ALLOW_IPS"):
        _prod().validate_for_prod()
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "172.30.231.10")
    _prod().validate_for_prod()


def test_every_problem_is_listed_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    with pytest.raises(RuntimeError) as raised:
        _prod(
            allowed_hosts=(), egress_guard=False, dev_api_key="pa_x", docling_artifacts_path=None
        ).validate_for_prod()
    text = str(raised.value)
    for needle in (
        "PA_DEV_API_KEY",
        "PA_DOCLING_ARTIFACTS_PATH",
        "PA_EGRESS_GUARD",
        "PA_ALLOWED_HOSTS",
        "FORWARDED_ALLOW_IPS",
    ):
        assert needle in text


def test_the_egress_guard_defaults_on_and_the_lan_off() -> None:
    s = Settings()
    assert s.egress_guard is True
    assert s.llm_allow_lan is False
