"""运行级配置、背景材料、带反馈的修订。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.artifacts.store import parse_front_matter, read_artifact, write_artifact
from app.config import settings
from app.conversation.policy import policy_for_run
from app.db.base import init_db, session_scope
from app.db.models import Node, Run, Workflow
from app.engine import runners
from app.engine.materials import read_materials
from app.engine.orchestrator import orchestrator
from app.engine.params import validate_rosters
from app.engine.templates import (
    instantiate_nodes, role_library, role_template, seed_defaults, workflow_definition,
)
from app.main import app

IDEA = "给小团队做一个反馈收集工具"
_counter = iter(range(1, 1000))
REVISED = (
    "# 需求规格\n\n## 1. 范围\n\n覆盖从收集到导出的最小闭环，不含实时协作。\n\n"
    "### R1 收集反馈\n\nWHEN 用户提交反馈 THE SYSTEM SHALL 保存并显示确认。\n\n"
    "验收标准：\n- Given 已登录，When 提交，Then 保存成功并提示。\n\n"
    "修改说明：按反馈补充了确认提示。\n\n"
    "### R2 导出\n\nWHEN 用户点击导出 THE SYSTEM SHALL 生成文件。\n\n"
    "验收标准：\n- Given 有数据，When 导出，Then 下载成功。\n\n"
    "### R3 失败提示\n\nWHEN 保存失败 THE SYSTEM SHALL 显示可读的错误。\n\n"
    "验收标准：\n- Given 服务不可用，When 提交，Then 提示稍后重试。\n"
)


def _client(monkeypatch) -> TestClient:
    init_db()
    seed_defaults()
    monkeypatch.setattr(orchestrator, "start", lambda run_id: None)     # 不真正开始调度
    return TestClient(app)


def _create(client: TestClient, **extra) -> dict:
    response = client.post("/api/runs", json={"idea": IDEA, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def _node(run_id: str, key: str) -> Node:
    with session_scope() as s:
        return s.query(Node).filter(Node.run_id == run_id, Node.node_key == key).one()


def test_run_config_reaches_meeting_nodes(monkeypatch) -> None:
    with _client(monkeypatch) as client:
        snapshot = _create(client, config={
            "params": {"discussion": {"rounds_max": 6},
                       "context": {"compact_at": 0.6, "compact_to": 0.3}}})
    run_id = snapshot["run"]["id"]
    assert snapshot["run"]["config"]["params"]["discussion"]["rounds_max"] == 6
    assert _node(run_id, "discuss").config_json["params"]["rounds_max"] == 6
    assert _node(run_id, "review").config_json["max_rounds"] == 3
    assert policy_for_run(run_id).compact_at == 0.6


def test_bad_run_config_is_rejected_with_reasons(monkeypatch) -> None:
    with _client(monkeypatch) as client:
        bad_level = client.post("/api/runs", json={
            "idea": IDEA, "config": {"automation": {"level": "L9"}}})
        unknown_role = client.post("/api/runs", json={
            "idea": IDEA, "config": {"rosters": {"participants": {"role_keys": ["no_such_role"]}}}})
        bad_count = client.post("/api/runs", json={
            "idea": IDEA, "config": {"rosters": {"participants": {"count": 0}}}})
        out_of_range = client.post("/api/runs", json={
            "idea": IDEA, "config": {"params": {"discussion": {"rounds_max": 0}}}})
    assert bad_level.status_code == 400 and "未知的自动化等级" in bad_level.json()["detail"]
    assert unknown_role.status_code == 400
    assert "角色模板不存在：no_such_role" in unknown_role.json()["detail"]
    assert bad_count.status_code == 400 and "固定人数" in bad_count.json()["detail"]
    assert out_of_range.status_code == 400 and "讨论轮次上限" in out_of_range.json()["detail"]


def test_materials_are_saved_listed_and_bounded(monkeypatch) -> None:
    with _client(monkeypatch) as client:
        run_id = _create(client, materials=[{"name": "需求片段", "content": "要能导出周报"}])["run"]["id"]
        added = client.post(f"/api/runs/{run_id}/materials", json={"content": "预算有限"})
        listed = client.get(f"/api/runs/{run_id}/materials")
        fetched = client.get(f"/api/runs/{run_id}/materials/需求片段")
        monkeypatch.setattr(settings, "materials_max_chars", 30)
        oversized = client.post(f"/api/runs/{run_id}/materials", json={"content": "字" * 40})
        bad_name = client.post(f"/api/runs/{run_id}/materials",
                               json={"name": "a/b", "content": "x"})
    assert added.status_code == 201 and added.json()["name"] == "材料2"
    assert [item["name"] for item in listed.json()["items"]] == ["需求片段", "材料2"]
    assert fetched.json()["content"] == "要能导出周报"
    assert oversized.status_code == 400 and "总量" in oversized.json()["detail"]
    assert bad_name.status_code == 400
    text, truncated = read_materials(run_id, budget=5)
    assert truncated is True and "仅提供了部分材料" in text


def test_run_config_can_be_changed_before_or_between_checkpoints(monkeypatch) -> None:
    with _client(monkeypatch) as client:
        run_id = _create(client)["run"]["id"]
        patched = client.patch(f"/api/runs/{run_id}/config", json={
            "params": {"discussion": {"rounds_max": 4}}, "automation": {"level": "L1"}})
        with session_scope() as s:
            s.get(Run, run_id).status = "running"
        refused = client.patch(f"/api/runs/{run_id}/config", json={"automation": {"level": "L1"}})
        shown = client.get(f"/api/runs/{run_id}/config")
    assert patched.status_code == 200, patched.text
    assert _node(run_id, "discuss").config_json["params"]["rounds_max"] == 4
    assert refused.status_code == 400
    assert shown.json()["config"]["automation"] == {"level": "L1"}


def test_template_endpoint_exposes_params_and_levels(monkeypatch) -> None:
    with _client(monkeypatch) as client:
        body = client.get("/api/templates/sdlc_v1").json()
        missing = client.get("/api/templates/nope")
    assert body["params"]["discussion"]["rounds_max"]["max"] == 99
    assert body["automation_levels"]["L2"]["consensus"] == "program"
    assert set(body["rubrics"]) == {"proposal", "review", "requirement", "artifact"}
    assert missing.status_code == 404


def test_role_library_helpers() -> None:
    init_db()
    seed_defaults()
    assert {"facilitator", "architect", "judge"} <= {row["role_key"] for row in role_library()}
    assert role_template("architect")["meta"]["meeting_roles"] == ["discussion", "review"]
    assert role_template("nope") is None


def test_roster_structure_is_validated() -> None:
    assert validate_rosters({"participants": {"role_keys": ["architect"]},
                             "reviewers": {"count": 2}}) == []
    assert validate_rosters({"participants": {"role_keys": ["architect"], "count": 2}})
    assert validate_rosters({"reviewers": {"count": 100}})
    assert validate_rosters({"others": {}})


def _doc_node(run_id: str) -> runners.NodeContext:
    with session_scope() as s:
        node = s.query(Node).filter(Node.run_id == run_id, Node.node_key == "requirement").one()
        return runners.NodeContext(
            run_id=run_id, node_id=node.id, node_key="requirement", type="AGENT", name=node.name,
            phase=node.phase, config=dict(node.config_json), assignee=dict(node.assignee_json),
            attempt_id=0, attempt_no=1, idea=IDEA)


def _install_doc_agent(monkeypatch, seen: list[str]) -> None:
    model = SimpleNamespace(model_name="mock-fast", display_name="Mock Fast",
                            context_window=32768, max_output_tokens=4096)
    monkeypatch.setattr(runners.pool, "pick_role_model", lambda role: model)

    async def fake_agent(**kwargs):
        seen.append(kwargs["user_prompt"])
        return SimpleNamespace(content=REVISED)

    monkeypatch.setattr(runners, "run_agent", fake_agent)


def _template_run() -> str:
    init_db()
    seed_defaults()
    run_id = f"revise-run-{next(_counter)}"
    with session_scope() as s:
        workflow_id = s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").one().id
        s.add(Run(id=run_id, workflow_id=workflow_id, status="running", idea_text=IDEA))
        s.flush()
    instantiate_nodes(run_id, workflow_definition("sdlc_v1"))
    write_artifact(run_id, "idea", "# 想法\n\n" + IDEA, created_by="user", inputs=[], status="approved")
    return run_id


def test_doc_agent_revises_previous_version_when_feedback_is_given(monkeypatch) -> None:
    plain_run, revise_run = _template_run(), _template_run()
    for run_id in (plain_run, revise_run):
        write_artifact(run_id, "requirement", "# 需求规格\n\n旧的一版。", created_by="test", inputs=[])
    seen: list[str] = []
    _install_doc_agent(monkeypatch, seen)

    asyncio.run(runners.execute_node(_doc_node(plain_run)))                 # 不带反馈：从头写
    ctx = _doc_node(revise_run)
    ctx.feedback = "补充失败时的提示"
    result = asyncio.run(runners.execute_node(ctx))                         # 带反馈：修订

    assert "上一版" not in seen[0]
    assert "上一版" in seen[1] and "旧的一版" in seen[1] and "修订" in seen[1]
    assert parse_front_matter(read_artifact(revise_run, "requirement"))[1].strip() == REVISED.strip()
    assert result["artifact"]["version"] == 2
