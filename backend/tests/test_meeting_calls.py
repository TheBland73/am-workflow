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
    types = [e["type"] for e in load_events(mtg.run_id)]
    assert types[:2] == ["meeting_turn_started", "meeting_turn_finished"]
    assert "node_log" in types


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
