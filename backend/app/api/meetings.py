"""会议的查询与介入接口。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func

from app.db.base import session_scope
from app.db.models import (MEETING_ABANDONED, MEETING_COMPLETED, AgentTemplate, Gate, Meeting,
                           MeetingIntervention, ModelEntry, Node)
from app.engine.events import emit
from app.engine.gates import gate_payload
from app.meeting import store

router = APIRouter(tags=["meetings"])

EDITABLE_MODES = ("participant", "reviewer", "quality_reviewer")
ROSTER_PHASES = ("prepare", "roster")
EDITABLE_POINT_STATUSES = ("open", "agreed", "deferred")
MAX_QUEUED_INTERVENTIONS = 20


class ParticipantIn(BaseModel):
    role_key: str | None = None
    display_name: str = Field(min_length=1, max_length=120)
    persona: str = Field(default="", max_length=4000)   # 临时角色的人设；有 role_key 时忽略
    model_id: int | None = None
    mode: str = "participant"


class RosterIn(BaseModel):
    participants: list[ParticipantIn]


class PointPatch(BaseModel):
    status: str | None = None
    conclusion: str | None = Field(default=None, max_length=4000)


class InterveneIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


def _meeting_or_404(run_id: str, meeting_id: int) -> dict:
    try:
        meeting = store.get_meeting(meeting_id)
    except LookupError as exc:
        raise HTTPException(404, "会议不存在") from exc
    if meeting["run_id"] != run_id:
        raise HTTPException(404, "会议不存在")
    return meeting


def _summary(meeting: dict) -> dict:
    return {"id": meeting["id"], "run_id": meeting["run_id"], "node_id": meeting["node_id"],
            "node_key": meeting["node_key"], "protocol": meeting["protocol"],
            "status": meeting["status"], "phase": meeting["phase"], "round": meeting["round"]}


@router.get("/runs/{run_id}/meetings")
def list_meetings(run_id: str) -> dict:
    with session_scope() as s:
        ids = [row.id for row in s.query(Meeting).filter(Meeting.run_id == run_id)
               .order_by(Meeting.id.asc()).all()]
    return {"items": [_summary(store.get_meeting(m)) for m in ids]}


@router.get("/runs/{run_id}/meetings/{meeting_id}")
def meeting_detail(run_id: str, meeting_id: int) -> dict:
    meeting = _meeting_or_404(run_id, meeting_id)
    with session_scope() as s:
        node = s.get(Node, meeting["node_id"])
        node_status = node.status if node else None
        checkpoints = [gate_payload(s, g) for g in
                       s.query(Gate).filter(Gate.node_id == meeting["node_id"])
                       .order_by(Gate.id.asc()).all()]
    return {"meeting": _summary(meeting), "participants": store.list_participants(meeting_id, False),
            "decision_points": store.list_decision_points(meeting_id),
            "checkpoints": checkpoints, "turn_count": store.count_turns(meeting_id),
            "node_status": node_status}


@router.get("/runs/{run_id}/meetings/{meeting_id}/turns")
def meeting_turns(run_id: str, meeting_id: int, since_seq: int = 0, limit: int = 200) -> dict:
    _meeting_or_404(run_id, meeting_id)
    items = store.list_turns(meeting_id, since_seq=since_seq, limit=max(1, min(limit, 1000)))
    return {"items": items, "last_seq": items[-1]["seq"] if items else since_seq}


@router.put("/runs/{run_id}/meetings/{meeting_id}/participants")
async def replace_participants(run_id: str, meeting_id: int, payload: RosterIn) -> dict:
    meeting = _meeting_or_404(run_id, meeting_id)
    if meeting["phase"] not in ROSTER_PHASES:
        raise HTTPException(400, "名单只能在会前准备和名单确认两步修改")
    # 先把请求全部校验完，再一次事务替换：任何一条不合法都不动库
    prompts: list[str] = []
    with session_scope() as s:
        for item in payload.participants:
            if item.mode not in EDITABLE_MODES:
                raise HTTPException(400, f"不能指定模式为 {item.mode} 的角色")
            if item.model_id is not None:
                model = s.get(ModelEntry, item.model_id)
                if model is None or not model.enabled:
                    raise HTTPException(400, f"模型不存在或已禁用：{item.model_id}")
            prompt = item.persona
            if item.role_key:
                template = (s.query(AgentTemplate)
                            .filter(AgentTemplate.role_key == item.role_key).first())
                if template is None:
                    raise HTTPException(400, f"角色模板不存在：{item.role_key}")
                prompt = template.system_prompt
            prompts.append(prompt)
    store.replace_participants(meeting_id, [
        {"role_key": item.role_key, "display_name": item.display_name, "mode": item.mode,
         "system_prompt": prompt, "model_id": item.model_id}
        for item, prompt in zip(payload.participants, prompts)], EDITABLE_MODES)
    await emit(run_id, "meeting_ledger_updated", meeting["node_id"],
               {"meeting_id": meeting_id, "version": 0, "decision_points": [], "roster": True})
    return {"meeting_id": meeting_id, "participants": store.list_participants(meeting_id)}


@router.patch("/runs/{run_id}/meetings/{meeting_id}/decision-points/{point_id}")
def patch_decision_point(run_id: str, meeting_id: int, point_id: int,
                         payload: PointPatch) -> dict:
    meeting = _meeting_or_404(run_id, meeting_id)
    if meeting["status"] != "suspended":
        raise HTTPException(400, "决策点只能在会议挂起时修改")
    points = store.list_decision_points(meeting_id)
    if not any(p["id"] == point_id for p in points):
        raise HTTPException(404, "决策点不存在")
    changes: dict = {}
    if payload.status is not None:
        if payload.status not in EDITABLE_POINT_STATUSES:
            raise HTTPException(400, f"状态只能是 {EDITABLE_POINT_STATUSES}")
        changes["status"] = payload.status
    if payload.conclusion is not None:
        changes["conclusion"] = payload.conclusion
    if changes:
        store.update_decision_point(point_id, **changes)
    return next(p for p in store.list_decision_points(meeting_id) if p["id"] == point_id)


@router.post("/runs/{run_id}/meetings/{meeting_id}/intervene", status_code=201)
async def intervene(run_id: str, meeting_id: int, payload: InterveneIn) -> dict:
    meeting = _meeting_or_404(run_id, meeting_id)
    if meeting["status"] in (MEETING_COMPLETED, MEETING_ABANDONED):
        raise HTTPException(400, "会议已结束，不能再插话")
    with session_scope() as s:
        queued = s.query(func.count(MeetingIntervention.id)).filter(
            MeetingIntervention.meeting_id == meeting_id,
            MeetingIntervention.status == "queued").scalar() or 0
    if queued >= MAX_QUEUED_INTERVENTIONS:
        raise HTTPException(400, "排队中的插话过多，请等会议处理后再提交")
    intervention_id = store.queue_intervention(meeting_id, content=payload.content)
    await emit(run_id, "meeting_intervention_queued", meeting["node_id"],
               {"meeting_id": meeting_id, "intervention_id": intervention_id})
    return {"id": intervention_id, "status": "queued"}
