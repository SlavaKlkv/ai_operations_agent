"""Кто вызывает и что ему разрешено делать.

Эндпоинт подтверждения — причина существования этого модуля. До аутентификации
имя человека, подтверждающего запись, приходило в теле запроса — значит, это
была метка, а не личность: любой мог подписать решение чужим адресом. Журнал
аудита, построенный на этом, — просто украшение.

Поэтому личность берётся из учётных данных и ниоткуда больше. Запрос говорит,
что решить; кто это решил — не вызывающему утверждать.

Два уровня, потому что действия действительно различаются по последствиям. Любой
аутентифицированный пользователь может запустить расследование — оно только
читает. Подтверждение записи требует can_approve, а это свойство строки
пользователя, а не запроса.

Токены хранятся как дайджесты SHA-256. Это случайные строки с высокой энтропией,
а не пароли, поэтому медленный KDF ничего не даёт против перебора, который и так
неосуществим; важно, чтобы утёкшая база не отдавала рабочие учётные данные.
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
    """Аутентифицированный вызывающий. Единственный источник личности действующего лица."""

    email: str
    can_approve: bool
    #: True, если аутентификация отключена для локальной разработки.
    anonymous: bool = False

    @property
    def actor(self) -> str:
        return self.email


DEVELOPMENT_PRINCIPAL = Principal(email="anonymous@localhost", can_approve=True, anonymous=True)


def issue_token() -> tuple[str, str]:
    """Новый токен и дайджест для хранения. Сам токен никогда не хранится."""
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
    """Определить вызывающего или отклонить запрос.

    При auth_enabled=false каждый вызывающий — это development-принципал.
    Это намеренная лазейка для локального запуска стека без предварительного
    выпуска токена — и она отражается в /health, потому что деплой, случайно
    включивший её, должен иметь возможность это заметить.
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
    """Подтверждение записи — отдельное разрешение от запуска расследования.

    Расследование только для чтения дешево и обратимо; санкционирование изменения
    во внешней системе — ни то, ни другое, поэтому оно не выдаётся просто за
    наличие действующего токена.
    """
    if not principal.can_approve:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=f"{principal.email} may not approve write actions",
        )
    return principal
