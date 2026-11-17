"""评审闭环：计划提交追加评审/验收门、阻塞回退、轮次用尽、评审 API 与指标。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.artifacts.store import write_artifact
from app.db.base import session_scope
from app.db.models import Gate, Node, NodeAttempt, PlanTask, Run, Task, Workflow, new_run_id
from app.engine import orchestrator as orch_mod
from app.engine.orchestrator import Orchestrator
from app.main import app

BACKEND_DIR = Path(__file__).resolve().parents[1]
DESIGN = (BACKEND_DIR / "tests" / "fixtures" / "designs" / "chat-product.md").read_text(encoding="utf-8")
TITLES = [
    "Feedback intake form", "Conversation model", "Keyword search", "Category and priority",
    "Permission and rate limits", "Unit and regression tests", "Monitoring panel", "README and demo",
]


def _seed_plan(client: TestClient) -> str:
    from app.engine.planner import extract_sections

    created = client.post("/api/plans", json={
        "product_name": "Review loop demo", "source_type": "text", "design_text": DESIGN,
    })
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan"]["id"]
    sections = extract_sections(DESIGN)
    with session_scope() as session:
        for index in range(8):
            session.add(PlanTask(
                plan_id=plan_id, task_uid=f"T{index + 1}", title=TITLES[index],
                description="Implement the task end to end, including error paths and tests.",
                deps_json=[f"T{index}"] if index else [],
                design_section=sections[index]["title"],
                acceptance_json=["unit tests pass", "no new dependency"],
                role_key="dev", effort="M", priority="P1", order_index=index,
            ))
    validated = client.post(f"/api/plans/{plan_id}/validate")
    assert validated.status_code == 200, validated.text
    return plan_id


def _run_with_nodes(run_id: str, specs: list[dict]) -> None:
    with session_scope() as s:
        s.query(Node).filter(Node.run_id == run_id).delete()
        for index, spec in enumerate(specs):
            s.add(Node(
                run_id=run_id, node_key=spec["key"], type=spec.get("type", "AGENT"),
                name=spec.get("name", spec["key"]), phase=spec.get("phase", "execution"),
                status=spec.get("status", "pending"), deps_json=spec.get("deps", []),
                wave=spec.get("wave", 1), max_attempts=spec.get("max_attempts", 1),
                config_json=spec.get("config", {}), order_index=index,
            ))


def _new_run(status: str = "running") -> str:
    from app.engine.templates import seed_defaults

    seed_defaults()
    with session_scope() as s:
        wf = s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").one()
        run_id = new_run_id()
        s.add(Run(id=run_id, workflow_id=wf.id, status=status, idea_text="review loop"))
        s.add(Task(run_id=run_id, task_uid="T1", title="Task one", deps_json=[],
                   design_section="", test_cases_json=[], wave=1, status="done",
                   assignee_json={"role": "dev"}))
        s.add(Task(run_id=run_id, task_uid="T2", title="Task two", deps_json=["T1"],
                   design_section="", test_cases_json=[], wave=2, status="done",
                   assignee_json={"role": "dev"}))
    return run_id


def _node(run_id: str, key: str) -> Node:
    with session_scope() as s:
        return s.query(Node).filter(Node.run_id == run_id, Node.node_key == key).one()


def test_plan_commit_appends_review_and_accept_gate(monkeypatch) -> None:
    from app.api import plans as plans_api

    monkeypatch.setattr(plans_api.orchestrator, "start", lambda run_id: None)
    with TestClient(app) as client:
        plan_id = _seed_plan(client)
        response = client.post(f"/api/plans/{plan_id}/commit", json={"mode": "run"})
        assert response.status_code == 200, response.text
        run_id = response.json()["run_id"]
        detail = client.get(f"/api/runs/{run_id}").json()
        nodes = {node["node_key"]: node for node in detail["nodes"]}
        assert nodes["review"]["type"] == "REVIEW"
        assert nodes["review"]["deps"] == ["verify"]
        assert nodes["review"]["wave"] > nodes["verify"]["wave"]
        assert nodes["review"]["config"]["max_rounds"] == 2
        assert nodes["gate_accept"]["type"] == "HUMAN_GATE"
        assert nodes["gate_accept"]["deps"] == ["review"]
        assert nodes["gate_accept"]["config"]["artifact"] == "review"

        # 显式关闭：只保留 dev + verify。
        plan2 = _seed_plan(client)
        plain = client.post(
            f"/api/plans/{plan2}/commit",
            json={"mode": "run", "review": False, "accept_gate": False},
        )
        assert plain.status_code == 200, plain.text
        plain_nodes = {
            n["node_key"] for n in client.get(f"/api/runs/{plain.json()['run_id']}").json()["nodes"]
        }
        assert "review" not in plain_nodes and "gate_accept" not in plain_nodes
        assert {"dev_T1", "verify"} <= plain_nodes


def test_review_block_resets_targets_then_passes(monkeypatch) -> None:
    run_id = _new_run()
    _run_with_nodes(run_id, [
        {"key": "dev_T1", "wave": 1, "config": {"task_uid": "T1"}},
        {"key": "dev_T2", "wave": 2, "deps": ["dev_T1"], "config": {"task_uid": "T2"}},
        {"key": "verify", "type": "VERIFY", "wave": 3, "deps": ["dev_T1", "dev_T2"]},
        {"key": "review", "type": "REVIEW", "wave": 4, "deps": ["verify"],
         "config": {"round": 1, "max_rounds": 2}},
        {"key": "gate_accept", "type": "HUMAN_GATE", "wave": 5, "deps": ["review"],
         "config": {"artifact": "review"}},
    ])
    calls = {"review": 0}

    async def fake_execute(ctx):
        if ctx.type == "REVIEW":
            calls["review"] += 1
            if calls["review"] == 1:
                return {"summary": "接口错误处理缺失", "verdict": "block", "issues": 1,
                        "blocking": 1, "targets": ["T1"]}
            return {"summary": "通过", "verdict": "pass", "issues": 0, "blocking": 0,
                    "targets": []}
        return {"summary": "ok"}

    monkeypatch.setattr(orch_mod, "execute_node", fake_execute)
    asyncio.run(Orchestrator()._loop(run_id))

    review = _node(run_id, "review")
    assert review.config_json["round"] == 2
    assert _node(run_id, "dev_T1").status == "done"
    assert _node(run_id, "verify").status == "done"
    assert _node(run_id, "gate_accept").status == "waiting_human"
    with session_scope() as s:
        assert s.get(Run, run_id).status == "waiting_human"
        gate = s.query(Gate).filter(Gate.run_id == run_id, Gate.status == "open").one()
        gate_node_id = gate.node_id
    assert gate_node_id == _node(run_id, "gate_accept").id
    from app.engine.events import load_events

    events = load_events(run_id)
    blocked = [e for e in events if e["type"] == "review_blocked"]
    assert len(blocked) == 1
    assert blocked[0]["payload"]["targets"] == ["T1"]
    assert blocked[0]["payload"]["escalated"] is False


def test_review_rounds_exhausted_escalates(monkeypatch) -> None:
    run_id = _new_run()
    _run_with_nodes(run_id, [
        {"key": "dev_T1", "deps": [], "config": {"task_uid": "T1"}},
        {"key": "verify", "type": "VERIFY", "deps": ["dev_T1"]},
        {"key": "review", "type": "REVIEW", "deps": ["verify"],
         "config": {"round": 1, "max_rounds": 1}},
    ])

    async def fake_execute(ctx):
        if ctx.type == "REVIEW":
            return {"summary": "仍有阻塞", "verdict": "block", "issues": 2,
                    "blocking": 2, "targets": ["T1"]}
        return {"summary": "ok"}

    monkeypatch.setattr(orch_mod, "execute_node", fake_execute)
    asyncio.run(Orchestrator()._loop(run_id))

    review = _node(run_id, "review")
    assert review.status == "waiting_human"
    with session_scope() as s:
        gate = s.query(Gate).filter(Gate.run_id == run_id, Gate.status == "open").one()
        assert gate.node_id == review.id
    from app.engine.events import load_events

    blocked = [e for e in load_events(run_id) if e["type"] == "review_blocked"]
    assert blocked and blocked[-1]["payload"]["escalated"] is True


def test_review_api_metrics_and_manual_rework(monkeypatch) -> None:
    run_id = _new_run(status="waiting_human")
    now = datetime.now(timezone.utc)
    report = {
        "verdict": "block", "summary": "两个阻塞问题", "blocking": 2,
        "targets": ["T1"], "round": 1, "max_rounds": 2, "files": ["app.py"],
        "reviewed_at": now.isoformat(),
        "issue_count": 2,
        "issues": [
            {"severity": "blocker", "task_uid": "T1", "file": "app.py",
             "message": "未处理空输入", "suggestion": "增加校验"},
            {"severity": "minor", "task_uid": "T2", "file": "README.md",
             "message": "说明缺失", "suggestion": "补充用法"},
        ],
    }
    with session_scope() as s:
        review = Node(run_id=run_id, node_key="review", type="REVIEW", name="代码评审",
                      phase="execution", status="waiting_human", deps_json=["verify"],
                      wave=4, max_attempts=2, order_index=3,
                      config_json={"round": 2, "max_rounds": 2, "review": report,
                                   "review_history": [{"round": 1, "verdict": "block"},
                                                      {"round": 2, "verdict": "block"}]})
        dev = Node(run_id=run_id, node_key="dev_T1", type="AGENT", name="Dev T1",
                   phase="execution", status="done", deps_json=[], wave=1,
                   max_attempts=3, order_index=0)
        verify = Node(run_id=run_id, node_key="verify", type="VERIFY", name="验证",
                      phase="execution", status="done", deps_json=["dev_T1"], wave=2,
                      max_attempts=1, order_index=1)
        s.query(Node).filter(Node.run_id == run_id).delete()
        s.add_all([dev, verify, review])
        s.flush()
        s.add_all([
            NodeAttempt(node_id=review.id, attempt_no=1, status="done",
                        started_at=now - timedelta(seconds=9),
                        finished_at=now - timedelta(seconds=4)),
            NodeAttempt(node_id=dev.id, attempt_no=1, status="done",
                        started_at=now - timedelta(seconds=30),
                        finished_at=now - timedelta(seconds=20)),
        ])
    write_artifact(run_id, "review", "# 评审报告\n\n两个阻塞问题。", created_by="review")

    with TestClient(app) as client:
        review_resp = client.get(f"/api/runs/{run_id}/review")
        assert review_resp.status_code == 200, review_resp.text
        data = review_resp.json()
        assert data["available"] is True
        assert data["verdict"] == "block"
        assert data["blocking"] == 2
        assert data["can_rework"] is True
        assert "评审报告" in data["body"]

        metrics = client.get(f"/api/runs/{run_id}/metrics").json()
        review_metrics = metrics["review"]
        assert review_metrics["total"] == 1
        assert review_metrics["verdict"] == "block"
        assert review_metrics["round"] == 2
        assert review_metrics["rework_count"] == 1
        assert review_metrics["first_pass"] is False
        assert review_metrics["severities"]["blocker"] == 1
        assert review_metrics["avg_review_ms"] == 5000

        monkeypatch.setattr(orch_mod.orchestrator, "kick", lambda run_id: None)
        rework = client.post(f"/api/runs/{run_id}/review/rework",
                             json={"task_uids": ["T1"], "feedback": "修复空输入"})
        assert rework.status_code == 200, rework.text
        assert rework.json()["targets"] == ["T1"]

    assert _node(run_id, "dev_T1").status == "pending"
    assert _node(run_id, "verify").status == "pending"
    assert _node(run_id, "review").status == "pending"
    assert _node(run_id, "review").config_json["round"] == 3


def test_run_review_normalizes_json_and_writes_artifact(monkeypatch) -> None:
    """REVIEW 执行器本身：JSON 规范化、无效 uid 丢弃、阻塞判定、工件与 config。"""
    import json
    from types import SimpleNamespace

    from app.engine import runners

    run_id = _new_run()
    with session_scope() as s:
        node = Node(
            run_id=run_id, node_key="review", type="REVIEW", name="代码评审",
            phase="execution", status="running", deps_json=["verify"], wave=3,
            max_attempts=2, order_index=0,
            config_json={"round": 1, "max_rounds": 2, "task_uids": ["T1", "T2"]},
        )
        s.add(node)
        s.flush()
        node_id = node.id

    model = SimpleNamespace(model_name="mock-review", display_name="Mock Review",
                            max_output_tokens=1024)
    monkeypatch.setattr(runners.pool, "pick_role_model", lambda role: model)

    async def fake_agent(**_kwargs):
        return SimpleNamespace(content=json.dumps({
            "verdict": "pass",
            "summary": "发现一个阻塞问题",
            "issues": [
                {"severity": "blocker", "task_uid": "T2", "file": "app.py",
                 "message": "缺少空值保护", "suggestion": "补校验"},
                {"severity": "minor", "task_uid": "T99", "file": "",
                 "message": "未知任务的问题"},
                {"severity": "huge", "task_uid": "T1", "file": "x.py", "message": "级别归一"},
            ],
        }, ensure_ascii=False))

    monkeypatch.setattr(runners, "run_agent", fake_agent)
    ctx = runners.NodeContext(
        run_id=run_id, node_id=node_id, node_key="review", type="REVIEW",
        name="代码评审", phase="execution", config={"round": 1, "max_rounds": 2},
        assignee={"role": "review"}, attempt_id=0, attempt_no=1, idea="review demo",
    )
    result = asyncio.run(runners.execute_node(ctx))

    assert result["verdict"] == "block"  # 模型说 pass，但存在 blocker → block
    assert result["blocking"] == 1
    assert result["targets"] == ["T2"]
    assert result["issues"] == 3
    node = _node(run_id, "review")
    report = node.config_json["review"]
    issues = report["issues"]
    assert [item["severity"] for item in issues] == ["blocker", "minor", "minor"]
    assert issues[1]["task_uid"] == ""  # 不存在的 uid 被丢弃
    assert report["verdict"] == "block"
    assert report["round"] == 1
    assert node.config_json["review_history"][-1]["verdict"] == "block"
    from app.artifacts.store import read_artifact

    assert "代码评审报告" in (read_artifact(run_id, "review") or "")
