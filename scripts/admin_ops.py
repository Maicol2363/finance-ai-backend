"""Controlled Administrator provisioning (not exposed through the portal or its API).

    uv run python scripts/admin_ops.py create-admin --email owner@example.com
    uv run python scripts/admin_ops.py promote --email person@example.com
    uv run python scripts/admin_ops.py reset-mfa --email person@example.com

The password is read from an interactive prompt only, never from arguments or environment
variables. The database comes from FINANCE_AI_DATABASE_URL like the API; its host and name
are shown (never credentials) and the operator must confirm unless ``--yes`` is given.
An administrator cannot use any administrative function until MFA enrollment completes.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy.engine import make_url

from finance_ai.admin.provisioning import AdminProvisioner, ProvisioningError
from finance_ai.auth.dependencies import get_password_hasher, get_unsafe_password_provider
from finance_ai.core.config import get_settings
from finance_ai.db.session import get_session_factory


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("create-admin", "create a new administrator account"),
        ("promote", "promote an existing active account to administrator"),
        ("reset-mfa", "reset the authenticator of an administrator or auditor"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("--email", required=True)
    return parser


def _confirm(action: str, email: str, *, assume_yes: bool) -> None:
    url = make_url(get_settings().database_url)
    target = f"{url.host or 'local'}:{url.port or ''}/{url.database}"
    print(f"Database: {target}")
    print(f"Action:   {action} for {email}")
    if assume_yes:
        return
    if input("Type 'yes' to continue: ").strip().lower() != "yes":
        raise SystemExit("Cancelled.")


def _prompt_password() -> str:
    first = getpass.getpass("New administrator password: ")
    if first != getpass.getpass("Repeat the password: "):
        raise ProvisioningError("The passwords do not match.")
    return first


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    provisioner = AdminProvisioner(
        session_factory=get_session_factory(),
        password_hasher=get_password_hasher(),
        unsafe_passwords=get_unsafe_password_provider(),
    )
    try:
        _confirm(args.command, args.email, assume_yes=args.yes)
        if args.command == "create-admin":
            result = provisioner.create_admin(email=args.email, password=_prompt_password())
        elif args.command == "promote":
            result = provisioner.promote_to_admin(email=args.email)
        else:
            result = provisioner.reset_mfa(email=args.email)
    except ProvisioningError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1
    print(f"Done: {result.action} (account id {result.user_id}).")
    if args.command != "reset-mfa":
        print("The account must enroll its authenticator at the next sign-in.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
