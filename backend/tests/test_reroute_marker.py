"""编排器接受回流时的标记写入：目标节点确实重开之后才写，重开被跳过就不写。"""
from __future__ import annotations

import asyncio

import pytest

from app.db.base import init_db, session_scope
from app.db.models import Node, Run, Workflow
from app.engine import orchestrator as orchestrator_module
from app.engine.orchestrator import orchestrator
from app.engine.templates import seed_defaults
from app.meeting import store

_counter = iter(range(1, 1000))


def _reroute_setup(target_status: str) -> tuple[str, int, int]:
    """一个最小运行：评审会议节点发起回流，讨论会议节点按 target_status 摆好。"""
    init_db()
    seed_defaults()
    run_id = f"reroute-marker-{next(_counter)}"
    with session_scope() as s:
        workflow_id = s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").one().id
        s.add(Run(id=run_id, workflow_id=workflow_id, status="running",
                  idea_text="给小团队做一个反馈收集工具", config_json={}))
        s.flush()
        review = Node(run_id=run_id, node_key="review", type="MEETING", name="评审会议",
                      phase="review", status="running", deps_json=[], wave=3,
                      max_attempts=2, order_index=2,
                      config_json={"round": 1, "max_rounds": 3})
        discuss = Node(run_id=run_id, node_key="discuss", type="MEETING", name="讨论会议",
                       phase="discussion", status=target_status, deps_json=[], wave=1,
                       max_attempts=2, order_index=0, config_json={})
        s.add_all([review, discuss])
        s.flush()
        review_id = review.id
    meeting_id = store.create_meeting(run_id=run_id, node_id=review_id, node_key="review",
                                      protocol="review", charter={}, config={})
    return run_id, review_id, meeting_id


def _reroute(run_id: str, review_id: int) -> None:
    with session_scope() as s:
        config = dict(s.get(Node, review_id).config_json or {})
    asyncio.run(orchestrator._handle_reroute(
        run_id, {"id": review_id, "node_key": "review", "config": config},
        {"targets": ["discuss"], "feedback": "复议 D2 的结论", "self_feedback": "",
         "extra_round": False}))


def test_marker_written_after_targets_reopen() -> None:
    run_id, review_id, meeting_id = _reroute_setup("done")
    _reroute(run_id, review_id)
    state = store.get_state(meeting_id)
    assert state.get("rework_pending") is True


def test_marker_skipped_when_targets_not_reopened() -> None:
    """目标节点正在运行、没有被重开：不写标记，重入的协议要重新发起回流。"""
    run_id, review_id, meeting_id = _reroute_setup("running")
    _reroute(run_id, review_id)
    state = store.get_state(meeting_id)
    assert "rework_pending" not in state


def test_interrupted_acceptance_leaves_no_half_applied_state(monkeypatch) -> None:
    """接受回流的写库动作在同一个事务里：目标重开被中断，发起节点的轮次也不会先加上。"""
    run_id, review_id, meeting_id = _reroute_setup("done")
    original = orchestrator_module.with_feedback
    armed = {"on": True}

    def flaky(config, feedback, kind=None):
        if armed["on"] and kind == "reroute":        # 目标重开刚要落库：进程在这里被杀
            armed["on"] = False
            raise asyncio.CancelledError()
        return original(config, feedback, kind)

    monkeypatch.setattr(orchestrator_module, "with_feedback", flaky)
    with pytest.raises(asyncio.CancelledError):
        _reroute(run_id, review_id)

    with session_scope() as s:
        review = s.get(Node, review_id)
        discuss = s.query(Node).filter(Node.node_key == "discuss",
                                       Node.run_id == run_id).one()
        assert review.status == "running" and int(review.config_json["round"]) == 1
        assert "feedback" not in (discuss.config_json or {})
    assert store.get_state(meeting_id).get("rework_pending") is None

    _reroute(run_id, review_id)                      # 重入（不再中断）：整体生效
    with session_scope() as s:
        review = s.get(Node, review_id)
        discuss = s.query(Node).filter(Node.node_key == "discuss",
                                       Node.run_id == run_id).one()
        assert int(review.config_json["round"]) == 2
        assert discuss.config_json.get("feedback") == "复议 D2 的结论"
    assert store.get_state(meeting_id).get("rework_pending") is True
