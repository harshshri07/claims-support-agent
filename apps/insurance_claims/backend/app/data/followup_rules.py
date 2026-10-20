"""Deterministic engine over required_document_guideline.json.

The LLM never invents claim follow-up policy: these functions return the exact
guideline text (templates filled with the case's own data). The PROCESS_CASE agent
is free to phrase it, and the output guard checks it did not add facts.
"""
from __future__ import annotations

import re
from typing import Any

from .docs_matcher import canonical_document
from .repository import Repository

_UNAVAILABLE = ("don't have", "dont have", "do not have", "can't get", "cant get", "cannot get", "lost",
                "unavailable", "alternative", "instead", "no longer have", "doesn't have", "won't give",
                "not able to get", "unable to get", "can't find", "cannot find", "substitute")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]", " ", s.lower())).strip()


def join_docs(docs: list[str]) -> str:
    docs = [f"the {d}" for d in docs]
    if len(docs) <= 1:
        return "".join(docs)
    return ", ".join(docs[:-1]) + " and " + docs[-1]


def _fill(template: str, case: dict[str, Any], settings_map: dict[str, Any]) -> str:
    values = {"case_id": case["case_id"], "documents": join_docs(case.get("documents_needed", []))}
    values.update({k: v["en"] for k, v in settings_map.items()})
    return template.format(**values)


def followup_guidance(repo: Repository, case: dict[str, Any], question: str, intent: str | None) -> dict[str, Any]:
    g = repo.guidelines
    q = _norm(question)
    has_docs = bool(case.get("documents_needed"))
    matched, contextual = [], []
    for rule in g["claim_followup_guidance"]:
        if rule.get("requires_documents") and not has_docs:
            continue
        text = _fill(rule["en"], case, g["claim_followup_settings"])
        if rule.get("match_any"):
            if any(_norm(p) in q for p in rule["match_any"]):
                matched.append({"topic": rule["topic"], "guidance": text})
        elif any(k in q for k in _UNAVAILABLE) and (intent is None or intent in rule.get("intent_hints", [])):
            contextual.append({"topic": rule["topic"], "guidance": text})
    result: dict[str, Any] = {"case_id": case["case_id"], "matched": matched + contextual}
    if not result["matched"]:
        result["fallback"] = g["claim_followup_fallback"]["en"]
    if any(k in q for k in _UNAVAILABLE):
        result["human_review_rule"] = g["claim_followup_settings"]["human_review_after_document_alternatives_exhausted"]["en"]
    return result


def document_guidance(repo: Repository, case: dict[str, Any] | None, document: str) -> dict[str, Any]:
    g = repo.guidelines
    keys = list(g["document_guidance"].keys())
    key = canonical_document(document, keys)
    out: dict[str, Any] = {"requested": document, "matched_document": key,
                           "general_submission_guidance": g["default_guidance"]["en"]}
    if case and case["case_type"] in g["case_type_guidance"]:
        out["claim_type_guidance"] = g["case_type_guidance"][case["case_type"]]["en"]
    if key:
        out["requirements"] = g["document_guidance"][key]["en"]
        alt = g["document_alternative_guidance"].get(key)
        out["if_unavailable"] = (alt or g["document_alternative_guidance"]["default"])["en"]
    else:
        out["note"] = "No document-specific guideline exists for this item; only the general guidance applies."
        out["if_unavailable"] = g["document_alternative_guidance"]["default"]["en"]
    return out
