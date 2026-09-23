"""Confidence weights, in one table so they can be tuned against the gold set.

These numbers used to be scattered as literals across the rules, the LLM extractor and the
merger, which made them impossible to reason about or to tune. They express one ordering:

    label anchor  >  specific regex rule  >  LLM  >  unclassified regex hit

An anchor read a value from the label the document itself puts in front of it, so it is the
strongest evidence available short of a human. A rule matched a pattern. The LLM produced a
value that survived quote verification. A generic hit found a number with no idea what it is.
"""

from __future__ import annotations

from typing import Final

#: A value read from behind its own label ("საიდენტიფიკაციო კოდი: ...").
ANCHOR: Final = 0.92
#: A regex rule that also identified what the value is.
RULE_CLASSIFIED: Final = 0.7
#: A regex rule that found a value but could not label it.
RULE_UNCLASSIFIED: Final = 0.5
#: An LLM answer whose supporting quote was found verbatim in the chunk.
LLM: Final = 0.75
#: Ceiling for an LLM free-text value that cannot be found in the quote it cites. The quote is
#: real, so the fact is kept, but nothing ties the value to it; it ranks below every rule.
LLM_UNGROUNDED: Final = 0.5
#: An LLM answer to a free-form question.
LLM_ANSWER: Final = 0.7
#: An unlabelled date in the document header, promoted to "document date".
HEADER_DATE: Final = 0.55

#: Added when two independent sources agree, and the ceiling that agreement can reach.
CROSS_SOURCE_BONUS: Final = 0.2
CROSS_SOURCE_CAP: Final = 0.98
#: Added when the same source repeats itself, which is much weaker evidence.
SAME_SOURCE_BONUS: Final = 0.05
SAME_SOURCE_CAP: Final = 0.95

#: Order used when two sources disagree: the earlier wins.
SOURCE_PRIORITY: Final = ("both", "anchor", "rules", "llm")

# --- closed-answer-space calls ------------------------------------------------------------------
#
# A choice answer is evidence of a different kind from the ones above: the model could not invent
# the value, because the answer space was fixed before the call. That makes it safe from
# hallucination but says nothing about whether it read the document correctly, so it is placed
# alongside a classified rule rather than above an anchor.
#
# The probability the model reports is NOT used as the confidence. Measured on gemma3:4b it is
# saturated — p = 1.0 on questions with an obvious answer, even at temperature 1 — so treating it
# as a calibrated probability would manufacture certainty. It is used only to *withhold* a fact
# when the model was unsure, never to promote one.

#: A value decided by picking from a fixed option set.
CHOICE: Final = 0.7
#: Below this renormalised probability the choice is treated as undecided and no fact is emitted.
CHOICE_MIN_CONFIDENCE: Final = 0.75
#: Subtracted from an LLM fact whose verifier answered "no" — the quote was real but the model
#: would not stand behind what it means. The fact is kept, and demoted below a classified rule.
VERIFIER_PENALTY: Final = 0.25
