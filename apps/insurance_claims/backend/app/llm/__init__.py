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
    "openai": {"label": "OpenAI", "model": "gpt-4o-mini", "fields": ["api_key", "model"],
               "suggested": ["gpt-4o-mini", "gpt-4.1-mini", "gpt-4o"]},
    "anthropic": {"label": "Anthropic (Claude)", "model": "claude-haiku-4-5", "fields": ["api_key", "model"],
                  "suggested": ["claude-haiku-4-5", "claude-sonnet-4-5", "claude-haiku-5-5", "claude-sonnet-5-5"]},
    "gemini": {"label": "Google Gemini", "model": "gemini-2.5-flash", "fields": ["api_key", "model"],
               "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
               "suggested": ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro"]},
    "azure": {"label": "Azure OpenAI", "model": "", "fields": ["api_key", "base_url", "model", "api_version"],
              "hints": {"base_url": "https://<resource>.openai.azure.com", "model": "your deployment name"},
              "api_version": "2024-10-21", "suggested": []},
    "bedrock": {"label": "AWS Bedrock", "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                "fields": ["aws_access_key_id", "aws_secret_access_key", "region", "model"], "region": "us-east-1",
                "suggested": ["us.anthropic.claude-haiku-4-5-20251001-v1:0",
                              "us.anthropic.claude-sonnet-4-5-20250929-v1:0"]},
    "openai_compatible": {"label": "OpenAI-compatible (Ollama, OpenRouter, Groq, vLLM…)", "model": "",
                          "fields": ["base_url", "api_key", "model"], "suggested": [],
                          "hints": {"base_url": "http://localhost:11434/v1  ·  https://openrouter.ai/api/v1",
                                    "api_key": "optional for local servers"}},
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
                    "gemini" if e("GEMINI_API_KEY") else
                    "azure" if e("AZURE_OPENAI_API_KEY") else "")
    if not provider:
        return None
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown LLM_PROVIDER={provider!r}. Use one of: {', '.join(PROVIDERS)}")
    model = e("LLM_MODEL") or {
        "bedrock": e("BEDROCK_MODEL_ID"), "anthropic": e("ANTHROPIC_MODEL"), "openai": e("OPENAI_MODEL"),
        "gemini": e("GEMINI_MODEL"), "azure": e("AZURE_OPENAI_DEPLOYMENT")}.get(provider) or ""
    key = e("LLM_API_KEY") or {
        "anthropic": e("ANTHROPIC_API_KEY"), "openai": e("OPENAI_API_KEY"), "gemini": e("GEMINI_API_KEY"),
        "azure": e("AZURE_OPENAI_API_KEY"), "openai_compatible": e("OPENAI_API_KEY")}.get(provider) or ""
    base = e("LLM_BASE_URL") or {"azure": e("AZURE_OPENAI_ENDPOINT"), "openai": e("OPENAI_BASE_URL"),
                                 "openai_compatible": e("OPENAI_BASE_URL")}.get(provider) or ""
    return LLMConfig(provider, model, key, base, e("AZURE_OPENAI_API_VERSION", ""), e("AWS_REGION", ""),
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
    if p in ("openai", "gemini", "openai_compatible"):
        from .openai_compat import OpenAILLM
        return OpenAILLM(cfg.api_key or "not-needed", cfg.model, cfg.base_url, label=p)
    if p == "azure":
        from .openai_compat import OpenAILLM
        return OpenAILLM(cfg.api_key, cfg.model, azure_endpoint=cfg.base_url, api_version=cfg.api_version, label="azure")
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
_NOT_CHAT = ("embed", "whisper", "tts", "dall-e", "moderation", "audio", "realtime", "image", "transcribe",
             "search", "davinci", "babbage", "computer-use", "aqa", "imagen", "veo", "stability", "twelvelabs",
             "upscale", "outpaint", "sora", "rerank")


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


def list_models(cfg: LLMConfig) -> list[str]:
    """The chat models this key can use, straight from the provider."""
    p = cfg.provider
    if p == "bedrock":
        import boto3
        creds = ({"aws_access_key_id": cfg.aws_access_key_id, "aws_secret_access_key": cfg.aws_secret_access_key}
                 if cfg.aws_access_key_id else {})
        client = boto3.client("bedrock", region_name=cfg.region or "us-east-1", **creds)
        profiles = client.list_inference_profiles(maxResults=500)["inferenceProfileSummaries"]
        ids = [x["inferenceProfileId"] for x in profiles if x.get("status") == "ACTIVE"]
        ids = [i for i in ids if not any(w in i for w in _NOT_CHAT)]
        # each model appears under several geographies (us./eu./apac./global.); keep the one for this region
        geo = {"us": "us.", "eu": "eu.", "ap": "apac.", "ca": "ca."}.get((cfg.region or "us-east-1")[:2], "us.")
        ids = [i for i in ids if i.startswith(geo)] or ids
        return sorted(ids, key=lambda i: (".anthropic." not in i, i))
    if p == "anthropic":
        import anthropic
        return [m.id for m in anthropic.Anthropic(api_key=cfg.api_key).models.list(limit=100).data]
    if p in ("openai", "gemini", "openai_compatible"):
        import openai
        client = openai.OpenAI(api_key=cfg.api_key or "not-needed", base_url=cfg.base_url or None, timeout=20)
        ids = [m.id.removeprefix("models/") for m in client.models.list().data]
        ids = [i for i in ids if not any(w in i.lower() for w in _NOT_CHAT)]
        if p == "gemini":
            ids = [i for i in ids if i.startswith("gemini")]
        return sorted(set(ids))
    return []  # azure: deployments are named by the user


def test_config(cfg: LLMConfig) -> dict[str, Any]:
    """Two tiny calls: a completion and a forced tool call (the harness needs both)."""
    if cfg.provider == "rules":
        return {"ok": True, "message": "Rules-only mode: no model calls."}
    t = time.time()
    try:
        llm = build(cfg)
        r = llm.complete("You are a test harness.", [{"role": "user", "content": "Reply with: OK"}], max_tokens=20)
        out = structured(llm, "Extract data.", [{"role": "user", "content": "My birthday is March 15 1985"}],
                         ToolSpec("record", "Record fields", {"type": "object", "properties": {
                             "dob": {"type": "string", "description": "YYYY-MM-DD"}}, "required": ["dob"]}),
                         max_tokens=200)
        ok = str(out.get("dob", "")).startswith("1985-03-15")
        return {"ok": ok, "latency_s": round(time.time() - t, 2), "reply": r.text[:40],
                "message": "Connected. Text and tool calling both work." if ok
                else "Connected, but structured output looks unreliable with this model."}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "message": f"{type(ex).__name__}: {str(ex)[:300]}"}
