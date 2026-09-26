# syntax=docker/dockerfile:1

# ── Этап сборки: установка зависимостей в виртуальное окружение ──────────────
FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

# Зависимости устанавливаются первыми, в отдельном слое: они меняются гораздо
# реже приложения, поэтому правка кода не пересобирает всё дерево зависимостей.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

COPY app ./app
# Флаг --locked гарантирует сборку из версий, проверенных CI. Расхождение между
# pyproject и lock-файлом прерывает сборку вместо незаметной поставки
# непроверенной конфигурации.
RUN uv sync --locked --no-dev --no-editable

# ── Этап выполнения ──────────────────────────────────────────────────────────
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# Запуск от непривилегированного пользователя: агенту не нужны права хоста.
RUN useradd --create-home --uid 1000 agent
WORKDIR /srv/app

COPY --from=builder /opt/venv /opt/venv
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

USER agent
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
