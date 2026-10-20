"""Identity verification — fully deterministic. The LLM never decides who someone is."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.data.repository import Repository
from app.nlu.normalize import name_matches, norm_date, norm_email, norm_last4, norm_phone

REQUIRED_FACTORS = 3


@dataclass
class VerifyResult:
    status: str                       # verified | incomplete | failed
    party_id: str | None = None
    matched: list[str] = field(default_factory=list)
    mismatched: list[str] = field(default_factory=list)
    collected: list[str] = field(default_factory=list)


def _match_record(rec: dict[str, Any], f: dict[str, Any]) -> tuple[list[str], list[str]]:
    matched, mismatched = [], []

    def check(key: str, ok: bool):
        if f.get(key):
            (matched if ok else mismatched).append(key)

    check("name", name_matches(f.get("name"), rec["name"], rec.get("name_aliases")))
    check("dob", norm_date(f.get("dob")) == rec["dob"])
    phones = {rec["phone"], *rec.get("phone_aliases", [])}
    check("phone", norm_phone(f.get("phone")) in phones)
    emails = {rec["email"].lower(), *(e.lower() for e in rec.get("email_aliases", []))}
    check("email", norm_email(f.get("email")) in emails)
    # ssn_last4 and national_id_last4 are the same factor: "government ID last 4"
    check("id_last4", norm_last4(f.get("id_last4")) == rec["id_last4"])
    return matched, mismatched


def evaluate(repo: Repository, factors: dict[str, Any], policy_number: str | None = None) -> VerifyResult:
    collected = [k for k in ("name", "dob", "phone", "email", "id_last4") if factors.get(k)]
    scored = []
    for rec in repo.policyholders:
        m, mm = _match_record(rec, factors)
        if policy_number and m:
            # policy number is a lookup key, not a factor, but a contradicting one is a red flag
            if policy_number.upper() != rec["policy_number"]:
                mm.append("policy_number")
        scored.append((rec, m, mm))
    scored.sort(key=lambda t: (len(t[1]) - len(t[2]), len(t[1])), reverse=True)
    rec, matched, mismatched = scored[0]
    strong = [s for s in scored if len(s[1]) >= REQUIRED_FACTORS]

    if len(matched) >= REQUIRED_FACTORS and not mismatched and len(strong) == 1:
        return VerifyResult("verified", rec["party_id"], matched, [], collected)
    if len(collected) >= REQUIRED_FACTORS or (len(collected) >= 2 and mismatched and matched):
        # An attempt was made and it did not verify. Which fields failed stays internal.
        bad = mismatched if matched else collected
        return VerifyResult("failed", None, matched, bad, collected)
    return VerifyResult("incomplete", None, [], [], collected)


def rep_on_file(repo: Repository, party_id: str, rep_name: str | None) -> bool:
    """The real protection is the policyholder's 3 factors + their live consent; the rep's own name
    only has to identify a listed representative (a first name is enough)."""
    given = (rep_name or "").lower().split()
    for r in repo.representatives_for(party_id):
        if name_matches(rep_name, r["rep_name"]) or (given and given[0] == r["rep_name"].lower().split()[0]
                                                     and set(given) <= set(r["rep_name"].lower().split())):
            return True
    return False


def poll_consent(repo: Repository, scenario: str, polls: int) -> str:
    """Simulated consent request to the policyholder's phone. Returns pending | approved | timeout."""
    seq = repo.consent_scenarios.get(scenario, repo.consent_scenarios["default"])["status_sequence"]
    if polls < len(seq):
        return seq[polls]
    return "approved" if seq[-1] == "approved" else "timeout"
