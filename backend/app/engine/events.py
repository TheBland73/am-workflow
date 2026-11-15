"""事件总线：events 表 append-only + 进程内 SSE 扇出（设计规格 §5.3、§5.4）。

红线：node_token 只在内存直推，不落库；SSE 生成器不持有数据库 session。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from sqlalchemy import func

from app.db.base import session_scope
from app.db.models import Event

_locks: dict[str, asyncio.Lock] = {}


def _run_lock(run_id: str) -> asyncio.Lock:
    lock = _locks.get(run_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[run_id] = lock
    return lock


class EventBus:
    """内存扇出：每个订阅者一个队列；只保留会话期事件，历史从 events 表补。"""

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = {}

    def subscribe(self, run_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subs.setdefault(run_id, set()).add(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        subs = self._subs.get(run_id)
        if subs:
            subs.discard(queue)
            if not subs:
                self._subs.pop(run_id, None)

    def publish(self, run_id: str, event: dict) -> None:
        for queue in list(self._subs.get(run_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass


bus = EventBus()


async def emit(run_id: str, type_: str, node_id: int | None = None,
               payload: dict | None = None) -> dict:
    """追加事件并广播。seq 在进程内按 run 串行分配（唯一约束兜底）。"""
    async with _run_lock(run_id):
        with session_scope() as s:
            current = (
                s.query(func.max(Event.seq)).filter(Event.run_id == run_id).scalar() or 0
            )
            seq = current + 1
            ts = datetime.now(timezone.utc)
            row = Event(run_id=run_id, seq=seq, ts=ts, type=type_, node_id=node_id,
                        payload_json=payload or {})
            s.add(row)
            s.flush()
            event = {
                "seq": seq,
                "type": type_,
                "node_id": node_id,
                "ts": ts.isoformat(),
                "payload": payload or {},
            }
    bus.publish(run_id, event)
    return event


def load_events(run_id: str, since: int = 0, limit: int = 5000) -> list[dict]:
    with session_scope() as s:
        rows = (
            s.query(Event)
            .filter(Event.run_id == run_id, Event.seq > since)
            .order_by(Event.seq.asc())
            .limit(limit)
            .all()
        )
        return [
            {
                "seq": r.seq,
                "type": r.type,
                "node_id": r.node_id,
                "ts": r.ts.isoformat() if r.ts else "",
                "payload": r.payload_json or {},
            }
            for r in rows
        ]


def emit_token(run_id: str, node_id: int | None, text: str) -> None:
    """流式 token：内存直推，不写库、不分配 seq（§5.3 红线）。"""
    bus.publish(
        run_id,
        {
            "seq": None,
            "type": "node_token",
            "node_id": node_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "payload": {"text": text},
        },
    )


def sse_format(event: dict) -> str:
    data = json.dumps(event, ensure_ascii=False, default=str)
    if event.get("seq") is None:
        return f"event: {event['type']}\ndata: {data}\n\n"
    return f"id: {event['seq']}\nevent: {event['type']}\ndata: {data}\n\n"
