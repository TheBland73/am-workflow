"""角色模板 API（AgentTemplate）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.schemas import TemplateCreate, TemplatePatch
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
        "builtin": bool(row.builtin_hash),
        "meta": dict(row.meta_json or {}),
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
        data = payload.model_dump(exclude_unset=True, exclude_none=True)
        meta = data.pop("meta", None)
        if "default_model_id" in data:
            if s.get(ModelEntry, data["default_model_id"]) is None:
                raise HTTPException(400, "指定的模型不存在")
        if meta is not None:
            row.meta_json = dict(meta)
        for key, value in data.items():
            setattr(row, key, value)
        return _serialize(row)


@router.post("/agent-templates", status_code=201)
def create_template(payload: TemplateCreate) -> dict:
    with session_scope() as s:
        if s.query(AgentTemplate).filter(AgentTemplate.role_key == payload.role_key).first():
            raise HTTPException(400, f"角色键已存在：{payload.role_key}")
        if payload.default_model_id is not None and s.get(
                ModelEntry, payload.default_model_id) is None:
            raise HTTPException(400, "指定的模型不存在")
        row = AgentTemplate(
            role_key=payload.role_key, name=payload.name, system_prompt=payload.system_prompt,
            tools_json=list(payload.tools_json), memory_scope=payload.memory_scope,
            max_steps=payload.max_steps, default_model_id=payload.default_model_id,
            stop_condition=[], meta_json=dict(payload.meta), builtin_hash=None)
        s.add(row)
        s.flush()
        return _serialize(row)


@router.delete("/agent-templates/{template_id}")
def delete_template(template_id: int) -> dict:
    with session_scope() as s:
        row = s.get(AgentTemplate, template_id)
        if row is None:
            raise HTTPException(404, "角色模板不存在")
        if row.builtin_hash:
            raise HTTPException(400, "内置角色只能编辑，不能删除")
        s.delete(row)
        s.flush()
    return {"deleted": template_id}
