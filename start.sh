#!/usr/bin/env sh
# Запуск готового образа из распакованного GitHub Release.
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
compose_file="$script_dir/compose.yaml"

if ! command -v docker >/dev/null 2>&1; then
    echo "Docker не найден. Установите и запустите Docker, затем повторите команду." >&2
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "Docker недоступен. Запустите Docker Desktop или Docker Engine." >&2
    exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
    echo "Нужен Docker Compose v2 (команда docker compose)." >&2
    exit 1
fi

if [ ! -f "$compose_file" ]; then
    echo "Рядом со start.sh не найден compose.yaml. Распакуйте полный комплект Release." >&2
    exit 1
fi

docker compose -f "$compose_file" pull
docker compose -f "$compose_file" up -d --wait

echo "Приложение запущено: http://localhost:8000/"
echo "Если Ollama недоступен, запустите его на компьютере и обновите страницу."
