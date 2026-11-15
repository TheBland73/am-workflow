"""Run 生命周期、快照与 SSE（设计规格 §5.4、§5.5）。"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func

from app.api.schemas import RunIn
from app.artifacts.store import list_artifacts, write_artifact
from app.config import settings
from app.db.base import session_scope
from app.db.models import Artifact, Event, Gate, Node, NodeAttempt, Run, Task, Workflow, new_run_id
from app.engine.events import bus, emit, load_events, sse_format
from app.engine.orchestrator import orchestrator
from app.engine.templates import instantiate_nodes, phase_names

router = APIRouter(tags=["runs"])


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _run_dict(run: Run) -> dict:
    return {
        "id": run.id, "workflow_id": run.workflow_id, "status": run.status,
        "current_phase": run.current_phase, "idea": run.idea_text,
        "created_at": _iso(run.created_at), "finished_at": _iso(run.finished_at),
        "budget_tokens": run.budget_tokens, "error": run.error,
    }


@router.post("/runs", status_code=201)
async def create_run(payload: RunIn) -> dict:
    from app.engine.templates import workflow_definition

    definition = workflow_definition(payload.template_key)
    if not definition:
        raise HTTPException(400, f"工作流模板不存在：{payload.template_key}")
    run_id = new_run_id()
    with session_scope() as s:
        wf = s.query(Workflow).filter(Workflow.template_key == payload.template_key).one_or_none()
        if wf is None:
            raise HTTPException(400, "工作流模板尚未初始化")
        run = Run(
            id=run_id, workflow_id=wf.id, status="created", idea_text=payload.idea,
            budget_tokens=payload.budget_tokens or settings.default_budget_tokens,
        )
        s.add(run)
    instantiate_nodes(run_id, definition)
    record = write_artifact(run_id, "idea", payload.idea, created_by="user",
                            inputs=[], status="approved")
    await emit(run_id, "run_created", None,
               {"run_id": run_id, "template": payload.template_key,
                "idea": payload.idea[:500]})
    await emit(run_id, "artifact_written", None, record)
    orchestrator.start(run_id)
    return get_run(run_id)


@router.get("/runs")
def list_runs(limit: int = 50) -> dict:
    with session_scope() as s:
        runs = s.query(Run).order_by(Run.created_at.desc()).limit(limit).all()
        items = []
        for run in runs:
            total = s.query(func.count(Node.id)).filter(Node.run_id == run.id).scalar() or 0
            done = (
                s.query(func.count(Node.id))
                .filter(Node.run_id == run.id, Node.status.in_(["done", "skipped"]))
                .scalar()
                or 0
            )
            items.append({**_run_dict(run), "nodes_total": total, "nodes_done": done})
        return {"items": items}


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    with session_scope() as s:
        run = s.get(Run, run_id)
        if run is None:
            raise HTTPException(404, "run 不存在")
        wf = s.get(Workflow, run.workflow_id)
        nodes = (
            s.query(Node).filter(Node.run_id == run_id)
            .order_by(Node.order_index.asc(), Node.id.asc()).all()
        )
        attempts = {
            node_id: count
            for node_id, count in (
                s.query(NodeAttempt.node_id, func.count(NodeAttempt.id))
                .join(Node, Node.id == NodeAttempt.node_id)
                .filter(Node.run_id == run_id)
                .group_by(NodeAttempt.node_id)
                .all()
            )
        }
        tasks = s.query(Task).filter(Task.run_id == run_id).order_by(Task.wave, Task.id).all()
        gates = s.query(Gate).filter(Gate.run_id == run_id).order_by(Gate.id.desc()).all()
        usage = (
            s.query(func.coalesce(func.sum(NodeAttempt.tokens_in), 0),
                     func.coalesce(func.sum(NodeAttempt.tokens_out), 0))
            .join(Node, Node.id == NodeAttempt.node_id)
            .filter(Node.run_id == run_id)
            .one()
        )
        node_rows = [
            {
                "id": n.id, "node_key": n.node_key, "type": n.type, "name": n.name,
                "phase": n.phase, "status": n.status, "assignee": n.assignee_json or {},
                "deps": list(n.deps_json or []), "wave": n.wave,
                "max_attempts": n.max_attempts, "attempts": attempts.get(n.id, 0),
                "config": n.config_json or {},
            }
            for n in nodes
        ]
        gate_rows = [
            {
                "id": g.id, "node_id": g.node_id,
                "node_key": next((n["node_key"] for n in node_rows if n["id"] == g.node_id), ""),
                "status": g.status, "feedback": g.feedback, "decided_by": g.decided_by,
                "decided_at": _iso(g.decided_at),
            }
            for g in gates
        ]
        task_rows = [
            {
                "task_uid": t.task_uid, "title": t.title, "deps": list(t.deps_json or []),
                "design_section": t.design_section, "test_cases": list(t.test_cases_json or []),
                "assignee": t.assignee_json or {}, "wave": t.wave, "status": t.status,
            }
            for t in tasks
        ]
        definition = dict(wf.definition_json or {}) if wf else {}
        names = phase_names(definition)
        phases = [
            item["key"] if isinstance(item, dict) else str(item)
            for item in definition.get("phases", [])
        ]
        tokens_in, tokens_out = int(usage[0] or 0), int(usage[1] or 0)
        remaining = int(run.budget_tokens or 0)
        snapshot = {
            "run": _run_dict(run),
            "workflow": {"key": wf.template_key if wf else "", "name": wf.name if wf else "",
                         "phases": phases},
            "phase_names": names,
            "nodes": node_rows,
            "tasks": task_rows,
            "gates": gate_rows,
            "usage": {
                "tokens_in": tokens_in, "tokens_out": tokens_out,
                "total": tokens_in + tokens_out,
                "remaining": max(0, remaining),
                "budget": tokens_in + tokens_out + max(0, remaining),
            },
        }
    # 单连接池：工件读取必须在上面的事务关闭之后，避免嵌套占用连接。
    snapshot["artifacts"] = list_artifacts(run_id)
    return snapshot


@router.post("/runs/{run_id}/events")
async def noop() -> None:  # pragma: no cover - 占位，避免误用 POST
    raise HTTPException(405, "请使用 GET /runs/{run_id}/events")


@router.get("/runs/{run_id}/events")
async def stream_events(run_id: str, request: Request) -> StreamingResponse:
    with session_scope() as s:
        if s.get(Run, run_id) is None:
            raise HTTPException(404, "run 不存在")
    since = 0
    raw_since = request.headers.get("last-event-id") or request.query_params.get("since") or "0"
    try:
        since = int(raw_since)
    except ValueError:
        since = 0

    queue = bus.subscribe(run_id)

    async def generator():
        try:
            for event in load_events(run_id, since):
                yield sse_format(event)
                if event["type"] in ("run_finished", "run_error", "run_stopped"):
                    return
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except (asyncio.TimeoutError, TimeoutError):
                    yield ": ping\n\n"
                    continue
                yield sse_format(event)
                if event["type"] in ("run_finished", "run_error", "run_stopped"):
                    return
        finally:
            bus.unsubscribe(run_id, queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


@router.post("/runs/{run_id}/stop")
async def stop_run(run_id: str) -> dict:
    await orchestrator.stop(run_id)
    return {"id": run_id, "status": "stopped"}


@router.get("/runs/{run_id}/events/history")
def event_history(run_id: str, since: int = 0, limit: int = 2000) -> dict:
    return {"items": load_events(run_id, since, limit)}


@router.get("/events/{event_id}")
def event_detail(event_id: int) -> dict:
    with session_scope() as s:
        row = s.get(Event, event_id)
        if row is None:
            raise HTTPException(404, "事件不存在")
        return {"id": row.id, "run_id": row.run_id, "seq": row.seq,
                "type": row.type, "node_id": row.node_id,
                "ts": _iso(row.ts), "payload": row.payload_json or {}}
