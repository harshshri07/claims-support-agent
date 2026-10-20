"""Emotional support & SOP recovery policy.

The extractor reads tone; this module decides *how* to respond (empathy mode, which
rationale to give) and *when to stop persuading* and offer a human. It never relaxes a gate.
"""
from __future__ import annotations

from typing import Any

from .phases import Phase
from .state import SessionState

# Why each gate exists, phrased for callers. The responder must paraphrase, not recite.
RATIONALES = {
    "identity": ("Claim files hold medical and financial details, so we confirm identity before opening one. "
                 "It's what stops anyone who calls pretending to be you from hearing your information."),
    "consent": ("Because the claim belongs to the policyholder, they need to approve sharing it with someone else. "
                "That approval protects their privacy, even from family."),
    "email_on_file": ("Summaries only go to the email already on file, so claim details can't be redirected "
                      "to an address we haven't verified."),
    "grounded": ("I can only share what's actually in the claim file and our guidelines. I won't guess about "
                 "something that affects your money."),
}

STYLE = {
    "frustrated": "Acknowledge the frustration directly and specifically, without defensiveness. Be brief and concrete.",
    "angry": "Stay calm and respectful. Acknowledge the anger without arguing, apologise once at most, "
             "and make the path forward short and clear.",
    "anxious": "Be reassuring and steady. Explain what happens next in simple steps so they feel in control.",
    "confused": "Slow down. Use plain words, explain one thing at a time, and check understanding.",
    "sad": "Be warm and human. Acknowledge that this is a hard situation before moving on.",
}

HIGH_STREAK_LIMIT = 3
PUSHBACK_LIMIT = 3


def assess(state: SessionState, ext: dict[str, Any]) -> dict[str, Any]:
    """Update emotional counters and return the empathy directive for this turn."""
    emo = ext.get("emotion") or {}
    label, intensity = emo.get("label", "calm"), int(emo.get("intensity") or 0)
    acts = set(ext.get("acts") or [])
    c = state.counters

    if label in ("frustrated", "angry") and intensity >= 2:
        c.high_emotion_streak += 1
    else:
        c.high_emotion_streak = 0
    at_gate = state.phase == Phase.VERIFY_ID or state.verification.consent_status == "pending"
    if at_gate and acts & {"demand_bypass", "refusal"}:
        c.gate_pushback += 1

    out: dict[str, Any] = {"label": label, "intensity": intensity, "cue": emo.get("cue")}
    if label != "calm" and intensity >= 1:
        c.empathy_turns += 1
        out["mode"] = "empathize"
        out["style"] = STYLE.get(label, STYLE["frustrated"])
    # explain *why* only when the caller is pushing against the gate, not on every emotional or off-topic message
    if at_gate and ext.get("scope") != "out_of_scope" and acts & {"demand_bypass", "refusal", "complaint"}:
        out["rationale_key"] = "consent" if state.verification.consent_status == "pending" else "identity"
        out["rationale"] = RATIONALES[out["rationale_key"]]

    reasons = []
    if c.high_emotion_streak >= HIGH_STREAK_LIMIT:
        reasons.append("sustained high frustration")
    if label == "angry" and intensity >= 3:
        reasons.append("very high anger")
    if c.gate_pushback >= PUSHBACK_LIMIT:
        reasons.append("repeated refusal at a required step")
    out["offer_human"] = bool(reasons)
    out["escalation_reasons"] = reasons
    return out
