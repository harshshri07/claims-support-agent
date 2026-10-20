"""Canonicalise free-text document names onto the guideline's document keys.

Claims say "pathology report"; the guideline is keyed "original pathology report".
Callers say "the doctor's note" or "photos". Qualifier words are dropped and the
content words are compared.
"""
from __future__ import annotations

import re

_QUALIFIERS = {"the", "a", "an", "my", "of", "original", "supplemental", "treating", "provider",
               "copy", "full", "complete", "doctor", "doctors", "doctor's", "s"}
_SYNONYMS = {"notes": "note", "photo": "photos", "pictures": "photos", "picture": "photos",
             "images": "photos", "quote": "estimate", "pathology": "pathology", "visit": "office"}


def _content(name: str) -> set[str]:
    toks = re.findall(r"[a-z']+", name.lower())
    return {_SYNONYMS.get(t, t) for t in toks if t not in _QUALIFIERS}


def canonical_document(name: str, keys: list[str]) -> str | None:
    want = _content(name)
    if not want:
        return None
    best, best_score = None, 0.0
    for key in keys:
        have = _content(key)
        if want <= have or have <= want:
            score = len(want & have) / len(want | have) + 0.5
        else:
            score = len(want & have) / len(want | have)
        if score > best_score:
            best, best_score = key, score
    return best if best_score >= 0.5 else None
