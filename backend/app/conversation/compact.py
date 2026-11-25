"""条件压缩：占用到阈值才压缩，一次压到目标占用以下。只改发给模型的视图，不改存储。"""
from __future__ import annotations

from typing import Awaitable, Callable

from app.conversation.policy import ContextPolicy
from app.engine.usage import add_attempt_usage, consume_run_budget
from app.models.budget import ContextBudget, estimate_tokens, truncate_to_tokens
from app.models.pool import ModelInfo, pool
from app.models.types import GenParams, Message, Usage

SUMMARIZER_PROMPT = (
    "你在为一个智能体压缩它较早的对话记录。把给你的内容压成一段摘要，保留："
    "已经确定的结论及其理由、还没解决的问题、关键的数据与名称。"
    "不要加入原文没有的信息。只输出摘要正文，不要标题。"
)
SUMMARY_PREFIX = "【较早对话的摘要】\n"
_RECENT_MESSAGE_CAP = 2000
_MIN_RECENT_CAP = 200

Summarizer = Callable[[str], Awaitable[str]]


def estimate_messages(messages: list[Message]) -> int:
    return sum(estimate_tokens(m.content or "") for m in messages)


def needs_compaction(messages: list[Message], *, input_budget: int,
                     policy: ContextPolicy) -> bool:
    return estimate_messages(messages) >= policy.compact_at * max(1, input_budget)


def _render(messages: list[Message]) -> str:
    lines = []
    for message in messages:
        if message.role == "tool":
            lines.append(f"[工具结果] {message.content}")
        elif message.role == "assistant":
            lines.append(f"[助手] {message.content or '（工具调用）'}")
        else:
            lines.append(f"[用户] {message.content}")
    return "\n".join(lines)


def _cap(message: Message, cap: int = _RECENT_MESSAGE_CAP) -> Message:
    """把过长的一条消息截短到 cap 个 token。带工具调用的助手消息不能截，原样保留。"""
    if message.role == "assistant" and message.tool_calls:
        return message
    if estimate_tokens(message.content or "") <= cap:
        return message
    return Message(role=message.role, content=truncate_to_tokens(message.content, cap),
                   tool_calls=message.tool_calls, tool_call_id=message.tool_call_id,
                   name=message.name)


async def compact_messages(messages: list[Message], *, summarize: Summarizer,
                           input_budget: int,
                           policy: ContextPolicy) -> tuple[list[Message], int, int]:
    """压缩 messages 里除系统提示与最近若干条之外的内容。

    返回 (压缩后的列表, 压缩前的估算 token, 压缩后的估算 token)。
    分两步：较早的消息压成一条摘要；压完仍高于目标占用时，把保留的最近消息逐级截短。
    可压缩的内容不足时原样返回，后两个值相等。
    """
    before = estimate_messages(messages)
    head = [m for m in messages if m.role == "system"]
    rest = [m for m in messages if m.role != "system"]
    if len(rest) <= policy.keep_recent:
        return messages, before, before
    cut = len(rest) - policy.keep_recent
    # 保留部分不能以工具结果开头：它回应的那条助手消息若已被压进摘要，下一次请求就不合法。
    # 这样的工具结果一并交给摘要。
    while cut < len(rest) and rest[cut].role == "tool":
        cut += 1
    summary = await summarize(truncate_to_tokens(_render(rest[:cut]), 24_000))
    summary_message = Message(role="user", content=SUMMARY_PREFIX + summary)
    candidate = head + [summary_message] + [_cap(m) for m in rest[cut:]]
    after = estimate_messages(candidate)
    cap = _RECENT_MESSAGE_CAP
    while after > policy.compact_to * max(1, input_budget) and cap > _MIN_RECENT_CAP:
        cap = max(_MIN_RECENT_CAP, cap // 2)
        candidate = head + [summary_message] + [_cap(m, cap) for m in rest[cut:]]
        after = estimate_messages(candidate)
    return candidate, before, after


async def maybe_compact(*, run_id: str, node_id: int, model: ModelInfo,
                        policy: ContextPolicy, messages: list[Message],
                        summarize: Summarizer | None = None) -> tuple[list[Message], int, int]:
    """没到阈值时原样返回，后两个值为 -1。"""
    input_budget = ContextBudget(model.context_window, model.max_output_tokens).input_budget
    if not needs_compaction(messages, input_budget=input_budget, policy=policy):
        return messages, -1, -1
    summarize = summarize or make_summarizer(run_id=run_id, attempt_id=0, model=model)
    result, before, after = await compact_messages(
        messages, summarize=summarize, input_budget=input_budget, policy=policy)
    return result, before, after


def make_summarizer(*, run_id: str, attempt_id: int, model: ModelInfo) -> Summarizer:
    """用同一个模型做摘要，并把这次调用的估算用量记到执行记录与运行预算上。"""
    async def summarize(text: str) -> str:
        adapter = pool.adapter_for(model)
        messages = [Message(role="system", content=SUMMARIZER_PROMPT),
                    Message(role="user", content=text)]
        reply = await adapter.chat(messages, None, GenParams(
            model=model.model_name, temperature=0.2,
            max_tokens=min(1024, model.max_output_tokens or 1024)))
        usage = Usage(input_tokens=adapter.count_tokens_estimate(messages),
                      output_tokens=max(1, len(reply.content or "") // 3))
        if attempt_id:
            add_attempt_usage(attempt_id, usage, model_id=model.id)
        consume_run_budget(run_id, usage)
        return (reply.content or "").strip() or "（摘要为空）"

    return summarize
