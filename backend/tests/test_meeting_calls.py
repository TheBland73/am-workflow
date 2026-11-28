"""角色发言：拼装、落盘、事件、结构化输出校验、断点续跑、插话。"""
from __future__ import annotations

import asyncio

from types import SimpleNamespace

import pytest

from app.conversation.builder import Segment
from app.db.base import init_db, session_scope
from app.db.models import Node, Run, Workflow
from app.engine.events import load_events
from app.engine.runners import NodeContext
from app.engine.templates import seed_defaults
from app.meeting import calls, store
from app.meeting.calls import CallOutputError, speak, speak_many
from app.meeting.session import MeetingSession
from app.models.types import Usage
from tests.meeting_fakes import MODEL, install

_counter = iter(range(1, 1000))


def _meeting() -> tuple[MeetingSession, list[dict]]:
    init_db()
    seed_defaults()
    index = next(_counter)
    with session_scope() as s:
        workflow_id = s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").one().id
        run_id = f"calls-run-{index}"
        s.add(Run(id=run_id, workflow_id=workflow_id, status="running"))
        s.flush()
        node = Node(node_key="discuss", type="MEETING", name="讨论会议", run_id=run_id,
                    status="running", config_json={"protocol": "discussion"})
        s.add(node)
        s.flush()
        ctx = NodeContext(run_id=run_id, node_id=node.id, node_key="discuss", type="MEETING",
                          name="讨论会议", phase="", config={"protocol": "discussion"},
                          assignee={}, attempt_id=0, attempt_no=1)
    meeting_id = store.create_meeting(run_id=run_id, node_id=node.id, node_key="discuss",
                                      protocol="discussion", charter={}, config={})
    store.update_meeting(meeting_id, phase="positions")
    store.add_participant(meeting_id, role_key="facilitator", display_name="主持人",
                          mode="facilitator", system_prompt="你是主持人")
    store.add_participant(meeting_id, role_key="architect", display_name="架构",
                          mode="participant", system_prompt="你是架构师")
    return MeetingSession(meeting_id=meeting_id, ctx=ctx), store.list_participants(meeting_id)


def _is_list_of_strings(data: dict) -> list[str]:
    must = data.get("must")
    return [] if isinstance(must, list) and all(isinstance(x, str) for x in must) else [
        "must 须是字符串数组"]


def _speak(mtg, speaker, **kwargs) -> dict:
    return asyncio.run(speak(mtg, speaker, kind="position", turn_kind="position", **kwargs))


def test_speak_writes_turn_and_emits_events(monkeypatch) -> None:
    install(monkeypatch, {"position": "我的立场是先保证可验证性。"})
    mtg, (_, speaker) = _meeting()
    result = _speak(mtg, speaker, segments=[Segment("frozen", "章程", name="章程")],
                    instruction="请写立场")
    assert result["content"].startswith("我的立场")
    assert result["data"] is None
    turns = store.list_turns(mtg.meeting_id)
    assert [(t["kind"], t["speaker_name"], t["cached_tokens"]) for t in turns] == [
        ("position", "架构", 60)]
    assert turns[0]["seq"] == 1
    types = [e["type"] for e in load_events(mtg.run_id)]
    assert types[:2] == ["meeting_turn_started", "meeting_turn_finished"]
    assert "node_log" in types
    # 开始发言的事件带会议内的序号：与发言增量接口（since_seq）同一套编号
    started = load_events(mtg.run_id)[0]["payload"]
    assert started["seq"] == turns[0]["seq"] == 1
    assert started["turn_id"] == turns[0]["id"]


def test_speak_validates_structured_output(monkeypatch) -> None:
    install(monkeypatch, {"position": '```json\n{"must": ["可验证"]}\n```'})
    mtg, (_, speaker) = _meeting()
    result = _speak(mtg, speaker, segments=[], instruction="请写立场",
                    structured=_is_list_of_strings)
    assert result["data"] == {"must": ["可验证"]}
    assert store.list_turns(mtg.meeting_id)[0]["structured"] == {"must": ["可验证"]}


def test_speak_rejects_bad_structured_output(monkeypatch) -> None:
    install(monkeypatch, {"position": '{"must": "不是数组"}'})
    mtg, (_, speaker) = _meeting()
    with pytest.raises(CallOutputError):
        _speak(mtg, speaker, segments=[], instruction="请写立场",
               structured=_is_list_of_strings)


def test_call_tag_is_appended_after_segments(monkeypatch) -> None:
    seen: list[dict] = []
    install(monkeypatch, {"position": "立场"}, seen)
    mtg, (_, speaker) = _meeting()
    _speak(mtg, speaker, segments=[Segment("append", "已有发言")], instruction="请写立场")
    text = seen[0]["text"]
    assert text.index("已有发言") < text.index("请写立场") < text.index("【调用类型：position】")


def test_frozen_segment_change_raises_warning_event(monkeypatch) -> None:
    install(monkeypatch, {"position": "立场"})
    mtg, (_, speaker) = _meeting()
    _speak(mtg, speaker, segments=[Segment("frozen", "章程 A", name="章程")], instruction="x")
    _speak(mtg, speaker, segments=[Segment("frozen", "章程 B", name="章程")], instruction="x")
    warnings = [e for e in load_events(mtg.run_id) if e["type"] == "context_prefix_changed"]
    assert len(warnings) == 1 and warnings[0]["payload"]["segments"] == ["章程"]


def test_speak_many_skips_completed_speakers(monkeypatch) -> None:
    install(monkeypatch, {"position": "立场"})
    mtg, (facilitator, speaker) = _meeting()
    other_id = store.add_participant(mtg.meeting_id, role_key="qa", display_name="测试",
                                     mode="participant", system_prompt="你是测试")
    other = next(p for p in store.list_participants(mtg.meeting_id) if p["id"] == other_id)
    _speak(mtg, speaker, segments=[], instruction="请写立场")
    call = {"kind": "position", "turn_kind": "position", "segments": [], "instruction": "请写立场"}
    results = asyncio.run(speak_many(mtg, [{**call, "participant": speaker},
                                           {**call, "participant": other}]))
    assert [r["content"] for r in results] == ["立场", "立场"]
    # 架构已有完整发言，没有再调一次
    assert [t["speaker_name"] for t in store.list_turns(mtg.meeting_id)] == ["架构", "测试"]


def test_intervention_is_injected_and_acknowledged(monkeypatch) -> None:
    install(monkeypatch, {"position": "立场", "intervention_ack": "收到，接下来先核对成本。"})
    mtg, (_, speaker) = _meeting()
    store.queue_intervention(mtg.meeting_id, content="注意成本")
    _speak(mtg, speaker, segments=[], instruction="请写立场")
    turns = store.list_turns(mtg.meeting_id)
    assert [(t["kind"], t["speaker_name"]) for t in turns][:2] == [
        ("user_intervention", "用户"), ("facilitator_note", "主持人")]
    types = [e["type"] for e in load_events(mtg.run_id)]
    assert "meeting_intervention_injected" in types


def test_shared_charter_comes_before_persona(monkeypatch) -> None:
    """章程排在人设之前：它和系统提示在全体角色之间是同一段前缀。"""
    seen: list[dict] = []
    install(monkeypatch, {"position": "立场"}, seen)
    mtg, (_, speaker) = _meeting()
    _speak(mtg, speaker, charter=[Segment("frozen", "会议章程内容", name="章程")],
           segments=[Segment("append", "已有发言")], instruction="请写立场")
    text = seen[0]["text"]
    assert text.index("会议章程内容") < text.index("## 你的角色") < text.index("已有发言")
    assert text.index("已有发言") < text.index("请写立场") < text.index("【调用类型：position】")


def test_switching_speaker_is_not_a_frozen_change(monkeypatch) -> None:
    install(monkeypatch, {"position": "立场"})
    mtg, (_, speaker) = _meeting()
    other_id = store.add_participant(mtg.meeting_id, role_key="qa", display_name="测试",
                                     mode="participant", system_prompt="你是测试")
    other = next(p for p in store.list_participants(mtg.meeting_id) if p["id"] == other_id)
    _speak(mtg, speaker, segments=[], instruction="x")
    _speak(mtg, other, segments=[], instruction="x")
    assert [e for e in load_events(mtg.run_id) if e["type"] == "context_prefix_changed"] == []


def test_turn_round_is_recorded_on_the_turn(monkeypatch) -> None:
    install(monkeypatch, {"position": "立场"})
    mtg, (_, speaker) = _meeting()
    asyncio.run(speak(mtg, speaker, kind="position", turn_kind="stance", segments=[],
                      instruction="请表态", turn_round=7))
    assert store.list_turns(mtg.meeting_id)[0]["round"] == 7


def test_meeting_input_budget_takes_the_smallest_participant(monkeypatch) -> None:
    """章程按会议里最小的可用窗口截：每个参会者都要装得下同一份章程。"""
    from app.models.budget import ContextBudget

    mtg, _ = _meeting()
    small = SimpleNamespace(context_window=8192, max_output_tokens=1024)
    big = SimpleNamespace(context_window=128_000, max_output_tokens=8192)
    monkeypatch.setattr(calls, "_model_for",
                        lambda participant: small if participant["display_name"] == "主持人" else big)
    assert calls.meeting_input_budget(mtg) == ContextBudget(8192, 1024).input_budget


def test_charter_uses_the_meeting_budget_instead_of_fixed_caps(monkeypatch) -> None:
    from app.meeting.protocols import discussion

    install(monkeypatch, {"position": "立场"})
    big = SimpleNamespace(id=1, model_name="mock-big", display_name="Mock Big",
                          context_window=200_000, max_output_tokens=8192, cap_tools=True)
    monkeypatch.setattr(calls, "_model_for", lambda participant: big)
    mtg, _ = _meeting()
    store.update_meeting(mtg.meeting_id, charter_json={
        "title": "讨论会议章程", "sections": [], "materials": "材料" * 10_000})
    segments = discussion._charter(mtg)
    assert segments[1].text.count("材") == 10_000          # 大窗口下背景材料全文保留


def _medium_model():
    return SimpleNamespace(id=1, model_name="mock-medium", display_name="Mock Medium",
                           context_window=8192, max_output_tokens=1024, cap_tools=True)


def test_parallel_speakers_summarize_the_shared_history_once(monkeypatch) -> None:
    """同一批并行发言看到的是同一段历史：摘要只做一遍，后来者直接用已存下的摘要。

    历史按摘要模型的实际预算分成 2 块（旧实现按固定 24000 分块、再在摘要器入口静默截尾，
    只调一次却丢掉块尾内容）；这里顺带断言全部历史都进了摘要，没有被截断丢失。
    """
    seen: list[dict] = []
    install(monkeypatch, {"position": "立场", "summarize": "较早交锋的摘要"}, seen)
    monkeypatch.setattr(calls, "_model_for", lambda participant: _medium_model())
    mtg, (_, speaker) = _meeting()
    other_id = store.add_participant(mtg.meeting_id, role_key="qa", display_name="测试",
                                     mode="participant", system_prompt="你是测试")
    other = next(p for p in store.list_participants(mtg.meeting_id) if p["id"] == other_id)
    old = [Segment("append", "字" * 150, name=f"发言{index}") for index in range(40)]
    call = {"kind": "position", "turn_kind": "position",
            "segments": [Segment("frozen", "章程", name="章程"), *old],
            "instruction": "请写立场"}

    asyncio.run(speak_many(mtg, [{**call, "participant": speaker},
                                 {**call, "participant": other}]))

    assert [item["kind"] for item in seen].count("summarize") == 2
    summarizer_texts = "".join(item["text"] for item in seen if item["kind"] == "summarize")
    summarized = 40 - calls.DEFAULT_POLICY.keep_recent
    assert summarizer_texts.count("字") == summarized * 150   # 压进去的历史没有被静默截断
    stored = store.get_state(mtg.meeting_id)["compaction"]
    assert stored["covered_len"] > 0 and stored["summary"]


def test_the_summary_is_reused_on_the_next_turn(monkeypatch) -> None:
    seen: list[dict] = []
    install(monkeypatch, {"position": "立场", "summarize": "较早交锋的摘要"}, seen)
    monkeypatch.setattr(calls, "_model_for", lambda participant: _medium_model())
    mtg, (_, speaker) = _meeting()
    charter = Segment("frozen", "章程", name="章程")
    old = [Segment("append", "字" * 150, name=f"发言{index}") for index in range(40)]

    _speak(mtg, speaker, segments=[charter, *old], instruction="请写立场")
    first = [item["kind"] for item in seen].count("summarize")
    assert first == 2                                 # 历史按摘要模型预算分成 2 块
    assert store.get_state(mtg.meeting_id)["compaction"]["round"] == 1

    # 又多了两条发言，但替换成摘要后仍在阈值以下：不再调摘要
    newer = old + [Segment("append", "字" * 150, name=f"发言{index}") for index in range(40, 42)]
    _speak(mtg, speaker, segments=[charter, *newer], instruction="请写立场")
    assert [item["kind"] for item in seen].count("summarize") == first


def test_a_changed_prefix_invalidates_the_stored_summary(monkeypatch) -> None:
    """迟到的更早发言会改变已覆盖的前缀：不能拿旧摘要冒充已覆盖，必须重新摘要。"""
    seen: list[dict] = []
    install(monkeypatch, {"position": "立场", "summarize": "较早交锋的摘要"}, seen)
    monkeypatch.setattr(calls, "_model_for", lambda participant: _medium_model())
    mtg, (_, speaker) = _meeting()
    charter = Segment("frozen", "章程", name="章程")
    old = [Segment("append", "字" * 150, name=f"发言{index}") for index in range(40)]

    _speak(mtg, speaker, segments=[charter, *old], instruction="请写立场")
    first = [item["kind"] for item in seen].count("summarize")

    inserted = [Segment("append", "迟到的发言", name="发言-1"), *old]
    _speak(mtg, speaker, segments=[charter, *inserted], instruction="请写立场")
    assert [item["kind"] for item in seen].count("summarize") > first


def test_speak_compacts_old_turns_at_threshold(monkeypatch) -> None:
    """追加区占到阈值：较早的发言先压成摘要再发言。纪要不变，压缩留痕。"""
    seen: list[dict] = []
    install(monkeypatch, {"position": "立场", "summarize": "较早交锋的摘要"}, seen)
    small = SimpleNamespace(id=1, model_name="mock-small", display_name="Mock Small",
                            context_window=1600, max_output_tokens=256, cap_tools=True)
    monkeypatch.setattr(calls, "_model_for", lambda participant: small)
    mtg, (_, speaker) = _meeting()
    old = [Segment("append", "字" * 150, name=f"发言{index}") for index in range(10)]

    _speak(mtg, speaker, segments=[Segment("frozen", "章程", name="章程"), *old],
           instruction="请写立场")

    # 较旧的发言压成一条摘要；压完仍高于触发线时会继续把更早的整组并入，
    # 所以摘要调用可能不止一次，保留的逐字发言也少于 keep_recent。
    kinds = [call["kind"] for call in seen]
    assert kinds[0] == "summarize" and kinds[-1] == "position"
    assert "较早交锋的摘要" in seen[-1]["text"]
    assert 0 < seen[-1]["text"].count("字" * 150) <= 6
    assert "context_compacted" in [e["type"] for e in load_events(mtg.run_id)]
    assert len(store.list_turns(mtg.meeting_id)) == 1          # 做摘要不算一次发言


def test_summarization_failure_falls_back_to_uncompressed(monkeypatch) -> None:
    """摘要调用失败时按原视图继续：压缩只是优化，不该让这次发言作废。"""
    install(monkeypatch, {"position": "立场"})
    small = SimpleNamespace(id=1, model_name="mock-small", display_name="Mock Small",
                            context_window=1600, max_output_tokens=256, cap_tools=True)
    monkeypatch.setattr(calls, "_model_for", lambda participant: small)
    mtg, (_, speaker) = _meeting()
    old = [Segment("append", "字" * 150, name=f"发言{index}") for index in range(10)]

    async def broken(*args, **kwargs):
        raise RuntimeError("摘要服务挂了")

    monkeypatch.setattr(calls, "compact_messages", broken)
    result = _speak(mtg, speaker, segments=[Segment("frozen", "章程", name="章程"), *old],
                    instruction="请写立场")
    assert result["content"] == "立场"
    logs = [e for e in load_events(mtg.run_id) if e["type"] == "node_log"]
    assert any("压缩失败" in e["payload"].get("message", "") for e in logs)


def test_resumed_turn_keeps_data_shape(monkeypatch) -> None:
    install(monkeypatch, {"position": '{"must": ["可验证"]}'})
    mtg, (_, speaker) = _meeting()
    _speak(mtg, speaker, segments=[], instruction="请写立场",
           structured=_is_list_of_strings)
    call = {"kind": "position", "turn_kind": "position", "segments": [], "instruction": "请写立场"}
    results = asyncio.run(speak_many(mtg, [{**call, "participant": speaker}]))
    assert results[0]["data"] == {"must": ["可验证"]}


def test_resumed_turn_without_structure_has_data_none(monkeypatch) -> None:
    install(monkeypatch, {"position": "立场"})
    mtg, (_, speaker) = _meeting()
    _speak(mtg, speaker, segments=[], instruction="请写立场")
    call = {"kind": "position", "turn_kind": "position", "segments": [], "instruction": "请写立场"}
    results = asyncio.run(speak_many(mtg, [{**call, "participant": speaker}]))
    assert results[0]["data"] is None


def test_speak_many_preserves_request_order(monkeypatch) -> None:
    install(monkeypatch, {"position": lambda text: "甲" if "架构师" in text else "乙"})
    mtg, (_, speaker) = _meeting()
    other_id = store.add_participant(mtg.meeting_id, role_key="qa", display_name="测试",
                                     mode="participant", system_prompt="你是测试")
    other = next(p for p in store.list_participants(mtg.meeting_id) if p["id"] == other_id)
    call = {"kind": "position", "turn_kind": "position", "segments": [], "instruction": "请写立场"}
    results = asyncio.run(speak_many(mtg, [{**call, "participant": other},
                                           {**call, "participant": speaker}]))
    assert [r["content"] for r in results] == ["乙", "甲"]


def test_speak_many_cancels_siblings_on_failure(monkeypatch) -> None:
    """一名发言者失败时不留下同组的孤儿调用，重试时不会写出重复发言。"""
    mtg, (_, speaker) = _meeting()
    other_id = store.add_participant(mtg.meeting_id, role_key="qa", display_name="测试",
                                     mode="participant", system_prompt="你是测试")
    other = next(p for p in store.list_participants(mtg.meeting_id) if p["id"] == other_id)
    finished: list[str] = []

    async def fake_run_agent(**kwargs):
        text = "\n".join(m.content for m in kwargs.get("extra_messages") or [])
        if "架构师" in text:
            raise CallOutputError("模型输出不符合要求")
        await asyncio.sleep(0.05)
        finished.append("测试")
        return SimpleNamespace(content="立场", usage=Usage(10, 5, 0), steps=1,
                               stopped_by="final")

    monkeypatch.setattr(calls, "run_agent", fake_run_agent)
    monkeypatch.setattr(calls, "_model_for", lambda participant: MODEL)
    call = {"kind": "position", "turn_kind": "position", "segments": [], "instruction": "请写立场"}
    with pytest.raises(CallOutputError):
        asyncio.run(speak_many(mtg, [{**call, "participant": speaker},
                                     {**call, "participant": other}]))
    assert finished == []                                     # 同组任务已在后台被取消
    assert all(t["content"] == "" for t in store.list_turns(mtg.meeting_id))


# ---------------- 断点评审的回归用例 ----------------
def test_intervention_ack_failure_keeps_it_queued(monkeypatch) -> None:
    """主持人回应失败时插话仍在队列里；重试复用同一条用户发言，不重复也不假标已注入。"""
    install(monkeypatch, {"position": "立场"})                 # 没有准备 intervention_ack
    mtg, (_, speaker) = _meeting()
    store.queue_intervention(mtg.meeting_id, content="注意成本")
    with pytest.raises(AssertionError):
        _speak(mtg, speaker, segments=[], instruction="请写立场")

    assert [item["content"] for item in store.take_queued_interventions(mtg.meeting_id)] == [
        "注意成本"]

    install(monkeypatch, {"position": "立场", "intervention_ack": "收到，先核对成本。"})
    assert asyncio.run(calls.drain_interventions(mtg)) is True
    assert store.take_queued_interventions(mtg.meeting_id) == []
    user_turns = [t for t in store.list_turns(mtg.meeting_id)
                  if t["kind"] == "user_intervention"]
    assert len(user_turns) == 1                               # 重试没有写出第二遍


def test_a_too_large_previous_proposal_fails_instead_of_being_truncated(monkeypatch) -> None:
    """修订要看到上一版全文：放不下时致命失败，不能截短后当作全文写回。"""
    from app.engine.runners import NodeFailure

    install(monkeypatch, {"position": "立场"})
    small = SimpleNamespace(id=1, model_name="mock-small", display_name="Mock Small",
                            context_window=1600, max_output_tokens=256, cap_tools=True)
    monkeypatch.setattr(calls, "_model_for", lambda participant: small)
    mtg, (_, speaker) = _meeting()
    previous = Segment("append", "方案正文" * 2000, name="上一版方案")

    with pytest.raises(NodeFailure, match="上一版建设方案"):
        _speak(mtg, speaker,
               segments=[Segment("frozen", "章程", name="章程"), previous], instruction="修订")


def test_intervention_ack_is_not_repeated_after_a_crash(monkeypatch) -> None:
    """主持人的插话回应落盘后被杀：重入按同一批插话的键找回回应，不再重复发言。"""
    install(monkeypatch, {"position": "立场", "intervention_ack": "收到。"}, None)
    mtg, _ = _meeting()
    store.queue_intervention(mtg.meeting_id, content="注意成本")
    speaker = next(p for p in store.list_participants(mtg.meeting_id)
                   if p["mode"] == "participant")
    _speak(mtg, speaker, segments=[], instruction="请写立场", drain=False)
    armed = {"on": True}
    original_emit = calls.emit

    async def flaky_emit(run_id_, type_, node_id_, payload):
        if armed["on"] and type_ == "meeting_turn_finished" \
                and payload.get("kind") == "facilitator_note":
            armed["on"] = False                    # 回应已落盘：进程在这里被杀
            raise asyncio.CancelledError()
        return await original_emit(run_id_, type_, node_id_, payload)

    monkeypatch.setattr(calls, "emit", flaky_emit)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(calls.drain_interventions(mtg))
    monkeypatch.setattr(calls, "emit", original_emit)

    asyncio.run(calls.drain_interventions(mtg))

    notes = [t for t in store.list_turns(mtg.meeting_id) if t["kind"] == "facilitator_note"]
    assert len(notes) == 1


def test_charter_caps_scale_down_on_a_small_budget() -> None:
    """小窗口下章程与材料的上限让位：两个固定下限加起来不顶穿整段预算。"""
    from app.meeting.render import _caps

    charter, materials = _caps(8000)
    assert charter == 4000 and materials == 2000 and charter + materials <= 8000


# ---------------- 单次输出上限的截断提示 ----------------
def _cap_model():
    """窗口 1600、最大输出 256：输出预留被 15% 窗口压到 240，单次上限仍取 256 下限。"""
    return SimpleNamespace(id=1, model_name="mock-cap", display_name="Mock Cap",
                           context_window=1600, max_output_tokens=256, cap_tools=True)


def _install_capped_output(monkeypatch, output_tokens: int) -> None:
    async def fake_run_agent(**kwargs):
        return SimpleNamespace(content="立场", usage=Usage(100, output_tokens, 0), steps=1,
                               stopped_by="final")

    monkeypatch.setattr(calls, "run_agent", fake_run_agent)
    monkeypatch.setattr(calls, "_model_for", lambda participant: _cap_model())


def _node_log_payloads(mtg: MeetingSession) -> list[dict]:
    return [e["payload"] for e in load_events(mtg.run_id) if e["type"] == "node_log"]


def test_speak_warns_when_the_output_reaches_the_cap(monkeypatch) -> None:
    """输出顶到单次上限时提示可能被截断：自由文本（如建设方案）只提示，由人决定调大上限。"""
    _install_capped_output(monkeypatch, 256)
    mtg, (_, speaker) = _meeting()
    _speak(mtg, speaker, segments=[], instruction="请写立场")
    warnings = [payload for payload in _node_log_payloads(mtg)
                if payload.get("level") == "warn"]
    assert any("可能被截断" in payload.get("message", "") for payload in warnings)
    assert any("【架构】" in payload["message"] and "256 token" in payload["message"]
               for payload in warnings)


def test_speak_does_not_warn_below_the_cap(monkeypatch) -> None:
    _install_capped_output(monkeypatch, 255)
    mtg, (_, speaker) = _meeting()
    _speak(mtg, speaker, segments=[], instruction="请写立场")
    assert [payload for payload in _node_log_payloads(mtg)
            if "可能被截断" in payload.get("message", "")] == []
