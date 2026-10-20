@echo off
rem One-command local run without Docker (Windows). Needs Python 3.11+.
cd /d "%~dp0"
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
pip install -q -r apps\insurance_claims\backend\requirements.txt
if not exist .env copy .env.example .env >nul
echo Open http://localhost:8000
cd apps\insurance_claims\backend
uvicorn app.main:app --port 8000
