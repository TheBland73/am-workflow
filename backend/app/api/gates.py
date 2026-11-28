"""人工门决策 API（设计规格 §5.5、§5.7）。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from app.api.schemas import GateDecision
from app.db.base import session_scope
from app.db.models import Gate, Node, Run
from app.engine.events import emit
from app.engine.gates import GateError, get_gate
from app.engine.orchestrator import orchestrator

router = APIRouter(tags=["gates"])

# 后台判定任务要持有强引用，避免被垃圾回收；进程退出时随事件循环取消
_background_tasks: set[asyncio.Task] = set()


async def _decide(gate_id: int, action: str, payload: GateDecision) -> dict:
    try:
        return await orchestrator.decide_gate(
            gate_id=gate_id, action=action, feedback=payload.feedback,
            target_node=payload.target_node, decided_by=payload.decided_by or "human",
            artifact_kind=payload.artifact_kind, artifact_body=payload.artifact_body,
            data=payload.data,
        )
    except GateError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/gates")
def list_gates(run_id: str) -> dict:
    from app.db.base import session_scope
    from app.db.models import Gate

    with session_scope() as s:
        rows = s.query(Gate).filter(Gate.run_id == run_id).order_by(Gate.id.desc()).all()
        return {
            "items": [
                {"id": g.id, "run_id": g.run_id, "node_id": g.node_id, "status": g.status,
                 "feedback": g.feedback, "decided_by": g.decided_by}
                for g in rows
            ]
        }


@router.get("/gates/{gate_id}")
def gate_detail(gate_id: int) -> dict:
    gate = get_gate(gate_id)
    if gate is None:
        raise HTTPException(404, "门不存在")
    return gate


@router.post("/gates/{gate_id}/approve")
async def approve_gate(gate_id: int, payload: GateDecision | None = None) -> dict:
    return await _decide(gate_id, "approve", payload or GateDecision())


@router.post("/gates/{gate_id}/reject")
async def reject_gate(gate_id: int, payload: GateDecision) -> dict:
    return await _decide(gate_id, "reject", payload)


# ---------------- 交给模型判 ----------------
async def _delegate_checkpoint(gate: dict) -> None:
    """会议内的确认点：不调裁决模型，按仅兜底等级下的做法落一个决定。"""
    kind = str(gate.get("kind") or "")
    decision: dict = {"action": "approve", "decided_by": "user-delegated"}
    data: dict = {}
    if kind == "clarify":
        decision["answers"] = []              # 未答的记为假设
        data["answers"] = []
    elif kind == "consensus":
        decision["option"] = "delegate"       # 授权主持人裁决
        data["option"] = "delegate"
    elif kind == "escalation":
        decision["option"] = "accept"         # 接受现状
        data["option"] = "accept"
    with session_scope() as s:
        row = s.get(Gate, gate["id"])
        row.status = "approved"
        row.decided_by = "user-delegated"
        row.decided_at = datetime.now(timezone.utc)
        # 会议从 decision_json["data"] 读决定的内容（与人工路径一致）
        row.decision_json = {"action": "approve", "feedback": "", "data": data}
        node = s.get(Node, gate["node_id"])
        if node is not None:
            node.status = "pending"
        run = s.get(Run, gate["run_id"])
        if run is not None:
            run.status = "running"
            run.error = None
    await emit(gate["run_id"], "checkpoint_decided", gate["node_id"], {
        "gate_id": gate["id"], "kind": kind, "decided_by": "user-delegated",
        "decision": decision})


async def _judge_in_background(gate_id: int) -> None:
    gate = get_gate(gate_id)
    if gate is None or gate["status"] != "open":
        return
    _, nodes = orchestrator._load(gate["run_id"])
    node = next((item for item in nodes if item["id"] == gate["node_id"]), None)
    if node is None:
        return
    try:
        await orchestrator._judge_gate(gate["run_id"], node)
    except Exception as exc:                  # 判定失败留在开着的门上，原因可见
        await orchestrator._gate_to_human(
            gate["run_id"], node, f"把确认点交给模型判定失败：{exc}")


@router.post("/gates/{gate_id}/delegate", status_code=202)
async def delegate_gate(gate_id: int) -> dict:
    gate = get_gate(gate_id)
    if gate is None:
        raise HTTPException(404, "门不存在")
    if gate["status"] != "open":
        raise HTTPException(400, "只有开着的确认点可以交给模型判定")
    with session_scope() as s:
        node = s.get(Node, gate["node_id"])
        node_type = node.type if node is not None else ""
    if node_type == "MEETING":
        if not gate["kind"]:
            raise HTTPException(400, "会议节点的这个确认点不能交给模型判定，请人工处理")
        await _delegate_checkpoint(gate)
        orchestrator.kick(gate["run_id"])
        return {"delegated": True, "gate_id": gate_id, "to": "program"}
    task = asyncio.create_task(_judge_in_background(gate_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"delegated": True, "gate_id": gate_id, "to": "model"}
