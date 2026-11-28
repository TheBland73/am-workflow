"""条件压缩：占用到阈值才压缩，尽量一次压到目标占用以下。只改发给模型的视图，不改存储。

系统提示本身就很大，或保留部分里有截不掉的大工具调用时，压不到目标占用、甚至压不到触发线
以下：这时只能带着超线的内容继续，等它再涨上去时重试。
"""
from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from typing import Awaitable, Callable

from app.conversation.policy import ContextPolicy
from app.engine.usage import add_attempt_usage, consume_run_budget
from app.models.budget import ContextBudget, estimate_tokens, truncate_to_tokens
from app.models.pool import ModelInfo, pool
from app.models.types import GenParams, Message, ToolCall, Usage

SUMMARIZER_PROMPT = (
    "你在为一个智能体压缩它较早的对话记录。把给你的内容压成一段摘要，保留："
    "已经确定的结论及其理由、还没解决的问题、关键的数据与名称。"
    "不要加入原文没有的信息。只输出摘要正文，不要标题。"
)
SUMMARY_PREFIX = "【较早对话的摘要】\n"
_EARLIER_SUMMARY_HEADING = "【此前内容的摘要】\n"
_LATER_CONTENT_HEADING = "【之后的内容】\n"
_SUMMARY_CHUNK_TOKENS = 24_000   # 一次送给摘要模型的旧内容上限（估算 token）
MAX_SUMMARY_CALLS = 8            # 一次压缩最多调用几次摘要模型
_RECENT_MESSAGE_CAP = 2000
_MIN_RECENT_CAP = 200

Summarizer = Callable[[str], Awaitable[str]]
ProgressCallback = Callable[[int, int | None], Awaitable[None]]


@dataclass
class CompactionStats:
    """一次压缩的账：调了几次摘要、送了大约多少 token、压掉了前面多少条。"""
    calls: int = 0
    summarized_tokens: int = 0
    cut: int = 0


def _call_text(call: ToolCall) -> str:
    return f"{call.name}({json.dumps(call.args, ensure_ascii=False, default=str)})"


def estimate_messages(messages: list[Message]) -> int:
    """估算占用。工具调用的参数也占输入窗口（写文件时参数就是整个文件内容），一并计入。"""
    total = 0
    for message in messages:
        total += estimate_tokens(message.content or "")
        for call in message.tool_calls:
            total += estimate_tokens(_call_text(call))
    return total


def needs_compaction(messages: list[Message], *, input_budget: int,
                     policy: ContextPolicy) -> bool:
    return estimate_messages(messages) >= policy.compact_at * max(1, input_budget)


def _render(messages: list[Message]) -> str:
    lines = []
    for message in messages:
        if message.role == "tool":
            lines.append(f"[工具结果] {message.content}")
        elif message.role == "assistant":
            lines.append(f"[助手] {message.content or ''}")
            for call in message.tool_calls:
                lines.append(f"[助手调用工具] {_call_text(call)}")
        else:
            lines.append(f"[用户] {message.content}")
    return "\n".join(lines)


def _chunks(messages: list[Message], chunk_tokens: int | None = None) -> list[str]:
    """把要压缩的消息渲染成按时间顺序排列的若干块，每块不超过 chunk_tokens。

    分块是为了不丢内容：待压缩的部分常常比一次摘要能处理的量大，而离保留窗口最近的
    那部分最有用，整段截到上限丢掉的恰恰是它。单条消息就超限时（例如一次很大的工具
    调用）只保留它的开头。chunk_tokens 由调用方按摘要模型的实际预算给出；不给时用固定值。
    """
    cap = max(1, int(chunk_tokens or _SUMMARY_CHUNK_TOKENS))
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for message in messages:
        text = _render([message])
        cost = estimate_tokens(text)
        if cost > cap:
            if current:
                chunks.append("\n".join(current))
                current, size = [], 0
            chunks.append(truncate_to_tokens(text, cap))
            continue
        if size + cost > cap:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(text)
        size += cost
    if current:
        chunks.append("\n".join(current))
    return chunks


def _group_size(rest: list[Message], start: int) -> int:
    """从 start 起的一组有多大：带工具调用的助手消息连同紧跟它的工具结果，其余情况是单条。"""
    size = 1
    if rest[start].role == "assistant" and rest[start].tool_calls:
        while start + size < len(rest) and rest[start + size].role == "tool":
            size += 1
    return size


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
                           input_budget: int, policy: ContextPolicy,
                           max_calls: int | None = None,
                           chunk_tokens: int | None = None,
                           stats: CompactionStats | None = None,
                           on_progress: ProgressCallback | None = None
                           ) -> tuple[list[Message], int, int]:
    """压缩 messages 里除系统提示与最近若干条之外的内容。

    返回 (压缩后的列表, 压缩前的估算 token, 压缩后的估算 token)。
    分三步：较早的消息从旧到新分块压成一条滚动摘要；压完仍高于目标占用时，把保留的
    最近消息逐级截短；都不够了才把保留部分里较早的几组也并进摘要。
    可压缩的内容不足时原样返回，后两个值相等。

    max_calls 给一次压缩的摘要调用数封顶：块数超过上限时只压最近的几块，更早的内容
    在摘要里注明没有纳入；合并保留部分的整组也不会超过这个上限。stats 收集调用次数、
    送入的 token 量与压掉的条数，on_progress 在每个块之前报告进度。
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
    if cut == 1 and rest[0].role == "user" and (rest[0].content or "").startswith(SUMMARY_PREFIX):
        # 可压的只剩上一版摘要：再压一次只是把摘要摘要一遍，白花一次调用
        return messages, before, before
    limit = max(1, int(max_calls if max_calls is not None else MAX_SUMMARY_CALLS))
    chunks = _chunks(rest[:cut], chunk_tokens)
    omitted = 0
    if len(chunks) > limit:
        omitted = len(chunks) - limit
        chunks = chunks[-limit:]
    planned = len(chunks)
    counted = CompactionStats()

    async def summarize_chunk(text: str) -> str:
        if on_progress is not None:
            await on_progress(counted.calls + 1, planned if counted.calls < planned else None)
        counted.calls += 1
        counted.summarized_tokens += estimate_tokens(text)
        return await summarize(text)

    summary = ""
    for chunk in chunks:
        text = chunk if not summary else (
            _EARLIER_SUMMARY_HEADING + summary + "\n\n" + _LATER_CONTENT_HEADING + chunk)
        summary = await summarize_chunk(text)
    if omitted:
        summary = (f"【更早的 {omitted} 段内容因单次压缩的摘要调用上限未纳入摘要】\n"
                   + summary)
    summary_message = Message(role="user", content=SUMMARY_PREFIX + summary)
    candidate = head + [summary_message] + [_cap(m) for m in rest[cut:]]
    after = estimate_messages(candidate)
    target = policy.compact_to * max(1, input_budget)
    trigger = policy.compact_at * max(1, input_budget)
    cap = _RECENT_MESSAGE_CAP
    while after > target and cap > _MIN_RECENT_CAP:
        cap = max(_MIN_RECENT_CAP, cap // 2)
        candidate = head + [summary_message] + [_cap(m, cap) for m in rest[cut:]]
        after = estimate_messages(candidate)
    # 压完仍在触发线之上（保留部分里有截不掉的大工具调用）：下一次请求会立刻再次触发压缩，
    # 每一步都得重新摘要。把保留部分里较早的几组整组并进摘要，直到落到触发线以下，
    # 或者保留部分已经并空（系统提示本身就很大时降不下去，只能带着超线的内容继续）。
    # 整组进出，工具结果不会和它回应的助手消息拆开。
    while after >= trigger and cut < len(rest) and counted.calls < limit:
        size = _group_size(rest, cut)
        group = "\n".join(_render([m]) for m in rest[cut:cut + size])
        summary = await summarize_chunk(
            _EARLIER_SUMMARY_HEADING + summary + "\n\n" + _LATER_CONTENT_HEADING + group)
        summary_message = Message(role="user", content=SUMMARY_PREFIX + summary)
        cut += size
        candidate = head + [summary_message] + [_cap(m, cap) for m in rest[cut:]]
        after = estimate_messages(candidate)
    if stats is not None:
        stats.calls = counted.calls
        stats.summarized_tokens = counted.summarized_tokens
        stats.cut = cut
    return candidate, before, after


async def maybe_compact(*, run_id: str, node_id: int, model: ModelInfo,
                        policy: ContextPolicy, messages: list[Message],
                        summarize: Summarizer | None = None,
                        max_calls: int | None = None,
                        stats: CompactionStats | None = None,
                        on_progress: ProgressCallback | None = None
                        ) -> tuple[list[Message], int, int]:
    """没压缩时（没到阈值，或者到了阈值但没有可压的内容）原样返回，后两个值为 -1。

    压缩过时返回新列表和压缩前后的估算占用；摘要消息的正文用 summary_of() 取。
    """
    input_budget = ContextBudget(model.context_window, model.max_output_tokens).input_budget
    if not needs_compaction(messages, input_budget=input_budget, policy=policy):
        return messages, -1, -1
    summarize = summarize or make_summarizer(run_id=run_id, attempt_id=0, model=model)
    result, before, after = await compact_messages(
        messages, summarize=summarize, input_budget=input_budget, policy=policy,
        max_calls=max_calls, stats=stats, on_progress=on_progress)
    if result is messages:
        return messages, -1, -1
    return result, before, after


def summary_of(messages: list[Message]) -> str | None:
    """压缩结果里那条摘要消息的正文；没有就是 None。"""
    return next((m.content for m in messages
                 if m.role == "user" and m.content.startswith(SUMMARY_PREFIX)), None)


def make_summarizer(*, run_id: str, attempt_id: int, model: ModelInfo) -> Summarizer:
    """用同一个模型做摘要，并把这次调用的估算用量记到执行记录与运行预算上。"""
    async def summarize(text: str) -> str:
        adapter = pool.adapter_for(model)
        messages = [Message(role="system", content=SUMMARIZER_PROMPT),
                    Message(role="user", content=text)]
        # 摘要也是一次模型调用，和正文调用一样受供应商并发数的约束
        provider = model.provider
        gate = await pool.slot(provider) if provider is not None else contextlib.nullcontext()
        async with gate:
            reply = await adapter.chat(messages, None, GenParams(
                model=model.model_name, temperature=0.2,
                max_tokens=min(1024, model.max_output_tokens or 1024)))
        usage = Usage(input_tokens=adapter.count_tokens_estimate(messages),
                      output_tokens=max(1, len(reply.content or "") // 3))
        if attempt_id:
            add_attempt_usage(attempt_id, usage, model_id=model.id)
        consume_run_budget(run_id, usage)
        summary = (reply.content or "").strip()
        if not summary:
            # 空正文不能当摘要：滚动摘要里，一句占位话会把此前各块压好的内容全替换掉
            raise RuntimeError("摘要模型返回了空正文")
        return summary

    return summarize
