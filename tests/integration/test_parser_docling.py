"""The parser container's Docling, for real: a document through the socket is the document inline.

The parser here is the real service in a process of its own (``python -m
procurement_agent.parser_service``: the same command the container runs), with the real Docling
models; the web side is the real client. For each fixture the same file is converted two ways -
in this process, as the worker does inline, and through the socket - and the ``docling.json`` each
leaves is compared byte for byte. Needs the prefetched models: ./scripts/prefetch_models.sh
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from procurement_agent.config import Settings
from procurement_agent.ingestion import docling_service, parser_client
from procurement_agent.security import egress

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "models" / "docling"
FIXTURES = ROOT / "tests" / "fixtures"

pytestmark = [
    pytest.mark.docling,
    pytest.mark.skipif(not MODELS.exists(), reason="docling models not downloaded"),
]

THREADS = 8


@pytest.fixture(scope="module")
def parser() -> Iterator[Path]:
    """The service, started the way the container starts it; the socket's path once it listens."""
    short = Path(tempfile.mkdtemp(prefix="pa-pd-"))
    path = short / "parser.sock"
    env = {k: v for k, v in os.environ.items() if not k.startswith("PA_")}
    env.update(
        PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
        PYTHONDONTWRITEBYTECODE="1",
        PA_ENV="dev",
        PA_PARSER_SOCKET=str(path),
        PA_DOCLING_ARTIFACTS_PATH=str(MODELS),
        PA_DOCLING_NUM_THREADS=str(THREADS),
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "procurement_agent.parser_service"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        cwd=short,
    )
    try:
        end = time.monotonic() + 600
        while not path.exists():
            assert proc.poll() is None, (proc.stderr.read() if proc.stderr else b"")[-2000:]
            assert time.monotonic() < end, "the parser never started listening"
            time.sleep(0.2)
        egress.installed().allow_unix(path)  # type: ignore[union-attr]
        yield path
    finally:
        proc.terminate()
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(30)
        if proc.stderr:
            proc.stderr.close()
        shutil.rmtree(short, ignore_errors=True)


def _settings(socket_path: Path | None = None) -> Settings:
    return Settings(
        ollama_base_url="http://127.0.0.1:1",
        docling_artifacts_path=MODELS,
        docling_num_threads=THREADS,
        parser_mode="socket" if socket_path else None,
        parser_socket=socket_path or Path("/run/parser/parser.sock"),
        parser_connect_wait_s=5.0,
        max_file_mb=50,
    )


@pytest.mark.parametrize(
    ("fixture", "kind"),
    [
        ("offer_ka_en.pdf", "pdf"),
        ("offer_ka_en_scanned.pdf", "pdf"),
        ("contract_ka.docx", "docx"),
    ],
)
def test_docling_json_through_the_socket_is_byte_identical_to_the_inline_one(
    parser: Path, tmp_path: Path, fixture: str, kind: str
) -> None:
    doc_id = (
        uuid.uuid4().hex
    )  # what the worker names an upload; Docling writes the name into its output
    upload = tmp_path / f"{doc_id}.{kind}"
    shutil.copyfile(FIXTURES / fixture, upload)

    inline = docling_service.convert(upload, _settings())
    inline_path = docling_service.save_document(inline.document, tmp_path / "inline")
    inline_pages = len(inline.document.pages) or None

    reply = parser_client.convert_through_parser(
        upload, kind, tmp_path / "socket", _settings(parser)
    )

    through_socket = (tmp_path / "socket" / "docling.json").read_bytes()
    assert through_socket == inline_path.read_bytes()
    assert reply.pages == inline_pages
    assert docling_service.load_document(tmp_path / "socket" / "docling.json").texts is not None


def test_the_container_health_check_works_against_the_real_service(parser: Path) -> None:
    done = subprocess.run(
        [sys.executable, "-m", "procurement_agent.parser_service", "--ping"],
        env={
            **{k: v for k, v in os.environ.items() if not k.startswith("PA_")},
            "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PA_PARSER_SOCKET": str(parser),
        },
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-500:]
