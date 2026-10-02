#!/usr/bin/env python3
"""Keep the client's own identifiers out of the repository.

The client's real documents name real companies, people, tax IDs and bank accounts, and the rule
for this project is that none of it is ever copied into code, tests or docs. That rule has been
broken twice by accident (a party name in a comment and in a spec), so it is enforced here: the
identifiers are harvested once from the client's documents into a file OUTSIDE the repository, and
git hooks refuse a commit or a push that adds one.

    client_canary.py harvest [--source DIR ...] [--dictionary FILE]
    client_canary.py scan-diff [--cached | RANGE ...]      added lines of a diff
    client_canary.py scan-tree [PATH ...]                  files (default: everything git knows)
    client_canary.py allow REF [REF ...]                   demote reviewed public names to warnings
    client_canary.py install [--git-dir DIR]               tool copy + commit/push hooks
    client_canary.py hook pre-commit | pre-push            what the installed hooks run

Nothing readable is stored. The file holds a random salt and keyed 64-bit hashes of normalised
identifiers (tax IDs, IBANs, e-mails, phone numbers, party and person names) and of every run of
six consecutive words of the client's text; a hit is reported as ``path:line: kind`` and never
shows the text. A name that is also an ordinary dictionary word is a *soft* hit: a warning, since
refusing every use of a common Georgian word would make the hook useless. A run of twenty-five
words copied from the client's text is a hard hit (contract boilerplate is shorter than that).

A hard name hit that turns out to be a public institution (a major bank, a ministry) is demoted
with ``allow``, using the short reference the finding shows; it then only warns.

Python's standard library only, so the copy installed next to the hooks runs anywhere.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import unicodedata
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIR = Path(
    os.environ.get("PA_CANARY_DIR", str(Path.home() / ".config" / "procurement-canary"))
)
STORE_NAME = "tokens.json"
TOOL_NAME = "client_canary.py"
SHINGLE = 6  # words in one window of verbatim text
RUN_WORDS = 25  # consecutive words copied from the client's text that make a hard hit
MAX_NGRAM = 4  # the longest name (in words) that is stored and looked for
DOCUMENT = re.compile(r"\.(pdf|docx?|docm|xlsx?|xlsm|xlsb|zip|rar|7z)$", re.IGNORECASE)

_WORD = re.compile(r"\w+", re.UNICODE)
# Nine or eleven digits that are not part of a longer number or of a decimal ("1,987654321",
# "987654321.50") - but may be followed by a sentence's own comma or full stop.
_ID = re.compile(r"(?<!\d)(?<!\d[.,])(\d{9}|\d{11})(?!\d)(?![.,]\d)")
# GE, two check digits, then eighteen letters and digits, with a space allowed between any two of
# them: "GE29NB0000000101904917" and "GE29 NB00 0000 0101 9049 17" are one account.
_IBAN = re.compile(r"\bGE\d{2}(?:\s?[A-Z0-9]){18}(?![A-Z0-9])")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]*\w")
_PHONE = re.compile(r"(?<!\d)(?:\+?995[\s-]?)?5\d{2}[\s-]?\d{2}[\s-]?\d{2}[\s-]?\d{2}(?!\d)")
_ORG_KA = re.compile(r"(?:შპს|სს|სსიპ|ააიპ|კს)\s*[„\"“]\s*([^„\"“”\n]{3,60}?)\s*[”\"“]")
_ORG_EN = re.compile(r"\b([A-Z][\w&.\- ]{2,40}?)\s+(?:LLC|Ltd\.?|JSC|LTD)\b")
_PERSON_KA = re.compile(r"(?:ი/მ|ფ/პ|ინდივიდუალური მეწარმე)\s+([ა-ჰ]{3,}\s+[ა-ჰ]{3,})")
_HUNK = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def norm(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def words(text: str) -> list[str]:
    return _WORD.findall(norm(text))


def digest(salt: bytes, text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=8, key=salt).hexdigest()


# --- what is looked for ----------------------------------------------------------------------


def structured(text: str) -> Iterator[tuple[str, str]]:
    """(kind, canonical form) of every ID, bank account, e-mail and phone number in ``text``."""
    for m in _ID.finditer(text):
        yield "id", m.group(1)
    for m in _IBAN.finditer(text):
        yield "iban", re.sub(r"\s", "", m.group(0))
    for m in _EMAIL.finditer(text):
        yield "email", m.group(0).lower()
    for m in _PHONE.finditer(text):
        yield "phone", re.sub(r"\D", "", m.group(0))[-9:]


def names(text: str) -> Iterator[list[str]]:
    for pattern in (_ORG_KA, _ORG_EN, _PERSON_KA):
        for m in pattern.finditer(text):
            parts = words(m.group(1))[:MAX_NGRAM]
            if parts:
                yield parts


@dataclass(frozen=True, slots=True)
class Store:
    salt: bytes
    hard: frozenset[str]
    soft: frozenset[str]
    runs: frozenset[str]
    counts: dict[str, int]

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": 1,
                "salt": self.salt.hex(),
                "hard": sorted(self.hard),
                "soft": sorted(self.soft),
                "runs": sorted(self.runs),
                "counts": self.counts,
            }
        )

    @staticmethod
    def from_json(raw: str) -> Store:
        d = json.loads(raw)
        return Store(
            bytes.fromhex(d["salt"]),
            frozenset(d["hard"]),
            frozenset(d["soft"]),
            frozenset(d["runs"]),
            dict(d.get("counts", {})),
        )


def harvest_texts(texts: Sequence[str], *, dictionary: frozenset[str] = frozenset()) -> Store:
    """The store for the client's texts. ``dictionary`` is the set of ordinary words (casefolded):
    a one-word name among them is a soft hit, not a hard one."""
    salt = secrets.token_bytes(16)
    hard: set[str] = set()
    soft: set[str] = set()
    runs: set[str] = set()
    counts: dict[str, int] = {}
    for text in texts:
        for kind, canonical in structured(text):
            hard.add(digest(salt, canonical))
            counts[kind] = counts.get(kind, 0) + 1
        for parts in names(text):
            phrase = " ".join(parts)
            ordinary = len(parts) == 1 and (parts[0] in dictionary or len(parts[0]) < 4)
            (soft if ordinary else hard).add(digest(salt, phrase))
            kind = "name~soft" if ordinary else "name"
            counts[kind] = counts.get(kind, 0) + 1
        stream = words(text)
        for i in range(len(stream) - SHINGLE + 1):
            runs.add(digest(salt, " ".join(stream[i : i + SHINGLE])))
    counts["text windows"] = len(runs)
    return Store(salt, frozenset(hard), frozenset(soft), frozenset(runs), counts)


# --- looking for it --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    line: int
    kind: str  # id, iban, email, phone, name, name~soft, text
    ref: str = ""  # the first characters of a name's hash: enough to ``allow`` it, nothing more

    @property
    def hard(self) -> bool:
        return self.kind != "name~soft"

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: client {self.kind}" + (
            f" [#{self.ref}]" if self.ref else ""
        )


def scan_lines(path: str, lines: Sequence[tuple[int, str]], store: Store) -> list[Finding]:
    found: set[Finding] = set()
    stream: list[tuple[int, str]] = []
    for number, line in lines:
        for kind, canonical in structured(line):
            if digest(store.salt, canonical) in store.hard:
                found.add(Finding(path, number, kind))
        stream.extend((number, w) for w in words(line))
    for n in range(1, MAX_NGRAM + 1):
        for i in range(len(stream) - n + 1):
            if n == 1 and stream[i][1].isdigit():
                continue  # a number is an ID only by the pattern above, with its boundary rules
            h = digest(store.salt, " ".join(w for _, w in stream[i : i + n]))
            if h in store.hard:
                found.add(Finding(path, stream[i][0], "name", h[:6]))
            elif h in store.soft:
                found.add(Finding(path, stream[i][0], "name~soft", h[:6]))
    # a run of RUN_WORDS consecutive words copied from the client's text
    hits = [
        digest(store.salt, " ".join(w for _, w in stream[i : i + SHINGLE])) in store.runs
        for i in range(len(stream) - SHINGLE + 1)
    ]
    start: int | None = None
    for i, hit in enumerate([*hits, False]):
        if hit and start is None:
            start = i
        elif not hit and start is not None:
            if (i - start) + SHINGLE - 1 >= RUN_WORDS:
                found.add(Finding(path, stream[start][0], "text"))
            start = None
    return sorted(found, key=lambda f: (f.path, f.line, f.kind))


def added_lines(diff: str) -> dict[str, list[tuple[int, str]]]:
    """The added lines of a unified diff (``-U0``), by path, with their new line numbers."""
    out: dict[str, list[tuple[int, str]]] = {}
    path: str | None = None
    number = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            if target == "/dev/null":
                path = None
            else:
                path = target[2:] if target.startswith("b/") else target
        elif raw.startswith("@@"):
            m = _HUNK.match(raw)
            number = int(m.group(1)) if m else 0
        elif raw.startswith("+") and not raw.startswith("+++"):
            if path is not None:
                out.setdefault(path, []).append((number, raw[1:]))
            number += 1
    return out


def scan_diff(diff: str, store: Store) -> list[Finding]:
    found: list[Finding] = []
    for path, lines in added_lines(diff).items():
        found.extend(scan_lines(path, lines, store))
    return found


# --- the client's documents -------------------------------------------------------------------


def _xml_text(data: bytes) -> str:
    s = data.decode("utf-8", "replace")
    s = re.sub(r"</w:p>|</w:tr>|<w:br/>|</row>|</si>", "\n", s)
    return re.sub(r"<[^>]+>", " ", s)


def document_text(path: Path) -> str:
    """The text of a .docx, .xlsx or .pdf (pdftotext, when installed); empty when unreadable."""
    try:
        suffix = path.suffix.lower()
        if suffix in (".docx", ".xlsx"):
            parts: list[str] = []
            with zipfile.ZipFile(path) as z:
                for name in z.namelist():
                    word_part = re.fullmatch(
                        r"word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml", name
                    )
                    if (
                        word_part
                        or name == "xl/sharedStrings.xml"
                        or name.startswith("xl/worksheets/sheet")
                    ):
                        parts.append(_xml_text(z.read(name)))
            return "\n".join(parts)
        if suffix == ".pdf":
            done = subprocess.run(
                ["pdftotext", "-q", str(path), "-"],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            return done.stdout
    except (OSError, zipfile.BadZipFile, subprocess.SubprocessError):
        pass
    return ""


def load_dictionary(path: Path | None) -> frozenset[str]:
    if path is None or not path.is_file():
        return frozenset()
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return frozenset(norm(e) for e in (line.split("/")[0].strip() for line in lines) if e)


# --- the store on disk -------------------------------------------------------------------------


def store_path() -> Path:
    return CONFIG_DIR / STORE_NAME


def save_store(store: Store) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.chmod(0o700)
    target = store_path()
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(store.to_json())
    target.chmod(0o600)
    return target


def load_store() -> Store | None:
    try:
        return Store.from_json(store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError):
        return None


# --- git ---------------------------------------------------------------------------------------


def git(*args: str, check: bool = True) -> str:
    done = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if check and done.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {done.stderr.strip()[:200]}")
    return done.stdout


def _report(findings: Sequence[Finding], documents: Sequence[str]) -> int:
    for doc in documents:
        print(f"refusing to commit (not allowed in the repository): {doc}", file=sys.stderr)
    for f in findings:
        print(("BLOCKED " if f.hard else "warning ") + str(f), file=sys.stderr)
    if documents or any(f.hard for f in findings):
        print(
            "\nThe client's identifiers and documents must not enter the repository. Replace "
            "them with synthetic values; `--no-verify` is the deliberate override.",
            file=sys.stderr,
        )
        return 1
    return 0


def _no_store() -> None:
    print(
        f"client-canary: no token file at {store_path()}; identifiers are not checked",
        file=sys.stderr,
    )


def _generic_refusals(staged: Sequence[str]) -> list[str]:
    """What ``scripts/check_no_documents.sh`` refuses among the staged names, when this checkout
    has the script: databases, keys, environment files and the client's directories - a link to one
    included, which is how a git worktree holds them - beyond the documents checked below.

    CI runs the same script, but CI runs after the push; this runs before the commit exists. A
    checkout without the script gets only the document check, and a failure to run it is not a
    refusal (the hook must never be what stops an ordinary commit).
    """
    top = git("rev-parse", "--show-toplevel", check=False).strip()
    script = Path(top) / "scripts" / "check_no_documents.sh" if top else None
    if script is None or not script.is_file() or not staged:
        return []
    try:
        done = subprocess.run(
            ["bash", str(script), *staged],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if done.returncode != 1:  # 1 = it found offenders; anything else is a script that did not run
        return []
    return [line.strip() for line in done.stderr.splitlines() if line.startswith("  ")]


def hook_pre_commit() -> int:
    staged = git("diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines()
    documents = [f for f in staged if DOCUMENT.search(f) and not f.startswith("tests/fixtures/")]
    seen = set(documents)
    documents += [entry for entry in _generic_refusals(staged) if entry.split("  ")[0] not in seen]
    store = load_store()
    findings: list[Finding] = []
    if store is None:
        _no_store()
    else:
        findings = scan_diff(git("diff", "--cached", "-U0", "--no-color", "--no-ext-diff"), store)
    return _report(findings, documents)


def hook_pre_push() -> int:
    store = load_store()
    if store is None:
        _no_store()
        return 0
    findings: list[Finding] = []
    zero = "0" * 40
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[1] == zero:
            continue
        local, remote = parts[1], parts[3]
        rev = [f"{remote}..{local}"] if remote != zero else [local, "--not", "--remotes"]
        patch = git(
            "log", "-p", "-U0", "--no-color", "--no-ext-diff", "--format=", *rev, check=False
        )
        findings.extend(scan_diff(patch, store))
    return _report(findings, [])


# --- commands ----------------------------------------------------------------------------------

_HOOK = """#!/usr/bin/env bash
# client-canary: refuses a {name} that adds the client's identifiers or documents.
# Installed by scripts/client_canary.py; the token file lives outside the repository.
top="$(git rev-parse --show-toplevel 2>/dev/null)"
tool="$top/scripts/{tool}"
[ -f "$tool" ] || tool="{config}/{tool}"
[ -f "$tool" ] || exit 0
exec python3 "$tool" hook {name} "$@"
"""


def cmd_harvest(args: argparse.Namespace) -> int:
    root = Path(git("rev-parse", "--show-toplevel", check=False).strip() or ".")
    sources = [Path(s) for s in args.source] or [root / "AI Test", root / "samples" / "AI Test"]
    dict_path = Path(args.dictionary) if args.dictionary else root / "models/hunspell/ka_GE.dic"
    dictionary = load_dictionary(dict_path)
    texts: list[str] = []
    for source in sources:
        for path in sorted(source.rglob("*")) if source.is_dir() else []:
            if path.is_file() and not path.name.startswith((".~lock", "~$")):
                text = document_text(path)
                if text.strip():
                    texts.append(text)
    if not texts:
        print("no readable client documents found", file=sys.stderr)
        return 1
    store = harvest_texts(texts, dictionary=dictionary)
    target = save_store(store)
    print(f"read {len(texts)} documents; wrote {target} ({stat.filemode(target.stat().st_mode)})")
    print("stored (hashes only):", ", ".join(f"{k}={v}" for k, v in sorted(store.counts.items())))
    return 0


def cmd_scan_diff(args: argparse.Namespace) -> int:
    store = load_store()
    if store is None:
        print(f"no token file at {store_path()}; run `harvest` first", file=sys.stderr)
        return 2
    if args.cached or not args.range:
        diff = git("diff", "--cached", "-U0", "--no-color")
    else:
        diff = git("diff", "-U0", "--no-color", *args.range)
    return _report(scan_diff(diff, store), [])


def cmd_scan_tree(args: argparse.Namespace) -> int:
    store = load_store()
    if store is None:
        print(f"no token file at {store_path()}; run `harvest` first", file=sys.stderr)
        return 2
    files = args.paths or (
        git("ls-files").splitlines()
        + git("ls-files", "--others", "--exclude-standard").splitlines()
    )
    findings: list[Finding] = []
    for name in files:
        p = Path(name)
        if not p.is_file() or DOCUMENT.search(name) or p.stat().st_size > 3_000_000:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        findings.extend(scan_lines(name, list(enumerate(text.splitlines(), 1)), store))
    return _report(findings, [])


def cmd_allow(args: argparse.Namespace) -> int:
    store = load_store()
    if store is None:
        print(f"no token file at {store_path()}; run `harvest` first", file=sys.stderr)
        return 2
    hard, soft = set(store.hard), set(store.soft)
    for ref in args.refs:
        ref = ref.lstrip("#")
        matches = [h for h in hard if h.startswith(ref)]
        if len(matches) != 1:
            print(f"#{ref}: {len(matches)} hard names match; nothing changed", file=sys.stderr)
            return 1
        hard.discard(matches[0])
        soft.add(matches[0])
    save_store(Store(store.salt, frozenset(hard), frozenset(soft), store.runs, store.counts))
    print(f"demoted {len(args.refs)} name(s) to warnings")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.chmod(0o700)
    copy = CONFIG_DIR / TOOL_NAME
    copy.write_bytes(Path(__file__).read_bytes())
    copy.chmod(0o700)
    hooks = Path(args.git_dir or git("rev-parse", "--git-common-dir").strip()) / "hooks"
    for name in ("pre-commit", "pre-push"):
        target = hooks / name
        if target.exists() and "client-canary" not in target.read_text(
            encoding="utf-8", errors="replace"
        ):
            print(f"left alone (another hook is there): {target}", file=sys.stderr)
            continue
        text = _HOOK.format(name=name, tool=TOOL_NAME, config=str(CONFIG_DIR))
        target.write_text(text, encoding="utf-8")
        target.chmod(0o755)
        print(f"installed {target}")
    print(f"tool copy: {copy}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("harvest")
    h.add_argument("--source", action="append", default=[])
    h.add_argument("--dictionary", default="")
    d = sub.add_parser("scan-diff")
    d.add_argument("--cached", action="store_true")
    d.add_argument("range", nargs="*")
    t = sub.add_parser("scan-tree")
    t.add_argument("paths", nargs="*")
    a = sub.add_parser("allow")
    a.add_argument("refs", nargs="+")
    i = sub.add_parser("install")
    i.add_argument("--git-dir", default="")
    k = sub.add_parser("hook")
    k.add_argument("which", choices=["pre-commit", "pre-push"])
    k.add_argument("rest", nargs="*")
    args = parser.parse_args(argv)
    if args.cmd == "harvest":
        return cmd_harvest(args)
    if args.cmd == "scan-diff":
        return cmd_scan_diff(args)
    if args.cmd == "scan-tree":
        return cmd_scan_tree(args)
    if args.cmd == "allow":
        return cmd_allow(args)
    if args.cmd == "install":
        return cmd_install(args)
    return hook_pre_commit() if args.which == "pre-commit" else hook_pre_push()


if __name__ == "__main__":
    sys.exit(main())
