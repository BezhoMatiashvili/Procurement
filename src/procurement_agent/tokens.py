"""Token estimation, kept free of heavy imports so any layer can size a prompt.

Air-gapped, so there is no tokenizer to download; this over-estimates on purpose. An
over-estimate costs a little context window, while an under-estimate silently truncates a
prompt, which Ollama does without reporting an error.

The per-character costs were measured, not guessed, against the model we run: gemma3:4b
(Q4_K_M) through Ollama, reading ``prompt_eval_count`` for a raw ``/api/generate`` call and
subtracting the count for the same call without the text. On synthetic text of each class:

    class                      measured tok/char   factor here
    Georgian (Mkhedruli)       0.41 - 0.52         0.6
    Georgian capitals          1.44 - 1.54         1.7    (Mtavruli, U+1C90-U+1CBF)
    digits                     1.0                 1.0    (every digit is its own token)
    Latin letters              ~0.2 in prose       0.4    (codes such as IBANs cost more)
    whitespace                 mostly merged       0.3
    whitespace before a digit  1.0                 1.0    (never merged into the digit)
    punctuation and symbols    ~0.65               1.0

The estimate this replaces (0.5 per Georgian character, 0.3 for everything else) under-counted
a realistic chunk by up to a third - a table row with amounts came out at 0.64 of the real
count - and did not recognise Mtavruli as Georgian at all, which put a heading written in
capitals at a fifth of its real cost. ``tests/unit/test_hardening_llm.py`` pins the measured
samples, so a change to these factors that under-counts any of them fails.
"""

from __future__ import annotations

import re

_GEORGIAN = re.compile(r"[Ⴀ-ჿⴀ-⴯]")
_MTAVRULI = re.compile(r"[Ა-Ჿ]")
_DIGIT = re.compile(r"\d")
_LATIN = re.compile(r"[A-Za-z]")
_SPACE = re.compile(r"\s")
_SPACE_BEFORE_DIGIT = re.compile(r"\s(?=\d)")

_PER_GEORGIAN = 0.6
_PER_MTAVRULI = 1.7
_PER_DIGIT = 1.0
_PER_LATIN = 0.4
_PER_SPACE = 0.3
_PER_SPACE_BEFORE_DIGIT = 1.0
_PER_OTHER = 1.0


def estimate_tokens(text: str) -> int:
    """Conservative token estimate for gemma3's tokenizer; see the module docstring."""
    georgian = len(_GEORGIAN.findall(text))
    mtavruli = len(_MTAVRULI.findall(text))
    digits = len(_DIGIT.findall(text))
    latin = len(_LATIN.findall(text))
    spaces = len(_SPACE.findall(text))
    spaces_before_digit = len(_SPACE_BEFORE_DIGIT.findall(text))
    other = len(text) - georgian - mtavruli - digits - latin - spaces
    total = (
        georgian * _PER_GEORGIAN
        + mtavruli * _PER_MTAVRULI
        + digits * _PER_DIGIT
        + latin * _PER_LATIN
        + (spaces - spaces_before_digit) * _PER_SPACE
        + spaces_before_digit * _PER_SPACE_BEFORE_DIGIT
        + other * _PER_OTHER
    )
    return int(total) + 1
