"""Who is calling, and what they are allowed to do.

The approval endpoint is the reason this exists. Before authentication, the
name of the person approving a write arrived *in the request body* — which
means it was a label, not an identity: anyone could sign a decision with
someone else's address. An audit trail built on that is decoration.

So identity comes from the credential and nothing else. The request says what
to decide; who decided it is not the caller's to assert.

Two levels, because the actions genuinely differ in consequence. Any
authenticated user can start an investigation — it only reads. Approving a
write requires ``can_approve``, which is a property of the user row and not of
the request.

Tokens are stored as SHA-256 digests. They are high-entropy random strings
rather than passwords, so a slow KDF buys nothing against a brute-force that
is already infeasible; what matters is that a leaked database does not hand
over working credentials.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.base import get_session
from app.db.models import User

log = structlog.get_logger(__name__)

TOKEN_BYTES = 32
#: Префикс делает утёкший токен узнаваемым в логах и вставленном тексте,
#: а сканерам секретов даёт шаблон для поиска.
TOKEN_PREFIX = "aoa_"

bearer = HTTPBearer(auto_error=False, description="API token issued with `make token`.")


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller. The only source of an actor's identity."""

    email: str
    can_approve: bool
    #: True, если аутентификация отключена для локальной разработки.
    anonymous: bool = False

    @property
    def actor(self) -> str:
        return self.email


DEVELOPMENT_PRINCIPAL = Principal(email="anonymous@localhost", can_approve=True, anonymous=True)


def issue_token() -> tuple[str, str]:
    """A new token and the digest to store. The token is never stored."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def current_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Principal:
    """Resolve the caller, or refuse the request.

    With ``auth_enabled=false`` every caller is the development principal.
    That is a deliberate escape hatch for running the stack locally without
    issuing a token first — and it is reported by ``/health``, because a
    deployment that has it on by accident should be able to notice.
    """
    if not settings.auth_enabled:
        return DEVELOPMENT_PRINCIPAL

    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="an API token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    digest = hash_token(credentials.credentials)
    user = (
        await session.execute(
            select(User).where(User.api_token_hash == digest, User.is_active.is_(True))
        )
    ).scalar_one_or_none()

    if user is None:
        log.warning("auth.rejected", path=request.url.path)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="unknown or inactive API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return Principal(email=user.email, can_approve=user.can_approve)


async def require_approver(
    principal: Principal = Depends(current_principal),
) -> Principal:
    """Approving a write is a separate permission from starting a run.

    Read-only investigation is cheap and reversible; authorising a change to
    an external system is neither, so it is not granted by merely holding a
    valid token.
    """
    if not principal.can_approve:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=f"{principal.email} may not approve write actions",
        )
    return principal
