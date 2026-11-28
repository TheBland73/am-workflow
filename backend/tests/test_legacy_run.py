"""旧模板建立的运行在当前代码下仍可查看。

升级不做迁移：模板原地替换后，已有运行保留历史查看（详设 §4.1）。
fixtures/sdlc_workflow.before-meetings.json 是会议式流程之前的模板原文。这里照升级前的做法
写一条运行——节点由旧模板实例化，停在"进行中"，一扇旧式人工门开着——再把运行页面会用到的
读接口逐个调一遍，并确认启动时的恢复扫描遇到旧运行不报错。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.db.base import init_db, session_scope
from app.db.models import Gate, Node, Run, Workflow, new_run_id
from app.engine.orchestrator import Orchestrator, orchestrator
from app.engine.templates import instantiate_nodes, seed_defaults
from app.main import app

LEGACY = json.loads((Path(__file__).parent / "fixtures" / "sdlc_workflow.before-meetings.json")
                    .read_text(encoding="utf-8"))
READ_PATHS = ("review", "metrics", "events/history", "config", "materials", "artifacts", "meetings")


def _legacy_run() -> str:
    """照升级前的做法建一条运行：旧模板实例化节点，停在"进行中"，一扇旧式人工门开着。"""
    with session_scope() as s:
        wf = s.query(Workflow).filter(Workflow.template_key == LEGACY["key"]).one()
        run_id = new_run_id()
        s.add(Run(id=run_id, workflow_id=wf.id, status="running", idea_text="升级前建立的一条运行"))
    instantiate_nodes(run_id, LEGACY)
    with session_scope() as s:
        nodes = s.query(Node).filter(Node.run_id == run_id).order_by(Node.order_index.asc()).all()
        assert not any(node.type == "MEETING" for node in nodes)      # 确实是旧模板的节点
        nodes[0].status = "done"
        nodes[1].status = "running"
        gate_node = next(node for node in nodes if node.type == "HUMAN_GATE")
        gate_node.status = "waiting_human"
        s.add(Gate(run_id=run_id, node_id=gate_node.id, status="open"))
    return run_id


def test_run_created_before_the_meeting_flow_stays_viewable(monkeypatch) -> None:
    init_db()
    seed_defaults()
    started: list = []
    monkeypatch.setattr(orchestrator, "start", started.append)
    with TestClient(app) as client:
        run_id = _legacy_run()

        listed = client.get("/api/runs")
        assert listed.status_code == 200
        assert any(row["id"] == run_id for row in listed.json()["items"])
        detail = client.get(f"/api/runs/{run_id}")
        assert detail.status_code == 200
        assert run_id in json.dumps(detail.json(), ensure_ascii=False)
        for path in READ_PATHS:
            response = client.get(f"/api/runs/{run_id}/{path}")
            assert response.status_code == 200, f"{path}: {response.text[:300]}"
        with session_scope() as s:
            node_id = s.query(Node.id).filter(Node.run_id == run_id).order_by(
                Node.order_index.asc()).first()[0]
        assert client.get(f"/api/nodes/{node_id}").status_code == 200

        # 启动时的恢复扫描遇到旧运行不报错，扫描之后旧运行照旧可读
        client.portal.call(Orchestrator.recover, orchestrator)
        assert client.get(f"/api/runs/{run_id}").status_code == 200
        assert client.get(f"/api/runs/{run_id}/metrics").status_code == 200
