"""Admin CLI: API keys and maintenance.

uv run python -m procurement_agent.cli create-key alice
uv run python -m procurement_agent.cli list-keys
uv run python -m procurement_agent.cli revoke-key <key-id>
uv run python -m procurement_agent.cli sweep
uv run python -m procurement_agent.cli load-registry registry.csv --as-of 2026-09-01
uv run python -m procurement_agent.cli registry-status
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import select

from procurement_agent.config import get_settings
from procurement_agent.db import init_db, session_scope
from procurement_agent.models import ApiKey
from procurement_agent.security.auth import create_api_key, revoke_api_key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="procurement-agent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_create = sub.add_parser("create-key", help="create an API key for a user")
    p_create.add_argument("user")
    sub.add_parser("list-keys")
    p_revoke = sub.add_parser("revoke-key")
    p_revoke.add_argument("key_id")
    sub.add_parser("sweep", help="delete documents past their retention date")
    p_reg = sub.add_parser(
        "load-registry",
        help="load a business-registry CSV export (id_code,name,address,directors,as_of)",
    )
    p_reg.add_argument("csv_path", type=Path)
    p_reg.add_argument("--as-of", default=None, help="date the export was taken (ISO)")
    sub.add_parser("registry-status", help="show whether a registry snapshot is loaded")
    args = parser.parse_args(argv)

    settings = get_settings()
    settings.ensure_dirs()
    init_db(settings.db_path)

    if args.cmd == "create-key":
        with session_scope() as s:
            key, row = create_api_key(s, args.user)
            print(f"user={row.user} id={row.id}")
        print(f"API key (shown once, store it securely):\n{key}")
    elif args.cmd == "list-keys":
        with session_scope() as s:
            for k in s.scalars(select(ApiKey).order_by(ApiKey.created_at)):
                state = "revoked" if k.revoked_at else "active"
                print(f"{k.id}  {k.user:<20} {k.prefix}...  {state}  last_used={k.last_used_at}")
    elif args.cmd == "revoke-key":
        with session_scope() as s:
            ok = revoke_api_key(s, args.key_id)
        print("revoked" if ok else "not found / already revoked")
        return 0 if ok else 1
    elif args.cmd == "sweep":
        from procurement_agent.jobs.worker import Worker

        n = Worker(settings, None).sweep_expired()
        print(f"deleted {n} expired document(s)")
    elif args.cmd == "load-registry":
        from procurement_agent.registry.snapshot import load_snapshot

        target = settings.registry_db or (settings.data_dir / "registry.db")
        count = load_snapshot(args.csv_path, target, as_of=args.as_of)
        print(f"loaded {count} companies into {target}")
        if settings.registry_db is None:
            print(f"set PA_REGISTRY_DB={target} to enable the registry checks")
    elif args.cmd == "registry-status":
        from procurement_agent.registry.snapshot import open_snapshot

        snap = open_snapshot(settings.registry_db) if settings.registry_db else None
        if snap is None:
            print("no registry snapshot loaded; registry rules will be reported as skipped")
            return 0
        with snap:
            print(f"{snap.count} companies, exported {snap.as_of}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
