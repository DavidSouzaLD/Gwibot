@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

title Gwibot - Mercado Livre Local Scraper

echo ============================================================
echo   Gwibot - Scraper Local do Mercado Livre (Windows)
echo ============================================================
echo.

cd /d "%~dp0"

:: Verifica se o Python esta instalado
where python >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [ERRO] Python 3.12+ nao foi encontrado no PATH do sistema.
    echo Instale o Python em https://www.python.org/downloads/ e tente novamente.
    pause
    exit /b 1
)

:: Cria ou recria o ambiente virtual (.venv) se nao existir ou vier de outro PC
.\.venv\Scripts\python.exe --version >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [1/4] Criando ambiente virtual Python ^(.venv^)...
    python -m venv --clear .venv
    if !ERRORLEVEL! neq 0 (
        echo [ERRO] Falha ao criar o ambiente virtual.
        pause
        exit /b 1
    )
) else (
    echo [1/4] Ambiente virtual ^(.venv^) encontrado.
)

:: Instala ou atualiza as dependencias
echo [2/4] Verificando dependencias Python ^(requirements.txt^)...
.\.venv\Scripts\python.exe -m pip install --prefer-binary -r requirements.txt --quiet
if %ERRORLEVEL% neq 0 (
    echo [ERRO] Falha ao instalar dependencias do requirements.txt.
    pause
    exit /b 1
)

:: Configura pasta local para o navegador Chromium se existir ou instala
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0.playwright-browsers"
if not exist ".playwright-browsers" (
    echo [3/4] Instalando navegador Chromium isolado do Playwright...
    .\.venv\Scripts\python.exe -m playwright install chromium
    if !ERRORLEVEL! neq 0 (
        echo [ERRO] Falha ao instalar o navegador Chromium do Playwright.
        pause
        exit /b 1
    )
) else (
    echo [3/4] Navegador Chromium isolado pronto em .playwright-browsers.
)

echo [4/4] Iniciando servidor FastAPI acessivel na rede local ^(0.0.0.0:8000^)...
echo       Local: http://127.0.0.1:8000
for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -ne '127.0.0.1' -and $_.IPAddress -notlike '169.254.*' -and $_.PrefixOrigin -ne 'WellKnown' } | Select-Object -ExpandProperty IPAddress -Unique"`) do (
    echo       Rede:  http://%%I:8000
)
echo.
echo Pressione CTRL+C para encerrar o servidor.
echo.

:: Abre o navegador padrao apos 2 segundos em background
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:8000"

.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
pause
