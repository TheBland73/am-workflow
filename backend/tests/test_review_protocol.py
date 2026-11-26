"""评审协议：独立评审、汇总分级、作者回应、主席定夺、程序裁决、修订与验证、回流与增量复审。"""
from __future__ import annotations

import asyncio
import json
import re
from collections import Counter

import pytest

from app.artifacts.store import parse_front_matter, read_artifact, write_artifact
from app.db.base import init_db, session_scope
from app.db.models import Gate, Node, Run, Workflow
from app.engine.feedback import with_feedback
from app.engine.orchestrator import orchestrator
from app.engine.params import apply_run_config
from app.engine.templates import instantiate_nodes, seed_defaults, workflow_definition
from app.meeting import store
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


def _review_run(config: dict | None = None, *, proxy: bool = False) -> tuple[str, int]:
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
    kinds = Counter(call["kind"] for call in seen)
    assert "author_response" not in kinds and "author_revise" not in kinds
    report = parse_front_matter(read_artifact(run_id, "review"))[1]
    assert "结论：接受。" in report and "方案覆盖了主要决策点" in report


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
