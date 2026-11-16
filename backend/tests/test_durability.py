"""可靠性增强：并发闸门、节点超时、退避重试、事件序号、取消收据与指标 API。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db.base import init_db, session_scope
from app.db.models import Gate, Node, NodeAttempt, Run, Workflow, new_run_id
from app.engine import orchestrator as orch_mod
from app.engine.events import emit, load_events, reset_seq_cache
from app.engine.orchestrator import Orchestrator
from app.engine.runners import NodeFailure
from app.engine.templates import instantiate_nodes, seed_defaults, workflow_definition
from app.main import app


def _new_run(status: str = "running") -> str:
    init_db()
    seed_defaults()
    with session_scope() as s:
        wf = s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").one()
        run_id = new_run_id()
        s.add(Run(id=run_id, workflow_id=wf.id, status=status, idea_text="可靠性测试"))
    instantiate_nodes(run_id, workflow_definition("sdlc_v1"))
    return run_id


def _replace_nodes(run_id: str, specs: list[dict]) -> None:
    with session_scope() as s:
        s.query(Node).filter(Node.run_id == run_id).delete()
        for index, spec in enumerate(specs):
            s.add(Node(
                run_id=run_id, node_key=spec.get("key", f"n{index}"),
                type=spec.get("type", "AGENT"), name=spec.get("name", f"节点{index}"),
                phase="test", status="pending", deps_json=spec.get("deps", []),
                wave=spec.get("wave", 1), max_attempts=spec.get("max_attempts", 1),
                config_json=spec.get("config", {}), order_index=index,
            ))


def _node(run_id: str, key: str) -> Node:
    with session_scope() as s:
        return s.query(Node).filter(Node.run_id == run_id, Node.node_key == key).one()


def _attempts(node_id: int) -> list[NodeAttempt]:
    with session_scope() as s:
        return (
            s.query(NodeAttempt).filter(NodeAttempt.node_id == node_id)
            .order_by(NodeAttempt.attempt_no.asc()).all()
        )


def _status(run_id: str) -> str:
    with session_scope() as s:
        return s.get(Run, run_id).status


# ---------------- 并发闸门 ----------------
def test_node_concurrency_is_capped(monkeypatch) -> None:
    run_id = _new_run()
    _replace_nodes(run_id, [{"key": f"job{i}"} for i in range(6)])
    monkeypatch.setattr(settings, "node_concurrency", 2)
    state = {"active": 0, "peak": 0}

    async def fake_execute(_ctx):
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        await asyncio.sleep(0.05)
        state["active"] -= 1
        return {"ok": True}

    monkeypatch.setattr(orch_mod, "execute_node", fake_execute)
    asyncio.run(Orchestrator()._loop(run_id))

    assert state["peak"] == 2
    assert _status(run_id) == "finished"
    with session_scope() as s:
        assert s.query(Node).filter(
            Node.run_id == run_id, Node.status == "done"
        ).count() == 6


# ---------------- 节点超时 ----------------
def test_node_timeout_escalates_to_human_gate(monkeypatch) -> None:
    run_id = _new_run()
    _replace_nodes(run_id, [{"key": "slow", "config": {"timeout_s": 0.05}}])

    async def fake_execute(_ctx):
        await asyncio.sleep(5)
        return {"ok": True}

    monkeypatch.setattr(orch_mod, "execute_node", fake_execute)
    asyncio.run(Orchestrator()._loop(run_id))

    node = _node(run_id, "slow")
    assert node.status == "waiting_human"
    rows = _attempts(node.id)
    assert rows and rows[-1].status == "failed"
    assert "超时" in (rows[-1].error or "")
    with session_scope() as s:
        gate = s.query(Gate).filter(Gate.run_id == run_id, Gate.status == "open").one()
        assert gate.node_id == node.id
    assert _status(run_id) == "waiting_human"


# ---------------- 退避重试 ----------------
def test_retry_backoff_and_scheduled_event(monkeypatch) -> None:
    run_id = _new_run()
    _replace_nodes(run_id, [{"key": "flaky", "max_attempts": 2}])
    calls = {"n": 0}

    async def flaky(_ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise NodeFailure("第一次失败")
        return {"ok": True}

    monkeypatch.setattr(orch_mod, "execute_node", flaky)
    monkeypatch.setattr(settings, "retry_base_s", 0.05)
    monkeypatch.setattr(settings, "retry_max_s", 0.2)
    asyncio.run(Orchestrator()._loop(run_id))

    node = _node(run_id, "flaky")
    assert node.status == "done"
    assert calls["n"] == 2
    assert _status(run_id) == "finished"
    events = load_events(run_id)
    assert any(e["type"] == "node_retry_scheduled" for e in events)
    assert len(_attempts(node.id)) == 2


def test_retry_delay_is_exponential_with_jitter(monkeypatch) -> None:
    monkeypatch.setattr(settings, "retry_base_s", 8.0)
    monkeypatch.setattr(settings, "retry_max_s", 60.0)
    orch = Orchestrator()
    first, second, third = (orch._retry_delay(n) for n in (1, 2, 3))
    assert 4.0 <= first <= 8.0
    assert 8.0 <= second <= 16.0
    assert 16.0 <= third <= 32.0
    assert orch._retry_delay(10) <= 60.0


# ---------------- 取消收据 ----------------
def test_cancelled_attempt_is_recorded(monkeypatch) -> None:
    run_id = _new_run()
    _replace_nodes(run_id, [{"key": "cancelme"}])

    async def hang(_ctx):
        await asyncio.Event().wait()

    monkeypatch.setattr(orch_mod, "execute_node", hang)

    async def scenario() -> None:
        orch = Orchestrator()
        _run, nodes = orch._load(run_id)
        task = asyncio.create_task(orch._run_attempt(run_id, nodes[0]))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    node = _node(run_id, "cancelme")
    rows = _attempts(node.id)
    assert rows and rows[-1].status == "failed"
    assert "取消" in (rows[-1].error or "")
    assert node.status == "pending"


# ---------------- 事件 seq ----------------
def test_event_seq_monotonic_and_cached(monkeypatch) -> None:
    run_id = _new_run()
    reset_seq_cache()

    async def emit_many() -> None:
        await asyncio.gather(*(emit(run_id, "probe", None, {"i": i}) for i in range(20)))

    asyncio.run(emit_many())
    events = load_events(run_id)
    assert [e["seq"] for e in events] == list(range(1, 21))
    assert asyncio.run(emit(run_id, "probe"))["seq"] == 21
    # 清空内存游标后要从 DB 重新续号，而不是从 1 重来。
    reset_seq_cache(run_id)
    assert asyncio.run(emit(run_id, "probe"))["seq"] == 22


# ---------------- 指标 API ----------------
def test_run_metrics_api() -> None:
    run_id = _new_run(status="finished")
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        run = s.get(Run, run_id)
        run.created_at = now - timedelta(seconds=10)
        run.finished_at = now
        s.query(Node).filter(Node.run_id == run_id).delete()
        fast = Node(run_id=run_id, node_key="fast", type="AGENT", name="快节点",
                    phase="test", status="done", deps_json=[], wave=1, order_index=0)
        slow = Node(run_id=run_id, node_key="slow", type="VERIFY", name="慢节点",
                    phase="test", status="done", deps_json=[], wave=2, order_index=1)
        s.add_all([fast, slow])
        s.flush()
        s.add_all([
            NodeAttempt(node_id=fast.id, attempt_no=1, status="done",
                        started_at=now - timedelta(seconds=8), finished_at=now - timedelta(seconds=7),
                        tokens_in=1000, tokens_out=200, heartbeat_at=now),
            NodeAttempt(node_id=slow.id, attempt_no=1, status="failed",
                        started_at=now - timedelta(seconds=6), finished_at=now - timedelta(seconds=5),
                        tokens_in=500, tokens_out=100, error="第一次失败"),
            NodeAttempt(node_id=slow.id, attempt_no=2, status="done",
                        started_at=now - timedelta(seconds=4), finished_at=now - timedelta(seconds=3),
                        tokens_in=400, tokens_out=80),
        ])
        s.add(Gate(run_id=run_id, node_id=slow.id, status="approved",
                   created_at=now - timedelta(seconds=9), decided_at=now - timedelta(seconds=8)))

    with TestClient(app) as client:
        response = client.get(f"/api/runs/{run_id}/metrics")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["nodes"]["total"] == 2
    assert data["execution"]["attempts"] == 3
    assert data["execution"]["retries"] == 1
    assert data["usage"]["tokens_in"] == 1900
    assert data["usage"]["total"] == 2280
    assert data["gates"]["total"] == 1 and data["gates"]["approved"] == 1
    assert data["gates"]["avg_decision_ms"] == 1000
    assert data["events"] == 0
    assert len(data["per_node"]) == 2
    assert data["wall_ms"] == 10000
