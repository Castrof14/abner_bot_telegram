@echo off
REM Inicia o TeleAgent (Windows). Uso: start.bat [--takeover]
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Criando ambiente virtual .venv...
  py -3 -m venv .venv 2>nul || python -m venv .venv
  if errorlevel 1 ( echo Python 3.10+ nao encontrado. Instale em https://www.python.org & exit /b 1 )
)
if not exist ".venv\.req-ok" (
  ".venv\Scripts\python.exe" -m pip install --upgrade --quiet pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || exit /b 1
  type nul > ".venv\.req-ok"
)
if not exist ".env" (
  echo Arquivo .env nao encontrado - iniciando o assistente de configuracao.
  ".venv\Scripts\python.exe" -m teleagent setup || exit /b 1
)
".venv\Scripts\python.exe" -m teleagent run %*
