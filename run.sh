#!/usr/bin/env sh
# One-command local run without Docker (macOS / Linux). Needs Python 3.11+.
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r apps/insurance_claims/backend/requirements.txt
[ -f .env ] || cp .env.example .env
echo "Open http://localhost:8000"
cd apps/insurance_claims/backend && exec uvicorn app.main:app --port "${PORT:-8000}"
