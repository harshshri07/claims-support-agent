"""AWS Bedrock adapter (Converse API) — default provider."""
from __future__ import annotations

import json
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .base import LLMError, LLMResponse, ToolCall, ToolSpec


class BedrockLLM:
    def __init__(self, model_id: str, region: str, access_key: str = "", secret_key: str = ""):
        self.name = f"bedrock:{model_id}"
        self.model_id = model_id
        creds = {"aws_access_key_id": access_key, "aws_secret_access_key": secret_key} if access_key else {}
        self.client = boto3.client(  # without explicit keys: default AWS chain (env, profile, AWS_BEARER_TOKEN_BEDROCK)
            "bedrock-runtime",
            region_name=region,
            **creds,
            config=Config(retries={"max_attempts": 4, "mode": "adaptive"}, read_timeout=60, connect_timeout=10),
        )

    @staticmethod
    def _to_bedrock(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []

        def push(role: str, blocks: list[dict[str, Any]]):
            if out and out[-1]["role"] == role:  # Converse requires strict alternation
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": blocks})

        for m in messages:
            if m["role"] == "user":
                push("user", [{"text": m["content"] or "(no text)"}])
            elif m["role"] == "assistant":
                blocks: list[dict[str, Any]] = []
                if m.get("content"):
                    blocks.append({"text": m["content"]})
                for c in m.get("tool_calls", []):
                    blocks.append({"toolUse": {"toolUseId": c["id"], "name": c["name"], "input": c["input"]}})
                push("assistant", blocks or [{"text": "..."}])
            elif m["role"] == "tool":
                push("user", [
                    {"toolResult": {"toolUseId": r["id"], "content": [{"json": r["content"]}]
                                    if isinstance(r["content"], dict) else [{"text": json.dumps(r["content"])}]}}
                    for r in m["results"]
                ])
        if out and out[0]["role"] != "user":
            out.insert(0, {"role": "user", "content": [{"text": "(conversation start)"}]})
        return out

    def complete(self, system, messages, *, tools=None, force_tool=None, max_tokens=600, temperature=0.3) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "modelId": self.model_id,
            "system": [{"text": system}],
            "messages": self._to_bedrock(messages),
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
        }
        if tools:
            kwargs["toolConfig"] = {
                "tools": [{"toolSpec": {"name": t.name, "description": t.description,
                                        "inputSchema": {"json": t.schema}}} for t in tools],
                "toolChoice": {"tool": {"name": force_tool}} if force_tool else {"auto": {}},
            }
        try:
            r = self.client.converse(**kwargs)
        except (ClientError, BotoCoreError) as e:
            raise LLMError(str(e)) from e
        text, calls = [], []
        for block in r["output"]["message"]["content"]:
            if "text" in block:
                text.append(block["text"])
            elif "toolUse" in block:
                tu = block["toolUse"]
                calls.append(ToolCall(tu["toolUseId"], tu["name"], tu.get("input") or {}))
        return LLMResponse("".join(text).strip(), calls, r.get("stopReason", ""), r.get("usage", {}))
