"""角色模板 API（AgentTemplate）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.schemas import TemplatePatch
from app.db.base import session_scope
from app.db.models import AgentTemplate, ModelEntry

router = APIRouter(tags=["agent-templates"])


def _serialize(row: AgentTemplate) -> dict:
    return {
        "id": row.id,
        "role_key": row.role_key,
        "name": row.name,
        "system_prompt": row.system_prompt,
        "tools_json": list(row.tools_json or []),
        "memory_scope": row.memory_scope,
        "default_model_id": row.default_model_id,
        "max_steps": row.max_steps,
        "stop_condition": list(row.stop_condition or []),
    }


@router.get("/agent-templates")
def list_templates() -> dict:
    with session_scope() as s:
        rows = s.query(AgentTemplate).order_by(AgentTemplate.id.asc()).all()
        return {"items": [_serialize(r) for r in rows]}


@router.patch("/agent-templates/{template_id}")
def update_template(template_id: int, payload: TemplatePatch) -> dict:
    with session_scope() as s:
        row = s.get(AgentTemplate, template_id)
        if row is None:
            raise HTTPException(404, "角色模板不存在")
        data = payload.model_dump(exclude_unset=True)
        if data.get("default_model_id"):
            if s.get(ModelEntry, data["default_model_id"]) is None:
                raise HTTPException(400, "指定的模型不存在")
        for key, value in data.items():
            setattr(row, key, value)
        return _serialize(row)
