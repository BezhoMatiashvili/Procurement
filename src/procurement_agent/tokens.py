"""Token estimation, kept free of heavy imports so any layer can size a prompt.

Air-gapped, so there is no tokenizer to download; this over-estimates on purpose. Georgian
tokenizes poorly in most LLMs (roughly two tokens per character in the worst case), and an
over-estimate costs a little context window while an under-estimate silently truncates a
prompt, which Ollama does without reporting an error.
"""

from __future__ import annotations

import re

_GEORGIAN = re.compile(r"[Ⴀ-ჿⴀ-⴯]")
_WORDS = re.compile(r"\S+")


def estimate_tokens(text: str) -> int:
    """Conservative token estimate: ~0.5 tok/char for Georgian script, ~0.3 tok/char otherwise."""
    ka = len(_GEORGIAN.findall(text))
    other = len(text) - ka
    return int(ka * 0.5 + other * 0.3 + len(_WORDS.findall(text)) * 0.1) + 1
