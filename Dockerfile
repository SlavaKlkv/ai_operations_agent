# ── Этап сборки: установка зависимостей в виртуальное окружение ──────────────
FROM python:3.13-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1

COPY --from=ghcr.io/astral-sh/uv:latest@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 /uv /usr/local/bin/uv

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
FROM python:3.13-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b AS runtime

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

RUN mkdir -p /srv/app/data/runbooks && chown -R agent:agent /srv/app/data

USER agent
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
