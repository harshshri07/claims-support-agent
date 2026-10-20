"""Which models suit this app.

Every turn needs three things from a model: (1) a plain reply, (2) structured output through a *forced* tool call
(the understanding step), and (3) multi-step tool use (the grounded claim agent). It also makes two model calls per
message, so speed and cost matter. The lists a provider returns are full of models that can't or shouldn't do this
(embeddings, audio, image, reasoning-first models that take 10+ seconds, legacy generations). This module keeps the
ones that fit, and orders them best-first. `test_config` in `app/llm/__init__.py` then verifies the choice for real.
"""
from __future__ import annotations

import re

# never useful here, whatever the provider
_EXCLUDE = ("embed", "whisper", "tts", "dall-e", "moderation", "audio", "realtime", "image", "transcribe", "search",
            "davinci", "babbage", "computer-use", "aqa", "imagen", "veo", "stability", "twelvelabs", "upscale",
            "outpaint", "sora", "rerank", "codex", "instruct", "deep-research", "guard", "live", "native-audio",
            "robotics", "tuning", "chatgpt", "pegasus", "pixtral", "palmyra", "kimi", "glm", "grok", "r1")
_DATED = re.compile(r"-\d{4}-\d{2}-\d{2}$")          # gpt-4o-2024-08-06: keep the alias, hide the snapshot


def _bad(model_id: str) -> bool:
    m = model_id.lower()
    return any(w in m for w in _EXCLUDE)


def rank(provider: str, model_id: str) -> int | None:
    """Lower = more recommended. None = not suitable for this app."""
    m = model_id.lower()
    if _bad(model_id):
        return None
    if provider == "openai":
        if _DATED.search(m) or "-pro" in m or re.match(r"^(o\d|gpt-3|gpt-4-)", m):   # snapshots, very slow/expensive, reasoning-first, legacy
            return None
        order = [("gpt-4.1-mini", 0), ("gpt-4o-mini", 1), ("gpt-4.1-nano", 2), ("gpt-4.1", 3), ("gpt-4o", 4),
                 ("gpt-5-mini", 5), ("gpt-5-nano", 6), ("gpt-5", 7)]
        for prefix, r in order:
            if m == prefix:
                return r
        return 20 if re.match(r"^gpt-(4o|4\.1|5)", m) else None
    if provider == "anthropic":
        mm = re.match(r"^claude-(haiku|sonnet)-(\d+)", m)
        return None if not mm or int(mm.group(2)) < 4 else (0 if mm.group(1) == "haiku" else 1)
    if provider == "gemini":
        if _DATED.search(m) or not re.match(r"^gemini-(2\.5|[3-9])", m):
            return None
        return 0 if "flash-lite" not in m and "flash" in m else 1 if "flash-lite" in m else 2 if "pro" in m else None
    if provider == "bedrock":
        # inference-profile ids look like  us.anthropic.claude-haiku-4-5-20251001-v1:0
        bare = re.sub(r"^(us|eu|apac|global|ca|jp|au)\.", "", m)
        mm = re.match(r"^anthropic\.claude-(haiku|sonnet)-(\d+)", bare)
        if mm:
            return None if int(mm.group(2)) < 4 else (0 if mm.group(1) == "haiku" else 1)
        return None
    return None


def prune(provider: str, ids: list[str]) -> tuple[list[str], list[str]]:
    """(suitable models best-first, the recommended few)."""
    scored = [(rank(provider, i), i) for i in ids]
    keep = sorted((s, i) for s, i in scored if s is not None)
    # within a rank, newest first (higher version numbers sort later alphabetically)
    ordered: list[str] = []
    for r in sorted({s for s, _ in keep}):
        ordered += sorted((i for s, i in keep if s == r), reverse=True)
    return ordered, [i for i in ordered if (r := rank(provider, i)) is not None and r <= 4][:4]
