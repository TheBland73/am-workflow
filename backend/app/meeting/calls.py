"""角色发言：拼装上下文 → 调模型 → 落盘 → 推事件。

会议里的每一次模型调用都走 speak()。上下文按 冻结 → 追加 → 尾部 排好交给装配器，
调用标识追加在最后。模型调用复用工具循环 run_agent（单步、无工具），从而沿用它的
流式输出、用量记账与预算扣减。

一次调用发出去的内容，顺序固定（详设 §7.7、§10.1）：
系统提示（全体共用）→ 会议章程（全体相同）→ 角色人设 → 账本快照（本轮不变）→
本轮的逐字发言（只追加）→ 尾部指令。前面的段在两次调用之间逐字节不变，前缀缓存才能命中。
"""
from __future__ import annotations

import asyncio
from typing import Callable

from app.artifacts.store import strip_outer_fence
from app.conversation.builder import Segment
from app.conversation.compact import compact_messages
from app.conversation.policy import policy_for_run
from app.conversation.store import record_compaction
from app.engine.agent_loop import _extract_json, run_agent
from app.engine.events import emit
from app.engine.runners import NodeFailure
from app.engine.usage import BudgetExceeded
from app.meeting import store
from app.meeting.prompts import BASE_SYSTEM_PROMPT, call_tag, persona_block
from app.meeting.session import MeetingSession
from app.models.budget import ContextBudget, estimate_tokens, truncate_to_tokens
from app.models.pool import ProviderError, pool
from app.models.types import Message

Validator = Callable[[dict], list[str]]


class CallOutputError(RuntimeError):
    """模型的输出不符合要求的结构。"""


def parse_structured(content: str, validator: Validator | None = None) -> dict:
    data = _extract_json(strip_outer_fence(content))
    if data is None:
        raise CallOutputError("模型没有返回合法的 JSON 对象")
    issues = validator(data) if validator else []
    if issues:
        raise CallOutputError("；".join(issues))
    return data


def _model_for(participant: dict):
    """先取参会者绑定的模型，再取角色默认模型，再退到主持人的模型。"""
    model = None
    if participant.get("model_id"):
        try:
            model = pool.load_model(int(participant["model_id"]))
        except ProviderError:
            model = None
    if model is None and participant.get("role_key"):
        model = pool.pick_role_model(participant["role_key"])
    if model is None:
        model = pool.pick_role_model("facilitator")
    if model is None:
        raise NodeFailure(
            "尚未配置可用模型：请先在「设置 → 供应商」添加供应商与模型并测试连接", fatal=True)
    return model


def _phase(mtg: MeetingSession) -> str:
    return store.get_meeting(mtg.meeting_id)["phase"]


_TAIL_RESERVE = 300          # 尾部指令很短，压缩判断时按固定值预留
_SUMMARIZER = "你是会议记录员，负责把较早的发言压成摘要。"


async def _summarize_exchanges(mtg: MeetingSession, model, text: str) -> str:
    """把较早的逐字发言压成摘要。也走工具循环并带调用标识，Mock 与测试替身按标识路由。"""
    input_budget = ContextBudget(model.context_window, model.max_output_tokens).input_budget
    prompt = (
        f"{_SUMMARIZER}\n\n"
        "把下面这些会议发言压成一段摘要。保留：已经确定的结论及其理由、还没解决的问题、"
        "谁坚持什么。不要加入原文没有的信息。只输出摘要正文，不要标题。\n\n"
        f"{truncate_to_tokens(text, int(input_budget * 0.6))}\n\n{call_tag('summarize')}")
    result = await run_agent(
        run_id=mtg.run_id, node_id=mtg.node_id, attempt_id=mtg.ctx.attempt_id, model=model,
        system_prompt=BASE_SYSTEM_PROMPT, user_prompt=prompt, toolbox=None,
        max_steps=1, temperature=0.2, quiet=True,
        token_extra={"meeting_id": mtg.meeting_id})
    summary = strip_outer_fence(result.content).strip()
    if not summary:
        # 空摘要会顶掉滚动摘要里此前的内容（与 A3 的 make_summarizer 同一口径）
        raise RuntimeError("摘要模型返回了空正文")
    return summary


async def _fit(mtg: MeetingSession, model, segments: list[Segment],
               input_budget: int) -> list[Segment]:
    """追加区占到压缩阈值时，把其中较早的发言压成一条摘要。只改这次发出的视图，不改纪要。

    压缩只动追加区：冻结区（章程、人设、账本快照）是前缀缓存命中的部分，本轮内逐字节不变。
    摘要失败时按原视图继续——压缩只是优化，不该让一次发言作废；预算耗尽是例外。
    """
    policy = policy_for_run(mtg.run_id)
    fixed = estimate_tokens(BASE_SYSTEM_PROMPT) + sum(
        estimate_tokens(s.text) for s in segments if s.kind == "frozen")
    append = [s for s in segments if s.kind == "append"]
    size = fixed + sum(estimate_tokens(s.text) for s in append) + _TAIL_RESERVE
    if size < policy.compact_at * input_budget:
        return segments
    messages = [Message(role="system", content="系统")]
    messages += [Message(role=s.role, content=s.text) for s in append]
    try:
        compacted, before, after = await compact_messages(
            messages, summarize=lambda text: _summarize_exchanges(mtg, model, text),
            input_budget=max(1, input_budget - fixed - _TAIL_RESERVE), policy=policy)
    except BudgetExceeded:
        raise
    except Exception as exc:
        await emit(mtg.run_id, "node_log", mtg.node_id, {
            "level": "warn", "message": f"会议上下文压缩失败，按原内容继续：{exc}"[:300]})
        return segments
    if compacted is messages:
        return segments
    await record_compaction(
        run_id=mtg.run_id, node_id=mtg.node_id, attempt_id=mtg.ctx.attempt_id, scope="meeting",
        summary=compacted[1].content, before_tokens=before + fixed, after_tokens=after + fixed)
    return [s for s in segments if s.kind == "frozen"] + [
        Segment("append", m.content, role=m.role, name="交锋摘要" if index == 0 else "较早发言")
        for index, m in enumerate(compacted[1:])]


async def speak(mtg: MeetingSession, participant: dict, *, kind: str, turn_kind: str,
                segments: list[Segment], instruction: str,
                charter: list[Segment] | None = None,
                structured: Validator | None = None, decision_point_id: int | None = None,
                temperature: float = 0.4, turn_round: int | None = None,
                drain: bool = True) -> dict:
    """让一个参会者发言一次。返回 {"turn_id", "content", "data"}。

    发出去的上下文依次是：共用的系统提示 → 会议章程（所有角色相同，由协议经 charter 传入）→
    这个角色的人设 → segments（账本快照与本轮的逐字发言）→ 尾部的指令与调用标识。
    这样排是为了前缀缓存（详设 §10.1）：从开头到人设之前，全体角色的内容完全相同。
    """
    if drain:
        await drain_interventions(mtg)
    model = _model_for(participant)
    persona = Segment("frozen", persona_block(participant),
                      name=f"角色人设:{participant.get('id')}")
    segments = [s for s in list(charter or []) + [persona] + list(segments) if s.text.strip()]
    budget = ContextBudget(model.context_window, model.max_output_tokens)
    segments = await _fit(mtg, model, segments, budget.input_budget)
    tail = Segment("tail", f"{instruction}\n\n{call_tag(kind)}", name="instruction")
    built = mtg.builder.build(segments + [tail], system=BASE_SYSTEM_PROMPT,
                              input_budget=budget.input_budget)
    if built.truncated:
        await emit(mtg.run_id, "node_log", mtg.node_id, {
            "level": "warn",
            "message": f"上下文超出预算，已截短：{'、'.join(built.truncated)}"[:300]})
    if built.frozen_changed:
        await emit(mtg.run_id, "context_prefix_changed", mtg.node_id,
                   {"meeting_id": mtg.meeting_id, "segments": built.frozen_changed})
    speaker_id = participant["id"]
    speaker_name = participant["display_name"]
    turn_id = store.add_turn(
        mtg.meeting_id, round=turn_round or mtg.round_no(), phase=_phase(mtg),
        speaker_id=speaker_id, speaker_name=speaker_name, kind=turn_kind,
        decision_point_id=decision_point_id)
    await emit(mtg.run_id, "meeting_turn_started", mtg.node_id, {
        "meeting_id": mtg.meeting_id, "turn_id": turn_id, "speaker": speaker_name,
        "speaker_id": speaker_id, "kind": turn_kind, "decision_point_id": decision_point_id,
        "call": kind})
    result = await run_agent(
        run_id=mtg.run_id, node_id=mtg.node_id, attempt_id=mtg.ctx.attempt_id, model=model,
        system_prompt=BASE_SYSTEM_PROMPT, user_prompt="", toolbox=None, max_steps=1,
        temperature=temperature, quiet=True,
        token_extra={"turn_id": turn_id, "meeting_id": mtg.meeting_id},
        extra_messages=built.messages[1:])
    data = parse_structured(result.content, structured) if structured else None
    store.finish_turn(turn_id, content=result.content, structured=data,
                      tokens_in=result.usage.input_tokens, tokens_out=result.usage.output_tokens,
                      cached_tokens=result.usage.cached_tokens, model_id=model.id)
    await emit(mtg.run_id, "meeting_turn_finished", mtg.node_id, {
        "meeting_id": mtg.meeting_id, "turn_id": turn_id, "speaker": speaker_name,
        "kind": turn_kind, "tokens_in": result.usage.input_tokens,
        "tokens_out": result.usage.output_tokens, "cached_tokens": result.usage.cached_tokens,
        "summary": result.content[:200]})
    await emit(mtg.run_id, "node_log", mtg.node_id,
               {"level": "info", "message": f"【{speaker_name}】{result.content[:200]}"})
    return {"turn_id": turn_id, "content": result.content, "data": data}


async def speak_many(mtg: MeetingSession, calls: list[dict], *,
                     resumable: bool = True) -> list[dict]:
    """多个参会者并行发言，结果顺序与 calls 一致。

    resumable 为真时，这一步这一轮里已经有完整发言的参会者不再调用，直接取回原发言。
    轮次取 calls 里的 turn_round，没给时取节点级轮次。
    """
    if not calls:
        return []
    rounds = {call.get("turn_round") or mtg.round_no() for call in calls}
    kinds = {call["turn_kind"] for call in calls}
    if len(rounds) > 1 or len(kinds) > 1:
        raise ValueError("同一批并行发言必须属于同一轮、同一类型")
    phase, round_no = _phase(mtg), rounds.pop()
    kind = kinds.pop()
    done: set[int] = set()
    if resumable:
        done = store.completed_speakers(mtg.meeting_id, phase=phase, round=round_no, kind=kind)
    results: list[dict | None] = [None] * len(calls)
    pending: list[int] = []
    for index, call in enumerate(calls):
        speaker_id = call["participant"]["id"]
        if speaker_id in done:
            found = store.find_turn(mtg.meeting_id, phase=phase, round=round_no,
                                    kind=kind, speaker_id=speaker_id)
            if found is not None:
                results[index] = found
                continue
        pending.append(index)
    if pending:
        await drain_interventions(mtg)
    # 一名发言者失败时不留下同组的孤儿调用：取消其余任务，重试时不会出现重复发言
    tasks = [asyncio.create_task(speak(mtg, drain=False, **calls[index]))
             for index in pending]
    try:
        outputs = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    for index, output in zip(pending, outputs):
        results[index] = output
    if any(result is None for result in results):
        raise RuntimeError("并行发言的结果不完整")
    return results


async def drain_interventions(mtg: MeetingSession) -> bool:
    """取出排队中的插话，写成用户发言，并让主持人回应一次。返回是否有插话。"""
    queued = store.take_queued_interventions(mtg.meeting_id)
    for item in queued:
        turn_id = store.add_turn(
            mtg.meeting_id, round=mtg.round_no(), phase=_phase(mtg), speaker_id=None,
            speaker_name="用户", kind="user_intervention", content=item["content"])
        store.attach_intervention_turn(item["id"], turn_id)
        await emit(mtg.run_id, "meeting_intervention_injected", mtg.node_id,
                   {"meeting_id": mtg.meeting_id, "intervention_id": item["id"], "turn_id": turn_id})
    if not queued:
        return False
    facilitator = next(iter(mtg.participants("facilitator")), None)
    if facilitator is not None:
        text = "\n".join(f"- {item['content']}" for item in queued)
        await speak(mtg, facilitator, kind="intervention_ack", turn_kind="facilitator_note",
                    segments=[Segment("append", f"## 用户在会议进行中插话\n{text}", name="插话")],
                    instruction="逐条回应用户的插话：你理解它要求什么，接下来怎么安排。只写一段话。",
                    drain=False)
    return True
