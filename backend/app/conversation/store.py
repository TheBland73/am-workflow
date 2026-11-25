"""会话日志的落盘与读取。只追加，不改写、不删除。"""
from __future__ import annotations

from sqlalchemy import func

from app.db.base import session_scope
from app.db.models import Conversation, ConversationMessage
from app.engine.events import emit


def get_or_create_conversation(*, run_id: str, node_id: int | None, scope: str,
                               owner: str = "") -> int:
    with session_scope() as s:
        row = (
            s.query(Conversation)
            .filter(Conversation.run_id == run_id, Conversation.node_id == node_id,
                    Conversation.scope == scope, Conversation.owner == owner)
            .first()
        )
        if row is None:
            row = Conversation(run_id=run_id, node_id=node_id, scope=scope, owner=owner)
            s.add(row)
            s.flush()
        return row.id


def append_message(conversation_id: int, *, role: str, content: str, kind: str = "normal",
                   meta: dict | None = None, tokens_in: int = 0, tokens_out: int = 0,
                   cached_tokens: int = 0) -> int:
    with session_scope() as s:
        last = (
            s.query(func.max(ConversationMessage.seq))
            .filter(ConversationMessage.conversation_id == conversation_id)
            .scalar() or 0
        )
        row = ConversationMessage(
            conversation_id=conversation_id, seq=last + 1, role=role, content=content,
            kind=kind, meta_json=meta or {}, tokens_in=tokens_in, tokens_out=tokens_out,
            cached_tokens=cached_tokens,
        )
        s.add(row)
        s.flush()
        return row.id


def load_messages(conversation_id: int, since_seq: int = 0, limit: int = 500) -> list[dict]:
    with session_scope() as s:
        rows = (
            s.query(ConversationMessage)
            .filter(ConversationMessage.conversation_id == conversation_id,
                    ConversationMessage.seq > since_seq)
            .order_by(ConversationMessage.seq.asc())
            .limit(limit)
            .all()
        )
        return [
            {"seq": r.seq, "role": r.role, "content": r.content, "kind": r.kind,
             "meta": dict(r.meta_json or {})}
            for r in rows
        ]


async def record_compaction(*, run_id: str, node_id: int, attempt_id: int, scope: str,
                            summary: str, before_tokens: int, after_tokens: int) -> int:
    """把一次压缩写进会话日志，并推送 context_compacted 事件。"""
    conversation_id = get_or_create_conversation(
        run_id=run_id, node_id=node_id, scope=scope, owner=f"attempt-{attempt_id}")
    message_id = append_message(
        conversation_id, role="user", content=summary, kind="summary",
        meta={"before_tokens": before_tokens, "after_tokens": after_tokens})
    await emit(run_id, "context_compacted", node_id,
               {"scope": scope, "before_tokens": before_tokens, "after_tokens": after_tokens})
    return message_id
