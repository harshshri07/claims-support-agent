"""Read-only access to the claims system fixtures.

Every claim lookup is scoped to a party_id: the harness can never fetch a claim
for anyone other than the verified policyholder.
"""
from __future__ import annotations

import json
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import settings

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def pretty_date(iso: str | None) -> str | None:
    if not iso:
        return None
    d = date.fromisoformat(iso)
    return f"{MONTHS[d.month - 1]} {d.day}, {d.year}"


def money(v: str | None) -> str | None:
    return None if v is None else f"${float(v):,.2f}"


class Repository:
    def __init__(self, root: Path):
        load = lambda n: json.loads((root / n).read_text(encoding="utf-8"))  # noqa: E731
        self.policyholders: list[dict[str, Any]] = load("policyholders.json")
        self.claims: list[dict[str, Any]] = load("claims.json")
        self.representatives: list[dict[str, Any]] = load("representatives.json")
        self.consent_scenarios: dict[str, Any] = load("consent_scenarios.json")
        self.guidelines: dict[str, Any] = load("required_document_guideline.json")
        self.schema: dict[str, Any] = load("claim_schema.json")

    # ---- parties -----------------------------------------------------------
    def party(self, party_id: str) -> dict[str, Any] | None:
        return next((p for p in self.policyholders if p["party_id"] == party_id), None)

    def representatives_for(self, party_id: str) -> list[dict[str, Any]]:
        return [r for r in self.representatives if r["buyer_party_id"] == party_id]

    # ---- claims (always party-scoped) -------------------------------------
    def cases_for(self, party_id: str) -> list[dict[str, Any]]:
        return sorted((c for c in self.claims if c["party_id"] == party_id),
                      key=lambda c: c["created_at"], reverse=True)

    def case(self, party_id: str, case_id: str) -> dict[str, Any] | None:
        case_id = (case_id or "").strip().upper()
        return next((c for c in self.cases_for(party_id) if c["case_id"] == case_id), None)

    def fact_sheet(self, c: dict[str, Any], today: date) -> dict[str, Any]:
        """The grounded view of a claim the LLM is allowed to talk about."""
        fd = self.schema["field_descriptions"]
        sheet: dict[str, Any] = {
            "case_id": c["case_id"],
            "claim_type": c["case_type"],
            "status": c["status"],
            "filed_on": c["created_at"],
            "filed_on_pretty": pretty_date(c["created_at"]),
            "summary": c["summary"],
            "amounts": {
                k: {"value": money(c[k]), "meaning": fd[k]["description"]}
                for k in ("allowed_max_amount", "expected_reimbursement_amount", "net_pay", "net_fee") if k in c
            },
        }
        if c.get("denial_reason"):
            sheet["denial_reason"] = c["denial_reason"]
        if c.get("documents_needed"):
            sheet["documents_needed"] = c["documents_needed"]
        if c.get("appeal_deadline"):
            dl = date.fromisoformat(c["appeal_deadline"])
            days = (dl - today).days
            sheet["appeal_deadline"] = c["appeal_deadline"]
            sheet["appeal_deadline_pretty"] = pretty_date(c["appeal_deadline"])
            sheet["days_until_appeal_deadline"] = days
            sheet["appeal_deadline_passed"] = days < 0
        return sheet

    def brief(self, c: dict[str, Any]) -> dict[str, str]:
        d = date.fromisoformat(c["created_at"])
        return {"case_id": c["case_id"], "type": c["case_type"], "status": c["status"],
                "filed": f"{MONTHS[d.month - 1]} {d.year}"}


@lru_cache(maxsize=1)
def get_repo() -> Repository:
    return Repository(settings.fixtures_dir)
