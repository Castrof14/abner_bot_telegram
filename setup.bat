@echo off
REM Assistente de configuracao inicial (Windows).
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" ( py -3 -m venv .venv 2>nul || python -m venv .venv )
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || exit /b 1
type nul > ".venv\.req-ok"
".venv\Scripts\python.exe" -m teleagent setup
".venv\Scripts\python.exe" -m teleagent check
