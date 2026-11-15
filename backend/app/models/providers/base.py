"""协议适配器统一接口（模型接入配置设计 §4）。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from app.models.types import GenParams, Message, StreamEvent, ToolCall, Usage


class ProtocolAdapter(ABC):
    protocol: str = "base"

    def __init__(self, *, base_url: str, api_key: str | None, headers: dict | None = None,
                 timeout_s: int = 60):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.extra_headers = headers or {}
        self.timeout_s = timeout_s

    @abstractmethod
    async def chat(self, messages: list[Message], tools: list[dict] | None,
                   params: GenParams) -> Message: ...

    @abstractmethod
    def stream(self, messages: list[Message], tools: list[dict] | None,
               params: GenParams) -> AsyncIterator[StreamEvent]: ...

    def count_tokens_estimate(self, messages: list[Message]) -> int:
        from app.models.budget import estimate_tokens

        return sum(estimate_tokens(m.content or "") for m in messages)

    # 子类共用的工具 schema 转换钩子
    def tool_schemas(self, tools: list[dict] | None) -> list[dict] | None:
        return tools

    @staticmethod
    def _json_args(raw: str | dict | None) -> dict:
        import json

        if isinstance(raw, dict):
            return raw
        if not raw:
            return {}
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {"value": value}
        except json.JSONDecodeError:
            return {"_raw": raw}


__all__ = ["ProtocolAdapter", "GenParams", "Message", "StreamEvent", "ToolCall", "Usage"]
