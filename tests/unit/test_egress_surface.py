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

import procurement_agent

#: The package as Python imports it - the source under test, a mutated copy of it included
#: (scripts/mutate_security.py) - rather than whatever sits beside this file.
SRC = Path(procurement_agent.__file__).resolve().parent

#: Modules that can reach a network, and the only file that may import each.
NETWORK_IMPORTS = {
    "httpx": {"llm/ollama_client.py"},  # the one outbound path to a host: the model server
    "socket": {
        "security/egress.py",  # the guard that watches every other one
        # The second sanctioned path, and Unix sockets only (``UNIX_ONLY`` below): the parser
        # container's socket, from the client in the web process and in the service itself. The
        # framing (``parser_service/protocol.py``) opens nothing and imports no socket.
        "ingestion/parser_client.py",
        "parser_service/server.py",
    },
}
#: The modules that open a socket for the parser, and the only ``socket`` names they may use: one
#: stream socket of the Unix family. An internet socket, a lookup or ``create_connection`` in
#: either would be a second way out that nothing below would notice.
UNIX_ONLY = {"ingestion/parser_client.py", "parser_service/server.py"}
UNIX_ONLY_NAMES = {"socket.socket", "socket.AF_UNIX", "socket.SOCK_STREAM", "socket.SHUT_WR"}
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


def _not_unix_only(tree: ast.AST) -> list[str]:
    """What a module reaches in ``socket`` beyond ``UNIX_ONLY_NAMES``, and every ``socket.socket``
    call that does not name the Unix family first."""
    found: list[str] = []
    for node in ast.walk(tree):
        reached = _dotted(node) if isinstance(node, ast.Attribute) else ""
        if reached.startswith("socket.") and reached not in UNIX_ONLY_NAMES:
            found.append(reached)
        elif (
            isinstance(node, ast.Call)
            and _dotted(node.func) == "socket.socket"
            and (not node.args or _dotted(node.args[0]) != "socket.AF_UNIX")
        ):
            found.append("socket.socket() without socket.AF_UNIX first")
    return found


@pytest.mark.parametrize("module", sorted(UNIX_ONLY))
def test_the_parser_sockets_are_unix_stream_sockets_and_nothing_else(module: str) -> None:
    tree = ast.parse((SRC / module).read_text(encoding="utf-8"))
    assert _not_unix_only(tree) == []
    opened = [
        n for n in ast.walk(tree) if isinstance(n, ast.Call) and _dotted(n.func) == "socket.socket"
    ]
    assert opened, "the module opens a socket: if it no longer does, drop it from UNIX_ONLY"
    assert not any(
        isinstance(n, ast.ImportFrom) and n.module == "socket" for n in ast.walk(tree)
    ), "import the module, not names from it: the check above reads `socket.<name>`"


def test_the_unix_only_check_sees_what_it_is_meant_to_see() -> None:
    bad = ast.parse(
        "\n".join(
            [
                "import socket",
                "socket.socket(socket.AF_INET, socket.SOCK_STREAM)",
                "socket.create_connection(('x', 1))",
                "socket.getaddrinfo('x', 1)",
                "socket.socket()",
            ]
        )
    )
    found = _not_unix_only(bad)
    assert "socket.AF_INET" in found and "socket.create_connection" in found
    assert "socket.getaddrinfo" in found
    assert found.count("socket.socket() without socket.AF_UNIX first") == 2
    good = ast.parse("import socket\nsocket.socket(socket.AF_UNIX, socket.SOCK_STREAM)")
    assert _not_unix_only(good) == []


#: Names with which Python code reaches a Unix socket (``uds`` is what httpx and aiohttp call it).
UNIX_NAMES = {
    "AF_UNIX",
    "create_unix_connection",
    "open_unix_connection",
    "create_unix_server",
    "start_unix_server",
    "UnixSocketTransport",
    "uds",
}


def test_only_the_parser_modules_and_the_guard_name_a_unix_socket() -> None:
    """The guard used to let every Unix address through; now it lets one path through. If another
    module of ``src`` connected to a Unix socket it would have been working by that exemption."""
    owners = UNIX_ONLY | {"security/egress.py"}
    offenders: list[str] = []
    for path in _modules():
        relative = str(path.relative_to(SRC))
        if relative in owners:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            name = (
                node.attr
                if isinstance(node, ast.Attribute)
                else node.id
                if isinstance(node, ast.Name)
                else node.arg
                if isinstance(node, ast.keyword)
                else None
            )
            if name in UNIX_NAMES:
                offenders.append(f"{relative}:{node.lineno} uses {name}")
    assert offenders == []


def test_the_wire_format_opens_nothing() -> None:
    names = _imports(ast.parse((SRC / "parser_service/protocol.py").read_text("utf-8")))
    assert not {n for n in names if _under(n, {"socket", "ssl", "httpx", "os", "subprocess"})}


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
