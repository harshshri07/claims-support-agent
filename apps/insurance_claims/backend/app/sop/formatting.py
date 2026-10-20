"""Reply layout. Applied to every agent message so replies are scannable whichever model wrote them.

  * a long single paragraph is split into short paragraphs (at most two sentences each)
  * a closing question goes on its own line
  * claim numbers, dates and amounts are bold
Text the author already structured (blank lines or bullet lists) keeps its layout.
"""
from __future__ import annotations

import re

_ABBREV = ("dr", "mr", "mrs", "ms", "st", "inc", "vs", "etc", "e.g", "i.e", "no", "approx", "jan", "feb", "mar", "apr",
           "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"“(*$])")
_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_FACTS = re.compile(rf"\bCL-\d{{4}}\b|\b(?:{_MONTHS}) \d{{1,2}}, \d{{4}}\b|\$\d[\d,]*(?:\.\d{{2}})?")
_BOLD_SPAN = re.compile(r"(\*\*.+?\*\*)")

MAX_WORDS_PER_PARAGRAPH = 48
LONG_REPLY_WORDS = 38


def _sentences(text: str) -> list[str]:
    parts, out = _SENTENCE_END.split(text.strip()), []
    for part in parts:  # re-join splits that happened after an abbreviation ("Dr. Smith", "No. 5")
        if out and out[-1].rstrip(".").split(" ")[-1].lower().rstrip(".") in _ABBREV and out[-1].endswith("."):
            out[-1] += " " + part
        else:
            out.append(part)
    return [s.strip() for s in out if s.strip()]


def _paragraphs(text: str) -> str:
    paras: list[str] = []
    cur: list[str] = []
    words = 0
    for s in _sentences(text):
        n = len(s.split())
        if s.endswith("?"):  # the question gets its own line
            if cur:
                paras.append(" ".join(cur))
            paras.append(s)
            cur, words = [], 0
            continue
        if cur and (len(cur) >= 2 or words + n > MAX_WORDS_PER_PARAGRAPH):
            paras.append(" ".join(cur))
            cur, words = [], 0
        cur.append(s)
        words += n
    if cur:
        paras.append(" ".join(cur))
    return "\n\n".join(paras)


def _bold_facts(text: str) -> str:
    chunks = _BOLD_SPAN.split(text)
    for i in range(0, len(chunks), 2):  # even chunks are outside existing **bold**
        chunks[i] = _FACTS.sub(lambda m: f"**{m.group(0)}**", chunks[i])
    return "".join(chunks)


def tidy(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return t
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    if "\n" not in t and len(t.split()) > LONG_REPLY_WORDS:
        t = _paragraphs(t)
    return _bold_facts(t)
