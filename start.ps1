# Inicializador PowerShell para o Gwibot (Windows)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Gwibot - Scraper Local do Mercado Livre (PowerShell)" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "[1/4] Criando ambiente virtual (.venv)..." -ForegroundColor Yellow
    python -m venv .venv
} else {
    Write-Host "[1/4] Ambiente virtual (.venv) encontrado." -ForegroundColor Green
}

Write-Host "[2/4] Verificando pacotes de requirements.txt..." -ForegroundColor Yellow
.\.venv\Scripts\python.exe -m pip install -r requirements.txt --quiet

$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PSScriptRoot ".playwright-browsers"
if (-not (Test-Path $env:PLAYWRIGHT_BROWSERS_PATH)) {
    Write-Host "[3/4] Instalando Chromium para o Playwright..." -ForegroundColor Yellow
    .\.venv\Scripts\python.exe -m playwright install chromium
} else {
    Write-Host "[3/4] Navegador Chromium pronto em .playwright-browsers." -ForegroundColor Green
}

Write-Host "[4/4] Iniciando servidor em http://127.0.0.1:8000 ..." -ForegroundColor Green
Start-Job -ScriptBlock { Start-Sleep -Seconds 2; Start-Process "http://127.0.0.1:8000" } | Out-Null
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
