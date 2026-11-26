"""会议内的确认点：复用门的机制，挂在会议节点上。

门行上多存三样：类型（kind）、挂起时给判定者看的内容（payload_json）、做出的决定
（decision_json）。会议内确认点的通过和驳回都让会议续开，所以"决定"以 decision_json
里有没有 action 为准——节点重置也会把关着的门置为已批准，但那不是决定。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.db.base import session_scope
from app.db.models import MEETING_SUSPENDED, Gate, Meeting, Node, Run
from app.engine.events import emit
from app.engine.gates import gate_payload

if TYPE_CHECKING:
    from app.meeting.session import MeetingSession


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def open_checkpoint(mtg: "MeetingSession", kind: str, *, title: str, description: str,
                          payload: dict | None = None) -> dict:
    """建一条 open 的门并挂起，返回门的载荷。节点上还开着的旧门（如节点被重置留下的）一并作废。

    门、节点与运行的状态、会议的状态和"在等哪扇门"在同一个事务里写完：中间崩溃不会留下一扇
    会议不知道自己在等的大门。所以这里直接改会议行，不走 store（store 的函数各开各的事务，
    在这个事务里再开一个会在连接池上死等）。
    """
    with session_scope() as s:
        for stale in s.query(Gate).filter(Gate.node_id == mtg.node_id, Gate.status == "open").all():
            stale.status = "superseded"
        gate = Gate(run_id=mtg.run_id, node_id=mtg.node_id, status="open", kind=kind,
                    payload_json={"title": title, "description": description, **(payload or {})})
        s.add(gate)
        s.flush()
        node = s.get(Node, mtg.node_id)
        if node is not None:
            node.status = "waiting_human"
        run = s.get(Run, mtg.run_id)
        if run is not None:
            run.status = "waiting_human"
        meeting = s.get(Meeting, mtg.meeting_id)
        if meeting is not None:
            meeting.status = MEETING_SUSPENDED
            meeting.state_json = {**(meeting.state_json or {}), "awaiting_gate_id": gate.id,
                                  "checkpoint_kind": kind}
        body = gate_payload(s, gate)
    await emit(mtg.run_id, "gate_requested", mtg.node_id, body)
    return body


def get_decision(gate_id: int) -> dict | None:
    """门已有决定时返回 {gate_id, action, feedback, data}，否则返回 None。"""
    with session_scope() as s:
        gate = s.get(Gate, gate_id)
        if gate is None or gate.status == "open":
            return None
        decision = dict(gate.decision_json or {})
        if not decision.get("action"):
            return None
        return {"gate_id": gate.id, "action": decision["action"],
                "feedback": decision.get("feedback") or gate.feedback or "",
                "data": decision.get("data") or {}}


async def record_auto_decision(mtg: "MeetingSession", kind: str, *, decided_by: str,
                               decision: dict, title: str = "",
                               payload: dict | None = None) -> int:
    """不是由人当场判定的确认点：直接落一条已决定的门留审计，并推 checkpoint_decided。"""
    action = "approve" if decision.get("action") == "approve" else "reject"
    with session_scope() as s:
        gate = Gate(run_id=mtg.run_id, node_id=mtg.node_id,
                    status="approved" if action == "approve" else "rejected", kind=kind,
                    payload_json={"title": title, **(payload or {})},
                    decision_json={**decision, "action": action}, decided_by=decided_by,
                    decided_at=_now(), feedback=decision.get("feedback"))
        s.add(gate)
        s.flush()
        gate_id = gate.id
    await emit(mtg.run_id, "checkpoint_decided", mtg.node_id,
               {"gate_id": gate_id, "kind": kind, "decided_by": decided_by, "decision": decision})
    return gate_id
