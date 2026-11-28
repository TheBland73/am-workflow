"""会议内确认点的决定路径：通过或驳回都让会议续开，决定存在门上；工件确认点的行为不变；
写进节点的反馈带上来路和序号。"""
from __future__ import annotations

import asyncio

import pytest

from fastapi.testclient import TestClient

from app.db.base import init_db, session_scope
from app.db.models import Gate, Node, Run, Workflow
from app.engine.feedback import with_feedback
from app.engine.gates import GateError, decide_gate, get_gate, request_gate
from app.engine.orchestrator import orchestrator
from app.engine.runners import NodeContext
from app.engine.templates import instantiate_nodes, seed_defaults, workflow_definition
from app.main import app
from app.meeting import store
from app.meeting.checkpoints import open_checkpoint, record_auto_decision
from app.meeting.session import MeetingSession

_counter = iter(range(1, 1000))


def _new_run(node_kwargs: dict) -> tuple[str, int]:
    init_db()
    seed_defaults()
    index = next(_counter)
    with session_scope() as s:
        workflow_id = s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").one().id
        run_id = f"checkpoint-run-{index}"
        s.add(Run(id=run_id, workflow_id=workflow_id, status="running"))
        s.flush()
        node = Node(run_id=run_id, status="pending", **node_kwargs)
        s.add(node)
        s.flush()
        return run_id, node.id


def _open_checkpoint() -> MeetingSession:
    run_id, node_id = _new_run(dict(node_key="discuss", type="MEETING", name="讨论会议",
                                   config_json={"protocol": "discussion"}))
    ctx = NodeContext(run_id=run_id, node_id=node_id, node_key="discuss", type="MEETING",
                      name="讨论会议", phase="", config={"protocol": "discussion"}, assignee={},
                      attempt_id=0, attempt_no=1)
    meeting_id = store.create_meeting(run_id=run_id, node_id=node_id, node_key="discuss",
                                      protocol="discussion", charter={}, config={})
    mtg = MeetingSession(meeting_id=meeting_id, ctx=ctx)
    asyncio.run(open_checkpoint(mtg, "consensus", title="请确认", description="还有未决议题",
                                payload={"open": ["D2"]}))
    return mtg


def _template_run() -> tuple[str, int]:
    """按当前模板实例化的运行，返回 (运行 id, 方案确认门的节点 id)。"""
    init_db()
    seed_defaults()
    run_id = f"template-run-{next(_counter)}"
    with session_scope() as s:
        workflow_id = s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").one().id
        s.add(Run(id=run_id, workflow_id=workflow_id, status="running"))
        s.flush()
    instantiate_nodes(run_id, workflow_definition("sdlc_v1"))
    with session_scope() as s:
        node_id = s.query(Node).filter(Node.run_id == run_id, Node.node_key == "gate_proposal").one().id
    return run_id, node_id


def test_open_checkpoint_suspends_node_and_run() -> None:
    mtg = _open_checkpoint()
    gate = get_gate(store.get_state(mtg.meeting_id)["awaiting_gate_id"])
    assert gate["kind"] == "consensus" and gate["status"] == "open"
    assert gate["title"] == "请确认"
    assert gate["payload"]["open"] == ["D2"]
    with session_scope() as s:
        assert s.get(Node, mtg.node_id).status == "waiting_human"
    assert store.get_meeting(mtg.meeting_id)["status"] == "suspended"


def test_checkpoint_gate_records_the_meeting_it_belongs_to() -> None:
    # 会议归档或重开后同一节点上会有新旧两代门：门上记下所属会议，才分得清哪条是哪场会议的
    mtg = _open_checkpoint()
    gate = get_gate(store.get_state(mtg.meeting_id)["awaiting_gate_id"])
    assert gate["meeting_id"] == mtg.meeting_id
    with session_scope() as s:
        assert s.get(Gate, gate["id"]).meeting_id == mtg.meeting_id


def test_auto_decision_gate_records_the_meeting() -> None:
    mtg = _open_checkpoint()
    gate_id = asyncio.run(record_auto_decision(mtg, "roster", decided_by="program",
                                               decision={"action": "approve"}))
    assert get_gate(gate_id)["meeting_id"] == mtg.meeting_id


def test_artifact_gates_are_not_tied_to_a_meeting() -> None:
    _run_id, node_id = _template_run()
    gate = request_gate(_run_id, node_id)
    assert gate["kind"] == "" and gate["meeting_id"] is None


def test_reject_needs_no_target_and_lets_meeting_resume() -> None:
    mtg = _open_checkpoint()
    gate_id = store.get_state(mtg.meeting_id)["awaiting_gate_id"]
    result = decide_gate(gate_id=gate_id, action="reject", feedback="给 D2 再多一轮")
    assert result["checkpoint"] is True and result["status"] == "rejected"
    with session_scope() as s:
        assert s.get(Node, mtg.node_id).status == "pending"
        assert s.get(Run, mtg.run_id).status == "running"
        assert s.get(Gate, gate_id).decision_json == {
            "action": "reject", "feedback": "给 D2 再多一轮", "data": {}}
    # 会议本身的挂起状态由运行器在续开时改，决定路径不动它
    assert store.get_meeting(mtg.meeting_id)["status"] == "suspended"


def test_checkpoint_reject_needs_no_feedback() -> None:
    mtg = _open_checkpoint()
    gate_id = store.get_state(mtg.meeting_id)["awaiting_gate_id"]
    result = decide_gate(gate_id=gate_id, action="reject")
    assert result["checkpoint"] is True and result["status"] == "rejected"
    assert get_gate(gate_id)["decision"]["feedback"] == ""


def test_checkpoint_gate_cannot_be_decided_twice() -> None:
    mtg = _open_checkpoint()
    gate_id = store.get_state(mtg.meeting_id)["awaiting_gate_id"]
    decide_gate(gate_id=gate_id, action="approve")
    with pytest.raises(GateError):
        decide_gate(gate_id=gate_id, action="approve")


def test_approve_carries_data_and_decider() -> None:
    mtg = _open_checkpoint()
    gate_id = store.get_state(mtg.meeting_id)["awaiting_gate_id"]
    decide_gate(gate_id=gate_id, action="approve", decided_by="model:mock",
                data={"option": "proceed"})
    gate = get_gate(gate_id)
    assert gate["status"] == "approved"
    assert gate["decision"]["data"] == {"option": "proceed"}
    assert gate["decided_by"] == "model:mock"


def test_api_approve_passes_data_through(monkeypatch) -> None:
    mtg = _open_checkpoint()
    gate_id = store.get_state(mtg.meeting_id)["awaiting_gate_id"]
    monkeypatch.setattr(orchestrator, "kick", lambda run_id: None)
    with TestClient(app) as client:
        response = client.post(f"/api/gates/{gate_id}/approve",
                               json={"data": {"option": "proceed"}})
    assert response.status_code == 200, response.text
    assert get_gate(gate_id)["decision"]["data"] == {"option": "proceed"}
    from app.engine.events import load_events
    decided = [e for e in load_events(mtg.run_id) if e["type"] == "checkpoint_decided"]
    # 会议内确认点的人工决定也推 checkpoint_decided（与其它三类判定者同一可观测口径）
    assert decided and decided[-1]["payload"]["kind"] == "consensus"
    assert decided[-1]["payload"]["decided_by"] == "human"


def test_artifact_gate_reject_still_requires_target() -> None:
    run_id, gate_node_id = _template_run()
    gate = request_gate(run_id, gate_node_id)
    with pytest.raises(GateError):
        decide_gate(gate_id=gate["id"], action="reject", feedback="重做")
    after = get_gate(gate["id"])
    assert after["status"] == "open" and after["feedback"] is None


def test_safety_net_gate_on_meeting_node_keeps_gate_semantics() -> None:
    """会议节点上的失败转人工门没有确认点类型，不能当成会议内确认点：通过后要清掉升级标记。"""
    run_id, node_id = _new_run(dict(node_key="discuss", type="MEETING", name="讨论会议",
                                    config_json={"protocol": "discussion", "escalation": True,
                                                 "escalation_reason": "模型连续失败"}))
    gate = request_gate(run_id, node_id)
    result = decide_gate(gate_id=gate["id"], action="approve")
    assert not result.get("checkpoint")
    with session_scope() as s:
        node = s.get(Node, node_id)
        assert node.status == "pending"
        assert "escalation" not in node.config_json
        assert "escalation_reason" not in node.config_json


def test_safety_net_gate_on_meeting_node_reject_still_requires_target() -> None:
    run_id, node_id = _new_run(dict(node_key="discuss", type="MEETING", name="讨论会议",
                                    config_json={"protocol": "discussion"}))
    gate = request_gate(run_id, node_id)
    with pytest.raises(GateError):
        decide_gate(gate_id=gate["id"], action="reject", feedback="重做")
    assert get_gate(gate["id"])["status"] == "open"


def test_reject_marks_feedback_with_kind_and_sequence() -> None:
    run_id, gate_node_id = _template_run()
    gate = request_gate(run_id, gate_node_id)
    decide_gate(gate_id=gate["id"], action="reject", feedback="范围太大", target_node="idea")
    with session_scope() as s:
        config = s.query(Node).filter(Node.run_id == run_id, Node.node_key == "idea").one().config_json
    assert config["feedback"] == "范围太大"
    assert config["feedback_kind"] == "reject"
    assert config["feedback_seq"] == 1


def test_retry_with_feedback_is_marked_as_user_feedback(monkeypatch) -> None:
    run_id, gate_node_id = _template_run()
    with session_scope() as s:
        s.get(Node, gate_node_id).status = "failed"
    monkeypatch.setattr(orchestrator, "kick", lambda run_id: None)
    with TestClient(app) as client:
        response = client.post(f"/api/nodes/{gate_node_id}/retry", json={"feedback": "请重做"})
    assert response.status_code == 200, response.text
    with session_scope() as s:
        config = s.get(Node, gate_node_id).config_json
    assert (config["feedback"], config["feedback_kind"], config["feedback_seq"]) == ("请重做", "user", 1)


def test_with_feedback_keeps_sequence_and_marks_kind() -> None:
    config = with_feedback({"feedback_seq": 2}, "再看看", "reject")
    assert (config["feedback"], config["feedback_kind"], config["feedback_seq"]) == ("再看看", "reject", 3)


def test_with_feedback_without_kind_clears_stale_kind() -> None:
    config = with_feedback({"feedback": "旧的", "feedback_kind": "reject", "feedback_seq": 3},
                           "自动重试的反馈")
    assert config["feedback"] == "自动重试的反馈"
    assert "feedback_kind" not in config
    assert config["feedback_seq"] == 4


def test_with_feedback_none_clears_feedback_but_keeps_sequence() -> None:
    config = with_feedback({"feedback": "旧的", "feedback_kind": "user", "feedback_seq": 4},
                           None, "user")
    assert "feedback" not in config and "feedback_kind" not in config
    assert config["feedback_seq"] == 4


def test_with_feedback_does_not_mutate_input() -> None:
    original = {"feedback": "旧的", "feedback_kind": "reject", "feedback_seq": 1}
    with_feedback(original, "新的", "reroute")
    assert original == {"feedback": "旧的", "feedback_kind": "reject", "feedback_seq": 1}


def test_auto_retry_feedback_has_its_own_kind(monkeypatch) -> None:
    """失败后的自动重试不继承旧的来路，否则会议会把它当成要复议的意见。"""
    init_db()
    captured: dict = {}
    monkeypatch.setattr(orchestrator, "_update_node",
                        lambda node_id, **kwargs: captured.update(kwargs))

    async def no_emit(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr("app.engine.orchestrator.emit", no_emit)
    node = {"id": 1, "node_key": "discuss", "type": "MEETING", "name": "讨论会议",
            "phase": "", "config": {"feedback_kind": "reject", "feedback_seq": 3},
            "assignee": {}, "max_attempts": 3}
    asyncio.run(orchestrator._handle_failure("run-auto-retry", node, 0, 1,
                                             "模型输出不符合要求", False))
    assert captured["status"] == "pending"
    assert captured["feedback"] == "模型输出不符合要求"
    assert captured["extra"]["feedback_kind"] == "auto_retry"
    assert captured["extra"]["feedback_seq"] == 4


def test_escalation_clears_stale_feedback_kind(monkeypatch) -> None:
    """失败转人工时不能留着上一次驳回的来路，否则人工通过后会被会议当成未处理的意见。"""
    run_id, node_id = _new_run(dict(
        node_key="discuss", type="MEETING", name="讨论会议",
        config_json={"protocol": "discussion", "feedback": "未消费的驳回",
                     "feedback_kind": "reject", "feedback_seq": 5}))

    async def no_emit(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr("app.engine.orchestrator.emit", no_emit)
    _, nodes = orchestrator._load(run_id)
    node = next(n for n in nodes if n["id"] == node_id)
    asyncio.run(orchestrator._handle_failure(run_id, node, 0, 1, "连续失败", True))
    with session_scope() as s:
        config = s.get(Node, node_id).config_json
    assert config["feedback"] == "连续失败"
    assert "feedback_kind" not in config
    assert config["escalation"] is True
