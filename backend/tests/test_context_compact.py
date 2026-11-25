"""条件压缩：到阈值才压缩，压到目标以下，系统提示与最近的消息不动。"""
from __future__ import annotations

import asyncio

from app.conversation.compact import compact_messages, needs_compaction
from app.conversation.policy import ContextPolicy
from app.models.types import Message, ToolCall

POLICY = ContextPolicy(compact_at=0.8, compact_to=0.5, keep_recent=2)


def _messages(count: int, each: int = 1000) -> list[Message]:
    rest = [Message(role="user" if i % 2 == 0 else "assistant", content="字" * each)
            for i in range(count)]
    return [Message(role="system", content="系统")] + rest


async def _noop(text: str) -> str:
    return "摘要"


def test_not_triggered_below_threshold() -> None:
    assert needs_compaction(_messages(3), input_budget=10_000, policy=POLICY) is False


def test_compacts_older_messages_and_keeps_recent() -> None:
    messages = _messages(8, each=100)             # 约 800 token，窗口 800 → 到达阈值
    assert needs_compaction(messages, input_budget=800, policy=POLICY) is True
    seen: list[str] = []

    async def summarize(text: str) -> str:
        seen.append(text)
        return "摘要"

    result, before, after = asyncio.run(compact_messages(
        messages, summarize=summarize, input_budget=800, policy=POLICY))
    assert before >= 800
    assert after < 400                              # 压到目标占用（50%）以下
    assert result[0].role == "system"
    assert result[1].role == "user" and result[1].content.startswith("【较早对话的摘要】")
    assert [m.content for m in result[2:]] == [m.content for m in messages[-2:]]
    assert len(seen) == 1 and "字" in seen[0]


def test_long_recent_messages_are_capped_to_reach_target() -> None:
    """保留的最近几条本身很长：压完仍高于目标时，把它们逐级截短。"""
    messages = _messages(3, each=3000) + [Message(role="user", content="字" * 3000)]
    result, before, after = asyncio.run(compact_messages(
        messages, summarize=_noop, input_budget=4000, policy=POLICY))
    assert before > 0.8 * 4000
    assert after < 0.5 * 4000
    assert result[0].role == "system" and len(result) == 4      # 系统提示 + 摘要 + 最近两条


def test_leaves_short_conversations_alone() -> None:
    messages = _messages(2)
    result, before, after = asyncio.run(compact_messages(
        messages, summarize=_noop, input_budget=1000, policy=POLICY))
    assert result == messages and before == after


def test_kept_messages_never_start_with_an_orphaned_tool_result() -> None:
    # 工具结果必须紧跟在发起调用的那条助手消息后面。保留部分若以工具结果开头，
    # 它回应的消息已经被压进摘要，下一次请求就不合法：这样的工具结果一并交给摘要
    messages = _messages(3, each=100) + [
        Message(role="assistant", content="",
                tool_calls=[ToolCall(id="c1", name="查", args={}), ToolCall(id="c2", name="查", args={})]),
        Message(role="tool", content="字" * 100, tool_call_id="c1"),
        Message(role="tool", content="字" * 100, tool_call_id="c2"),
        Message(role="assistant", content="字" * 100),
    ]
    seen: list[str] = []

    async def summarize(text: str) -> str:
        seen.append(text)
        return "摘要"

    result, _, _ = asyncio.run(compact_messages(
        messages, summarize=summarize, input_budget=800, policy=POLICY))
    assert [m.role for m in result[2:]] == ["assistant"]          # 最近两条里的工具结果没有孤立保留
    assert "[工具结果]" in seen[0]
