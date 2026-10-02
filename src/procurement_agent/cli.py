"""Admin CLI: API keys and maintenance.

uv run python -m procurement_agent.cli create-key alice
uv run python -m procurement_agent.cli list-keys
uv run python -m procurement_agent.cli revoke-key <key-id>
uv run python -m procurement_agent.cli sweep
uv run python -m procurement_agent.cli audit [--last 50] [--action X] [--actor Y] [--since DATE]
uv run python -m procurement_agent.cli audit-prune --older-than-days 365 --yes
uv run python -m procurement_agent.cli load-registry registry.csv --as-of 2026-09-01
uv run python -m procurement_agent.cli registry-status
uv run python -m procurement_agent.cli models-manifest write|verify
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from procurement_agent.config import Settings, get_settings
from procurement_agent.db import init_db, prune_audit, session_scope
from procurement_agent.models import ApiKey, AuditLog
from procurement_agent.security.audit import audit, audit_now
from procurement_agent.security.auth import create_api_key, revoke_api_key

#: Who the audit trail names for what an administrator does at the command line.
CLI_ACTOR = "cli"


def _as_of(value: str) -> str:
    """argparse type for --as-of: reject a non-date before anything is written."""
    from procurement_agent.registry.snapshot import parse_as_of

    try:
        return parse_as_of(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _print_audit(args: argparse.Namespace) -> None:
    with session_scope() as s:
        query = select(AuditLog).order_by(AuditLog.id.desc()).limit(max(1, min(args.last, 10_000)))
        if args.action:
            query = query.where(AuditLog.action == args.action)
        if args.actor:
            query = query.where(AuditLog.actor == args.actor)
        if args.since:
            query = query.where(AuditLog.at >= datetime.fromisoformat(args.since))
        rows = list(reversed(s.scalars(query).all()))
        for r in rows:
            record = {
                "id": r.id,
                "at": r.at.isoformat(timespec="seconds"),
                "actor": r.actor,
                "action": r.action,
                "outcome": r.outcome,
                "document_id": r.document_id,
                "job_id": r.job_id,
                "ip": r.ip,
                "detail": r.detail,
            }
            if args.json:
                print(json.dumps(record, ensure_ascii=False))
            else:
                where = f" doc={r.document_id}" if r.document_id else ""
                note = f" ({r.detail})" if r.detail else ""
                head = f"{record['at']}  {r.actor:<14} {r.action:<24} {r.outcome:<7}"
                print(f"{head} {r.ip or '-'}{where}{note}")


def _prune_audit(args: argparse.Namespace) -> int:
    if args.older_than_days < 1:
        print("--older-than-days must be at least 1", file=sys.stderr)
        return 2
    cutoff = datetime.now(UTC) - timedelta(days=args.older_than_days)
    with session_scope() as s:
        count = s.query(AuditLog).filter(AuditLog.at < cutoff.replace(tzinfo=None)).count()
    if not args.yes:
        print(f"dry run: {count} audit row(s) older than {args.older_than_days} day(s); pass --yes")
        return 0
    removed = prune_audit(cutoff)
    # recorded after the deletion, so that the act of shortening the trail is on the trail
    audit_now(
        actor=CLI_ACTOR,
        action="audit.prune",
        detail=f"{removed} row(s) older than {args.older_than_days} day(s)",
    )
    print(f"deleted {removed} audit row(s)")
    return 0


def _models_manifest(action: str, settings: Settings) -> int:
    from procurement_agent.security import models_manifest

    root = settings.docling_artifacts_path
    if root is None or not root.is_dir():
        print("PA_DOCLING_ARTIFACTS_PATH must point at the models directory", file=sys.stderr)
        return 2
    if action == "write":
        count = models_manifest.write(root)
        audit_now(actor=CLI_ACTOR, action="models.manifest", detail=f"wrote {count} file(s)")
        print(f"wrote {models_manifest.MANIFEST_NAME}: {count} file(s) in {root}")
        return 0
    checked = models_manifest.verify(root)
    for problem in checked.problems:
        print(problem)
    print(f"{checked.checked} file(s) checked, {len(checked.problems)} problem(s)")
    return 0 if checked.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="procurement-agent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_create = sub.add_parser("create-key", help="create an API key for a user")
    p_create.add_argument("user")
    sub.add_parser("list-keys")
    p_revoke = sub.add_parser("revoke-key")
    p_revoke.add_argument("key_id")
    sub.add_parser("sweep", help="delete documents past their retention date")
    p_audit = sub.add_parser("audit", help="show the newest audit rows (never any document text)")
    p_audit.add_argument("--last", type=int, default=50, help="how many rows (default 50)")
    p_audit.add_argument("--action", default=None, help="only this action, e.g. auth.denied")
    p_audit.add_argument("--actor", default=None, help="only this actor")
    p_audit.add_argument("--since", default=None, type=_as_of, help="only rows from this date on")
    p_audit.add_argument("--json", action="store_true", help="one JSON object per line")
    p_prune = sub.add_parser(
        "audit-prune",
        help="delete audit rows older than N days (the one way to shorten the append-only trail)",
    )
    p_prune.add_argument("--older-than-days", type=int, required=True)
    p_prune.add_argument("--yes", action="store_true", help="really delete (without it: a dry run)")
    p_reg = sub.add_parser(
        "load-registry",
        help="load a business-registry CSV export (id_code,name,address,directors,as_of)",
    )
    p_reg.add_argument("csv_path", type=Path)
    p_reg.add_argument(
        "--as-of", default=None, type=_as_of, help="date the export was taken (ISO, YYYY-MM-DD)"
    )
    sub.add_parser("registry-status", help="show whether a registry snapshot is loaded")
    p_models = sub.add_parser(
        "models-manifest", help="write or verify MANIFEST.sha256 for the model files"
    )
    p_models.add_argument("action", choices=["write", "verify"])
    args = parser.parse_args(argv)

    settings = get_settings()
    settings.ensure_dirs()
    init_db(settings.db_path)

    if args.cmd == "create-key":
        with session_scope() as s:
            key, row = create_api_key(s, args.user)
            audit(s, actor=CLI_ACTOR, action="key.create", detail=f"user {row.user} id {row.id}")
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
            audit(
                s,
                actor=CLI_ACTOR,
                action="key.revoke",
                outcome="ok" if ok else "denied",
                detail=f"id {args.key_id[:32]}",
            )
        print("revoked" if ok else "not found / already revoked")
        return 0 if ok else 1
    elif args.cmd == "sweep":
        from procurement_agent.jobs.worker import Worker

        n = Worker(settings, None).sweep_expired()
        audit_now(actor=CLI_ACTOR, action="retention.sweep", detail=f"{n} document(s)")
        print(f"deleted {n} expired document(s)")
    elif args.cmd == "audit":
        _print_audit(args)
    elif args.cmd == "audit-prune":
        return _prune_audit(args)
    elif args.cmd == "load-registry":
        from procurement_agent.registry.snapshot import load_snapshot

        target = settings.registry_db or (settings.data_dir / "registry.db")
        count = load_snapshot(args.csv_path, target, as_of=args.as_of)
        print(f"loaded {count} companies into {target}")
        if settings.registry_db is None:
            print(f"set PA_REGISTRY_DB={target} to enable the registry checks")
    elif args.cmd == "models-manifest":
        return _models_manifest(args.action, settings)
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
