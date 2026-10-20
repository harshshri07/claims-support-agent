FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
WORKDIR /app

COPY apps/insurance_claims/backend/requirements.txt apps/insurance_claims/backend/requirements.txt
RUN pip install --no-cache-dir -r apps/insurance_claims/backend/requirements.txt

# starter layout: apps/insurance_claims/{fixtures,backend,frontend}
COPY apps ./apps

RUN useradd -m appuser
USER appuser
WORKDIR /app/apps/insurance_claims/backend

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://localhost:{os.environ.get(\"PORT\",\"8000\")}/api/health')"
# No credentials are baked in: pass them at runtime (.env / --env-file), or connect a model in the UI.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
