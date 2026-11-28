"""自定义角色：新建、删除；内置角色只能改不能删。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.db.base import init_db, session_scope
from app.db.models import AgentTemplate
from app.engine.templates import seed_defaults
from app.main import app


def _client(monkeypatch) -> TestClient:
    init_db()
    seed_defaults()
    return TestClient(app)


def test_create_and_delete_custom_role(monkeypatch) -> None:
    with _client(monkeypatch) as client:
        created = client.post("/api/agent-templates", json={
            "role_key": "data_expert", "name": "数据专家",
            "system_prompt": "你关注数据模型与查询成本。",
            "meta": {"description": "数据建模与查询成本", "tags": ["数据"],
                     "meeting_roles": ["discussion", "review"]},
        })
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["builtin"] is False and body["meta"]["tags"] == ["数据"]
        duplicate = client.post("/api/agent-templates", json={
            "role_key": "data_expert", "name": "重复", "system_prompt": "x"})
        assert duplicate.status_code == 400
        patched = client.patch(f"/api/agent-templates/{body['id']}",
                               json={"name": "数据与算法", "meta": {"tags": ["数据", "算法"]}})
        assert patched.json()["name"] == "数据与算法"
        assert patched.json()["meta"]["tags"] == ["数据", "算法"]
        assert client.delete(f"/api/agent-templates/{body['id']}").status_code == 200
        with session_scope() as s:
            assert s.query(AgentTemplate).filter(
                AgentTemplate.role_key == "data_expert").count() == 0


def test_null_name_and_bad_model_id_are_rejected_without_500(monkeypatch) -> None:
    with _client(monkeypatch) as client:
        rows = client.get("/api/agent-templates").json()["items"]
        chair = next(r for r in rows if r["role_key"] == "review_chair")
        null_name = client.patch(f"/api/agent-templates/{chair['id']}", json={"name": None})
        bad_model = client.patch(f"/api/agent-templates/{chair['id']}",
                                 json={"default_model_id": 0})
        created = client.post("/api/agent-templates", json={
            "role_key": "low_model_role", "name": "x", "system_prompt": "x",
            "default_model_id": 0})
    assert null_name.status_code == 200 and null_name.json()["name"] == chair["name"]
    assert bad_model.status_code == 422
    assert created.status_code == 422


def test_builtin_role_cannot_be_deleted(monkeypatch) -> None:
    with _client(monkeypatch) as client:
        rows = client.get("/api/agent-templates").json()["items"]
        chair = next(r for r in rows if r["role_key"] == "review_chair")
        assert chair["builtin"] is True
        assert client.delete(f"/api/agent-templates/{chair['id']}").status_code == 400


def test_bad_role_key_is_rejected(monkeypatch) -> None:
    with _client(monkeypatch) as client:
        response = client.post("/api/agent-templates", json={
            "role_key": "Bad Key", "name": "x", "system_prompt": "x"})
        assert response.status_code == 422
