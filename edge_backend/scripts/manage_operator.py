#!/usr/bin/env python3
"""Local recovery tool for dashboard operator accounts.

An offline edge box has no e-mail recovery. Whoever can run this script with
read/write access to the database file already controls the machine, which is
the proof of ownership used instead.

Run it from edge_backend/ with the app's Python so it picks up the same .env
and database as the server:

    ../.venv_test/bin/python scripts/manage_operator.py list
    ../.venv_test/bin/python scripts/manage_operator.py reset-password --username manager
    ../.venv_test/bin/python scripts/manage_operator.py reset-setup
    ../.venv_test/bin/python scripts/manage_operator.py create --username manager

On a systemd install, run it as the service user (e.g. ``sudo -u <user>``).

reset-password and reset-setup both sign out every existing session
(dashboard and paired apps). reset-setup backs the database up first.

Options:
    --db PATH   operate on this database file instead of the configured one
                (the setup code file then goes next to it).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
from pathlib import Path

EDGE_BACKEND = Path(__file__).resolve().parent.parent


def _configure_env(db: str | None) -> None:
    """Must run before anything imports app.config (paths resolve at import)."""
    if db:
        db_path = Path(db).expanduser().resolve()
        os.environ["DATABASE_PATH"] = str(db_path)
        os.environ["SQLITE_DB_PATH"] = str(db_path)
        os.environ.setdefault("STORAGE_DIR", str(db_path.parent))
    # The server runs from edge_backend/ and reads edge_backend/.env; do the
    # same so the default database is the one the server uses.
    os.chdir(EDGE_BACKEND)
    os.environ["SQL_ECHO"] = "false"
    if str(EDGE_BACKEND) not in sys.path:
        sys.path.insert(0, str(EDGE_BACKEND))
    # Messages are printed explicitly; keep library logging quiet.
    logging.getLogger("edge").addHandler(logging.NullHandler())
    logging.getLogger("edge").propagate = False
    logging.getLogger("BackupService").setLevel(logging.WARNING)


def _read_new_password(args, username: str) -> str:
    from app.services.auth_service import password_policy_error

    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        if not sys.stdin.isatty():
            raise SystemExit("No terminal to prompt on; pass the password with --password-stdin.")
        password = getpass.getpass(f"New password for {username}: ")
        again = getpass.getpass("Repeat the new password: ")
        if password != again:
            raise SystemExit("Passwords do not match; nothing was changed.")
    problem = password_policy_error(password, username)
    if problem:
        raise SystemExit(f"{problem} Nothing was changed.")
    return password


async def _prepare():
    from app.database import engine
    from app.models.db_models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def cmd_list(args) -> int:
    from sqlalchemy import select
    from app.database import async_session_factory
    from app.models.db_models import AdminUserModel
    from app.services.setup_service import setup_code_manager, setup_service

    async with async_session_factory() as session:
        users = (await session.execute(select(AdminUserModel).order_by(AdminUserModel.created_at))).scalars().all()
        completed = await setup_service.is_setup_completed(session)
    if not users:
        print("No operator accounts. The dashboard will show the first-run form.")
        if setup_code_manager.read():
            print("A setup code has been issued; it is in the server log and in the setup code file.")
        return 0
    print(f"{'USERNAME':<24} {'ROLE':<8} {'ACTIVE':<6} {'CREATED (UTC)':<20} LAST SIGN-IN (UTC)")
    for u in users:
        created = u.created_at.strftime("%Y-%m-%d %H:%M:%S") if u.created_at else "-"
        last = u.last_login.strftime("%Y-%m-%d %H:%M:%S") if u.last_login else "never"
        active = "no" if u.is_active is False else "yes"
        print(f"{u.username:<24} {u.role:<8} {active:<6} {created:<20} {last}")
    print(f"\nSetup marked complete: {'yes' if completed else 'no'}")
    return 0


async def cmd_reset_password(args) -> int:
    from sqlalchemy import select
    from app.database import async_session_factory
    from app.models.db_models import AdminUserModel
    from app.services.auth_service import bump_auth_epoch, hash_password

    async with async_session_factory() as session:
        user = (await session.execute(
            select(AdminUserModel).where(AdminUserModel.username == args.username)
        )).scalar_one_or_none()
        if user is None:
            print(f"No operator named {args.username!r}. Run 'list' to see accounts.", file=sys.stderr)
            return 2
        password = _read_new_password(args, user.username)
        user.password_hash = hash_password(password)
        user.is_active = True
        await session.commit()
        await bump_auth_epoch(session)
    print(f"Password for {args.username!r} changed. All existing sessions were signed out.")
    return 0


async def cmd_create(args) -> int:
    from fastapi import HTTPException
    from app.database import async_session_factory
    from app.services.auth_service import auth_service, username_policy_error
    from app.services.setup_service import setup_code_manager

    problem = username_policy_error(args.username)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    password = _read_new_password(args, args.username)
    async with async_session_factory() as session:
        try:
            await auth_service.create_admin_user(
                session, args.username, password, args.display_name or args.username, args.role
            )
        except HTTPException as exc:
            print(f"Not created: {exc.detail}", file=sys.stderr)
            return 2
    # An account now exists, so the first-run code must no longer work.
    setup_code_manager.invalidate()
    print(f"Operator {args.username!r} ({args.role}) created. Sign in on the dashboard.")
    return 0


def _backup_database() -> str:
    from app.config import settings
    from app.services.backup_service import BackupService

    db_path = Path(settings.DATABASE_PATH)
    if not db_path.exists():
        return "(no database file yet; nothing to back up)"
    svc = BackupService(backups_dir=db_path.parent / "backups", db_path=db_path)
    return svc.create_backup(tag="pre_reset_setup")["filepath"]


async def cmd_reset_setup(args) -> int:
    from sqlalchemy import delete, func, select
    from app.config import settings
    from app.database import async_session_factory
    from app.models.db_models import AdminUserModel
    from app.services.auth_service import bump_auth_epoch
    from app.services.setup_service import setup_code_manager, setup_code_path, setup_service

    async with async_session_factory() as session:
        count = await session.scalar(select(func.count(AdminUserModel.id)))

    print(f"Database: {settings.DATABASE_PATH}")
    print(f"This deletes all {count} operator account(s), signs out every session and "
          "returns the dashboard to first-run setup. Cameras, zones and analytics are kept.")
    if not args.yes:
        if not sys.stdin.isatty():
            raise SystemExit("Refusing without confirmation; re-run with --yes.")
        if input("Type RESET to continue: ").strip() != "RESET":
            print("Cancelled; nothing was changed.")
            return 1

    backup = _backup_database()
    print(f"Backup: {backup}")

    async with async_session_factory() as session:
        await session.execute(delete(AdminUserModel))
        await session.commit()
        await setup_service.reset_setup_state(session)
        await bump_auth_epoch(session)

    code = setup_code_manager.generate("issued by manage_operator.py reset-setup")
    print("\nOperator accounts removed. Open the dashboard and create the owner account.")
    print(f"\n    SETUP CODE:  {code}\n")
    print(f"(Also saved to {setup_code_path()}, readable only by this user.)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Recover or manage dashboard operator accounts (local access only).")
    p.add_argument("--db", help="database file to operate on (default: the server's configured database)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list operator accounts (never shows password hashes)")

    rp = sub.add_parser("reset-password", help="set a new password for an existing operator")
    rp.add_argument("--username", required=True)
    rp.add_argument("--password-stdin", action="store_true", help="read the new password from standard input")

    rs = sub.add_parser("reset-setup", help="delete all operators and return the dashboard to first-run")
    rs.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    cr = sub.add_parser("create", help="create an operator account")
    cr.add_argument("--username", required=True)
    cr.add_argument("--display-name", default="")
    cr.add_argument("--role", default="owner", choices=("owner", "admin", "operator"))
    cr.add_argument("--password-stdin", action="store_true", help="read the password from standard input")
    return p


COMMANDS = {
    "list": cmd_list,
    "reset-password": cmd_reset_password,
    "reset-setup": cmd_reset_setup,
    "create": cmd_create,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_env(args.db)

    async def run() -> int:
        from app.database import engine

        try:
            await _prepare()
            return await COMMANDS[args.command](args)
        finally:
            await engine.dispose()

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
