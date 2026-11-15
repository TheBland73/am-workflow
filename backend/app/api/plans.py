"""编排工作台 API（ORCH-01）：Plan / 生成 / 校验 / 编辑 / 快照 / 提交。"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func

from app.api.schemas import PlanCommit, PlanCreate, PlanTaskCreate, PlanTaskPatch
from app.artifacts.store import latest_artifact, parse_front_matter, read_artifact, write_artifact
from app.config import settings
from app.db.base import session_scope
from app.db.models import (
    ModelEntry,
    Node,
    Plan,
    PlanSnapshot,
    PlanTask,
    Run,
    Task,
    Workflow,
    new_run_id,
    utcnow,
)
from app.engine.graph import compute_waves
from app.engine.orchestrator import orchestrator
from app.engine.plan_bus import plan_bus, sse_format
from app.engine.plan_generation import generate_plan
from app.engine.planner import extract_sections, validate_plan

router = APIRouter(tags=["plans"])

_TASKS: set[asyncio.Task] = set()


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _error(status: int, code: str, message: str, hint: str = "", **details):
    return HTTPException(
        status_code=status,
        detail={"code": code, "message": message, "hint": hint, "details": details},
    )


def _task_dict(task: PlanTask) -> dict:
    return {
        "id": task.id,
        "task_uid": task.task_uid,
        "title": task.title,
        "description": task.description,
        "deps": list(task.deps_json or []),
        "design_section": task.design_section,
        "acceptance": list(task.acceptance_json or []),
        "role_key": task.role_key,
        "effort": task.effort,
        "priority": task.priority,
        "order_index": task.order_index,
    }


def _snapshot_dict(snapshot: PlanSnapshot | None, include_tasks: bool = False) -> dict | None:
    if snapshot is None:
        return None
    data = {
        "version": snapshot.version,
        "metrics": snapshot.metrics_json or {},
        "waves": snapshot.waves_json or {},
        "critical_path": list(snapshot.critical_path_json or []),
        "dag": snapshot.dag_json or {},
        "validation": snapshot.validation_json or {},
        "is_valid": snapshot.is_valid,
        "created_at": _iso(snapshot.created_at),
    }
    if include_tasks:
        data["tasks"] = list(snapshot.tasks_json or [])
    return data


def _plan_dict(plan: Plan) -> dict:
    return {
        "id": plan.id,
        "product_name": plan.product_name,
        "source_type": plan.source_type,
        "source_ref": plan.source_ref,
        "status": plan.status,
        "model_id": plan.model_id,
        "error": plan.error,
        "run_id": plan.run_id,
        "created_at": _iso(plan.created_at),
        "updated_at": _iso(plan.updated_at),
    }


def _load_plan(plan_id: str) -> dict:
    with session_scope() as s:
        plan = s.get(Plan, plan_id)
        if plan is None:
            raise _error(404, "plan_not_found", "计划不存在")
        tasks = (
            s.query(PlanTask)
            .filter(PlanTask.plan_id == plan_id)
            .order_by(PlanTask.order_index.asc(), PlanTask.id.asc())
            .all()
        )
        snapshot = (
            s.query(PlanSnapshot)
            .filter(PlanSnapshot.plan_id == plan_id)
            .order_by(PlanSnapshot.version.desc())
            .first()
        )
        model_name = None
        if plan.model_id:
            model = s.get(ModelEntry, plan.model_id)
            model_name = (model.display_name or model.model_name) if model else None
        return {
            "plan": {
                **_plan_dict(plan),
                "design_text": plan.design_text,
                "raw_output": plan.raw_output,
                "model_name": model_name,
                "sections": list(plan.sections_json or []),
                "budget_tokens": settings.default_budget_tokens,
            },
            "tasks": [_task_dict(task) for task in tasks],
            "snapshot": _snapshot_dict(snapshot),
        }


@router.post("/plans", status_code=201)
def create_plan(payload: PlanCreate) -> dict:
    if payload.source_type not in {"artifact", "upload", "text"}:
        raise _error(400, "source_type", "来源类型必须是 artifact/upload/text")
    design_text = (payload.design_text or "").strip()
    source_ref = payload.source_ref
    if payload.source_type == "artifact":
        if not source_ref:
            raise _error(400, "source_ref", "从工件导入时必须提供 run_id:kind")
        run_id, _, kind = source_ref.partition(":")
        kind = kind or "design"
        record = latest_artifact(run_id, kind)
        if record is None:
            raise _error(400, "artifact_missing", "没有找到可用的设计工件")
        raw = read_artifact(run_id, kind) or ""
        _, design_text = parse_front_matter(raw)
        design_text = design_text.strip()
    if len(design_text) < 200:
        raise _error(400, "design_too_short", "设计规格至少需要 200 字", "粘贴完整设计文档，或从已完成的运行导入 design 工件。")
    plan_id = new_run_id()
    sections = extract_sections(design_text)
    if not sections:
        raise _error(400, "no_sections", "设计规格中没有识别到 Markdown 章节", "用 `## 1 模块划分` 这样的编号标题组织设计。")
    with session_scope() as s:
        s.add(
            Plan(
                id=plan_id,
                product_name=payload.product_name.strip(),
                source_type=payload.source_type,
                source_ref=source_ref,
                design_text=design_text,
                sections_json=sections,
                status="draft",
            )
        )
    return _load_plan(plan_id)


@router.get("/plans")
def list_plans(status: str | None = None, limit: int = 50) -> dict:
    with session_scope() as s:
        query = s.query(Plan)
        if status:
            query = query.filter(Plan.status == status)
        plans = query.order_by(Plan.updated_at.desc()).limit(limit).all()
        items = []
        for plan in plans:
            count = (
                s.query(func.count(PlanTask.id))
                .filter(PlanTask.plan_id == plan.id)
                .scalar()
                or 0
            )
            snapshot = (
                s.query(PlanSnapshot)
                .filter(PlanSnapshot.plan_id == plan.id)
                .order_by(PlanSnapshot.version.desc())
                .first()
            )
            items.append(
                {
                    **_plan_dict(plan),
                    "task_count": count,
                    "metrics": (snapshot.metrics_json if snapshot else {}) or {},
                }
            )
        return {"items": items}


@router.get("/plans/{plan_id}")
def get_plan(plan_id: str) -> dict:
    return _load_plan(plan_id)


@router.post("/plans/{plan_id}/generate", status_code=202)
async def start_generation(plan_id: str) -> dict:
    with session_scope() as s:
        plan = s.get(Plan, plan_id)
        if plan is None:
            raise _error(404, "plan_not_found", "计划不存在")
        if plan.status == "generating":
            raise _error(409, "generating", "该计划正在生成，请等待完成")
        if plan.status in {"committed", "archived"}:
            raise _error(400, "plan_locked", "已提交或已归档的计划不能重新生成")
        plan.status = "generating"
        plan.error = None
        plan.updated_at = utcnow()
    task = asyncio.create_task(generate_plan(plan_id))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return {"id": plan_id, "status": "generating"}


@router.get("/plans/{plan_id}/events")
async def plan_events(plan_id: str, request: Request) -> StreamingResponse:
    with session_scope() as s:
        if s.get(Plan, plan_id) is None:
            raise _error(404, "plan_not_found", "计划不存在")

    async def generator():
        yield ": connected\n\n"
        try:
            async for event in plan_bus.subscribe(plan_id):
                if await request.is_disconnected():
                    break
                yield sse_format(event)
                if event["type"] in {"plan_generated", "plan_failed"}:
                    break
        finally:
            pass

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/plans/{plan_id}/cancel")
async def cancel_plan(plan_id: str) -> dict:
    with session_scope() as s:
        plan = s.get(Plan, plan_id)
        if plan is None:
            raise _error(404, "plan_not_found", "计划不存在")
        if plan.status == "generating":
            plan.status = "draft"
            plan.error = "已取消生成"
            plan.updated_at = utcnow()
    await plan_bus.publish(plan_id, "plan_failed", {"plan_id": plan_id, "error": "已取消"})
    return _load_plan(plan_id)


@router.post("/plans/{plan_id}/validate")
def validate_plan_endpoint(plan_id: str) -> dict:
    with session_scope() as s:
        plan = s.get(Plan, plan_id)
        if plan is None:
            raise _error(404, "plan_not_found", "计划不存在")
        if plan.status in {"committed", "archived"}:
            raise _error(400, "plan_locked", "已提交或已归档的计划不能再次校验")
        rows = (
            s.query(PlanTask)
            .filter(PlanTask.plan_id == plan_id)
            .order_by(PlanTask.order_index.asc(), PlanTask.id.asc())
            .all()
        )
        if not rows:
            raise _error(400, "no_tasks", "计划还没有任务，先运行 PM 分解")
        raw_tasks = [
            {
                "uid": row.task_uid,
                "title": row.title,
                "description": row.description,
                "deps": list(row.deps_json or []),
                "design_section": row.design_section or "",
                "acceptance": list(row.acceptance_json or []),
                "role": row.role_key,
                "effort": row.effort,
                "priority": row.priority,
            }
            for row in rows
        ]
        result = validate_plan(raw_tasks, plan.design_text or "")
        last = (
            s.query(PlanSnapshot)
            .filter(PlanSnapshot.plan_id == plan_id)
            .order_by(PlanSnapshot.version.desc())
            .first()
        )
        version = (last.version + 1) if last else 1
        snapshot_data = result.get("snapshot")
        if snapshot_data:
            s.query(PlanSnapshot).filter(
                PlanSnapshot.plan_id == plan_id, PlanSnapshot.is_valid.is_(True)
            ).update({PlanSnapshot.is_valid: False})
            snapshot = PlanSnapshot(
                plan_id=plan_id,
                version=version,
                tasks_json=result["tasks"],
                dag_json=snapshot_data["dag"],
                waves_json=snapshot_data["waves"],
                critical_path_json=snapshot_data["critical_path"],
                metrics_json=snapshot_data["metrics"],
                validation_json={"ok": True, "errors": []},
                is_valid=True,
            )
            plan.status = "valid"
            plan.error = None
        else:
            snapshot = PlanSnapshot(
                plan_id=plan_id,
                version=version,
                tasks_json=result.get("tasks") or [],
                dag_json={},
                waves_json={},
                critical_path_json=[],
                metrics_json={},
                validation_json={"ok": False, "errors": result["errors"]},
                is_valid=False,
            )
            plan.status = "invalid"
            plan.error = result["errors"][0]["message"] if result["errors"] else "校验未通过"
        s.add(snapshot)
        plan.updated_at = utcnow()
        snapshot_out = _snapshot_dict(snapshot)
    return {
        "status": "valid" if snapshot_data else "invalid",
        "errors": result["errors"],
        "snapshot": snapshot_out,
        "metrics": snapshot_out["metrics"] if snapshot_out else {},
    }


@router.patch("/plans/{plan_id}/tasks/{task_uid}")
def patch_plan_task(plan_id: str, task_uid: str, payload: PlanTaskPatch) -> dict:
    with session_scope() as s:
        plan = s.get(Plan, plan_id)
        if plan is None:
            raise _error(404, "plan_not_found", "计划不存在")
        if plan.status in {"committed", "archived"}:
            raise _error(400, "plan_locked", "已提交或已归档的计划不能编辑")
        task = (
            s.query(PlanTask)
            .filter(PlanTask.plan_id == plan_id, PlanTask.task_uid == task_uid)
            .one_or_none()
        )
        if task is None:
            raise _error(404, "task_not_found", "任务不存在")
        data = payload.model_dump(exclude_unset=True)
        if "deps" in data:
            task.deps_json = data.pop("deps") or []
        if "acceptance" in data:
            task.acceptance_json = data.pop("acceptance") or []
        for key, value in data.items():
            setattr(task, key, value)
        plan.status = "editing"
        plan.updated_at = utcnow()
        return _task_dict(task)


@router.post("/plans/{plan_id}/tasks", status_code=201)
def add_plan_task(plan_id: str, payload: PlanTaskCreate) -> dict:
    with session_scope() as s:
        plan = s.get(Plan, plan_id)
        if plan is None:
            raise _error(404, "plan_not_found", "计划不存在")
        if plan.status in {"committed", "archived"}:
            raise _error(400, "plan_locked", "已提交或已归档的计划不能编辑")
        existing = (
            s.query(PlanTask).filter(PlanTask.plan_id == plan_id).all()
        )
        numbers = []
        for row in existing:
            if row.task_uid.startswith("T") and row.task_uid[1:].isdigit():
                numbers.append(int(row.task_uid[1:]))
        uid = f"T{max(numbers, default=0) + 1}"
        order = max((row.order_index for row in existing), default=-1) + 1
        task = PlanTask(
            plan_id=plan_id,
            task_uid=uid,
            title=payload.title,
            description=payload.description,
            deps_json=payload.deps,
            design_section=payload.design_section,
            acceptance_json=payload.acceptance,
            role_key=payload.role_key,
            effort=payload.effort.upper(),
            priority=payload.priority.upper(),
            order_index=order,
        )
        s.add(task)
        plan.status = "editing"
        plan.updated_at = utcnow()
        s.flush()
        return _task_dict(task)


@router.delete("/plans/{plan_id}/tasks/{task_uid}")
def delete_plan_task(plan_id: str, task_uid: str) -> dict:
    with session_scope() as s:
        plan = s.get(Plan, plan_id)
        if plan is None:
            raise _error(404, "plan_not_found", "计划不存在")
        if plan.status in {"committed", "archived"}:
            raise _error(400, "plan_locked", "已提交或已归档的计划不能编辑")
        task = (
            s.query(PlanTask)
            .filter(PlanTask.plan_id == plan_id, PlanTask.task_uid == task_uid)
            .one_or_none()
        )
        if task is None:
            raise _error(404, "task_not_found", "任务不存在")
        s.delete(task)
        affected: list[str] = []
        others = (
            s.query(PlanTask)
            .filter(PlanTask.plan_id == plan_id, PlanTask.task_uid != task_uid)
            .all()
        )
        for other in others:
            deps = list(other.deps_json or [])
            if task_uid in deps:
                deps = [dep for dep in deps if dep != task_uid]
                other.deps_json = deps
                affected.append(other.task_uid)
        plan.status = "editing"
        plan.updated_at = utcnow()
    return {"deleted": task_uid, "removed_from": affected}


@router.get("/plans/{plan_id}/snapshots")
def list_snapshots(plan_id: str) -> dict:
    with session_scope() as s:
        if s.get(Plan, plan_id) is None:
            raise _error(404, "plan_not_found", "计划不存在")
        rows = (
            s.query(PlanSnapshot)
            .filter(PlanSnapshot.plan_id == plan_id)
            .order_by(PlanSnapshot.version.desc())
            .all()
        )
        return {
            "items": [
                {
                    "version": row.version,
                    "metrics": row.metrics_json or {},
                    "is_valid": row.is_valid,
                    "validation": row.validation_json or {},
                    "created_at": _iso(row.created_at),
                }
                for row in rows
            ]
        }


@router.post("/plans/{plan_id}/restore/{version}")
def restore_snapshot(plan_id: str, version: int) -> dict:
    with session_scope() as s:
        plan = s.get(Plan, plan_id)
        if plan is None:
            raise _error(404, "plan_not_found", "计划不存在")
        if plan.status in {"committed", "archived"}:
            raise _error(400, "plan_locked", "已提交或已归档的计划不能恢复快照")
        snapshot = (
            s.query(PlanSnapshot)
            .filter(PlanSnapshot.plan_id == plan_id, PlanSnapshot.version == version)
            .one_or_none()
        )
        if snapshot is None:
            raise _error(404, "snapshot_not_found", "快照不存在")
        s.query(PlanTask).filter(PlanTask.plan_id == plan_id).delete()
        for index, task in enumerate(snapshot.tasks_json or []):
            s.add(
                PlanTask(
                    plan_id=plan_id,
                    task_uid=task.get("task_uid"),
                    title=task.get("title") or "",
                    description=task.get("description") or "",
                    deps_json=task.get("deps") or [],
                    design_section=task.get("design_section"),
                    acceptance_json=task.get("acceptance") or [],
                    role_key=task.get("role_key") or "dev",
                    effort=task.get("effort") or "M",
                    priority=task.get("priority") or "P1",
                    order_index=index,
                )
            )
        plan.status = "editing"
        plan.updated_at = utcnow()
    return _load_plan(plan_id)


@router.post("/plans/{plan_id}/archive")
def archive_plan(plan_id: str) -> dict:
    with session_scope() as s:
        plan = s.get(Plan, plan_id)
        if plan is None:
            raise _error(404, "plan_not_found", "计划不存在")
        if plan.status == "generating":
            raise _error(400, "generating", "生成中的计划不能归档，先取消")
        plan.status = "archived"
        plan.updated_at = utcnow()
    return _load_plan(plan_id)


@router.post("/plans/{plan_id}/commit")
async def commit_plan(plan_id: str, payload: PlanCommit) -> dict:
    if payload.mode not in {"save", "run"}:
        raise _error(400, "mode", "提交模式必须是 save 或 run")
    run_id: str | None = None
    created_run = False
    design_text = ""
    with session_scope() as s:
        plan = s.get(Plan, plan_id)
        if plan is None:
            raise _error(404, "plan_not_found", "计划不存在")
        if plan.status != "committed":
            if plan.status != "valid":
                raise _error(
                    400,
                    "plan_not_valid",
                    "只有校验通过的计划才能提交",
                    "先修复校验错误并重新校验。",
                )
            if payload.mode == "save":
                plan.status = "committed"
                plan.updated_at = utcnow()
            else:
                workflow = (
                    s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").first()
                )
                if workflow is None:
                    raise _error(400, "workflow_missing", "默认工作流尚未初始化")
                run_id = new_run_id()
                design_text = plan.design_text or ""
                rows = (
                    s.query(PlanTask)
                    .filter(PlanTask.plan_id == plan_id)
                    .order_by(PlanTask.order_index.asc())
                    .all()
                )
                deps_map = {row.task_uid: list(row.deps_json or []) for row in rows}
                waves = compute_waves(deps_map)
                run = Run(
                    id=run_id,
                    workflow_id=workflow.id,
                    status="created",
                    current_phase="execution",
                    idea_text=f"编排工作台提交：{plan.product_name}（{len(rows)} 个任务）",
                    budget_tokens=settings.default_budget_tokens,
                )
                s.add(run)
                s.flush()
                for row in rows:
                    s.add(
                        Task(
                            run_id=run_id,
                            task_uid=row.task_uid,
                            title=row.title,
                            deps_json=list(row.deps_json or []),
                            design_section=row.design_section,
                            test_cases_json=list(row.acceptance_json or []),
                            assignee_json={"role_key": row.role_key},
                            wave=waves.get(row.task_uid, 1),
                            status="pending",
                        )
                    )
                dev_keys: list[str] = []
                for index, row in enumerate(rows):
                    key = f"dev_{row.task_uid}"
                    dev_keys.append(key)
                    s.add(
                        Node(
                            run_id=run_id,
                            node_key=key,
                            type="AGENT",
                            name=f"开发 {row.task_uid} {row.title}",
                            phase="execution",
                            status="pending",
                            assignee_json={"role_key": row.role_key},
                            deps_json=[f"dev_{dep}" for dep in (row.deps_json or [])],
                            config_json={
                                "task_uid": row.task_uid,
                                "task_title": row.title,
                                "design_section": row.design_section,
                            },
                            max_attempts=3,
                            order_index=index,
                            wave=waves.get(row.task_uid, 1),
                        )
                    )
                if dev_keys:
                    s.add(
                        Node(
                            run_id=run_id,
                            node_key="verify",
                            type="VERIFY",
                            name="验证测试",
                            phase="execution",
                            status="pending",
                            deps_json=dev_keys,
                            config_json={
                                "command": 'python -m unittest discover -s . -p "test_*.py"'
                            },
                            max_attempts=2,
                            order_index=len(rows),
                            wave=max(waves.values(), default=1) + 1,
                        )
                    )
                plan.status = "committed"
                plan.run_id = run_id
                plan.updated_at = utcnow()
                created_run = True
        run_id = plan.run_id or run_id
    if created_run and run_id:
        write_artifact(
            run_id,
            "design",
            design_text,
            created_by="plan",
            inputs=["plan"],
            status="approved",
        )
        orchestrator.start(run_id)
    result = _load_plan(plan_id)
    return {**result, "run_id": run_id}
