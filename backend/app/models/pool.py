"""Provider → Model → Binding 与并发闸门（模型接入配置设计 §2、§5）。

注意：SQLAlchemy 连接池只有 1 个连接（§5.1 红线），因此本模块内绝不嵌套
session_scope()，每次读取都在一个 session 内取完所需行再退出。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.db.base import session_scope
from app.db.models import AgentTemplate, ModelEntry, ProviderConfig
from app.models.crypto import decrypt_key
from app.models.providers import ADAPTERS
from app.models.providers.base import ProtocolAdapter
from app.models.types import GenParams, Message, ProviderError


@dataclass
class ProviderInfo:
    id: int
    name: str
    protocol: str
    base_url: str
    api_key: str | None = None
    extra_headers: dict = field(default_factory=dict)
    timeout_s: int = 60
    max_concurrency: int = 4
    enabled: bool = True


@dataclass
class ModelInfo:
    id: int
    provider_id: int
    model_name: str
    display_name: str
    context_window: int = 32768
    max_output_tokens: int = 4096
    cap_tools: bool = True
    cap_json: bool = True
    cap_vision: bool = False
    price_in: float = 0.0
    price_out: float = 0.0
    provider: ProviderInfo | None = None


def _provider_info(row: ProviderConfig) -> ProviderInfo:
    return ProviderInfo(
        id=row.id, name=row.name, protocol=row.protocol, base_url=row.base_url,
        api_key=decrypt_key(row.api_key_enc), extra_headers=dict(row.extra_headers or {}),
        timeout_s=row.timeout_s, max_concurrency=row.max_concurrency, enabled=row.enabled,
    )


class ModelPool:
    def __init__(self) -> None:
        self._semaphores: dict[int, asyncio.Semaphore] = {}
        self._guard = asyncio.Lock()

    # ---------- 读取 ----------
    def load_provider(self, provider_id: int) -> ProviderInfo:
        with session_scope() as s:
            p = s.get(ProviderConfig, provider_id)
            if not p:
                raise ProviderError(f"供应商 #{provider_id} 不存在")
            return _provider_info(p)

    def load_model(self, model_id: int) -> ModelInfo:
        with session_scope() as s:
            m = s.get(ModelEntry, model_id)
            if not m or not m.enabled:
                raise ProviderError(f"模型 #{model_id} 不存在或已禁用")
            p = s.get(ProviderConfig, m.provider_id)
            if not p or not p.enabled:
                raise ProviderError("该模型所属供应商已禁用")
            provider = _provider_info(p)
            return ModelInfo(
                id=m.id, provider_id=m.provider_id, model_name=m.model_name,
                display_name=m.display_name or m.model_name, context_window=m.context_window,
                max_output_tokens=m.max_output_tokens, cap_tools=m.cap_tools,
                cap_json=m.cap_json, cap_vision=m.cap_vision, price_in=m.price_in,
                price_out=m.price_out, provider=provider,
            )

    def adapter_for(self, model: ModelInfo) -> ProtocolAdapter:
        provider = model.provider or self.load_provider(model.provider_id)
        cls = ADAPTERS.get(provider.protocol)
        if not cls:
            raise ProviderError(f"不支持的协议：{provider.protocol}")
        return cls(
            base_url=provider.base_url, api_key=provider.api_key,
            headers=provider.extra_headers, timeout_s=provider.timeout_s,
        )

    # ---------- 绑定解析 ----------
    def pick_role_model(self, role_key: str) -> ModelInfo | None:
        candidates: list[int] = []
        with session_scope() as s:
            template = s.query(AgentTemplate).filter(AgentTemplate.role_key == role_key).first()
            if template and template.default_model_id:
                candidates.append(template.default_model_id)
            row = (
                s.query(ModelEntry)
                .join(ProviderConfig, ProviderConfig.id == ModelEntry.provider_id)
                .filter(ModelEntry.enabled.is_(True), ProviderConfig.enabled.is_(True))
                .order_by(ModelEntry.id.asc())
                .first()
            )
            if row:
                candidates.append(row.id)
        for model_id in candidates:
            try:
                return self.load_model(model_id)
            except ProviderError:
                continue
        return None

    def require_role_model(self, role_key: str) -> ModelInfo:
        model = self.pick_role_model(role_key)
        if model is None:
            raise ProviderError(
                "尚未配置可用模型：请先在「设置 → 供应商」添加供应商与模型，并测试连接"
            )
        return model

    # ---------- 并发 ----------
    async def slot(self, provider: ProviderInfo):
        async with self._guard:
            sem = self._semaphores.get(provider.id)
            if sem is None:
                sem = asyncio.Semaphore(max(1, provider.max_concurrency))
                self._semaphores[provider.id] = sem
        return sem

    # ---------- 测试连接 ----------
    async def test_connection(self, provider_id: int, model_id: int | None = None) -> dict:
        provider = self.load_provider(provider_id)
        if provider.protocol == "openai_chat":
            from app.models.providers.openai_chat import test_connection

            return await test_connection(provider.base_url, provider.api_key,
                                         provider.extra_headers, provider.timeout_s)
        if model_id is None:
            with session_scope() as s:
                row = (
                    s.query(ModelEntry)
                    .filter(ModelEntry.provider_id == provider_id, ModelEntry.enabled.is_(True))
                    .first()
                )
                model_id = row.id if row else None
        if model_id is None:
            raise ProviderError("请先为该供应商添加至少一个模型，再测试连接")
        model = self.load_model(model_id)
        adapter = self.adapter_for(model)
        reply = await adapter.chat(
            [Message(role="user", content="ping")], None,
            GenParams(model=model.model_name, max_tokens=8, temperature=0),
        )
        return {"ok": True, "message": f"连接成功：{reply.content[:80] or '(空回复)'}", "models": []}


pool = ModelPool()
