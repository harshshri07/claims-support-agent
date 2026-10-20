"""LLM registry: one active model for the whole server.

It is configured from environment variables at startup, or at runtime from the UI's *Model* dialog
(the key is kept in server memory only and never written to disk or returned by the API).
Provider "rules" runs the harness with no model at all (heuristic understanding + templates),
which is useful for checking that the SOP gates are enforced in code.
"""
from __future__ import annotations

import os
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any

from app import config as _config  # noqa: F401  (loads .env before reading the environment)

from .base import LLM, LLMError, LLMResponse, ToolCall, ToolSpec, structured

__all__ = ["LLM", "LLMError", "LLMResponse", "ToolCall", "ToolSpec", "structured", "get_llm", "llm_enabled",
           "LLMConfig", "PROVIDERS", "configure", "status", "test_config", "config_from_env"]

# provider -> UI metadata. "fields" lists what the Model dialog asks for.
# "suggested" is only the fallback shown before the provider's real list is loaded with the user's key.
PROVIDERS: dict[str, dict[str, Any]] = {
    "openai": {"label": "OpenAI", "model": "gpt-4.1-mini", "fields": ["api_key", "model"],
               "suggested": ["gpt-4.1-mini", "gpt-4o-mini", "gpt-4.1", "gpt-4o"]},
    "anthropic": {"label": "Anthropic (Claude)", "model": "claude-haiku-4-5", "fields": ["api_key", "model"],
                  "suggested": ["claude-haiku-4-5", "claude-sonnet-4-5"]},
    "gemini": {"label": "Google Gemini", "model": "gemini-2.5-flash", "fields": ["api_key", "model"],
               "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
               "suggested": ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro"]},
    "bedrock": {"label": "AWS Bedrock", "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                "fields": ["aws_access_key_id", "aws_secret_access_key", "region", "model"], "region": "us-east-1",
                "suggested": ["us.anthropic.claude-haiku-4-5-20251001-v1:0",
                              "us.anthropic.claude-sonnet-4-5-20250929-v1:0"]},
    "rules": {"label": "No model (rules-only check)", "model": "", "fields": []},
}


@dataclass
class LLMConfig:
    provider: str
    model: str = ""
    api_key: str = field(default="", repr=False)
    base_url: str = ""
    api_version: str = ""
    region: str = ""
    aws_access_key_id: str = field(default="", repr=False)
    aws_secret_access_key: str = field(default="", repr=False)
    source: str = "env"

    def filled(self) -> "LLMConfig":
        meta = PROVIDERS.get(self.provider, {})
        return replace(self, model=self.model or meta.get("model", ""),
                       base_url=self.base_url or meta.get("base_url", ""),
                       api_version=self.api_version or meta.get("api_version", ""),
                       region=self.region or meta.get("region", ""))


def config_from_env() -> LLMConfig | None:
    e = os.getenv
    provider = (e("LLM_PROVIDER") or "").lower().strip()
    if not provider:  # infer from whichever credentials are present
        provider = ("bedrock" if e("AWS_ACCESS_KEY_ID") or e("AWS_BEARER_TOKEN_BEDROCK") else
                    "anthropic" if e("ANTHROPIC_API_KEY") else
                    "openai" if e("OPENAI_API_KEY") else
                    "gemini" if e("GEMINI_API_KEY") else "")
    if not provider:
        return None
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown LLM_PROVIDER={provider!r}. Use one of: {', '.join(PROVIDERS)}")
    model = e("LLM_MODEL") or {
        "bedrock": e("BEDROCK_MODEL_ID"), "anthropic": e("ANTHROPIC_MODEL"), "openai": e("OPENAI_MODEL"),
        "gemini": e("GEMINI_MODEL")}.get(provider) or ""
    key = e("LLM_API_KEY") or {
        "anthropic": e("ANTHROPIC_API_KEY"), "openai": e("OPENAI_API_KEY"), "gemini": e("GEMINI_API_KEY")}.get(provider) or ""
    base = e("LLM_BASE_URL") or ""
    return LLMConfig(provider, model, key, base, "", e("AWS_REGION", ""),
                     e("AWS_ACCESS_KEY_ID", ""), e("AWS_SECRET_ACCESS_KEY", ""), "env").filled()


def build(cfg: LLMConfig) -> LLM | None:
    p = cfg.provider
    if p == "rules":
        return None
    if p == "bedrock":
        from .bedrock import BedrockLLM
        return BedrockLLM(cfg.model, cfg.region or "us-east-1", cfg.aws_access_key_id, cfg.aws_secret_access_key)
    if p == "anthropic":
        from .anthropic_direct import AnthropicLLM
        return AnthropicLLM(cfg.api_key, cfg.model)
    if p in ("openai", "gemini"):
        from .openai_compat import OpenAILLM
        return OpenAILLM(cfg.api_key or "not-needed", cfg.model, cfg.base_url, label=p)
    raise ValueError(f"unknown provider {p}")


_lock = threading.Lock()
_state: dict[str, Any] = {"cfg": None, "llm": None, "error": None}


def _init_from_env() -> None:
    try:
        cfg = config_from_env()
        if cfg:
            _state.update(cfg=cfg, llm=build(cfg), error=None)
    except Exception as ex:  # noqa: BLE001 - surface in /api/llm instead of crashing startup
        _state["error"] = f"{type(ex).__name__}: {ex}"


_init_from_env()


def configure(cfg: LLMConfig) -> None:
    """Set the server-wide default model (used by .env setups and scripts)."""
    llm = build(cfg)
    with _lock:
        _state.update(cfg=cfg, llm=llm, error=None)


# A visitor's own model choice applies only to their browser, never to other visitors of a hosted demo.
# The API sets it for the duration of a request; everything that calls get_llm() then uses it.
@dataclass
class ClientModel:
    cfg: LLMConfig
    llm: LLM | None
    created: float = field(default_factory=time.time)


_client_model: ContextVar[ClientModel | None] = ContextVar("client_model", default=None)


def use_client_model(entry: ClientModel | None):
    return _client_model.set(entry)


def reset_client_model(token) -> None:
    _client_model.reset(token)


def get_llm() -> LLM:
    entry = _client_model.get()
    llm = entry.llm if entry else _state["llm"]
    if llm is None:
        raise LLMError("No language model is connected.")
    return llm


def llm_enabled() -> bool:
    entry = _client_model.get()
    return (entry.llm if entry else _state["llm"]) is not None


def status(entry: ClientModel | None = None) -> dict[str, Any]:
    cfg: LLMConfig | None = entry.cfg if entry else _state["cfg"]
    return {
        "configured": cfg is not None,
        "provider": cfg.provider if cfg else None,
        "provider_label": PROVIDERS[cfg.provider]["label"] if cfg else None,
        "model": cfg.model if cfg else None,
        "source": ("your key" if entry and any(getattr(cfg, f) for f in SECRET_FIELDS) and cfg.source == "ui-own"
                   else "server" if cfg else None),
        "error": None if entry else _state["error"],
    }


SECRET_FIELDS = ("api_key", "aws_access_key_id", "aws_secret_access_key")
def server_key_models() -> list[str]:
    """Models a visitor may run on the SERVER's key (SERVER_KEY_MODELS, comma-separated). Empty = any (local use)."""
    return [m.strip() for m in os.getenv("SERVER_KEY_MODELS", "").split(",") if m.strip()]


def with_current_secrets(cfg: LLMConfig) -> LLMConfig:
    """Same provider as the server's and no key typed: reuse the server's key (it's never sent to the browser).
    On a hosted demo, SERVER_KEY_MODELS limits which models that key can be used for, to cap cost."""
    cur: LLMConfig | None = _state["cfg"]
    if any(getattr(cfg, f) for f in SECRET_FIELDS):
        return replace(cfg, source="ui-own")
    if cur and cur.provider == cfg.provider:
        allowed = server_key_models()
        if allowed and cfg.model not in allowed:
            raise PermissionError("This demo's shared key is limited to: " + ", ".join(allowed)
                                  + ". Use your own API key for other models.")
        return replace(cfg, **{f: getattr(cur, f) for f in SECRET_FIELDS},
                       base_url=cfg.base_url or cur.base_url, region=cfg.region or cur.region, source="ui-server-key")
    return cfg


def _invokable(client_cfg: LLMConfig, ids: list[str]) -> list[str]:
    """Bedrock lists models the account may not be allowed to call; try one tiny request on each, in parallel."""
    from concurrent.futures import ThreadPoolExecutor

    def ok(model_id: str) -> bool:
        try:
            build(replace(client_cfg, model=model_id)).complete("Reply OK.", [{"role": "user", "content": "hi"}], max_tokens=5)
            return True
        except Exception:  # noqa: BLE001
            return False

    with ThreadPoolExecutor(max_workers=6) as pool:
        flags = list(pool.map(ok, ids))
    return [m for m, f in zip(ids, flags) if f]


def list_models(cfg: LLMConfig) -> dict[str, list[str]]:
    """Chat models this key can use, narrowed to the ones that suit this app and ordered best-first."""
    from . import catalog

    p = cfg.provider
    if p == "bedrock":
        import boto3
        creds = ({"aws_access_key_id": cfg.aws_access_key_id, "aws_secret_access_key": cfg.aws_secret_access_key}
                 if cfg.aws_access_key_id else {})
        client = boto3.client("bedrock", region_name=cfg.region or "us-east-1", **creds)
        profiles = client.list_inference_profiles(maxResults=500)["inferenceProfileSummaries"]
        ids = [x["inferenceProfileId"] for x in profiles if x.get("status") == "ACTIVE"]
        # each model appears under several geographies (us./eu./apac./global.); keep the one for this region
        geo = {"us": "us.", "eu": "eu.", "ap": "apac.", "ca": "ca."}.get((cfg.region or "us-east-1")[:2], "us.")
        ids = [i for i in ids if i.startswith(geo)] or ids
    elif p == "anthropic":
        import anthropic
        ids = [m.id for m in anthropic.Anthropic(api_key=cfg.api_key).models.list(limit=100).data]
    elif p in ("openai", "gemini"):
        import openai
        client = openai.OpenAI(api_key=cfg.api_key or "not-needed", base_url=cfg.base_url or None, timeout=20)
        ids = sorted({m.id.removeprefix("models/") for m in client.models.list().data})
    else:
        return {"models": [], "recommended": []}
    models, recommended = catalog.prune(p, ids)
    if p == "bedrock":   # listed is not the same as invokable: drop what this account has no access to
        models = _invokable(client_cfg=cfg, ids=models) or models
        recommended = [m for m in recommended if m in models]
    return {"models": models, "recommended": recommended}


FAST_S, OK_S = 4.0, 9.0   # average seconds per model call; every chat message makes two calls


def _check_plain(llm) -> tuple[bool, str]:
    r = llm.complete("You are a test harness.", [{"role": "user", "content": "Reply with: OK"}], max_tokens=20)
    return bool(r.text.strip()), r.text[:30]


def _check_structured(llm) -> tuple[bool, str]:
    out = structured(llm, "Extract data.", [{"role": "user", "content": "My birthday is March 15 1985"}],
                     ToolSpec("record", "Record fields", {"type": "object", "properties": {
                         "dob": {"type": "string", "description": "YYYY-MM-DD"}}, "required": ["dob"]}), max_tokens=200)
    return str(out.get("dob", "")).startswith("1985-03-15"), str(out)[:60]


def _check_tool_loop(llm) -> tuple[bool, str]:
    tool = ToolSpec("get_claim_status", "Look up the status of a claim by its ID.",
                    {"type": "object", "properties": {"case_id": {"type": "string"}}, "required": ["case_id"]})
    msgs: list[dict[str, Any]] = [{"role": "user", "content":
                                   "What is the status of claim CL-2048? Look it up with the tool, then tell me."}]
    r1 = llm.complete("You answer using the tools you are given.", msgs, tools=[tool], max_tokens=300)
    if not r1.tool_calls:
        return False, "did not call the tool"
    call = r1.tool_calls[0]
    msgs += [{"role": "assistant", "content": r1.text,
              "tool_calls": [{"id": call.id, "name": call.name, "input": call.input}]},
             {"role": "tool", "results": [{"id": call.id, "content": {"status": "denied"}}]}]
    r2 = llm.complete("You answer using the tools you are given.", msgs, tools=[tool], max_tokens=300)
    return "denied" in r2.text.lower(), r2.text[:60]


_CHECKS = (("plain", _check_plain, 1, "it did not return text"),
           ("structured", _check_structured, 1, "it can't return reliable structured output"),
           ("tools", _check_tool_loop, 2, "it doesn't handle multi-step tool use"))


def test_config(cfg: LLMConfig) -> dict[str, Any]:
    """Check what the app needs from a model: plain replies, structured output (forced tool call) and a tool loop,
    and measure speed. Used by "Test connection", before a model is switched in."""
    if cfg.provider == "rules":
        return {"ok": True, "message": "Rules-only mode: no model calls."}
    try:
        llm = build(cfg)
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "message": f"{type(ex).__name__}: {str(ex)[:300]}"}
    results, calls, total = [], 0, 0.0
    for name, fn, n_calls, why in _CHECKS:
        t = time.time()
        try:
            ok, detail = fn(llm)
        except Exception as ex:  # noqa: BLE001
            ok, detail = False, f"{type(ex).__name__}: {str(ex)[:240]}"
        dt = time.time() - t
        total, calls = total + dt, calls + n_calls
        results.append({"check": name, "ok": ok, "secs": round(dt, 2), "detail": detail})
        if not ok:
            reason = detail if name == "plain" or detail.startswith(("AuthenticationError", "LLMError", "PermissionDenied")) \
                or "Error" in detail[:40] else why
            return {"ok": False, "checks": results, "message": f"This model can't be used here: {reason}."
                    if detail == reason else f"This model can't be used here: {why}. ({detail[:160]})"}
    per_call = total / calls
    speed = "fast" if per_call <= FAST_S else "ok" if per_call <= OK_S else "slow"
    note = {"fast": f"Works well with this app ({per_call:.1f}s per model call).",
            "ok": f"Works, but a little slow ({per_call:.1f}s per model call, two per message).",
            "slow": f"Works, but too slow for a chat ({per_call:.1f}s per model call, two per message). "
                    "A faster model is recommended."}[speed]
    return {"ok": True, "speed": speed, "latency_s": round(per_call, 2), "checks": results, "message": note}
