"""模型发现：预设自动建模型、远程模型导入、协议不支持时的报错。"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.pool import pool
from app.models.providers.openai_chat import list_models
from app.models.types import ProviderError


def test_preset_seeds_models() -> None:
    with TestClient(app) as client:
        created = client.post("/api/providers", json={
            "name": "预设供应商", "protocol": "openai_chat",
            "base_url": "https://api.deepseek.com/v1", "api_key": "sk-preset-123456",
            "preset_key": "deepseek",
        })
        assert created.status_code in (200, 201), created.text
        provider = created.json()
        assert provider["models_created"] == 2

        models = client.get("/api/models", params={"provider_id": provider["id"]}).json()["items"]
        names = {m["model_name"] for m in models}
        assert {"deepseek-chat", "deepseek-reasoner"} <= names
        chat = next(m for m in models if m["model_name"] == "deepseek-chat")
        assert chat["context_window"] == 65536


def test_import_models_skips_existing() -> None:
    with TestClient(app) as client:
        provider = client.post("/api/providers", json={
            "name": "导入供应商", "protocol": "openai_chat",
            "base_url": "http://127.0.0.1:9/v1", "api_key": "sk-import-123456",
        }).json()
        first = client.post(f"/api/providers/{provider['id']}/models/import",
                            json={"models": ["alpha-model", "beta-model", "alpha-model"]})
        assert first.status_code in (200, 201), first.text
        assert first.json()["created"] and len(first.json()["created"]) == 2
        assert first.json()["skipped"] == []

        second = client.post(f"/api/providers/{provider['id']}/models/import",
                             json={"models": ["alpha-model", "gamma-model"]})
        assert second.status_code in (200, 201), second.text
        assert second.json()["skipped"] == ["alpha-model"]
        assert [row["model_name"] for row in second.json()["created"]] == ["gamma-model"]


def test_remote_models_unsupported_protocol() -> None:
    with TestClient(app) as client:
        provider = client.post("/api/providers", json={
            "name": "Anthropic 供应商", "protocol": "anthropic_messages",
            "base_url": "https://api.anthropic.com/v1", "api_key": "sk-ant-123456",
        }).json()
        resp = client.get(f"/api/providers/{provider['id']}/models")
        assert resp.status_code == 400
        assert "不支持" in resp.json()["detail"]


def test_remote_models_returns_list(monkeypatch) -> None:
    async def fake_list_models(provider_id: int) -> list[dict]:
        return [{"id": "model-a", "owned_by": "acme", "created": 0},
                {"id": "model-b", "owned_by": "", "created": 0}]

    monkeypatch.setattr(pool, "list_models", fake_list_models)
    with TestClient(app) as client:
        provider = client.post("/api/providers", json={
            "name": "远程列表供应商", "protocol": "openai_chat",
            "base_url": "http://127.0.0.1:9/v1", "api_key": "sk-remote-123456",
        }).json()
        resp = client.get(f"/api/providers/{provider['id']}/models")
        assert resp.status_code == 200, resp.text
        assert resp.json()["count"] == 2
        assert resp.json()["items"][0]["id"] == "model-a"


def test_list_models_falls_back_to_v1(monkeypatch) -> None:
    """Base URL 没带 /v1 时，先试 /models，再试 /v1/models。"""
    calls: list[str] = []

    async def fake_get(self, url, **kwargs):
        text = str(url)
        calls.append(text)
        if text == "https://x.test/models":
            return httpx.Response(404, request=httpx.Request("GET", text))
        return httpx.Response(200, json={"data": [{"id": "m1", "owned_by": "acme"}]},
                              request=httpx.Request("GET", text))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    items = asyncio.run(list_models("https://x.test", "k", None))
    assert [row["id"] for row in items] == ["m1"]
    assert calls == ["https://x.test/models", "https://x.test/v1/models"]


def test_list_models_all_404_is_actionable(monkeypatch) -> None:
    async def fake_get(self, url, **kwargs):
        text = str(url)
        return httpx.Response(404, request=httpx.Request("GET", text))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    with pytest.raises(ProviderError) as excinfo:
        asyncio.run(list_models("https://x.test/v1", "k", None))
    message = str(excinfo.value)
    assert "未提供 /models 接口" in message
    assert "手动填写模型名" in message
    assert "https://x.test/v1/models" in message


def test_list_models_accepts_models_key(monkeypatch) -> None:
    async def fake_get(self, url, **kwargs):
        text = str(url)
        return httpx.Response(200, json={"models": [{"id": "alt-1"}]},
                              request=httpx.Request("GET", text))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    items = asyncio.run(list_models("https://x.test/v1", "k", None))
    assert items == [{"id": "alt-1", "owned_by": "", "created": 0}]
