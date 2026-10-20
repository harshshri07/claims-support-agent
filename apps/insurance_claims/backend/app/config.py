"""Runtime settings, read once from the environment (.env at the repo root or in apps/insurance_claims/backend/)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

BACKEND = Path(__file__).resolve().parent.parent      # apps/insurance_claims/backend
APP_DIR = BACKEND.parent                               # apps/insurance_claims (starter layout)
REPO = APP_DIR.parent.parent                           # repository root
load_dotenv(REPO / ".env")
load_dotenv(BACKEND / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    demo_today: date = date.fromisoformat(os.getenv("DEMO_TODAY", "2026-03-05"))
    max_verify_failures: int = _int("MAX_VERIFY_FAILURES", 3)
    off_topic_limit: int = _int("OFF_TOPIC_LIMIT", 3)
    # lets testers pick a model + paste a key in the UI (kept in server memory only)
    allow_ui_llm_config: bool = os.getenv("ALLOW_UI_LLM_CONFIG", "true").lower() in ("1", "true", "yes")

    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = _int("SMTP_PORT", 587)
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_from: str = os.getenv("SMTP_FROM", "claims-support@example.com")
    smtp_redirect_to: str = os.getenv("SMTP_REDIRECT_TO", "")

    # the starter's fixtures, used in place and unmodified
    fixtures_dir: Path = Path(os.getenv("FIXTURES_DIR", str(APP_DIR / "fixtures")))
    frontend_dir: Path = Path(os.getenv("FRONTEND_DIR", str(APP_DIR / "frontend")))


settings = Settings()
