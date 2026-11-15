"""供应商配置 API（设计规格 §5.5 / 模型接入配置设计 §4）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import func

from app.api.schemas import ProviderIn, ProviderPatch
from app.db.base import session_scope
from app.db.models import ModelEntry, ProviderConfig
from app.models.crypto import decrypt_key, encrypt_key, mask_key
from app.models.pool import pool
from app.models.providers.presets import PRESETS
from app.models.types import ProviderError

router = APIRouter(tags=["providers"])


def _serialize(row: ProviderConfig, models_count: int = 0) -> dict:
    key = decrypt_key(row.api_key_enc)
    return {
        "id": row.id,
        "name": row.name,
        "protocol": row.protocol,
        "base_url": row.base_url,
        "api_key_masked": mask_key(key) if key else "",
        "has_key": bool(key),
        "extra_headers": dict(row.extra_headers or {}),
        "timeout_s": row.timeout_s,
        "max_concurrency": row.max_concurrency,
        "enabled": row.enabled,
        "models_count": models_count,
    }


@router.get("/presets")
def list_presets() -> dict:
    return {"presets": PRESETS}


@router.get("/providers")
def list_providers() -> dict:
    with session_scope() as s:
        rows = s.query(ProviderConfig).order_by(ProviderConfig.id.asc()).all()
        counts = {
            pid: cnt
            for pid, cnt in (
                s.query(ModelEntry.provider_id, func.count(ModelEntry.id))
                .group_by(ModelEntry.provider_id)
                .all()
            )
        }
        return {"items": [_serialize(r, counts.get(r.id, 0)) for r in rows]}


@router.post("/providers", status_code=201)
def create_provider(payload: ProviderIn) -> dict:
    with session_scope() as s:
        if s.query(ProviderConfig).filter(ProviderConfig.name == payload.name).first():
            raise HTTPException(400, "供应商名称已存在")
        row = ProviderConfig(
            name=payload.name, protocol=payload.protocol, base_url=payload.base_url,
            api_key_enc=encrypt_key(payload.api_key) if payload.api_key else None,
            extra_headers=payload.extra_headers, timeout_s=payload.timeout_s,
            max_concurrency=payload.max_concurrency, enabled=payload.enabled,
        )
        s.add(row)
        s.flush()
        return _serialize(row)


@router.patch("/providers/{provider_id}")
def update_provider(provider_id: int, payload: ProviderPatch) -> dict:
    with session_scope() as s:
        row = s.get(ProviderConfig, provider_id)
        if row is None:
            raise HTTPException(404, "供应商不存在")
        data = payload.model_dump(exclude_unset=True)
        if "api_key" in data:
            api_key = data.pop("api_key")
            row.api_key_enc = encrypt_key(api_key) if api_key else None
        for key, value in data.items():
            setattr(row, key, value)
        return _serialize(row)


@router.delete("/providers/{provider_id}", status_code=204)
def delete_provider(provider_id: int) -> None:
    with session_scope() as s:
        row = s.get(ProviderConfig, provider_id)
        if row is None:
            raise HTTPException(404, "供应商不存在")
        s.delete(row)


@router.post("/providers/{provider_id}/test")
async def test_provider(provider_id: int, model_id: int | None = None) -> dict:
    try:
        result = await pool.test_connection(provider_id, model_id)
        return result
    except ProviderError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # 网络等异常也转成可读信息
        raise HTTPException(400, f"测试连接失败：{exc}") from exc
