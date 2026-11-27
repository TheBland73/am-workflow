"""会议运行器：步进循环、挂起与恢复、续开与重开的判定、会议内确认点、回流轮次用尽转人工。不调模型。"""
from __future__ import annotations

import asyncio

import pytest

from app.db.base import init_db, session_scope
from app.db.models import Event, Gate, Meeting, Node, Run, Workflow
from app.engine.gates import get_gate
from app.engine.orchestrator import orchestrator
from app.engine.runners import NodeContext, NodeFailure
from app.engine.templates import seed_defaults
from app.meeting import store
from app.meeting.calls import CallOutputError
from app.meeting.protocols.base import (MeetingProtocol, StepResult, advance, done,
                                        register_protocol, reroute_or_escalate, suspend)
from app.meeting.runner import run_meeting

_counter = iter(range(1, 1000))


@register_protocol
class ToyProtocol(MeetingProtocol):
    name = "toy"
    steps = ("first", "ask", "last")

    async def on_enter(self, mtg):
        feedback = mtg.consume_feedback()
        if feedback:
            seen = list(mtg.state().get("feedbacks") or [])
            mtg.set_state(feedbacks=seen + [feedback])

    async def step_first(self, mtg):
        mtg.set_state(ran_first=True)
        return advance()

    async def step_ask(self, mtg):
        decision = mtg.decision()
        if decision is None:
            return suspend(await mtg.open_checkpoint(
                "consensus", title="请确认", description="玩具确认点", payload={"q": 1}))
        mtg.set_state(answer=decision["action"])
        mtg.ack_decision(decision)                    # 决定按它做完事了：标记为已处理
        return advance()

    async def step_last(self, mtg):
        return done("玩具协议完成", artifact={"kind": "toy"})


@register_protocol
class LoopProtocol(MeetingProtocol):
    """一进来就要把上游定点重开的协议：用来验证回流轮次用尽时转人工。"""

    name = "loop"
    steps = ("check",)

    async def step_check(self, mtg):
        result = await reroute_or_escalate(
            mtg, ["upstream"], "还有未解决的问题", title="回流轮次已用尽",
            description="请选择：接受现状，或写明意见再评一轮")
        if result is None:
            return done("人接受了现状")
        return result


@register_protocol
class BadRerouteProtocol(MeetingProtocol):
    """回流的契约写坏了：要求回流却没有指令。"""

    name = "bad_reroute"
    steps = ("go",)

    async def step_go(self, mtg):
        return StepResult(action="reroute")


@register_protocol
class OutputErrorProtocol(MeetingProtocol):
    name = "output_error"
    steps = ("go",)

    async def step_go(self, mtg):
        raise CallOutputError("缺少 must 字段")


def _run(protocol: str = "toy", **config) -> tuple[str, int]:
    init_db()
    seed_defaults()
    index = next(_counter)
    with session_scope() as s:
        workflow_id = s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").one().id
        run_id = f"runner-run-{index}"
        s.add(Run(id=run_id, workflow_id=workflow_id, status="running"))
        s.flush()
        node = Node(node_key=protocol, type="MEETING", name="玩具", run_id=run_id, status="pending",
                    config_json={"protocol": protocol, **config})
        s.add(node)
        s.flush()
        return run_id, node.id


def _ctx(run_id: str, node_id: int, protocol: str = "toy", **config) -> NodeContext:
    with session_scope() as s:
        node = s.get(Node, node_id)
        return NodeContext(run_id=run_id, node_id=node_id, node_key=protocol, type="MEETING",
                           name=node.name, phase="", config={**node.config_json, **config},
                           assignee={}, attempt_id=0, attempt_no=1)


def _decide(run_id: str, action: str, feedback: str = "") -> None:
    """模拟确认点已有决定（门上带着决定），节点回到待调度。"""
    with session_scope() as s:
        gate = s.query(Gate).filter(Gate.run_id == run_id, Gate.status == "open").one()
        gate.status = "approved" if action == "approve" else "rejected"
        gate.decision_json = {"action": action, "feedback": feedback, "data": {}}
        s.query(Node).filter(Node.run_id == run_id).one().status = "pending"


def test_suspends_then_resumes_from_decision() -> None:
    run_id, node_id = _run()
    first = asyncio.run(run_meeting(_ctx(run_id, node_id)))
    assert first["suspended"] is True
    meeting_id = first["meeting_id"]
    assert store.get_state(meeting_id)["ran_first"] is True
    assert store.get_meeting(meeting_id)["phase"] == "ask"
    assert store.get_meeting(meeting_id)["status"] == "suspended"
    gate = get_gate(store.get_state(meeting_id)["awaiting_gate_id"])
    assert gate["kind"] == "consensus" and gate["payload"] == {"title": "请确认",
                                                              "description": "玩具确认点", "q": 1}
    with session_scope() as s:
        assert s.get(Node, node_id).status == "waiting_human"

    _decide(run_id, "reject", feedback="继续")
    second = asyncio.run(run_meeting(_ctx(run_id, node_id)))
    assert second["done"] is True and second["summary"] == "玩具协议完成"
    assert second["artifact"] == {"kind": "toy"}
    assert store.get_state(meeting_id)["answer"] == "reject"
    assert store.get_meeting(meeting_id)["status"] == "completed"


def test_completed_meeting_without_feedback_is_restarted() -> None:
    run_id, node_id = _run()
    first = asyncio.run(run_meeting(_ctx(run_id, node_id)))
    _decide(run_id, "approve")
    asyncio.run(run_meeting(_ctx(run_id, node_id)))
    assert store.get_meeting(first["meeting_id"])["status"] == "completed"

    third = asyncio.run(run_meeting(_ctx(run_id, node_id)))      # 不带反馈：重开
    assert third["suspended"] is True
    assert third["meeting_id"] != first["meeting_id"]
    assert store.get_meeting(first["meeting_id"])["status"] == "abandoned"


def test_reopen_feedback_is_processed_once() -> None:
    """带着驳回意见续开的会议又在确认点挂起：恢复时同一条意见还留在节点配置里，不再处理第二遍。"""
    run_id, node_id = _run()
    first = asyncio.run(run_meeting(_ctx(run_id, node_id)))
    _decide(run_id, "approve")
    asyncio.run(run_meeting(_ctx(run_id, node_id)))
    store.update_meeting(first["meeting_id"], phase="ask")
    handled = dict(feedback="驳回：范围太大", feedback_kind="reject", feedback_seq=3)

    asyncio.run(run_meeting(_ctx(run_id, node_id, **handled)))       # 带着意见续开，又在确认点挂起
    _decide(run_id, "approve")
    again = asyncio.run(run_meeting(_ctx(run_id, node_id, **handled)))   # 恢复

    assert again["meeting_id"] == first["meeting_id"] and again["done"] is True
    assert store.get_state(first["meeting_id"])["feedbacks"] == ["驳回：范围太大"]


def test_resume_in_place_after_interruption() -> None:
    run_id, node_id = _run()
    first = asyncio.run(run_meeting(_ctx(run_id, node_id)))
    # 模拟中断后恢复：会议还在进行中，停在某一步
    store.update_meeting(first["meeting_id"], status="active")
    _decide(run_id, "approve")
    again = asyncio.run(run_meeting(_ctx(run_id, node_id)))
    assert again["meeting_id"] == first["meeting_id"]
    assert again["done"] is True
    assert store.get_meeting(first["meeting_id"])["status"] == "completed"


def test_reset_closed_checkpoint_restarts() -> None:
    """确认点被节点重置关掉、没有留下决定：归档旧会议，新建一场。"""
    run_id, node_id = _run()
    first = asyncio.run(run_meeting(_ctx(run_id, node_id)))
    with session_scope() as s:
        gate = s.query(Gate).filter(Gate.run_id == run_id).one()
        gate.status = "approved"                  # 被重置关闭：状态变了，但没有决定
        s.query(Node).filter(Node.run_id == run_id).one().status = "pending"

    again = asyncio.run(run_meeting(_ctx(run_id, node_id)))
    assert again["meeting_id"] != first["meeting_id"]
    assert store.get_meeting(first["meeting_id"])["status"] == "abandoned"


def test_auto_retry_feedback_resumes_in_place() -> None:
    """模型输出不合格后的自动重试也带反馈，但那不是复议：不触发入口处的反馈处理，原地续开。"""
    run_id, node_id = _run()
    first = asyncio.run(run_meeting(_ctx(run_id, node_id)))
    with session_scope() as s:       # 模拟：确认点被重置关闭（没有留下决定），节点因失败重试
        s.query(Gate).filter(Gate.run_id == run_id).update({Gate.status: "approved"})
    store.update_meeting(first["meeting_id"], status="active")

    again = asyncio.run(run_meeting(_ctx(run_id, node_id, feedback="模型输出不符合要求",
                                         feedback_kind="auto_retry", feedback_seq=2)))
    assert again["meeting_id"] == first["meeting_id"]
    assert again["suspended"] is True                    # 停在同一步，重新问
    assert "feedbacks" not in store.get_state(first["meeting_id"])


def test_escalation_checkpoint_when_rounds_are_used_up() -> None:
    run_id, node_id = _run("loop", round=2, max_rounds=2)
    first = asyncio.run(run_meeting(_ctx(run_id, node_id, "loop")))
    assert first["suspended"] is True
    assert first["checkpoint"]["kind"] == "escalation"
    assert first["checkpoint"]["payload"]["targets"] == ["upstream"]

    _decide(run_id, "approve")
    second = asyncio.run(run_meeting(_ctx(run_id, node_id, "loop")))
    assert second["done"] is True and second["summary"] == "人接受了现状"


def test_escalation_reject_grants_one_more_round() -> None:
    run_id, node_id = _run("loop", round=2, max_rounds=2)
    asyncio.run(run_meeting(_ctx(run_id, node_id, "loop")))
    _decide(run_id, "reject", feedback="再查一次成本")

    again = asyncio.run(run_meeting(_ctx(run_id, node_id, "loop")))
    assert again["reroute"] == {
        "targets": ["upstream"],
        "feedback": "还有未解决的问题\n\n用户在确认点上的意见：再查一次成本",
        "self_feedback": None, "extra_round": True}
    assert store.get_meeting(again["meeting_id"])["status"] == "waiting_rework"


def test_escalation_decision_survives_a_crash_after_the_ack(monkeypatch) -> None:
    """认领与处置记录同一事务：ack 后被打断，重入按处置记录重放，不重开升级门、不换会议。"""
    run_id, node_id = _run("loop", round=2, max_rounds=2)
    asyncio.run(run_meeting(_ctx(run_id, node_id, "loop")))      # 轮次用尽：挂起升级确认点
    _decide(run_id, "reject", feedback="再查一次成本")
    meeting_id = store.latest_meeting_for_node(node_id)["id"]

    armed = {"on": True}
    original_update = store.update_meeting

    def flaky_update(meeting_id_, **fields):
        if armed["on"] and fields.get("status") == "waiting_rework":
            armed["on"] = False                        # 处置已认领、回流还没交给编排器：进程在这里被杀
            raise asyncio.CancelledError()
        return original_update(meeting_id_, **fields)

    monkeypatch.setattr(store, "update_meeting", flaky_update)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_meeting(_ctx(run_id, node_id, "loop")))
    monkeypatch.setattr(store, "update_meeting", original_update)

    again = asyncio.run(run_meeting(_ctx(run_id, node_id, "loop")))
    assert again["reroute"]["extra_round"] is True
    assert "再查一次成本" in again["reroute"]["feedback"]
    assert again["meeting_id"] == meeting_id                     # 还是同一场会议，没有被换新
    with session_scope() as s:
        assert s.query(Gate).filter(Gate.run_id == run_id).count() == 1   # 没有重开第二扇门


def test_reroute_before_rounds_run_out() -> None:
    run_id, node_id = _run("loop", round=1, max_rounds=3)
    result = asyncio.run(run_meeting(_ctx(run_id, node_id, "loop")))
    assert result["reroute"]["extra_round"] is False
    assert "用户在确认点上的意见" not in result["reroute"]["feedback"]


@register_protocol
class LeakyProtocol(MeetingProtocol):
    """最后一步返回了 advance：协议写错了。"""

    name = "leaky"
    steps = ("only",)

    async def step_only(self, mtg):
        return advance()


def test_advancing_past_the_last_step_is_a_clear_fatal_error() -> None:
    run_id, node_id = _run("leaky")
    # 协议自己的错，重试也不会好：报清楚的错，并按不可重试处理
    with pytest.raises(NodeFailure, match="最后一步") as caught:
        asyncio.run(run_meeting(_ctx(run_id, node_id, "leaky")))
    assert caught.value.fatal is True


def test_suspended_meeting_keeps_waiting_when_reentered_without_a_decision() -> None:
    # 确认点还开着、还没有决定时节点又被调度了一次（例如有人重试了正在等待的节点）：
    # 什么都不做，继续等这个决定。不能把会议归档重开——那会丢掉整场会议
    run_id, node_id = _run()
    first = asyncio.run(run_meeting(_ctx(run_id, node_id)))

    again = asyncio.run(run_meeting(_ctx(run_id, node_id)))

    assert again["suspended"] is True and again["meeting_id"] == first["meeting_id"]
    assert again["checkpoint"]["id"] == first["checkpoint"]["id"]
    assert store.get_meeting(first["meeting_id"])["status"] == "suspended"
    with session_scope() as s:
        assert s.query(Gate).filter(Gate.run_id == run_id).count() == 1
        assert s.get(Node, node_id).status == "waiting_human"

    _decide(run_id, "approve")                                   # 之后照常：有了决定就续开并完成
    final = asyncio.run(run_meeting(_ctx(run_id, node_id)))
    assert final["done"] is True and final["meeting_id"] == first["meeting_id"]


def test_orchestrator_parks_the_run_at_a_checkpoint_and_resumes_after_the_decision() -> None:
    # 走真实的编排器：会议挂起后运行停在"等待人工"，节点不会被标成完成；
    # 在等待期间再转一圈不会重复开门，也不会重开会议；有了决定再续开并跑完
    run_id, node_id = _run()
    asyncio.run(orchestrator._loop(run_id))
    with session_scope() as s:
        assert s.get(Node, node_id).status == "waiting_human"
        assert s.get(Run, run_id).status == "waiting_human"
        assert s.query(Gate).filter(Gate.run_id == run_id, Gate.status == "open").count() == 1

    asyncio.run(orchestrator._loop(run_id))                      # 还在等：什么都不该发生
    with session_scope() as s:
        assert s.get(Run, run_id).status == "waiting_human"
        assert s.query(Gate).filter(Gate.run_id == run_id).count() == 1
        assert s.query(Meeting).filter(Meeting.run_id == run_id).count() == 1

    _decide(run_id, "approve")
    asyncio.run(orchestrator._loop(run_id))                      # 续开并跑完
    with session_scope() as s:
        assert s.get(Node, node_id).status == "done"
        assert s.get(Run, run_id).status == "finished"
        events = [e.type for e in s.query(Event).filter(Event.run_id == run_id).all()]
    assert events.index("meeting_started") < events.index("meeting_suspended") < events.index(
        "meeting_resumed") < events.index("meeting_finished") < events.index("run_finished")


def test_redundant_scheduling_does_not_repeat_meeting_suspended() -> None:
    """挂起中的节点被多余调度：meeting_suspended 只发一次，不刷事件流。"""
    run_id, node_id = _run()
    node = orchestrator._load(run_id)[1][0]
    asyncio.run(orchestrator._run_attempt(run_id, node))      # 第一次挂起：发事件
    with session_scope() as s:
        s.get(Node, node_id).status = "pending"               # 多余的调度把节点摆回待执行
        s.get(Run, run_id).status = "running"
    node = orchestrator._load(run_id)[1][0]
    asyncio.run(orchestrator._run_attempt(run_id, node))      # 多余调度：不再重复发
    asyncio.run(orchestrator._run_attempt(run_id, node))

    with session_scope() as s:
        events = [e.type for e in s.query(Event).filter(Event.run_id == run_id).all()]
    assert events.count("meeting_suspended") == 1


def test_entering_waiting_clears_stale_auto_retry_feedback() -> None:
    """进入等待分支时清掉滞留的 auto_retry 反馈：它不会被消费，留在节点配置里只会误导。
    序号保留：真正的复议反馈按序号认。"""
    run_id, node_id = _run()
    first = asyncio.run(run_meeting(_ctx(run_id, node_id)))
    assert first["suspended"] is True
    with session_scope() as s:
        node = s.get(Node, node_id)
        node.config_json = {**(node.config_json or {}),
                            "feedback": "模型输出不符合要求", "feedback_kind": "auto_retry",
                            "feedback_seq": 1}
    asyncio.run(run_meeting(_ctx(run_id, node_id)))     # 多余调度：确认点还开着，进等待分支

    with session_scope() as s:
        config = dict(s.get(Node, node_id).config_json or {})
    assert "feedback" not in config and "feedback_kind" not in config
    assert config.get("feedback_seq") == 1


def test_reroute_self_note_reaches_the_protocol() -> None:
    """回流写给下一轮的自我提示也是一条待处理反馈，协议在入口取得到。"""
    run_id, node_id = _run()
    first = asyncio.run(run_meeting(_ctx(run_id, node_id)))      # 先挂起一次
    store.update_meeting(first["meeting_id"], status="waiting_rework")
    asyncio.run(run_meeting(_ctx(run_id, node_id, feedback="下一轮复核 D3 的处理结果",
                                 feedback_kind="reroute_self", feedback_seq=2)))
    assert store.get_state(first["meeting_id"])["feedbacks"] == ["下一轮复核 D3 的处理结果"]


def test_protocol_reroute_without_instruction_is_fatal() -> None:
    run_id, node_id = _run("bad_reroute")
    with pytest.raises(NodeFailure) as caught:
        asyncio.run(run_meeting(_ctx(run_id, node_id, "bad_reroute")))
    assert caught.value.fatal is True


def test_call_output_error_becomes_a_retryable_node_failure() -> None:
    run_id, node_id = _run("output_error")
    with pytest.raises(NodeFailure) as caught:
        asyncio.run(run_meeting(_ctx(run_id, node_id, "output_error")))
    assert caught.value.fatal is False
    assert "模型输出不符合要求" in str(caught.value)
