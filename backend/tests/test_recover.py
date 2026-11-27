"""进程重启后的恢复流程 Orchestrator.recover()。

测试夹具把编排器实例上的 recover 换成了空操作（各用例共用一个库，启动应用时不把别的
用例留下的运行拉起来）；这里直接调用类上的原方法。重新拉起换成记录调用，不真的调度。
恢复流程会扫到库里别的用例留下的未结束运行，所以断言只针对本用例自己建的运行。
"""
from __future__ import annotations

import asyncio

import pytest

from app.db.base import init_db, session_scope
from app.db.models import Gate, Node, NodeAttempt, Run, Workflow, new_run_id
from app.engine.events import load_events
from app.engine.gates import get_gate, open_gate_for_run, request_gate
from app.engine.orchestrator import Orchestrator, orchestrator
from app.engine.templates import seed_defaults


def _run(status: str, **nodes) -> tuple[str, dict[str, int]]:
    """一个运行和它的几个节点。节点参数写状态，或 {"status": …, "config": …}。"""
    init_db()
    seed_defaults()
    run_id = new_run_id()
    ids: dict[str, int] = {}
    with session_scope() as s:
        workflow_id = s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").one().id
        s.add(Run(id=run_id, workflow_id=workflow_id, status=status, config_json={}))
        s.flush()
        for order, (key, spec) in enumerate(nodes.items()):
            spec = {"status": spec} if isinstance(spec, str) else spec
            node = Node(node_key=key, type="AGENT", name=key, run_id=run_id,
                        status=spec["status"], order_index=order,
                        config_json=spec.get("config") or {})
            s.add(node)
            s.flush()
            ids[key] = node.id
    return run_id, ids


def _attempt(node_id: int, status: str = "running") -> int:
    with session_scope() as s:
        row = NodeAttempt(node_id=node_id, attempt_no=1, status=status)
        s.add(row)
        s.flush()
        return row.id


def _node_statuses(run_id: str) -> dict[str, str]:
    with session_scope() as s:
        rows = s.query(Node).filter(Node.run_id == run_id).all()
        return {row.node_key: row.status for row in rows}


def _gates(run_id: str) -> list[tuple[int, int, str]]:
    with session_scope() as s:
        rows = s.query(Gate).filter(Gate.run_id == run_id).order_by(Gate.id.asc()).all()
        return [(row.id, row.node_id, row.status) for row in rows]


def _recovered(run_id: str) -> list[dict]:
    return [event["payload"] for event in load_events(run_id) if event["type"] == "run_recovered"]


@pytest.fixture
def started(monkeypatch) -> list[str]:
    """记录恢复流程重新拉起了哪些运行；不真的调度。"""
    calls: list[str] = []
    monkeypatch.setattr(orchestrator, "start", calls.append)
    return calls


def _recover() -> None:
    asyncio.run(Orchestrator.recover(orchestrator))


def test_an_interrupted_node_is_reset_and_the_run_is_restarted(started) -> None:
    """进程重启时正在执行的节点：未回执的执行记录作废，节点回到待调度，发恢复事件并重新拉起。"""
    run_id, nodes = _run("running", collect="running", plan="pending")
    orphan = _attempt(nodes["collect"])
    receipted = _attempt(nodes["plan"], status="done")

    _recover()

    with session_scope() as s:
        orphan_row, done_row = s.get(NodeAttempt, orphan), s.get(NodeAttempt, receipted)
        assert (orphan_row.status, orphan_row.error, orphan_row.finished_at is not None) == (
            "failed", "进程重启，未回执的 attempt 作废", True)
        assert done_row.status == "done"            # 已回执的执行记录不动
    assert _node_statuses(run_id) == {"collect": "pending", "plan": "pending"}
    assert _recovered(run_id) == [{"reset_nodes": 1, "status": "running"}]
    assert run_id in started


def test_a_run_waiting_on_an_open_gate_keeps_that_gate(started) -> None:
    """等待人工且有开着的门：门原样保留，不重复开门，也不重新拉起。"""
    run_id, nodes = _run("running", gate="pending")
    gate = request_gate(run_id, nodes["gate"])

    _recover()

    assert _gates(run_id) == [(gate["id"], nodes["gate"], "open")]
    assert open_gate_for_run(run_id) == {"id": gate["id"], "node_id": nodes["gate"]}
    assert _recovered(run_id) == []
    assert run_id not in started


def test_a_waiting_node_without_an_open_gate_gets_a_new_one_with_the_feedback(
        started) -> None:
    """等待人工的节点却没有开着的门（上一扇已经有了决定）：补开一扇，带上节点上的反馈，
    免得运行一直等下去。已经有决定的旧门不算开着的门。"""
    run_id, nodes = _run("waiting_human", gate={
        "status": "waiting_human", "config": {"feedback": "请补充验收标准"}})
    with session_scope() as s:
        decided = Gate(run_id=run_id, node_id=nodes["gate"], status="approved")
        s.add(decided)
        s.flush()
        decided_id = decided.id

    _recover()

    opened = open_gate_for_run(run_id)
    assert opened is not None and opened["node_id"] == nodes["gate"]
    assert get_gate(opened["id"])["feedback"] == "请补充验收标准"
    assert _gates(run_id) == [(decided_id, nodes["gate"], "approved"),
                              (opened["id"], nodes["gate"], "open")]
    assert run_id not in started


def test_an_interrupted_node_in_a_waiting_run_is_reset_without_restarting(
        monkeypatch, started) -> None:
    """并行分支：一支在门上等人，另一支崩溃时还在执行。后者回到待调度；等人期间不重新拉起。"""
    run_id, nodes = _run("running", gate="pending", collect="running")
    gate = request_gate(run_id, nodes["gate"])
    _attempt(nodes["collect"])

    _recover()

    assert _node_statuses(run_id) == {"gate": "waiting_human", "collect": "pending"}
    assert _gates(run_id) == [(gate["id"], nodes["gate"], "open")]
    assert _recovered(run_id) == [{"reset_nodes": 1, "status": "waiting_human"}]
    assert run_id not in started


def test_a_cancelled_run_is_restarted_with_a_recovered_event(started) -> None:
    """运行被取消时节点已经回到待调度：没有可重置的节点，也发恢复事件（下游靠它确认恢复发生过）。"""
    run_id, _nodes = _run("running", collect="pending")

    _recover()

    assert _recovered(run_id) == [{"reset_nodes": 0, "status": "running"}]
    assert run_id in started


def test_a_created_run_is_started_without_a_recovered_event(started) -> None:
    run_id, _nodes = _run("created", collect="pending")

    _recover()

    assert run_id in started
    assert _recovered(run_id) == []


@pytest.mark.parametrize("status", ["finished", "failed", "stopped"])
def test_a_finished_run_is_left_alone(started, status) -> None:
    """结束的运行不重新拉起、不发事件；里面残留的执行中节点也原样保留。"""
    run_id, nodes = _run(status, collect="running")
    _attempt(nodes["collect"])

    _recover()

    assert run_id not in started
    assert load_events(run_id) == []
    assert _node_statuses(run_id) == {"collect": "running"}
