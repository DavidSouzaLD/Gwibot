# Inicializador PowerShell para o Gwibot (Windows)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Gwibot - Scraper Local do Mercado Livre (PowerShell)" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

$venvValid = $false
if (Test-Path ".venv\Scripts\python.exe") {
    try {
        & ".\.venv\Scripts\python.exe" --version *> $null
        if ($LASTEXITCODE -eq 0) { $venvValid = $true }
    } catch {
        $venvValid = $false
    }
}

if (-not $venvValid) {
    Write-Host "[1/4] Criando ambiente virtual (.venv)..." -ForegroundColor Yellow
    python -m venv --clear .venv
} else {
    Write-Host "[1/4] Ambiente virtual (.venv) encontrado." -ForegroundColor Green
}

Write-Host "[2/4] Verificando pacotes de requirements.txt..." -ForegroundColor Yellow
.\.venv\Scripts\python.exe -m pip install --prefer-binary -r requirements.txt --quiet

$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PSScriptRoot ".playwright-browsers"
if (-not (Test-Path $env:PLAYWRIGHT_BROWSERS_PATH)) {
    Write-Host "[3/4] Instalando Chromium para o Playwright..." -ForegroundColor Yellow
    .\.venv\Scripts\python.exe -m playwright install chromium
} else {
    Write-Host "[3/4] Navegador Chromium pronto em .playwright-browsers." -ForegroundColor Green
}

$lanIps = @()
try {
    $lanIps = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -ne "127.0.0.1" -and $_.IPAddress -notlike "169.254.*" -and $_.PrefixOrigin -ne "WellKnown" } |
        Select-Object -ExpandProperty IPAddress -Unique
} catch {
    $lanIps = @()
}

Write-Host "[4/4] Iniciando servidor acessível na rede local (0.0.0.0:8000)..." -ForegroundColor Green
Write-Host "      Local: http://127.0.0.1:8000" -ForegroundColor Cyan
foreach ($ip in $lanIps) {
    Write-Host "      Rede:  http://${ip}:8000" -ForegroundColor Cyan
}
Start-Job -ScriptBlock { Start-Sleep -Seconds 2; Start-Process "http://127.0.0.1:8000" } | Out-Null
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
