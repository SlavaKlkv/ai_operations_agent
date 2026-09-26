"""Operator commands: ``python -m app.cli ...``

Issuing credentials is an administrative act, not an API call. There is no
endpoint that mints a token, because an endpoint that mints tokens is an
endpoint that can be asked to mint one by whoever finds it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.api.security import issue_token
from app.core.logging import configure_logging
from app.db.base import get_sessionmaker
from app.db.models import User


async def _issue(email: str, *, can_approve: bool, display_name: str) -> int:
    """Create or re-key a user and print the token once."""
    async with get_sessionmaker()() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()

        token, digest = issue_token()
        if user is None:
            user = User(
                email=email,
                display_name=display_name or email.split("@")[0],
                can_approve=can_approve,
                api_token_hash=digest,
            )
            session.add(user)
            action = "created"
        else:
            # Повторный выпуск заменяет хеш и тем самым отзывает предыдущий токен.
            user.api_token_hash = digest
            user.can_approve = can_approve
            user.is_active = True
            action = "re-keyed"
        await session.commit()

    print(f"{action} {email} (can_approve={can_approve})")
    print(f"\n  {token}\n")
    print("This is the only time the token is shown. It is stored as a SHA-256 digest.")
    return 0


async def _revoke(email: str) -> int:
    async with get_sessionmaker()() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            print(f"no user {email!r}", file=sys.stderr)
            return 1
        user.api_token_hash = None
        user.is_active = False
        await session.commit()
    print(f"revoked {email}")
    return 0


async def _list_users() -> int:
    async with get_sessionmaker()() as session:
        users = (await session.execute(select(User).order_by(User.email))).scalars().all()
    if not users:
        print("no users; issue a token with: python -m app.cli token <email>")
        return 0
    width = max(len(u.email) for u in users) + 2
    print(f"{'email':<{width}} {'approve':<8} {'active':<7} token")
    for user in users:
        print(
            f"{user.email:<{width}} {user.can_approve!s:<8} {user.is_active!s:<7} "
            f"{'set' if user.api_token_hash else 'none'}"
        )
    return 0


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="app.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    token = sub.add_parser("token", help="Issue or re-issue an API token for a user.")
    token.add_argument("email")
    token.add_argument(
        "--approve",
        action="store_true",
        help="Allow this user to approve write actions.",
    )
    token.add_argument("--name", default="", help="Display name.")

    revoke = sub.add_parser("revoke", help="Revoke a user's token and deactivate them.")
    revoke.add_argument("email")

    sub.add_parser("users", help="List users and whether they hold a token.")
    return parser.parse_args(argv)


def main() -> None:
    configure_logging("WARNING")
    args = _parse()
    match args.command:
        case "token":
            code = asyncio.run(_issue(args.email, can_approve=args.approve, display_name=args.name))
        case "revoke":
            code = asyncio.run(_revoke(args.email))
        case _:
            code = asyncio.run(_list_users())
    sys.exit(code)


if __name__ == "__main__":
    main()
