<!-- Тип в заголовке: feat / fix / docs / test / ci / chore. -->

## Что и зачем

<!-- Что меняется и почему. Ссылка на пункт плана, если применимо. -->

## Проверка

- [ ] `ruff check .` и `ruff format --check .`
- [ ] `pytest`
- [ ] `python -m app.evaluation --quiet`
- [ ] если затронут интерфейс или поставка: контейнер пересобран и пересоздан, `./manage.sh status` → `healthy`, `GET /` = 200

## Labels

<!-- area: / quality: по фактической области изменений (см. AGENTS.md); без enhancement. -->
