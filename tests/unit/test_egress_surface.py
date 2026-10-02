"""What in ``src/`` can open a connection, start a process or run text: a short, pinned list.

"Nothing leaves this computer" is easiest to keep when there is almost nothing that could. This
reads every module's syntax tree and fails when a new import or call appears that could reach a
network, run a program, load code from data, or deserialise something untrusted - so adding one is
a decision somebody makes on purpose, in this file, and not a side effect of a dependency.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "procurement_agent"

#: Modules that can reach a network, and the only file that may import each.
NETWORK_IMPORTS = {
    "httpx": {"llm/ollama_client.py"},  # the one outbound path: the model server
    "socket": {"security/egress.py"},  # the guard that watches every other one
}
#: Raw C bindings, and the only file that may import each. PDFium's own API, for what its Python
#: wrapper does not offer: the highlights and notes the annotated PDF carries. Fixed setters on
#: annotations the module itself creates, under PDFIUM_LOCK - no library is loaded, nothing
#: reaches a network; ``ctypes`` itself stays forbidden below.
FFI_IMPORTS = {
    "pypdfium2.raw": {"report/pdf_annotate.py"},
    "pypdfium2_raw": set(),
}

#: Never, anywhere in the application.
FORBIDDEN_IMPORTS = {
    "requests",
    "urllib.request",
    "urllib3",
    "http.client",
    "http.server",
    "aiohttp",
    "ftplib",
    "smtplib",
    "poplib",
    "imaplib",
    "telnetlib",
    "xmlrpc",
    "websockets",
    "websocket",
    "paramiko",
    "pycurl",
    "socketserver",
    "ssl",
    "subprocess",
    "multiprocessing",
    "pickle",
    "marshal",
    "shelve",
    "ctypes",
    "webbrowser",
    "code",
    "pty",
}
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__"}
FORBIDDEN_ATTRIBUTE_CALLS = {
    "os.system",
    "os.popen",
    "os.execv",
    "os.execve",
    "os.execl",
    "os.execlp",
    "os.spawnl",
    "os.spawnv",
    "importlib.import_module",
    "yaml.load",
    "yaml.unsafe_load",
    "yaml.full_load",
}


def _modules() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _imports(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            for alias in node.names:
                found.add(f"{node.module}.{alias.name}")
    return found


def _under(name: str, banned: set[str]) -> bool:
    return any(name == b or name.startswith(b + ".") for b in banned)


def _dotted(node: ast.expr) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def test_the_source_tree_is_found() -> None:
    assert len(_modules()) > 60  # a moved directory must not turn every check below into a no-op


def test_no_forbidden_module_is_imported() -> None:
    offenders: list[str] = []
    for path in _modules():
        for name in _imports(ast.parse(path.read_text(encoding="utf-8"))):
            if _under(name, FORBIDDEN_IMPORTS):
                offenders.append(f"{path.relative_to(SRC)} imports {name}")
    assert offenders == []


@pytest.mark.parametrize("module", sorted(FFI_IMPORTS))
def test_each_raw_binding_is_imported_by_its_one_owner(module: str) -> None:
    importers = {
        str(p.relative_to(SRC))
        for p in _modules()
        if any(_under(n, {module}) for n in _imports(ast.parse(p.read_text(encoding="utf-8"))))
    }
    assert importers == FFI_IMPORTS[module]


@pytest.mark.parametrize("module", sorted(NETWORK_IMPORTS))
def test_each_network_capable_module_is_imported_by_its_one_owner(module: str) -> None:
    importers = {
        str(p.relative_to(SRC))
        for p in _modules()
        if any(_under(n, {module}) for n in _imports(ast.parse(p.read_text(encoding="utf-8"))))
    }
    assert importers == NETWORK_IMPORTS[module]


def test_no_code_is_run_from_data() -> None:
    offenders: list[str] = []
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            called = _dotted(node.func)
            if called in FORBIDDEN_CALLS or called in FORBIDDEN_ATTRIBUTE_CALLS:
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno} calls {called}")
    assert offenders == []


def test_the_one_http_client_neither_follows_redirects_nor_reads_the_proxy_environment() -> None:
    tree = ast.parse((SRC / "llm" / "ollama_client.py").read_text(encoding="utf-8"))
    clients = [
        n for n in ast.walk(tree) if isinstance(n, ast.Call) and _dotted(n.func) == "httpx.Client"
    ]
    assert len(clients) == 1, "the model client is the only HTTP client the application has"
    keywords = {k.arg: k.value for k in clients[0].keywords}
    for flag in ("trust_env", "follow_redirects"):
        value = keywords[flag]
        assert isinstance(value, ast.Constant) and value.value is False, flag


def test_the_guard_itself_is_checked_by_this_file() -> None:
    """If the walker could not see a violation, the checks above would pass for the wrong reason."""
    tree = ast.parse(
        "\n".join(["import subprocess", "from urllib import request", "import urllib.request as u"])
    )
    found = _imports(tree)
    assert {"subprocess", "urllib.request"} <= found
    assert all(_under(n, FORBIDDEN_IMPORTS) for n in ("subprocess", "urllib.request"))
    calls = [
        _dotted(n.func)
        for n in ast.walk(ast.parse("\n".join(["os.system('x')", "eval('1')"])))
        if isinstance(n, ast.Call)
    ]
    assert set(calls) == {"os.system", "eval"}
    assert set(calls) <= FORBIDDEN_CALLS | FORBIDDEN_ATTRIBUTE_CALLS
