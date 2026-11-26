"""Run 生命周期、快照、指标与 SSE（设计规格 §5.4、§5.5）。"""
from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func

from app.api.schemas import MaterialIn, RunConfigPatch, RunIn
from app.artifacts.store import latest_artifact, list_artifacts, read_artifact, write_artifact
from app.config import settings
from app.conversation.policy import reset_policy_cache
from app.db.base import session_scope
from app.db.models import (
    AgentTemplate, Artifact, Event, Gate, Meeting, MeetingTurn, ModelEntry, Node, NodeAttempt,
    Run, Task, Workflow, new_run_id,
)
from app.engine.events import bus, emit, load_events, sse_format
from app.engine.gates import GateError, gate_payload
from app.engine.materials import (
    check_materials, free_material_name, list_materials, read_material, save_material, total_chars,
)
from app.engine.orchestrator import orchestrator
from app.engine.params import apply_run_config, run_of, validate_run_config
from app.engine.templates import instantiate_nodes, phase_names, workflow_definition

router = APIRouter(tags=["runs"])

# 运行开始前，或有确认点挂起时，可以修改运行级配置
EDITABLE_RUN_STATUSES = ("created", "waiting_human")


def _run_or_404(run_id: str) -> dict:
    with session_scope() as s:
        run = s.get(Run, run_id)
        if run is None:
            raise HTTPException(404, "run 不存在")
        return {"id": run.id, "status": run.status, "config": dict(run.config_json or {})}


def _roster_errors(config: dict) -> list[str]:
    """名单里点名的角色必须存在。结构不合法时交给 validate_rosters，这里不再二次崩溃。"""
    rosters = config.get("rosters")
    if not isinstance(rosters, dict):
        return []
    wanted = [key for group in rosters.values() if isinstance(group, dict)
              for key in (group.get("role_keys") if isinstance(group.get("role_keys"), list) else [])
              if isinstance(key, str) and key]
    if not wanted:
        return []
    with session_scope() as s:
        known = {row[0] for row in s.query(AgentTemplate.role_key)
                 .filter(AgentTemplate.role_key.in_(wanted)).all()}
    return [f"角色模板不存在：{key}" for key in wanted if key not in known]


def _merge_run_config(base: dict, patch: dict) -> dict:
    """按分组浅合并一层：只改 params.discussion 时，params.context 等其他分组保持不变。"""
    merged = dict(base)
    for key in ("automation", "params", "rosters"):
        if key not in patch:
            continue
        value = patch[key]
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            section = dict(current)
            for name, item in value.items():
                section[name] = ({**section[name], **item}
                                 if isinstance(section.get(name), dict) and isinstance(item, dict)
                                 else item)
            merged[key] = section
        else:
            merged[key] = value
    return merged


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _run_dict(run: Run) -> dict:
    return {
        "id": run.id, "workflow_id": run.workflow_id, "status": run.status,
        "current_phase": run.current_phase, "idea": run.idea_text,
        "created_at": _iso(run.created_at), "finished_at": _iso(run.finished_at),
        "budget_tokens": run.budget_tokens, "error": run.error,
        "config": dict(run.config_json or {}),
    }


@router.post("/runs", status_code=201)
async def create_run(payload: RunIn) -> dict:
    """新建运行。配置与背景材料先全部校验，有任何不合法都不建运行。"""
    try:
        definition = workflow_definition(payload.template_key)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if payload.config is not None and not isinstance(payload.config, dict):
        raise HTTPException(400, "运行级配置须是对象")
    config = payload.config or {}
    used: set[str] = set()
    materials: list[tuple[str, str]] = []
    for index, item in enumerate(payload.materials):
        name = item.name.strip()
        if not name:
            name = free_material_name(used, index + 1)
        used.add(name)
        materials.append((name, item.content))
    errors = (validate_run_config(definition, config) + _roster_errors(config)
              + check_materials(materials))
    if errors:
        raise HTTPException(400, "运行级配置不合法：" + "；".join(errors))
    run_id = new_run_id()
    with session_scope() as s:
        wf = s.query(Workflow).filter(Workflow.template_key == payload.template_key).one_or_none()
        if wf is None:
            raise HTTPException(400, "工作流模板尚未初始化")
        s.add(Run(id=run_id, workflow_id=wf.id, status="created", idea_text=payload.idea,
                  budget_tokens=payload.budget_tokens or settings.default_budget_tokens,
                  config_json=config))
    try:
        instantiate_nodes(run_id, definition)
        apply_run_config(run_id, definition, config)   # 生效参数写进会议节点
        for name, content in materials:
            save_material(run_id, name, content)
        record = write_artifact(run_id, "idea", payload.idea, created_by="user",
                                inputs=[], status="approved")
        await emit(run_id, "run_created", None,
                   {"run_id": run_id, "template": payload.template_key,
                    "idea": payload.idea[:500]})
        await emit(run_id, "artifact_written", None, record)
        orchestrator.start(run_id)
    except Exception as exc:
        # 建运行后的任何一步失败都不能留下一个可被恢复流程拉起的 created 运行
        with session_scope() as s:
            row = s.get(Run, run_id)
            if row is not None:
                row.status = "failed"
                row.error = f"创建运行失败：{exc}"[:2000]
        shutil.rmtree(settings.runs_dir / run_id, ignore_errors=True)
        try:
            await emit(run_id, "run_error", None, {"error": f"创建运行失败：{exc}"[:500]})
        except Exception:
            pass
        raise
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
        # 与 request_gate 共用同一份 payload：刷新/重载后卡片仍能拿到 title/description
        # （方案确认门的 description 含讨论未决项）。
        gate_rows = [gate_payload(s, g) for g in gates]
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


@router.get("/runs/{run_id}/review")
def get_run_review(run_id: str) -> dict:
    """最新评审报告：结论、问题清单、报告正文与是否可回退。"""
    with session_scope() as s:
        run = s.get(Run, run_id)
        if run is None:
            raise HTTPException(404, "run 不存在")
        node = (
            s.query(Node)
            .filter(Node.run_id == run_id, Node.type == "REVIEW")
            .order_by(Node.order_index.desc(), Node.id.desc())
            .first()
        )
        if node is None:
            return {"available": False, "run_id": run_id, "status": run.status}
        config = dict(node.config_json or {})
        report = dict(config.get("review") or {})
        issues = report.get("issues")
        if not isinstance(issues, list):
            issues = []
        payload = {
            "available": bool(report),
            "run_id": run_id,
            "run_status": run.status,
            "node_id": node.id,
            "node_key": node.node_key,
            "node_status": node.status,
            "verdict": report.get("verdict"),
            "summary": report.get("summary"),
            "issues": issues,
            "blocking": int(report.get("blocking") or 0),
            "targets": list(report.get("targets") or []),
            "round": int(config.get("round") or report.get("round") or 0),
            "max_rounds": int(config.get("max_rounds") or report.get("max_rounds") or 2),
            "reviewed_at": report.get("reviewed_at"),
            "files": list(report.get("files") or []),
            "history": list(config.get("review_history") or [])[-8:],
            "escalated": bool(config.get("escalation")),
            "can_rework": run.status in ("waiting_human", "failed") and bool(report),
        }
    payload["artifact"] = latest_artifact(run_id, "review")
    raw = read_artifact(run_id, "review")
    if raw:
        from app.artifacts.store import parse_front_matter

        _meta, body = parse_front_matter(raw)
        payload["body"] = body.strip()
    return payload


class ReviewReworkIn(BaseModel):
    task_uids: list[str] = Field(default_factory=list)
    feedback: str | None = None


@router.post("/runs/{run_id}/review/rework")
async def rework_from_review(run_id: str, payload: ReviewReworkIn) -> dict:
    if not payload.task_uids:
        raise HTTPException(400, "至少选择一个要回退的任务")
    try:
        return await orchestrator.rework_review(run_id, payload.task_uids, payload.feedback)
    except GateError as exc:
        raise HTTPException(400, str(exc))


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
        review_data = [
            {
                "node_id": n.id, "node_key": n.node_key, "name": n.name,
                "round": int((n.config_json or {}).get("round") or 0),
                "max_rounds": int((n.config_json or {}).get("max_rounds") or 2),
                "report": dict((n.config_json or {}).get("review") or {}),
                "history": list((n.config_json or {}).get("review_history") or []),
            }
            for n in nodes if n.type == "REVIEW"
        ]
        prices = {
            row.id: (float(row.price_in or 0), float(row.price_out or 0))
            for row in s.query(ModelEntry).all()
        }
        compactions = int(
            s.query(func.count(Event.id))
            .filter(Event.run_id == run_id, Event.type == "context_compacted").scalar() or 0)
        cached_tokens = int(
            s.query(func.coalesce(func.sum(NodeAttempt.cached_tokens), 0))
            .filter(NodeAttempt.node_id.in_(node_ids)).scalar() or 0) if node_ids else 0
        meeting_total = int(
            s.query(func.count(Meeting.id)).filter(Meeting.run_id == run_id).scalar() or 0)
        meeting_turns = int(
            s.query(func.count(MeetingTurn.id))
            .join(Meeting, Meeting.id == MeetingTurn.meeting_id)
            .filter(Meeting.run_id == run_id).scalar() or 0)
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
    # 确认点按判定者分：模型以 model: 前缀认，程序是 program/proxy，其余算人工，没决定的是开着
    checkpoints = {"human": 0, "program": 0, "model": 0, "open": 0}
    for gate in gates:
        decided_by = str(gate.decided_by or "")
        if gate.status == "open":
            checkpoints["open"] += 1
        elif decided_by.startswith("model:"):
            checkpoints["model"] += 1
        elif decided_by in ("program", "proxy"):
            checkpoints["program"] += 1
        else:
            checkpoints["human"] += 1

    # 评审指标：轮次、一次通过、阻塞/问题分布、评审耗时。
    severities = {"blocker": 0, "major": 0, "minor": 0}
    review_nodes: list[dict] = []
    review_ms: list[int] = []
    for item in review_data:
        rows = by_node.get(item["node_id"], [])
        ms = [_span_ms(a.started_at, a.finished_at) for a in rows]
        review_ms.extend(ms)
        report = item["report"]
        issues = report.get("issues")
        if not isinstance(issues, list):
            issues = []
        for issue in issues:
            severity = str(issue.get("severity") or "minor")
            severities[severity] = severities.get(severity, 0) + 1
        history = item["history"]
        review_nodes.append({
            "node_key": item["node_key"], "name": item["name"],
            "verdict": report.get("verdict"),
            "round": item["round"] or int(report.get("round") or 0),
            "max_rounds": item["max_rounds"] or int(report.get("max_rounds") or 2),
            "issues": len(issues), "blocking": int(report.get("blocking") or 0),
            "summary": str(report.get("summary") or ""), "reviewed_at": report.get("reviewed_at"),
            "attempts": len(rows), "exec_ms": sum(ms),
            "blocks": sum(1 for h in history if str(h.get("verdict")) == "block"),
            "history": history[-8:],
        })
    latest_review = review_nodes[-1] if review_nodes else None
    first_pass = bool(
        latest_review
        and latest_review["verdict"] == "pass"
        and latest_review["round"] == 1
        and latest_review["blocks"] == 0
    )
    rework_count = max(0, (latest_review["round"] - 1)) if latest_review else 0

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
        "context": {
            "compactions": compactions,
            "cached_tokens": cached_tokens,
            "cache_hit_rate": round(cached_tokens / tokens_in, 4) if tokens_in else 0,
        },
        "meetings": {
            "total": meeting_total,
            "turns": meeting_turns,
            "checkpoints": checkpoints,
        },
        "events": events,
        "review": {
            "total": len(review_nodes),
            "verdict": latest_review["verdict"] if latest_review else None,
            "round": latest_review["round"] if latest_review else 0,
            "max_rounds": latest_review["max_rounds"] if latest_review else 0,
            "first_pass": first_pass,
            "rework_count": rework_count,
            "blocks": latest_review["blocks"] if latest_review else 0,
            "issues": latest_review["issues"] if latest_review else 0,
            "blocking": latest_review["blocking"] if latest_review else 0,
            "severities": severities,
            "avg_review_ms": int(sum(review_ms) / len(review_ms)) if review_ms else 0,
            "nodes": review_nodes,
        },
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


@router.get("/templates/{template_key}")
def template_detail(template_key: str) -> dict:
    """模板定义：节点、参数表（硬限、建议范围、默认值）、自动化等级预设、判据文本。"""
    try:
        return workflow_definition(template_key)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/runs/{run_id}/config")
def get_run_config(run_id: str) -> dict:
    run = _run_or_404(run_id)
    return {"run_id": run_id, "status": run["status"], "config": run["config"],
            "editable": run["status"] in EDITABLE_RUN_STATUSES}


@router.patch("/runs/{run_id}/config")
def patch_run_config(run_id: str, payload: RunConfigPatch) -> dict:
    """修改运行级配置。参数只写进还没开始的会议节点；已经开过会的节点沿用它开始时的参数。"""
    run = _run_or_404(run_id)
    if run["status"] not in EDITABLE_RUN_STATUSES:
        raise HTTPException(400, "运行级配置只能在运行开始前或有确认点挂起时修改")
    config = _merge_run_config(run["config"], payload.model_dump(exclude_unset=True))
    definition = workflow_definition(run_of(run_id)["template_key"])
    errors = validate_run_config(definition, config) + _roster_errors(config)
    if errors:
        raise HTTPException(400, "运行级配置不合法：" + "；".join(errors))
    apply_run_config(run_id, definition, config, only_pending=True)
    with session_scope() as s:
        s.get(Run, run_id).config_json = config
    reset_policy_cache(run_id)
    return {"run_id": run_id, "config": config}


@router.get("/runs/{run_id}/materials")
def list_run_materials(run_id: str) -> dict:
    _run_or_404(run_id)
    return {"items": list_materials(run_id), "total_chars": total_chars(run_id),
            "max_chars": settings.materials_max_chars}


@router.post("/runs/{run_id}/materials", status_code=201)
def add_run_material(run_id: str, payload: MaterialIn) -> dict:
    _run_or_404(run_id)
    try:
        return save_material(run_id, payload.name.strip(), payload.content)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/runs/{run_id}/materials/{name}")
def read_run_material(run_id: str, name: str) -> dict:
    _run_or_404(run_id)
    try:
        return {"name": name, "content": read_material(run_id, name)}
    except FileNotFoundError as exc:
        raise HTTPException(404, "材料不存在") from exc
