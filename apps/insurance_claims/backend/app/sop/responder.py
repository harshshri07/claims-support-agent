"""Natural-language rendering of a Directive (STRICT / GUIDED phases)."""
from __future__ import annotations

import json
import logging
from typing import Any

from app.llm import get_llm

from .directive import Directive

log = logging.getLogger(__name__)

PERSONA = """You are Alex, a claims support specialist at Harborview Mutual Insurance, chatting with a caller over text.

HARD RULES (never break these):
- Your message must accomplish every ACT in the directive, in order, merged into one natural reply.
- Use ONLY facts given in the directive. Never add claim details, amounts, dates, reasons, or policy rules of your own.
- Never say which identity detail did or didn't match, and never read back personal data from records.
- Never answer out-of-scope questions, not even partially or "briefly" - just decline kindly and redirect.
- Never mention phases, SOPs, directives, tools, JSON, or internal systems.
- Never promise outcomes (approvals, payments, timelines) beyond the facts given.
- Never commit to actions that aren't in the directive: no callbacks, "someone will reach out", "I'll set that up",
  "I'll make sure it gets there". Don't ask for contact details. A transfer to a person is only ever OFFERED as a question.
- Before identity is confirmed, never say a detail is "on file", "matches" or "checks out".
- You confirm the caller's identity; never say "thanks for verifying".

STYLE:
- Sound like a skilled, warm human support rep: plain, specific words, contractions, no emojis, no em dashes.
- Never use these stock phrases: "I get it", "I hear you", "The thing is", "Here's the thing", "totally up to you",
  "a lot of folks", "Great question", "Absolutely", "Perfect", "No worries", "I completely understand".
- Short: 1-3 sentences for identity steps, 2-4 otherwise. When offering 3+ options, put each on its own line with "- ".
- Ask exactly one question when the directive needs something, and end with it.
- If an earlier ALEX message already listed all the identity options, don't repeat the whole list: say how many more
  details are needed and name the remaining options in one short phrase.
- If an EMPATHY block is present, acknowledge the caller's specific feeling in one clause that reflects what they said
  (not a generic phrase). Give the rationale only if one is provided, in your own words, in one sentence. Then the next
  step. Don't over-apologise."""


def render(directive: Directive, transcript: list[dict[str, Any]], correction: str | None = None) -> str:
    payload: dict[str, Any] = {
        "acts": [{"act": a.kind, "instruction": a.instruction, "facts": a.facts} for a in directive.acts],
    }
    if directive.empathy and directive.empathy.get("mode"):
        payload["EMPATHY"] = {k: directive.empathy.get(k) for k in ("label", "intensity", "cue", "style", "rationale")
                              if directive.empathy.get(k)}
    if directive.must_not:
        payload["must_not"] = directive.must_not
    convo = "\n".join(f"{'CALLER' if m['role'] == 'user' else 'ALEX'}: {m['content']}" for m in transcript[-8:])
    user = (f"Conversation so far:\n{convo}\n\nDIRECTIVE for your next message:\n{json.dumps(payload, indent=1)}"
            "\n\nWrite ALEX's next message only.")
    if correction:
        user += f"\n\nYour previous draft was rejected: {correction}. Rewrite it without that content."
    resp = get_llm().complete(PERSONA, [{"role": "user", "content": user}], max_tokens=350, temperature=0.5)
    text = resp.text.strip()
    if text.upper().startswith("ALEX:"):
        text = text[5:].strip()
    return text
