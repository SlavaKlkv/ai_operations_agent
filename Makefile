.DEFAULT_GOAL := help
VENV := .venv/bin
# Рядом лежат пользовательский compose.yaml (только сервис app) и dev-стек
# docker-compose.yml. Без явного -f `docker compose` выбирает compose.yaml, поэтому
# dev-цели адресуют docker-compose.yml напрямую.
DEV_COMPOSE := docker compose -f docker-compose.yml

.PHONY: help install up down observability migrate run token users test eval lint format check clean

help:  ## Показать эту справку
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "\033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Создать виртуальное окружение и установить проект с dev-зависимостями
	uv venv --python 3.13
	uv pip install -e ".[dev]"

up:  ## Запустить PostgreSQL и Redis
	$(DEV_COMPOSE) up -d postgres redis

observability:  ## Запустить полный стек с Prometheus и Grafana
	$(DEV_COMPOSE) --profile observability up -d
	@echo "Grafana  http://localhost:$${GRAFANA_PORT:-3000}/d/ai-operations-agent"
	@echo "Metrics  http://localhost:8000/metrics"

down:  ## Остановить стек
	$(DEV_COMPOSE) --profile observability down

migrate:  ## Применить миграции базы данных
	$(VENV)/alembic upgrade head

token:  ## Выпустить API-токен: make token EMAIL=you@example.com APPROVE=1
	$(VENV)/python -m app.cli token $(EMAIL) $(if $(APPROVE),--approve,)

users:  ## Показать пользователей и наличие у них токена
	$(VENV)/python -m app.cli users

run:  ## Запустить API с автоматической перезагрузкой
	$(VENV)/uvicorn app.main:app --reload --port 8000

test:  ## Запустить тесты с измерением покрытия
	$(VENV)/pytest --cov=app --cov-report=term-missing

eval:  ## Запустить набор оценочных сценариев агента
	MCP_ENABLED=false CHECKPOINTER=memory $(VENV)/python -m app.evaluation --quiet

lint:  ## Проверить стиль и типы
	$(VENV)/ruff check .
	$(VENV)/ruff format --check .
	$(VENV)/mypy app

format:  ## Автоматически исправить форматирование и замечания линтера
	$(VENV)/ruff check --fix .
	$(VENV)/ruff format .

check: lint test eval  ## Запустить всё, что выполняет CI

clean:  ## Удалить кэши и артефакты сборки
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov coverage.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
