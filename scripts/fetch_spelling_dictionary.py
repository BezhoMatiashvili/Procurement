#!/usr/bin/env python3
"""Fetch the hunspell ka_GE dictionary that the act's prose spelling check reads (AOA.SPELLING).

    uv run python scripts/fetch_spelling_dictionary.py             # into models/hunspell/
    uv run python scripts/fetch_spelling_dictionary.py --dest DIR
    uv run python scripts/fetch_spelling_dictionary.py --verify    # offline: check what is there

This is the feature's one online step, like scripts/prefetch_models.sh for the models: run it
where there is internet access and copy the directory to the server. Nothing reads the network
at runtime; without the files the check says it did not run and why.

Source: gamag/ka_GE.spell, the two files under ``dictionaries/`` at one pinned commit. The
dictionary and its affix file are MIT-licensed (the licence text is in the .aff header) and were
generated from word lists by Dato Bumbeishvili (GeoWordsDatabase, MIT) and Kevin Scannell
(Crúbadán, CC-BY 4.0). The repository's GPL-3.0 covers only its generator scripts, which are
not used. Each file must match its SHA-256 before it is written, so a changed upstream file is
an error here rather than a quiet change in what the check accepts. The Dockerfile pins the same
URLs and hashes; tests/unit/test_aoa_spelling.py fails when the two drift apart.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "26d33614360c3784c199e3a6766eb5b095a134b8"
_BASE = f"https://raw.githubusercontent.com/gamag/ka_GE.spell/{COMMIT}/dictionaries"
#: file name -> (pinned URL, SHA-256)
FILES: dict[str, tuple[str, str]] = {
    "ka_GE.aff": (
        f"{_BASE}/ka_GE.aff",
        "80d04afc4319209882a6e9be3bcb8d41543e79d94a8057c1e08b07d5e805352d",
    ),
    "ka_GE.dic": (
        f"{_BASE}/ka_GE.dic",
        "5963f268e996f1e27e17163873b4d32f41fae2a1e5bef0117126caefe62f5857",
    ),
}
DEFAULT_DEST = ROOT / "models" / "hunspell"
_TIMEOUT_S = 120
#: The .dic is 3.9 MB; a response far past that is not the file.
_MAX_BYTES = 16 * 1024 * 1024


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(url: str) -> bytes:
    if not url.startswith("https://"):
        msg = f"refusing a non-HTTPS URL: {url}"
        raise ValueError(msg)
    # a pinned HTTPS URL from the table above, never user input
    with urllib.request.urlopen(url, timeout=_TIMEOUT_S) as response:  # noqa: S310
        data: bytes = response.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        msg = f"{url} is larger than {_MAX_BYTES} bytes"
        raise ValueError(msg)
    return data


def verify(dest: Path) -> int:
    """0 when every file is there with its pinned hash, else 1."""
    status = 0
    for name, (_, digest) in FILES.items():
        target = dest / name
        if not target.is_file():
            print(f"missing  {target}", file=sys.stderr)
            status = 1
        elif sha256(target.read_bytes()) != digest:
            print(f"MISMATCH {target}: not the pinned file", file=sys.stderr)
            status = 1
        else:
            print(f"ok       {target}")
    return status


def install(dest: Path) -> int:
    """Download what is missing or different, check it, and write it in place atomically."""
    dest.mkdir(parents=True, exist_ok=True)
    for name, (url, digest) in FILES.items():
        target = dest / name
        if target.is_file() and sha256(target.read_bytes()) == digest:
            print(f"ok       {target}")
            continue
        data = fetch(url)
        got = sha256(data)
        if got != digest:
            print(f"MISMATCH {url}: sha256 {got}, pinned {digest}", file=sys.stderr)
            return 1
        # written beside the target and renamed, so a reader never sees half a file
        with tempfile.NamedTemporaryFile(dir=dest, prefix=f".{name}.", delete=False) as tmp:
            tmp.write(data)
        os.chmod(tmp.name, 0o644)  # noqa: PTH101 - the temporary file's name is a str
        Path(tmp.name).replace(target)
        print(f"fetched  {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dest", type=Path, default=DEFAULT_DEST, help=f"default: {DEFAULT_DEST}")
    ap.add_argument(
        "--verify", action="store_true", help="check the files already there; no network"
    )
    args = ap.parse_args(argv)
    return verify(args.dest) if args.verify else install(args.dest)


if __name__ == "__main__":
    raise SystemExit(main())
