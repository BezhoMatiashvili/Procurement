"""Ollama client: JSON-schema constrained generation, deterministic settings, hard timeouts.

Ollama enforces the schema with grammar-constrained decoding (``format`` parameter), and we
validate the result again with Pydantic — the model output is never trusted as-is.

Three details of this file were established by measurement against the model we actually run,
and each is load-bearing:

*Gemma 3 has no system role.* ``ollama show gemma3:4b --template`` renders ``system`` and
``user`` as two consecutive ``<start_of_turn>user`` blocks. The model was not trained on
consecutive user turns, so sending a separate system message puts the request off-distribution
for no benefit. The instructions are therefore prepended to the single user turn, which is what
Gemma's own guidance prescribes.

*Repetition penalties are wrong for this task.* Ollama defaults to ``repeat_penalty=1.1`` over
the last 64 tokens, and applies it as a logit processor even under greedy decoding. Our output
is JSON with repeated field names, containing quotes copied verbatim from the document — both
of which that penalty actively discourages. It is turned off.

*An over-long prompt is silently truncated*, not rejected. Ollama drops the overflow and answers
from what is left, which reads as a confident wrong answer. The context window is therefore
sized per request from an estimate, and a prompt that cannot fit raises.
"""

from __future__ import annotations

import json
import time
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from procurement_agent.config import Settings
from procurement_agent.llm.base import LLMError, LLMUnavailableError
from procurement_agent.security.audit import log
from procurement_agent.tokens import estimate_tokens

T = TypeVar("T", bound=BaseModel)

_SEED = 42
#: Room for the JSON the model writes back, on top of the prompt.
_CTX_HEADROOM = 512
#: num_ctx is rounded up to a multiple of this; changing it per request would defeat the
#: prompt cache Ollama keeps for a loaded model.
_CTX_GRANULARITY = 2048

#: Sampling options. Deterministic and repetition-neutral; see the module docstring.
BASE_OPTIONS: dict[str, Any] = {
    "temperature": 0,
    "top_k": 1,  # explicit greedy, independent of sampler ordering
    "seed": _SEED,
    "repeat_penalty": 1.0,
    "repeat_last_n": 0,
}

_RETRY_STATUS = frozenset({500, 502, 503, 504})
_MAX_ATTEMPTS = 3
_BACKOFF_S = 1.5


class PromptTooLongError(LLMError):
    """The prompt cannot fit in the model's context window.

    Raised rather than letting Ollama truncate, because a truncated prompt produces an answer
    that looks normal and is wrong.
    """


class OllamaClient:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._http = httpx.Client(
            base_url=settings.ollama_base_url,
            timeout=httpx.Timeout(
                connect=5.0, read=float(settings.llm_timeout_s), write=30.0, pool=5.0
            ),
            trust_env=False,  # never pick up HTTP(S)_PROXY from the environment
        )

    # --- public --------------------------------------------------------------------------------

    def generate_structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        model: str | None = None,
        max_output_tokens: int | None = None,
    ) -> T:
        payload = self._payload(system, user, schema, model or self._s.llm_model, max_output_tokens)
        content = self._chat(payload)
        try:
            return schema.model_validate_json(content)
        except ValidationError as first:
            log.warning("llm_invalid_json_retry", model=payload["model"], error=str(first)[:200])
            payload["messages"].append({"role": "assistant", "content": content})
            payload["messages"].append(
                {
                    "role": "user",
                    "content": "The JSON was invalid. Return only valid JSON matching the schema.",
                }
            )
            content = self._chat(payload)
            try:
                return schema.model_validate_json(content)
            except ValidationError as e:
                msg = "model did not return valid structured output"
                raise LLMError(msg) from e

    def options(self) -> dict[str, Any]:
        """The sampling configuration, for recording on an evaluation run."""
        return {
            **BASE_OPTIONS,
            "num_thread": self._s.llm_num_thread,
            "num_ctx_max": self._s.llm_num_ctx,
        }

    def health(self) -> dict[str, object]:
        try:
            r = self._http.get("/api/tags", timeout=5.0)
            r.raise_for_status()
        except httpx.HTTPError as e:
            return {"ok": False, "error": type(e).__name__}
        names = {m.get("name", "") for m in r.json().get("models", [])}
        wanted = {self._s.llm_model, self._s.llm_fast_model}
        return {
            "ok": self._s.llm_model in names,
            "models_present": sorted(wanted & names),
            "models_missing": sorted(wanted - names),
        }

    def close(self) -> None:
        self._http.close()

    # --- internals -----------------------------------------------------------------------------

    def _context_size(self, prompt: str, max_output_tokens: int) -> int:
        needed = estimate_tokens(prompt) + max_output_tokens + _CTX_HEADROOM
        if needed > self._s.llm_num_ctx:
            msg = (
                f"prompt needs about {needed} tokens but the context window is "
                f"{self._s.llm_num_ctx}; Ollama would silently truncate it. "
                "Reduce the chunk size or raise PA_LLM_NUM_CTX."
            )
            raise PromptTooLongError(msg)
        rounded = -(-needed // _CTX_GRANULARITY) * _CTX_GRANULARITY
        return min(max(rounded, _CTX_GRANULARITY), self._s.llm_num_ctx)

    def _payload(
        self,
        system: str,
        user: str,
        schema: type[BaseModel],
        model: str,
        max_output_tokens: int | None,
    ) -> dict[str, Any]:
        # Gemma renders a system message as its own user turn; merge it instead.
        prompt = f"{system.strip()}\n\n{user.strip()}" if system.strip() else user.strip()
        num_predict = max_output_tokens or self._s.llm_max_output_tokens
        return {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "format": schema.model_json_schema(),
            "stream": False,
            "keep_alive": -1,  # keep the model resident: CPU reload is very slow
            "options": {
                **BASE_OPTIONS,
                "num_ctx": self._context_size(prompt, num_predict),
                "num_thread": self._s.llm_num_thread,
                "num_predict": num_predict,
            },
        }

    def _chat(self, payload: dict[str, Any]) -> str:
        r = self._post_with_retry(payload)
        if r.status_code == 404:
            msg = f"model {payload['model']!r} is not installed on the LLM service"
            raise LLMUnavailableError(msg)
        if r.status_code >= 400:
            msg = f"LLM service error {r.status_code}"
            raise LLMError(msg)
        try:
            body = r.json()
            content = body["message"]["content"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            raise LLMError("malformed LLM response") from e
        eval_count = body.get("eval_count") or 0
        if eval_count >= payload["options"]["num_predict"]:
            # grammar-constrained output that hits the cap is truncated JSON, not an answer
            log.warning("llm_output_truncated", model=payload["model"], num_predict=eval_count)
        log.info(
            "llm_call",
            model=payload["model"],
            prompt_tokens=body.get("prompt_eval_count"),
            completion_tokens=eval_count,
            num_ctx=payload["options"]["num_ctx"],
            total_ms=(body.get("total_duration") or 0) // 1_000_000,
        )
        return str(content)

    def _post_with_retry(self, payload: dict[str, Any]) -> httpx.Response:
        """Retry only what is safe to retry: a refused connection or a 5xx.

        Generation is deterministic and side-effect free, so a repeat is harmless. A timeout is
        not retried — on CPU it means the model is still working, and a second request would
        queue behind the first and time out again.
        """
        last: Exception | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                r = self._http.post("/api/chat", json=payload)
            except httpx.ConnectError as e:
                last = e
            except httpx.TimeoutException as e:
                raise LLMError("LLM request timed out") from e
            else:
                if r.status_code not in _RETRY_STATUS:
                    return r
                last = LLMError(f"LLM service error {r.status_code}")
            if attempt < _MAX_ATTEMPTS - 1:
                delay = _BACKOFF_S * (2**attempt)
                log.warning("llm_retry", attempt=attempt + 1, delay_s=delay)
                time.sleep(delay)
        raise LLMUnavailableError("LLM service unreachable") from last
