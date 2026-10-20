"""Check the model configured in .env: one completion + one forced tool call.

Usage (from apps/insurance_claims/backend/):  python scripts/check_llm.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.llm import config_from_env, test_config  # noqa: E402

cfg = config_from_env()
if not cfg:
    sys.exit("No model configured. Set LLM_PROVIDER and its key in .env (see .env.example), "
             "or connect one from the UI's Model dialog.")
print(f"provider: {cfg.provider}  model: {cfg.model or '(default)'}")
result = test_config(cfg)
print(result)
sys.exit(0 if result["ok"] else 1)
