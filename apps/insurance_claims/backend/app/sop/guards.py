"""Output guard: the last line of defence after the LLM writes a reply.

1. Disclosure firewall (pre-verification): no claims-system data and no on-file PII may
   appear in a reply unless the caller said it themselves.
2. Grounding check (post-verification): every case ID, money amount and calendar date
   in a reply must exist in the facts/tool results the model was given this turn.
3. PII echo: never read back on-file contact/ID data the caller did not say.
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.data.repository import Repository, money, pretty_date
from app.nlu.normalize import month_number

CASE_RE = re.compile(r"\bCL-\d{4}\b", re.I)
MONEY_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")
ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
MDY_RE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.I)


def _money(text: str) -> set[float]:
    return {float(m.replace(",", "")) for m in MONEY_RE.findall(text)}


def _days(text: str) -> set[tuple[int, int]]:
    out = {(int(m), int(d)) for _, m, d in ISO_RE.findall(text)}
    out |= {(month_number(mon), int(d)) for mon, d in MDY_RE.findall(text)}
    return out


def _sensitive_lexicon(repo: Repository) -> list[str]:
    terms: set[str] = set()
    for c in repo.claims:
        terms.add(c["case_id"].lower())
        for k in ("expected_reimbursement_amount", "allowed_max_amount", "net_pay", "net_fee"):
            if float(c[k]) > 0:
                terms.add(money(c[k]).lower())
        terms.update(d.lower() for d in c.get("documents_needed", []))
        if c.get("appeal_deadline"):
            terms.update({c["appeal_deadline"], pretty_date(c["appeal_deadline"]).lower().rsplit(",", 1)[0]})
        if c.get("denial_reason"):
            terms.add("denied because")
    for p in repo.policyholders:
        terms.update({p["phone"][-10:], p["email"].lower(), p["dob"], p["id_last4"], p["policy_number"].lower()})
    return sorted(terms)


# Promises of actions the system can't perform ("someone will reach out", "I'll set that up").
COMMITMENT_RE = re.compile(
    r"\b(will|'ll|is going to|are going to)\s+(reach out|call you|contact you|follow up|get back to you|text you|"
    r"be in touch)\b"
    r"|\b(someone|a representative|our team|they)\s+will\s+(reach out|call|contact|follow up)\b"
    r"|\bI'?ll\s+(arrange|set (that|this|it|you) up|make sure|get (that|this|it) (set up|sorted))\b"
    r"|\bI'?ve\s+(arranged|scheduled|set (that|this|it) up)\b"
    r"|\bbest (way|number|email) to reach you\b", re.I)
# steps where the agent genuinely does act, so this wording is legitimate
ACTING_STEPS = {"handoff", "verification_locked", "email_sent", "consent_requested", "consent_pending"}


def unauthorized_commitments(reply: str, act_kinds: set[str]) -> list[str]:
    if act_kinds & ACTING_STEPS:
        return []
    return [f"promises an action the system can't take: '{m.group(0)}'" for m in COMMITMENT_RE.finditer(reply)]


def check_reply(reply: str, *, repo: Repository, verified: bool, allowed_facts: Any, caller_text: str,
                party: dict[str, Any] | None) -> list[str]:
    r = reply.lower()
    said = caller_text.lower()
    said_digits = re.sub(r"\D", "", said)
    violations: list[str] = []

    if not verified:
        for t in _sensitive_lexicon(repo):
            if t in r and t not in said and not (t.isdigit() and t in said_digits):
                violations.append(f"pre-verification disclosure: '{t}'")
        return violations

    facts = json.dumps(allowed_facts, default=str).lower() if not isinstance(allowed_facts, str) else allowed_facts.lower()
    facts_and_said = facts + " " + said
    for cid in {c.lower() for c in CASE_RE.findall(r)}:
        if cid not in facts_and_said:
            violations.append(f"ungrounded case id {cid.upper()}")
    allowed_money = _money(facts_and_said) | {float(re.sub(r"[^\d.]", "", v)) for v in re.findall(r'"(\d+\.\d{2})"', facts)}
    for amt in _money(reply):
        if amt not in allowed_money:
            violations.append(f"ungrounded amount ${amt:,.2f}")
    allowed_days = _days(facts_and_said)
    for day in _days(reply):
        if day not in allowed_days:
            violations.append(f"ungrounded date {day[0]}/{day[1]}")
    if party:
        for label, val in (("phone", party["phone"][-10:]), ("email", party["email"].lower()),
                           ("dob", party["dob"]), ("id", party["id_last4"])):
            if val in r and val not in said and not (val.isdigit() and val in said_digits):
                violations.append(f"PII echo: {label}")
    return violations
