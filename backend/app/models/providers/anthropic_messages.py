"""Anthropic Messages 协议适配器（基础版：非流式请求 + 单段伪流式）。"""
from __future__ import annotations

from typing import AsyncIterator

import httpx

from app.models.providers.base import ProtocolAdapter
from app.models.types import GenParams, Message, ProviderError, StreamEvent, ToolCall, Usage


class AnthropicAdapter(ProtocolAdapter):
    protocol = "anthropic_messages"

    def _headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            **self.extra_headers,
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    def _wire(self, messages: list[Message]) -> tuple[str, list[dict]]:
        system_parts: list[str] = []
        wire: list[dict] = []
        for m in messages:
            if m.role == "system":
                system_parts.append(m.content)
            elif m.role == "tool":
                wire.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.tool_call_id or "",
                                "content": m.content,
                            }
                        ],
                    }
                )
            elif m.role == "assistant":
                blocks: list[dict] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for call in m.tool_calls:
                    blocks.append(
                        {"type": "tool_use", "id": call.id, "name": call.name, "input": call.args}
                    )
                wire.append({"role": "assistant", "content": blocks or m.content})
            else:
                wire.append({"role": "user", "content": m.content})
        return "\n\n".join(system_parts), wire

    def _payload(self, messages: list[Message], tools: list[dict] | None,
                 params: GenParams) -> dict:
        system, wire = self._wire(messages)
        payload: dict = {
            "model": params.model,
            "messages": wire,
            "max_tokens": params.max_tokens,
            "temperature": params.temperature,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("parameters")
                    or {"type": "object", "properties": {}},
                }
                for t in tools
            ]
        return payload

    async def chat(self, messages: list[Message], tools: list[dict] | None,
                   params: GenParams) -> Message:
        url = self.base_url + "/messages"
        try:
            async with httpx.AsyncClient(timeout=float(self.timeout_s)) as client:
                resp = await client.post(url, json=self._payload(messages, tools, params),
                                         headers=self._headers())
        except httpx.HTTPError as exc:
            raise ProviderError(f"无法连接供应商：{exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(
                f"Anthropic 请求失败（{resp.status_code}）：{resp.text[:500]}", resp.status_code
            )
        data = resp.json()
        text = "".join(b.get("text", "") for b in data.get("content") or []
                       if b.get("type") == "text")
        calls = [
            ToolCall(id=b.get("id") or f"call_{idx}", name=b.get("name") or "",
                     args=b.get("input") or {})
            for idx, b in enumerate(data.get("content") or [])
            if b.get("type") == "tool_use"
        ]
        return Message(role="assistant", content=text, tool_calls=calls)

    async def stream(self, messages: list[Message], tools: list[dict] | None,
                     params: GenParams) -> AsyncIterator[StreamEvent]:
        message = await self.chat(messages, tools, params)
        if message.content:
            yield StreamEvent(kind="text_delta", text=message.content)
        for call in message.tool_calls:
            yield StreamEvent(kind="tool_call_delta", tool_name=call.name)
        yield StreamEvent(kind="done", message=message)


def usage_of(data: dict) -> Usage:
    usage = data.get("usage") or {}
    return Usage(input_tokens=usage.get("input_tokens") or 0,
                 output_tokens=usage.get("output_tokens") or 0,
                 cached_tokens=usage.get("cache_read_input_tokens") or 0)
