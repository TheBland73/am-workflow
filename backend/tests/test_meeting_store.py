"""会议存取层：建会、参会者、发言的序号、决策点的 upsert、插话队列。"""
from __future__ import annotations

import pytest

from app.db.base import init_db, session_scope
from app.db.models import Node, Run, Workflow
from app.engine.templates import seed_defaults
from app.meeting import store

_counter = iter(range(1, 1000))


def _node() -> tuple[str, int]:
    """新建一个运行和一个会议节点，返回 (运行 id, 节点 id)。会议的外键要指向真实的运行。"""
    init_db()
    seed_defaults()
    suffix = str(next(_counter))
    with session_scope() as s:
        workflow_id = s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").one().id
        run_id = f"store-run-{suffix}"
        s.add(Run(id=run_id, workflow_id=workflow_id, status="running"))
        s.flush()
        node = Node(node_key="discuss", type="MEETING", name="讨论会议",
                    run_id=run_id, status="pending")
        s.add(node)
        s.flush()
        return run_id, node.id


def _meeting() -> tuple[int, int]:
    run_id, node_id = _node()
    return node_id, store.create_meeting(run_id=run_id, node_id=node_id, node_key="discuss",
                                         protocol="discussion", charter={}, config={})


def test_create_and_latest() -> None:
    run_id, node_id = _node()
    assert store.latest_meeting_for_node(node_id) is None
    first = store.create_meeting(run_id=run_id, node_id=node_id, node_key="discuss",
                                 protocol="discussion", charter={"a": 1}, config={"b": 2})
    meeting = store.get_meeting(first)
    assert (meeting["status"], meeting["charter"], meeting["config"], meeting["state"]) == (
        "active", {"a": 1}, {"b": 2}, {})
    store.update_meeting(first, status="completed", finished_at=True)
    second = store.create_meeting(run_id=run_id, node_id=node_id, node_key="discuss",
                                  protocol="discussion", charter={}, config={})
    assert store.latest_meeting_for_node(node_id)["id"] == second
    done = store.get_meeting(first)
    assert done["status"] == "completed" and done["finished_at"] is not None
    assert store.get_meeting(second)["finished_at"] is None


def test_participants_and_turn_sequence() -> None:
    node_id, meeting_id = _meeting()
    speaker = store.add_participant(meeting_id, role_key="architect", display_name="架构",
                                    mode="participant", expertise=["架构", "安全"])
    temporary = store.add_participant(meeting_id, role_key=None, display_name="临时", mode="participant",
                                      source="adhoc")
    assert [p["id"] for p in store.list_participants(meeting_id)] == [speaker, temporary]
    assert store.list_participants(meeting_id)[0]["expertise"] == ["架构", "安全"]

    # 发言行在调用模型之前就建好；没说完（内容为空）的那条不算数
    first = store.add_turn(meeting_id, round=1, phase="positions", speaker_id=speaker,
                           speaker_name="架构", kind="position")
    assert store.completed_speakers(meeting_id, phase="positions", round=1, kind="position") == set()
    second = store.add_turn(meeting_id, round=1, phase="positions", speaker_id=None,
                            speaker_name="主持人", kind="facilitator_note", reply_to=first)
    store.finish_turn(first, content="立场", structured={"must": ["可验证"]}, tokens_in=10,
                      tokens_out=5, cached_tokens=4)

    turns = store.list_turns(meeting_id)
    assert [(t["seq"], t["kind"], t["speaker_id"]) for t in turns] == [
        (1, "position", speaker), (2, "facilitator_note", None)]
    assert turns[0]["content"] == "立场" and turns[0]["cached_tokens"] == 4
    assert turns[0]["structured"] == {"must": ["可验证"]}
    assert turns[1]["reply_to"] == first and turns[1]["content"] == ""
    assert [t["seq"] for t in store.list_turns(meeting_id, since_seq=1)] == [2]
    assert store.completed_speakers(meeting_id, phase="positions", round=1, kind="position") == {speaker}

    # 被移出名单的参会者默认不再列出，但历史发言还在
    store.update_participant(temporary, status="removed")
    assert [p["id"] for p in store.list_participants(meeting_id)] == [speaker]
    assert len(store.list_participants(meeting_id, active_only=False)) == 2


def test_decision_point_upsert_keeps_id() -> None:
    _, meeting_id = _meeting()
    point_id = store.upsert_decision_point(meeting_id, "D1", title="先做哪个平台")
    again = store.upsert_decision_point(meeting_id, "D1", title="先做哪个平台", question="Web 还是桌面")
    assert again == point_id
    store.upsert_decision_point(meeting_id, "D2", title="用什么存储")
    # 表态矩阵的键是参会者 id 的字符串：这一列存成 JSON，整数键读出来会变成字符串，
    # 所以后面的代码一律按 str(参会者 id) 存取
    stance = {"level": 1, "reason": "都能接受", "round": 1}
    store.update_decision_point(point_id, status="agreed", conclusion="Web",
                                stances_json={"7": stance})
    points = store.list_decision_points(meeting_id)
    assert [(p["index"], p["status"], p["conclusion"]) for p in points] == [
        ("D1", "agreed", "Web"), ("D2", "open", "")]
    assert points[0]["question"] == "Web 还是桌面" and points[0]["stances"] == {"7": stance}


def test_intervention_queue() -> None:
    _, meeting_id = _meeting()
    first = store.queue_intervention(meeting_id, content="注意成本")
    second = store.queue_intervention(meeting_id, content="补充：要离线可用")
    contents = lambda: [item["content"] for item in store.take_queued_interventions(meeting_id)]
    assert contents() == ["注意成本", "补充：要离线可用"]

    # 取出不改状态：写进纪要之前如果中断，下次还会被取出，插话不会丢
    assert contents() == ["注意成本", "补充：要离线可用"]
    store.attach_intervention_turn(first, turn_id=41)
    assert contents() == ["补充：要离线可用"]              # 写进纪要的那条才离开队列
    store.attach_intervention_turn(second, turn_id=42)
    assert contents() == []


def test_state_roundtrip() -> None:
    _, meeting_id = _meeting()
    store.set_state(meeting_id, {"open_points": ["D2"], "round": 3})
    assert store.get_state(meeting_id) == {"open_points": ["D2"], "round": 3}
    store.update_meeting(meeting_id, phase="exchange", round=2)
    meeting = store.get_meeting(meeting_id)
    assert (meeting["phase"], meeting["round"], meeting["status"]) == ("exchange", 2, "active")


def test_missing_rows_raise_instead_of_silently_doing_nothing() -> None:
    # 状态存不进去却没有声音，后面会按"已保存"继续跑；id 不存在时要报错
    _, meeting_id = _meeting()
    no_such = 999_999
    with pytest.raises(LookupError):
        store.get_meeting(no_such)
    with pytest.raises(LookupError):
        store.update_meeting(no_such, status="completed")
    with pytest.raises(LookupError):
        store.set_state(no_such, {"x": 1})
    with pytest.raises(LookupError):
        store.finish_turn(no_such, content="x")
    with pytest.raises(LookupError):
        store.update_participant(no_such, status="removed")
    with pytest.raises(LookupError):
        store.update_decision_point(no_such, status="agreed")
    with pytest.raises(LookupError):
        store.attach_intervention_turn(no_such, turn_id=1)
    assert store.get_state(meeting_id) == {}              # 出错的调用没有留下半截状态
