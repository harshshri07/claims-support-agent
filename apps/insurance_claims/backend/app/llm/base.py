"""Provider-neutral LLM interface.

The harness only needs three things from a model:
  * plain completion            -> natural-language rendering
  * forced tool call            -> structured extraction (JSON that matches a schema)
  * auto tool use (agent loop)  -> grounded PROCESS_CASE reasoning over read-only tools

Neutral message format used across the code base:
  {"role": "user", "content": "text"}
  {"role": "assistant", "content": "text", "tool_calls": [{"id", "name", "input"}]}
  {"role": "tool", "results": [{"id", "content": <json-serialisable>}]}
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    name: str

    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[ToolSpec] | None = None,
        force_tool: str | None = None,
        max_tokens: int = 600,
        temperature: float = 0.3,
    ) -> LLMResponse: ...


def structured(llm: LLM, system: str, messages: list[dict[str, Any]], tool: ToolSpec, **kw) -> dict[str, Any]:
    """Force the model to call `tool` and return its input as the structured result."""
    resp = llm.complete(system, messages, tools=[tool], force_tool=tool.name, temperature=0.0, **kw)
    for call in resp.tool_calls:
        if call.name == tool.name:
            return call.input
    # weaker / local models sometimes answer with the JSON as text instead of a tool call
    start, end = resp.text.find("{"), resp.text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(resp.text[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise LLMError(f"model did not call {tool.name}")
