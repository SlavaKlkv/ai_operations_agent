# Обслуживание готовой поставки из GitHub Release.
[CmdletBinding()]
param(
    [Parameter(Position = 0)] [ValidateSet("start", "stop", "update", "status", "diagnose", "backup", "restore", "destroy", "help")]
    [string] $Command = "help",
    [Parameter(Position = 1)] [string] $Path,
    [switch] $DeleteData
)

$ErrorActionPreference = "Stop"
$composeFile = Join-Path $PSScriptRoot "compose.yaml"
$service = "app"
$dataDir = "/srv/app/data"
$database = "$dataDir/ai_operations_agent.db"

function Require-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker не найден." }
    docker info *> $null
    if ($LASTEXITCODE -ne 0) { throw "Docker недоступен. Запустите Docker Desktop." }
    docker compose version *> $null
    if ($LASTEXITCODE -ne 0) { throw "Нужен Docker Compose v2." }
    if (-not (Test-Path -LiteralPath $composeFile -PathType Leaf)) { throw "Рядом с manage.ps1 не найден compose.yaml." }
}

function Invoke-Compose { param([Parameter(ValueFromRemainingArguments = $true)] $Arguments) & docker compose -f $composeFile @Arguments; if ($LASTEXITCODE -ne 0) { throw "Команда Docker Compose завершилась с ошибкой." } }
function Show-Usage { Write-Host "Команды: start, stop, update, status, diagnose, backup <файл.db>, restore <файл.db>, destroy -DeleteData" }

Require-Docker
switch ($Command) {
    "start" { Invoke-Compose pull; Invoke-Compose up -d --wait }
    "stop" { Invoke-Compose stop }
    "update" { Invoke-Compose pull; Invoke-Compose up -d --wait }
    "status" { Invoke-Compose ps }
    "diagnose" { Invoke-Compose ps; try { Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/health | Select-Object -ExpandProperty Content } catch { Write-Warning $_ }; Invoke-Compose logs --tail 100 $service }
    "backup" {
        if (-not $Path) { throw "Укажите путь к файлу резервной копии." }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
        Invoke-Compose exec -T $service python -c "from pathlib import Path; from app.db.backup import create_backup; create_backup(Path('$database'), Path('$dataDir/export.db'))"
        Invoke-Compose cp "$service`:$dataDir/export.db" $Path
        Invoke-Compose exec -T $service rm -f "$dataDir/export.db"
    }
    "restore" {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Файл резервной копии не найден." }
        Invoke-Compose cp $Path "$service`:$dataDir/import.db"
        Invoke-Compose stop $service
        Invoke-Compose run --rm --no-deps $service python -c "from pathlib import Path; from app.db.backup import restore_backup; restore_backup(Path('$dataDir/import.db'), Path('$database'))"
        Invoke-Compose up -d --wait
    }
    "destroy" { if (-not $DeleteData) { throw "Удаление volume требует -DeleteData." }; Invoke-Compose down -v }
    default { Show-Usage }
}
