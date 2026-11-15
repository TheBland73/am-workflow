"""API 冒烟：健康检查、预设、供应商/模型/角色模板 CRUD。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_health_and_presets() -> None:
    with TestClient(app) as client:
        assert client.get("/api/health").json()["ok"] is True
        presets = client.get("/api/presets").json()["presets"]
        assert any(p["key"] == "deepseek" for p in presets)


def test_provider_model_and_template_flow() -> None:
    with TestClient(app) as client:
        created = client.post("/api/providers", json={
            "name": "测试供应商", "protocol": "openai_chat",
            "base_url": "http://127.0.0.1:9/v1", "api_key": "sk-test-1234567890",
            "max_concurrency": 2,
        })
        assert created.status_code in (200, 201), created.text
        provider = created.json()
        assert provider["api_key_masked"].endswith("7890")

        model = client.post("/api/models", json={
            "provider_id": provider["id"], "model_name": "mock-fast",
            "display_name": "Mock Fast", "context_window": 32000,
            "max_output_tokens": 4096, "cap_tools": True, "cap_json": True,
        })
        assert model.status_code in (200, 201), model.text
        model_id = model.json()["id"]

        templates = client.get("/api/agent-templates").json()["items"]
        assert len(templates) >= 8
        patched = client.patch(f"/api/agent-templates/{templates[0]['id']}",
                              json={"default_model_id": model_id})
        assert patched.status_code == 200, patched.text
        assert patched.json()["default_model_id"] == model_id

        assert client.get("/api/runs").status_code == 200
        assert client.delete(f"/api/providers/{provider['id']}").status_code in (200, 204)
