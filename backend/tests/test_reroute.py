"""定点回流：目标节点定点重开且不级联，发起节点轮次加一，轮次用尽转人工。"""
from __future__ import annotations

import asyncio

from app.db.base import init_db, session_scope
from app.db.models import Gate, Node, Run, Workflow
from app.engine.events import load_events
from app.engine.orchestrator import Orchestrator
from app.engine.templates import seed_defaults

_counter = iter(range(1, 1000))


def _graph(round_no: int) -> str:
    """三个节点：target ← child ← self。self 发现需要 target 返工。"""
    init_db()
    seed_defaults()
    index = next(_counter)
    with session_scope() as s:
        workflow_id = s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").one().id
        run_id = f"reroute-run-{index}"
        s.add(Run(id=run_id, workflow_id=workflow_id, status="running"))
        s.flush()
        specs = {
            "target": dict(name="讨论会议", type="MEETING", status="done", deps_json=[]),
            "child": dict(name="下游", type="HUMAN_GATE", status="done", deps_json=["target"]),
            "self": dict(name="评审会议", type="MEETING", status="running", deps_json=["child"],
                         config_json={"round": round_no, "max_rounds": 2}),
        }
        for key, spec in specs.items():
            s.add(Node(run_id=run_id, node_key=key, **spec))
        s.flush()
        return run_id


def _self_node(orchestrator: Orchestrator, run_id: str) -> dict:
    _, nodes = orchestrator._load(run_id)
    return next(n for n in nodes if n["node_key"] == "self")


def _reroute(run_id: str, orchestrator: Orchestrator, **instruction) -> None:
    asyncio.run(orchestrator._handle_reroute(
        run_id, _self_node(orchestrator, run_id),
        {"targets": ["target"], "feedback": "D3 需要复议", **instruction}))


def test_reroute_resets_targets_without_cascade() -> None:
    run_id = _graph(round_no=1)
    orchestrator = Orchestrator()
    _reroute(run_id, orchestrator, self_feedback=None)
    _, nodes = orchestrator._load(run_id)
    by_key = {n["node_key"]: n for n in nodes}
    assert by_key["target"]["status"] == "pending"
    assert by_key["target"]["config"]["feedback"] == "D3 需要复议"
    assert by_key["target"]["config"]["feedback_kind"] == "reroute"
    assert by_key["target"]["config"]["feedback_seq"] == 1
    assert by_key["child"]["status"] == "done"                # 不级联重置下游
    assert by_key["self"]["status"] == "pending"
    assert by_key["self"]["config"]["round"] == 2
    events = load_events(run_id)
    assert [e["type"] for e in events] == ["node_rerouted"]
    assert events[0]["payload"]["targets"] == ["target"]


def test_self_feedback_replaces_stale_feedback() -> None:
    """发起回流的节点上留着一条已经处理过的驳回意见：新的自我提示把它替换掉。"""
    run_id = _graph(round_no=1)
    orchestrator = Orchestrator()
    with session_scope() as s:
        node = s.query(Node).filter(Node.run_id == run_id, Node.node_key == "self").one()
        node.config_json = {**node.config_json, "feedback": "用户的驳回意见",
                            "feedback_kind": "reject", "feedback_seq": 3}
    _reroute(run_id, orchestrator, self_feedback="下一轮复核 D3 的处理结果")
    node = _self_node(orchestrator, run_id)
    assert node["config"]["feedback"] == "下一轮复核 D3 的处理结果"
    assert node["config"]["feedback_kind"] == "reroute_self"
    assert node["config"]["feedback_seq"] == 4


def test_no_self_feedback_clears_stale_feedback() -> None:
    run_id = _graph(round_no=1)
    orchestrator = Orchestrator()
    with session_scope() as s:
        node = s.query(Node).filter(Node.run_id == run_id, Node.node_key == "self").one()
        node.config_json = {**node.config_json, "feedback": "用户的驳回意见",
                            "feedback_kind": "reject", "feedback_seq": 3}
    _reroute(run_id, orchestrator, self_feedback=None)
    node = _self_node(orchestrator, run_id)
    assert "feedback" not in node["config"]
    assert node["config"]["feedback_seq"] == 3                # 序号保留，下一次写入接着涨


def test_reroute_escalates_when_rounds_are_used_up() -> None:
    run_id = _graph(round_no=2)
    orchestrator = Orchestrator()
    _reroute(run_id, orchestrator)
    node = _self_node(orchestrator, run_id)
    assert node["status"] == "waiting_human"
    assert node["config"]["escalation"] is True
    with session_scope() as s:
        assert s.query(Gate).filter(Gate.run_id == run_id, Gate.status == "open").count() == 1
    events = load_events(run_id)
    assert [e["type"] for e in events] == ["node_rerouted", "gate_requested"]
    assert events[0]["payload"]["escalated"] is True


def test_extra_round_allows_one_more_round() -> None:
    run_id = _graph(round_no=2)
    orchestrator = Orchestrator()
    _reroute(run_id, orchestrator, feedback="再查一次成本", extra_round=True)
    node = _self_node(orchestrator, run_id)
    assert node["status"] == "pending"
    assert (node["config"]["round"], node["config"]["max_rounds"]) == (3, 3)
    assert load_events(run_id)[0]["payload"]["escalated"] is False
