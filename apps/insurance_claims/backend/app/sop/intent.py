"""RESOLVE_INTENT: bounded intents + deterministic case scoring from remembered hints."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

IMPLIED_STATUS = {"denial_question": "denied", "appeal": "denied"}

INTENTS = ("denial_question", "status_inquiry", "document_submission", "next_steps", "payment_question",
           "appeal", "general_claim_question", "unsupported_request")

# hints are relaxed in this order when nothing matches (least reliable first)
_RELAX_ORDER = ("year", "month", "status", "case_type")


@dataclass
class CaseResolution:
    kind: str                          # unique | ambiguous | none | no_hints | no_cases
    case: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    relaxed: list[str] = field(default_factory=list)
    used_hints: dict[str, Any] = field(default_factory=dict)


def _filter(cases: list[dict[str, Any]], h: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for c in cases:
        y, m = int(c["created_at"][:4]), int(c["created_at"][5:7])
        if h.get("case_type") and c["case_type"] != h["case_type"]:
            continue
        if h.get("status") and c["status"] != h["status"]:
            continue
        if h.get("month") and m != int(h["month"]):
            continue
        if h.get("year") and y != int(h["year"]):
            continue
        out.append(c)
    return out


def resolve_case(cases: list[dict[str, Any]], hints: dict[str, Any], soft_status: str | None = None) -> CaseResolution:
    hints = {k: v for k, v in hints.items() if k in ("case_id", "case_type", "status", "month", "year") and v}
    if not cases:
        return CaseResolution("no_cases", used_hints=hints)
    if hints.get("case_id"):
        hit = [c for c in cases if c["case_id"] == str(hints["case_id"]).upper()]
        if hit:
            return CaseResolution("unique", hit[0], hit, used_hints=hints)
        hints = {k: v for k, v in hints.items() if k != "case_id"}  # unknown/foreign ID: fall back to other hints
    if not hints:
        pool = _filter(cases, {"status": soft_status}) if soft_status else []
        if len(cases) == 1 or len(pool) == 1:
            pick = cases[0] if len(cases) == 1 else pool[0]
            return CaseResolution("unique", pick, [pick], used_hints={"status (implied)": soft_status} if pool else {})
        return CaseResolution("no_hints", candidates=pool or cases)

    hits = _filter(cases, hints)
    if len(hits) > 1 and soft_status and not hints.get("status"):
        # intent implies a status ("why was it denied?") - use it only if it narrows without emptying
        narrowed = _filter(hits, {"status": soft_status})
        if narrowed:
            hits = narrowed
    if len(hits) == 1:
        return CaseResolution("unique", hits[0], hits, used_hints=hints)
    if len(hits) > 1:
        return CaseResolution("ambiguous", candidates=hits, used_hints=hints)

    relaxed, h = [], dict(hints)
    for key in _RELAX_ORDER:
        if key in h:
            h.pop(key)
            relaxed.append(key)
            hits = _filter(cases, h) if h else cases
            if hits:
                return CaseResolution("none", candidates=hits, relaxed=relaxed, used_hints=hints)
    return CaseResolution("none", candidates=cases, relaxed=relaxed, used_hints=hints)
