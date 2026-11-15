"""Plan 生成事件总线：plan_token 只在内存里推，不落库（ORCH-01）。"""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict


class PlanBus:
    def __init__(self) -> None:
        self._queues: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._seq: dict[str, int] = defaultdict(int)

    async def publish(self, plan_id: str, type_: str, payload: dict) -> int:
        self._seq[plan_id] += 1
        seq = self._seq[plan_id]
        event = {"seq": seq, "type": type_, "payload": payload}
        for queue in list(self._queues.get(plan_id, set())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass
        return seq

    async def subscribe(self, plan_id: str):
        queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._queues[plan_id].add(queue)
        try:
            while True:
                event = await queue.get()
                yield event
        finally:
            self._queues[plan_id].discard(queue)


def sse_format(event: dict) -> str:
    return (
        f"id: {event['seq']}\n"
        f"event: {event['type']}\n"
        f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    )


plan_bus = PlanBus()
