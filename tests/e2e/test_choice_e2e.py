"""Closed-answer-space calls against a live local Ollama (slow, CPU).

    PA_OLLAMA_BASE_URL=http://127.0.0.1:11434 uv run pytest -m e2e -q

Three things can only be checked against the real model and the real server, and each one
silently degrades rather than failing if it stops holding:

* whether the options are actually single tokens in the model's vocabulary — a two-token option
  never receives probability, and the answer quietly becomes whichever option was whole;
* whether the model can tell a value the document states from one it does not;
* whether asking a second question about the same chunk reuses the prefill, which is the only
  reason the pass is affordable at all.

Unlike the other e2e module this one needs no Docling models: it talks to the LLM only.
"""

from __future__ import annotations

import os

import httpx
import pytest

from procurement_agent.config import Settings
from procurement_agent.extraction.schemas import VAT_OPTIONS, YESNO_OPTIONS
from procurement_agent.llm.ollama_client import OllamaClient

BASE_URL = os.environ.get("PA_OLLAMA_BASE_URL", "http://127.0.0.1:11434")


def _ollama_up() -> bool:
    try:
        return httpx.get(f"{BASE_URL}/api/tags", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not _ollama_up(), reason=f"no Ollama at {BASE_URL}"),
]

#: A Georgian act, repeated well past the size above which the server reuses a cached prefix.
#: Reuse has been observed here at 1193 tokens and never at 1050 or below, so the fixture sits
#: deliberately clear of the boundary rather than on it.
CLAUSE = (
    "მიღება-ჩაბარების აქტი გაფორმდა შპს „ნიმუში ჯგუფი“-სა და შემსყიდველს შორის. "
    "სამუშაოს ღირებულება შეადგენს 45 780 (ორმოცდახუთი ათას შვიდას ოთხმოცი) ლარს, დღგ-ს ჩათვლით. "
    "ანგარიშსწორება განხორციელდება 10 (ათი) სამუშაო დღის ვადაში. "
    "საგარანტიო ვადა შეადგენს 24 თვეს ობიექტის ექსპლუატაციაში მიღებიდან. "
)
DOC = CLAUSE * 10


@pytest.fixture(scope="module")
def llm() -> OllamaClient:
    settings = Settings(
        ollama_base_url=BASE_URL,
        llm_model=os.environ.get("PA_LLM_FAST_MODEL", "gemma3:4b"),
        llm_num_thread=min(16, os.cpu_count() or 4),
        llm_num_ctx=8192,
    )
    client = OllamaClient(settings)
    if settings.llm_model not in client.health().get("models_present", []):
        pytest.skip(f"model {settings.llm_model} not pulled")
    return client


@pytest.mark.parametrize("options", [YESNO_OPTIONS, VAT_OPTIONS])
def test_options_are_single_tokens_in_the_real_vocabulary(
    llm: OllamaClient, options: tuple[str, ...]
) -> None:
    """Near-total coverage is the evidence that every option can receive probability at all."""
    res = llm.generate_choice(
        document=DOC, question="What is the VAT treatment of the amounts?", options=options
    )
    assert res.choice in options
    assert res.covered > 0.9, f"options {options} hold only {res.covered:.2f} of the mass"
    assert sum(res.probabilities.values()) == pytest.approx(1.0)


def test_the_verifier_separates_a_stated_value_from_an_invented_one(llm: OllamaClient) -> None:
    """The point of the verifier: both quotes are plausible, only one is what the document says."""
    stated = llm.generate_choice(
        document=DOC,
        question='Does this document state that the total amount is "45 780 GEL"?',
        options=YESNO_OPTIONS,
    )
    invented = llm.generate_choice(
        document=DOC,
        question='Does this document state that the total amount is "99 999 GEL"?',
        options=YESNO_OPTIONS,
    )
    assert stated.choice == "yes"
    assert invented.choice == "no"


def test_a_repeat_question_reuses_the_prefill(llm: OllamaClient) -> None:
    """Reuse exists and depends on the document being the prefix; ordering regressions fail here.

    Read what this does *not* prove. The fixture is ~2000 tokens, comfortably above the
    boundary. Real chunks measure around 1100 tokens, which straddles it — reuse was observed at
    1193 and never at 1050. So a pass here means the mechanism works and the ordering is right;
    it does not mean the production path gets the saving. That is settled by the `prefill_ms`
    field on the `llm_choice` log line of an actual run, not by this test.
    """

    def prefill_ms(question: str) -> tuple[int, int]:
        payload = llm._choice_payload(DOC, question, YESNO_OPTIONS, llm._s.llm_model)
        body = llm._request(payload)
        return (
            int(body.get("prompt_eval_count") or 0),
            int(body.get("prompt_eval_duration") or 0) // 1_000_000,
        )

    # The server keeps one cached prefix per slot across requests and processes, and the test
    # above prefills this same DOC, so without this the "first" call is already warm and the
    # comparison measures nothing. A different prompt evicts it.
    llm.generate_choice(
        document="An unrelated short text.", question="is it short?", options=YESNO_OPTIONS
    )
    tokens, first = prefill_ms("does the document state a warranty period?")
    if tokens < 1200:
        pytest.skip(f"prompt is {tokens} tokens; reuse was never observed at or below 1050")
    _, second = prefill_ms("does the document state a penalty clause?")
    _, third = prefill_ms("does the document state a payment term?")

    # Measured here: ~27 s cold against ~0.8 s warm. A third of the cold cost is a wide margin
    # that still fails loudly if the document stops being the shared prefix.
    assert second < first / 3, f"no prefix reuse: {first}ms then {second}ms"
    assert third < first / 3
