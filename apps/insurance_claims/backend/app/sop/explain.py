"""Plain-language "Why this answer" for every reply, built from the turn's trace.

It is deterministic (no model call), so a reviewer can trust it as a record of what the workflow decided,
what data the reply was allowed to use, and which safety checks it passed.
"""
from __future__ import annotations

from typing import Any

from app.data.repository import MONTHS

from .state import SessionState

SHORT_FACTOR = {"name": "name", "dob": "date of birth", "phone": "phone", "email": "email", "id_last4": "SSN/ID last 4"}

STEP = {"VERIFY_ID": "Verify identity", "RESOLVE_INTENT": "Find the claim", "PROCESS_CASE": "Help with the claim",
        "POST_PROCESS": "Wrap up", "HANDOFF": "Handed to a person", "CLOSED": "Closed"}
MODE = {"VERIFY_ID": "strict: the workflow decides, the model only phrases",
        "RESOLVE_INTENT": "guided: the model interprets, code picks from the caller's own claims",
        "PROCESS_CASE": "grounded: the model reasons freely but may only state facts from the sources",
        "POST_PROCESS": "strict: nothing is sent without an explicit yes"}
TOPICS = {"missing_required_material_alternatives": "what to do if a document can't be obtained",
          "submission_timing": "when to submit", "processing_time_after_submission": "review time after submitting",
          "submission_method": "how to submit", "file_format_requirements": "file format",
          "receipt_confirmation": "confirming receipt"}


INTENT_TEXT = {"denial_question": "wants to know why the claim was denied", "status_inquiry": "wants the claim status",
               "document_submission": "asking about sending documents", "next_steps": "wants the next steps",
               "payment_question": "asking about payment amounts", "appeal": "wants to appeal",
               "general_claim_question": "has a question about a claim"}


def _hint_text(h: dict[str, Any]) -> str:
    parts = [h.get("case_id"), h.get("status"), h.get("case_type"),
             f"from {MONTHS[int(h['month']) - 1]}" if h.get("month") else None,
             str(h["year"]) if h.get("year") else None]
    return " · ".join(p for p in parts if p)


def explain(trace: dict[str, Any], state: SessionState, llm_on: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def add(kind: str, text: str, ok: bool | None = None):
        out.append({"kind": kind, "text": text, "ok": ok})

    v = state.verification
    before, after = trace.get("phase_before"), trace.get("phase_after")
    path = [before] + [t["to"] for t in trace.get("transitions", [])]
    if len(path) > 1:
        add("step", "Moved " + " → ".join(STEP.get(p, p) for p in path))
    elif after in MODE:
        add("step", f"{STEP[after]} ({MODE[after]})")

    if not llm_on:
        add("mode", "Rules-only mode: no model was used. Understanding came from patterns and the reply from a template.")

    # --- identity & access
    ver = trace.get("verification")
    if ver:
        if ver["status"] == "verified":
            names = ", ".join(SHORT_FACTOR[f] for f in v.matched_factors)
            add("gate", f"Identity verified: {names} matched one customer record.", True)
        elif ver["status"] == "failed":
            add("gate", f"Verification attempt failed ({v.failures} of 3). The caller isn't told which detail was wrong.", False)
        else:
            add("gate", f"Identity: {len(ver['collected'])} of 3 required details given so far.")
    rep = trace.get("representative")
    if rep:
        add("gate", f"Caller is a representative ({rep['rep_name']}): "
                    + ("listed on the account." if rep["on_file"] else "NOT listed on the account, so nothing is shared."),
            rep["on_file"])
    if trace.get("consent"):
        st = trace["consent"]["status"]
        add("gate", {"pending": "Waiting for the policyholder to approve access. Claim details stay locked.",
                     "approved": "The policyholder approved access.",
                     "timeout": "The policyholder's approval expired. Claim details stay locked."}[st],
            {"pending": None, "approved": True, "timeout": False}[st])
    cleared = v.verified and v.consent_status in ("not_required", "approved")
    if not cleared and after not in ("HANDOFF",):
        add("gate", "Claim data locked: this reply was written without access to any claim record.", True)

    # --- memory
    res = trace.get("case_resolution")
    noted = [k for k in trace.get("memory_updates", []) if k.split(".")[0] in ("case", "intent")]
    if noted and not cleared:
        hint = _hint_text(state.memory.family("case", include_consumed=True))
        need = INTENT_TEXT.get(state.memory.get("intent.primary") or "")
        what = "; ".join(x for x in (f"the claim ({hint})" if hint else "", need) if x) or "the caller's request"
        add("memory", f"Remembered for later: {what}. It will be used right after verification.")
    if res:
        hints = {k: v_ for k, v_ in res["hints"].items() if k != "status (implied)"}
        earlier = res.get("hints_from_earlier_turns") or {}
        if res["kind"] == "unique":
            src = (f"what the caller said in message {min(earlier.values())}" if earlier else "the caller's description")
            add("memory", f"Picked claim {state.active_case_id} using {src}"
                          + (f" ({_hint_text(hints)})" if hints else "")
                          + (" plus the denial question" if "status (implied)" in res["hints"] else "") + ".", True)
        elif res["kind"] == "ambiguous":
            add("memory", f"{len(res['candidates'])} claims match \"{_hint_text(hints)}\", so it asked which one "
                          "instead of guessing.")
        elif res["kind"] == "none":
            add("memory", f"No claim matches \"{_hint_text(hints)}\" exactly, so it listed the closest ones.")
        elif res["kind"] == "no_cases":
            add("memory", "This customer has no claims on file.")

    # --- sources
    src = trace.get("sources")
    if src:
        parts = [f"claim file {src['claim']}"]
        if src.get("topics"):
            parts.append("guideline on " + ", ".join(TOPICS.get(t, t) for t in src["topics"]))
        if src.get("documents"):
            parts.append("document requirements for " + ", ".join(src["documents"]))
        if src.get("fallback"):
            parts.append("the general follow-up guideline (no specific rule matched)")
        add("source", "Facts came only from: " + "; ".join(parts) + ".")

    # --- scope, manipulation, emotion
    for g in trace.get("guards", []):
        if g["guard"] == "scope":
            n = g["off_topic_streak"]
            add("scope", f"Out of scope ({n} in a row): declined without answering"
                         + (", and offered a person." if state.pending_offer == "handoff" and n >= 3 else "."))
        elif g["guard"] == "manipulation":
            add("scope", "Attempt to override the rules was ignored; the gates live in code, not in the prompt.", True)
    emo = trace.get("emotion") or {}
    if emo.get("mode") == "empathize":
        bits = ["acknowledged it first"]
        if emo.get("rationale_key"):
            bits.append("explained why " + ("identity checks" if emo["rationale_key"] == "identity" else
                                            "the policyholder's approval") + " matter")
        bits.append("kept the next step clear")
        add("tone", f"Caller sounded {emo['label']} ({emo['intensity']}/3): " + ", ".join(bits) + ".")
    if emo.get("offer_human") and state.pending_offer == "handoff":
        add("tone", "Offered a person because of " + " and ".join(emo["escalation_reasons"]) + ".")

    # --- outcomes
    if trace.get("email"):
        add("outcome", "Email summary sent only to the address on file, after an explicit yes.", True)
    acts = [a["kind"] for a in (trace.get("directive") or {}).get("acts", [])]
    if "email_skipped" in acts:
        add("outcome", "Email skipped because the caller said no.", True)
    if "alt_email_declined" in acts:
        add("outcome", "Refused to send to a different address; summaries only go to the email on file.", True)
    if after == "HANDOFF" and state.handoff and before != "HANDOFF":
        add("outcome", f"Handed to a person ({state.handoff['reason']}). Notes passed along, ref {state.handoff['ticket_id']}.")

    # --- safety checks
    for g in trace.get("guards", []):
        if "violations" not in g:
            continue
        label = "Privacy check" if g["guard"] == "disclosure" else "Fact check"
        if any("promises an action" in x for x in g["violations"]):
            label = "Commitment check"
        what = ("no claim details or on-file personal data" if g["guard"] == "disclosure"
                else "every claim ID, amount and date appears in the sources")
        if not g["violations"]:
            add("check", f"{label} passed: {what}.", True)
        else:
            add("check", f"{label} blocked a draft ({'; '.join(g['violations'])}) and it was rewritten.", False)
    err = trace.get("model_error") or (trace.get("extraction") or {}).get("_error")
    if err and llm_on:
        reason = ("it couldn't be reached (network or endpoint problem)" if "connect" in err.lower() or "timeout" in err.lower()
                  else "the key or model was rejected" if any(w in err.lower() for w in ("401", "403", "auth", "credential", "denied"))
                  else "it returned an error")
        if trace.get("model_error"):
            text = (f"The model wasn't used for this reply because {reason}. The workflow still ran and a safe "
                    "pre-written reply was sent.")
        else:
            text = (f"The model couldn't read this message because {reason}, so simple patterns were used; "
                    "the reply itself was still written by the model.")
        add("model", text, False)
        out[-1]["model_error"] = True
    elif any("fell back" in n for n in trace.get("notes", [])):
        add("check", "The model's drafts didn't pass the checks, so a safe pre-written reply was sent instead.", None)
    return out
