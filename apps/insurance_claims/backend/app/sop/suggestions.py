"""Follow-up suggestions: what a caller could plausibly say next, from the real state of the conversation.

Deterministic and grounded: they use the verified caller's own claim (document names, amounts, deadline, status),
the actual candidates when the agent asked "which claim?", and the pending question (send the email? transfer?).
Before the caller is cleared they are generic and contain no claim data. A suggestion that was already used is not
offered again.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.data.repository import MONTHS, Repository

from .phases import Phase
from .state import SessionState

MAX_SUGGESTIONS = 4


def _s(sid: str, text: str, label: str | None = None) -> dict[str, str]:
    return {"id": sid, "text": text, "label": label or text}


def suggest(state: SessionState, repo: Repository, today: date) -> list[dict[str, str]]:
    if state.phase in (Phase.HANDOFF, Phase.CLOSED):
        return []
    v = state.verification
    cleared = v.verified and v.consent_status in ("not_required", "approved")

    if state.pending_offer == "handoff":  # a yes/no question about a transfer is always the most relevant thing
        picks = [_s("handoff.yes", "Yes, please connect me to a person"), _s("handoff.no", "No thanks, let's keep going")]
    elif state.phase == Phase.VERIFY_ID:
        picks = _verify(state)
    elif state.phase == Phase.RESOLVE_INTENT and cleared:
        picks = _resolve(state, repo)
    elif state.phase == Phase.PROCESS_CASE and cleared:
        picks = _process(state, repo, today)
    elif state.phase == Phase.POST_PROCESS:
        picks = _post(state)
    else:
        picks = []

    picks = [p for p in picks if p["id"] not in state.used_suggestions]
    closing = [p for p in picks if p["id"].endswith("wrap.up")]
    rest = [p for p in picks if not p["id"].endswith("wrap.up")]
    return (rest[: MAX_SUGGESTIONS - len(closing)] + closing)[:MAX_SUGGESTIONS]


# ---------------------------------------------------------------------------------- VERIFY_ID
def _verify(state: SessionState) -> list[dict[str, str]]:
    v, mem = state.verification, state.memory
    if v.consent_status == "pending":
        return [_s("consent.approved", "She says she approved it"), _s("consent.how", "How long will this take?"),
                _s("verify.human", "Can I speak to a person instead?")]
    if v.consent_status == "rep_name_needed":
        return []
    given = set(mem.family("identity"))
    refused = {k.split(".", 1)[1] for k in mem.slots if k.startswith("refused.")}
    out: list[dict[str, str]] = []
    if v.failures:
        out.append(_s("verify.retry", "Let me try a different detail"))
    if not given:
        out += [_s("verify.why", "Why do you need all of this?"), _s("verify.policy", "Isn't my policy number enough?")]
    else:
        out.append(_s("verify.why3", "Why do I need to give three details?"))
        if "phone" not in given and "phone" not in refused:
            out.append(_s("verify.phone", "Can I use my phone number instead?"))
    if "id_last4" not in given and "id_last4" not in refused:
        out.append(_s("verify.ssn", "I'd rather not share my SSN"))
    out.append(_s("verify.human", "I'd like to talk to a person"))
    return out


# ---------------------------------------------------------------------------------- RESOLVE_INTENT
def _distinguish(cases: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """A natural way to refer to each claim that tells it apart from the others in the list."""
    def unique(values: list[str]) -> bool:
        return len(set(values)) == len(values)

    # one consistent way of naming the whole list: by the first attribute that tells every claim apart
    statuses = [c["status"] for c in cases]
    types = [c["case_type"] for c in cases]
    both = [f"{c['status']} {c['case_type']}" for c in cases]
    when = [f"{MONTHS[int(c['created_at'][5:7]) - 1]} {c['created_at'][:4]}" for c in cases]
    if unique(statuses):
        names = [f"The {x} one" for x in statuses]
    elif unique(types):
        names = [f"The {x} claim" for x in types]
    elif unique(both):
        names = [f"The {x} claim" for x in both]
    elif unique(when):
        names = [f"The one from {x}" for x in when]
    else:
        names = [f"Claim {c['case_id']}" for c in cases]
    return [(c["case_id"], n) for c, n in zip(cases, names)]


def _resolve(state: SessionState, repo: Repository) -> list[dict[str, str]]:
    party = state.verification.party_id
    if state.pending_offer == "case_choice" and state.last_candidates:
        cases = [c for cid in state.last_candidates if (c := repo.case(party, cid))]
        return [_s(f"pick.{cid}", phrase + ".", phrase) for cid, phrase in _distinguish(cases)]
    return []


# ---------------------------------------------------------------------------------- PROCESS_CASE
def _process(state: SessionState, repo: Repository, today: date) -> list[dict[str, str]]:
    party, cid = state.verification.party_id, state.active_case_id
    case = repo.case(party, cid) if cid else None
    if not case:
        return []
    f = repo.fact_sheet(case, today)
    docs = case.get("documents_needed", [])
    out: list[dict[str, str]] = []

    def add(key: str, text: str, label: str | None = None) -> None:
        out.append(_s(f"{cid}.{key}", text, label))

    if docs:
        add("docs.submit", "How do I submit the documents?" if len(docs) > 1 else f"How do I submit the {docs[0]}?")
        add("docs.missing", f"What if I can't get the {docs[0]}?")
        add("review.time", "How long does the review take once I send them?")
        if f.get("appeal_deadline"):
            if f["appeal_deadline_passed"]:
                add("appeal.late", "The deadline has passed. What are my options?")
            else:
                add("appeal.miss", "What happens if I miss the deadline?")
    elif case["status"] == "open":
        amounts = {k: v["value"] for k, v in f["amounts"].items()}
        add("next", "What happens next with this claim?")
        if amounts.get("net_pay") == "$0.00" and amounts.get("expected_reimbursement_amount") not in (None, "$0.00"):
            add("unpaid", "Why hasn't anything been paid yet?")
        add("amounts", "What do the amounts on this claim mean?", "What do the amounts mean?")
    elif case["status"] == "closed":
        amounts = {k: v["value"] for k, v in f["amounts"].items()}
        add("paid", "What was paid on this claim?")
        if amounts.get("net_pay") and amounts.get("allowed_max_amount") and amounts["net_pay"] != amounts["allowed_max_amount"]:
            add("shortfall", f"Why wasn't the full {amounts['allowed_max_amount']} paid?")
        add("amounts", "What do the amounts on this claim mean?", "What do the amounts mean?")
    else:
        add("next", "What happens next with this claim?")

    if len(repo.cases_for(party)) > 1:
        add("other", "Can you tell me about my other claims?", "My other claims")
    if state.case_ledger.get(cid, {}).get("topics"):  # only offer to wrap up once something was actually answered
        add("wrap.up", "That's everything, thanks.", "That's all, thanks")
    return out


# ---------------------------------------------------------------------------------- POST_PROCESS
def _post(state: SessionState) -> list[dict[str, str]]:
    if state.pending_offer == "email":
        return [_s("email.yes", "Yes please, send it."), _s("email.no", "No thanks, skip the email.")]
    return [_s("end.done", "No, that's all. Thanks!"), _s("end.more", "Actually, I have another question.")]
