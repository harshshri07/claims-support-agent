"""A Directive is the controller's instruction to the responder: WHAT to say (acts + facts),
never left to the model. The model decides only HOW to say it."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


EMPATHY_OPENERS = {
    "frustrated": "I understand this is frustrating.",
    "angry": "I'm sorry this has been so frustrating.",
    "anxious": "I know this is stressful, so I'll keep it simple.",
    "confused": "No problem, let's take it one step at a time.",
    "sad": "I'm sorry you're dealing with this.",
}
RATIONALE_SHORT = {
    "identity": "Claim files hold medical and financial details, so I have to confirm it's you before opening one.",
    "consent": "Because the claim belongs to the policyholder, they need to approve sharing it, even with family.",
}


@dataclass
class Act:
    kind: str
    instruction: str               # what this part of the reply must accomplish
    template: str                  # deterministic fallback wording
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass
class Directive:
    acts: list[Act] = field(default_factory=list)
    empathy: dict[str, Any] | None = None
    must_not: list[str] = field(default_factory=list)
    delegate_process: bool = False       # PROCESS_CASE: hand the turn to the grounded agent
    process_request: str | None = None   # what the agent should answer (may be a remembered request)

    def add(self, kind: str, instruction: str, template: str, **facts) -> "Directive":
        self.acts.append(Act(kind, instruction, template, facts))
        return self

    def template(self) -> str:
        """Deterministic reply, used when the model is unavailable or its drafts fail the checks.
        It keeps the empathy policy too: acknowledge, explain why, then the next step."""
        parts = []
        e = self.empathy or {}
        if e.get("mode") == "empathize":
            parts.append(EMPATHY_OPENERS.get(e.get("label"), EMPATHY_OPENERS["frustrated"]))
            if e.get("rationale_key") in RATIONALE_SHORT:
                parts.append(RATIONALE_SHORT[e["rationale_key"]])
        parts += [a.template for a in self.acts if a.template]
        return " ".join(parts)

    def facts(self) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for a in self.acts:
            merged.update(a.facts)
        return merged

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
