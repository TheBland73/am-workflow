"""需求确认协议：程序核对、写预期、忠实性核对、质量评审、汇总裁决、回流与增量复核。"""
from __future__ import annotations

import asyncio
import json
import re
from collections import Counter

import pytest

from app.artifacts.store import parse_front_matter, read_artifact, write_artifact
from app.db.base import init_db, session_scope
from app.db.models import Node, Run, Workflow
from app.engine.feedback import with_feedback
from app.engine.orchestrator import orchestrator
from app.engine.params import apply_run_config
from app.engine.runners import NodeFailure
from app.engine.templates import instantiate_nodes, seed_defaults, workflow_definition
from app.meeting import store
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
                 stances: dict[str, list[str]] | None = None) -> tuple[str, int]:
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
    review_meeting = store.create_meeting(run_id=run_id, node_id=review_id, node_key="review",
                                          protocol="review", charter={"sections": []}, config={})
    store.update_meeting(review_meeting, status="completed", phase="done", round=1)
    store.add_participant(review_meeting, role_key="architect", display_name="架构师",
                          mode="reviewer", system_prompt="你是评审者")
    return run_id, confirm_id


def _never(checkpoint: dict) -> tuple:
    raise AssertionError(f"不该出现人工确认点：{checkpoint['title']}")


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
    assert _items(node_id) == []
    report = parse_front_matter(read_artifact(run_id, "review"))[1]
    assert "结论：通过。" in report and "全部通过" in report
    kinds = Counter(call["kind"] for call in seen)
    assert kinds["expect"] == 1 and kinds["fidelity"] == 1 and kinds["quality"] == 1
    assert kinds["confirm_intake"] == 0 and kinds["confirm_merge"] == 0


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
