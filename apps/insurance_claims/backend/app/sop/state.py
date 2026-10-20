"""Session state. Everything the SOP knows lives here; the LLM is stateless."""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from .phases import SPECS, TRANSITIONS, Phase

IDENTITY_FACTORS = ("name", "dob", "phone", "email", "id_last4")
FACTOR_LABELS = {"name": "full name", "dob": "date of birth", "phone": "phone number on file",
                 "email": "email on file", "id_last4": "last four digits of your SSN or national ID"}


class TransitionError(RuntimeError):
    pass


@dataclass
class Slot:
    value: Any
    turn: int
    phase: str               # phase in which it was heard
    source: str              # "llm" | "regex" | "system"
    status: str = "active"   # active | deferred | consumed | rejected
    consumed_turn: int | None = None
    history: list[dict[str, Any]] = field(default_factory=list)


class Memory:
    """Slot store with provenance. Anything the caller says is remembered, whatever the phase."""

    # which phase consumes which slot family; a slot heard before its consumer phase is 'deferred'
    CONSUMER = {"identity": Phase.VERIFY_ID, "caller": Phase.VERIFY_ID, "intent": Phase.RESOLVE_INTENT,
                "case": Phase.RESOLVE_INTENT, "request": Phase.RESOLVE_INTENT, "contact": Phase.POST_PROCESS}
    ORDER = [Phase.VERIFY_ID, Phase.RESOLVE_INTENT, Phase.PROCESS_CASE, Phase.POST_PROCESS]

    def __init__(self):
        self.slots: dict[str, Slot] = {}

    def put(self, key: str, value: Any, turn: int, phase: Phase, source: str) -> bool:
        if value in (None, "", [], {}):
            return False
        consumer = self.CONSUMER.get(key.split(".")[0])
        deferred = consumer is not None and phase in self.ORDER and consumer in self.ORDER and \
            self.ORDER.index(phase) < self.ORDER.index(consumer)
        status = "deferred" if deferred else "active"
        old = self.slots.get(key)
        if old and old.value == value and old.status in ("active", "deferred", "consumed"):
            return False  # hearing the same thing again doesn't make an already-used hint new
        slot = Slot(value, turn, phase.value, source, status)
        if old:
            slot.history = old.history + [{"value": old.value, "turn": old.turn, "status": old.status}]
        self.slots[key] = slot
        return True

    def get(self, key: str, include_consumed: bool = True) -> Any:
        s = self.slots.get(key)
        if not s or s.status == "rejected" or (s.status == "consumed" and not include_consumed):
            return None
        return s.value

    def family(self, prefix: str, include_consumed: bool = False) -> dict[str, Any]:
        return {k.split(".", 1)[1]: s.value for k, s in self.slots.items()
                if k.startswith(prefix + ".") and s.status in (("active", "deferred", "consumed")
                                                              if include_consumed else ("active", "deferred"))}

    def consume(self, prefix: str, turn: int) -> list[str]:
        done = []
        for k, s in self.slots.items():
            if k.startswith(prefix + ".") and s.status in ("active", "deferred"):
                s.status, s.consumed_turn = "consumed", turn
                done.append(k)
        return done

    def reject(self, key: str, turn: int) -> None:
        if s := self.slots.get(key):
            s.status, s.consumed_turn = "rejected", turn

    def clear(self, prefix: str) -> None:
        for k in [k for k in self.slots if k.startswith(prefix + ".")]:
            del self.slots[k]

    def to_dict(self) -> dict[str, Any]:
        return {k: asdict(v) for k, v in self.slots.items()}


@dataclass
class Verification:
    verified: bool = False
    party_id: str | None = None
    verified_turn: int | None = None
    matched_factors: list[str] = field(default_factory=list)
    failures: int = 0
    locked: bool = False
    caller_role: str = "self"            # self | representative
    rep_on_file: bool | None = None
    consent_status: str = "not_required"  # not_required | pending | approved | timeout
    consent_polls: int = 0


@dataclass
class Counters:
    off_topic_streak: int = 0
    high_emotion_streak: int = 0
    gate_pushback: int = 0               # refusals / bypass demands at a gate
    empathy_turns: int = 0
    injection_attempts: int = 0


@dataclass
class SessionState:
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    consent_scenario: str = "default"
    phase: Phase = Phase.VERIFY_ID
    turn: int = 0
    created: float = field(default_factory=time.time)
    transcript: list[dict[str, Any]] = field(default_factory=list)
    memory: Memory = field(default_factory=Memory)
    verification: Verification = field(default_factory=Verification)
    counters: Counters = field(default_factory=Counters)
    active_case_id: str | None = None
    active_case_needs_documents: bool = False
    case_ledger: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_offer: str | None = None     # handoff | email | case_choice | anything_else
    pending_offer_turn: int = 0
    post_stage: str | None = None        # offered | sent | skipped
    email_status: str | None = None
    handoff: dict[str, Any] | None = None
    handoff_reason: str | None = None
    last_candidates: list[str] = field(default_factory=list)
    process_intent: str | None = None
    hint_acknowledged: bool = False
    traces: list[dict[str, Any]] = field(default_factory=list)
    outbox: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def transition(self, to: Phase, reason: str, log: list[dict[str, Any]]) -> None:
        if to not in TRANSITIONS[self.phase]:
            raise TransitionError(f"illegal transition {self.phase.value} -> {to.value}")
        if to == Phase.RESOLVE_INTENT and not self.verification.verified:
            raise TransitionError("gate: identity not verified")
        if to == Phase.RESOLVE_INTENT and self.verification.consent_status not in ("not_required", "approved"):
            raise TransitionError("gate: policyholder consent not approved")
        if to == Phase.PROCESS_CASE and not self.active_case_id:
            raise TransitionError("gate: no case selected")
        log.append({"from": self.phase.value, "to": to.value, "reason": reason, "turn": self.turn})
        self.phase = to

    def offer(self, what: str | None) -> None:
        self.pending_offer, self.pending_offer_turn = what, self.turn

    def public(self) -> dict[str, Any]:
        """State snapshot for the UI inspector."""
        spec = SPECS[self.phase]
        v = self.verification
        identity = self.memory.family("identity", include_consumed=True)
        return {
            "session_id": self.session_id,
            "phase": self.phase.value,
            "autonomy": spec.autonomy.value,
            "phase_goal": spec.goal,
            "turn": self.turn,
            "verification": {
                "verified": v.verified, "failures": v.failures, "locked": v.locked,
                "matched_factors": v.matched_factors, "caller_role": v.caller_role,
                "rep_on_file": v.rep_on_file, "consent_status": v.consent_status,
                "collected": [f for f in IDENTITY_FACTORS if f in identity],
            },
            "memory": self.memory.to_dict(),
            "active_case_id": self.active_case_id,
            "active_case_needs_documents": self.active_case_needs_documents,
            "case_ledger": self.case_ledger,
            "pending_offer": self.pending_offer,
            "counters": asdict(self.counters),
            "email_status": self.email_status,
            "handoff": self.handoff,
            "outbox_count": len(self.outbox),
            "consent_scenario": self.consent_scenario,
        }
