"""工件读取与人工编辑（设计规格 §5.5）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.schemas import ArtifactPatch
from app.artifacts.store import (
    ARTIFACT_KINDS,
    latest_artifact,
    list_artifacts,
    parse_front_matter,
    read_artifact,
    write_artifact,
)
from app.db.base import session_scope
from app.db.models import Run
from app.engine.events import emit
from app.engine.orchestrator import orchestrator

router = APIRouter(tags=["artifacts"])


def _ensure_run(run_id: str) -> None:
    with session_scope() as s:
        if s.get(Run, run_id) is None:
            raise HTTPException(404, "run 不存在")


@router.get("/runs/{run_id}/artifacts")
def get_artifacts(run_id: str) -> dict:
    _ensure_run(run_id)
    return {"items": list_artifacts(run_id)}


@router.get("/runs/{run_id}/artifacts/{kind}")
def get_artifact(run_id: str, kind: str) -> dict:
    _ensure_run(run_id)
    if kind not in ARTIFACT_KINDS:
        raise HTTPException(404, f"未知工件类型：{kind}")
    raw = read_artifact(run_id, kind)
    if raw is None:
        raise HTTPException(404, "该工件尚未生成")
    meta, body = parse_front_matter(raw)
    record = latest_artifact(run_id, kind) or {}
    return {"kind": kind, "meta": meta, "body": body, **record}


@router.patch("/runs/{run_id}/artifacts/{kind}")
async def edit_artifact(run_id: str, kind: str, payload: ArtifactPatch) -> dict:
    _ensure_run(run_id)
    if kind not in ARTIFACT_KINDS:
        raise HTTPException(404, f"未知工件类型：{kind}")
    status = payload.status if payload.status in ("draft", "reviewed", "approved") else "reviewed"
    meta, body = parse_front_matter(payload.body)
    body = body if body.strip() else payload.body
    record = write_artifact(run_id, kind, body, created_by="user", inputs=meta.get("inputs") or [],
                            status=status)
    await emit(run_id, "artifact_written", None, record)
    orchestrator.kick(run_id)
    return record
