"""门禁语义：驳回重置目标与其下游、通过推进、失败转人工确认后重试。"""
from __future__ import annotations

from app.db.base import init_db, session_scope
from app.db.models import Node, Run, Workflow, new_run_id
from app.engine.gates import decide_gate, request_gate
from app.engine.templates import instantiate_nodes, seed_defaults, workflow_definition


def _make_run() -> str:
    init_db()
    seed_defaults()
    with session_scope() as s:
        wf = s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").first()
        run = Run(id=new_run_id(), workflow_id=wf.id, status="running", idea_text="演示想法")
        s.add(run)
        s.flush()
        run_id = run.id
    instantiate_nodes(run_id, workflow_definition("sdlc_v1"))
    return run_id


def _node_id(run_id: str, key: str) -> int:
    with session_scope() as s:
        return s.query(Node).filter(Node.run_id == run_id, Node.node_key == key).one().id


def _status(run_id: str, key: str) -> str:
    with session_scope() as s:
        return s.query(Node).filter(Node.run_id == run_id, Node.node_key == key).one().status


def _finish(run_id: str) -> None:
    with session_scope() as s:
        run = s.get(Run, run_id)
        run.status = "finished"


def test_reject_resets_target_and_downstream() -> None:
    run_id = _make_run()
    with session_scope() as s:
        for node in s.query(Node).filter(Node.run_id == run_id).all():
            node.status = "done"
    gate = request_gate(run_id, _node_id(run_id, "gate_requirement"))
    result = decide_gate(gate_id=gate["id"], action="reject", feedback="补充导出周报的验收标准",
                         target_node="requirement")
    assert result["status"] == "rejected"
    assert _status(run_id, "requirement") == "pending"
    assert _status(run_id, "design") == "pending"
    assert _status(run_id, "gate_requirement") == "pending"
    _finish(run_id)


def test_approve_marks_gate_done_and_run_running() -> None:
    run_id = _make_run()
    gate = request_gate(run_id, _node_id(run_id, "gate_proposal"))
    result = decide_gate(gate_id=gate["id"], action="approve", decided_by="tester")
    assert result["status"] == "approved"
    assert _status(run_id, "gate_proposal") == "done"
    with session_scope() as s:
        assert s.get(Run, run_id).status == "running"
    _finish(run_id)


def test_escalation_approve_retries_node_instead_of_completing() -> None:
    run_id = _make_run()
    node_id = _node_id(run_id, "requirement")
    with session_scope() as s:
        node = s.get(Node, node_id)
        node.status = "waiting_human"
        node.config_json = {"feedback": "模型调用失败", "escalation": True}
    gate = request_gate(run_id, node_id, feedback="模型调用失败")
    decide_gate(gate_id=gate["id"], action="approve", decided_by="tester")
    assert _status(run_id, "requirement") == "pending"
    _finish(run_id)


def test_reject_requires_feedback_and_target() -> None:
    run_id = _make_run()
    gate = request_gate(run_id, _node_id(run_id, "gate_requirement"))
    try:
        decide_gate(gate_id=gate["id"], action="reject", target_node="requirement")
        raise AssertionError("expected GateError")
    except Exception as exc:  # GateError
        assert "反馈" in str(exc)
    _finish(run_id)
