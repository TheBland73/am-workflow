"""OpenAI Chat Completions 兼容适配器（W1 主协议；DeepSeek/GLM/豆包/Kimi/百炼/SiliconFlow/OpenRouter 均走此协议）。"""
from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncIterator

import httpx

from app.conversation.telemetry import parse_cached_tokens
from app.models.providers.base import ProtocolAdapter
from app.models.types import (
    GenParams,
    Message,
    ProviderError,
    StreamEvent,
    ToolCall,
    Usage,
)

_RETRY_STATUS = {429, 500, 502, 503, 504}


def _wire_messages(messages: list[Message]) -> list[dict]:
    wire: list[dict] = []
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            wire.append(
                {
                    "role": "assistant",
                    "content": m.content or None,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {
                                "name": c.name,
                                "arguments": json.dumps(c.args, ensure_ascii=False),
                            },
                        }
                        for c in m.tool_calls
                    ],
                }
            )
        elif m.role == "tool":
            wire.append(
                {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
            )
        else:
            wire.append({"role": m.role, "content": m.content})
    return wire


def _tool_wire(tools: list[dict] | None) -> list[dict] | None:
    if not tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("parameters") or {"type": "object", "properties": {}},
            },
        }
        for t in tools
    ]


def _friendly_error(status: int, body: bytes) -> ProviderError:
    text = body.decode("utf-8", errors="replace")[:600]
    try:
        data = json.loads(text)
        text = (data.get("error") or {}).get("message") or data.get("message") or text
    except (json.JSONDecodeError, AttributeError):
        pass
    if status == 401:
        prefix = "密钥无效或未授权（401）"
    elif status == 403:
        prefix = "无权限访问该模型（403）"
    elif status == 404:
        prefix = "接口地址或模型名不存在（404）"
    elif status == 429:
        prefix = "触发限流或余额不足（429）"
    elif status >= 500:
        prefix = f"供应商服务异常（{status}）"
    else:
        prefix = f"请求失败（{status}）"
    return ProviderError(f"{prefix}：{text}", status)


class OpenAIChatAdapter(ProtocolAdapter):
    protocol = "openai_chat"

    def _url(self, path: str) -> str:
        base = self.base_url
        if not base:
            raise ProviderError("未配置 base_url")
        return base + path

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _payload(self, messages: list[Message], tools: list[dict] | None,
                 params: GenParams, stream: bool) -> dict:
        payload: dict = {
            "model": params.model,
            "messages": _wire_messages(messages),
            "temperature": params.temperature,
            "max_tokens": params.max_tokens,
            "stream": stream,
        }
        wire_tools = _tool_wire(tools)
        if wire_tools:
            payload["tools"] = wire_tools
        if params.stop:
            payload["stop"] = params.stop
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15.0, read=float(self.timeout_s), write=30.0, pool=15.0)
        )

    async def chat(self, messages: list[Message], tools: list[dict] | None,
                   params: GenParams) -> Message:
        payload = self._payload(messages, tools, params, stream=False)
        last_error: ProviderError | None = None
        for attempt in range(2):
            try:
                async with self._client() as client:
                    resp = await client.post(self._url("/chat/completions"), json=payload,
                                             headers=self._headers())
                if resp.status_code >= 400:
                    raise _friendly_error(resp.status_code, resp.content)
                return _parse_message(resp.json())
            except (httpx.HTTPError, ProviderError) as exc:
                err = exc if isinstance(exc, ProviderError) else ProviderError(
                    f"无法连接供应商：{exc}", None
                )
                last_error = err
                retryable = (
                    isinstance(exc, httpx.HTTPError)
                    or (err.status_code in _RETRY_STATUS)
                )
                if attempt == 0 and retryable:
                    await asyncio.sleep(1.0)
                    continue
                raise err
        raise last_error or ProviderError("请求失败")

    async def stream(self, messages: list[Message], tools: list[dict] | None,
                     params: GenParams) -> AsyncIterator[StreamEvent]:
        payload = self._payload(messages, tools, params, stream=True)
        started = False
        last_error: ProviderError | None = None
        for attempt in range(2):
            try:
                async for event in self._stream_once(payload):
                    started = True
                    yield event
                return
            except (httpx.HTTPError, ProviderError) as exc:
                err = exc if isinstance(exc, ProviderError) else ProviderError(
                    f"无法连接供应商：{exc}", None
                )
                last_error = err
                retryable = isinstance(exc, httpx.HTTPError) or err.status_code in _RETRY_STATUS
                if attempt == 0 and retryable and not started:
                    await asyncio.sleep(1.0)
                    continue
                raise err
        raise last_error or ProviderError("请求失败")

    async def _stream_once(self, payload: dict) -> AsyncIterator[StreamEvent]:
        text_parts: list[str] = []
        calls: dict[int, dict] = {}
        usage = Usage()
        async with self._client() as client:
            async with client.stream("POST", self._url("/chat/completions"), json=payload,
                                     headers=self._headers()) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise _friendly_error(resp.status_code, body)
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        usage.input_tokens = chunk["usage"].get("prompt_tokens") or 0
                        usage.output_tokens = chunk["usage"].get("completion_tokens") or 0
                        usage.cached_tokens = parse_cached_tokens(chunk["usage"])
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            text_parts.append(piece)
                            yield StreamEvent(kind="text_delta", text=piece)
                        for tc in delta.get("tool_calls") or []:
                            idx = tc.get("index", 0)
                            slot = calls.setdefault(idx, {"id": "", "name": "", "args": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                                yield StreamEvent(kind="tool_call_delta", tool_name=fn["name"])
                            if fn.get("arguments"):
                                slot["args"] += fn["arguments"]
        tool_calls = [
            ToolCall(id=slot["id"] or f"call_{idx}", name=slot["name"],
                     args=self._json_args(slot["args"]))
            for idx, slot in sorted(calls.items())
            if slot["name"]
        ]
        message = Message(role="assistant", content="".join(text_parts), tool_calls=tool_calls)
        if usage.total:
            yield StreamEvent(kind="usage", usage=usage)
        yield StreamEvent(kind="done", message=message, usage=usage)


def _parse_message(data: dict) -> Message:
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    calls = []
    for idx, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        calls.append(
            ToolCall(id=tc.get("id") or f"call_{idx}", name=fn.get("name") or "",
                     args=OpenAIChatAdapter._json_args(fn.get("arguments")))
        )
    return Message(role="assistant", content=msg.get("content") or "", tool_calls=calls)


def _model_list_urls(base_url: str) -> list[str]:
    """候选列表地址：优先 {base}/models；base 未带 /v1 时再试 {base}/v1/models。"""
    base = (base_url or "").rstrip("/")
    if not base:
        return []
    urls = [f"{base}/models"]
    if not base.endswith("/v1"):
        urls.append(f"{base}/v1/models")
    return urls


async def list_models(base_url: str, api_key: str | None, headers: dict | None,
                      timeout_s: int = 30) -> list[dict]:
    """GET /models，返回 [{"id","owned_by","created"}]；供应商不支持时抛 ProviderError。"""
    adapter = OpenAIChatAdapter(base_url=base_url, api_key=api_key, headers=headers,
                                timeout_s=timeout_s)
    candidates = _model_list_urls(base_url)
    if not candidates:
        raise ProviderError("供应商未配置 Base URL，无法拉取模型列表")
    resp = None
    async with adapter._client() as client:
        for url in candidates:
            try:
                resp = await client.get(url, headers=adapter._headers())
            except httpx.HTTPError as exc:
                raise ProviderError(f"无法连接供应商：{exc}") from exc
            if resp.status_code in (404, 405):
                continue
            break
    if resp is None or resp.status_code in (404, 405):
        tried = "、".join(candidates)
        raise ProviderError(f"该供应商未提供 /models 接口（已尝试 {tried}），请手动填写模型名")
    if resp.status_code >= 400:
        err = _friendly_error(resp.status_code, resp.content)
        raise ProviderError(f"{err}（{resp.url}）", resp.status_code)
    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        raise ProviderError("模型列表不是合法 JSON") from exc
    rows = data.get("data") or data.get("models") or data.get("result") or []
    if isinstance(rows, dict):
        rows = rows.get("data") or rows.get("models") or []
    items: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id")
        if not model_id:
            continue
        items.append({
            "id": str(model_id),
            "owned_by": str(row.get("owned_by") or ""),
            "created": int(row.get("created") or 0),
        })
    return items[:500]


async def test_connection(base_url: str, api_key: str | None, headers: dict | None,
                          timeout_s: int = 30, model_name: str | None = None) -> dict:
    """连接自检：带 model_name 时真发一次 8 token 对话；否则 GET /models。"""
    adapter = OpenAIChatAdapter(base_url=base_url, api_key=api_key, headers=headers,
                                timeout_s=timeout_s)
    started = time.perf_counter()
    if model_name:
        try:
            reply = await adapter.chat(
                [Message(role="user", content="ping")], None,
                GenParams(model=model_name, max_tokens=32, temperature=0),
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"无法连接供应商：{exc}") from exc
        elapsed = int((time.perf_counter() - started) * 1000)
        text = (reply.content or "").strip().replace("\n", " ")[:80]
        if text:
            message = f"{model_name} 对话正常（{elapsed} ms）：{text}"
        else:
            message = (f"{model_name} 连通（{elapsed} ms），但未返回正文："
                       "思考型模型可能把 32 token 上限耗在推理上，正式调用不受影响")
        return {"ok": True, "message": message,
                "latency_ms": elapsed, "reply": text, "models": []}
    try:
        models = await list_models(base_url, api_key, headers, timeout_s)
    except ProviderError as exc:
        if "未提供 /models" in str(exc):
            elapsed = int((time.perf_counter() - started) * 1000)
            return {"ok": True,
                    "message": f"连接成功（未提供模型列表接口，请手动填写模型名，{elapsed} ms）",
                    "latency_ms": elapsed, "models": []}
        raise
    elapsed = int((time.perf_counter() - started) * 1000)
    return {"ok": True, "message": f"连接成功，发现 {len(models)} 个模型（{elapsed} ms）",
            "latency_ms": elapsed, "models": models}
