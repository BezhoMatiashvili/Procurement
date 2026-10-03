"""The relay's configuration (the Caddyfile inside docker-compose.yml), asserted as text.

The relay sits between the host's reverse proxy and the app. Three properties of it are kept here
because each one failed once against a real three-container stack and none shows in the app's own
tests:

* an oversize body is refused *by the relay*, with the app's table of caps (``body_limit``): the app
  answers 413 early and closes while the relay is still sending, and the relay then turned the reset
  into a 502;
* the relay cannot resolve public names (a DNS query is an exfiltration channel even when TCP out
  is impossible);
* no log line of the relay holds a credential.

Nothing here needs Docker: the Caddyfile is parsed out of the compose file the way compose would
hand it to the container.
"""

from __future__ import annotations

import ipaddress
import json
import re
import shlex
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from procurement_agent.config import SMALL_BODY_BYTES, Settings
from procurement_agent.main import create_app
from procurement_agent.security.body_limit import _TOO_LARGE
from procurement_agent.security.headers import COMMON, ERROR_CSP, HSTS
from procurement_agent.security.preauth import upload_routes

ROOT = Path(__file__).resolve().parents[2]
PROD: dict[str, Any] = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
PROXY: dict[str, Any] = PROD["services"]["proxy"]
DEV: dict[str, Any] = yaml.safe_load((ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8"))


# --- reading the Caddyfile the way compose and Caddy would ---------------------------------------


def _interpolate(text: str) -> str:
    """Compose's substitution: ``${NAME:-default}`` gives the default, ``$$`` gives ``$``."""
    text = re.sub(r"\$\{[A-Z_]+:-([^}]*)\}", r"\1", text)
    return text.replace("$$", "$")


@dataclass
class Node:
    """One Caddyfile line; ``body`` holds the lines of the block it opens."""

    head: str
    body: list[Node] = field(default_factory=list)

    def find(self, prefix: str) -> list[Node]:
        return [n for n in self.body if n.head == prefix or n.head.startswith(prefix + " ")]

    def only(self, prefix: str) -> Node:
        found = self.find(prefix)
        assert len(found) == 1, (prefix, [n.head for n in self.body])
        return found[0]

    def walk(self) -> Iterator[Node]:
        for node in self.body:
            yield node
            yield from node.walk()


def _parse(text: str) -> Node:
    """A line-oriented reader: the file is written one directive per line, a block opens with a
    trailing ``{`` and closes with a ``}`` on a line of its own."""
    root = Node("")
    stack = [root]
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line == "}":
            stack.pop()
        elif line.endswith("{") and not line.endswith("{$"):
            node = Node(line[:-1].strip())
            stack[-1].body.append(node)
            stack.append(node)
        else:
            stack[-1].body.append(Node(line))
    assert len(stack) == 1, "unbalanced braces in the Caddyfile"
    return root


def _expand(node: Node, snippets: dict[str, Node], args: tuple[str, ...] = ()) -> Node:
    """Replace each ``import name arg...`` by the body of the snippet ``(name)``, its
    ``{args[n]}`` placeholders filled in, as Caddy does."""
    body: list[Node] = []
    for child in node.body:
        if child.head.startswith("import "):
            name, *given = child.head.split()[1:]
            body.extend(_expand(snippets[name], snippets, tuple(given)).body)
            continue
        head = child.head
        for index, value in enumerate(args):
            head = head.replace(f"{{args[{index}]}}", value)
        body.append(Node(head, _expand(child, snippets, args).body))
    return Node(node.head, body)


def caddyfile() -> Node:
    """The relay's config with snippets expanded: ``.only(':8080')`` is the site, ``.body[0]`` the
    global options."""
    raw = PROXY["environment"]["CADDYFILE"]
    tree = _parse(_interpolate(raw))
    snippets = {
        n.head[1:-1]: n for n in tree.body if n.head.startswith("(") and n.head.endswith(")")
    }
    top = Node("", [n for n in tree.body if n.head not in {f"({s})" for s in snippets}])
    return _expand(top, snippets)


def _bytes(size: str) -> int:
    """A Caddy size. Caddy reads ``MB`` as 10^6 and ``MiB`` as 2^20; only an exact count or the
    binary spelling is accepted here, so a unit cannot quietly mean the other thing."""
    plain = re.fullmatch(r"\d+", size)
    if plain:
        return int(size)
    binary = re.fullmatch(r"(\d+)(KiB|MiB|GiB)", size)
    assert binary, f"ambiguous or unreadable size {size!r}: write bytes or KiB/MiB/GiB"
    return int(binary.group(1)) * {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}[binary.group(2)]


# --- the app's table of caps ---------------------------------------------------------------------


def _app_upload_routes(tmp_path: Path) -> frozenset[tuple[str, str]]:
    app = create_app(Settings(env="dev", data_dir=tmp_path / "data"))
    return upload_routes(app.routes)


def _upload_cap_script() -> str:
    """The lines of the relay's start command that run before the Caddyfile is written."""
    (command,) = PROXY["command"]
    script = _interpolate(command)
    head, _, _ = script.partition("printf '%s' \"$CADDYFILE\"")
    assert head.strip(), "the relay's start command must compute the upload cap first"
    return head


def _run_cap_script(max_file_mb: str | None) -> tuple[int, str]:
    """Run the real start-up lines under ``sh`` with only ``PA_MAX_FILE_MB`` set; return the exit
    status and the value of the exported upload cap."""
    env = {} if max_file_mb is None else {"PA_MAX_FILE_MB": max_file_mb}
    probe = _upload_cap_script() + '\nprintf %s "${PA_UPLOAD_MAX_BYTES:-}"'
    done = subprocess.run(
        ["/bin/sh", "-c", probe], env=env, capture_output=True, text=True, check=False, timeout=10
    )
    return done.returncode, done.stdout


# --- F1: the relay refuses an oversize body, with the app's caps ---------------------------------


def _site() -> Node:
    return caddyfile().only(":8080")


def _handler(site: Node, matcher: str | None) -> Node:
    """The ``handle`` block with this matcher (``None``: the one with no matcher)."""
    head = "handle" + (f" {matcher}" if matcher else "")
    found = [h for h in site.find("handle") if h.head == head]
    assert len(found) == 1, (matcher, [h.head for h in site.body])
    return found[0]


def _cap(handler: Node) -> str:
    """The cap the handler passes to ``request_body``; ``respond`` below must use the same."""
    sizes = [
        n.head.split(None, 1)[1] for b in handler.find("request_body") for n in b.find("max_size")
    ]
    assert len(sizes) == 1, [n.head for n in handler.body]
    return sizes[0]


def test_every_request_is_handled_by_exactly_one_capped_route() -> None:
    site = _site()
    assert [h.head for h in site.find("handle")] == ["handle @upload", "handle"]
    for handler in site.find("handle"):
        names = [n.head.split()[0] for n in handler.body]
        assert {"request_body", "respond", "reverse_proxy"} <= set(names), names
        assert names.index("request_body") < names.index("reverse_proxy"), names
    stray = {n.head.split()[0] for n in site.body} & {"reverse_proxy", "request_body", "respond"}
    assert not stray, f"{stray} outside a capped handle would bypass the body limit"


def test_the_relay_caps_non_upload_routes_at_the_apps_small_body_limit() -> None:
    assert _bytes(_cap(_handler(_site(), None))) == SMALL_BODY_BYTES


def test_the_relay_matches_exactly_the_apps_upload_routes(tmp_path: Path) -> None:
    matcher = _site().only("@upload")
    methods = {m for n in matcher.find("method") for m in n.head.split()[1:]}
    paths = {p for n in matcher.find("path") for p in n.head.split()[1:]}
    assert {(m, p) for m in methods for p in paths} == _app_upload_routes(tmp_path)
    assert all("*" not in p for p in paths), "an upload route is an exact path, not a prefix"


@pytest.mark.parametrize("megabytes", ["1", "5", "50", "123", "500"])
def test_the_relays_upload_cap_is_the_apps_for_every_per_file_limit(megabytes: str) -> None:
    status, cap = _run_cap_script(megabytes)
    assert status == 0
    assert int(cap) == Settings(max_file_mb=int(megabytes)).max_request_bytes
    # and the Caddyfile uses that value, not a number of its own
    assert _cap(_handler(_site(), "@upload")) == "{$PA_UPLOAD_MAX_BYTES}"


def test_the_relays_default_per_file_limit_is_the_apps_default() -> None:
    passed = PROXY["environment"]["PA_MAX_FILE_MB"]
    default = Settings.model_fields["max_file_mb"].default
    assert _interpolate(passed) == str(default)
    assert passed.startswith("${PA_MAX_FILE_MB:-"), "the same .env variable the app reads"
    status, cap = _run_cap_script(str(default))
    assert status == 0 and int(cap) == Settings().max_request_bytes


@pytest.mark.parametrize("junk", ["", "0", "050", "abc", "5 0", "1;id", "-3", "1.5", "$(id)"])
def test_the_relay_refuses_to_start_on_a_per_file_limit_it_cannot_read(junk: str) -> None:
    status, cap = _run_cap_script(junk)
    assert status != 0 and cap == "", "no upload cap may be exported from a value nobody checked"


@pytest.mark.parametrize("matcher", ["@upload", None])
def test_a_body_that_announces_more_than_the_cap_is_refused_before_it_is_forwarded(
    matcher: str | None,
) -> None:
    """``request_body`` alone does not do this: it lets the relay start forwarding and cuts the body
    off mid-stream, which is the race. The announced length is compared up front."""
    handler = _handler(_site(), matcher)
    cap = _cap(handler)
    (over,) = handler.find("@over")
    assert over.head.count(f"> {cap}`") == 1, over.head
    assert "Content-Length" in over.head
    (refusal,) = [n for n in handler.find("respond") if n.head.startswith("respond @over ")]
    assert " 413 " in refusal.head + " " and [c.head for c in refusal.body] == ["close"]
    body = re.search(r"`(.*)`", refusal.head)
    assert body and json.loads(body.group(1)) == {"detail": _TOO_LARGE}


def test_the_relays_refusal_carries_the_headers_every_answer_of_the_app_carries() -> None:
    handler = _handler(_site(), None)
    (block,) = handler.find("header @over")
    sent = dict(shlex.split(n.head) for n in block.body)
    expected = {
        **COMMON,
        "Content-Security-Policy": ERROR_CSP,
        "Strict-Transport-Security": HSTS,  # the relay exists only in production
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
    }
    assert sent == expected


def test_the_relay_tells_a_caller_nothing_about_the_software_that_answered() -> None:
    for matcher in ("@upload", None):
        heads = [n.head for n in _handler(_site(), matcher).body]
        assert "header -Server" in heads  # Caddy's own, on an answer the relay writes itself
        assert "header -Via" in heads  # added to proxied answers by reverse_proxy


# --- F2: the relay cannot resolve a public name --------------------------------------------------


@pytest.mark.parametrize(
    ("doc", "name"), [pytest.param(PROD, "proxy", id="prod"), pytest.param(DEV, "relay", id="dev")]
)
def test_the_relay_has_no_upstream_resolver_that_can_answer(doc: dict[str, Any], name: str) -> None:
    """Docker's embedded resolver answers the names of the other containers itself and forwards the
    rest to the host's resolvers; on the relay's routable ``edge`` network that made a query for any
    public name work although no TCP connection could leave (a DNS lookup is a way to send data
    out). ``dns:`` replaces the forwarding targets. Loopback is the one address that cannot be
    reached from the container's own network namespace: nothing listens there, the query ends in
    SERVFAIL at once, and no packet leaves the container. Measured on Docker 29.7."""
    servers = doc["services"][name].get("dns")
    assert isinstance(servers, list) and servers, "the relay must name its upstream resolvers"
    for server in servers:
        assert ipaddress.ip_address(str(server)).is_loopback, server


@pytest.mark.parametrize("doc", [pytest.param(PROD, id="prod"), pytest.param(DEV, id="dev")])
def test_no_service_names_a_resolver_outside_this_machine(doc: dict[str, Any]) -> None:
    for name, svc in doc["services"].items():
        for server in svc.get("dns", []):
            assert ipaddress.ip_address(str(server)).is_loopback, (name, server)
        assert "dns_search" not in svc and "extra_hosts" not in svc, name
        assert svc.get("network_mode") is None, name


def test_the_relay_still_finds_the_app_by_the_name_the_embedded_resolver_serves() -> None:
    """The service name is answered by Docker itself, so a loopback ``dns:`` does not hide it; it
    must be on a network the relay shares with the app."""
    assert "reverse_proxy app:8000" in _interpolate(PROXY["environment"]["CADDYFILE"])
    shared = set(PROXY["networks"]) & set(PROD["services"]["app"]["networks"])
    assert shared == {"internal"}
