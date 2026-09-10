#!/usr/bin/env python
"""
Create, inspect and repair accounts from the command line.

The API only exposes the *first* account (``POST /api/auth/bootstrap``) and, when
``ALLOW_REGISTRATION=true``, self-service sign-up. Everything else — extra
testers, a forgotten password, promoting an owner — needs a script.

    # from the repository root (or anywhere: the path bootstrap is below)
    python backend/scripts/create_user.py --email you@example.com --password 'correct horse battery'

    # same, but non-interactively safe: read the password from a file/env
    python backend/scripts/create_user.py --email you@example.com --password-file ./pw.txt
    PASSWORD='…' python backend/scripts/create_user.py --email you@example.com --password-env PASSWORD

    # create the first account as owner (equivalent to /api/auth/bootstrap)
    python backend/scripts/create_user.py --email owner@example.com --password '…' --role owner

    # reset a password / change a role / list accounts
    python backend/scripts/create_user.py --email you@example.com --password '…' --reset
    python backend/scripts/create_user.py --email you@example.com --role owner
    python backend/scripts/create_user.py --list

By default a password is *required* for a new account and is checked against the
configured policy (``PASSWORD_MIN_LENGTH``). ``--random-password`` generates a
strong one and prints it once.

Exit codes: 0 = success, 1 = usage/validation error, 2 = database error.
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import func  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.security import hash_password, password_problems  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models.models import User  # noqa: E402

VALID_ROLES = ("owner", "member")


def _resolve_password(args: argparse.Namespace) -> str:
    if args.random_password:
        return "jh-" + secrets.token_urlsafe(18)
    if args.password_env:
        value = os.environ.get(args.password_env, "")
        if not value:
            raise SystemExit(f"environment variable {args.password_env} is empty or unset")
        return value
    if args.password_file:
        try:
            with open(args.password_file, "r", encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError as exc:
            raise SystemExit(f"cannot read --password-file {args.password_file}: {exc}") from exc
    return args.password or ""


def _print_user(user: User) -> None:
    print(f"  id={user.id} email={user.email} name={user.name!r} role={user.role} "
          f"active={user.is_active} created_at={user.created_at}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create or update a JobHunter AI account")
    parser.add_argument("--email", help="account email (stored lower-cased)")
    parser.add_argument("--name", default="", help="display name (defaults to the email local-part)")
    parser.add_argument("--password", help="password (prefer --password-file / --password-env)")
    parser.add_argument("--password-file", help="read the password from a file")
    parser.add_argument("--password-env", help="read the password from an environment variable")
    parser.add_argument("--random-password", action="store_true", help="generate and print a strong password")
    parser.add_argument("--role", choices=VALID_ROLES, help="role to assign (created or existing account)")
    parser.add_argument("--reset", action="store_true", help="allow resetting the password of an existing account")
    parser.add_argument("--deactivate", action="store_true", help="deactivate an existing account")
    parser.add_argument("--list", action="store_true", dest="list_users", help="list existing accounts and exit")
    args = parser.parse_args(argv)

    if not any([args.email, args.list_users]):
        parser.error("provide --email (or --list)")

    init_db()
    db = SessionLocal()
    try:
        if args.list_users:
            users = db.query(User).order_by(User.id).all()
            print(f"{len(users)} account(s) in {settings.database_url.split('://')[0]} "
                  f"({settings.database_url.rsplit('/', 1)[-1]})")
            for user in users:
                _print_user(user)
            return 0

        email = args.email.strip().lower()
        if "@" not in email:
            parser.error("--email must be a valid email address")

        existing = db.query(User).filter(func.lower(User.email) == email).first()
        password = _resolve_password(args)

        if existing is None:
            if not password:
                parser.error("a password is required for a new account "
                             "(use --password-file, --password-env or --random-password)")
            problems = password_problems(password)
            if problems:
                print("password rejected: " + "; ".join(problems), file=sys.stderr)
                return 1
            is_first = db.query(User).count() == 0
            role = args.role or ("owner" if is_first else "member")
            user = User(
                email=email,
                name=args.name or email.split("@")[0],
                password_hash=hash_password(password),
                role=role,
                is_active=True,
                consents={},
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            print(f"created account {user.email} (role={user.role})")
            _print_user(user)
            if args.random_password:
                print(f"  password: {password}  (shown once — store it in a password manager)")
            return 0

        changed = []
        if password:
            if not args.reset:
                print(f"{email} already exists — pass --reset to change its password", file=sys.stderr)
                return 1
            problems = password_problems(password)
            if problems:
                print("password rejected: " + "; ".join(problems), file=sys.stderr)
                return 1
            # A password change must also sign the account out everywhere:
            # refresh tokens stay valid for up to REFRESH_TOKEN_DAYS otherwise.
            from datetime import datetime

            from app.models.models import RefreshToken

            existing.password_hash = hash_password(password)
            db.query(RefreshToken).filter(
                RefreshToken.user_id == existing.id, RefreshToken.revoked_at.is_(None)
            ).update({RefreshToken.revoked_at: datetime.utcnow()}, synchronize_session=False)
            changed.append("password (sessions revoked)")
        if args.role and (existing.role or "") != args.role:
            existing.role = args.role
            changed.append(f"role={args.role}")
        if args.name and existing.name != args.name:
            existing.name = args.name
            changed.append("name")
        if args.deactivate and existing.is_active:
            existing.is_active = False
            changed.append("deactivated")
        if not changed:
            print(f"{email} unchanged")
            _print_user(existing)
            return 0
        db.commit()
        db.refresh(existing)
        print(f"updated {existing.email}: " + ", ".join(changed))
        _print_user(existing)
        if args.random_password and password:
            print(f"  password: {password}  (shown once — store it in a password manager)")
        return 0
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - CLI surface: report, don't traceback
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
