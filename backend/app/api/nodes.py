"""节点详情与人工重试。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.schemas import RetryIn
from app.db.base import session_scope
from app.db.models import Gate, Message, Node, NodeAttempt, Run
from app.engine.events import emit
from app.engine.feedback import with_feedback
from app.engine.orchestrator import orchestrator

router = APIRouter(tags=["nodes"])


def _iso(value) -> str | None:
    return value.isoformat() if value else None


@router.get("/nodes/{node_id}")
def node_detail(node_id: int, messages: int = 40) -> dict:
    with session_scope() as s:
        node = s.get(Node, node_id)
        if node is None:
            raise HTTPException(404, "节点不存在")
        attempts = (
            s.query(NodeAttempt).filter(NodeAttempt.node_id == node_id)
            .order_by(NodeAttempt.attempt_no.asc()).all()
        )
        rows = (
            s.query(Message).filter(Message.node_id == node_id)
            .order_by(Message.id.desc()).limit(max(1, min(messages, 200))).all()
        )
        return {
            "id": node.id, "run_id": node.run_id, "node_key": node.node_key,
            "type": node.type, "name": node.name, "phase": node.phase,
            "status": node.status, "deps": list(node.deps_json or []),
            "wave": node.wave, "config": node.config_json or {},
            "assignee": node.assignee_json or {},
            "attempts": [
                {
                    "id": a.id, "attempt_no": a.attempt_no, "model_id": a.model_id,
                    "status": a.status, "error": a.error,
                    "started_at": _iso(a.started_at), "finished_at": _iso(a.finished_at),
                    "tokens_in": a.tokens_in, "tokens_out": a.tokens_out,
                }
                for a in attempts
            ],
            "messages": [
                {"role": m.role, "content": (m.content_json or {}).get("content", ""),
                 "tool_call_id": (m.content_json or {}).get("tool_call_id")}
                for m in reversed(rows)
            ],
        }


@router.post("/nodes/{node_id}/retry")
async def retry_node(node_id: int, payload: RetryIn | None = None) -> dict:
    feedback = (payload.feedback if payload else None) or ""
    with session_scope() as s:
        node = s.get(Node, node_id)
        if node is None:
            raise HTTPException(404, "节点不存在")
        run = s.get(Run, node.run_id)
        if run is None:
            raise HTTPException(404, "run 不存在")
        if node.status in ("running",):
            raise HTTPException(400, "节点正在运行，不能重试")
        for gate in (
            s.query(Gate).filter(Gate.node_id == node_id, Gate.status == "open").all()
        ):
            gate.status = "approved"
            gate.decided_by = "human-retry"
            gate.feedback = feedback or "人工重试"
        config = with_feedback(dict(node.config_json or {}), feedback or None, "user")
        config.pop("escalation", None)
        node.config_json = config
        node.status = "pending"
        run.status = "running"
        run.error = None
        run_id = run.id
        node_key = node.node_key
    await emit(run_id, "node_retried", node_id,
               {"node_key": node_key, "feedback": feedback})
    orchestrator.kick(run_id)
    return {"node_id": node_id, "status": "pending", "run_id": run_id}
