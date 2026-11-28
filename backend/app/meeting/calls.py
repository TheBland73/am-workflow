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
import hashlib
from typing import Callable

from app.artifacts.store import strip_outer_fence
from app.conversation.builder import Segment
from app.conversation.compact import (SUMMARY_PREFIX, CompactionStats, compact_messages,
                                      estimate_messages, summary_of)
from app.conversation.policy import DEFAULT_POLICY, policy_for_run
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


def meeting_input_budget(mtg: MeetingSession, default: int = 32_000) -> int:
    """这场会议里最小的可用输入预算：章程按它截，保证每个参会者都装得下同一份章程。"""
    budgets: list[int] = []
    for participant in mtg.participants():
        try:
            model = _model_for(participant)
        except NodeFailure:
            continue
        budgets.append(ContextBudget(model.context_window, model.max_output_tokens).input_budget)
    return min(budgets) if budgets else default


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


def _prefix_hash(append: list[Segment]) -> str:
    """已压进摘要的那一段追加内容的指纹：内容或顺序变了，旧摘要就不能再用。"""
    digest = hashlib.sha256()
    for segment in append:
        digest.update(segment.role.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(segment.text.encode("utf-8"))
        digest.update(b"\x01")
    return digest.hexdigest()


def _stored_compaction(mtg: MeetingSession) -> dict | None:
    """这一轮存下的压缩结果；轮次不对、内容缺失或形状不对都当没有。"""
    stored = mtg.state().get("compaction")
    if not isinstance(stored, dict):
        return None
    try:
        round_no = int(stored.get("round") or 0)
    except (TypeError, ValueError):
        return None
    if round_no != mtg.meeting_round():
        return None
    if not isinstance(stored.get("covered_len"), int) or not stored.get("summary"):
        return None
    return stored


def _covered_len(append: list[Segment], stored: dict | None) -> int | None:
    """存下的摘要覆盖了 append 的前多少条；前缀对不上就不复用。"""
    if stored is None:
        return None
    covered = stored["covered_len"]
    if covered <= 0 or covered > len(append):
        return None
    if _prefix_hash(append[:covered]) != stored.get("prefix_hash"):
        return None
    return covered


def _assembled(segments: list[Segment], summary: str, kept: list[Segment],
               previous: list[Segment]) -> list[Segment]:
    """把冻结段、一条摘要、未覆盖的追加段与上一版方案拼成这次的视图。"""
    return ([s for s in segments if s.kind == "frozen"]
            + [Segment("append", SUMMARY_PREFIX + summary, name="交锋摘要")]
            + list(kept) + previous)


async def _fit(mtg: MeetingSession, model, segments: list[Segment],
               input_budget: int) -> list[Segment]:
    """追加区占到压缩阈值时，把其中较早的发言压成一条摘要。只改这次发出的视图，不改纪要。

    压缩只动追加区：冻结区（章程、人设、账本快照）是前缀缓存命中的部分，本轮内逐字节不变。
    摘要按"会议 + 轮次"存进会议状态：同一轮的并行发言与后续发言直接复用，不重复摘要。
    摘要失败时按原视图继续——压缩只是优化，不该让一次发言作废；预算耗尽是例外。
    """
    try:
        policy = policy_for_run(mtg.run_id)
    except Exception:
        policy = DEFAULT_POLICY
    # 上一版方案不能压缩也不能截短：修订必须在看到全文的前提下进行（与 C2 的预算口径一致）
    previous = [s for s in segments if s.name == "上一版方案"]
    append = [s for s in segments if s.kind == "append" and s.name != "上一版方案"]
    fixed = estimate_tokens(BASE_SYSTEM_PROMPT) + sum(
        estimate_tokens(s.text) for s in segments if s.kind == "frozen")
    fixed += sum(estimate_tokens(s.text) for s in previous)
    threshold = policy.compact_at * input_budget
    size = fixed + sum(estimate_tokens(s.text) for s in append) + _TAIL_RESERVE
    if size < threshold:
        return segments
    # 并行发言会同时进来：串行进出，后来者读到刚存下的摘要就直接复用
    async with mtg.compaction_lock():
        stored = _stored_compaction(mtg)
        covered = _covered_len(append, stored)
        if covered:
            base = [Message(role="system", content="系统"),
                    Message(role="user", content=SUMMARY_PREFIX + stored["summary"])]
            base += [Message(role=s.role, content=s.text) for s in append[covered:]]
            if fixed + estimate_messages(base) + _TAIL_RESERVE < threshold:
                # 摘要 + 尚未覆盖的发言已经装得下：不再调摘要
                return _assembled(segments, stored["summary"], append[covered:], previous)
        else:
            base = [Message(role="system", content="系统")]
            base += [Message(role=s.role, content=s.text) for s in append]
        stats = CompactionStats()

        async def report_progress(index: int, total: int | None) -> None:
            done = f"/{total}" if total else ""
            await emit(mtg.run_id, "node_log", mtg.node_id, {
                "level": "info", "message": f"压缩上下文：第 {index}{done} 块"})

        try:
            compacted, before, after = await compact_messages(
                base, summarize=lambda text: _summarize_exchanges(mtg, model, text),
                input_budget=max(1, input_budget - fixed - _TAIL_RESERVE), policy=policy,
                stats=stats, on_progress=report_progress)
        except BudgetExceeded:
            raise
        except Exception as exc:
            await emit(mtg.run_id, "node_log", mtg.node_id, {
                "level": "warn", "message": f"会议上下文压缩失败，按原内容继续：{exc}"[:300]})
            return segments
        if compacted is base:
            # 没有可压的内容（例如保留条数已经覆盖全部追加段）
            if covered:
                return _assembled(segments, stored["summary"], append[covered:], previous)
            return segments
        new_covered = (covered or 0) + stats.cut - (1 if covered else 0)
        summary = (summary_of(compacted) or "")[len(SUMMARY_PREFIX):]
        kept_source = append[new_covered:]
        kept = []
        for offset, message in enumerate(compacted[2:]):
            name = kept_source[offset].name if offset < len(kept_source) else "较早发言"
            kept.append(Segment("append", message.content, role=message.role, name=name))
        mtg.set_state(compaction={
            "round": mtg.meeting_round(), "covered_len": new_covered,
            "prefix_hash": _prefix_hash(append[:new_covered]), "summary": summary})
        await record_compaction(
            run_id=mtg.run_id, node_id=mtg.node_id, attempt_id=mtg.ctx.attempt_id,
            scope="meeting", summary=summary, before_tokens=before + fixed,
            after_tokens=after + fixed, calls=stats.calls,
            summarized_tokens=stats.summarized_tokens)
        return ([s for s in segments if s.kind == "frozen"]
                + [Segment("append", SUMMARY_PREFIX + summary, name="交锋摘要")]
                + kept + previous)


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
        # 上一版方案被截短意味着修订会丢掉后半篇：宁可失败，也不静默写回残缺内容
        if any("上一版方案" in name for name in built.truncated):
            raise NodeFailure("上一版建设方案超出上下文预算，无法在保留全文的前提下修订", fatal=True)
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
        "seq": store.turn_seq(mtg.meeting_id, turn_id),        # 会议内的序号，与发言增量接口同一套
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


async def reuse_or_speak(mtg: MeetingSession, key: str, *, require_data: bool = False,
                         **kwargs) -> dict:
    """步骤的模型调用只做一次：状态里记着这一步的发言就直接复用。

    复用键在发言之前先认领（记下当时的发言行数）：崩溃落在"发言落盘、编号写上"之间时，
    重入按"认领之后才落盘"找回这一次的发言，不重复调用模型。
    """
    turns = dict(mtg.state().get("step_turns") or {})
    remembered = turns.get(key)
    if isinstance(remembered, int):
        found = store.get_turn(mtg.meeting_id, remembered)
        if found is not None and (found.get("data") is not None or not require_data):
            return found
    elif isinstance(remembered, dict):
        found = store.find_turn(mtg.meeting_id, phase=mtg.meeting()["phase"],
                                round=int(kwargs.get("turn_round") or mtg.round_no()),
                                kind=kwargs.get("turn_kind"),
                                speaker_id=kwargs["participant"]["id"],
                                after_seq=int(remembered.get("seq") or 0))
        if found is not None and (found.get("data") is not None or not require_data):
            return found
    turns = dict(mtg.state().get("step_turns") or {})
    turns[key] = {"seq": store.count_turns(mtg.meeting_id)}     # 认领：只认这次调用之后的发言
    mtg.set_state(step_turns=turns)
    result = await speak(mtg, **kwargs)
    turns = dict(mtg.state().get("step_turns") or {})
    turns[key] = result["turn_id"]
    mtg.set_state(step_turns=turns)
    return result


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
    """取出排队中的插话，写成用户发言，并让主持人回应一次。返回是否有插话。

    状态改为"已注入"要等主持人回应成功之后：回应失败时插话仍在队列里，
    下次重试会复用已经写下的那条用户发言，不会重复，也不会出现"标了已注入却没回应"。
    """
    queued = store.take_queued_interventions(mtg.meeting_id)
    if not queued:
        return False
    phase = _phase(mtg)
    round_no = mtg.round_no()
    existing = {turn["content"]: turn["id"] for turn in store.round_turns(
        mtg.meeting_id, round=round_no, kinds=["user_intervention"])}
    pairs: list[tuple[int, int]] = []
    for item in queued:
        turn_id = existing.get(item["content"])
        if turn_id is None:
            turn_id = store.add_turn(
                mtg.meeting_id, round=round_no, phase=phase, speaker_id=None,
                speaker_name="用户", kind="user_intervention", content=item["content"])
            existing[item["content"]] = turn_id
        pairs.append((item["id"], turn_id))
    for item_id, turn_id in pairs:
        await emit(mtg.run_id, "meeting_intervention_injected", mtg.node_id,
                   {"meeting_id": mtg.meeting_id, "intervention_id": item_id,
                    "turn_id": turn_id})
    facilitator = next(iter(mtg.participants("facilitator")), None)
    if facilitator is not None:
        text = "\n".join(f"- {item['content']}" for item in queued)
        from app.meeting import render  # 延迟导入：避免与 render → rules 的导入顺序纠缠

        charter = render.charter_segments(mtg.meeting().get("charter") or {},
                                          input_budget=meeting_input_budget(mtg))
        ledger = render.ledger_segment(store.list_decision_points(mtg.meeting_id),
                                       round_no=mtg.meeting_round())
        # 认领式复用：回应落盘后被杀，重入按同一批插话的键找回这条回应，不再重复发言
        batch = ",".join(str(item["id"]) for item in sorted(queued, key=lambda i: i["id"]))
        await reuse_or_speak(
            mtg, f"intervention_ack:{batch}",
            participant=facilitator, kind="intervention_ack", turn_kind="facilitator_note",
            charter=charter,
            segments=[ledger, Segment("append", f"## 用户在会议进行中插话\n{text}",
                                      name="插话")],
            instruction="逐条回应用户的插话：你理解它要求什么，接下来怎么安排。只写一段话。",
            drain=False)
    for item_id, turn_id in pairs:
        store.attach_intervention_turn(item_id, turn_id)
    return True
