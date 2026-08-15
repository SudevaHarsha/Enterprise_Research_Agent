#!/usr/bin/env pwsh
# ECRKE developer convenience wrapper.
# Usage:  .\scripts\dev.ps1 <command>
#   setup          create venv + install editable + dev deps
#   lint           ruff check
#   format         ruff format
#   type           mypy app
#   security       bandit (medium+ severity)
#   test           pytest
#   verify         lint + format + type + security + test (local quality gate)
#   compose-up     docker compose up -d
#   compose-down   docker compose down
#   db-init        start only the Postgres service
#   migrate        alembic upgrade head
#   seed           idempotent tenant seed (python -m app.db.seed)
#   db-psql        open psql inside the ecrke Postgres container

param([Parameter(Mandatory = $true)][string]$Command)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

switch ($Command) {
    "setup" {
        if (-not (Test-Path ".venv")) { python -m venv .venv }
        & .\.venv\Scripts\python.exe -m pip install --upgrade pip
        & .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
        Write-Host "Setup complete. Activate with: .\.venv\Scripts\Activate.ps1"
    }
    "lint" { & .\.venv\Scripts\python.exe -m ruff check . }
    "format" { & .\.venv\Scripts\python.exe -m ruff format . }
    "type" { & .\.venv\Scripts\python.exe -m mypy app }
    "security" { & .\.venv\Scripts\python.exe -m bandit -q -r app -ll }
    "test" { & .\.venv\Scripts\python.exe -m pytest }
    "verify" {
        & .\.venv\Scripts\python.exe -m ruff check .
        & .\.venv\Scripts\python.exe -m ruff format --check .
        & .\.venv\Scripts\python.exe -m mypy app
        & .\.venv\Scripts\python.exe -m bandit -q -r app -ll
        & .\.venv\Scripts\python.exe -m pytest
    }
    "compose-up" { docker compose up -d }
    "compose-down" { docker compose down }
    "db-init" { docker compose up -d postgres }
    "migrate" { & .\.venv\Scripts\python.exe -m alembic upgrade head }
    "seed" { & .\.venv\Scripts\python.exe -m app.db.seed }
    "db-psql" {
        if (-not (docker ps --format "{{.Names}}" | Select-String -Quiet "ecrke-postgres-1")) {
            Write-Host "Postgres is not running. Start it with: .\scripts\dev.ps1 db-init"
            exit 1
        }
        docker exec -it ecrke-postgres-1 psql -U ecrke -d ecrke
    }
    default {
        Write-Host "Unknown command: $Command"
        Write-Host "Commands: setup, lint, format, type, security, test, verify, compose-up, compose-down, db-init, migrate, seed, db-psql"
    }
}
