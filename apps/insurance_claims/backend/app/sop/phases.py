"""The SOP as data: phases, how much freedom the LLM has in each, and legal transitions."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Phase(str, Enum):
    VERIFY_ID = "VERIFY_ID"
    RESOLVE_INTENT = "RESOLVE_INTENT"
    PROCESS_CASE = "PROCESS_CASE"
    POST_PROCESS = "POST_PROCESS"
    HANDOFF = "HANDOFF"
    CLOSED = "CLOSED"


class Autonomy(str, Enum):
    STRICT = "STRICT"                 # code decides every step; LLM extracts + phrases only
    GUIDED = "GUIDED"                 # LLM interprets into a bounded choice set; code validates
    GROUNDED_FREE = "GROUNDED_FREE"   # LLM reasons freely over read-only tools; facts must be grounded
    TERMINAL = "TERMINAL"


@dataclass(frozen=True)
class PhaseSpec:
    phase: Phase
    autonomy: Autonomy
    goal: str
    claim_data_visible: bool          # can the responder see ANY claims-system data?
    allowed_actions: tuple[str, ...]


SPECS: dict[Phase, PhaseSpec] = {
    Phase.VERIFY_ID: PhaseSpec(
        Phase.VERIFY_ID, Autonomy.STRICT,
        "Verify the caller with at least 3 matching identity factors before anything else.",
        claim_data_visible=False,
        allowed_actions=("ask_identity", "explain_why", "offer_alternative_fields", "request_consent",
                         "decline_off_topic", "offer_handoff", "handoff")),
    Phase.RESOLVE_INTENT: PhaseSpec(
        Phase.RESOLVE_INTENT, Autonomy.GUIDED,
        "Work out which of the caller's claims this is about and what they need.",
        claim_data_visible=True,
        allowed_actions=("confirm_case", "disambiguate_case", "list_cases", "ask_intent", "offer_handoff", "handoff")),
    Phase.PROCESS_CASE: PhaseSpec(
        Phase.PROCESS_CASE, Autonomy.GROUNDED_FREE,
        "Answer the caller's questions about the selected claim using only claims-system data and guidelines.",
        claim_data_visible=True,
        allowed_actions=("answer_grounded", "document_guidance", "followup_guidance", "explain_amounts",
                         "switch_case", "offer_handoff", "handoff", "wrap_up")),
    Phase.POST_PROCESS: PhaseSpec(
        Phase.POST_PROCESS, Autonomy.STRICT,
        "Offer an email summary (send or skip, caller's choice), then close or take another request.",
        claim_data_visible=True,
        allowed_actions=("offer_email", "send_email", "skip_email", "anything_else", "close", "handoff")),
    Phase.HANDOFF: PhaseSpec(Phase.HANDOFF, Autonomy.TERMINAL, "Transferred to a human.", False, ()),
    Phase.CLOSED: PhaseSpec(Phase.CLOSED, Autonomy.TERMINAL, "Conversation closed.", False, ()),
}

TRANSITIONS: dict[Phase, set[Phase]] = {
    Phase.VERIFY_ID: {Phase.RESOLVE_INTENT, Phase.HANDOFF},
    Phase.RESOLVE_INTENT: {Phase.PROCESS_CASE, Phase.POST_PROCESS, Phase.HANDOFF},
    Phase.PROCESS_CASE: {Phase.RESOLVE_INTENT, Phase.POST_PROCESS, Phase.HANDOFF},
    Phase.POST_PROCESS: {Phase.RESOLVE_INTENT, Phase.CLOSED, Phase.HANDOFF},
    Phase.HANDOFF: set(),
    Phase.CLOSED: set(),
}

WORKFLOW = [Phase.VERIFY_ID, Phase.RESOLVE_INTENT, Phase.PROCESS_CASE, Phase.POST_PROCESS]
