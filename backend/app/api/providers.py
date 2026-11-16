"""供应商配置 API（设计规格 §5.5 / 模型接入配置设计 §4）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import func

from app.api.schemas import ModelImportIn, ProviderIn, ProviderPatch
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


def _preset_specs(provider: ProviderConfig) -> dict[str, dict]:
    """按 base_url 找到预设，返回 {model_name: 模型元数据}；找不到返回空字典。"""
    base = (provider.base_url or "").rstrip("/")
    for preset in PRESETS:
        if (preset.get("base_url") or "").rstrip("/") == base and base:
            return {m["model_name"]: m for m in preset.get("models") or []}
    return {}


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


def _seed_preset_models(s, provider: ProviderConfig, preset_key: str | None) -> int:
    """按预设补建模型；只补不覆盖，已存在的同名模型跳过。"""
    if not preset_key:
        return 0
    preset = next((p for p in PRESETS if p.get("key") == preset_key), None)
    if preset is None:
        return 0
    existing = {
        name for (name,) in
        s.query(ModelEntry.model_name).filter(ModelEntry.provider_id == provider.id).all()
    }
    created = 0
    for spec in preset.get("models") or []:
        name = spec.get("model_name")
        if not name or name in existing:
            continue
        s.add(ModelEntry(
            provider_id=provider.id, model_name=name,
            display_name=spec.get("display_name") or name,
            context_window=int(spec.get("context_window") or 32768),
            max_output_tokens=int(spec.get("max_output_tokens") or 4096),
            cap_tools=bool(spec.get("cap_tools", True)),
            cap_json=bool(spec.get("cap_json", True)),
            cap_vision=bool(spec.get("cap_vision", False)),
        ))
        created += 1
    return created


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
        created = _seed_preset_models(s, row, payload.preset_key)
        return {**_serialize(row, created), "models_created": created}


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


@router.get("/providers/{provider_id}/models")
async def list_remote_models(provider_id: int) -> dict:
    """向供应商拉取可选模型列表（openai_chat 走 GET /models）。"""
    try:
        items = await pool.list_models(provider_id)
        return {"items": items, "count": len(items)}
    except ProviderError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(400, f"拉取模型列表失败：{exc}") from exc


@router.post("/providers/{provider_id}/models/import", status_code=201)
def import_models(provider_id: int, payload: ModelImportIn) -> dict:
    """把选中的远程模型写入本地模型表；预设里有元数据的带上上下文/能力。"""
    with session_scope() as s:
        provider = s.get(ProviderConfig, provider_id)
        if provider is None:
            raise HTTPException(404, "供应商不存在")
        specs = _preset_specs(provider)
        existing = {
            name for (name,) in
            s.query(ModelEntry.model_name).filter(ModelEntry.provider_id == provider_id).all()
        }
        created: list[dict] = []
        skipped: list[str] = []
        for name in dict.fromkeys(payload.models):
            name = name.strip()
            if not name:
                continue
            if name in existing:
                skipped.append(name)
                continue
            spec = specs.get(name, {})
            row = ModelEntry(
                provider_id=provider_id, model_name=name,
                display_name=spec.get("display_name") or name,
                context_window=int(spec.get("context_window") or 32768),
                max_output_tokens=int(spec.get("max_output_tokens") or 4096),
                cap_tools=bool(spec.get("cap_tools", True)),
                cap_json=bool(spec.get("cap_json", True)),
                cap_vision=bool(spec.get("cap_vision", False)),
            )
            s.add(row)
            s.flush()
            created.append({"id": row.id, "model_name": row.model_name})
            existing.add(name)
        return {"created": created, "skipped": skipped}


@router.post("/providers/{provider_id}/test")
async def test_provider(provider_id: int, model_id: int | None = None) -> dict:
    try:
        result = await pool.test_connection(provider_id, model_id)
        return result
    except ProviderError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # 网络等异常也转成可读信息
        raise HTTPException(400, f"测试连接失败：{exc}") from exc
