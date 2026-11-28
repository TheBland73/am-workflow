"""缓存命中数：各家协议字段的解析，以及累加到执行记录。"""
from __future__ import annotations

import asyncio

import httpx
from sqlalchemy import text

from app.conversation.telemetry import cache_hit_rate, parse_cached_tokens
from app.db.base import init_db, session_scope
from app.db.models import NodeAttempt, Node, Run, Workflow
from app.engine.templates import seed_defaults
from app.engine.usage import add_attempt_usage
from app.models.providers.openai_chat import OpenAIChatAdapter
from app.models.types import GenParams, Message, StreamEvent, Usage


def _make_attempt(run_id: str) -> int:
    """建一条最简的执行记录（运行 → 节点 → 执行），返回它的 id。"""
    init_db()
    seed_defaults()
    with session_scope() as s:
        workflow_id = s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").one().id
        s.add(Run(id=run_id, workflow_id=workflow_id, status="running"))
        s.flush()
        node = Node(node_key="n", type="AGENT", name="n", run_id=run_id, status="pending")
        s.add(node)
        s.flush()
        attempt = NodeAttempt(node_id=node.id, attempt_no=1, status="running")
        s.add(attempt)
        s.flush()
        return attempt.id


def test_parse_cached_tokens_supports_each_protocol() -> None:
    assert parse_cached_tokens({"prompt_tokens_details": {"cached_tokens": 1024}}) == 1024
    assert parse_cached_tokens({"prompt_cache_hit_tokens": 512, "prompt_cache_miss_tokens": 8}) == 512
    assert parse_cached_tokens({"cache_read_input_tokens": 300}) == 300
    assert parse_cached_tokens({"usageMetadata": {"cachedContentTokenCount": 64}}) == 64


def test_parse_cached_tokens_returns_zero_when_absent() -> None:
    assert parse_cached_tokens(None) == 0
    assert parse_cached_tokens({"prompt_tokens": 10}) == 0


def test_cache_hit_rate() -> None:
    assert cache_hit_rate(500, 1000) == 0.5
    assert cache_hit_rate(0, 0) == 0.0


def test_openai_stream_records_cached_tokens(monkeypatch) -> None:
    # OpenAI 兼容协议的流式响应：usage 块里带着缓存命中数
    sse = "\n".join([
        'data: {"choices": [{"delta": {"content": "好"}}]}',
        'data: {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 5,'
        ' "prompt_tokens_details": {"cached_tokens": 64}}}',
        "data: [DONE]",
    ])
    monkeypatch.setattr(
        OpenAIChatAdapter, "_client",
        lambda self: httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=sse))))
    adapter = OpenAIChatAdapter(base_url="http://mock/v1", api_key="")

    async def collect() -> list[StreamEvent]:
        return [event async for event in
                adapter.stream([Message(role="user", content="你好")], None, GenParams(model="m"))]

    events = asyncio.run(collect())
    usage = next(event.usage for event in events if event.kind == "usage")
    assert (usage.input_tokens, usage.cached_tokens) == (100, 64)


def test_attempt_usage_accumulates_cached_tokens() -> None:
    attempt_id = _make_attempt("telemetry-run")
    add_attempt_usage(attempt_id, Usage(input_tokens=100, output_tokens=20, cached_tokens=60))
    add_attempt_usage(attempt_id, Usage(input_tokens=100, output_tokens=20, cached_tokens=40))
    with session_scope() as s:
        row = s.get(NodeAttempt, attempt_id)
        assert (row.tokens_in, row.cached_tokens) == (200, 100)


def test_attempt_usage_treats_missing_cached_tokens_as_zero() -> None:
    # 补列之前就存在的执行记录，这一列是 NULL：启动时用 ALTER TABLE 加的列，旧行不会填默认值。
    # 所以用 SQL 把它置成 NULL，而不是靠 ORM 赋 None（ORM 会把 None 当成没给值，默认值 0 照样生效）
    attempt_id = _make_attempt("telemetry-run-null")
    with session_scope() as s:
        s.execute(text("UPDATE node_attempts SET cached_tokens = NULL WHERE id = :id"),
                  {"id": attempt_id})
    add_attempt_usage(attempt_id, Usage(input_tokens=10, cached_tokens=7))
    with session_scope() as s:
        assert s.get(NodeAttempt, attempt_id).cached_tokens == 7
