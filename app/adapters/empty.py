"""Явные адаптеры отсутствия, применяемые, когда у реального источника ещё нет аналога."""

from __future__ import annotations

from datetime import datetime

from app.domain.models import ErrorGroup


class NoLogProvider:
    """Режим реальных источников никогда не подменяет отсутствующие логи синтетическими."""

    async def get_error_groups(
        self, service: str, start: datetime, end: datetime, min_count: int = 1
    ) -> list[ErrorGroup]:
        del service, start, end, min_count
        return []
