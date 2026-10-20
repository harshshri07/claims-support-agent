"""OpenAI, Azure OpenAI, Gemini (OpenAI endpoint) and any OpenAI-compatible server (Ollama, OpenRouter, Groq, vLLM).

Different models behind the same API accept different parameters. Instead of hard-coding a list that goes stale,
the adapter starts with the standard request and, when a model rejects something, adapts and remembers:
  * newer / reasoning models: max_tokens -> max_completion_tokens, no custom temperature
  * models that reject a named tool_choice: fall back to "required"
  * gpt-5 family: a small reasoning budget (otherwise replies are slow), relaxed or dropped if rejected
"""
from __future__ import annotations

import json
import re
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
        self._new_params = False       # max_completion_tokens instead of max_tokens, no temperature
        self._no_forced_tool = False   # named tool_choice rejected -> "required"
        # gpt-5 models reason by default, which is slow for chat; ask for the smallest budget
        self._effort: str | None = "minimal" if re.match(r"^gpt-5", model.lower()) else None

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
        r = None
        last_error = ""
        for _ in range(6):   # each failure teaches the adapter one thing about this model, so retries are bounded
            kwargs.pop("max_tokens", None)
            kwargs.pop("max_completion_tokens", None)
            kwargs.pop("temperature", None)
            kwargs.pop("reasoning_effort", None)
            if self._new_params:
                kwargs["max_completion_tokens"] = max(max_tokens * 4, 2000)   # reasoning tokens count against it
            else:
                kwargs["max_tokens"], kwargs["temperature"] = max_tokens, temperature
            if self._effort:
                kwargs["reasoning_effort"] = self._effort
            if tools:
                if force_tool and not self._no_forced_tool:
                    kwargs["tool_choice"] = {"type": "function", "function": {"name": force_tool}}
                else:
                    kwargs["tool_choice"] = "required" if force_tool else "auto"
            try:
                r = self.client.chat.completions.create(**kwargs)
                break
            except self._err as e:
                msg = last_error = str(e).lower()
                if self._effort and "reasoning" in msg:
                    self._effort = {"minimal": "low", "low": None}.get(self._effort)
                    continue
                if not self._new_params and ("max_completion_tokens" in msg or "temperature" in msg
                                             or "max_tokens" in msg):
                    self._new_params = True
                    continue
                if tools and force_tool and not self._no_forced_tool and "tool_choice" in msg:
                    self._no_forced_tool = True
                    continue
                raise LLMError(str(e)) from e
        if r is None:
            raise LLMError(f"request failed: {last_error[:200]}")
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
