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
from tests.meeting_fakes import install

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
