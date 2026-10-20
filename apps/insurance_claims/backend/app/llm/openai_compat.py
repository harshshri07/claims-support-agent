"""OpenAI, Azure OpenAI, Gemini (OpenAI endpoint) and any OpenAI-compatible server (Ollama, OpenRouter, Groq, vLLM)."""
from __future__ import annotations

import json
from typing import Any

from .base import LLMError, LLMResponse, ToolCall


class OpenAILLM:
    def __init__(self, api_key: str, model: str, base_url: str = "", *, azure_endpoint: str = "",
                 api_version: str = "", label: str = "openai"):
        import openai

        self.name = f"{label}:{model}"
        self.model = model
        if azure_endpoint:
            self.client = openai.AzureOpenAI(api_key=api_key, azure_endpoint=azure_endpoint,
                                             api_version=api_version or "2024-10-21", max_retries=2, timeout=60)
        else:
            self.client = openai.OpenAI(api_key=api_key, base_url=base_url or None, max_retries=2, timeout=60)
        self._err = openai.OpenAIError
        self._new_params = False      # reasoning models want max_completion_tokens and no temperature
        self._no_forced_tool = False  # some compatible servers reject a named tool_choice

    @staticmethod
    def _convert(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.get("content") or None}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                          "function": {"name": c["name"], "arguments": json.dumps(c["input"])}}
                                         for c in m["tool_calls"]]
                out.append(msg)
            elif m["role"] == "tool":
                out += [{"role": "tool", "tool_call_id": r["id"], "content": json.dumps(r["content"])}
                        for r in m["results"]]
        return out

    def complete(self, system, messages, *, tools=None, force_tool=None, max_tokens=600, temperature=0.3) -> LLMResponse:
        kwargs: dict[str, Any] = dict(model=self.model, messages=self._convert(system, messages))
        if tools:
            kwargs["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                                 "parameters": t.schema}} for t in tools]
            if force_tool and not self._no_forced_tool:
                kwargs["tool_choice"] = {"type": "function", "function": {"name": force_tool}}
            elif force_tool:
                kwargs["tool_choice"] = "required"
            else:
                kwargs["tool_choice"] = "auto"
        r = None
        for _ in range(3):
            if self._new_params:
                kwargs.pop("max_tokens", None)
                kwargs.pop("temperature", None)
                kwargs["max_completion_tokens"] = max(max_tokens * 4, 2000)
            else:
                kwargs["max_tokens"], kwargs["temperature"] = max_tokens, temperature
            try:
                r = self.client.chat.completions.create(**kwargs)
                break
            except self._err as e:
                msg = str(e).lower()
                if not self._new_params and ("max_completion_tokens" in msg or "temperature" in msg):
                    self._new_params = True
                    continue
                if tools and force_tool and not self._no_forced_tool and "tool_choice" in msg:
                    self._no_forced_tool = True
                    kwargs["tool_choice"] = "required"
                    continue
                raise LLMError(str(e)) from e
        if r is None:
            raise LLMError("request failed")
        msg = r.choices[0].message
        calls = []
        for c in msg.tool_calls or []:
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(c.id, c.function.name, args))
        return LLMResponse((msg.content or "").strip(), calls, r.choices[0].finish_reason or "",
                           r.usage.model_dump() if r.usage else {})
