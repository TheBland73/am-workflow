"""评审协议：独立评审、汇总分级、作者回应、主席定夺、程序裁决、修订与验证、回流与增量复审。"""
from __future__ import annotations

import asyncio
import json
import re
from collections import Counter

import pytest
from fastapi.testclient import TestClient

from app.artifacts.store import parse_front_matter, read_artifact, write_artifact
from app.db.base import init_db, session_scope
from app.db.models import Gate, Node, Run, Workflow
from app.engine.events import load_events
from app.engine.feedback import with_feedback
from app.engine.orchestrator import orchestrator
from app.engine.params import apply_run_config
from app.engine.runners import NodeContext, NodeFailure
from app.engine.templates import instantiate_nodes, seed_defaults, workflow_definition
from app.main import app
from app.meeting import store
from app.meeting.protocols import review as review_module
from app.meeting.session import MeetingSession
from tests.meeting_fakes import drive, install

IDEA = "给小团队做一个反馈收集工具"
_counter = iter(range(1, 1000))

PROPOSAL = (
    "# 建设方案\n\n## 目标与成功标准\n\n两周内让小团队用上反馈收集：能收集、能整理、能导出。\n\n"
    "## 范围\n\n必做：收集、整理、导出周报。不做：实时协作、细粒度权限。\n\n"
    "## 总体方案\n\nD1：首版做网页版，部署最简单。D2：首版需要登录，多人共用时区分提交人。\n\n"
    "## 关键流程\n\n提交反馈 → 整理 → 导出。\n\n## 里程碑与验收\n\n第一周：收集与导出可用。\n\n"
    "## 风险与缓解\n\n范围蔓延：首版功能冻结。\n")
REVISED_PROPOSAL = "## 修改说明\n\n- 按意见补充了范围说明。\n\n" + PROPOSAL

WORDING = {"severity": "major", "category": "wording", "location": "## 范围",
           "problem": "范围写得不够清楚", "suggestion": "把必做与不做分别列清楚"}
WORDING_2 = {"severity": "major", "category": "wording", "location": "## 里程碑与验收",
             "problem": "验收标准不可核对", "suggestion": "写出可验证的验收条件"}
DECISION = {"severity": "blocker", "category": "decision", "location": "D2",
            "problem": "方案结论与账本不符", "suggestion": "按账本的结论改写"}


def _ids(text: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"\bI\d+\b", text)))


def _respond(text: str, *, accepted: bool = True, ask: list[str] | None = None) -> str:
    return json.dumps({"responses": [
        {"id": issue_id, "accepted": accepted,
         "reason": "" if accepted else "现有方案已经覆盖了这个风险",
         "ask_requester": ask or []} for issue_id in _ids(text)]}, ensure_ascii=False)


def _rule(text: str, *, keep: bool) -> str:
    return json.dumps({"rulings": [
        {"id": issue_id, "keep": keep, "category": "wording", "severity": "major",
         "note": "意见成立" if keep else "意见不成立",
         "dissent": "有评审者坚持这条意见" if keep else ""}
        for issue_id in _ids(text)]}, ensure_ascii=False)


def _verify(text: str, *, fixed: bool) -> str:
    return json.dumps({"verified": [
        {"id": issue_id, "fixed": fixed, "note": "已核对"} for issue_id in _ids(text)]},
        ensure_ascii=False)


def _replies(**overrides) -> dict:
    base = {
        "review_brief": '{"checklist": ["范围是否完整", "验收是否可核对"]}',
        "review_points": '{"focus": ["决策点与方案是否一致"]}',
        "review": '{"strengths": ["方案覆盖了主要决策点"], "issues": []}',
        "review_delta": '{"strengths": [], "issues": [], "resolved": []}',
        "review_merge": '{"issues": []}',
        "author_response": '{"responses": []}',
        "review_defend": "这条意见成立：不改会阻塞评审。",
        "review_rule": '{"rulings": []}',
        "author_revise": REVISED_PROPOSAL,
        "revision_verify": '{"verified": []}',
        "review_intake": json.dumps({"issues": [
            {"severity": "major", "category": "wording", "location": "## 范围",
             "problem": "用户认为范围不清楚", "suggestion": "写清楚"}]}, ensure_ascii=False),
        "answer": '{"answers": [{"text": "材料中没有说明", "source": ""}]}',
    }
    base.update(overrides)
    return base


def _review_run(config: dict | None = None, *, proxy: bool = False,
                proposal: bool = True) -> tuple[str, int]:
    init_db()
    seed_defaults()
    run_id = f"review-run-{next(_counter)}"
    with session_scope() as s:
        workflow_id = s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").one().id
        s.add(Run(id=run_id, workflow_id=workflow_id, status="running", idea_text=IDEA,
                  config_json=config or {}))
        s.flush()
    definition = workflow_definition("sdlc_v1")
    instantiate_nodes(run_id, definition)
    apply_run_config(run_id, definition, config or {})
    write_artifact(run_id, "idea", "# 想法\n\n" + IDEA, created_by="user", inputs=[],
                   status="approved")
    if proposal:
        write_artifact(run_id, "proposal", PROPOSAL, created_by="facilitator",
                       inputs=["idea", "discussion"], status="draft")
    with session_scope() as s:
        discuss_id = s.query(Node).filter(Node.run_id == run_id,
                                          Node.node_key == "discuss").one().id
        review_id = s.query(Node).filter(Node.run_id == run_id,
                                         Node.node_key == "review").one().id
    meeting_id = store.create_meeting(run_id=run_id, node_id=discuss_id, node_key="discuss",
                                      protocol="discussion", charter={"sections": []}, config={})
    store.update_meeting(meeting_id, status="completed", phase="done", round=1)
    store.add_participant(meeting_id, role_key="facilitator", display_name="主持人",
                          mode="facilitator", system_prompt="你是主持人")
    store.add_participant(meeting_id, role_key="architect", display_name="架构师",
                          mode="participant", system_prompt="你是架构师")
    store.add_participant(meeting_id, role_key="ux", display_name="用户体验",
                          mode="participant", system_prompt="你是用户体验")
    if proxy:
        store.add_participant(meeting_id, role_key="requester_proxy", display_name="需求方代理",
                              mode="proxy", system_prompt="你是需求方代理，只回答事实问题")
    store.upsert_decision_point(meeting_id, "D1", title="首版的范围与入口",
                                question="首版做哪些功能", options_json=["网页版"],
                                status="agreed", conclusion="网页版", closed_round=1,
                                raised_by="架构师")
    store.upsert_decision_point(meeting_id, "D2", title="多人共用时如何区分反馈",
                                question="首版是否需要登录", options_json=["需要", "不需要"],
                                status="agreed", conclusion="不需要", closed_round=1,
                                raised_by="用户体验")
    return run_id, review_id


def _approve(checkpoint: dict) -> tuple:
    return "approve", {}, ""


def _never(checkpoint: dict) -> tuple:
    raise AssertionError(f"不该出现人工确认点：{checkpoint['title']}")


def _meeting(node_id: int) -> dict:
    return store.latest_meeting_for_node(node_id)


def _issues(node_id: int) -> list[dict]:
    return list(_meeting(node_id)["state"].get("issues") or [])


def _merge(*rows: dict) -> str:
    return json.dumps({"issues": [{**row, "raised_by": ["架构师"], "disputed": False}
                                  for row in rows]}, ensure_ascii=False)


def test_review_without_issues_accepts(monkeypatch) -> None:
    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    run_id, node_id = _review_run()

    result = drive(run_id, node_id, _never, idea=IDEA)

    assert result["done"] is True
    assert result["verdict"] == "accept" and result["precheck_ok"] is True
    # 前置检查的结论要写进会议状态：裁决模型（F1）从这里读
    state = _meeting(node_id)["state"]
    assert state["precheck_ok"] is True
    # 报告的摘要与阻塞数也记进会议状态：/runs/{id}/review 与指标按报告的会议取
    assert state["summary"] == "评审会议完成：结论 接受，0 条意见，0 条达到阻塞线"
    assert state["blocking"] == 0
    assert any(section["title"] == "被评审的建设方案"
               for section in _meeting(node_id)["charter"].get("sections") or [])
    kinds = Counter(call["kind"] for call in seen)
    assert "author_response" not in kinds and "author_revise" not in kinds
    report = parse_front_matter(read_artifact(run_id, "review"))
    assert report[0]["review_type"] == "proposal"      # 与需求确认报告共用 review 工件，靠它区分
    assert "结论：接受。" in report[1] and "方案覆盖了主要决策点" in report[1]


def test_minutes_endpoint_returns_the_report_this_meeting_wrote(monkeypatch) -> None:
    """纪要接口只认会议自己写的那一版报告：之后写进同一个工件类型的报告不会顶掉它。"""
    install(monkeypatch, _replies())
    run_id, node_id = _review_run()

    result = drive(run_id, node_id, _never, idea=IDEA)

    assert result["done"] is True
    # 之后的需求确认会议写进 review 工件的报告（第 2 版）
    write_artifact(run_id, "review", "# 需求确认报告", created_by="chair",
                   inputs=["requirement"], status="reviewed",
                   extra_meta={"review_type": "requirement"})
    meeting_id = _meeting(node_id)["id"]
    with TestClient(app) as client:
        response = client.get(f"/api/runs/{run_id}/meetings/{meeting_id}/minutes")

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["kind"] == "review"
    assert data["version"] == result["report"]["version"] == 1
    assert "结论：接受。" in data["body"] and "需求确认报告" not in data["body"]


ISSUE = {"id": "I1", "standing": True, "accepted": True, "category": "wording",
         "severity": "major", "problem": "范围写得不够清楚", "suggestion": "分别列出必做与不做",
         "disposition": "", "raised_by": ["架构师"], "disputed": False, "response": ""}


def _review_session(run_id: str, node_id: int, *, phase: str,
                    state: dict | None = None) -> MeetingSession:
    """直接建一场评审会议与它的会话，用来单独驱动某个步骤（不经 run_meeting）。"""
    meeting_id = store.create_meeting(run_id=run_id, node_id=node_id, node_key="review",
                                      protocol="review", charter={}, config={})
    store.update_meeting(meeting_id, phase=phase, round=1)
    store.set_state(meeting_id, dict(state or {}))
    store.add_participant(meeting_id, role_key="review_chair", display_name="评审主席",
                          mode="chair", system_prompt="你是评审主席")
    store.add_participant(meeting_id, role_key="author", display_name="执笔者",
                          mode="author", system_prompt="你是执笔者")
    store.add_participant(meeting_id, role_key="architect", display_name="架构师",
                          mode="reviewer", system_prompt="你是评审者")
    with session_scope() as s:
        node = s.get(Node, node_id)
        ctx = NodeContext(run_id=run_id, node_id=node_id, node_key="review", type="MEETING",
                          name="评审会议", phase=phase, config=dict(node.config_json or {}),
                          assignee={}, attempt_id=0, attempt_no=1)
    return MeetingSession(meeting_id=meeting_id, ctx=ctx)


def _bare_review_meeting(run_id: str, node_id: int, *, phase: str = "intake",
                         state: dict | None = None, round_no: int = 1) -> MeetingSession:
    """手工建一场没有参会者的评审会议，用来直接驱动单个步骤与防御分支。"""
    meeting_id = store.create_meeting(run_id=run_id, node_id=node_id, node_key="review",
                                      protocol="review", charter={}, config={})
    store.update_meeting(meeting_id, phase=phase, round=round_no)
    store.set_state(meeting_id, dict(state or {}))
    with session_scope() as s:
        node = s.get(Node, node_id)
        ctx = NodeContext(run_id=run_id, node_id=node_id, node_key="review", type="MEETING",
                          name="评审会议", phase=phase, config=dict(node.config_json or {}),
                          assignee={}, attempt_id=0, attempt_no=1)
    return MeetingSession(meeting_id=meeting_id, ctx=ctx)


def test_revising_the_same_issues_twice_reuses_the_recorded_turn(monkeypatch) -> None:
    """崩溃重试会重跑修订步骤：已落盘的发言要复用，不能再写出一版方案。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    run_id, node_id = _review_run()
    mtg = _review_session(run_id, node_id, phase="revise", state={"issues": [dict(ISSUE)]})
    protocol = review_module.ReviewProtocol()

    asyncio.run(protocol._revise(mtg, [dict(ISSUE)]))
    asyncio.run(protocol._revise(mtg, [dict(ISSUE)]))

    assert Counter(call["kind"] for call in seen)["author_revise"] == 1
    assert parse_front_matter(read_artifact(run_id, "proposal"))[0]["version"] == 2


def test_verifying_the_same_issues_twice_reuses_the_recorded_turn(monkeypatch) -> None:
    seen: list[dict] = []
    install(monkeypatch, _replies(
        revision_verify=lambda text: _verify(text, fixed=True)), seen)
    run_id, node_id = _review_run()
    issue = {**ISSUE, "disposition": "已修订，待验证"}
    mtg = _review_session(run_id, node_id, phase="verify", state={"issues": [issue]})
    protocol = review_module.ReviewProtocol()

    asyncio.run(protocol._verify(mtg, [issue]))
    asyncio.run(protocol._verify(mtg, [issue]))

    assert Counter(call["kind"] for call in seen)["revision_verify"] == 1
    assert [row["disposition"] for row in store.get_state(mtg.meeting_id)["issues"]] == [
        "已修订并验证"]


def test_chair_rulings_are_reused_after_a_crash(monkeypatch) -> None:
    """发言已经落盘、意见还没更新时中断：重跑讨论步骤直接用已落盘的定夺。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(
        review_rule=lambda text: _rule(text, keep=True),
        review_defend="这条意见成立。"), seen)
    run_id, node_id = _review_run()
    issue = {**ISSUE, "accepted": False, "disputed": True}
    mtg = _review_session(run_id, node_id, phase="discuss", state={"issues": [issue]})
    protocol = review_module.ReviewProtocol()
    original = review_module._update_issue
    armed = {"on": True}

    def flaky(mtg_, issue_id: str, **fields):
        if armed["on"] and str(fields.get("disposition") or "").startswith("主席定夺"):
            armed["on"] = False
            raise RuntimeError("进程在更新意见前中断")
        return original(mtg_, issue_id, **fields)

    monkeypatch.setattr(review_module, "_update_issue", flaky)
    with pytest.raises(RuntimeError):
        asyncio.run(protocol.step_discuss(mtg))
    asyncio.run(protocol.step_discuss(mtg))

    assert Counter(call["kind"] for call in seen)["review_rule"] == 1
    assert store.get_state(mtg.meeting_id)["issues"][0]["disposition"] == "主席定夺：成立"


def test_step_speech_is_salvaged_when_the_reuse_key_write_is_lost(monkeypatch) -> None:
    """复用键在发言之前先认领：发言落盘、编号没写上时中断，重入按发言行找回，不重复调用。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(
        review_rule=lambda text: _rule(text, keep=True),
        review_defend="这条意见成立。"), seen)
    run_id, node_id = _review_run()
    issue = {**ISSUE, "accepted": False, "disputed": True}
    mtg = _review_session(run_id, node_id, phase="discuss", state={"issues": [issue]})
    protocol = review_module.ReviewProtocol()
    armed = {"on": True}
    from app.meeting import calls as calls_module

    original_emit = calls_module.emit

    async def flaky_emit(run_id_, type_, node_id_, payload):
        if armed["on"] and type_ == "meeting_turn_finished" \
                and payload.get("kind") == "review_rule":
            armed["on"] = False                    # 定夺发言刚落盘、复用键还没写上：进程在这里被杀
            raise asyncio.CancelledError()
        return await original_emit(run_id_, type_, node_id_, payload)

    monkeypatch.setattr(calls_module, "emit", flaky_emit)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(protocol.step_discuss(mtg))
    monkeypatch.setattr(calls_module, "emit", original_emit)

    asyncio.run(protocol.step_discuss(mtg))

    assert Counter(call["kind"] for call in seen)["review_rule"] == 1
    assert store.get_state(mtg.meeting_id)["issues"][0]["disposition"] == "主席定夺：成立"


def test_defends_are_not_repeated_after_a_crash(monkeypatch) -> None:
    """两条意见各由一名评审者辩护：第一条落盘后被中断，重入不再为它重复调用模型。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(
        review_rule=lambda text: _rule(text, keep=True),
        review_defend="这条意见成立：不改会阻塞评审。"), seen)
    run_id, node_id = _review_run()
    issues = [
        {**ISSUE, "id": "I1", "accepted": False, "disputed": True},
        {**ISSUE, "id": "I2", "accepted": False, "disputed": True,
         "raised_by": ["测试与质量"], "problem": "验收标准不可核对"},
    ]
    mtg = _review_session(run_id, node_id, phase="discuss", state={"issues": issues})
    store.add_participant(mtg.meeting_id, role_key="qa", display_name="测试与质量",
                          mode="reviewer", system_prompt="你是评审者")
    protocol = review_module.ReviewProtocol()
    original = review_module.speak
    armed = {"on": True}

    async def flaky(mtg_, participant, **kwargs):
        texts = "".join(getattr(segment, "text", "") for segment in kwargs.get("segments") or [])
        if armed["on"] and kwargs.get("kind") == "review_defend" and "I2" in texts:
            armed["on"] = False                        # 第一条发言与标记都已落盘：进程在这里被杀
            raise asyncio.CancelledError()
        return await original(mtg_, participant, **kwargs)

    monkeypatch.setattr(review_module, "speak", flaky)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(protocol.step_discuss(mtg))
    asyncio.run(protocol.step_discuss(mtg))

    kinds = Counter(call["kind"] for call in seen)
    assert kinds["review_defend"] == 2                 # 每条意见恰好一次，重入从第二条继续
    assert kinds["review_rule"] == 1
    state = store.get_state(mtg.meeting_id)
    assert [row["disposition"] for row in state["issues"]] == ["主席定夺：成立"] * 2
    assert sorted(state["discussed"]) == ["I1", "I2"]


def test_wording_issues_are_revised_and_verified(monkeypatch) -> None:
    seen: list[dict] = []
    install(monkeypatch, _replies(
        review=json.dumps({"strengths": ["结构清楚"], "issues": [WORDING, WORDING_2]}),
        review_merge=_merge(WORDING, WORDING_2),
        author_response=_respond,
        revision_verify=lambda text: _verify(text, fixed=True)), seen)
    run_id, node_id = _review_run()

    result = drive(run_id, node_id, _approve, idea=IDEA)

    assert result["done"] is True and result["verdict"] == "conditional"
    meta, body = parse_front_matter(read_artifact(run_id, "proposal"))
    assert meta["version"] == 2 and "## 修改说明" in body
    assert [issue["disposition"] for issue in _issues(node_id)] == ["已修订并验证"] * 2
    kinds = Counter(call["kind"] for call in seen)
    assert kinds["author_revise"] == 1 and kinds["revision_verify"] == 1


def test_blocking_decision_issue_reroutes_and_then_incremental_review(monkeypatch) -> None:
    seen: list[dict] = []
    install(monkeypatch, _replies(
        review=json.dumps({"strengths": [], "issues": [DECISION]}),
        review_merge=_merge(DECISION), author_response=_respond), seen)
    run_id, node_id = _review_run()

    first = drive(run_id, node_id, _never, idea=IDEA)
    assert first["reroute"]["targets"] == ["discuss"]
    assert "按账本的结论改写" in first["reroute"]["feedback"]
    meeting_id = _meeting(node_id)["id"]
    assert store.get_meeting(meeting_id)["status"] == "waiting_rework"

    # 编排器把讨论会议定点重开，评审节点轮次加一
    with session_scope() as s:
        config = dict(s.get(Node, node_id).config_json or {})
    asyncio.run(orchestrator._handle_reroute(
        run_id, {"id": node_id, "node_key": "review", "config": config}, first["reroute"]))
    # 讨论会议复议后：账本的结论改了，方案出了第 2 版
    discussion = store.latest_meeting(run_id, "discussion")
    points = store.list_decision_points(discussion["id"])
    d2 = next(point for point in points if point["index"] == "D2")
    store.update_decision_point(d2["id"], conclusion="需要")
    write_artifact(run_id, "proposal", REVISED_PROPOSAL, created_by="author",
                   inputs=["discussion"], status="draft")

    install(monkeypatch, _replies(
        review=json.dumps({"strengths": [], "issues": [DECISION]}),
        review_delta=json.dumps({"strengths": [], "issues": [],
                                 "resolved": [{"id": "I1", "handled": True,
                                               "note": "账本已改"}]})), seen)

    second = drive(run_id, node_id, _never, idea=IDEA)

    assert second["done"] is True and second["verdict"] == "accept"
    assert _meeting(node_id)["id"] == meeting_id
    kinds = Counter(call["kind"] for call in seen)
    assert kinds["review_delta"] == 2                   # 两名评审者各做一次增量复审
    delta_prompts = [call["text"] for call in seen if call["kind"] == "review_delta"]
    assert all("上一轮的决策级意见与处理情况" in text for text in delta_prompts)
    assert all("改动的决策点" in text for text in delta_prompts)


def test_reroute_is_reissued_after_an_interruption_before_the_orchestrator_accepts(
        monkeypatch) -> None:
    """协议写完状态、返回回流指令之前被中断：重入要重新发起回流，而不是加一轮去等返工。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(
        review=json.dumps({"strengths": [], "issues": [DECISION]}),
        review_merge=_merge(DECISION), author_response=_respond), seen)
    run_id, node_id = _review_run()

    original = review_module.ReviewProtocol.step_verdict
    armed = {"on": True}

    async def flaky(self, mtg):
        result = await original(self, mtg)
        if armed["on"] and result.action == "reroute":
            armed["on"] = False
            raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(review_module.ReviewProtocol, "step_verdict", flaky)
    with pytest.raises(asyncio.CancelledError):
        drive(run_id, node_id, _never, idea=IDEA)

    meeting_id = _meeting(node_id)["id"]
    round_before = store.get_meeting(meeting_id)["round"]

    second = drive(run_id, node_id, _never, idea=IDEA)

    assert second["reroute"]["targets"] == ["discuss"]
    assert _meeting(node_id)["id"] == meeting_id          # 同一场会议续开，没有归档重开
    assert store.get_meeting(meeting_id)["round"] == round_before  # 中断没有多加轮次
    kinds = Counter(call["kind"] for call in seen)
    # 裁决是程序判定的：重入只重发指令，不重跑评审、汇总与回应
    assert kinds["review"] == 2 and kinds["review_merge"] == 1 and kinds["author_response"] == 1
    # 重算裁决不重复发事件；重发的是同一条回流意见
    verdicts = [e["payload"]["round"] for e in load_events(run_id)
                if e["type"] == "review_verdict"]
    assert verdicts == [1]
    assert "按账本的结论改写" in second["reroute"]["feedback"]


def test_reopen_branch_clears_stale_reroute_marks_and_last_report_fields() -> None:
    """用户带反馈重开：上一轮滞留的回流标记与旧报告的摘要、阻塞数一并清掉。"""
    run_id, node_id = _review_run()
    mtg = _review_session(run_id, node_id, phase="verdict", state={
        "pending_feedback": "范围还是不清楚",
        "rework_pending": True,
        "verdict": "conditional", "summary": "评审会议完成：结论 接受", "blocking": 2,
        "minutes_version": 1})

    asyncio.run(review_module.ReviewProtocol().on_enter(mtg))

    state = store.get_state(mtg.meeting_id)
    assert state["intake_feedback"] == "范围还是不清楚"     # 反馈已移交给受理步
    assert state["rework_pending"] is False
    assert state["summary"] is None and state["blocking"] is None


def test_feedback_speech_is_not_repeated_after_a_crash(monkeypatch) -> None:
    """入口反馈分支的发言与状态移交不在同一事务：中断重入按轮次与内容去重，不重复发言。"""
    run_id, node_id = _review_run()
    mtg = _review_session(run_id, node_id, phase="verdict", state={
        "reopen_feedback": "把验收标准写清楚", "feedback_kind": "reroute"})
    armed = {"on": True}
    original_update = store.update_meeting

    def flaky_update(meeting_id, **fields):
        if armed["on"] and fields.get("phase") == "intake":
            armed["on"] = False                        # 反馈发言刚落盘、状态还没移交：进程在这里被杀
            raise asyncio.CancelledError()
        return original_update(meeting_id, **fields)

    monkeypatch.setattr(store, "update_meeting", flaky_update)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(review_module.ReviewProtocol().on_enter(mtg))
    monkeypatch.setattr(store, "update_meeting", original_update)

    asyncio.run(review_module.ReviewProtocol().on_enter(mtg))
    turns = store.round_turns(mtg.meeting_id, round=2, kinds=["user_feedback"])
    assert [t["content"] for t in turns] == ["把验收标准写清楚"]
    state = store.get_state(mtg.meeting_id)
    assert state["intake_feedback"] == "把验收标准写清楚" and state["reopen_feedback"] == ""


def _land_turn(mtg, *, phase: str, kind: str, speaker: dict, content: str,
               structured: dict) -> None:
    """预先落一条完整发言：让步骤的 find_turn 复用它，不调模型。"""
    turn = store.add_turn(mtg.meeting_id, round=1, phase=phase, speaker_id=speaker["id"],
                          speaker_name=speaker["display_name"], kind=kind)
    store.finish_turn(turn, content=content, structured=structured)


def test_intake_accounting_is_idempotent_by_signature(monkeypatch) -> None:
    """受理入账按（轮次、出处、位置、问题）签名去重：重跑入账不会以新编号重复入账。"""
    install(monkeypatch, _replies())
    run_id, node_id = _review_run()
    seeded = {**ISSUE, "location": WORDING["location"], "origin": "user", "round": 1}
    mtg = _review_session(run_id, node_id, phase="intake", state={
        "intake_feedback": "把验收标准写清楚", "issues": [seeded]})
    _land_turn(mtg, phase="intake", kind="review_intake", speaker=mtg.participants("chair")[0],
               content="整理意见", structured={"issues": [dict(WORDING)]})
    protocol = review_module.ReviewProtocol()

    asyncio.run(protocol.step_intake(mtg))

    issues = store.get_state(mtg.meeting_id)["issues"]
    assert [issue["id"] for issue in issues] == ["I1"]


def test_merge_accounting_is_idempotent_by_signature(monkeypatch) -> None:
    """汇总入账按（轮次、出处、位置、问题）签名去重：重跑入账不会以新编号重复入账。"""
    install(monkeypatch, _replies())
    run_id, node_id = _review_run()
    seeded = {**ISSUE, "location": WORDING["location"], "origin": "reviewer", "round": 1,
              "raised_by": ["架构师"]}
    mtg = _review_session(run_id, node_id, phase="merge", state={
        "review_batch": [dict(WORDING, raised_by=["架构师"], origin="reviewer")],
        "issues": [seeded]})
    _land_turn(mtg, phase="merge", kind="review_merge", speaker=mtg.participants("chair")[0],
               content="汇总意见",
               structured={"issues": [dict(WORDING, raised_by=["架构师"])]})
    protocol = review_module.ReviewProtocol()

    asyncio.run(protocol.step_merge(mtg))

    issues = store.get_state(mtg.meeting_id)["issues"]
    assert [issue["id"] for issue in issues] == ["I1"]


def test_reroute_self_feedback_is_recorded_as_review_voice() -> None:
    """回流写给下一轮的自我提示也是"评审意见"的发言，不标成用户。"""
    run_id, node_id = _review_run()
    mtg = _review_session(run_id, node_id, phase="verdict", state={
        "reopen_feedback": "先补齐验证的依据", "feedback_kind": "reroute_self"})

    asyncio.run(review_module.ReviewProtocol().on_enter(mtg))

    turns = store.round_turns(mtg.meeting_id, round=2, kinds=["user_feedback"])
    assert [t["speaker_name"] for t in turns] == ["评审意见"]


def test_defend_speech_is_not_repeated_when_the_marker_write_is_lost(monkeypatch) -> None:
    """辩护发言落盘后被打断（完成标记来不及写）：重入按发言行找回，不重复辩护。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(
        review_rule=lambda text: _rule(text, keep=True),
        review_defend="这条意见成立。"), seen)
    run_id, node_id = _review_run()
    issues = [
        {**ISSUE, "id": "I1", "accepted": False, "disputed": True},
        {**ISSUE, "id": "I2", "accepted": False, "disputed": True,
         "raised_by": ["测试与质量"], "problem": "验收标准不可核对"},
    ]
    mtg = _review_session(run_id, node_id, phase="discuss", state={"issues": issues})
    store.add_participant(mtg.meeting_id, role_key="qa", display_name="测试与质量",
                          mode="reviewer", system_prompt="你是评审者")
    protocol = review_module.ReviewProtocol()
    armed = {"on": True}
    from app.meeting import calls as calls_module

    original_emit = calls_module.emit

    async def flaky_emit(run_id_, type_, node_id_, payload):
        if armed["on"] and type_ == "meeting_turn_finished" \
                and "review_defend" in str(payload.get("kind") or ""):
            armed["on"] = False                    # 第一条辩护发言刚落盘：进程在这里被杀
            raise asyncio.CancelledError()
        return await original_emit(run_id_, type_, node_id_, payload)

    monkeypatch.setattr(calls_module, "emit", flaky_emit)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(protocol.step_discuss(mtg))
    monkeypatch.setattr(calls_module, "emit", original_emit)

    asyncio.run(protocol.step_discuss(mtg))
    kinds = Counter(call["kind"] for call in seen)
    assert kinds["review_defend"] == 2             # 每条意见恰好一次，重入从第二条继续


def test_rework_branch_clears_last_report_fields() -> None:
    """从等待返工恢复：回流标记照旧清掉，旧报告的摘要与阻塞数也一并清掉。"""
    run_id, node_id = _review_run()
    mtg = _review_session(run_id, node_id, phase="verdict", state={
        "rework_pending": True,
        "verdict": "conditional", "summary": "评审会议完成：结论 接受", "blocking": 2,
        "minutes_version": 1})

    asyncio.run(review_module.ReviewProtocol().on_enter(mtg))

    state = store.get_state(mtg.meeting_id)
    assert state["rework_pending"] is False
    assert state["summary"] is None and state["blocking"] is None


def test_chair_keeps_a_rejected_issue(monkeypatch) -> None:
    install(monkeypatch, _replies(
        review=json.dumps({"strengths": [], "issues": [WORDING]}),
        review_merge=_merge(WORDING),
        author_response=lambda text: _respond(text, accepted=False),
        review_rule=lambda text: _rule(text, keep=True),
        revision_verify=lambda text: _verify(text, fixed=True)))
    run_id, node_id = _review_run()

    result = drive(run_id, node_id, _approve, idea=IDEA)

    assert result["done"] is True and result["verdict"] == "conditional"
    issue = _issues(node_id)[0]
    assert issue["standing"] is True and issue["accepted"] is True
    # 主席定夺成立之后按文字级意见修订并验证，处置最终落在"已修订并验证"
    assert issue["disposition"] == "已修订并验证"
    report = parse_front_matter(read_artifact(run_id, "review"))[1]
    assert "保留的异议" in report and "有评审者坚持这条意见" in report


def test_chair_overrules_a_rejected_issue(monkeypatch) -> None:
    install(monkeypatch, _replies(
        review=json.dumps({"strengths": [], "issues": [WORDING]}),
        review_merge=_merge(WORDING),
        author_response=lambda text: _respond(text, accepted=False),
        review_rule=lambda text: _rule(text, keep=False)))
    run_id, node_id = _review_run()

    result = drive(run_id, node_id, _approve, idea=IDEA)

    assert result["done"] is True and result["verdict"] == "accept"
    issue = _issues(node_id)[0]
    assert issue["standing"] is False and issue["disposition"] == "主席定夺：否决"


def test_revision_that_is_never_fixed_blocks_the_precheck(monkeypatch) -> None:
    seen: list[dict] = []
    install(monkeypatch, _replies(
        review=json.dumps({"strengths": [], "issues": [WORDING]}),
        review_merge=_merge(WORDING),
        author_response=_respond,
        revision_verify=lambda text: _verify(text, fixed=False)), seen)
    run_id, node_id = _review_run()

    result = drive(run_id, node_id, _approve, idea=IDEA)

    assert result["done"] is True and result["verdict"] == "conditional"
    assert result["precheck_ok"] is False and "没有改好" in result["precheck_reason"]
    state = _meeting(node_id)["state"]
    assert state["precheck_ok"] is False and "没有改好" in state["precheck_reason"]
    assert Counter(call["kind"] for call in seen)["author_revise"] == 2
    assert _issues(node_id)[0]["disposition"] == "未改好"


def test_escalation_when_rounds_are_used_up(monkeypatch) -> None:
    install(monkeypatch, _replies(
        review=json.dumps({"strengths": [], "issues": [DECISION]}),
        review_merge=_merge(DECISION), author_response=_respond))
    run_id, node_id = _review_run()
    with session_scope() as s:
        node = s.get(Node, node_id)
        node.config_json = {**(node.config_json or {}), "round": 3, "max_rounds": 3}
    asked: list[str] = []

    def answer(checkpoint: dict) -> tuple:
        asked.append(checkpoint["kind"])
        return "approve", {}, ""

    result = drive(run_id, node_id, answer, idea=IDEA)

    assert asked == ["escalation"]
    assert result["done"] is True and result["verdict"] == "return"
    assert result["accepted_by_user"] is True and result["precheck_ok"] is True
    report = parse_front_matter(read_artifact(run_id, "review"))[1]
    assert "接受了现状" in report


def test_user_rejection_becomes_an_issue_and_a_new_report(monkeypatch) -> None:
    install(monkeypatch, _replies())
    run_id, node_id = _review_run()
    drive(run_id, node_id, _never, idea=IDEA)
    assert parse_front_matter(read_artifact(run_id, "review"))[0]["version"] == 1

    with session_scope() as s:
        node = s.get(Node, node_id)
        node.config_json = with_feedback(node.config_json, "范围写得不清楚", "reject")

    install(monkeypatch, _replies(author_response=_respond,
                                  revision_verify=lambda text: _verify(text, fixed=True)))
    result = drive(run_id, node_id, _never, idea=IDEA)

    assert result["done"] is True
    issue = _issues(node_id)[0]
    assert issue["origin"] == "user" and issue["raised_by"] == ["用户"]
    assert parse_front_matter(read_artifact(run_id, "review"))[0]["version"] == 2
    # 用户驳回后进入的第二轮从"受理"起步、跳过准备，同样要有轮次开始事件，且每轮只发一次
    rounds = [e["payload"]["round"] for e in load_events(run_id)
              if e["type"] == "review_round_started"]
    assert rounds == [1, 2]


def test_author_can_ask_the_requester_proxy(monkeypatch) -> None:
    seen: list[dict] = []
    install(monkeypatch, _replies(
        review=json.dumps({"strengths": [], "issues": [WORDING]}),
        review_merge=_merge(WORDING),
        author_response=lambda text: _respond(text, ask=["为什么要冻结范围？"]),
        revision_verify=lambda text: _verify(text, fixed=True)), seen)
    run_id, node_id = _review_run(proxy=True)

    result = drive(run_id, node_id, _approve, idea=IDEA)

    assert result["done"] is True and result["verdict"] == "conditional"
    kinds = Counter(call["kind"] for call in seen)
    assert kinds["answer"] == 1 and kinds["author_response"] == 2
    answer_prompts = [call["text"] for call in seen if call["kind"] == "answer"]
    assert all("需求方代理" in text for text in answer_prompts)


def _respond_asking_about(text: str, ask_id: str = "I1") -> str:
    """第一次回应：追问指定的一条意见，其余直接采纳；根据代理回答的再回应不再追问。"""
    followup = "需求方代理的回答" in text
    return json.dumps({"responses": [
        {"id": issue_id, "accepted": True, "reason": "采纳",
         "ask_requester": [] if followup else (
             ["为什么要冻结范围？"] if issue_id == ask_id else [])}
        for issue_id in _ids(text)]}, ensure_ascii=False)


def _two_issues() -> list[dict]:
    return [
        {**ISSUE, "id": "I1", "accepted": None, "disputed": False},
        {**ISSUE, "id": "I2", "accepted": None, "disputed": False,
         "raised_by": ["测试与质量"], "problem": "验收标准不可核对"},
    ]


def test_proxy_answers_are_not_repeated_after_a_crash(monkeypatch) -> None:
    """代理回答落盘后被中断：重入复用已落盘的回应与回答，不重复发言，意见照常入账。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(
        author_response=_respond_asking_about,
        answer='{"answers": [{"text": "范围冻结是第一周的验收前提", "source": ""}]}'), seen)
    run_id, node_id = _review_run(proxy=True)
    mtg = _review_session(run_id, node_id, phase="respond", state={"issues": _two_issues()})
    store.add_participant(mtg.meeting_id, role_key="qa", display_name="测试与质量",
                          mode="reviewer", system_prompt="你是评审者")
    store.add_participant(mtg.meeting_id, role_key="requester_proxy", display_name="需求方代理",
                          mode="proxy", system_prompt="你是需求方代理，只回答事实问题")
    protocol = review_module.ReviewProtocol()
    original = review_module.speak
    armed = {"on": True}

    async def flaky(mtg_, participant, **kwargs):
        result = await original(mtg_, participant, **kwargs)
        if armed["on"] and kwargs.get("kind") == "answer":
            armed["on"] = False                        # 回答刚落盘：进程在这里被杀
            raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(review_module, "speak", flaky)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(protocol.step_respond(mtg))
    asyncio.run(protocol.step_respond(mtg))

    kinds = Counter(call["kind"] for call in seen)
    assert kinds["answer"] == 1                        # 重入不再让代理重复回答
    assert kinds["author_response"] == 2               # 第一次回应与再回应各恰好一次
    state = store.get_state(mtg.meeting_id)
    assert all(row["accepted"] is True for row in state["issues"])


def test_the_followup_response_is_reused_after_a_crash(monkeypatch) -> None:
    """再回应落盘、意见还没入账时被中断：重入整步零调用，两条意见照常入账。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(
        author_response=_respond_asking_about,
        answer='{"answers": [{"text": "范围冻结是第一周的验收前提", "source": ""}]}'), seen)
    run_id, node_id = _review_run(proxy=True)
    mtg = _review_session(run_id, node_id, phase="respond", state={"issues": _two_issues()})
    store.add_participant(mtg.meeting_id, role_key="qa", display_name="测试与质量",
                          mode="reviewer", system_prompt="你是评审者")
    store.add_participant(mtg.meeting_id, role_key="requester_proxy", display_name="需求方代理",
                          mode="proxy", system_prompt="你是需求方代理，只回答事实问题")
    protocol = review_module.ReviewProtocol()
    original = review_module.speak
    armed = {"on": True}

    async def flaky(mtg_, participant, **kwargs):
        result = await original(mtg_, participant, **kwargs)
        texts = "".join(getattr(segment, "text", "") for segment in kwargs.get("segments") or [])
        if armed["on"] and kwargs.get("kind") == "author_response" \
                and "需求方代理的回答" in texts:
            armed["on"] = False                        # 再回应刚落盘：进程在这里被杀
            raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(review_module, "speak", flaky)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(protocol.step_respond(mtg))
    calls_before = len(seen)
    asyncio.run(protocol.step_respond(mtg))

    assert len(seen) == calls_before                   # 重入整步零调用：三段发言全部复用
    state = store.get_state(mtg.meeting_id)
    assert all(row["accepted"] is True for row in state["issues"])


def test_reviewers_count_in_run_config_limits_the_panel(monkeypatch) -> None:
    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    run_id, node_id = _review_run({"rosters": {"reviewers": {"count": 1}}})

    result = drive(run_id, node_id, _never, idea=IDEA)

    assert result["done"] is True
    meeting_id = _meeting(node_id)["id"]
    reviewers = [row for row in store.list_participants(meeting_id)
                 if row["mode"] == "reviewer"]
    assert len(reviewers) == 1                    # 讨论会议留下两名参会者，按配置只请一名
    assert Counter(call["kind"] for call in seen)["review"] == 1


def test_named_reviewers_become_the_panel_in_order_without_inheriting_the_discussion(
        monkeypatch) -> None:
    """点名评审者：按配置里的顺序成为评审者，不沿用讨论会议的参会者。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    run_id, node_id = _review_run({
        "rosters": {"reviewers": {"role_keys": ["security", "qa"]}}})

    result = drive(run_id, node_id, _never, idea=IDEA)

    assert result["done"] is True and result["verdict"] == "accept"
    meeting_id = _meeting(node_id)["id"]
    reviewers = [(row["role_key"], row["display_name"])
                 for row in store.list_participants(meeting_id) if row["mode"] == "reviewer"]
    assert reviewers == [("security", "安全与合规"), ("qa", "测试与质量")]   # 按配置顺序，名称取自角色库
    assert Counter(call["kind"] for call in seen)["review"] == 2          # 两名评审者各发言一次


def test_a_named_reviewer_that_no_longer_exist_fails_the_meeting(monkeypatch) -> None:
    """点名的评审角色被删了：会议带明确的错误失败，fatal。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    run_id, node_id = _review_run({"rosters": {"reviewers": {"role_keys": ["deleted_role"]}}})

    with pytest.raises(NodeFailure) as excinfo:
        drive(run_id, node_id, _never, idea=IDEA)
    assert "已不存在：deleted_role" in str(excinfo.value)
    assert excinfo.value.fatal is True


def test_a_named_reviewer_not_meant_for_review_fails_the_meeting(monkeypatch) -> None:
    """点名不适合参加评审会议的角色（裁决模型）：会议带明确的错误失败，fatal。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    run_id, node_id = _review_run({"rosters": {"reviewers": {"role_keys": ["judge"]}}})

    with pytest.raises(NodeFailure) as excinfo:
        drive(run_id, node_id, _never, idea=IDEA)
    assert "不适合参加评审" in str(excinfo.value) and "judge" in str(excinfo.value)
    assert excinfo.value.fatal is True


def test_intake_reentry_continues_to_respond(monkeypatch) -> None:
    """受理步的状态写完之后被杀：重入直接去回应，不再把反馈轮带进完整重审。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    run_id, node_id = _review_run()
    mtg = _review_session(run_id, node_id, phase="intake",
                          state={"intake_feedback": "范围不清楚，请修订"})
    protocol = review_module.ReviewProtocol()
    armed = {"on": True}
    original_emit = review_module.emit

    async def flaky_emit(run_id_, type_, node_id_, payload):
        if armed["on"] and type_ == "review_round_started":
            armed["on"] = False                    # 受理已入账、轮次事件还没发：进程在这里被杀
            raise asyncio.CancelledError()
        return await original_emit(run_id_, type_, node_id_, payload)

    monkeypatch.setattr(review_module, "emit", flaky_emit)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(protocol.step_intake(mtg))
    monkeypatch.setattr(review_module, "emit", original_emit)

    result = asyncio.run(protocol.step_intake(mtg))

    assert result.next == "respond"
    kinds = Counter(call["kind"] for call in seen)
    assert kinds["review_intake"] == 1              # 受理发言不重复
    assert kinds["review_points"] == 0 and kinds["review"] == 0   # 不进完整重审


def test_review_round_started_names_the_protocol(monkeypatch) -> None:
    """review_round_started 的载荷带 protocol 字段，与 confirm 的同类事件对齐。"""
    install(monkeypatch, _replies(), None)
    run_id, node_id = _review_run()
    mtg = _review_session(run_id, node_id, phase="prepare", state={})
    emitted: list[dict] = []
    original_emit = review_module.emit

    async def capture_emit(run_id_, type_, node_id_, payload):
        if type_ == "review_round_started":
            emitted.append(dict(payload))
        return await original_emit(run_id_, type_, node_id_, payload)

    monkeypatch.setattr(review_module, "emit", capture_emit)
    asyncio.run(review_module._round_started(mtg))

    assert emitted and emitted[0].get("protocol") == "review"


def test_independent_review_reads_the_reviewers_own_points(monkeypatch) -> None:
    """详设 §8.2：独立评审对照方案与自己写的要点——要点步的产出要进评审步的上下文。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    run_id, node_id = _review_run()

    drive(run_id, node_id, _approve, idea=IDEA)

    review_calls = [call for call in seen if call["kind"] == "review"]
    assert review_calls
    for call in review_calls:
        assert "## 你写下的评审要点" in call["text"]
        assert "决策点与方案是否一致" in call["text"]       # 要点步写下的 focus 进了上下文
        assert "请对照方案、检查单与你先前写下的评审要点" in call["text"]


def test_reviewers_family_mix_is_checked_at_prepare(monkeypatch) -> None:
    """详设 §8.1：没有异家族评审者时，准备步给出提示（写进状态并发节点日志）。"""
    install(monkeypatch, _replies(), None)
    run_id, node_id = _review_run()

    drive(run_id, node_id, _approve, idea=IDEA)

    state = store.latest_meeting_for_node(node_id)["state"]
    assert "评审者与讨论会议的参会者全部同属一个模型家族" in (state.get("family_hint") or "")
    logs = [e for e in load_events(run_id) if e["type"] == "node_log"
            and "自我偏好" in str(e["payload"].get("message") or "")]
    assert logs


def test_a_heterogeneous_reviewer_suppresses_the_family_hint(monkeypatch) -> None:
    """有一名评审者绑定了别的家族的模型（评审者模式的行）：不给提示。"""
    from types import SimpleNamespace
    from app.meeting import calls as calls_module

    install(monkeypatch, _replies(), None)
    run_id, node_id = _review_run()
    other = SimpleNamespace(id=99_999, model_name="other-family-9x", display_name="其他家族",
                            context_window=32768, max_output_tokens=4096)
    base_model_for = calls_module._model_for

    def split_model(participant):
        if participant.get("mode") == "reviewer":
            return other                            # 评审者全部换到另一个家族的模型
        return base_model_for(participant)

    monkeypatch.setattr(calls_module, "_model_for", split_model)

    drive(run_id, node_id, _approve, idea=IDEA)

    state = store.latest_meeting_for_node(node_id)["state"]
    assert not (state.get("family_hint") or "")


def test_escalation_checkpoint_carries_the_family_hint(monkeypatch) -> None:
    """详设 §8.1 的"在确认点上提示"：升级确认点的载荷带上异家族提示。"""
    install(monkeypatch, _replies(
        review=json.dumps({"strengths": [], "issues": [DECISION]}),
        review_merge=_merge(DECISION), author_response=_respond), None)
    run_id, node_id = _review_run()
    with session_scope() as s:
        node = s.get(Node, node_id)
        node.config_json = {**(node.config_json or {}), "round": 3, "max_rounds": 3}

    drive(run_id, node_id, _approve, idea=IDEA)

    with session_scope() as s:
        gate = s.query(Gate).filter(Gate.run_id == run_id, Gate.kind == "escalation").one()
    assert "评审者与讨论会议的参会者全部同属一个模型家族" in gate.payload_json.get("family_hint", "")


def test_review_prepare_fails_without_a_ledger(monkeypatch) -> None:
    """详设 §8.2 准备行"确认方案和账本齐全"：账本是空的带明确的错误失败，不按空账本评审。"""
    install(monkeypatch, _replies(), None)
    run_id, node_id = _review_run()
    from app.db.models import DecisionPoint, Meeting as MeetingRow
    with session_scope() as s:
        meeting_ids = [m.id for m in s.query(MeetingRow).filter(
            MeetingRow.run_id == run_id).all()]
        s.query(DecisionPoint).filter(
            DecisionPoint.meeting_id.in_(meeting_ids)).delete(synchronize_session=False)

    with pytest.raises(NodeFailure) as excinfo:
        drive(run_id, node_id, _never, idea=IDEA)

    assert "账本是空的" in str(excinfo.value)
    assert excinfo.value.fatal is True


def test_review_report_sections_follow_the_design_order(monkeypatch) -> None:
    """详设 §8.7 报告节顺序：评了什么、谁评的、结论、问题清单、优点、保留的异议。"""
    install(monkeypatch, _replies(), None)
    run_id, node_id = _review_run()

    drive(run_id, node_id, _approve, idea=IDEA)

    body = parse_front_matter(read_artifact(run_id, "review"))[1]
    assert body.index("## 结论") < body.index("## 问题清单及处置状态") < body.index("## 优点")


# ---------------- S5.7：校验器、续开短路、参数开关与行为分支 ----------------
def test_structured_output_validators_reject_the_remaining_bad_shapes() -> None:
    """结构化输出的校验器逐个报错分支：坏的形状都要带着明确原因被拒。"""
    from app.meeting.protocols.review import (_validate_answers, _validate_checklist,
                                              _validate_focus, _validate_intake,
                                              _validate_merge, _validate_responses,
                                              _validate_review, _validate_rulings,
                                              _validate_verified)

    assert _validate_intake({"issues": "不是数组"}) == ["issues 须是数组"]
    assert _validate_intake({"issues": []}) == ["issues 须是非空数组"]
    assert _validate_intake({"issues": ["不是对象"]}) == ["每条意见须是对象"]
    assert _validate_intake({"issues": [{"problem": "   "}]}) == ["每条意见须有 problem"]
    assert _validate_checklist({"checklist": []}) == ["checklist 须是非空字符串数组"]
    assert _validate_checklist({"checklist": ["合法", 5]}) == ["checklist 须是非空字符串数组"]
    assert _validate_focus({"focus": "不是数组"}) == ["focus 须是非空字符串数组"]
    assert _validate_focus({"focus": ["  "]}) == ["focus 须是非空字符串数组"]
    assert _validate_review({"strengths": "不是数组", "issues": []}) == ["strengths 须是字符串数组"]
    assert _validate_review({"strengths": [], "issues": [], "resolved": "不是数组"},
                            delta=True) == ["resolved 须是数组"]
    assert _validate_review({"strengths": [], "issues": [],
                             "resolved": [{"id": " ", "handled": True}]},
                            delta=True) == ["resolved 的每一项须有 id 与布尔值 handled"]
    assert _validate_merge({"issues": [{"problem": ""}]}) == ["每条意见须有 problem"]
    assert _validate_responses(["I1"])({"responses": "不是数组"}) == ["responses 须是数组"]
    assert _validate_responses(["I1"])({"responses": ["不是对象"]}) == ["responses 的每一项须是对象"]
    assert _validate_responses(["I1"])({"responses": []}) == [
        "responses 须恰好覆盖这些意见：['I1']"]
    assert _validate_responses(["I1"])({"responses": [{"id": "I1", "accepted": "是"}]}) == [
        "I1：accepted 须是布尔值"]
    assert _validate_responses(["I1"])({"responses": [{"id": "I1", "accepted": False}]}) == [
        "I1：不采纳必须写明理由"]
    assert _validate_responses(["I1"])({
        "responses": [{"id": "I1", "accepted": True, "ask_requester": [5]}]}) == [
        "I1：ask_requester 须是字符串数组"]
    assert _validate_answers(2)({"answers": [{"text": "只有一条"}]}) == [
        "answers 须恰好包含 2 项，逐条对应问题"]
    assert _validate_answers(2)({"answers": [{"text": "一"}, {"text": " "}]}) == [
        "第 2 条回答缺少 text"]
    assert _validate_rulings(["I1"])({"rulings": "不是数组"}) == ["rulings 须是数组"]
    assert _validate_rulings(["I1"])({"rulings": ["不是对象"]}) == ["rulings 的每一项须是对象"]
    assert _validate_rulings(["I1"])({"rulings": []}) == ["rulings 须恰好覆盖这些意见：['I1']"]
    assert _validate_rulings(["I1"])({
        "rulings": [{"id": "I1", "keep": "是", "note": "理由"}]}) == ["I1：keep 须是布尔值"]
    assert _validate_rulings(["I1"])({
        "rulings": [{"id": "I1", "keep": True, "note": " "}]}) == ["I1：note 不能为空"]
    assert _validate_verified(["I1"])({"verified": "不是数组"}) == ["verified 须是数组"]
    assert _validate_verified(["I1"])({"verified": ["不是对象"]}) == ["verified 的每一项须是对象"]
    assert _validate_verified(["I1"])({"verified": []}) == ["verified 须恰好覆盖这些意见：['I1']"]
    assert _validate_verified(["I1"])({"verified": [{"id": "I1", "fixed": 1}]}) == [
        "I1：fixed 须是布尔值"]


def test_missing_chair_or_author_fails_fatally() -> None:
    """会议缺评审主席或执笔者：带明确的错误失败，不让后续步骤空转。"""
    run_id, node_id = _review_run()
    mtg = _bare_review_meeting(run_id, node_id)

    with pytest.raises(NodeFailure) as chair:
        review_module._chair(mtg)
    with pytest.raises(NodeFailure) as author:
        review_module._author(mtg)

    assert "评审会议缺少评审主席" in str(chair.value) and chair.value.fatal is True
    assert "评审会议缺少执笔者" in str(author.value) and author.value.fatal is True


def test_later_round_charter_lists_only_decision_issues() -> None:
    """详设 §8.3：第 2 轮起章程里的"上一轮意见"只列决策级的，文字级意见不进。"""
    run_id, node_id = _review_run()
    mtg = _review_session(run_id, node_id, phase="review", state={"issues": [
        {**ISSUE, "id": "I1", "round": 1, "category": "wording"},
        {**ISSUE, "id": "I2", "round": 1, "category": "decision", "severity": "blocker",
         "disposition": "已处理"}], "checklist": ["范围是否完整"]})
    store.update_meeting(mtg.meeting_id, round=2)

    charter = review_module._charter_dict(mtg)

    section = next(row["text"] for row in charter["sections"]
                   if row["title"] == "上一轮的决策级意见与处理情况")
    assert "I2" in section and "I1" not in section


def test_speaker_fallback_and_family_hint_guards(monkeypatch) -> None:
    """提出者不在名单时用第一名评审者；没有讨论会议或取不到参与者时异家族提示为空。"""
    install(monkeypatch, _replies(), None)
    run_id, node_id = _review_run()
    mtg = _review_session(run_id, node_id, phase="discuss")

    speaker = review_module._speaker_for(mtg, {"id": "I1", "raised_by": ["已离任的评审者"]})
    assert speaker["display_name"] == "架构师"

    bare = _bare_review_meeting(run_id, node_id, phase="discuss")
    assert review_module._speaker_for(bare, {"id": "I1", "raised_by": []}) is None

    monkeypatch.setattr(review_module, "_discussion", lambda _mtg: None)
    assert review_module._family_hint(mtg) == ""
    monkeypatch.setattr(review_module, "_discussion", lambda _mtg: {"id": 0, "state": {}})
    assert review_module._family_hint(mtg) == ""


def test_round_started_event_is_emitted_once_per_round(monkeypatch) -> None:
    """同一轮的轮次开始事件只发一次：重入再调一次不再发。"""
    install(monkeypatch, _replies(), None)
    run_id, node_id = _review_run()
    mtg = _review_session(run_id, node_id, phase="prepare", state={})
    emitted: list[dict] = []
    original_emit = review_module.emit

    async def capture_emit(run_id_, type_, node_id_, payload):
        if type_ == "review_round_started":
            emitted.append(dict(payload))
        return await original_emit(run_id_, type_, node_id_, payload)

    monkeypatch.setattr(review_module, "emit", capture_emit)
    asyncio.run(review_module._round_started(mtg))
    asyncio.run(review_module._round_started(mtg))

    assert len(emitted) == 1 and emitted[0]["round"] == 1


def test_intake_without_participants_defers_to_prepare(monkeypatch) -> None:
    """带着反馈续开、参会者还没建出来（如准备步中断）：受理直推准备，不空调模型。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    run_id, node_id = _review_run()
    mtg = _bare_review_meeting(run_id, node_id, phase="intake",
                               state={"intake_feedback": "用户要求重新评审"})
    protocol = review_module.ReviewProtocol()

    result = asyncio.run(protocol.step_intake(mtg))

    assert result.action == "advance" and result.next == "prepare"
    assert seen == []


def test_resuming_skips_review_steps_whose_results_are_already_in_state(monkeypatch) -> None:
    """各步的 done 标记命中后的直推：崩溃重入不再调模型，按顺序推进下一步。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    run_id, node_id = _review_run()
    mtg = _review_session(run_id, node_id, phase="intake")
    steps = ("intake", "prepare", "points", "review", "merge", "respond", "discuss",
             "verdict", "revise", "verify")
    store.set_state(mtg.meeting_id, {"done": {step: 1 for step in steps}})
    expected = {"intake": "prepare", "prepare": "points", "points": "review",
                "review": "merge", "merge": "respond", "respond": "discuss",
                "discuss": "verdict", "verdict": "revise", "revise": "verify",
                "verify": "report"}
    protocol = review_module.ReviewProtocol()

    for step, target in expected.items():
        result = asyncio.run(getattr(protocol, f"step_{step}")(mtg))
        assert result.action == "advance" and result.next == target, step

    assert seen == []


def test_pre_brief_and_conference_switches_skip_their_steps(monkeypatch) -> None:
    """pre_brief=False 跳过要点步（评审就用普通指令）；conference=False 跳过主席定夺步。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    run_id, node_id = _review_run(config={"params": {"review": {"pre_brief": False,
                                                                "conference": False}}})

    result = drive(run_id, node_id, _never, idea=IDEA)

    assert result["done"] is True
    kinds = Counter(call["kind"] for call in seen)
    assert "review_points" not in kinds and "review_defend" not in kinds
    assert "review_rule" not in kinds
    review_call = next(call for call in seen if call["kind"] == "review")
    assert "## 你写下的评审要点" not in review_call["text"]


def test_reviewers_max_limits_the_panel_and_min_shortage_fails(monkeypatch) -> None:
    """reviewers_max 截断评审者人数；评审者少于 reviewers_min 时带明确的错误失败。"""
    install(monkeypatch, _replies(), None)
    run_id, node_id = _review_run(config={"params": {"review": {"reviewers_max": 1}}})

    drive(run_id, node_id, _never, idea=IDEA)

    reviewers = [row for row in store.list_participants(_meeting(node_id)["id"])
                 if row["mode"] == "reviewer"]
    assert len(reviewers) == 1

    install(monkeypatch, _replies(), None)
    run_id, node_id = _review_run(config={"params": {"review": {"reviewers_min": 99}}})
    with pytest.raises(NodeFailure) as excinfo:
        drive(run_id, node_id, _never, idea=IDEA)

    assert "评审者人数不足" in str(excinfo.value) and excinfo.value.fatal is True


def _replace_latest_discussion(run_id: str, participants: list[tuple[str, str]]) -> None:
    """再建一场"最新的"讨论会议：用它替换评审会继承的参会者与账本。"""
    init_db()
    with session_scope() as s:
        discuss_id = s.query(Node).filter(Node.run_id == run_id,
                                          Node.node_key == "discuss").one().id
    meeting_id = store.create_meeting(run_id=run_id, node_id=discuss_id, node_key="discuss",
                                      protocol="discussion", charter={"sections": []}, config={})
    store.update_meeting(meeting_id, status="completed", phase="done", round=1)
    for role_key, mode in participants:
        store.add_participant(meeting_id, role_key=role_key, display_name=role_key,
                              mode=mode, system_prompt=f"你是{role_key}")
    store.upsert_decision_point(meeting_id, "D1", title="首版的范围与入口",
                                question="首版做哪些功能", options_json=["网页版"],
                                status="agreed", conclusion="网页版", closed_round=1,
                                raised_by="架构师")


def test_author_comes_from_the_template_when_the_discussion_has_no_facilitator(
        monkeypatch) -> None:
    """讨论会议没有主持人可继承时，执笔者取角色库的 facilitator 模板。"""
    from app.engine.templates import role_template

    install(monkeypatch, _replies(), None)
    run_id, node_id = _review_run()
    _replace_latest_discussion(run_id, [("architect", "participant"), ("ux", "participant")])

    result = drive(run_id, node_id, _never, idea=IDEA)

    assert result["done"] is True
    authors = [row for row in store.list_participants(_meeting(node_id)["id"])
               if row["mode"] == "author"]
    assert len(authors) == 1 and authors[0]["role_key"] == "facilitator"
    # 名字取自 facilitator 模板（不是别的模板）：模板换了人这里会变
    assert authors[0]["display_name"] == role_template("facilitator")["name"]


def test_reviewers_fall_back_to_the_role_library(monkeypatch) -> None:
    """讨论会议没有可继承的参会者时，评审者从角色库里取适合评审的前三名。"""
    install(monkeypatch, _replies(), None)
    run_id, node_id = _review_run()
    _replace_latest_discussion(run_id, [("facilitator", "facilitator")])

    result = drive(run_id, node_id, _never, idea=IDEA)

    assert result["done"] is True
    reviewers = [row for row in store.list_participants(_meeting(node_id)["id"])
                 if row["mode"] == "reviewer"]
    assert [row["role_key"] for row in reviewers] == ["architect", "ux", "qa"]


def test_role_library_fallback_respects_the_configured_reviewer_count(monkeypatch) -> None:
    """从角色库兜底时也按运行级配置的固定人数截断。"""
    install(monkeypatch, _replies(), None)
    run_id, node_id = _review_run(config={"rosters": {"reviewers": {"count": 2}}})
    _replace_latest_discussion(run_id, [("facilitator", "facilitator")])

    result = drive(run_id, node_id, _never, idea=IDEA)

    assert result["done"] is True
    reviewers = [row for row in store.list_participants(_meeting(node_id)["id"])
                 if row["mode"] == "reviewer"]
    assert [row["role_key"] for row in reviewers] == ["architect", "ux"]


def test_discuss_skips_defending_when_there_is_no_reviewer_to_defend(
        monkeypatch) -> None:
    """有分歧却找不到提出这条意见的评审者：跳过辩护，主席照样出定夺。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(review_rule=lambda text: _rule(text, keep=True)), seen)
    run_id, node_id = _review_run()
    mtg = _bare_review_meeting(run_id, node_id, phase="discuss", state={
        "issues": [{**ISSUE, "disputed": True}]})
    store.add_participant(mtg.meeting_id, role_key="review_chair", display_name="评审主席",
                          mode="chair", system_prompt="你是评审主席")
    protocol = review_module.ReviewProtocol()

    result = asyncio.run(protocol.step_discuss(mtg))

    assert result.action == "advance" and result.next == "verdict"
    assert "review_defend" not in Counter(call["kind"] for call in seen)
    assert Counter(call["kind"] for call in seen)["review_rule"] == 1


def test_review_prepare_fails_without_a_proposal(monkeypatch) -> None:
    """没有方案工件时准备步带明确的错误失败，不评审空方案。"""
    install(monkeypatch, _replies(), None)
    run_id, node_id = _review_run(proposal=False)
    mtg = _review_session(run_id, node_id, phase="prepare")

    with pytest.raises(NodeFailure) as excinfo:
        asyncio.run(review_module.ReviewProtocol().step_prepare(mtg))

    assert "方案还没有产出" in str(excinfo.value) and excinfo.value.fatal is True


@pytest.mark.parametrize("draft, message", [
    ("## 修改说明\n\n只改了范围一处。\n", "方案缺少这些章节"),
    (PROPOSAL, "修订版缺少「## 修改说明」一节"),
])
def test_revising_with_a_bad_draft_is_rejected(monkeypatch, draft, message) -> None:
    """修订稿缺章节或缺修改说明：带明确原因被拒，不写出不合格的版本。"""
    install(monkeypatch, _replies(author_revise=draft), None)
    run_id, node_id = _review_run()
    mtg = _review_session(run_id, node_id, phase="revise", state={"issues": [dict(ISSUE)]})

    with pytest.raises(NodeFailure, match=message):
        asyncio.run(review_module.ReviewProtocol().step_revise(mtg))

    assert read_artifact(run_id, "proposal").count("# 建设方案") == 1   # 没有写出新版


def test_precheck_rejects_a_returned_verdict_without_user_acceptance() -> None:
    """详设的程序前置检查：结论为退回且人未接受、或有意见没改好时都不通过。"""
    protocol = review_module.ReviewProtocol()

    assert protocol._precheck("accept", False, []) == (True, "")
    assert protocol._precheck("return", True, []) == (True, "")
    assert protocol._precheck("accept", False, ["I1"]) == (False, "有意见没有改好：I1")
    assert protocol._precheck("return", False, []) == (
        False, "评审结论为退回，讨论会议需要先复议")


def _first_then(first: str, rest: str):
    """第一次调用回第一份，之后一直回第二份。"""
    calls: list[int] = []

    def reply(text: str) -> str:
        calls.append(1)
        return first if len(calls) == 1 else rest

    return reply


def test_a_rejected_revision_is_asked_again_on_retry(monkeypatch) -> None:
    """修订稿校验不过要丢掉这一步的复用键：重试时重新问模型，拿到合格稿写出新版。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(author_revise=_first_then(PROPOSAL, REVISED_PROPOSAL)), seen)
    run_id, node_id = _review_run()
    mtg = _review_session(run_id, node_id, phase="revise", state={"issues": [dict(ISSUE)]})
    protocol = review_module.ReviewProtocol()
    targets = [dict(ISSUE)]
    key = review_module._step_key("revise", mtg, [ISSUE["id"]],
                                  extra=review_module._issue_states(targets))

    with pytest.raises(NodeFailure, match="修订版缺少"):
        asyncio.run(protocol._revise(mtg, targets))
    assert key not in (store.get_state(mtg.meeting_id).get("step_turns") or {})

    asyncio.run(protocol._revise(mtg, targets))

    assert Counter(call["kind"] for call in seen)["author_revise"] == 2
    assert parse_front_matter(read_artifact(run_id, "proposal"))[0]["version"] == 2
