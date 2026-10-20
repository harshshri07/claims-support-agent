"""Deterministic pre-extraction.

Runs before (and independently of) the LLM extractor. It is the source of truth for
well-formed values (emails, phones, IDs) and the backstop if the LLM call fails.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from .normalize import month_number, norm_date, norm_email, norm_last4, norm_phone

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<![\d-])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
POLICY = re.compile(r"\bPOL[\s-]?(\d{3,6})\b", re.I)
CASE_ID = re.compile(r"\b(?:CL|claim)[\s#-]*(\d{4})\b", re.I)
LAST4 = re.compile(
    r"(?:ssn|social(?:\s+security)?|national\s+id|government\s+id|id\s+number|last\s+(?:4|four)(?:\s+digits)?"
    r"|ending\s+in)\D{0,25}?(\d{4})\b", re.I)
_MON = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
DATE = re.compile(
    rf"\b(\d{{4}}-\d{{1,2}}-\d{{1,2}}|\d{{1,2}}[/.-]\d{{1,2}}[/.-]\d{{2,4}}|{_MON}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
    rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+{_MON},?\s+\d{{4}})", re.I)
MONTH_WORD = re.compile(rf"\b({_MON})\b(?:\s+(\d{{4}}))?", re.I)
DOB_CONTEXT = re.compile(r"\b(dob|d\.o\.b|born|birth|birthday)\b", re.I)

CASE_TYPES = {"healthcare": ("healthcare", "health care", "medical", "health", "hospital", "doctor", "surgery",
                             "pathology", "biopsy", "clinic"),
              "dental": ("dental", "dentist", "teeth", "tooth", "orthodont"),
              "auto": ("auto", "car", "vehicle", "accident", "collision", "fender")}
STATUSES = {"denied": ("denied", "denial", "rejected", "declined", "turned down"),
            "open": ("open", "pending", "in progress", "processing", "under review", "ongoing"),
            "closed": ("closed", "settled", "paid", "completed", "finished")}


def canonical_case_type(s: str | None) -> str | None:
    if not s:
        return None
    s = s.lower()
    return next((k for k, words in CASE_TYPES.items() if any(w in s for w in words)), None)


def canonical_status(s: str | None) -> str | None:
    if not s:
        return None
    s = s.lower()
    return next((k for k, words in STATUSES.items() if any(re.search(rf"\b{w}", s) for w in words)), None)


def extract(text: str, today: date) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if m := EMAIL.search(text):
        out["email"] = norm_email(m.group(0))
    scrub = EMAIL.sub(" ", text)

    for m in DATE.finditer(scrub):
        iso = norm_date(m.group(0))
        if not iso:
            continue
        y = int(iso[:4])
        if y <= today.year - 14 or DOB_CONTEXT.search(scrub):
            out.setdefault("dob", iso)
        else:  # a recent date is a claim-date hint, never a DOB
            out.setdefault("case_month", int(iso[5:7]))
            out.setdefault("case_year", y)
    scrub = DATE.sub(" ", scrub)

    if m := LAST4.search(scrub):
        out["id_last4"] = norm_last4(m.group(1))
        scrub = scrub.replace(m.group(0), " ")
    if m := POLICY.search(scrub):
        out["policy_number"] = f"POL-{m.group(1)}"
        scrub = POLICY.sub(" ", scrub)
    if m := CASE_ID.search(scrub):
        out["case_id"] = f"CL-{m.group(1)}"
        scrub = CASE_ID.sub(" ", scrub)
    if m := PHONE.search(scrub):
        if p := norm_phone(m.group(0)):
            out["phone"] = p

    if "case_month" not in out:
        for m in MONTH_WORD.finditer(text):
            # "may" is too ambiguous as a bare word; require a year or "in/from May"
            if m.group(1).lower() == "may" and not m.group(2) and not re.search(r"\b(in|from|since)\s+may\b", text, re.I):
                continue
            if (mn := month_number(m.group(1))) and not DATE.search(text[max(0, m.start() - 3):m.end() + 9]):
                out["case_month"] = mn
                if m.group(2):
                    out["case_year"] = int(m.group(2))
                break
    if t := canonical_case_type(text):
        out["case_type"] = t
    if s := canonical_status(text):
        out["case_status"] = s
    return {k: v for k, v in out.items() if v}
