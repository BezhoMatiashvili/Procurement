"""Closed-answer-space calls: option validation and reading a distribution off logprobs.

These tests never reach Ollama. They feed ``_read_choice`` the response shapes the real server
produces — including the awkward ones — because the interesting behaviour is all in how the
reply is interpreted, not in the HTTP call.
"""

from __future__ import annotations

import math

import pytest

from procurement_agent.config import Settings
from procurement_agent.extraction.schemas import (
    MIN_COVERAGE,
    VAT_OPTIONS,
    YESNO_OPTIONS,
    ChoiceResult,
)
from procurement_agent.llm.base import LLMError
from procurement_agent.llm.ollama_client import OllamaClient, validate_options

SETTINGS = Settings(ollama_base_url="http://localhost:11434")


def client() -> OllamaClient:
    return OllamaClient(SETTINGS)


def body(*tokens: tuple[str, float], content: str = "yes") -> dict:
    """An Ollama /api/chat reply carrying one decoded token and its alternatives."""
    return {
        "message": {"role": "assistant", "content": content},
        "logprobs": [
            {
                "token": tokens[0][0],
                "logprob": math.log(tokens[0][1]),
                "top_logprobs": [
                    {"token": t, "logprob": math.log(p) if p else -60.0} for t, p in tokens
                ],
            }
        ],
    }


# --- option validation --------------------------------------------------------------------------


def test_shipped_option_sets_are_valid() -> None:
    """The constants the pipeline actually uses must pass the same gate as anything else."""
    assert validate_options(VAT_OPTIONS) == ("incl", "excl", "none")
    assert validate_options(YESNO_OPTIONS) == ("yes", "no")


@pytest.mark.parametrize(
    "options",
    [
        pytest.param(["yes"], id="a-single-option-is-not-a-choice"),
        pytest.param(["yes", "YES "], id="collide-once-normalised"),
        pytest.param(["დიახ", "არა"], id="georgian-is-many-tokens-per-word"),
        pytest.param(["yes", "definitely-not"], id="punctuation-splits-into-tokens"),
        pytest.param(["yes", "elaborated"], id="too-long-to-be-one-token"),
        pytest.param(["yes", ""], id="empty-option"),
    ],
)
def test_unscoreable_option_sets_are_rejected(options: list[str]) -> None:
    with pytest.raises(ValueError, match=r"option|choice"):
        validate_options(options)


# --- reading the distribution ---------------------------------------------------------------------


def test_casing_and_leading_space_variants_are_folded_together() -> None:
    """The model spreads real mass over ' Yes'/'Yes'/'yes'; discarding it understates the answer."""
    res = client()._read_choice(
        body(("yes", 0.4), ("Yes", 0.3), (" yes", 0.2), ("no", 0.1)), YESNO_OPTIONS, "m"
    )
    assert res.choice == "yes"
    assert res.probabilities["yes"] == pytest.approx(0.9)
    assert res.covered == pytest.approx(1.0)


def test_probabilities_are_renormalised_over_the_allowed_options_only() -> None:
    """Mass the model spent on words we did not offer is reported, not silently redistributed."""
    res = client()._read_choice(body(("maybe", 0.6), ("yes", 0.3), ("no", 0.1)), YESNO_OPTIONS, "m")
    assert res.covered == pytest.approx(0.4)  # only yes+no counted
    assert res.probabilities["yes"] == pytest.approx(0.75)  # 0.3 / 0.4
    assert sum(res.probabilities.values()) == pytest.approx(1.0)
    assert res.covered < MIN_COVERAGE  # and the caller is told not to trust it


def test_an_answer_outside_the_option_set_reports_no_information() -> None:
    """Uniform with covered=0 is honest; renormalising nothing would look like certainty."""
    res = client()._read_choice(body(("perhaps", 1.0), ("unsure", 0.0)), YESNO_OPTIONS, "m")
    assert res.covered == 0.0
    assert res.probabilities == {"yes": 0.5, "no": 0.5}
    assert res.confidence == 0.5


def test_three_way_choice_picks_the_argmax() -> None:
    res = client()._read_choice(
        body(("excl", 0.55), ("incl", 0.3), ("none", 0.15), content="excl"), VAT_OPTIONS, "m"
    )
    assert res.choice == "excl"
    assert res.confidence == pytest.approx(0.55)


def test_missing_logprobs_raises_rather_than_guessing() -> None:
    """An Ollama too old to report logprobs must fail loudly, not return a default answer."""
    with pytest.raises(LLMError, match="logprobs"):
        client()._read_choice({"message": {"content": "yes"}}, YESNO_OPTIONS, "m")


# --- the result model -----------------------------------------------------------------------------


def test_confidence_reads_the_chosen_option() -> None:
    r = ChoiceResult(choice="no", probabilities={"yes": 0.2, "no": 0.8}, covered=1.0)
    assert r.confidence == 0.8


def test_the_document_is_the_prefix_of_a_choice_prompt() -> None:
    """Ordering is load-bearing: the prefix cache is what makes extra questions affordable."""
    payload = client()._choice_payload(
        "სახელშეკრულებო ტექსტი", "is there a price?", ("yes", "no"), "m"
    )
    prompt = payload["messages"][0]["content"]
    assert prompt.startswith("<document>")
    assert prompt.index("სახელშეკრულებო") < prompt.index("is there a price?")
    assert payload["logprobs"] is True
    assert payload["options"]["num_predict"] == 1
    assert "format" not in payload  # a JSON schema would make the answer multi-token
