#!/usr/bin/env python
"""Which characters the OCR confuses, learned from scans whose text is known.

Runs in the image, on the output of ``scripts/synthetic_scans.py`` (``X.scan.pdf`` beside the
``X.docx`` it was printed from):

    docker run --rm --entrypoint python \\
        -v "$PWD/src:/app/src:ro" -v "$PWD/scripts:/app/scripts:ro" \\
        -v "$PWD/samples:/app/samples:ro" -v "$PWD/models:/models:ro" -v "$OUT:/out" \\
        procurement-agent:0.1.0 /app/scripts/ocr_confusions.py \\
        /app/samples/synthetic_scans/test-set-2 --out /out/confusions.json --dump /out/synth

Each scan is read the way the product reads a PDF, its words are aligned with the Word file's,
and every word the OCR got almost right is compared letter by letter: which letter it read as
which, which it dropped and which it added. The counts are what ``checks/ocr.py`` is built on,
and they come from synthetic pages - not from the client's scans, which are what it is judged on.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from ocr_quality import _cer, _codes, _converter, _fold, _text  # noqa: E402

#: Two words are one word misread when they are at least this alike.
_ALIKE = 0.6


def _words(text: str) -> list[str]:
    return _fold(text).split()


def confusions(truth: str, ocr: str) -> tuple[Counter[str], Counter[str], Counter[str]]:
    """(letter read as letter, letter dropped, letter added) over the words the OCR misread."""
    subs: Counter[str] = Counter()
    dropped: Counter[str] = Counter()
    added: Counter[str] = Counter()
    a, b = _words(truth), _words(ocr)
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if op != "replace":
            continue
        for w in a[i1:i2]:
            candidates = difflib.get_close_matches(w, b[j1:j2], n=1, cutoff=_ALIKE)
            if not candidates:
                continue
            got = candidates[0]
            chars = difflib.SequenceMatcher(a=w, b=got, autojunk=False)
            for cop, c1, c2, d1, d2 in chars.get_opcodes():
                if cop == "replace" and c2 - c1 == d2 - d1:
                    for x, y in zip(w[c1:c2], got[d1:d2], strict=True):
                        subs[f"{x}>{y}"] += 1
                elif cop == "delete" or (cop == "replace" and c2 - c1 > d2 - d1):
                    dropped.update(w[c1:c2])
                elif cop in ("insert", "replace"):
                    added.update(got[d1:d2])
    return subs, dropped, added


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dump", type=Path, default=None, help="write each OCR text here")
    parser.add_argument("--mode", choices=["default", "full_page"], default="default")
    parser.add_argument("--scale", type=float, default=3.0)
    args = parser.parse_args(argv)

    converter = _converter(args.mode, args.scale, None, None)
    subs: Counter[str] = Counter()
    dropped: Counter[str] = Counter()
    added: Counter[str] = Counter()
    documents: list[dict[str, Any]] = []
    for pdf in sorted(args.folder.glob("*.scan.pdf")):
        docx = pdf.with_name(pdf.name.removesuffix(".scan.pdf") + ".docx")
        if not docx.exists():
            continue
        truth, ocr = _text(converter, docx), _text(converter, pdf)
        s, d, a = confusions(truth, ocr)
        subs.update(s)
        dropped.update(d)
        added.update(a)
        want, got = _codes(truth), _codes(ocr)
        documents.append(
            {
                "document": docx.name,
                "cer": round(_cer(truth, ocr), 4),
                "codes_read": {
                    k: f"{len(set(v) & set(got[k]))}/{len(v)}" for k, v in want.items() if v
                },
            }
        )
        if args.dump is not None:
            args.dump.mkdir(parents=True, exist_ok=True)
            (args.dump / f"{docx.stem}.ocr.txt").write_text(ocr, encoding="utf-8")
        print(f"{docx.name[:60]:<60} cer {documents[-1]['cer']:.3f} {documents[-1]['codes_read']}")
    report = {
        "documents": documents,
        "substitutions": dict(subs.most_common()),
        "dropped": dict(dropped.most_common()),
        "added": dict(added.most_common()),
    }
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {args.out}: {sum(subs.values())} substitutions, {len(subs)} distinct")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
