"""会话日志：只追加的消息日志，以及压缩的留痕。"""
from __future__ import annotations

import asyncio

from app.conversation.store import (
    append_message,
    get_or_create_conversation,
    load_messages,
    record_compaction,
)
from app.db.base import init_db, session_scope
from app.db.models import Event, Node, NodeAttempt, Run, Workflow
from app.engine.templates import seed_defaults


def _setup(run_id: str) -> tuple[int, int]:
    """建一条运行和一个执行中的节点，返回 (节点 id, attempt id)。"""
    init_db()
    seed_defaults()
    with session_scope() as s:
        workflow_id = s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").one().id
        s.add(Run(id=run_id, workflow_id=workflow_id, status="running", budget_tokens=10_000_000))
        s.flush()
        node = Node(node_key="dev_x", type="AGENT", name="dev", run_id=run_id, status="running")
        s.add(node)
        s.flush()
        attempt = NodeAttempt(node_id=node.id, attempt_no=1, status="running")
        s.add(attempt)
        s.flush()
        return node.id, attempt.id


def _event_types(run_id: str) -> list[str]:
    with session_scope() as s:
        return [e.type for e in s.query(Event).filter(Event.run_id == run_id).all()]


def test_conversation_log_is_scoped_by_owner_and_appends_in_order() -> None:
    node_id, _ = _setup("hook-a")
    first = get_or_create_conversation(run_id="hook-a", node_id=node_id, scope="meeting", owner="p1")
    # 同样的归属取到同一段会话；归属不同才是另一段
    assert get_or_create_conversation(
        run_id="hook-a", node_id=node_id, scope="meeting", owner="p1") == first
    assert get_or_create_conversation(
        run_id="hook-a", node_id=node_id, scope="meeting", owner="p2") != first

    append_message(first, role="user", content="问题")
    append_message(first, role="assistant", content="回答", tokens_in=12, cached_tokens=8)

    assert [(m["seq"], m["role"], m["content"]) for m in load_messages(first)] == [
        (1, "user", "问题"), (2, "assistant", "回答")]
    assert [m["content"] for m in load_messages(first, since_seq=1)] == ["回答"]


def test_record_compaction_writes_summary_and_event() -> None:
    node_id, attempt_id = _setup("hook-b")
    message_id = asyncio.run(record_compaction(
        run_id="hook-b", node_id=node_id, attempt_id=attempt_id, scope="agent_loop",
        summary="摘要", before_tokens=900, after_tokens=300))
    assert "context_compacted" in _event_types("hook-b")
    # 摘要作为一条标记了压缩边界的消息留在会话日志里，压缩前后的占用一并记下
    conversation_id = get_or_create_conversation(
        run_id="hook-b", node_id=node_id, scope="agent_loop", owner=f"attempt-{attempt_id}")
    rows = load_messages(conversation_id)
    assert [(r["seq"], r["kind"], r["content"], r["meta"]) for r in rows] == [
        (1, "summary", "摘要", {"before_tokens": 900, "after_tokens": 300})]
    assert message_id > 0
