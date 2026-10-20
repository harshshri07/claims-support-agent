"""Canonical forms for identity values, so matching is exact on meaning, not on spelling."""
from __future__ import annotations

import re
from datetime import date, datetime
from difflib import SequenceMatcher

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], 1)}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})
_MONTHS["sept"] = 9


def month_number(token: str) -> int | None:
    return _MONTHS.get(token.lower().strip(".")) if token else None


# ---- names ----------------------------------------------------------------
def norm_name(s: str | None) -> str | None:
    if not s:
        return None
    s = re.sub(r"[^a-z\s'-]", " ", s.lower())
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def name_matches(given: str | None, record_name: str, aliases: list[str] | None = None) -> bool:
    g = norm_name(given)
    if not g:
        return False
    gt = g.split()
    for cand in [record_name, *(aliases or [])]:
        c = norm_name(cand)
        ct = c.split()
        if g == c or sorted(gt) == sorted(ct):  # exact, or "Tian Ma" vs "Ma Tian"
            return True
        if len(ct) >= 2 and ct[0] in gt and ct[-1] in gt and len(gt) - len(ct) <= 1:  # middle name/initial
            return True
        if SequenceMatcher(None, " ".join(sorted(gt)), " ".join(sorted(ct))).ratio() >= 0.92:  # ASR typo
            return True
    return False


# ---- dates ----------------------------------------------------------------
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%m-%d-%y", "%m.%d.%Y",
                 "%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y")


def norm_date(s: str | None) -> str | None:
    if not s:
        return None
    t = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s.strip(), flags=re.I)
    t = re.sub(r"\bsept\b", "sep", t, flags=re.I)
    t = re.sub(r"\s+", " ", t.replace(",", ", ")).replace(" ,", ",").strip()
    for fmt in _DATE_FORMATS:
        for cand in (t, t.replace(",", "")):
            try:
                d = datetime.strptime(cand, fmt).date()
            except ValueError:
                continue
            if d.year > date.today().year + 1:  # 2-digit year rolled into the future
                d = d.replace(year=d.year - 100)
            return d.isoformat()
    return None


# ---- phone / email / id -----------------------------------------------------
def norm_phone(s: str | None) -> str | None:
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None


def norm_email(s: str | None) -> str | None:
    if not s:
        return None
    t = s.strip().lower().rstrip(".")
    t = re.sub(r"\s+at\s+", "@", t)
    t = re.sub(r"\s+dot\s+", ".", t)
    t = t.replace(" ", "")
    return t if re.fullmatch(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", t) else None


def norm_last4(s: str | None) -> str | None:
    if not s:
        return None
    digits = re.sub(r"\D", "", str(s))
    return digits[-4:] if len(digits) >= 4 else None


def mask_email(e: str) -> str:
    local, _, domain = e.partition("@")
    return f"{local[0]}{'*' * max(len(local) - 1, 3)}@{domain}"


def mask_phone(p: str) -> str:
    return f"(***) ***-{p[-4:]}"
