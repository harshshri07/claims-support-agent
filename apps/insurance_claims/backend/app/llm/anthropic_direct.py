"""Anthropic API adapter (LLM_PROVIDER=anthropic)."""
from __future__ import annotations

import json
from typing import Any

from .base import LLMError, LLMResponse, ToolCall


class AnthropicLLM:
    def __init__(self, api_key: str, model: str):
        import anthropic

        self.name = f"anthropic:{model}"
        self.model = model
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=3)
        self._err = anthropic.APIError

    @staticmethod
    def _convert(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []

        def push(role, blocks):
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": blocks})

        for m in messages:
            if m["role"] == "user":
                push("user", [{"type": "text", "text": m["content"] or "(no text)"}])
            elif m["role"] == "assistant":
                blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
                blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]}
                           for c in m.get("tool_calls", [])]
                push("assistant", blocks or [{"type": "text", "text": "..."}])
            elif m["role"] == "tool":
                push("user", [{"type": "tool_result", "tool_use_id": r["id"], "content": json.dumps(r["content"])}
                              for r in m["results"]])
        if out and out[0]["role"] != "user":
            out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(conversation start)"}]})
        return out

    def _create(self, kwargs: dict[str, Any]):
        # newer SDKs/models drop sampling params; retry without them instead of failing
        if getattr(self, "_no_temperature", False):
            kwargs.pop("temperature", None)
        try:
            return self.client.messages.create(**kwargs)
        except (TypeError, self._err) as e:
            if "temperature" not in str(e) or "temperature" not in kwargs:
                raise
            self._no_temperature = True
            kwargs.pop("temperature")
            return self.client.messages.create(**kwargs)

    def complete(self, system, messages, *, tools=None, force_tool=None, max_tokens=600, temperature=0.3) -> LLMResponse:
        kwargs: dict[str, Any] = dict(model=self.model, system=system, messages=self._convert(messages),
                                      max_tokens=max_tokens, temperature=temperature)
        if tools:
            kwargs["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.schema} for t in tools]
            kwargs["tool_choice"] = {"type": "tool", "name": force_tool} if force_tool else {"type": "auto"}
        try:
            r = self._create(kwargs)
        except self._err as e:
            raise LLMError(str(e)) from e
        text = "".join(b.text for b in r.content if b.type == "text").strip()
        calls = [ToolCall(b.id, b.name, b.input) for b in r.content if b.type == "tool_use"]
        return LLMResponse(text, calls, r.stop_reason or "", r.usage.model_dump() if r.usage else {})
