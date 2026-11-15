"""人工门决策 API（设计规格 §5.5、§5.7）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.schemas import GateDecision
from app.engine.gates import GateError, get_gate
from app.engine.orchestrator import orchestrator

router = APIRouter(tags=["gates"])


async def _decide(gate_id: int, action: str, payload: GateDecision) -> dict:
    try:
        return await orchestrator.decide_gate(
            gate_id=gate_id, action=action, feedback=payload.feedback,
            target_node=payload.target_node, decided_by=payload.decided_by or "human",
            artifact_kind=payload.artifact_kind, artifact_body=payload.artifact_body,
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
