"""Gemini generateContent 协议适配器（基础版：非流式请求 + 单段伪流式）。"""
from __future__ import annotations

from typing import AsyncIterator

import httpx

from app.models.providers.base import ProtocolAdapter
from app.models.types import GenParams, Message, ProviderError, StreamEvent, ToolCall


class GeminiAdapter(ProtocolAdapter):
    protocol = "gemini_generate"

    def _url(self, model: str) -> str:
        base = self.base_url.rstrip("/")
        if not base.endswith("/v1beta") and not base.endswith("/v1"):
            base += "/v1beta"
        return f"{base}/models/{model}:generateContent"

    def _wire(self, messages: list[Message]) -> tuple[dict | None, list[dict]]:
        system_parts: list[str] = []
        contents: list[dict] = []
        for m in messages:
            if m.role == "system":
                system_parts.append(m.content)
            elif m.role == "tool":
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {"functionResponse": {"name": m.name or "tool",
                                                  "response": {"result": m.content}}}
                        ],
                    }
                )
            elif m.role == "assistant":
                parts: list[dict] = []
                if m.content:
                    parts.append({"text": m.content})
                for call in m.tool_calls:
                    parts.append({"functionCall": {"name": call.name, "args": call.args}})
                contents.append({"role": "model", "parts": parts or [{"text": ""}]})
            else:
                contents.append({"role": "user", "parts": [{"text": m.content}]})
        system = {"parts": [{"text": "\n\n".join(system_parts)}]} if system_parts else None
        return system, contents

    def _payload(self, messages: list[Message], tools: list[dict] | None,
                 params: GenParams) -> dict:
        system, contents = self._wire(messages)
        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": params.temperature,
                "maxOutputTokens": params.max_tokens,
            },
        }
        if system:
            payload["systemInstruction"] = system
        if tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t["name"],
                            "description": t.get("description", ""),
                            "parameters": t.get("parameters")
                            or {"type": "object", "properties": {}},
                        }
                        for t in tools
                    ]
                }
            ]
        return payload

    async def chat(self, messages: list[Message], tools: list[dict] | None,
                   params: GenParams) -> Message:
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            headers["x-goog-api-key"] = self.api_key
        try:
            async with httpx.AsyncClient(timeout=float(self.timeout_s)) as client:
                resp = await client.post(self._url(params.model),
                                         json=self._payload(messages, tools, params),
                                         headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderError(f"无法连接供应商：{exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(
                f"Gemini 请求失败（{resp.status_code}）：{resp.text[:500]}", resp.status_code
            )
        data = resp.json()
        candidate = (data.get("candidates") or [{}])[0]
        text = ""
        calls: list[ToolCall] = []
        for idx, part in enumerate((candidate.get("content") or {}).get("parts") or []):
            if part.get("text"):
                text += part["text"]
            if part.get("functionCall"):
                fc = part["functionCall"]
                calls.append(ToolCall(id=f"call_{idx}", name=fc.get("name") or "",
                                      args=fc.get("args") or {}))
        return Message(role="assistant", content=text, tool_calls=calls)

    async def stream(self, messages: list[Message], tools: list[dict] | None,
                     params: GenParams) -> AsyncIterator[StreamEvent]:
        message = await self.chat(messages, tools, params)
        if message.content:
            yield StreamEvent(kind="text_delta", text=message.content)
        for call in message.tool_calls:
            yield StreamEvent(kind="tool_call_delta", tool_name=call.name)
        yield StreamEvent(kind="done", message=message)
