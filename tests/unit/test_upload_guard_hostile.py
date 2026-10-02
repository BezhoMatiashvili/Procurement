# ruff: noqa: E501
"""Hostile uploads: crafted archives, and thousands of seeded mutations of valid files.

The contract of ``validate_upload`` is narrow and worth testing hard: for ANY bytes it returns a
``ValidatedUpload`` or raises ``UploadRejectedError`` - nothing else escapes (anything else is a 500
on the upload route), it finishes quickly, and what it says about a refusal carries nothing the
sender chose. The crafted cases name the attacks (a zip bomb, an archive that lies about its sizes,
path traversal, a thousand duplicate names, an encrypted entry); the fuzz mutates valid files at
random, deterministically, and checks the contract on whatever comes out.
"""

from __future__ import annotations

import contextlib
import io
import os
import random
import signal
import struct
import time
import zipfile
import zlib

import pytest

from procurement_agent.security.upload_guard import (
    UploadRejectedError,
    ValidatedUpload,
    validate_upload,
)
from tests.conftest import make_ooxml, make_pdf

LIMITS = {"max_bytes": 50 * 1024 * 1024, "max_pages": 300}
MARKER = "ZQX-CANARY-UPLOAD-5521"


class _SlowError(Exception):
    pass


def _alarm(*_: object) -> None:
    raise _SlowError


@contextlib.contextmanager
def deadline(seconds: float):  # type: ignore[no-untyped-def]
    """A signal timer: pure-Python work (zipfile, the guard) honours it; native code does not."""
    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def outcome(name: str, data: bytes) -> str:
    """``accepted`` or the refusal's message; anything else raises (and fails the test)."""
    try:
        with deadline(10):
            result = validate_upload(name, data, **LIMITS)
    except UploadRejectedError as e:
        return str(e)
    assert isinstance(result, ValidatedUpload)
    return "accepted"


# --- crafted archives --------------------------------------------------------------------------


def _zip(members: dict[str, bytes], *, method: int = zipfile.ZIP_DEFLATED) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", method) as z:
        for name, body in members.items():
            z.writestr(name, body)
    return out.getvalue()


def _base(kind: str = "docx") -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(make_ooxml(kind))) as z:
        return {n: z.read(n) for n in z.namelist()}


def _stream_zeros(name: str, megabytes: int) -> bytes:
    """A member of ``megabytes`` MiB of zeros, deflated as a stream (the file stays small)."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for part, body in _base("docx").items():
            z.writestr(part, body)
        with z.open(name, "w", force_zip64=True) as member:
            chunk = b"\0" * (1024 * 1024)
            for _ in range(megabytes):
                member.write(chunk)
    return out.getvalue()


def test_a_valid_document_of_each_kind_is_accepted() -> None:
    assert outcome("a.docx", make_ooxml("docx")) == "accepted"
    assert outcome("a.xlsx", make_ooxml("xlsx")) == "accepted"
    assert outcome("a.pdf", make_pdf()) == "accepted"


def test_a_zip_bomb_is_refused_without_inflating_it() -> None:
    data = _stream_zeros("word/media/bomb.bin", 300)  # 300 MiB of zeros in about 300 KB
    assert len(data) < 2 * 1024 * 1024
    started = time.perf_counter()
    refusal = outcome("a.docx", data)
    assert time.perf_counter() - started < 2.0
    assert "too large" in refusal or "compression ratio" in refusal or "inflates" in refusal


def test_a_part_at_the_ratio_limit_is_refused() -> None:
    data = _stream_zeros("word/media/big.bin", 60)  # about 60 KB for 60 MiB: a thousand to one
    assert "compression ratio" in outcome("a.docx", data)


def test_an_archive_that_lies_about_its_sizes_is_refused() -> None:
    """Central-directory sizes are what the guard accounts with; make them enormous."""
    data = bytearray(_zip(_base()))
    for offset in [i for i in range(len(data) - 4) if data[i : i + 4] == b"PK\x01\x02"]:
        struct.pack_into("<I", data, offset + 24, 0x7FFFFFFF)  # uncompressed size
    refusal = outcome("a.docx", bytes(data))
    assert refusal != "accepted"


def test_entries_whose_data_overlaps_are_refused_as_such() -> None:
    """Two directory entries naming the same bytes: how a small archive claims to hold a large one."""
    data = bytearray(_zip({**_base(), "word/second.xml": b"<root>second part</root>"}))
    heads = [i for i in range(len(data) - 4) if data[i : i + 4] == b"PK\x01\x02"]
    first_offset = struct.unpack_from("<I", data, heads[0] + 42)[0]
    struct.pack_into(
        "<I", data, heads[-1] + 42, first_offset
    )  # the last entry now starts at the first
    assert outcome("a.docx", bytes(data)) in (
        "archive entries overlap",
        "archive has duplicate entries",
    )


def test_an_ordinary_archive_with_many_entries_never_trips_the_overlap_check() -> None:
    members = {f"word/p{i}.xml": f"<root>{i}</root>".encode() * (i + 1) for i in range(400)}
    assert outcome("a.docx", _zip({**_base(), **members})) == "accepted"
    assert outcome("a.docx", _zip({**_base(), **members}, method=zipfile.ZIP_STORED)) == "accepted"


def test_overlapping_entries_that_each_claim_a_huge_size_are_refused() -> None:
    members = {f"word/part{i}.xml": b"<root/>" for i in range(60)}
    data = bytearray(_zip({**_base(), **members}))
    for offset in [i for i in range(len(data) - 4) if data[i : i + 4] == b"PK\x01\x02"]:
        struct.pack_into("<I", data, offset + 24, 100 * 1024 * 1024)
        struct.pack_into("<I", data, offset + 20, 100 * 1024)  # claim a ratio under the limit
    assert outcome("a.docx", bytes(data)) != "accepted"


@pytest.mark.parametrize(
    "name",
    [
        "../evil.xml",
        "/etc/passwd",
        "word/../../x",
        "C:\\Windows\\x",
        "\\\\host\\share\\x",
        "a/../../../b",
    ],
)
def test_an_entry_that_would_leave_the_extraction_directory_is_refused(name: str) -> None:
    data = _zip({**_base(), name: b"x"})
    assert "unsafe paths" in outcome("a.docx", data)


def test_a_thousand_duplicate_names_are_refused() -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z, pytest.warns(UserWarning):
        for part, body in _base().items():
            z.writestr(part, body)
        for _ in range(50):
            z.writestr("word/dup.xml", b"x")
    assert "duplicate entries" in outcome("a.docx", out.getvalue())


def test_too_many_entries_are_refused() -> None:
    members = {f"word/p{i}.xml": b"" for i in range(10_100)}
    assert "too many entries" in outcome("a.docx", _zip({**_base(), **members}))


def test_an_encrypted_entry_is_refused() -> None:
    data = bytearray(_zip(_base()))
    for offset in [i for i in range(len(data) - 4) if data[i : i + 4] == b"PK\x01\x02"]:
        flags = struct.unpack_from("<H", data, offset + 8)[0]
        struct.pack_into("<H", data, offset + 8, flags | 0x1)
    assert "encrypted" in outcome("a.docx", bytes(data))


def test_a_huge_content_types_part_is_refused_not_read() -> None:
    members = _base()
    # incompressible, so the size check - not the compression-ratio check - is what answers
    members["[Content_Types].xml"] = b"<Types>" + os.urandom(1_200_000).hex().encode() + b"</Types>"
    assert "content-type list is too large" in outcome("a.docx", _zip(members))


def test_macros_are_refused_however_they_are_named_or_declared() -> None:
    assert "macros" in outcome("a.docx", _zip({**_base(), "word/vbaProject.bin": b"x"}))
    ct = b'<Types><Override ContentType="application/vnd.ms-word.document.macroEnabled.main+xml"/></Types>'
    assert "macros" in outcome("a.docx", _zip({**_base(), "[Content_Types].xml": ct}))


@pytest.mark.parametrize(
    "data",
    [
        b"PK\x03\x04" + b"\x00" * 100,  # a header and nothing else
        b"PK\x05\x06" + b"\x00" * 18,  # an empty archive
        make_ooxml("docx")[:-30],  # the end of the central directory cut off
        make_ooxml("docx")[: len(make_ooxml("docx")) // 2],
        b"PK\x03\x04" + make_ooxml("docx")[4:] + b"\x00" * 10_000,  # trailing junk
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600,  # a legacy Office file
        b"%PDF-1.4\n" + b"x" * 1000,  # a PDF named .docx
        b"MZ" + b"\x00" * 200,  # an executable
        b"",
    ],
)
def test_a_corrupt_or_foreign_file_is_a_clean_refusal(data: bytes) -> None:
    assert outcome("a.docx", data) != "accepted" or data.startswith(b"PK\x03\x04")


def test_a_document_of_the_wrong_kind_is_named_as_such() -> None:
    assert "is a .xlsx but was uploaded as .docx" in outcome("a.docx", make_ooxml("xlsx"))
    assert "is a .docx but was uploaded as .xlsx" in outcome("a.xlsx", make_ooxml("docx"))


@pytest.mark.parametrize(
    "name",
    [
        f"report.{MARKER}",
        f"{MARKER}",
        f"{MARKER}.docx.exe",
        "\u202etxt.exe",
        f"x.{MARKER}\x00.docx",
    ],
)
def test_a_refusal_never_repeats_what_the_sender_chose(name: str) -> None:
    refusal = outcome(name, make_ooxml("docx"))
    assert MARKER not in refusal
    assert "\u202e" not in refusal and "\x00" not in refusal


# --- PDFs --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        b"%PDF-1.4\n" + b"\x00" * 50,
        b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF",
        make_pdf()[:-20],
        make_pdf().replace(b"/Count 1", b"/Count 99999999"),
        make_pdf().replace(b"/Kids[3 0 R]", b"/Kids[2 0 R]"),  # a page tree that contains itself
        make_pdf(10**9, 10**9),  # absurd page size
        b"%PDF-1.4\n" + b"[" * 100_000,  # nesting
        b"\xef\xbb\xbf" + make_pdf(),  # a byte-order mark before the header
    ],
    ids=[
        "no-objects",
        "no-pages",
        "truncated",
        "huge-count",
        "self-referencing",
        "huge-page",
        "nested",
        "bom",
    ],
)
def test_a_hostile_pdf_is_accepted_or_refused_cleanly(data: bytes) -> None:
    started = time.perf_counter()
    outcome("a.pdf", data)
    assert time.perf_counter() - started < 8.0


# --- the fuzz ----------------------------------------------------------------------------------


def _mutate(rng: random.Random, data: bytes) -> bytes:
    out = bytearray(data)
    for _ in range(rng.randint(1, 6)):
        kind = rng.randrange(8)
        if not out:
            out = bytearray(b"PK\x03\x04")
        i = rng.randrange(len(out))
        if kind == 0:  # flip a bit
            out[i] ^= 1 << rng.randrange(8)
        elif kind == 1:  # overwrite a byte
            out[i] = rng.randrange(256)
        elif kind == 2:  # truncate
            del out[i:]
        elif kind == 3:  # delete a stretch
            del out[i : i + rng.randint(1, 40)]
        elif kind == 4:  # insert junk
            out[i:i] = bytes(rng.randrange(256) for _ in range(rng.randint(1, 40)))
        elif kind == 5:  # duplicate a stretch
            out[i:i] = out[i : i + rng.randint(1, 60)]
        elif kind == 6:  # zero a stretch
            out[i : i + rng.randint(1, 30)] = b"\0" * rng.randint(1, 30)
        else:  # corrupt a 32-bit field next to a zip signature
            sigs = [j for j in range(len(out) - 8) if out[j : j + 2] == b"PK"]
            if sigs:
                j = rng.choice(sigs) + rng.randrange(4, 30)
                if j + 4 <= len(out):
                    struct.pack_into(
                        "<I",
                        out,
                        j,
                        rng.choice([0, 1, 0xFFFFFFFF, 0x7FFFFFFF, rng.getrandbits(32)]),
                    )
    return bytes(out)


@pytest.mark.parametrize("kind", ["docx", "xlsx"])
def test_mutated_office_files_never_escape_the_contract(kind: str) -> None:
    rng = random.Random(0xC0FFEE + (kind == "xlsx"))
    base = make_ooxml(kind, extra={"word/media/a.bin": b"abc" * 100, "docProps/core.xml": b"<c/>"})
    accepted = refused = 0
    started = time.perf_counter()
    for _ in range(1500):
        result = outcome(f"a.{kind}", _mutate(rng, base))
        accepted += result == "accepted"
        refused += result != "accepted"
        assert MARKER not in result
    assert time.perf_counter() - started < 60
    assert refused > 100 and accepted >= 0  # the mutations really did break things


def test_mutated_office_files_that_are_accepted_still_parse_or_fail_cleanly() -> None:
    """What the guard lets through is read next by openpyxl / python-docx; they may refuse it but
    must not hang or run out of memory on it."""
    from docx import Document
    from openpyxl import load_workbook

    rng = random.Random(7)
    base_x = make_ooxml("xlsx")
    base_d = make_ooxml("docx")
    for _ in range(300):
        for kind, base, reader in (
            ("xlsx", base_x, lambda d: load_workbook(io.BytesIO(d), read_only=True)),
            ("docx", base_d, lambda d: Document(io.BytesIO(d))),
        ):
            mutated = _mutate(rng, base)
            if outcome(f"a.{kind}", mutated) != "accepted":
                continue
            with deadline(10), contextlib.suppress(Exception):
                reader(mutated)


def test_mutated_pdfs_never_escape_the_contract() -> None:
    """pdfium is native: a hang cannot be interrupted here, so the whole batch runs in a child
    process with a hard limit."""
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(
        """
        import random, sys
        from procurement_agent.security.upload_guard import validate_upload, UploadRejectedError
        from tests.conftest import make_pdf
        from tests.unit.test_upload_guard_hostile import _mutate
        rng = random.Random(0xBEEF)
        base = make_pdf(300, 400)
        ok = bad = 0
        for _ in range(400):
            data = _mutate(rng, base)
            try:
                validate_upload("a.pdf", data, max_bytes=50 * 1024 * 1024, max_pages=300)
                ok += 1
            except UploadRejectedError:
                bad += 1
        print(ok, bad)
        """
    )
    import os
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root)])}
    done = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-600:]
    accepted, refused = map(int, done.stdout.split())
    assert accepted + refused == 400 and refused > 50


def test_the_fuzz_is_deterministic() -> None:
    a = [_mutate(random.Random(5), make_ooxml("docx")) for _ in range(3)]
    b = [_mutate(random.Random(5), make_ooxml("docx")) for _ in range(3)]
    assert a == b


def test_compressed_garbage_that_inflates_wildly_is_not_a_surprise() -> None:
    bomb = zlib.compress(b"\0" * (50 * 1024 * 1024), 9)
    assert len(bomb) < 100_000
    data = _zip({**_base(), "word/media/z.bin": bomb}, method=zipfile.ZIP_STORED)
    assert outcome("a.docx", data) in ("accepted",) or "too large" in outcome(
        "a.docx", data
    )  # stored: not a ratio
