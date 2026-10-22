#!/usr/bin/env bash
# Inicia o TeleAgent (Linux/macOS). Uso: ./start.sh [--takeover]
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
  echo "Criando ambiente virtual .venv…"
  "$PY" -m venv .venv
fi
# Reinstala dependências só quando requirements.txt muda.
if [ ! -f .venv/.req-ok ] || [ requirements.txt -nt .venv/.req-ok ]; then
  .venv/bin/python -m pip install --upgrade --quiet pip
  .venv/bin/python -m pip install --quiet -r requirements.txt
  touch .venv/.req-ok
fi
if [ ! -f .env ]; then
  echo "Arquivo .env não encontrado — iniciando o assistente de configuração."
  .venv/bin/python -m teleagent setup
fi
exec .venv/bin/python -m teleagent run "$@"
