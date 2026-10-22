#!/usr/bin/env bash
# Assistente de configuração inicial (Linux/macOS).
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --quiet -r requirements.txt && touch .venv/.req-ok
.venv/bin/python -m teleagent setup
.venv/bin/python -m teleagent check || true
