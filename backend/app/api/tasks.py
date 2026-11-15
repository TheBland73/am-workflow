"""任务图人工维护 API（设计规格 §5.5、§5.6）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.schemas import TaskIn, TaskPatch
from app.db.base import session_scope
from app.db.models import Run, Task
from app.engine.events import emit
from app.engine.graph import GraphError, compute_waves
from app.engine.orchestrator import orchestrator

router = APIRouter(tags=["tasks"])


def _task_dict(t: Task) -> dict:
    return {
        "task_uid": t.task_uid, "title": t.title, "deps": list(t.deps_json or []),
        "design_section": t.design_section, "test_cases": list(t.test_cases_json or []),
        "assignee": t.assignee_json or {}, "wave": t.wave, "status": t.status,
    }


def _rewave(s, run_id: str) -> None:
    rows = s.query(Task).filter(Task.run_id == run_id).all()
    if not rows:
        return
    deps_map = {r.task_uid: list(r.deps_json or []) for r in rows}
    waves = compute_waves(deps_map)
    for row in rows:
        row.wave = waves[row.task_uid]


@router.get("/runs/{run_id}/tasks")
def list_tasks(run_id: str) -> dict:
    with session_scope() as s:
        rows = s.query(Task).filter(Task.run_id == run_id).order_by(Task.wave, Task.id).all()
        return {"items": [_task_dict(t) for t in rows]}


@router.post("/runs/{run_id}/tasks", status_code=201)
async def create_task(run_id: str, payload: TaskIn) -> dict:
    with session_scope() as s:
        if s.get(Run, run_id) is None:
            raise HTTPException(404, "run 不存在")
        if s.query(Task).filter(Task.run_id == run_id, Task.task_uid == payload.task_uid).first():
            raise HTTPException(400, "任务 id 已存在")
        row = Task(
            run_id=run_id, task_uid=payload.task_uid, title=payload.title,
            deps_json=payload.deps, design_section=payload.design_section,
            test_cases_json=payload.test_cases,
            assignee_json={"role_key": payload.assignee or "dev"},
            wave=payload.wave or 0, status=payload.status,
        )
        s.add(row)
        try:
            s.flush()
            _rewave(s, run_id)
        except GraphError as exc:
            raise HTTPException(400, f"依赖非法：{exc}") from exc
    await emit(run_id, "task_updated", None, {"action": "created", "task_uid": payload.task_uid})
    return _task_dict(row)


@router.patch("/runs/{run_id}/tasks/{task_uid}")
async def update_task(run_id: str, task_uid: str, payload: TaskPatch) -> dict:
    with session_scope() as s:
        row = s.query(Task).filter(Task.run_id == run_id, Task.task_uid == task_uid).first()
        if row is None:
            raise HTTPException(404, "任务不存在")
        data = payload.model_dump(exclude_unset=True)
        if "deps" in data and data["deps"] is not None:
            row.deps_json = data.pop("deps")
        if "test_cases" in data and data["test_cases"] is not None:
            row.test_cases_json = data.pop("test_cases")
        if "assignee" in data and data["assignee"] is not None:
            row.assignee_json = {"role_key": data.pop("assignee")}
        for key, value in data.items():
            if value is not None:
                setattr(row, key, value)
        try:
            _rewave(s, run_id)
        except GraphError as exc:
            raise HTTPException(400, f"依赖非法：{exc}") from exc
        s.flush()
        result = _task_dict(row)
    await emit(run_id, "task_updated", None, {"action": "updated", **result})
    return result


@router.delete("/runs/{run_id}/tasks/{task_uid}")
async def delete_task(run_id: str, task_uid: str) -> dict:
    with session_scope() as s:
        row = s.query(Task).filter(Task.run_id == run_id, Task.task_uid == task_uid).first()
        if row is None:
            raise HTTPException(404, "任务不存在")
        dependents = [
            t.task_uid for t in s.query(Task).filter(Task.run_id == run_id).all()
            if task_uid in (t.deps_json or [])
        ]
        if dependents:
            raise HTTPException(400, f"任务被依赖，不能删除：{', '.join(dependents)}")
        s.delete(row)
    await emit(run_id, "task_updated", None, {"action": "deleted", "task_uid": task_uid})
    orchestrator.kick(run_id)
    return {"task_uid": task_uid, "deleted": True}
