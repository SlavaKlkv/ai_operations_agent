#!/usr/bin/env sh
# Обслуживание готовой поставки из GitHub Release.
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
compose_file="$script_dir/compose.yaml"
service=app
data_dir=/srv/app/data
database="$data_dir/ai_operations_agent.db"

usage() {
    cat <<'EOF'
Использование: ./manage.sh <команда> [аргумент]

Команды:
  start                 Загрузить образ и запустить приложение.
  stop                  Остановить приложение, сохранив данные.
  update                Загрузить новую версию образа и перезапустить приложение.
  status                Показать состояние контейнера.
  diagnose              Показать состояние, health и последние логи.
  backup <файл.db>      Создать согласованную SQLite-копию.
  restore <файл.db>     Проверить и восстановить SQLite-копию.
  destroy --delete-data Остановить приложение и удалить volume с данными.
EOF
}

require_docker() {
    command -v docker >/dev/null 2>&1 || {
        echo "Docker не найден. Установите Docker и повторите команду." >&2
        exit 1
    }
    docker info >/dev/null 2>&1 || {
        echo "Docker недоступен. Запустите Docker Desktop или Docker Engine." >&2
        exit 1
    }
    docker compose version >/dev/null 2>&1 || {
        echo "Нужен Docker Compose v2 (команда docker compose)." >&2
        exit 1
    }
    [ -f "$compose_file" ] || {
        echo "Рядом с manage.sh не найден compose.yaml." >&2
        exit 1
    }
}

compose() {
    docker compose -f "$compose_file" "$@"
}

backup() {
    destination=$1
    destination_dir=$(dirname -- "$destination")
    mkdir -p "$destination_dir"
    compose exec -T "$service" python -c \
        "from pathlib import Path; from app.db.backup import create_backup; create_backup(Path('$database'), Path('$data_dir/export.db'))"
    compose cp "$service:$data_dir/export.db" "$destination"
    compose exec -T "$service" rm -f "$data_dir/export.db"
    echo "Резервная копия создана: $destination"
}

restore() {
    source=$1
    [ -f "$source" ] || {
        echo "Файл резервной копии не найден: $source" >&2
        exit 1
    }
    compose cp "$source" "$service:$data_dir/import.db"
    compose stop "$service"
    compose run --rm --no-deps "$service" python -c \
        "from pathlib import Path; from app.db.backup import restore_backup; restore_backup(Path('$data_dir/import.db'), Path('$database'))"
    compose up -d --wait
    echo "Резервная копия восстановлена. Предыдущая база сохранена в volume как ai_operations_agent.db.before-restore."
}

require_docker
command=${1:-help}
case "$command" in
    start) compose pull; compose up -d --wait ;;
    stop) compose stop ;;
    update) compose pull; compose up -d --wait ;;
    status) compose ps ;;
    diagnose) compose ps; curl -fsS http://127.0.0.1:8000/health || true; compose logs --tail 100 "$service" ;;
    backup) [ $# -eq 2 ] || { usage; exit 2; }; backup "$2" ;;
    restore) [ $# -eq 2 ] || { usage; exit 2; }; restore "$2" ;;
    destroy)
        [ "${2:-}" = "--delete-data" ] || {
            echo "Удаление volume требует явного аргумента --delete-data." >&2
            exit 2
        }
        compose down -v
        ;;
    help|--help|-h) usage ;;
    *) usage; exit 2 ;;
esac
