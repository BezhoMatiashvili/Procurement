"""Document text goes to one place: the model server this machine is configured for.

Three layers hold that, and each is tested on its own: the settings (a model server on this
machine unless the operator opted into the company network, and no hosted model), the client (the
same checks again, because ``model_copy`` skips validation; only two requests, only to that
server; a model the server forwards elsewhere is refused before any text is sent) and the health
report.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable

import httpx
import pytest
from pydantic import ValidationError

from procurement_agent.config import Settings, check_llm_url, check_model_name
from procurement_agent.extraction.schemas import YESNO_OPTIONS, LLMExtraction
from procurement_agent.llm.base import LLMError, LLMUnavailableError
from procurement_agent.llm.ollama_client import OllamaClient

Handler = Callable[[httpx.Request], httpx.Response]
BASE = "http://127.0.0.1:11434"


def _settings(**overrides: object) -> Settings:
    return Settings(ollama_base_url=BASE, **overrides)  # type: ignore[arg-type]


def _client(handler: Handler, **overrides: object) -> OllamaClient:
    client = OllamaClient(_settings(**overrides))
    client._http = httpx.Client(
        base_url=BASE,
        transport=httpx.MockTransport(handler),
        event_hooks={"request": [client._only_ollama]},
    )
    return client


def _chat_answer(request: httpx.Request) -> httpx.Response:
    content = json.dumps({"facts": []})
    return httpx.Response(200, json={"message": {"content": content}, "eval_count": 3})


# --- model names ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "gemma3:12b",
        "gemma3:4b",
        "gemma3",
        "qwen2.5:7b-instruct-q4_K_M",
        "hf.co/org/model-GGUF:Q4_K_M",
        "llama3.1:8b",
    ],
)
def test_ordinary_model_names_are_accepted(name: str) -> None:
    assert check_model_name(name) == name
    assert _settings(llm_model=name).llm_model == name


@pytest.mark.parametrize(
    "name",
    [
        "gpt-oss:120b-cloud",
        "qwen3-coder:480b-cloud",
        "deepseek-v3.1:671b-cloud",
        "something-CLOUD",
        "cloud/model:1b",
        "model:cloud",
    ],
)
def test_a_hosted_model_is_refused_by_name(name: str) -> None:
    with pytest.raises(ValueError, match="hosted model"):
        check_model_name(name)
    with pytest.raises(ValidationError, match="hosted model"):
        _settings(llm_model=name)
    with pytest.raises(ValidationError, match="hosted model"):
        _settings(llm_fast_model=name)


@pytest.mark.parametrize(
    "name", ["", " ", "a b", "x;rm -rf", "../etc", "m\\n", "http://host/model", "-leading"]
)
def test_a_malformed_model_name_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="not an Ollama model name"):
        check_model_name(name)


def test_cloudy_but_local_names_are_not_caught_by_accident() -> None:
    """The word has to stand alone: "cloudera" and "nebulacloudy" are somebody's local models."""
    assert check_model_name("cloudera-model:7b") == "cloudera-model:7b"
    assert check_model_name("nebulacloudy:1b") == "nebulacloudy:1b"


# --- the URL ---------------------------------------------------------------------------------


def test_the_url_check_is_one_function_shared_by_settings_client_and_scripts() -> None:
    assert check_llm_url("http://localhost:11434/") == "http://localhost:11434"
    with pytest.raises(ValueError, match="PA_LLM_ALLOW_LAN"):
        check_llm_url("http://10.0.0.5:11434")
    assert check_llm_url("http://10.0.0.5:11434", allow_lan=True) == "http://10.0.0.5:11434"
    with pytest.raises(ValueError, match="internal host"):
        check_llm_url("http://example.com:11434", allow_lan=True)


def test_model_copy_cannot_smuggle_a_public_server_into_a_client() -> None:
    """``model_copy(update=...)`` runs no validators; the client checks again."""
    base = _settings()
    for update in (
        {"ollama_base_url": "http://example.com:11434"},
        {"ollama_base_url": "http://10.0.0.5:11434"},  # LAN without the opt-in
        {"llm_model": "gpt-oss:120b-cloud"},
        {"llm_fast_model": "x;y"},
    ):
        with pytest.raises(ValueError, match=r"PA_OLLAMA_BASE_URL|model"):
            OllamaClient(base.model_copy(update=update))
    OllamaClient(
        base.model_copy(update={"ollama_base_url": "http://10.0.0.5:11434", "llm_allow_lan": True})
    )


def test_a_model_named_per_call_is_checked_too() -> None:
    """The evaluation scripts take ``--model``; a hosted name never reaches the wire."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _chat_answer(request)

    client = _client(handler)
    with pytest.raises(ValueError, match="hosted model"):
        client.generate_structured(system="s", user="u", schema=LLMExtraction, model="x:cloud")
    with pytest.raises(ValueError, match="hosted model"):
        client.generate_choice(document="d", question="q", options=YESNO_OPTIONS, model="x:cloud")
    assert seen == []


# --- only two requests, only to the configured server --------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/pull"),
        ("POST", "/api/push"),
        ("POST", "/api/create"),
        ("POST", "/api/copy"),
        ("DELETE", "/api/delete"),
        ("POST", "/api/show"),
        ("POST", "/api/generate"),
        ("GET", "/api/chat"),  # right path, wrong method
        ("POST", "/api/tags"),
        ("GET", "/"),
        ("GET", "/api/me"),
        ("POST", "/api/signin"),
    ],
)
def test_any_other_ollama_endpoint_is_refused_before_it_is_sent(method: str, path: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    client = _client(handler)
    with pytest.raises(LLMError, match="not an allowed LLM request"):
        client._http.request(method, path)
    assert seen == []


def test_a_request_to_another_host_is_refused_whatever_the_path() -> None:
    client = _client(_chat_answer)
    for url in (
        "http://example.com:11434/api/chat",
        "http://127.0.0.1:9999/api/chat",  # another port on the same host
        "https://127.0.0.1:11434/api/chat",
    ):
        with pytest.raises(LLMError, match="not an allowed LLM request"):
            client._http.post(url, json={})


def test_the_two_endpoints_the_client_uses_pass() -> None:
    client = _client(
        lambda r: (
            httpx.Response(200, json={"models": [{"name": "gemma3:12b"}]})
            if r.url.path == "/api/tags"
            else _chat_answer(r)
        )
    )
    assert client.health()["ok"] is True
    assert client.generate_structured(system="s", user="u", schema=LLMExtraction).facts == []


def test_a_base_url_with_a_path_prefix_keeps_its_prefix() -> None:
    settings = Settings(ollama_base_url="http://127.0.0.1:11434/ollama")
    client = OllamaClient(settings)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"models": [{"name": "gemma3:12b"}]})

    client._http = httpx.Client(
        base_url="http://127.0.0.1:11434/ollama",
        transport=httpx.MockTransport(handler),
        event_hooks={"request": [client._only_ollama]},
    )
    assert client.health()["ok"] is True
    assert seen == ["/ollama/api/tags"]
    with pytest.raises(LLMError, match="not an allowed LLM request"):
        client._http.get("http://127.0.0.1:11434/api/tags")  # same host, outside the prefix


def test_redirects_are_not_followed() -> None:
    client = OllamaClient(_settings())
    assert client._http.follow_redirects is False
    assert client._http.trust_env is False


# --- a model the server forwards elsewhere --------------------------------------------------------


def _tags(*entries: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json={"models": list(entries)})


def test_a_model_with_a_remote_host_is_refused_before_any_text_is_sent() -> None:
    chats: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return _tags({"name": "gemma3:12b", "remote_host": "https://ollama.com:443"})
        chats.append(request)
        return _chat_answer(request)

    client = _client(handler)
    with pytest.raises(LLMError, match="remote host"):
        client.generate_structured(system="s", user="the document text", schema=LLMExtraction)
    with pytest.raises(LLMError, match="remote host"):
        client.generate_choice(document="d", question="q", options=YESNO_OPTIONS)
    assert chats == []


@pytest.mark.parametrize("marker", ["remote_host", "remote_model"])
def test_either_marker_is_enough(marker: str) -> None:
    client = _client(lambda r: _tags({"name": "gemma3:12b", marker: "something"}))
    with pytest.raises(LLMError, match="remote host"):
        client.generate_structured(system="s", user="u", schema=LLMExtraction)


def test_a_local_model_is_verified_once_not_on_every_call() -> None:
    calls = {"tags": 0, "chat": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            calls["tags"] += 1
            return _tags({"name": "gemma3:12b", "size": 8_000_000_000})
        calls["chat"] += 1
        return _chat_answer(request)

    client = _client(handler)
    for _ in range(5):
        client.generate_structured(system="s", user="u", schema=LLMExtraction)
    assert calls == {"tags": 1, "chat": 5}


def test_the_verdict_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model replaced by a hosted one under the same name is noticed within the TTL."""
    from procurement_agent.llm import ollama_client

    now = [1000.0]
    monkeypatch.setattr(ollama_client.time, "monotonic", lambda: now[0])
    remote = [False]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return _tags(
                {"name": "gemma3:12b", **({"remote_host": "https://x"} if remote[0] else {})}
            )
        return _chat_answer(request)

    client = _client(handler)
    client.generate_structured(system="s", user="u", schema=LLMExtraction)
    remote[0] = True
    client.generate_structured(system="s", user="u", schema=LLMExtraction)  # still inside the TTL
    now[0] += 601
    with pytest.raises(LLMError, match="remote host"):
        client.generate_structured(system="s", user="u", schema=LLMExtraction)


def test_a_server_that_cannot_be_asked_leaves_the_decision_to_the_call_itself() -> None:
    """Nothing has been sent yet; the chat meets the same fault and reports it in its own words."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            raise httpx.ConnectError("refused", request=request)
        return _chat_answer(request)

    client = _client(handler)
    assert client.generate_structured(system="s", user="u", schema=LLMExtraction).facts == []
    assert client._local_until == {}  # and it is asked again next time


@pytest.mark.parametrize(
    "answer",
    [httpx.Response(200, text="<html>"), httpx.Response(500), httpx.Response(200, json=[])],
)
def test_an_unreadable_model_list_is_not_a_verdict(answer: httpx.Response) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return answer if request.url.path == "/api/tags" else _chat_answer(request)

    client = _client(handler)
    assert client.generate_structured(system="s", user="u", schema=LLMExtraction).facts == []


def test_an_uninstalled_model_still_reports_as_uninstalled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return _tags({"name": "other:1b"})
        return httpx.Response(404, json={"error": "model not found"})

    client = _client(handler)
    with pytest.raises(LLMUnavailableError, match="not installed"):
        client.generate_structured(system="s", user="u", schema=LLMExtraction)


def test_health_reports_and_fails_on_a_remote_model() -> None:
    client = _client(
        lambda r: _tags(
            {"name": "gemma3:12b", "remote_host": "https://ollama.com:443"}, {"name": "gemma3:4b"}
        )
    )
    h = client.health()
    assert h["ok"] is False
    assert h["models_remote"] == ["gemma3:12b"]
    assert h["models_present"] == ["gemma3:12b", "gemma3:4b"]


def test_health_of_a_clean_server_lists_no_remote_models() -> None:
    client = _client(lambda r: _tags({"name": "gemma3:12b"}, {"name": "gemma3:4b"}))
    assert client.health() == {
        "ok": True,
        "models_present": ["gemma3:12b", "gemma3:4b"],
        "models_missing": [],
        "models_remote": [],
    }


def test_infinite_ttl_helper_in_other_tests_matches_the_real_attribute() -> None:
    """test_hardening_llm pre-trusts models through this attribute; keep the two in step."""
    client = OllamaClient(_settings())
    assert client._local_until == {}
    client._local_until = {"gemma3:12b": math.inf}
    assert client._local_until["gemma3:12b"] > 1e18
