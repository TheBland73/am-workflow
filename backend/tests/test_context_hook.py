"""会话日志：只追加的消息日志，以及工具循环里的压缩钩子（不到阈值不动，到了阈值压缩并留痕）。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.artifacts.store import workspace_dir
from app.conversation.store import (
    append_message,
    get_or_create_conversation,
    load_messages,
    record_compaction,
)
from app.db.base import init_db, session_scope
from app.db.models import Event, Node, NodeAttempt, Run, Workflow
from app.engine import agent_loop, events
from app.engine.agent_loop import run_agent
from app.engine.templates import seed_defaults
from app.models.types import GenParams, Message, StreamEvent, ToolCall, Usage
from app.tools.registry import Toolbox

# 窗口 1200、输出上限 256：可用窗口取下限 1024 个 token，压缩阈值是它的 80%。
# 不给模型编号：执行记录的 model_id 有外键，测试库里没有对应的模型行。
_MODEL = dict(model_name="m", display_name="M", context_window=1200,
              max_output_tokens=256, cap_tools=True, id=None)


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


def _step_logs(run_id: str) -> int:
    """循环里逐步刷的"第 N 步：请求模型"日志条数。"""
    with session_scope() as s:
        logs = s.query(Event).filter(Event.run_id == run_id, Event.type == "node_log").all()
    return sum(1 for e in logs if str((e.payload_json or {}).get("message", "")).startswith("第 "))


def _install_fake_stream(monkeypatch, seen: list[list[Message]], tool_calls: int) -> None:
    """替换模型调用：前 tool_calls 次请求都调工具，之后给出最终答复。"""

    async def fake_stream(model, messages, schemas, params, *, run_id, node_id, token_extra=None):
        seen.append(list(messages))
        if len(seen) <= tool_calls:
            message = Message(role="assistant", content="",
                              tool_calls=[ToolCall(id=f"c{len(seen)}", name="list_files", args={})])
        else:
            message = Message(role="assistant", content="完成")
        return message, Usage(input_tokens=10, output_tokens=5)

    async def fake_summarize(text: str) -> str:
        return "摘要"

    monkeypatch.setattr(agent_loop, "_call_stream", fake_stream)
    monkeypatch.setattr(agent_loop, "make_summarizer", lambda **kwargs: fake_summarize)


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


def test_hook_compacts_when_threshold_is_reached(monkeypatch) -> None:
    node_id, attempt_id = _setup("hook-c")
    toolbox = Toolbox(workspace_dir("hook-c"), allow_shell=False)
    seen: list[list[Message]] = []
    # 连调五次工具，历史里非系统消息超过六条之后才会真正压缩
    _install_fake_stream(monkeypatch, seen, tool_calls=5)
    result = asyncio.run(run_agent(
        run_id="hook-c", node_id=node_id, attempt_id=attempt_id, model=SimpleNamespace(**_MODEL),
        system_prompt="系统", user_prompt="字" * 900, toolbox=toolbox, max_steps=6, quiet=True))
    assert result.content == "完成"
    # 第六次请求之前，较早的消息已被一条摘要取代
    last = seen[-1]
    assert last[0].content == "系统"
    assert last[1].content.startswith("【较早对话的摘要】")
    assert "context_compacted" in _event_types("hook-c")


def test_hook_leaves_short_history_alone(monkeypatch) -> None:
    node_id, attempt_id = _setup("hook-d")
    toolbox = Toolbox(workspace_dir("hook-d"), allow_shell=False)
    seen: list[list[Message]] = []
    _install_fake_stream(monkeypatch, seen, tool_calls=3)
    asyncio.run(run_agent(
        run_id="hook-d", node_id=node_id, attempt_id=attempt_id, model=SimpleNamespace(**_MODEL),
        system_prompt="系统", user_prompt="列出文件", toolbox=toolbox, max_steps=4, quiet=True))
    assert "context_compacted" not in _event_types("hook-d")
    assert [m.content for m in seen[-1][:2]] == ["系统", "列出文件"]     # 原始提示一直在


def test_quiet_suppresses_step_logs(monkeypatch) -> None:
    # 会议里的发言由会议运行时自己写节点日志，工具循环不必逐步刷"请求模型"
    for run_id, quiet in (("hook-e", True), ("hook-f", False)):
        node_id, attempt_id = _setup(run_id)
        _install_fake_stream(monkeypatch, [], tool_calls=0)
        asyncio.run(run_agent(
            run_id=run_id, node_id=node_id, attempt_id=attempt_id, model=SimpleNamespace(**_MODEL),
            system_prompt="系统", user_prompt="你好", toolbox=None, max_steps=2, quiet=quiet))
    assert _step_logs("hook-e") == 0
    assert _step_logs("hook-f") == 1


def test_token_extra_rides_on_streamed_tokens(monkeypatch) -> None:
    # 会议里的流式发言靠 node_token 载荷里的附加字段归并到具体的那条发言
    payloads: list[dict] = []

    def fake_emit_token(run_id, node_id, text, extra=None):
        payloads.append({"text": text, "extra": extra})

    class Adapter:
        async def stream(self, messages, schemas, params):
            yield StreamEvent(kind="text_delta", text="你")
            yield StreamEvent(kind="done", message=Message(role="assistant", content="你"))

    async def slot(provider):
        return asyncio.Semaphore(1)

    monkeypatch.setattr(agent_loop, "emit_token", fake_emit_token)
    monkeypatch.setattr(agent_loop.pool, "adapter_for", lambda model: Adapter())
    monkeypatch.setattr(agent_loop.pool, "slot", slot)

    asyncio.run(agent_loop._call_stream(
        SimpleNamespace(provider=object()), [Message(role="user", content="你好")], None,
        GenParams(model="m"), run_id="hook-g", node_id=1, token_extra={"turn_id": 7}))
    assert payloads == [{"text": "你", "extra": {"turn_id": 7}}]


def test_emit_token_merges_extra_into_payload(monkeypatch) -> None:
    published: list[dict] = []
    monkeypatch.setattr(events.bus, "publish", lambda run_id, event: published.append(event))
    events.emit_token("hook-g", 3, "你", {"turn_id": 7})
    events.emit_token("hook-g", 3, "好")
    assert [event["payload"] for event in published] == [{"text": "你", "turn_id": 7}, {"text": "好"}]
    assert all(event["seq"] is None for event in published)      # 流式 token 不占事件序号
