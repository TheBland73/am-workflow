"""需求确认协议：程序核对、写预期、忠实性核对、质量评审、汇总裁决、回流与增量复核。"""
from __future__ import annotations

import asyncio
import json
import re
from collections import Counter

import pytest
from fastapi.testclient import TestClient

from app.artifacts.store import parse_front_matter, read_artifact, write_artifact
from app.db.base import init_db, session_scope
from app.db.models import Node, Run, Workflow
from app.engine.events import load_events
from app.engine.feedback import with_feedback
from app.engine.orchestrator import orchestrator
from app.engine.params import apply_run_config
from app.engine.runners import NodeContext, NodeFailure
from app.engine.templates import instantiate_nodes, seed_defaults, workflow_definition
from app.main import app
from app.meeting import store
from app.meeting.protocols import confirm as confirm_module
from app.meeting.session import MeetingSession
from tests.meeting_fakes import drive, install

IDEA = "给小团队做一个反馈收集工具"
_counter = iter(range(1, 1000))

SPEC = (
    "## 需求规格\n\n"
    "### R1 收集反馈\n\n"
    "WHEN 团队成员提交反馈 THE SYSTEM SHALL 保存反馈并返回提交成功。\n\n"
    "验收标准：\n- Given 一名团队成员，When 提交反馈，Then 系统保存该反馈并提示成功。\n\n"
    "优先级：P1\n\n"
    "### R2 导出反馈\n\n"
    "WHEN 团队成员请求导出 THE SYSTEM SHALL 生成一份可打开的反馈清单文件。\n\n"
    "验收标准：\n- Given 已有反馈，When 请求导出，Then 得到一份可打开的文件。\n\n"
    "优先级：P2\n\n"
    "### R3 查看反馈列表\n\n"
    "WHEN 团队成员打开反馈列表 THE SYSTEM SHALL 按时间倒序显示全部反馈。\n\n"
    "验收标准：\n- Given 已有反馈，When 打开列表，Then 最新的反馈排在最前面。\n\n"
    "优先级：P3\n\n"
    "## 需求追踪矩阵\n\n"
    "| 需求编号 | 来源决策点 | 方案章节 | 优先级 |\n"
    "|---|---|---|---|\n"
    "| R1 | D1 | 关键流程 | P1 |\n"
    "| R2 | D1 | 关键流程 | P2 |\n"
    "| R3 | D2 | 关键流程 | P3 |\n"
)

# 一条需求不在追踪矩阵里：机械核对不过，但不牵连其它规则
MISSING_R1 = SPEC.replace("| R1 | D1 | 关键流程 | P1 |\n", "")


def _points_in(text: str) -> list[str]:
    """从指令里的"必须覆盖/核对这些决策点：D1、D2"取出本轮给的决策点。"""
    marker = re.search(r"必须(?:覆盖|核对)这些决策点[：:]([^\n]*)", text)
    segment = marker.group(1) if marker else text
    return list(dict.fromkeys(re.findall(r"\bD\d+\b", segment)))


def _expected(text: str) -> str:
    rows = [{"index": index, "content": f"{index} 的结论应当体现在需求里"}
            for index in _points_in(text)]
    return json.dumps({"expected": rows}, ensure_ascii=False)


def _faithful(text: str) -> str:
    rows = [{"index": index, "result": "faithful", "reason": ""}
            for index in _points_in(text)]
    return json.dumps({"checks": rows, "added": []}, ensure_ascii=False)


def _checks(text: str, *, distorted: tuple[str, ...] = ()) -> str:
    rows = []
    for index in _points_in(text):
        if index in distorted:
            rows.append({"index": index, "result": "distorted",
                         "reason": "需求没有按决策点的结论写"})
        else:
            rows.append({"index": index, "result": "faithful", "reason": ""})
    return json.dumps({"checks": rows, "added": []}, ensure_ascii=False)


def _first_only(text: str) -> str:
    """只核对指令里列出的第一个决策点：分区核对下正好是本角色的范围。"""
    rows = [{"index": index, "result": "faithful", "reason": ""}
            for index in _points_in(text)[:1]]
    return json.dumps({"checks": rows, "added": []}, ensure_ascii=False)


def _quality(severity: str) -> str:
    return json.dumps({"issues": [{
        "requirement": "R1", "criterion": "可验证", "severity": severity,
        "problem": "R1 的验收标准不够具体", "suggestion": "写出可核对的验收条件"}]},
        ensure_ascii=False)


def _merge(*rows: dict) -> str:
    return json.dumps({"items": list(rows)}, ensure_ascii=False)


def _replies(**overrides) -> dict:
    base = {
        "expect": _expected,
        "fidelity": _faithful,
        "quality": '{"issues": []}',
        "confirm_merge": '{"items": []}',
        "confirm_intake": json.dumps({"items": [{
            "kind": "quality", "requirement": "R1", "criterion": "无歧义",
            "severity": "major", "problem": "用户认为 R1 的验收标准不清楚",
            "suggestion": "把验收标准写成可核对的条件"}]}, ensure_ascii=False),
    }
    base.update(overrides)
    return base


def _confirm_run(config: dict | None = None, *, spec: str = SPEC,
                 participants: list[tuple[str, str]] | None = None,
                 stances: dict[str, list[str]] | None = None,
                 with_review: bool = True) -> tuple[str, int]:
    """一个跑到需求确认之前的运行：讨论会议（两个已一致决策点、一名参会者）与已完成的评审会议。"""
    init_db()
    seed_defaults()
    run_id = f"confirm-run-{next(_counter)}"
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
    write_artifact(run_id, "requirement", spec, created_by="requirement",
                   inputs=["proposal", "discussion"], status="draft")
    with session_scope() as s:
        discuss_id = s.query(Node).filter(Node.run_id == run_id,
                                          Node.node_key == "discuss").one().id
        review_id = s.query(Node).filter(Node.run_id == run_id,
                                         Node.node_key == "review").one().id
        confirm_id = s.query(Node).filter(Node.run_id == run_id,
                                          Node.node_key == "confirm").one().id
    discussion_id = store.create_meeting(run_id=run_id, node_id=discuss_id, node_key="discuss",
                                         protocol="discussion", charter={"sections": []},
                                         config={})
    store.update_meeting(discussion_id, status="completed", phase="done", round=1)
    store.add_participant(discussion_id, role_key="facilitator", display_name="主持人",
                          mode="facilitator", system_prompt="你是主持人")
    rows = participants or [("architect", "架构师")]
    ids: dict[str, int] = {}
    for role_key, name in rows:
        ids[role_key] = store.add_participant(
            discussion_id, role_key=role_key, display_name=name, mode="participant",
            system_prompt=f"你是{name}，关注需求是否忠实于决策点")
    points = {
        "D1": store.upsert_decision_point(discussion_id, "D1", title="首版的范围与入口",
                                          question="首版做哪些功能", options_json=["网页版"],
                                          status="agreed", conclusion="网页版", closed_round=1,
                                          raised_by="架构师"),
        "D2": store.upsert_decision_point(discussion_id, "D2", title="多人共用时如何区分反馈",
                                          question="首版是否需要登录",
                                          options_json=["需要", "不需要"], status="agreed",
                                          conclusion="不需要", closed_round=1,
                                          raised_by="用户体验"),
    }
    names = dict(rows)
    for role_key, indexes in (stances or {}).items():
        for index in indexes:
            store.set_stance(points[index], ids[role_key], {
                "level": 1, "reason": "同意这个结论", "choice": "", "round": 1,
                "name": names[role_key]})
    if with_review:
        review_meeting = store.create_meeting(run_id=run_id, node_id=review_id, node_key="review",
                                              protocol="review", charter={"sections": []},
                                              config={})
        store.update_meeting(review_meeting, status="completed", phase="done", round=1)
        store.add_participant(review_meeting, role_key="architect", display_name="架构师",
                              mode="reviewer", system_prompt="你是评审者")
    return run_id, confirm_id


def _never(checkpoint: dict) -> tuple:
    raise AssertionError(f"不该出现人工确认点：{checkpoint['title']}")


def _confirm_session(run_id: str, node_id: int, *, phase: str, state: dict) -> MeetingSession:
    """直接建一场确认会议与它的会话，用来单独驱动某个步骤（不经 run_meeting）。"""
    meeting_id = store.create_meeting(run_id=run_id, node_id=node_id, node_key="confirm",
                                      protocol="confirm", charter={}, config={})
    store.update_meeting(meeting_id, phase=phase, round=1)
    store.set_state(meeting_id, dict(state))
    with session_scope() as s:
        node = s.get(Node, node_id)
        ctx = NodeContext(run_id=run_id, node_id=node_id, node_key="confirm", type="MEETING",
                          name="需求确认会议", phase=phase, config=dict(node.config_json or {}),
                          assignee={}, attempt_id=0, attempt_no=1)
    return MeetingSession(meeting_id=meeting_id, ctx=ctx)


def _meeting(node_id: int) -> dict:
    return store.latest_meeting_for_node(node_id)


def _state(node_id: int) -> dict:
    return _meeting(node_id)["state"]


def _items(node_id: int) -> list[dict]:
    return list(_state(node_id).get("items") or [])


def test_faithful_spec_passes(monkeypatch) -> None:
    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    run_id, node_id = _confirm_run()

    result = drive(run_id, node_id, _never, idea=IDEA)

    assert result["done"] is True
    assert result["verdict"] == "pass" and result["precheck_ok"] is True
    assert _state(node_id)["precheck_ok"] is True
    # 报告的摘要与阻塞数也记进会议状态：/runs/{id}/review 与指标按报告的会议取
    assert _state(node_id)["summary"] == "需求确认会议完成：结论 通过，0 条待办，0 条阻塞"
    assert _state(node_id)["blocking"] == 0
    assert any(section["title"] == "需求规格（当前版本）"
               for section in _meeting(node_id)["charter"].get("sections") or [])
    assert _items(node_id) == []
    report = parse_front_matter(read_artifact(run_id, "review"))
    assert report[0]["review_type"] == "requirement"   # 与方案评审报告共用 review 工件，靠它区分
    assert "结论：通过。" in report[1] and "全部通过" in report[1]
    kinds = Counter(call["kind"] for call in seen)
    assert kinds["expect"] == 1 and kinds["fidelity"] == 1 and kinds["quality"] == 1
    assert kinds["confirm_intake"] == 0 and kinds["confirm_merge"] == 0
    # 写预期只看账本：提示里不能出现需求规格正文
    expect_prompts = [call["text"] for call in seen if call["kind"] == "expect"]
    assert expect_prompts and all("WHEN 团队成员提交反馈" not in text
                                  for text in expect_prompts)


def test_named_reviewers_become_quality_reviewers_when_no_review_meeting_exists(
        monkeypatch) -> None:
    """没有评审会议、运行级配置点名评审者：质量评审者取点名的角色。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    run_id, node_id = _confirm_run({"rosters": {"reviewers": {"role_keys": ["security"]}}},
                                   with_review=False)

    result = drive(run_id, node_id, _never, idea=IDEA)

    assert result["done"] is True
    meeting_id = _meeting(node_id)["id"]
    quality = [(row["role_key"], row["display_name"])
               for row in store.list_participants(meeting_id) if row["mode"] == "quality_reviewer"]
    assert quality == [("security", "安全与合规")]


def test_a_named_reviewer_not_meant_for_review_fails_the_confirm_meeting(monkeypatch) -> None:
    """点名不适合参加评审会议的角色（裁决模型）：确认会议带明确的错误失败，fatal。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    run_id, node_id = _confirm_run({"rosters": {"reviewers": {"role_keys": ["judge"]}}},
                                   with_review=False)

    with pytest.raises(NodeFailure) as excinfo:
        drive(run_id, node_id, _never, idea=IDEA)
    assert "不适合参加评审" in str(excinfo.value) and "judge" in str(excinfo.value)
    assert excinfo.value.fatal is True


def test_minutes_endpoint_returns_the_report_this_meeting_wrote(monkeypatch) -> None:
    """纪要接口只认会议自己写的那一版报告：方案评审报告与需求确认报告共用 review 工件。"""
    install(monkeypatch, _replies())
    run_id, node_id = _confirm_run()
    # 此前的评审会议留下的报告（第 1 版）
    write_artifact(run_id, "review", "# 方案评审报告", created_by="chair", inputs=["proposal"],
                   status="reviewed", extra_meta={"review_type": "proposal"})

    result = drive(run_id, node_id, _never, idea=IDEA)

    assert result["done"] is True
    meeting_id = _meeting(node_id)["id"]
    with TestClient(app) as client:
        response = client.get(f"/api/runs/{run_id}/meetings/{meeting_id}/minutes")

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["kind"] == "review"
    assert data["version"] == result["report"]["version"] == 2
    assert "结论：通过。" in data["body"] and "方案评审报告" not in data["body"]


def test_mechanical_failure_skips_model_checks_and_reroutes(monkeypatch) -> None:
    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    run_id, node_id = _confirm_run(spec=MISSING_R1)

    first = drive(run_id, node_id, _never, idea=IDEA)

    kinds = Counter(call["kind"] for call in seen)
    assert kinds["expect"] == 0 and kinds["fidelity"] == 0 and kinds["quality"] == 0
    assert first["reroute"]["targets"] == ["requirement"]
    feedback = first["reroute"]["feedback"]
    assert "R1" in feedback and "追踪矩阵" in feedback
    meeting_id = _meeting(node_id)["id"]
    assert store.get_meeting(meeting_id)["status"] == "waiting_rework"
    assert _state(node_id)["verdict"] == "fail"

    # 改好规格、写出第 2 版后重新驱动
    write_artifact(run_id, "requirement", SPEC, created_by="requirement",
                   inputs=["discussion"], status="draft")
    with session_scope() as s:
        config = dict(s.get(Node, node_id).config_json or {})
    asyncio.run(orchestrator._handle_reroute(
        run_id, {"id": node_id, "node_key": "confirm", "config": config}, first["reroute"]))
    install(monkeypatch, _replies(), seen)

    second = drive(run_id, node_id, _never, idea=IDEA)

    assert second["done"] is True and second["verdict"] == "pass"
    assert _meeting(node_id)["id"] == meeting_id
    counts = Counter(call["kind"] for call in seen)
    assert counts["expect"] == 1               # 一名核对者，只写一轮
    # 上一轮没有走样或遗漏，需求正文也没改：增量复核没有要看的点
    assert counts["fidelity"] == 0


def test_distorted_point_fails_and_chair_fills_the_requirement(monkeypatch) -> None:
    install(monkeypatch, _replies(
        fidelity=lambda text: _checks(text, distorted=("D2",)),
        confirm_merge=lambda text: _merge({
            "kind": "distorted", "index": "D2", "requirement": "R3",
            "problem": "需求没有按 D2 的结论写", "suggestion": "按 D2 的结论改写 R3"})))
    run_id, node_id = _confirm_run()

    result = drive(run_id, node_id, _never, idea=IDEA)

    assert result["reroute"]["targets"] == ["requirement"]
    assert _state(node_id)["verdict"] == "fail"
    items = _items(node_id)
    assert len(items) == 1
    assert items[0]["kind"] == "distorted"
    assert items[0]["index"] == "D2" and items[0]["requirement"] == "R3"
    feedback = result["reroute"]["feedback"]
    assert "D2" in feedback and "R3" in feedback


def test_added_requirement_is_reported(monkeypatch) -> None:
    install(monkeypatch, _replies(
        fidelity=lambda text: json.dumps({
            "checks": [{"index": index, "result": "faithful", "reason": ""}
                       for index in _points_in(text)],
            "added": [{"requirement": "R7", "reason": "账本里没有依据"}]},
            ensure_ascii=False)))
    run_id, node_id = _confirm_run()

    result = drive(run_id, node_id, _never, idea=IDEA)

    assert result["reroute"]["targets"] == ["requirement"]
    assert _state(node_id)["verdict"] == "fail"
    items = _items(node_id)
    assert [item["kind"] for item in items] == ["added"]
    assert items[0]["requirement"] == "R7"
    assert "R7" in result["reroute"]["feedback"]


@pytest.mark.parametrize("severity,verdict", [("major", "fail"), ("minor", "pass")])
def test_quality_severity_controls_the_verdict(monkeypatch, severity, verdict) -> None:
    install(monkeypatch, _replies(quality=_quality(severity)))
    run_id, node_id = _confirm_run()

    result = drive(run_id, node_id, _never, idea=IDEA)

    assert _state(node_id)["verdict"] == verdict
    if verdict == "fail":
        assert result["reroute"]["targets"] == ["requirement"]
        assert [item["severity"] for item in _items(node_id)] == ["major"]
        assert "R1" in result["reroute"]["feedback"]
    else:
        assert result["done"] is True and result["verdict"] == "pass"
        assert _items(node_id) == []
        report = parse_front_matter(read_artifact(run_id, "review"))[1]
        assert "结论：通过。" in report


def test_partitioned_coverage_uses_each_checkers_own_points(monkeypatch) -> None:
    participants = [("architect", "架构师"), ("ux", "用户体验")]
    stances = {"architect": ["D1"], "ux": ["D2"]}
    install(monkeypatch, _replies(fidelity=_first_only))
    run_id, node_id = _confirm_run({"params": {"confirm": {"coverage": "partitioned"}}},
                                   participants=participants, stances=stances)

    result = drive(run_id, node_id, _never, idea=IDEA)

    assert result["done"] is True and result["verdict"] == "pass"
    # 同样的"只写一个点"的回复，在全员核对下不满足覆盖要求
    install(monkeypatch, _replies(fidelity=_first_only))
    run_id2, node_id2 = _confirm_run({"params": {"confirm": {"coverage": "full"}}},
                                     participants=participants, stances=stances)
    with pytest.raises(NodeFailure, match="模型输出不符合要求"):
        drive(run_id2, node_id2, _never, idea=IDEA)


def test_rounds_used_up_escalates_and_human_acceptance_passes(monkeypatch) -> None:
    install(monkeypatch, _replies(
        fidelity=lambda text: _checks(text, distorted=("D2",)),
        confirm_merge=lambda text: _merge({
            "kind": "distorted", "index": "D2", "requirement": "R3",
            "problem": "需求没有按 D2 的结论写", "suggestion": "按 D2 的结论改写 R3"})))
    run_id, node_id = _confirm_run()
    with session_scope() as s:
        node = s.get(Node, node_id)
        node.config_json = {**(node.config_json or {}), "round": 3, "max_rounds": 3}
    asked: list[str] = []

    def answer(checkpoint: dict) -> tuple:
        asked.append(checkpoint["kind"])
        return "approve", {}, ""

    result = drive(run_id, node_id, answer, idea=IDEA)

    assert asked == ["escalation"]
    assert result["done"] is True and result["verdict"] == "fail"
    assert result["accepted_by_user"] is True and result["precheck_ok"] is True
    assert _state(node_id)["precheck_ok"] is True
    report = parse_front_matter(read_artifact(run_id, "review"))[1]
    assert "结论：不通过。" in report and "接受了现状" in report


def test_user_rejection_becomes_a_checklist_item_and_reroutes(monkeypatch) -> None:
    install(monkeypatch, _replies())
    run_id, node_id = _confirm_run()

    first = drive(run_id, node_id, _never, idea=IDEA)

    assert first["done"] is True and first["verdict"] == "pass"
    assert parse_front_matter(read_artifact(run_id, "review"))[0]["version"] == 1

    with session_scope() as s:
        node = s.get(Node, node_id)
        node.config_json = with_feedback(node.config_json, "R1 的验收标准写得不清楚", "reject")

    install(monkeypatch, _replies())
    result = drive(run_id, node_id, _never, idea=IDEA)

    assert result["reroute"]["targets"] == ["requirement"]
    assert _state(node_id)["verdict"] == "fail"
    items = _items(node_id)
    assert len(items) == 1
    assert items[0]["origin"] == "user" and items[0]["kind"] == "quality"
    assert items[0]["requirement"] == "R1"
    assert "R1" in result["reroute"]["feedback"]
    # 每一轮各发一次轮次开始事件（用户驳回后进入的第二轮也要有）
    rounds = [e["payload"]["round"] for e in load_events(run_id)
              if e["type"] == "review_round_started"]
    assert rounds == [1, 2]


def test_reopen_branch_clears_stale_reroute_mark_and_last_report_fields() -> None:
    """用户带反馈重开：滞留的回流标记与旧报告的摘要、阻塞数一并清掉。"""
    run_id, node_id = _confirm_run()
    mtg = _confirm_session(run_id, node_id, phase="verdict", state={
        "pending_feedback": "R1 的验收标准写得不清楚",
        "rework_pending": True,
        "verdict": "fail", "summary": "需求确认会议完成：结论 不通过", "blocking": 3,
        "minutes_version": 1})

    asyncio.run(confirm_module.ConfirmProtocol().on_enter(mtg))

    state = store.get_state(mtg.meeting_id)
    assert state["intake_feedback"] == "R1 的验收标准写得不清楚"   # 反馈已移交给受理步
    assert state["rework_pending"] is False
    assert state["summary"] is None and state["blocking"] is None


def test_feedback_speech_is_not_repeated_after_a_crash(monkeypatch) -> None:
    """入口反馈分支的发言与状态移交不在同一事务：中断重入按轮次与内容去重，不重复发言。"""
    run_id, node_id = _confirm_run()
    mtg = _confirm_session(run_id, node_id, phase="verdict", state={
        "reopen_feedback": "R1 的验收标准写得不清楚", "feedback_kind": "reroute"})
    armed = {"on": True}
    original_update = store.update_meeting

    def flaky_update(meeting_id, **fields):
        if armed["on"] and fields.get("phase") == "intake":
            armed["on"] = False                        # 反馈发言刚落盘、状态还没移交：进程在这里被杀
            raise asyncio.CancelledError()
        return original_update(meeting_id, **fields)

    monkeypatch.setattr(store, "update_meeting", flaky_update)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(confirm_module.ConfirmProtocol().on_enter(mtg))
    monkeypatch.setattr(store, "update_meeting", original_update)

    asyncio.run(confirm_module.ConfirmProtocol().on_enter(mtg))
    turns = store.round_turns(mtg.meeting_id, round=2, kinds=["user_feedback"])
    assert [t["content"] for t in turns] == ["R1 的验收标准写得不清楚"]
    state = store.get_state(mtg.meeting_id)
    assert state["intake_feedback"] == "R1 的验收标准写得不清楚" and state["reopen_feedback"] == ""


def _land_turn(mtg, *, phase: str, kind: str, speaker: dict, content: str,
               structured: dict) -> None:
    """预先落一条完整发言：让步骤的 find_turn 复用它，不调模型。"""
    turn = store.add_turn(mtg.meeting_id, round=1, phase=phase, speaker_id=speaker["id"],
                          speaker_name=speaker["display_name"], kind=kind)
    store.finish_turn(turn, content=content, structured=structured)


def _chair(mtg) -> dict:
    store.add_participant(mtg.meeting_id, role_key="review_chair", display_name="评审主席",
                          mode="chair", system_prompt="你是评审主席")
    return mtg.participants("chair")[0]


def test_intake_accounting_is_idempotent_by_signature(monkeypatch) -> None:
    """受理入账按（轮次、出处、需求、问题）签名去重：重跑入账不会以新编号重复入账。"""
    install(monkeypatch, _replies())
    run_id, node_id = _confirm_run()
    seeded = {"id": "C1", "kind": "quality", "requirement": "R1", "index": "",
              "severity": "major", "problem": "验收标准不可核对", "suggestion": "",
              "origin": "user", "round": 1}
    mtg = _confirm_session(run_id, node_id, phase="intake", state={
        "intake_feedback": "R1 的验收标准写得不清楚", "items": [seeded]})
    _land_turn(mtg, phase="intake", kind="confirm_intake", speaker=_chair(mtg),
               content="整理条目", structured={"items": [
                   {"kind": "quality", "requirement": "R1", "severity": "major",
                    "problem": "验收标准不可核对", "suggestion": ""}]})
    protocol = confirm_module.ConfirmProtocol()

    asyncio.run(protocol.step_intake(mtg))

    items = store.get_state(mtg.meeting_id)["items"]
    assert [item["id"] for item in items] == ["C1"]


def test_merge_accounting_is_idempotent_by_signature(monkeypatch) -> None:
    """汇总入账按（轮次、出处、需求、问题）签名去重：重跑入账沿用已入账的编号，不重复入账。"""
    install(monkeypatch, _replies())
    run_id, node_id = _confirm_run()
    seeded = {"id": "C1", "kind": "distorted", "requirement": "R1", "index": "D1",
              "severity": "blocker", "problem": "范围与账本不一致", "suggestion": "",
              "origin": "program", "round": 1}
    mtg = _confirm_session(run_id, node_id, phase="merge", state={
        "fidelity": [{"index": "D1", "result": "distorted",
                      "reason": "范围与账本不一致", "checker": "核对者"}],
        "items": [dict(seeded)]})
    _land_turn(mtg, phase="merge", kind="confirm_merge", speaker=_chair(mtg),
               content="归类", structured={"items": [{"index": "D1", "requirement": "R1"}]})
    protocol = confirm_module.ConfirmProtocol()

    asyncio.run(protocol.step_merge(mtg))

    items = store.get_state(mtg.meeting_id)["items"]
    assert [item["id"] for item in items] == ["C1"]
    assert items[0]["requirement"] == "R1"             # 归类的补充照常生效


def test_reroute_self_feedback_is_recorded_as_review_voice() -> None:
    """回流写给下一轮的自我提示也是"评审意见"的发言，不标成用户。"""
    run_id, node_id = _confirm_run()
    mtg = _confirm_session(run_id, node_id, phase="verdict", state={
        "reopen_feedback": "先把 R2 的度量口径写清楚", "feedback_kind": "reroute_self"})

    asyncio.run(confirm_module.ConfirmProtocol().on_enter(mtg))

    turns = store.round_turns(mtg.meeting_id, round=2, kinds=["user_feedback"])
    assert [t["speaker_name"] for t in turns] == ["评审意见"]


def test_quality_reviewer_fallback_applies_min_and_max(monkeypatch) -> None:
    """无评审会议时的质量评审者兜底：与评审协议同样应用 reviewers_max 与 reviewers_min。"""
    install(monkeypatch, _replies())
    run_id, node_id = _confirm_run(with_review=False)
    base = [{"mode": "participant", "role_key": "ux", "display_name": "用户体验",
             "system_prompt": "p", "model_id": None, "id": 1},
            {"mode": "participant", "role_key": "arch", "display_name": "架构师",
             "system_prompt": "p", "model_id": None, "id": 2}]
    protocol = confirm_module.ConfirmProtocol()

    with session_scope() as s:
        node = s.get(Node, node_id)
        node.config_json = {**(node.config_json or {}), "params": {"reviewers_max": 1}}
    mtg = _confirm_session(run_id, node_id, phase="prepare", state={})
    picked = protocol._quality_sources(mtg, base)
    assert [row["display_name"] for row in picked] == ["用户体验"]

    with session_scope() as s:
        node = s.get(Node, node_id)
        node.config_json = {**(node.config_json or {}),
                            "params": {"reviewers_min": 3}}
    mtg = _confirm_session(run_id, node_id, phase="prepare", state={})
    with pytest.raises(NodeFailure, match="评审者人数不足"):
        protocol._quality_sources(mtg, base)


def test_rework_branch_clears_last_report_fields() -> None:
    """从等待返工恢复：回流标记照旧清掉，旧报告的摘要与阻塞数也一并清掉。"""
    run_id, node_id = _confirm_run()
    mtg = _confirm_session(run_id, node_id, phase="verdict", state={
        "rework_pending": True,
        "verdict": "fail", "summary": "需求确认会议完成：结论 不通过", "blocking": 3,
        "minutes_version": 1})

    asyncio.run(confirm_module.ConfirmProtocol().on_enter(mtg))

    state = store.get_state(mtg.meeting_id)
    assert state["rework_pending"] is False
    assert state["summary"] is None and state["blocking"] is None
