"""Find the regular expressions whose running time grows faster than their input.

A regular expression in CPython runs on the thread that called it, holding the GIL, until it is
done; one that takes a second on a thousand characters and four on two thousand is a way for a
single document to hold the whole server for as long as it likes. The generic fuzz that preceded
this (random text against every pattern) missed the contract-number pattern that froze the process
on 60 characters, because the bad input has to be *built from the pattern's own pieces*: slashes
between digits for that one, whitespace around an optional comma for the next.

So this is directed. For each compiled pattern it takes the characters the pattern itself uses -
literals, the members of every class and range, one representative of each category (``\\s``,
``\\d``, ``\\w``) - and builds strings out of them that make a backtracking engine work hardest: one
character repeated, two alternated, a short prefix or suffix around a long run. And it takes the
pattern's own words - the runs of literal characters in it, "გვერდი" in a page footer's pattern -
and sets a long run of a separator after each: a pattern that only backtracks once its word has
matched is reached no other way (security review: a footer pattern took minutes on "გვერდი"
followed by blanks). Each string is run
at two sizes under a time budget (a signal timer interrupts a runaway match, which CPython's engine
honours); a pattern is reported when it blows the budget outright, or when doubling the input more
than triples the time.

What this does not prove: it is a search, not a proof. A pattern that passes has resisted these
families, not every input. ``tests/unit/test_regex_budget.py`` runs it over the whole package, and
over the pre-fix contract-number pattern as a negative control that must be found.
"""

from __future__ import annotations

import contextlib
import importlib
import itertools
import pkgutil
import re
import signal
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from re import (
    _parser as sre_parse,  # type: ignore[attr-defined]  # the stdlib parser, for introspection
)
from types import ModuleType

import procurement_agent

#: Runs below this are noise, whatever their ratio.
_FLOOR_S = 0.004
#: Doubling the input may cost at most this much more (a linear pattern costs about 2x).
_RATIO = 3.3
SMALL, LARGE = 1500, 3000

_GENERIC = [
    " ",
    "\n",
    "\t",
    ",",
    ".",
    "/",
    "-",
    "1",
    "a",
    "A",
    "ა",
    "(",
    ")",
    ";",
    ":",
    "_",
    "№",
    '"',
]
_CATEGORY_CHARS = {
    sre_parse.CATEGORY_SPACE: [" ", "\n", "\t"],
    sre_parse.CATEGORY_DIGIT: ["1", "7"],
    sre_parse.CATEGORY_WORD: ["a", "ა", "1", "_"],
    sre_parse.CATEGORY_NOT_SPACE: ["a", "1"],
    sre_parse.CATEGORY_NOT_DIGIT: ["a", " "],
    sre_parse.CATEGORY_NOT_WORD: [" ", ",", "-"],
}


@dataclass(frozen=True)
class Finding:
    where: str
    pattern: str
    text: str
    kind: str  # "over budget" | "superlinear"
    small_s: float
    large_s: float | None

    def describe(self) -> str:
        large = "interrupted" if self.large_s is None else f"{self.large_s:.3f}s"
        head = f"{self.where}: {self.kind} on {self.text} ({self.small_s:.3f}s -> {large})"
        return f"{head}: {self.pattern[:90]}"


@dataclass
class Report:
    patterns: int = 0
    trials: int = 0
    findings: list[Finding] = field(default_factory=list)


class _OverBudgetError(Exception):
    pass


def _alarm(*_: object) -> None:
    raise _OverBudgetError


# --- discovering the patterns ------------------------------------------------------------------


def _walk(value: object, depth: int = 0) -> Iterator[re.Pattern[str]]:
    if isinstance(value, re.Pattern):
        if isinstance(value.pattern, str):
            yield value
    elif depth < 3 and isinstance(value, tuple | list | frozenset | set):
        for item in value:
            yield from _walk(item, depth + 1)
    elif depth < 3 and isinstance(value, dict):
        for item in value.values():
            yield from _walk(item, depth + 1)


_METHODS = ("match", "fullmatch", "search", "finditer", "findall", "sub", "subn", "split")


def modes_of(name: str) -> frozenset[str]:
    """How the package calls ``<name>.<method>(...)``: ``match``, ``fullmatch`` or a scan.

    An anchored call costs a pattern's own backtracking once; a scan costs it at every start
    position. Testing a pattern only the way it is used keeps an anchored ``\\s*,?\\s*...`` from
    being reported at the cost of a scan it never runs. A pattern with no visible call (passed
    around, called through a loop variable) is assumed to be scanned."""
    attr = name.rsplit(".", 1)[-1]
    found: set[str] = set()
    call = re.compile(rf"\b{re.escape(attr)}\.({'|'.join(_METHODS)})\(")
    root = Path(procurement_agent.__file__).parent
    for path in root.rglob("*.py"):
        found.update(call.findall(path.read_text(encoding="utf-8")))
    scanned = {"search", "finditer", "findall", "sub", "subn", "split"}
    modes = {
        "match" if m == "match" else "fullmatch" if m == "fullmatch" else "search" for m in found
    }
    if not found or found & scanned:
        modes.add("search")
    return frozenset(modes)


def discover(prefix: str = "procurement_agent") -> list[tuple[str, re.Pattern[str]]]:
    """Every compiled pattern held by a module-level name in the package, once each."""
    for info in pkgutil.walk_packages(procurement_agent.__path__, "procurement_agent."):
        if ".eval." in info.name or info.name.endswith((".cli", ".__main__")):
            continue
        importlib.import_module(info.name)
    seen: set[tuple[str, int]] = set()
    found: list[tuple[str, re.Pattern[str]]] = []
    for name, module in sorted(sys.modules.items()):
        if not name.startswith(prefix) or not isinstance(module, ModuleType):
            continue
        for attr, value in sorted(vars(module).items()):
            for pattern in _walk(value):
                key = (pattern.pattern, pattern.flags)
                if key not in seen:
                    seen.add(key)
                    found.append((f"{name}.{attr}", pattern))
    return found


# --- what to attack a pattern with -------------------------------------------------------------


def _chars_of(parsed: object, out: set[str], depth: int = 0) -> None:
    """Representative characters of every literal, class member and range in a parsed pattern."""
    if depth > 12:
        return
    for op, arg in parsed:  # type: ignore[attr-defined]
        if op is sre_parse.LITERAL:
            out.add(chr(arg))
        elif op is sre_parse.IN:
            for sub_op, sub_arg in arg:
                if sub_op is sre_parse.LITERAL:
                    out.add(chr(sub_arg))
                elif sub_op is sre_parse.RANGE:
                    out.update(chr(c) for c in (sub_arg[0], sub_arg[1]))
                elif sub_op is sre_parse.CATEGORY:
                    out.update(_CATEGORY_CHARS.get(sub_arg, []))
        elif op is sre_parse.CATEGORY:
            out.update(_CATEGORY_CHARS.get(arg, []))
        elif op is sre_parse.ANY:
            out.update(["a", " "])
        elif op in (sre_parse.MAX_REPEAT, sre_parse.MIN_REPEAT, sre_parse.POSSESSIVE_REPEAT):
            _chars_of(arg[2], out, depth + 1)
        elif op in (sre_parse.SUBPATTERN, sre_parse.ATOMIC_GROUP):
            _chars_of(arg[-1], out, depth + 1)
        elif op is sre_parse.BRANCH:
            for branch in arg[1]:
                _chars_of(branch, out, depth + 1)
        elif op in (sre_parse.ASSERT, sre_parse.ASSERT_NOT):
            _chars_of(arg[1], out, depth + 1)
        elif op is sre_parse.GROUPREF_EXISTS:
            for branch in arg[1:]:
                if branch is not None:
                    _chars_of(branch, out, depth + 1)


def _literal_words(parsed: object, out: set[str], depth: int = 0) -> None:
    """The runs of two or more literal characters in a parsed pattern: its own words."""
    if depth > 12:
        return
    run: list[str] = []

    def flush() -> None:
        if len(run) >= 2:
            out.add("".join(run))
        run.clear()

    for op, arg in parsed:  # type: ignore[attr-defined]
        if op is sre_parse.LITERAL:
            run.append(chr(arg))
            continue
        flush()
        if op in (sre_parse.MAX_REPEAT, sre_parse.MIN_REPEAT, sre_parse.POSSESSIVE_REPEAT):
            _literal_words(arg[2], out, depth + 1)
        elif op in (sre_parse.SUBPATTERN, sre_parse.ATOMIC_GROUP):
            _literal_words(arg[-1], out, depth + 1)
        elif op is sre_parse.BRANCH:
            for branch in arg[1]:
                _literal_words(branch, out, depth + 1)
        elif op in (sre_parse.ASSERT, sre_parse.ASSERT_NOT):
            _literal_words(arg[1], out, depth + 1)
    flush()


def words_of(pattern: re.Pattern[str]) -> list[str]:
    """A pattern's longest words first (``_literal_words``), at most four."""
    out: set[str] = set()
    with contextlib.suppress(Exception):
        _literal_words(sre_parse.parse(pattern.pattern, pattern.flags), out)
    words = [w for w in out if w.isprintable() and w.strip()]
    return sorted(words, key=lambda w: (-len(w), w))[:4]


def _rank(c: str) -> tuple[int, str]:
    """Whitespace, then separators, then digits, then letters: the pairs that make a pattern
    backtrack are a separator beside a digit or a letter (``1/1/1/``), so those have to be among
    the characters that get paired."""
    if c.isspace():
        return (0, c)
    if c.isdigit():
        return (2, c)
    if c.isalpha():
        return (3, c)
    return (1, c)


def alphabet(pattern: re.Pattern[str]) -> list[str]:
    """The pattern's own characters first (that is what it is vulnerable to), then generic ones."""
    own: set[str] = set()
    with contextlib.suppress(Exception):  # an unparsable pattern is attacked with the generic set
        _chars_of(sre_parse.parse(pattern.pattern, pattern.flags), own)
    own = {c for c in own if c.isprintable() or c in "\n\t"}
    ordered = sorted(own, key=_rank)[:14]
    return [*ordered, *(c for c in _GENERIC if c not in ordered)][:22]


def _representatives(chars: list[str]) -> list[str]:
    """Eight characters that cover every kind the pattern uses: two whitespace, four separators,
    a digit and a letter. Taking the first eight would fill them with whitespace and punctuation
    and leave out the digit that ``1/1/1/`` needs."""
    # ``chars`` lists the pattern's own characters first: keep that order, so the separators the
    # pattern itself names are chosen before the generic ones appended after them
    spaces = [c for c in chars if c.isspace()][:2]
    marks = [c for c in chars if not c.isspace() and not c.isalnum()][:4]
    digits = [c for c in chars if c.isdigit()][:1]
    letters = [c for c in chars if c.isalpha()][:1]
    picked = [*spaces, *marks, *digits, *letters]
    return [*picked, *(c for c in chars if c not in picked)][:8]


def candidates(pattern: re.Pattern[str]) -> Iterator[tuple[str, Callable[[int], str]]]:
    """``(label, make)`` pairs: ``make(n)`` builds a string of about ``n`` characters."""
    chars = alphabet(pattern)
    own = _representatives(chars)
    spaces = [c for c in chars if c.isspace()][:2] or [" "]
    for c in chars:
        yield f"{c!r}*n", lambda n, c=c: c * n
    for c, d in itertools.permutations(own, 2):
        yield f"{c!r}{d!r}*n", lambda n, c=c, d=d: (c + d) * (n // 2)
    structural = [
        (c, d) for c in own if not c.isalnum() and not c.isspace() for d in own if d.isalnum()
    ]
    for prefix, (c, d) in itertools.product(
        ["№ ", "1"], [*structural, *((d, c) for c, d in structural)][:24]
    ):
        yield (
            f"{prefix!r}+({c!r}{d!r})*n+'!'",
            lambda n, p=prefix, c=c, d=d: p + (c + d) * (n // 2) + "!",
        )
    for prefix, c, suffix in itertools.product(["1", "1 ", "№ ", "a ", "1/"], own[:6], ["x", "!"]):
        yield (
            f"{prefix!r}+{c!r}*n+{suffix!r}",
            lambda n, p=prefix, c=c, s=suffix: p + c * n + s,
        )
    for space in spaces:
        for c in own[:6]:
            yield (
                f"({c!r}{space!r}...)*n",
                lambda n, c=c, sp=space: (c + sp + sp) * (n // 3) + "!",
            )
    separators = [*spaces, *(c for c in own if not c.isalnum() and not c.isspace())][:4]
    for word in words_of(pattern):
        for c in separators:
            yield f"{word!r}+{c!r}*n+'x'", lambda n, w=word, c=c: w + c * n + "x"
            yield (
                f"({word!r}+{c!r}*k)*n",
                lambda n, w=word, c=c: (w + c * 40) * max(1, n // (len(w) + 40)),
            )


# --- measuring ---------------------------------------------------------------------------------


def _time(
    pattern: re.Pattern[str], text: str, budget_s: float, mode: str = "search"
) -> float | None:
    """Seconds one call takes (``match``, ``fullmatch`` or ``search``), or None if it ran out of
    budget and was interrupted."""
    call = getattr(pattern, mode)
    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, budget_s)
    started = time.perf_counter()
    try:
        call(text)
    except _OverBudgetError:
        return None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    return time.perf_counter() - started


def check_pattern(
    where: str,
    pattern: re.Pattern[str],
    *,
    budget_s: float = 0.25,
    small: int = SMALL,
    large: int = LARGE,
    keep: int = 3,
    trials: list[int] | None = None,
    admit: Callable[[str], str] | None = None,
) -> list[Finding]:
    """Findings for one pattern; the worst string of each kind is kept.

    ``admit`` is what the application does to text before a pattern reads it (``bound_text``);
    with it, the strings that come out are the ones a pattern can actually be handed."""
    admit = admit or (lambda text: text)
    findings: list[Finding] = []
    modes = sorted(modes_of(where))
    for label, make in candidates(pattern):
        if len(findings) >= keep:
            break  # the three worst are all anyone reads; the rest only cost time
        for mode in modes:
            if trials is not None:
                trials[0] += 1
            tag = label if mode == "search" else f"{mode}: {label}"
            first = _time(pattern, admit(make(small)), budget_s, mode)
            if first is None:
                findings.append(Finding(where, pattern.pattern, tag, "over budget", budget_s, None))
                continue
            if first < _FLOOR_S / 4:
                continue  # nowhere near: not worth a second run
            second = _time(pattern, admit(make(large)), budget_s, mode)
            if second is None or second > budget_s:
                findings.append(Finding(where, pattern.pattern, tag, "over budget", first, second))
            elif second > _FLOOR_S and second / max(first, 1e-6) > _RATIO:
                findings.append(Finding(where, pattern.pattern, tag, "superlinear", first, second))
    findings.sort(key=lambda f: -(f.large_s or 1e9))
    return findings[:keep]


def scan(
    targets: list[tuple[str, re.Pattern[str]]] | None = None,
    *,
    budget_s: float = 0.25,
    small: int = SMALL,
    large: int = LARGE,
    admit: Callable[[str], str] | None = None,
) -> Report:
    report = Report()
    trials = [0]
    for where, pattern in targets if targets is not None else discover():
        report.patterns += 1
        report.findings.extend(
            check_pattern(
                where,
                pattern,
                budget_s=budget_s,
                small=small,
                large=large,
                trials=trials,
                admit=admit,
            )
        )
    report.trials = trials[0]
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Directed search for super-linear regular expressions")
    ap.add_argument("--small", type=int, default=SMALL, help="the smaller input size (characters)")
    ap.add_argument("--large", type=int, default=LARGE, help="the larger input size (characters)")
    ap.add_argument("--budget", type=float, default=0.25, help="seconds one match may take")
    ap.add_argument("--json", type=Path, default=None, help="also write the findings here")
    ap.add_argument(
        "--through-limits",
        action="store_true",
        help="hand each string to the pattern as the application does: after bound_text",
    )
    args = ap.parse_args(argv)
    started = time.time()
    admit = None
    if args.through_limits:
        from procurement_agent.ingestion.text_limits import bound_text

        admit = bound_text
    report = scan(budget_s=args.budget, small=args.small, large=args.large, admit=admit)
    for finding in sorted(report.findings, key=lambda f: f.where):
        print(finding.describe())
    if args.json:
        args.json.write_text(
            json.dumps([f.__dict__ for f in report.findings], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    print(
        f"\n{report.patterns} patterns, {report.trials} strings, "
        f"{len(report.findings)} finding(s), {time.time() - started:.0f}s"
    )
    return 1 if report.findings else 0


if __name__ == "__main__":
    sys.exit(main())
