"""POST_PROCESS: build and send the call summary — only with the caller's explicit consent."""
from __future__ import annotations

import logging
import time
from typing import Any

from app.config import settings
from app.data.repository import Repository, pretty_date
from app.llm import ToolSpec, get_llm, structured
from app.mail.render import render_email
from app.mail.smtp import deliver

from .guards import check_reply
from .state import SessionState

log = logging.getLogger(__name__)

SUMMARY_TOOL = ToolSpec("write_summary", "Write the call summary content.", {
    "type": "object",
    "properties": {
        "discussed": {"type": "array", "items": {"type": "string"},
                      "description": "2-5 short bullets: what the caller asked and what was explained"},
        "next_steps": {"type": "array", "items": {"type": "string"},
                       "description": "1-5 concrete follow-up actions for the caller, most urgent first, with deadlines"},
    },
    "required": ["discussed", "next_steps"],
})

SUMMARY_PROMPT = """You write the body of a post-call summary email for an insurance claims support call.
Use ONLY the transcript and CLAIM RECORDS given. Do not invent amounts, dates, documents or promises.
Write bullets in second person ("You asked...", "Upload the..."), plain and specific. No greetings or sign-offs."""


def ledger_cases(repo: Repository, state: SessionState) -> list[dict[str, Any]]:
    party = state.verification.party_id
    out = []
    for cid in state.case_ledger:
        if c := repo.case(party, cid):
            out.append(repo.fact_sheet(c, settings.demo_today))
    return out


def _fallback(cases: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    discussed = [f"Status of claim {c['case_id']} ({c['claim_type']})" for c in cases] or ["Your account and claims"]
    steps = []
    for c in cases:
        if c.get("documents_needed"):
            steps.append(f"Submit {', '.join(c['documents_needed'])} for claim {c['case_id']} via the member portal"
                         + (f" before {c['appeal_deadline_pretty']}" if c.get("appeal_deadline_pretty") else "") + ".")
    return discussed, steps or ["No action needed from you right now."]


def build_and_send(repo: Repository, state: SessionState, use_llm: bool = True) -> dict[str, Any]:
    party = repo.party(state.verification.party_id)
    cases = ledger_cases(repo, state)
    convo = "\n".join(f"{'CALLER' if m['role'] == 'user' else 'AGENT'}: {m['content']}" for m in state.transcript[-30:])
    source = "llm"
    try:
        if not use_llm:
            raise RuntimeError("LLM disabled")
        out = structured(get_llm(), SUMMARY_PROMPT, [{"role": "user", "content":
                         f"CLAIM RECORDS:\n{cases}\n\nTRANSCRIPT:\n{convo}"}], SUMMARY_TOOL, max_tokens=700)
        discussed, steps = out.get("discussed", []), out.get("next_steps", [])
        violations = check_reply(" ".join(discussed + steps), repo=repo, verified=True,
                                 allowed_facts={"cases": cases, "transcript": convo}, caller_text="", party=None)
        if violations or not discussed:
            log.warning("summary rejected: %s", violations)
            discussed, steps = _fallback(cases)
            source = f"template (guard: {violations})"
    except Exception as e:  # noqa: BLE001
        log.warning("summary fallback: %s", e)
        discussed, steps = _fallback(cases)
        source = "template"

    call_date = pretty_date(settings.demo_today.isoformat())
    html, text = render_email(to_name=party["name"], cases=cases, discussed=discussed, next_steps=steps,
                              handoff=state.handoff, call_date=call_date)
    subject = "Summary of your claims support call" + (f" – {cases[0]['case_id']}" if cases else "")
    delivery = deliver(party["email"], subject, html, text)
    email = {"id": f"EM-{len(state.outbox) + 1}", "to": party["email"], "subject": subject, "html": html,
             "text": text, "delivery": delivery, "content_source": source, "sent_at": time.strftime("%H:%M:%S")}
    state.outbox.append(email)
    return email
