"""Run 生命周期、快照、指标与 SSE（设计规格 §5.4、§5.5）。"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func

from app.api.schemas import RunIn
from app.artifacts.store import list_artifacts, write_artifact
from app.config import settings
from app.db.base import session_scope
from app.db.models import (
    Artifact, Event, Gate, ModelEntry, Node, NodeAttempt, Run, Task, Workflow, new_run_id,
)
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
        latest_attempts: dict[int, NodeAttempt] = {}
        for attempt in (
            s.query(NodeAttempt)
            .join(Node, Node.id == NodeAttempt.node_id)
            .filter(Node.run_id == run_id)
            .order_by(NodeAttempt.node_id.asc(), NodeAttempt.attempt_no.asc())
            .all()
        ):
            latest_attempts[attempt.node_id] = attempt
        tasks = s.query(Task).filter(Task.run_id == run_id).order_by(Task.wave, Task.id).all()
        gates = s.query(Gate).filter(Gate.run_id == run_id).order_by(Gate.id.desc()).all()
        event_stats = (
            s.query(
                func.count(Event.id),
                func.count(func.distinct(Event.type)),
                func.count(Event.id).filter(Event.type == "node_tool_call"),
            )
            .filter(Event.run_id == run_id)
            .one()
        )
        usage = (
            s.query(func.coalesce(func.sum(NodeAttempt.tokens_in), 0),
                     func.coalesce(func.sum(NodeAttempt.tokens_out), 0))
            .join(Node, Node.id == NodeAttempt.node_id)
            .filter(Node.run_id == run_id)
            .one()
        )
        node_rows = []
        for n in nodes:
            attempt = latest_attempts.get(n.id)
            node_rows.append({
                "id": n.id, "node_key": n.node_key, "type": n.type, "name": n.name,
                "phase": n.phase, "status": n.status, "assignee": n.assignee_json or {},
                "deps": list(n.deps_json or []), "wave": n.wave,
                "max_attempts": n.max_attempts, "attempts": attempts.get(n.id, 0),
                "config": n.config_json or {},
                "attempt": {
                    "attempt_no": attempt.attempt_no,
                    "status": attempt.status,
                    "started_at": _iso(attempt.started_at),
                    "finished_at": _iso(attempt.finished_at),
                    "tokens_in": int(attempt.tokens_in or 0),
                    "tokens_out": int(attempt.tokens_out or 0),
                    "error": attempt.error,
                } if attempt else None,
            })
        gate_rows = [
            {
                "id": g.id, "node_id": g.node_id,
                "node_key": next((n["node_key"] for n in node_rows if n["id"] == g.node_id), ""),
                "status": g.status, "feedback": g.feedback, "decided_by": g.decided_by,
                "created_at": _iso(g.created_at), "decided_at": _iso(g.decided_at),
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
            "stats": {
                "events": int(event_stats[0] or 0),
                "event_types": int(event_stats[1] or 0),
                "tool_calls": int(event_stats[2] or 0),
            },
        }
    # 单连接池：工件读取必须在上面的事务关闭之后，避免嵌套占用连接。
    snapshot["artifacts"] = list_artifacts(run_id)
    return snapshot


def _aware(value: datetime | None) -> datetime | None:
    """SQLite 读回的 DateTime 可能丢 tzinfo，统一按 UTC 处理后再做差。"""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _span_ms(start: datetime | None, end: datetime | None) -> int:
    a, b = _aware(start), _aware(end)
    if a is None or b is None:
        return 0
    return max(0, int((b - a).total_seconds() * 1000))


@router.get("/runs/{run_id}/metrics")
def run_metrics(run_id: str) -> dict:
    """运行指标：耗时、重试、token/成本、门等待。供看板与回归对比使用。"""
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        run = s.get(Run, run_id)
        if run is None:
            raise HTTPException(404, "run 不存在")
        nodes = (
            s.query(Node).filter(Node.run_id == run_id)
            .order_by(Node.order_index.asc(), Node.id.asc()).all()
        )
        node_ids = [n.id for n in nodes]
        attempts = (
            s.query(NodeAttempt)
            .filter(NodeAttempt.node_id.in_(node_ids))
            .order_by(NodeAttempt.node_id.asc(), NodeAttempt.attempt_no.asc())
            .all()
        ) if node_ids else []
        gates = s.query(Gate).filter(Gate.run_id == run_id).all()
        events = int(s.query(func.count(Event.id)).filter(Event.run_id == run_id).scalar() or 0)
        prices = {
            row.id: (float(row.price_in or 0), float(row.price_out or 0))
            for row in s.query(ModelEntry).all()
        }
        run_created, run_finished, run_status = run.created_at, run.finished_at, run.status

    by_node: dict[int, list[NodeAttempt]] = {}
    for attempt in attempts:
        by_node.setdefault(attempt.node_id, []).append(attempt)

    per_node: list[dict] = []
    durations: list[int] = []
    retries = 0
    tokens_in = tokens_out = 0
    cost = 0.0
    status_counts = {"total": len(nodes), "done": 0, "failed": 0, "running": 0,
                     "pending": 0, "waiting_human": 0, "skipped": 0}
    for n in nodes:
        rows = by_node.get(n.id, [])
        node_ms = 0
        n_in = n_out = 0
        for a in rows:
            if a.status == "running":
                dur = _span_ms(a.started_at, now)
            else:
                dur = _span_ms(a.started_at, a.finished_at)
            node_ms += dur
            durations.append(dur)
            a_in, a_out = int(a.tokens_in or 0), int(a.tokens_out or 0)
            n_in += a_in
            n_out += a_out
            price = prices.get(a.model_id) if a.model_id else None
            if price:
                cost += a_in / 1_000_000 * price[0] + a_out / 1_000_000 * price[1]
        retries += max(0, len(rows) - 1)
        tokens_in += n_in
        tokens_out += n_out
        status_counts[n.status] = status_counts.get(n.status, 0) + 1
        last = rows[-1] if rows else None
        per_node.append({
            "node_key": n.node_key, "type": n.type, "name": n.name, "phase": n.phase,
            "status": n.status, "attempts": len(rows), "retries": max(0, len(rows) - 1),
            "exec_ms": node_ms, "tokens_in": n_in, "tokens_out": n_out,
            "last_error": (last.error[:500] if last and last.error else None),
        })

    durations.sort()
    stale_before = now - timedelta(seconds=float(settings.attempt_stale_s))
    stale_attempts = sum(
        1 for a in attempts
        if a.status == "running"
        and (_aware(a.heartbeat_at) or _aware(a.started_at) or now) < stale_before
    )
    decided = [g for g in gates if g.decided_at is not None]
    decision_ms = [_span_ms(g.created_at, g.decided_at) for g in decided]
    open_wait_ms = [_span_ms(g.created_at, now) for g in gates if g.status == "open"]
    gate_counts = {"total": len(gates), "open": 0, "approved": 0, "rejected": 0}
    for g in gates:
        gate_counts[g.status] = gate_counts.get(g.status, 0) + 1

    return {
        "run_id": run_id,
        "status": run_status,
        "wall_ms": _span_ms(run_created, run_finished or now),
        "nodes": status_counts,
        "execution": {
            "attempts": len(attempts),
            "retries": retries,
            "busy_ms": sum(durations),
            "avg_attempt_ms": int(sum(durations) / len(durations)) if durations else 0,
            "p95_attempt_ms": durations[min(len(durations) - 1, int(len(durations) * 0.95))]
            if durations else 0,
            "max_attempt_ms": durations[-1] if durations else 0,
            "stale_attempts": stale_attempts,
        },
        "usage": {
            "tokens_in": tokens_in, "tokens_out": tokens_out,
            "total": tokens_in + tokens_out,
            "estimated_cost": round(cost, 6),
        },
        "gates": {
            **gate_counts,
            "avg_decision_ms": int(sum(decision_ms) / len(decision_ms)) if decision_ms else 0,
            "open_wait_ms": sum(open_wait_ms),
        },
        "events": events,
        "per_node": per_node,
    }


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
