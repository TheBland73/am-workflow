"""记忆查看与人工修订（设计规格 §6）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.schemas import MemoryPatch
from app.db.base import session_scope
from app.db.models import Memory
from app.memory.store import list_memories

router = APIRouter(tags=["memories"])


@router.get("/memories")
def get_memories(scope_type: str | None = None, scope_id: str | None = None,
                 status: str = "active", limit: int = 100) -> dict:
    return {"items": list_memories(scope_type=scope_type, scope_id=scope_id,
                                   status=status, limit=limit)}


@router.patch("/memories/{memory_id}")
def update_memory(memory_id: int, payload: MemoryPatch) -> dict:
    with session_scope() as s:
        row = s.get(Memory, memory_id)
        if row is None:
            raise HTTPException(404, "记忆不存在")
        for key, value in payload.model_dump(exclude_unset=True).items():
            if value is not None:
                setattr(row, key, value)
        return {
            "id": row.id, "scope_type": row.scope_type, "scope_id": row.scope_id,
            "kind": row.kind, "subject": row.subject, "content": row.content,
            "status": row.status,
        }
