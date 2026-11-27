"""MeetingSession 发出的事件：meeting_phase 的载荷取会议内部轮次。"""
from __future__ import annotations

import asyncio

from app.db.base import init_db, session_scope
from app.db.models import Node, Run, Workflow
from app.engine.events import load_events
from app.engine.runners import NodeContext
from app.engine.templates import seed_defaults
from app.meeting import store
from app.meeting.session import MeetingSession

_counter = iter(range(1, 1000))


def _session(node_round: int, meeting_round: int) -> MeetingSession:
    """一个评审会议节点：节点级轮次（回流次数）与会议内部轮次各不相干。"""
    init_db()
    seed_defaults()
    index = next(_counter)
    config = {"protocol": "review", "round": node_round}
    with session_scope() as s:
        workflow_id = s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").one().id
        run_id = f"session-run-{index}"
        s.add(Run(id=run_id, workflow_id=workflow_id, status="running"))
        s.flush()
        node = Node(node_key="review", type="MEETING", name="评审会议", run_id=run_id,
                    status="running", config_json=config)
        s.add(node)
        s.flush()
        ctx = NodeContext(run_id=run_id, node_id=node.id, node_key="review", type="MEETING",
                          name="评审会议", phase="", config=dict(config),
                          assignee={}, attempt_id=0, attempt_no=1)
    meeting_id = store.create_meeting(run_id=run_id, node_id=node.id, node_key="review",
                                      protocol="review", charter={}, config={})
    store.update_meeting(meeting_id, round=meeting_round)
    return MeetingSession(meeting_id=meeting_id, ctx=ctx)


def test_meeting_phase_event_carries_the_meeting_round() -> None:
    # 事件契约里的"轮次"是会议内部轮次（第几轮评审），不是节点被回流的次数
    mtg = _session(node_round=2, meeting_round=3)

    asyncio.run(mtg.advance("points", ("prepare", "points", "review")))

    events = [event for event in load_events(mtg.run_id) if event["type"] == "meeting_phase"]
    assert events[-1]["payload"] == {
        "meeting_id": mtg.meeting_id, "phase": "points", "round": 3}
    assert mtg.round_no() == 2                  # 节点级轮次照旧是 2
