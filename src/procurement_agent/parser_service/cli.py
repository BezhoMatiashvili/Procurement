"""``python -m procurement_agent.parser_service``: start the parser, or ask whether it is alive.

Start-up is what a production process does, in the order that matters: the models are checked
against their manifest and the egress guard is required (``validate_for_parser``) before anything is
loaded; the process is hardened (owner-only files, no core dumps); the guard is up before Docling
is imported - the library whose network habits it exists for; the kill switch takes its defaults
back; then the models load, and only then does the socket appear, so a socket that exists is a
parser that is ready. The offline switches for every library are set by importing
``procurement_agent`` itself (``procurement_agent/__init__.py``), before any of them is imported.

``--ping`` is the container's health check. It starts neither Docling nor the models, only this
module, the settings and the protocol.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from procurement_agent.config import Settings
from procurement_agent.parser_service import server
from procurement_agent.security import egress
from procurement_agent.security.hardening import harden_process

__all__ = ["main", "run"]

#: How long the health check waits for the parser to answer. A parser in the middle of a long
#: conversion answers when it is done: the container's retries are sized for that.
PING_TIMEOUT_S = 10.0


def _report_blocked(kind: str, destination: str) -> None:
    import structlog

    structlog.get_logger("procurement_agent.parser").warning(
        "egress_blocked", kind=kind, destination=destination
    )


def run(settings: Settings) -> int:
    from procurement_agent.security.audit import configure_logging

    configure_logging(json_output=settings.env == "prod")
    settings.validate_for_parser()
    restore_process = harden_process()
    try:
        if settings.egress_guard:
            egress.ensure_installed(on_block=_report_blocked)
        kill_switch = server.KillSwitch()
        kill_switch.install()
        from procurement_agent.parser_service import docling_converter

        convert_fn = docling_converter.load(settings)
        server.serve(
            convert_fn=convert_fn,
            socket_path=settings.parser_socket,
            limits=server.Limits.from_settings(settings),
            guard=kill_switch,
        )
    finally:
        restore_process()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m procurement_agent.parser_service",
        description="The parser container's service: Docling over a Unix socket.",
    )
    parser.add_argument(
        "--ping",
        action="store_true",
        help="exit 0 if a parser answers on the configured socket, 1 if not (the health check)",
    )
    args = parser.parse_args(argv)
    settings = Settings()
    if args.ping:
        egress.ensure_installed().allow_unix(settings.parser_socket)
        return 0 if server.ping(settings.parser_socket, timeout_s=PING_TIMEOUT_S) else 1
    return run(settings)
