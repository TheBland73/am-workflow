"""三方讨论：append 版本链、蒸馏引用原文与方案确认门未决项联动。

复用 scripts/mock_provider.py 的 canned 与路由逻辑驱动真实引擎路径
（角色模板 → _run_doc_agent → 工件版本链），与 e2e 演示保持同一数据源。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.artifacts.store import parse_front_matter, read_artifact, write_artifact
from app.db.base import init_db, session_scope
from app.db.models import Node, Run, Workflow, new_run_id
from app.engine import runners
from app.engine.gates import request_gate
from app.engine.templates import instantiate_nodes, seed_defaults, workflow_definition
from scripts.mock_provider import _pick

DISCUSSION_NODES = ("discuss_pro", "discuss_con", "discuss_response", "discuss_mod")


def _make_run() -> str:
    init_db()
    seed_defaults()
    with session_scope() as s:
        wf = s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").one()
        run_id = new_run_id()
        s.add(Run(id=run_id, workflow_id=wf.id, status="running", idea_text="三方讨论测试想法"))
        s.flush()
    instantiate_nodes(run_id, workflow_definition("sdlc_v1"))
    write_artifact(run_id, "idea", "# 想法\n\n做一个面向小团队的客户反馈收集工具。",
                   created_by="idea", inputs=["idea"])
    return run_id


def _node_id(run_id: str, key: str) -> int:
    with session_scope() as s:
        return s.query(Node).filter(Node.run_id == run_id, Node.node_key == key).one().id


def _run_discussion_nodes(run_id: str, monkeypatch) -> None:
    model = SimpleNamespace(model_name="mock-fast", display_name="Mock Fast",
                            context_window=32768, max_output_tokens=4096)
    monkeypatch.setattr(runners.pool, "pick_role_model", lambda role: model)

    async def fake_agent(**kwargs):
        messages = [
            {"role": "system", "content": kwargs["system_prompt"]},
            {"role": "user", "content": kwargs["user_prompt"]},
        ]
        text, _call = _pick(messages, None)
        return SimpleNamespace(content=text)

    monkeypatch.setattr(runners, "run_agent", fake_agent)
    with session_scope() as s:
        specs = {
            n.node_key: {"id": n.id, "name": n.name, "phase": n.phase,
                         "config": dict(n.config_json or {}),
                         "assignee": dict(n.assignee_json or {})}
            for n in s.query(Node).filter(Node.run_id == run_id).all()
        }
    for key in DISCUSSION_NODES:
        spec = specs[key]
        ctx = runners.NodeContext(
            run_id=run_id, node_id=spec["id"], node_key=key, type="AGENT",
            name=spec["name"], phase=spec["phase"], config=spec["config"],
            assignee=spec["assignee"], attempt_id=0, attempt_no=1,
            idea="三方讨论测试想法",
        )
        result = asyncio.run(runners.execute_node(ctx))
        assert result["artifact"]["kind"] == "discussion"


def test_discussion_append_chain_reaches_v4(monkeypatch) -> None:
    run_id = _make_run()
    _run_discussion_nodes(run_id, monkeypatch)
    meta, body = parse_front_matter(read_artifact(run_id, "discussion"))
    assert meta["version"] == 4
    h2 = [ln.strip() for ln in body.splitlines() if ln.startswith("## ")]
    for title in ("## 主张", "## 质疑", "## 回应", "## 共识蒸馏"):
        assert title in h2, h2


def test_discussion_mod_quotes_original_text(monkeypatch) -> None:
    run_id = _make_run()
    _run_discussion_nodes(run_id, monkeypatch)
    _meta, body = parse_front_matter(read_artifact(run_id, "discussion"))
    quotes = [ln for ln in body.splitlines() if ln.strip().startswith('> "')]
    assert len(quotes) >= 2


def test_gate_proposal_description_includes_pending_questions(monkeypatch) -> None:
    run_id = _make_run()
    _run_discussion_nodes(run_id, monkeypatch)
    gate = request_gate(run_id, _node_id(run_id, "gate_proposal"))
    assert "讨论未决项" in gate["description"]
    assert "- [ ] 人工门的具体位置" in gate["description"]
