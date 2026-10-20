"""Warm transfer to a human: the context packet means the caller never repeats themselves."""
from __future__ import annotations

import random
import time
from typing import Any

from .state import SessionState


def create_ticket(state: SessionState, reason: str, emotion: dict[str, Any] | None = None) -> dict[str, Any]:
    v = state.verification
    ticket = {
        "ticket_id": f"HX-{random.randint(10000, 99999)}",
        "reason": reason,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "phase_at_transfer": state.phase.value,
        "identity_verified": v.verified,
        "party_id": v.party_id if v.verified else None,
        "caller_role": v.caller_role,
        "consent_status": v.consent_status,
        "active_case_id": state.active_case_id,
        "cases_discussed": list(state.case_ledger),
        "intent": state.memory.get("intent.primary"),
        "caller_emotion": (emotion or {}).get("label"),
        "recent_transcript": state.transcript[-8:],
    }
    state.handoff = ticket
    return ticket
