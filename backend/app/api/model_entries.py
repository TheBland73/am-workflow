"""模型条目 API（Provider → Model → Binding）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.schemas import ModelIn, ModelPatch
from app.db.base import session_scope
from app.db.models import ModelEntry, ProviderConfig

router = APIRouter(tags=["models"])


def _serialize(row: ModelEntry) -> dict:
    return {
        "id": row.id,
        "provider_id": row.provider_id,
        "model_name": row.model_name,
        "display_name": row.display_name or row.model_name,
        "context_window": row.context_window,
        "max_output_tokens": row.max_output_tokens,
        "cap_tools": row.cap_tools,
        "cap_json": row.cap_json,
        "cap_vision": row.cap_vision,
        "price_in": row.price_in,
        "price_out": row.price_out,
        "enabled": row.enabled,
    }


@router.get("/models")
def list_models(provider_id: int | None = None) -> dict:
    with session_scope() as s:
        query = s.query(ModelEntry)
        if provider_id:
            query = query.filter(ModelEntry.provider_id == provider_id)
        rows = query.order_by(ModelEntry.id.asc()).all()
        return {"items": [_serialize(r) for r in rows]}


@router.post("/models", status_code=201)
def create_model(payload: ModelIn) -> dict:
    with session_scope() as s:
        provider = s.get(ProviderConfig, payload.provider_id)
        if provider is None:
            raise HTTPException(400, "供应商不存在")
        row = ModelEntry(**payload.model_dump())
        s.add(row)
        s.flush()
        return _serialize(row)


@router.patch("/models/{model_id}")
def update_model(model_id: int, payload: ModelPatch) -> dict:
    with session_scope() as s:
        row = s.get(ModelEntry, model_id)
        if row is None:
            raise HTTPException(404, "模型不存在")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(row, key, value)
        return _serialize(row)


@router.delete("/models/{model_id}", status_code=204)
def delete_model(model_id: int) -> None:
    with session_scope() as s:
        row = s.get(ModelEntry, model_id)
        if row is None:
            raise HTTPException(404, "模型不存在")
        s.delete(row)
