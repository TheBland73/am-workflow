"""四层记忆：L0 会话 / L1 角色经验卡 / L2 项目 / L3 全局（设计规格 §5.8）。

M0 不做向量：scope 过滤 + LIKE 检索；失效不删除，只置 superseded/valid_until。
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.db.base import session_scope
from app.db.models import Memory


def add_memory(*, scope_type: str, scope_id: str, kind: str, subject: str, content: str,
               evidence: dict | None = None, created_by: str | None = None,
               source_event: int | None = None) -> int:
    """Reconcile：同 scope/subject 的旧记忆置为 superseded，再写入新记忆。"""
    evidence = evidence or {}
    with session_scope() as s:
        olds = (
            s.query(Memory)
            .filter(
                Memory.scope_type == scope_type,
                Memory.scope_id == scope_id,
                Memory.subject == subject,
                Memory.status == "active",
            )
            .all()
        )
        for old in olds:
            if old.content.strip() == content.strip():
                return old.id
            old.status = "superseded"
            old.valid_until = datetime.now(timezone.utc)
        row = Memory(
            scope_type=scope_type, scope_id=scope_id, kind=kind, subject=subject,
            content=content, evidence_json=evidence, created_by=created_by,
            source_event=source_event, status="active",
        )
        s.add(row)
        s.flush()
        return row.id


def record_lesson(*, role_key: str, run_id: str, subject: str, content: str,
                  event_seq: int | None = None, created_by: str | None = None) -> int:
    return add_memory(
        scope_type="agent", scope_id=role_key, kind="procedural", subject=subject,
        content=content, evidence={"run_id": run_id, "event_seq": event_seq},
        source_event=event_seq, created_by=created_by,
    )


def list_memories(*, scope_type: str | None = None, scope_id: str | None = None,
                  status: str = "active", limit: int = 100) -> list[dict]:
    with session_scope() as s:
        query = s.query(Memory)
        if scope_type:
            query = query.filter(Memory.scope_type == scope_type)
        if scope_id:
            query = query.filter(Memory.scope_id == scope_id)
        if status:
            query = query.filter(Memory.status == status)
        rows = query.order_by(Memory.id.desc()).limit(limit).all()
        return [
            {
                "id": r.id, "scope_type": r.scope_type, "scope_id": r.scope_id,
                "kind": r.kind, "subject": r.subject, "content": r.content,
                "status": r.status, "created_by": r.created_by,
                "created_at": r.created_at.isoformat() if r.created_at else "",
            }
            for r in rows
        ]


def memory_block(*, role_key: str, run_id: str, limit: int = 6) -> str:
    """检索顺序：角色私有经验卡（L1）→ 本项目事实（L2）。"""
    lines: list[str] = []
    l1 = list_memories(scope_type="agent", scope_id=role_key, limit=limit)
    if l1:
        lines.append("### 角色经验卡（历史，仅作背景，不得覆盖本次事实）")
        lines.extend(f"- {m['subject']}：{m['content']}" for m in l1)
    l2 = list_memories(scope_type="run", scope_id=run_id, limit=limit)
    if l2:
        lines.append("### 本项目已确认事实")
        lines.extend(f"- {m['subject']}：{m['content']}" for m in l2)
    return "\n".join(lines)


def invalidate(memory_id: int) -> bool:
    with session_scope() as s:
        row = s.get(Memory, memory_id)
        if not row:
            return False
        row.status = "archived"
        row.valid_until = datetime.now(timezone.utc)
        return True
