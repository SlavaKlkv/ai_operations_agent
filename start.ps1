# Запуск готового образа из распакованного GitHub Release.
$ErrorActionPreference = "Stop"
$composeFile = Join-Path $PSScriptRoot "compose.yaml"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker не найден. Установите и запустите Docker Desktop, затем повторите команду."
}

docker info *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker недоступен. Запустите Docker Desktop."
}

docker compose version *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Нужен Docker Compose v2 (команда docker compose)."
}

if (-not (Test-Path -LiteralPath $composeFile -PathType Leaf)) {
    throw "Рядом со start.ps1 не найден compose.yaml. Распакуйте полный комплект Release."
}

docker compose -f $composeFile pull
if ($LASTEXITCODE -ne 0) {
    throw "Не удалось загрузить образ приложения. Проверьте сеть и повторите команду."
}

docker compose -f $composeFile up -d --wait
if ($LASTEXITCODE -ne 0) {
    throw "Контейнер не запустился. Проверьте состояние командой docker compose -f compose.yaml logs app."
}

Write-Host "Приложение запущено: http://localhost:8000/"
Write-Host "Если Ollama недоступен, запустите его на компьютере и обновите страницу."
