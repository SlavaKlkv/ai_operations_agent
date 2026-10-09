"""Диагностический отчёт, который не переносит секреты.

Отчёт для поддержки: собирает безопасное состояние приложения, а любой фрагмент,
похожий на токен, пароль, ключ или учётные данные в URL, маскируется до записи.
Такой файл можно приложить к обращению без риска раскрыть доступы.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.core.config import Settings

MASK = "<скрыто>"

#: Порядок важен: сначала вырезаются целые блоки и длинные токены, затем —
#: заголовки доступа, пары «ключ: значение» и учётные данные внутри URL.
_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    # Приватные ключи целиком.
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.S,
        ),
        MASK,
    ),
    # GitHub-токены (классические и fine-grained).
    (re.compile(r"\bgh[opusr]_[A-Za-z0-9]{20,}\b"), MASK),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), MASK),
    # Заголовки доступа вида "Bearer <token>" и "token <значение>".
    # Идут раньше пар «ключ: значение», иначе маскируется только слово Bearer.
    (re.compile(r"(?i)\b(?:bearer|token)\s+[A-Za-z0-9._\-]{8,}"), MASK),
    # Явные пары «ключ: значение» с секретным именем.
    (
        re.compile(
            r"(?i)\b(?:token|secret|password|passwd|api[_-]?key|client[_-]?secret)"
            r"\b\s*[:=]\s*['\"]?[^\s'\",}]+"
        ),
        MASK,
    ),
    # Пароль внутри строки подключения вида scheme://user:password@host.
    (
        re.compile(r"(?P<head>[a-z][a-z0-9+.\-]*://[^:/@\s]+:)['\"]?[^@\s/'\"]+(?=@)"),
        r"\g<head>" + MASK,
    ),
]


def redact(text: str) -> str:
    """Заменить всё секретоподобное в строке на маску."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def build_report(settings: Settings) -> dict[str, Any]:
    """Собрать отчёт из безопасных полей и прогнать его через маскирование."""
    report: dict[str, Any] = {
        "app_env": settings.app_env,
        "log_level": settings.log_level,
        "storage_backend": settings.storage_backend,
        "cache_backend": settings.cache_backend,
        "checkpointer": settings.checkpointer,
        "mcp_enabled": settings.mcp_enabled,
        "auth_enabled": settings.auth_enabled,
        "llm_enabled": settings.llm_enabled,
        "llm_model": settings.llm_model,
        # Адрес подключения может содержать пароль (Postgres, Redis) — маскируем.
        "database": settings.database_dsn,
        "ollama_base_url": settings.ollama_base_url,
        "prometheus_configured": bool(settings.prometheus_url),
        "github_app_configured": bool(settings.github_app_client_id and settings.github_app_slug),
    }
    # Сериализуем и маскируем текст целиком: так в отчёт не утечёт фрагмент,
    # прорвавшийся в любое из полей выше.
    return json.loads(redact(json.dumps(report, ensure_ascii=False)))


def render_report(settings: Settings) -> str:
    """Отчёт как готовый к записи JSON-текст."""
    return json.dumps(build_report(settings), ensure_ascii=False, indent=2, sort_keys=True)


def main() -> None:
    from app.core.config import get_settings

    print(render_report(get_settings()))


if __name__ == "__main__":
    main()
