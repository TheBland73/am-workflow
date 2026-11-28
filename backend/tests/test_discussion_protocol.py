"""讨论协议：人工确认点、程序结算、停滞退让、主持人裁决、复议、复核、断点续跑。"""
from __future__ import annotations

import asyncio
import json
import re
from collections import Counter

import pytest
from fastapi.testclient import TestClient

from app.api.runs import run_metrics
from app.artifacts.store import parse_front_matter, read_artifact, write_artifact
from app.db.base import init_db, session_scope
from app.db.models import AgentTemplate, Gate, Node, Run, Workflow
from app.engine.events import load_events
from app.engine.feedback import with_feedback
from app.engine.gates import decide_gate, get_gate, request_gate
from app.engine.materials import save_material
from app.engine.params import apply_run_config
from app.engine.runners import NodeContext, NodeFailure
from app.engine.templates import instantiate_nodes, seed_defaults, workflow_definition
from app.main import app
from app.meeting import store
from app.meeting.protocols import discussion as discussion_module
from app.meeting.session import MeetingSession
from tests.meeting_fakes import drive, install

IDEA = "给小团队做一个反馈收集工具"
_counter = iter(range(1, 1000))

BRIEF = ('{"brief": "目标：让小团队收集并整理反馈。用户：团队负责人。约束：两周内可用。'
         '假设：单机部署。", "questions": ["需要导出哪些格式？"]}')
ANSWER = '{"answers": [{"text": "需要导出周报", "source": ""}]}'
ROSTER = ('{"participants": [{"role_key": "architect", "reason": "把握技术路线"}, '
          '{"role_key": "ux", "reason": "把关核心任务路径"}]}')
ROSTER_ONE = '{"participants": [{"role_key": "architect", "reason": "唯一参会者"}]}'
ROSTER_SECURITY = ('{"participants": [{"role_key": "security", "reason": "把关授权与数据边界"}, '
                   '{"role_key": "ux", "reason": "把关核心任务路径"}]}')
POSITION = '{"must": ["每一步可验证"], "risks": ["范围蔓延"], "route": "先做最小闭环", "questions": []}'
AGENDA = ('{"points": ['
          '{"title": "首版做网页版还是桌面版", "question": "首版交付哪个平台", '
          '"options": ["网页版", "桌面版"], "raised_by": "架构师"}, '
          '{"title": "首版是否需要登录", "question": "多人共用时怎么区分反馈", '
          '"options": ["需要", "不需要"], "raised_by": "用户体验"}]}')
ARCHITECT = ('{"stances": ['
             '{"index": "D1", "level": 1, "reason": "网页版部署最简单", "choice": "网页版"}, '
             '{"index": "D2", "level": 5, "reason": "首版只有本机使用，登录是多余的门槛", "choice": "不需要"}, '
             '{"index": "D3", "level": 1, "reason": "外勤场景需要离线填写", "choice": "支持"}]}')
UX = ('{"stances": ['
      '{"index": "D1", "level": 2, "reason": "网页版够用，但手机上也要能填", "choice": "网页版"}, '
      '{"index": "D2", "level": 1, "reason": "多人共用必须区分是谁的反馈", "choice": "需要"}, '
      '{"index": "D3", "level": 2, "reason": "离线填写之后要能同步", "choice": "支持"}]}')
ARCHITECT_YIELDS = ARCHITECT.replace(
    '"level": 5, "reason": "首版只有本机使用，登录是多余的门槛", "choice": "不需要"',
    '"level": 1, "reason": "被体验说服：多人场景确实需要区分提交人", "choice": "需要"')
SUMMARY = ('{"summaries": [{"index": "D2", "summary": "架构主张首版不做登录，体验坚持需要；'
           '双方都关心多人共用的场景。"}]}')
RULING = ('{"rulings": [{"index": "D2", "conclusion": "需要", "rationale": "多人共用是核心场景", '
          '"dissent": "架构师：首版本机使用，登录是多余的门槛"}]}')
REOPEN = ('{"points": [{"index": "D2", "title": "", "question": "", "options": []}, '
          '{"index": "", "title": "是否支持离线填写", "question": "断网时能否先记下来", "options": ["支持", "不支持"]}], '
          '"note": "评审认为登录方案与离线场景都需要复议"}')
CHECK_OK = '{"ok": true, "issues": []}'
CHECK_ISSUE = ('{"ok": false, "issues": [{"index": "D1", "problem": "方案没有交代手机上填写的要求"}]}')
PROPOSAL = (
    "# 建设方案\n\n## 目标与成功标准\n\n两周内让小团队用上反馈收集：能收集、能整理、能导出。\n\n"
    "## 范围\n\n必做：收集、整理、导出周报。不做：实时协作、细粒度权限。\n\n"
    "## 总体方案\n\nD1：首版做网页版，部署最简单。D2：首版是否登录尚有分歧。\n\n"
    "## 关键流程\n\n提交反馈 → 整理 → 导出。\n\n## 里程碑与验收\n\n第一周：收集与导出可用。\n\n"
    "## 风险与缓解\n\n范围蔓延：首版功能冻结。\n")
PROPOSAL_V2 = "## 修改说明\n\n- D2：按复议结论改为首版需要登录。\n\n" + PROPOSAL

BRIEF_NOQ = '{"brief": "目标：让小团队收集并整理反馈。", "questions": []}'
BRIEF_TWOQ = ('{"brief": "目标：让小团队收集并整理反馈。", '
              '"questions": ["需要导出哪些格式？", "谁来维护导出模板？"]}')
ANSWER_TWOQ = ('{"answers": [{"text": "需要导出周报", "source": "简报：两周内可用"}, '
               '{"text": "导出模板由团队自行维护", "source": ""}]}')
ARCHITECT_NEW = ('{"stances": ['
                 '{"index": "D1", "level": 1, "reason": "网页版部署最简单", "choice": "网页版"}, '
                 '{"index": "D2", "level": 5, "reason": "首版只有本机使用，登录是多余的门槛", "choice": "不需要"}], '
                 '"new_points": [{"title": "是否支持离线填写", "question": "断网时能否先记下来", '
                 '"options": ["支持", "不支持"], "level": 5, '
                 '"reason": "外勤场景必须离线可用，替代建议是本地缓存"}]}')
UX_NO_D3 = ('{"stances": ['
            '{"index": "D1", "level": 2, "reason": "网页版够用，但手机上也要能填", "choice": "网页版"}, '
            '{"index": "D2", "level": 1, "reason": "多人共用必须区分是谁的反馈", "choice": "需要"}]}')
SUMMARY_TWO = ('{"summaries": [{"index": "D2", "summary": "架构主张首版不做登录，体验坚持需要。"}, '
               '{"index": "D3", "summary": "架构师认为外勤必须离线可用，体验没有表态。"}]}')
REOPEN_WITH_D99 = ('{"points": [{"index": "D2", "title": "", "question": "", "options": []}, '
                   '{"index": "D99", "title": "账本里没有的点", "question": "", "options": []}], '
                   '"note": "评审认为登录方案需要复议"}')
ARCHITECT_RESTANCE = ('{"stances": ['
                      '{"index": "D2", "level": 1, "reason": "被说服，首版做最简登录", "choice": "需要"}, '
                      '{"index": "D3", "level": 5, "reason": "外勤必须离线可用"}]}')
ROSTER_TEMP = ('{"participants": [{"role_key": "", "display_name": "运维顾问", '
               '"persona": "你关心部署与回滚，升级前必须能一键回滚", "reason": "补部署视角"}]}')


def _open_from_ledger(text: str) -> list[str]:
    """从提示里的账本表格解析出仍未了结的决策点编号（讨论中/复议中）。"""
    return [index for index, _status in
            re.findall(r"^\| (D\d+) \| (讨论中|复议中) ", text, re.M)]


def _summary_by_ledger(text: str) -> str:
    """交锋摘要按当前未决点精确覆盖（校验器要求恰好覆盖）。"""
    rows = [{"index": index, "summary": f"{index} 的分歧摘要"}
            for index in _open_from_ledger(text)]
    return json.dumps({"summaries": rows}, ensure_ascii=False)


def _split(text: str) -> str:
    return ARCHITECT if "架构视角" in text else UX


def _agree(text: str) -> str:
    return ARCHITECT_YIELDS if "架构视角" in text else UX


def _by_round(*replies):
    """按轮次给出回复；用完以后一直用最后一个。"""
    queue = list(replies)

    def reply(text: str) -> str:
        current = queue.pop(0) if len(queue) > 1 else queue[0]
        return current(text)

    return reply


def _first_then(first: str, rest: str):
    calls: list[int] = []

    def reply(text: str) -> str:
        calls.append(1)
        return first if len(calls) == 1 else rest

    return reply


def _restance_two_rounds():
    """第一轮架构师坚持，第二轮改变；体验始终不变。

    并行发言的完成顺序不固定，所以按人设文本区分是谁，而不是按调用次序。
    """
    architect_calls = 0

    def reply(text: str) -> str:
        nonlocal architect_calls
        if "架构视角" in text:
            architect_calls += 1
            return ARCHITECT if architect_calls == 1 else ARCHITECT_YIELDS
        return UX

    return reply


def _replies(**overrides) -> dict:
    """一套完整的脚本回复；按需覆盖其中几项。"""
    base = {
        "brief": BRIEF, "answer": ANSWER, "roster": ROSTER, "position": POSITION, "agenda": AGENDA,
        "stance": _split,
        "claim": "我支持首版不做登录：首版只有本机使用，登录是多余的门槛。",
        "challenge": "多人共用时无法区分反馈是谁提交的，后续跟进会乱。",
        "respond": "多人场景确实需要区分提交人，我改变立场：首版做最简登录。",
        "exchange_summary": SUMMARY, "restance": _restance_two_rounds(), "ruling": RULING,
        "proposal": _first_then(PROPOSAL, PROPOSAL_V2), "proposal_check": CHECK_OK,
        "proposal_revise": PROPOSAL_V2, "reopen": REOPEN,
    }
    base.update(overrides)
    return base


def _meeting_run(config: dict | None = None) -> tuple[str, int]:
    """按模板建一个运行，返回 (运行 id, 讨论会议节点的 id)。生效参数已写进节点。

    本文件的脚本只准备了两名参会者，所以默认把讨论的人数范围固定为 2 人；
    模板默认的 3–5 人由协议按参数表校验，另有用例覆盖。
    """
    config = dict(config or {})
    params = dict(config.get("params") or {})
    discussion = dict(params.get("discussion") or {})
    discussion.setdefault("participants_min", 2)
    discussion.setdefault("participants_max", 2)
    params["discussion"] = discussion
    config["params"] = params
    init_db()
    seed_defaults()
    run_id = f"discuss-run-{next(_counter)}"
    with session_scope() as s:
        workflow_id = s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").one().id
        s.add(Run(id=run_id, workflow_id=workflow_id, status="running", idea_text=IDEA,
                  config_json=config or {}))
        s.flush()
    definition = workflow_definition("sdlc_v1")
    instantiate_nodes(run_id, definition)
    apply_run_config(run_id, definition, config or {})
    write_artifact(run_id, "idea", "# 想法\n\n" + IDEA, created_by="user", inputs=[], status="approved")
    with session_scope() as s:
        return run_id, s.query(Node).filter(Node.run_id == run_id, Node.node_key == "discuss").one().id


def _approve(checkpoint: dict) -> tuple:
    return "approve", {}, ""


def _never(checkpoint: dict) -> tuple:
    raise AssertionError(f"全自动模式下不该出现人工确认点：{checkpoint['title']}")


def _ledger(node_id: int) -> dict[str, dict]:
    meeting_id = store.latest_meeting_for_node(node_id)["id"]
    return {point["index"]: point for point in store.list_decision_points(meeting_id)}


def _charter(node_id: int) -> dict[str, str]:
    charter = store.latest_meeting_for_node(node_id)["charter"]
    return {section["title"]: section["text"] for section in charter["sections"]}


def _proposal(run_id: str) -> tuple[dict, str]:
    meta, body = parse_front_matter(read_artifact(run_id, "proposal"))
    return meta, body


def test_meeting_reaches_a_proposal_with_two_human_checkpoints(monkeypatch) -> None:
    """默认等级：澄清和名单由人确认；共识由程序按闭合规则判定，不打扰人。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    run_id, node_id = _meeting_run()
    asked: list[str] = []

    def answer(checkpoint: dict) -> tuple:
        asked.append(checkpoint["kind"])
        data = {"answers": ["Markdown 即可"]} if checkpoint["kind"] == "clarify" else {}
        return "approve", data, ""

    result = drive(run_id, node_id, answer, idea=IDEA)

    assert result["done"] is True
    assert (result["decision_points"], result["deferred"], result["rounds"]) == (2, 0, 2)
    assert asked == ["clarify", "roster"]
    ledger = _ledger(node_id)
    assert {index: point["status"] for index, point in ledger.items()} == {"D1": "agreed", "D2": "agreed"}
    assert ledger["D1"]["conclusion"] == "网页版" and ledger["D2"]["conclusion"] == "需要"
    assert ledger["D2"]["raised_by"] == "用户体验"
    architect = next(s for s in ledger["D2"]["stances"].values() if s["name"] == "架构师")
    assert architect["level"] == 1 and [item["level"] for item in architect["history"]] == [5]
    assert "用户回答" in _charter(node_id)["已澄清的问题"]
    assert "Markdown 即可" in _charter(node_id)["已澄清的问题"]
    meta, body = _proposal(run_id)
    assert meta["version"] == 1 and "## 待确认问题" in body and "## 决策点索引" in body
    assert body.index("## 待确认问题") < body.index("## 决策点索引")   # 详设 §7.8 的节顺序
    assert "## 决策点账本" in parse_front_matter(read_artifact(run_id, "discussion"))[1]
    kinds = Counter(call["kind"] for call in seen)
    assert kinds["position"] == 2 and kinds["claim"] == 2 and kinds["exchange_summary"] == 2
    types = [event["type"] for event in load_events(run_id)]
    assert "meeting_ledger_updated" in types and "meeting_turn_finished" in types
    assert "context_prefix_changed" not in types        # 账本的正常更新不该报冻结段被改


def test_minutes_endpoint_returns_the_minutes_this_meeting_wrote(monkeypatch) -> None:
    """纪要接口只认会议自己写的那一版：协议写纪要时把版本记进会议状态。"""
    install(monkeypatch, _replies(restance=_split))
    run_id, node_id = _meeting_run({"automation": {"level": "L3"}})

    result = drive(run_id, node_id, _never, idea=IDEA)

    assert result["done"] is True
    meeting_id = store.latest_meeting_for_node(node_id)["id"]
    with TestClient(app) as client:
        response = client.get(f"/api/runs/{run_id}/meetings/{meeting_id}/minutes")

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["kind"] == "discussion"
    assert data["version"] == result["minutes"]["version"]
    assert "## 决策点账本" in data["body"]


def test_full_automation_falls_back_at_a_stall_without_asking_anyone(monkeypatch) -> None:
    """仅兜底的等级：需求方代理答澄清，名单自动生效，停滞后先按“无反对即可”了结，其余待定。"""
    install(monkeypatch, _replies(restance=_split))
    run_id, node_id = _meeting_run({"automation": {"level": "L3"}})

    result = drive(run_id, node_id, _never, idea=IDEA)

    assert result["done"] is True and result["deferred"] == 1 and result["rounds"] == 2
    ledger = _ledger(node_id)
    assert ledger["D1"]["status"] == "agreed" and ledger["D2"]["status"] == "deferred"
    _meta, body = _proposal(run_id)
    assert "## 待确认问题" in body and "D2 首版是否需要登录" in body
    answered = _charter(node_id)["已澄清的问题"]
    assert "需要导出周报" in answered and "按假设处理" in answered      # 没有出处的回答降级为假设
    with session_scope() as s:
        gates = {(g.kind, g.decided_by) for g in s.query(Gate).filter(Gate.run_id == run_id).all()}
    assert gates == {("clarify", "proxy"), ("roster", "program"), ("consensus", "program")}


def test_human_judges_consensus_each_round_and_can_send_it_back(monkeypatch) -> None:
    """全程人审：每轮结束都暂停由人判；人可以给指导语让它再讨论一轮。"""
    install(monkeypatch, _replies(restance=_split))
    run_id, node_id = _meeting_run({"automation": {"level": "L1"}})
    asked: list[dict] = []

    def answer(checkpoint: dict) -> tuple:
        asked.append(checkpoint)
        if checkpoint["kind"] != "consensus":
            return "approve", {}, ""
        if len(asked) == 3:                                    # 第一次到共识确认点
            return "reject", {"option": "continue"}, "请重点讨论多人共用的场景"
        return "approve", {"option": "integrate"}, ""

    result = drive(run_id, node_id, answer, idea=IDEA)

    assert [item["kind"] for item in asked] == ["clarify", "roster", "consensus", "consensus"]
    assert asked[2]["payload"]["reason"] == "round_end"
    assert [item["index"] for item in asked[2]["payload"]["open"]] == ["D2"]
    assert "带着分歧整合" in asked[2]["description"]
    assert result["done"] is True and result["deferred"] == 1 and result["rounds"] == 2
    meeting_id = store.latest_meeting_for_node(node_id)["id"]
    guidance = [t for t in store.list_turns(meeting_id) if t["kind"] == "user_guidance"]
    assert [t["content"] for t in guidance] == ["请重点讨论多人共用的场景"]


def test_delegating_lets_the_facilitator_rule_and_keeps_dissent(monkeypatch) -> None:
    install(monkeypatch, _replies(restance=_split))
    run_id, node_id = _meeting_run({"automation": {"level": "L1"}})

    def answer(checkpoint: dict) -> tuple:
        data = {"option": "delegate"} if checkpoint["kind"] == "consensus" else {}
        return "approve", data, ""

    drive(run_id, node_id, answer, idea=IDEA)

    point = _ledger(node_id)["D2"]
    assert point["status"] == "ruled" and point["conclusion"] == "需要"
    assert "多人共用是核心场景" in point["rationale"] and "保留的异议：架构师" in point["rationale"]


def test_facilitator_rules_closure_rules_once_exchanges_are_used_up(monkeypatch) -> None:
    """闭合规则是主持人裁决：交锋跨轮累计，第 2 轮重新表态仍未了结的才交主持人裁决。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(restance=lambda text: ARCHITECT), seen)
    run_id, node_id = _meeting_run({"params": {"discussion": {"closure_rule": "facilitator_rules"}}})

    result = drive(run_id, node_id, _approve, idea=IDEA)

    assert result["done"] is True and _ledger(node_id)["D2"]["status"] == "ruled"
    assert result["rounds"] == 2
    kinds = Counter(call["kind"] for call in seen)
    assert kinds["restance"] == 4 and kinds["claim"] == 2 and kinds["ruling"] == 1
    assert "主持人裁决" in _charter(node_id)["闭合规则"]


def test_a_single_participant_skips_stances_and_debate(monkeypatch) -> None:
    seen: list[dict] = []
    install(monkeypatch, _replies(roster=ROSTER_ONE), seen)
    run_id, node_id = _meeting_run({"rosters": {"participants": {"count": 1}}})

    result = drive(run_id, node_id, _approve, idea=IDEA)

    kinds = Counter(call["kind"] for call in seen)
    assert result["done"] is True and result["rounds"] == 1
    assert kinds["position"] == 1 and "stance" not in kinds and "claim" not in kinds
    assert {point["status"] for point in _ledger(node_id).values()} == {"agreed"}
    # 立场即共识：账本结论写他自己的立场，不是占位文案
    assert "先做最小闭环" in _ledger(node_id)["D1"]["conclusion"]


def test_a_fixed_headcount_is_enforced(monkeypatch) -> None:
    install(monkeypatch, _replies())                         # 主持人提了 2 名，用户要的是 3 名
    run_id, node_id = _meeting_run({"rosters": {"participants": {"count": 3}}})
    with pytest.raises(NodeFailure, match="固定人数"):
        drive(run_id, node_id, _approve, idea=IDEA)


def _participants(meeting_id: int) -> list[tuple[str | None, str]]:
    """这场会议里参会者的 (角色键, 状态)，含已撤掉的；主持人、需求方代理不算。"""
    rows = store.list_participants(meeting_id, active_only=False)
    return [(row["role_key"], row["status"]) for row in rows if row["mode"] == "participant"]


def test_participants_named_in_the_run_config_take_effect_without_a_proposal_or_a_checkpoint(
        monkeypatch) -> None:
    """运行级配置只点名角色：按用户的来。主持人不再挑名单，也不开名单确认点，会议照常走完。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    run_id, node_id = _meeting_run({"rosters": {"participants": {"role_keys": ["ux", "architect"]}}})
    asked: list[str] = []

    def answer(checkpoint: dict) -> tuple:
        asked.append(checkpoint["kind"])
        data = {"answers": ["Markdown 即可"]} if checkpoint["kind"] == "clarify" else {}
        return "approve", data, ""

    result = drive(run_id, node_id, answer, idea=IDEA)

    assert result["done"] is True and asked == ["clarify"]         # 名单没有拿来问人
    assert "roster" not in {call["kind"] for call in seen}         # 主持人没有被叫去挑名单
    meeting_id = store.latest_meeting_for_node(node_id)["id"]
    rows = [row for row in store.list_participants(meeting_id) if row["mode"] == "participant"]
    assert [(row["role_key"], row["display_name"], row["source"]) for row in rows] == [
        ("ux", "用户体验", "user"), ("architect", "架构师", "user")]  # 按配置里的顺序，名称取自角色库
    assert {index: point["status"] for index, point in _ledger(node_id).items()} == {
        "D1": "agreed", "D2": "agreed"}


def test_named_participants_that_no_longer_exist_fail_the_meeting_with_a_clear_message(
        monkeypatch) -> None:
    """点名的角色缺任何一个：带着明确的错误失败并列出缺的是哪个，不悄悄去掉剩下照开。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    run_id, node_id = _meeting_run({
        "rosters": {"participants": {"role_keys": ["architect", "deleted_role"]}}})

    with pytest.raises(NodeFailure) as excinfo:
        drive(run_id, node_id, _approve, idea=IDEA)
    assert "已不存在：deleted_role" in str(excinfo.value)
    assert excinfo.value.fatal is True                              # 重试没有意义
    assert "roster" not in {call["kind"] for call in seen}          # 没有退回去让主持人另挑一份


def test_a_named_role_that_is_not_meant_for_discussion_fails_the_meeting(monkeypatch) -> None:
    """点名了不适合参加讨论的角色（裁决模型）：会议带明确的错误失败。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    run_id, node_id = _meeting_run({"rosters": {"participants": {"role_keys": ["judge"]}}})

    with pytest.raises(NodeFailure) as excinfo:
        drive(run_id, node_id, _approve, idea=IDEA)
    assert "不适合参加讨论" in str(excinfo.value) and "judge" in str(excinfo.value)
    assert excinfo.value.fatal is True


def test_a_custom_role_without_meeting_roles_can_be_named(monkeypatch) -> None:
    """没填"适合的会议角色"的自定义角色不受限：点名它可以照常开会；用完把角色行删掉。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    with session_scope() as s:
        s.add(AgentTemplate(role_key="consultant_free", name="自由顾问",
                            system_prompt="你是自由顾问，从价值角度看这个想法",
                            meta_json={"description": "外部顾问，没有限定会议类型"}))
    run_id, node_id = _meeting_run({
        "rosters": {"participants": {"role_keys": ["consultant_free"]}}})
    try:
        result = drive(run_id, node_id, _approve, idea=IDEA)
        assert result["done"] is True
        meeting_id = store.latest_meeting_for_node(node_id)["id"]
        rows = [row for row in store.list_participants(meeting_id) if row["mode"] == "participant"]
        assert [(row["role_key"], row["display_name"]) for row in rows] == [
            ("consultant_free", "自由顾问")]
    finally:
        with session_scope() as s:
            row = s.query(AgentTemplate).filter(AgentTemplate.role_key == "consultant_free").one()
            s.delete(row)


def test_a_fixed_headcount_overrides_the_participant_range_in_the_prompt_and_at_the_checkpoint(
        monkeypatch) -> None:
    """只给固定人数：主持人被要求正好挑这么多（参数表的人数范围让位），确认点上也写明人数是用户定的。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(roster=ROSTER_ONE), seen)
    run_id, node_id = _meeting_run({"rosters": {"participants": {"count": 1}}})
    asked: list[dict] = []

    def answer(checkpoint: dict) -> tuple:
        asked.append(checkpoint)
        return "approve", {}, ""

    result = drive(run_id, node_id, answer, idea=IDEA)

    assert result["done"] is True
    (prompt,) = [call["text"] for call in seen if call["kind"] == "roster"]
    assert "请正好挑选 1 名" in prompt and "2 到 2 名" not in prompt   # 参数表的范围是 2 到 2
    checkpoint = next(item for item in asked if item["kind"] == "roster")
    assert checkpoint["payload"]["fixed"] == 1
    assert (checkpoint["payload"]["min"], checkpoint["payload"]["max"]) == (1, 1)
    assert "用户指定了固定人数。" in checkpoint["description"]


def test_a_rejected_roster_is_proposed_again_with_the_feedback_and_the_old_one_withdrawn(
        monkeypatch) -> None:
    """人在名单确认点上驳回并写明意见：旧名单撤掉，主持人带着这份意见重新提议，再请人确认新名单。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(roster=_first_then(ROSTER, ROSTER_SECURITY)), seen)
    run_id, node_id = _meeting_run()
    asked: list[str] = []

    def answer(checkpoint: dict) -> tuple:
        asked.append(checkpoint["kind"])
        assert asked.count("roster") <= 2, "名单确认点反复出现"     # drive 会一直等到会议结束
        if checkpoint["kind"] == "roster" and asked.count("roster") == 1:
            return "reject", {}, "换掉架构师，让安全与合规参加"     # 只驳回第一份名单
        return "approve", {}, ""

    result = drive(run_id, node_id, answer, idea=IDEA)

    assert result["done"] is True and asked.count("roster") == 2
    first, second = [call["text"] for call in seen if call["kind"] == "roster"]
    assert "用户对上一份名单的意见" not in first
    assert "换掉架构师，让安全与合规参加" in second
    meeting_id = store.latest_meeting_for_node(node_id)["id"]
    assert _participants(meeting_id) == [("architect", "removed"), ("ux", "removed"),
                                         ("security", "active"), ("ux", "active")]


def test_a_fixed_headcount_still_binds_the_roster_proposed_after_a_rejection(monkeypatch) -> None:
    """驳回后重新提议，人数要求不变：仍然必须正好是用户指定的名数。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(roster=_first_then(ROSTER_ONE, ROSTER)), seen)   # 第二次提了 2 名
    run_id, node_id = _meeting_run({"rosters": {"participants": {"count": 1}}})
    asked: list[str] = []

    def answer(checkpoint: dict) -> tuple:
        asked.append(checkpoint["kind"])
        assert asked.count("roster") <= 1, "名单确认点反复出现"
        if checkpoint["kind"] == "roster":
            return "reject", {}, "换一个人"
        return "approve", {}, ""

    with pytest.raises(NodeFailure, match="固定人数"):
        drive(run_id, node_id, answer, idea=IDEA)
    _first, second = [call["text"] for call in seen if call["kind"] == "roster"]
    assert "请正好挑选 1 名" in second and "换一个人" in second   # 重新提议时仍按固定人数，并带着意见


def test_feedback_reopens_points_and_produces_a_new_version(monkeypatch) -> None:
    seen: list[dict] = []
    install(monkeypatch, _replies(restance=_by_round(_agree, _agree)), seen)
    run_id, node_id = _meeting_run()
    drive(run_id, node_id, _approve, idea=IDEA)
    first = store.latest_meeting_for_node(node_id)
    with session_scope() as s:
        node = s.get(Node, node_id)
        node.config_json = with_feedback(node.config_json, "评审认为登录方案需要复议", "reroute")

    result = drive(run_id, node_id, _approve, idea=IDEA)

    assert result["done"] is True and store.latest_meeting_for_node(node_id)["id"] == first["id"]
    ledger = _ledger(node_id)
    assert ledger["D2"]["status"] == "agreed" and ledger["D2"]["closed_round"] == 2
    assert ledger["D3"]["raised_by"] == "评审意见"            # 反馈带出了账本里没有的问题
    meta, body = _proposal(run_id)
    assert meta["version"] == 2 and "## 修改说明" in body
    prompts = [call["text"] for call in seen if call["kind"] in ("proposal", "proposal_revise")]
    kinds = [call["kind"] for call in seen if call["kind"] in ("proposal", "proposal_revise")]
    assert kinds == ["proposal", "proposal_revise"]               # 修订用单独的调用标识
    assert len(prompts) == 2 and "上一版建设方案" in prompts[1]
    notes = [t for t in store.list_turns(first["id"]) if t["kind"] == "user_feedback"]
    assert [t["content"] for t in notes] == ["评审认为登录方案需要复议"]


def test_proposal_gate_lists_pending_points_from_the_ledger(monkeypatch) -> None:
    """待确认的问题由账本给出：方案确认门的卡片上直接看得到。"""
    install(monkeypatch, _replies(restance=_split))
    run_id, node_id = _meeting_run({"automation": {"level": "L3"}})
    drive(run_id, node_id, _never, idea=IDEA)
    with session_scope() as s:
        gate_node = s.query(Node).filter(Node.run_id == run_id,
                                         Node.node_key == "gate_proposal").one().id

    gate = request_gate(run_id, gate_node)

    assert "待确认问题（需要你决定）" in gate["description"]
    assert "D2 首版是否需要登录" in gate["description"]
    assert get_gate(gate["id"])["description"] == gate["description"]     # 冷读不重复追加


def test_resuming_does_not_repeat_finished_speech(monkeypatch) -> None:
    def crash(text: str) -> str:
        raise RuntimeError("模拟进程被杀")

    install(monkeypatch, _replies(restance=crash))
    run_id, node_id = _meeting_run()
    with pytest.raises(RuntimeError):
        drive(run_id, node_id, _approve, idea=IDEA)

    seen: list[dict] = []
    install(monkeypatch, _replies(restance=_by_round(_agree, _agree)), seen)
    drive(run_id, node_id, _approve, idea=IDEA)

    kinds = Counter(call["kind"] for call in seen)
    for finished in ("position", "agenda", "stance", "claim", "respond", "exchange_summary"):
        assert finished not in kinds
    assert kinds["restance"] == 2                            # 只重做了被中断的那一步


def test_check_flags_are_fixed_then_rechecked_only_where_flagged(monkeypatch) -> None:
    checks = iter([CHECK_ISSUE, CHECK_OK, CHECK_OK])
    seen: list[dict] = []
    install(monkeypatch, _replies(proposal_check=lambda text: next(checks)), seen)
    run_id, node_id = _meeting_run()

    result = drive(run_id, node_id, _approve, idea=IDEA)

    assert result["done"] is True
    kinds = Counter(call["kind"] for call in seen)
    assert kinds["proposal_check"] == 3 and kinds["proposal_revise"] == 1
    meta, body = _proposal(run_id)
    assert meta["version"] == 2 and "## 修改说明" in body


def test_unresolved_check_notes_are_recorded_instead_of_looping_forever(monkeypatch) -> None:
    install(monkeypatch, _replies(proposal_check=CHECK_ISSUE))
    run_id, node_id = _meeting_run()

    result = drive(run_id, node_id, _approve, idea=IDEA)

    assert result["done"] is True
    assert _proposal(run_id)[0]["version"] == 2               # 复核轮次上限是 2
    assert "## 复核遗留意见" in parse_front_matter(read_artifact(run_id, "discussion"))[1]


# ---------------- 断点评审的回归用例 ----------------
def _run_once(run_id: str, node_id: int, *, idea: str = IDEA) -> dict:
    """执行一次会议节点，不自动回答确认点；用于模拟"停在某个确认点上"。"""
    import asyncio

    from app.engine.runners import NodeContext
    from app.meeting.runner import run_meeting

    with session_scope() as s:
        node = s.get(Node, node_id)
        ctx = NodeContext(
            run_id=run_id, node_id=node_id, node_key=node.node_key, type=node.type,
            name=node.name, phase=node.phase, config=dict(node.config_json or {}),
            assignee=dict(node.assignee_json or {}), attempt_id=0, attempt_no=1,
            feedback=(node.config_json or {}).get("feedback"), idea=idea)
    return asyncio.run(run_meeting(ctx))


def _set_feedback(node_id: int, text: str, kind: str = "reroute") -> None:
    with session_scope() as s:
        node = s.get(Node, node_id)
        node.config_json = with_feedback(node.config_json, text, kind)


def test_feedback_before_the_roster_does_not_skip_the_meeting(monkeypatch) -> None:
    """澄清阶段带反馈重试：名单、立场、议题照常执行，不能空着就成稿。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    run_id, node_id = _meeting_run()
    first = _run_once(run_id, node_id)
    assert first["suspended"] is True and first["checkpoint"]["kind"] == "clarify"

    _set_feedback(node_id, "补充成本约束：两周内必须上线", "user")

    def answer(checkpoint: dict) -> tuple:
        data = {"answers": ["Markdown 即可"]} if checkpoint["kind"] == "clarify" else {}
        return "approve", data, ""

    result = drive(run_id, node_id, answer, idea=IDEA)

    assert result["done"] is True and result["decision_points"] == 2
    kinds = Counter(call["kind"] for call in seen)
    assert kinds["roster"] == 1 and kinds["position"] == 2 and kinds["agenda"] == 1
    meeting = store.latest_meeting_for_node(node_id)
    participants = [row for row in store.list_participants(meeting["id"])
                    if row["mode"] == "participant"]
    assert len(participants) == 2
    assert _charter(node_id)["想法原文"]
    assert any("补充成本约束" in call["text"] for call in seen if call["kind"] == "position")


def test_reopen_requires_fresh_stances_instead_of_reusing_old_ones(monkeypatch) -> None:
    """复议重开的点必须有复议轮的新表态；漏答不能沿用旧等级判为已一致。"""
    install(monkeypatch, _replies(restance=_by_round(_agree, _agree)))
    run_id, node_id = _meeting_run()
    drive(run_id, node_id, _approve, idea=IDEA)
    assert _ledger(node_id)["D2"]["status"] == "agreed"
    _set_feedback(node_id, "评审认为登录方案需要复议", "reroute")

    def omit_reopened(text: str) -> str:
        return '{"stances": [{"index": "D3", "level": 1, "reason": "支持"}]}'

    install(monkeypatch, _replies(restance=_by_round(_agree, _agree), stance=omit_reopened))
    with pytest.raises(NodeFailure, match="缺少这些未了结决策点的表态"):
        drive(run_id, node_id, _approve, idea=IDEA)
    assert _ledger(node_id)["D2"]["status"] == "reopened"

    install(monkeypatch, _replies(restance=_by_round(_agree, _agree)))
    result = drive(run_id, node_id, _approve, idea=IDEA)

    assert result["done"] is True
    point = _ledger(node_id)["D2"]
    assert point["status"] == "agreed" and point["closed_round"] == 2
    architect = next(stance for stance in point["stances"].values()
                     if stance["name"] == "架构师")
    assert architect["round"] == 2                   # 结算用的是复议轮的表态


def test_a_decided_consensus_is_not_put_back_after_a_step_failure(monkeypatch) -> None:
    """人已经做出的决定在后续步骤失败后仍然有效，不会被重新开一扇门。"""
    install(monkeypatch, _replies(restance=_split, ruling="{不是合法的 JSON"))
    run_id, node_id = _meeting_run({"automation": {"level": "L1"}})

    def answer(checkpoint: dict) -> tuple:
        if checkpoint["kind"] == "consensus":
            return "approve", {"option": "delegate"}, ""
        return "approve", {}, ""

    with pytest.raises(NodeFailure, match="模型输出不符合要求"):
        drive(run_id, node_id, answer, idea=IDEA)

    install(monkeypatch, _replies(restance=_split))
    result = drive(run_id, node_id, _never, idea=IDEA)

    assert result["done"] is True and _ledger(node_id)["D2"]["status"] == "ruled"


def test_more_exchanges_than_participants_do_not_crash(monkeypatch) -> None:
    install(monkeypatch, _replies())
    run_id, node_id = _meeting_run({"params": {"discussion": {"exchanges_per_point": 3}}})

    result = drive(run_id, node_id, _approve, idea=IDEA)

    assert result["done"] is True


def test_reopen_feedback_reaches_the_participants(monkeypatch) -> None:
    """复议反馈与第 1 轮立场要出现在复议轮的每一次发言里，不能只让主持人看见。"""
    install(monkeypatch, _replies(restance=_by_round(_agree, _agree)))
    run_id, node_id = _meeting_run()
    drive(run_id, node_id, _approve, idea=IDEA)
    _set_feedback(node_id, "评审认为登录方案需要复议", "reroute")

    seen: list[dict] = []
    install(monkeypatch, _replies(restance=_by_round(_agree, _agree)), seen)
    drive(run_id, node_id, _approve, idea=IDEA)

    for kind in ("stance", "claim", "challenge", "respond", "restance"):
        texts = [call["text"] for call in seen if call["kind"] == kind]
        assert texts, kind
        assert all("评审认为登录方案需要复议" in text for text in texts), kind
    stances = [call["text"] for call in seen if call["kind"] == "stance"]
    assert all("必须保证" in text for text in stances)          # 复议轮仍看得到原始立场


def test_full_history_switch_keeps_previous_rounds_verbatim(monkeypatch) -> None:
    """对照实验开关：打开时第二轮的提示里带着第一轮的原话，默认只带本轮。"""
    claim, challenge = _replies()["claim"], _replies()["challenge"]
    once = {"exchanges_per_point": 1}                         # 每轮每个决策点只交锋一次

    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    run_id, node_id = _meeting_run({"params": {"discussion": dict(once)}})
    drive(run_id, node_id, _approve, idea=IDEA)
    claims = [call["text"] for call in seen if call["kind"] == "claim"]
    assert len(claims) == 2                                   # 两轮交锋各一次主张
    assert claim not in claims[1] and challenge not in claims[1]

    seen = []
    install(monkeypatch, _replies(), seen)
    run_id, node_id = _meeting_run({"params": {"discussion": {**once, "full_history": True}}})
    drive(run_id, node_id, _approve, idea=IDEA)
    claims = [call["text"] for call in seen if call["kind"] == "claim"]
    assert len(claims) == 2
    assert claim in claims[1] and challenge in claims[1]      # 第一轮的主张与质疑仍在


def test_continue_at_an_all_closed_consensus_finishes(monkeypatch) -> None:
    """全部了结时人仍选了"继续讨论"：忽略它直接成稿，不能卡死在空交锋/空表态。"""
    install(monkeypatch, _replies(restance=_agree))
    run_id, node_id = _meeting_run({"automation": {"level": "L1"}})
    asked: list[str] = []

    def answer(checkpoint: dict) -> tuple:
        asked.append(checkpoint["kind"])
        if checkpoint["kind"] == "consensus":
            assert checkpoint["payload"]["reason"] == "all_closed"
            return "reject", {"option": "continue"}, "继续讨论也没有未决点了"
        return "approve", {}, ""

    result = drive(run_id, node_id, answer, idea=IDEA)

    assert result["done"] is True and result["deferred"] == 0
    assert asked == ["clarify", "roster", "consensus"]


def _continue_answer(checkpoint: dict) -> tuple:
    """第一次共识选"继续讨论"；重入后再问到的共识（新一轮的 stall）按分歧整合收尾。"""
    if checkpoint["kind"] == "consensus":
        return "reject", {"option": "continue"}, "先把登录的存储方案定下来"
    return "approve", {}, ""


def _integrate_answer(checkpoint: dict) -> tuple:
    if checkpoint["kind"] == "consensus":
        return "approve", {"option": "integrate"}, ""
    return "approve", {}, ""


def _clarify_answer(checkpoint: dict) -> tuple:
    data = {"answers": ["Markdown 即可"]} if checkpoint["kind"] == "clarify" else {}
    return "approve", data, ""


def test_consensus_decision_survives_a_crash_after_the_ack(monkeypatch) -> None:
    """共识确认点认领与处置记录同一事务：ack 后被打断，重入按记录重放"继续讨论"，
    不把人的决定丢掉重新开一扇门；重放出的下一轮交锋真的发生了。"""
    install(monkeypatch, _replies())
    run_id, node_id = _meeting_run({"automation": {"level": "L1"}})
    from app.meeting.session import MeetingSession

    original_ack = MeetingSession.ack_decision
    armed = {"on": True}

    def flaky_ack(self, decision=None, **kwargs):
        original_ack(self, decision, **kwargs)
        if armed["on"] and (decision or {}).get("data", {}).get("option") == "continue":
            armed["on"] = False                        # 处置刚认领、续开还没落地：进程在这里被杀
            raise asyncio.CancelledError()

    monkeypatch.setattr(MeetingSession, "ack_decision", flaky_ack)
    with pytest.raises(asyncio.CancelledError):
        drive(run_id, node_id, _continue_answer, idea=IDEA)
    monkeypatch.setattr(MeetingSession, "ack_decision", original_ack)

    meeting_id = store.latest_meeting_for_node(node_id)["id"]
    guidance = [t for t in store.list_turns(meeting_id) if t["kind"] == "user_guidance"]
    guidance_round = guidance[0]["round"]
    result = drive(run_id, node_id, _integrate_answer, idea=IDEA)
    assert result["done"] is True
    guidance = [t for t in store.list_turns(meeting_id) if t["kind"] == "user_guidance"]
    assert len(guidance) == 1 and "先把登录的存储方案定下来" in guidance[0]["content"]
    claims = [t for t in store.round_turns(meeting_id, round=guidance_round,
                                           kinds=["claim"])]
    assert claims                                       # 重放真的续开了一轮交锋，不是重新问人


def test_consensus_guidance_is_not_repeated_after_a_crash(monkeypatch) -> None:
    """共识指导语的落盘与轮次移交不在同一事务：中断重入按轮次与内容去重，轮次不多加。"""
    install(monkeypatch, _replies())
    run_id, node_id = _meeting_run({"automation": {"level": "L1"}})

    armed = {"on": True}
    original_add = store.add_turn

    def flaky_add(meeting_id, **kwargs):
        turn_id = original_add(meeting_id, **kwargs)
        if armed["on"] and kwargs.get("kind") == "user_guidance":
            armed["on"] = False                        # 指导语刚落盘、轮次还没移交：进程在这里被杀
            raise asyncio.CancelledError()
        return turn_id

    monkeypatch.setattr(store, "add_turn", flaky_add)
    with pytest.raises(asyncio.CancelledError):
        drive(run_id, node_id, _continue_answer, idea=IDEA)
    monkeypatch.setattr(store, "add_turn", original_add)

    round_at_crash = store.latest_meeting_for_node(node_id)["round"]
    result = drive(run_id, node_id, _integrate_answer, idea=IDEA)
    assert result["done"] is True
    # 轮次移交到下一轮且只加一次：重复重入不会把轮次多加
    assert store.latest_meeting_for_node(node_id)["round"] == round_at_crash + 1
    meeting_id = store.latest_meeting_for_node(node_id)["id"]
    guidance = [t for t in store.list_turns(meeting_id) if t["kind"] == "user_guidance"]
    assert len(guidance) == 1


def _meeting_context(run_id: str, node_id: int) -> NodeContext:
    with session_scope() as s:
        node = s.get(Node, node_id)
        return NodeContext(run_id=run_id, node_id=node_id, node_key=node.node_key,
                           type=node.type, name=node.name, phase=node.phase,
                           config=dict(node.config_json or {}),
                           assignee=dict(node.assignee_json or {}), attempt_id=0,
                           attempt_no=1, feedback=(node.config_json or {}).get("feedback"),
                           idea=IDEA)


def test_clarify_answers_are_not_repeated_after_a_crash(monkeypatch) -> None:
    """澄清回答逐条落盘与状态移交不在同一事务：中断重入按轮次与内容去重，每个问题只答一次。"""
    install(monkeypatch, _replies())
    run_id, node_id = _meeting_run()
    from app.meeting.runner import run_meeting

    armed = {"on": True}
    original_add = store.add_turn

    def flaky_add(meeting_id, **kwargs):
        turn_id = original_add(meeting_id, **kwargs)
        if armed["on"] and kwargs.get("kind") == "answer":
            armed["on"] = False                        # 第一条回答刚落盘：进程在这里被杀
            raise asyncio.CancelledError()
        return turn_id

    monkeypatch.setattr(store, "add_turn", flaky_add)
    with pytest.raises(asyncio.CancelledError):
        drive(run_id, node_id, _clarify_answer, idea=IDEA)
    monkeypatch.setattr(store, "add_turn", original_add)

    result = drive(run_id, node_id, _approve, idea=IDEA)
    assert result["done"] is True
    meeting_id = store.latest_meeting_for_node(node_id)["id"]
    answers = [t for t in store.list_turns(meeting_id) if t["kind"] == "answer"]
    questions = len(store.get_state(meeting_id)["clarifications"])
    assert len(answers) == questions                   # 每个问题恰好一条回答
    assert sum(1 for t in answers if "Markdown 即可" in t["content"]) == 1


def test_early_reroute_self_feedback_is_recorded_as_review_voice(monkeypatch) -> None:
    """还没到议题阶段时收到的 reroute_self 反馈：发言同样标"评审意见"，不标成用户。"""
    install(monkeypatch, _replies())
    run_id, node_id = _meeting_run()
    from app.meeting.runner import run_meeting

    first = asyncio.run(run_meeting(_meeting_context(run_id, node_id)))   # 挂起在澄清确认点
    assert first["suspended"] is True
    _set_feedback(node_id, "先把澄清里没答的问题补上", "reroute_self")
    asyncio.run(run_meeting(_meeting_context(run_id, node_id)))

    meeting_id = store.latest_meeting_for_node(node_id)["id"]
    turns = [t for t in store.list_turns(meeting_id)
             if t["kind"] == "user_feedback" and "先把澄清里没答的问题补上" in t["content"]]
    assert [t["speaker_name"] for t in turns] == ["评审意见"]


def test_reopen_feedback_from_reroute_self_is_recorded_as_review_voice(monkeypatch) -> None:
    """复议反馈来路是 reroute_self：发言标"评审意见"，不标成用户。"""
    install(monkeypatch, _replies(restance=_by_round(_agree, _agree)))
    run_id, node_id = _meeting_run()
    drive(run_id, node_id, _approve, idea=IDEA)
    _set_feedback(node_id, "先把 D1 的存储方案定下来", "reroute_self")
    drive(run_id, node_id, _approve, idea=IDEA)

    meeting_id = store.latest_meeting_for_node(node_id)["id"]
    turns = [t for t in store.list_turns(meeting_id)
             if t["kind"] == "user_feedback" and "先把 D1 的存储方案定下来" in t["content"]]
    assert [t["speaker_name"] for t in turns] == ["评审意见"]


def test_early_feedback_speech_is_not_repeated_after_a_crash(monkeypatch) -> None:
    """入口的早期反馈分支（还没到议题阶段的反馈）：发言与状态移交不在同一事务，
    中断重入按轮次与内容去重，不重复写反馈发言。"""
    install(monkeypatch, _replies())
    run_id, node_id = _meeting_run()
    from app.meeting.runner import run_meeting

    first = asyncio.run(run_meeting(_meeting_context(run_id, node_id)))   # 挂起在澄清确认点
    assert first["suspended"] is True
    _set_feedback(node_id, "补充成本约束：两周内必须上线", "user")

    armed = {"on": True}
    original_add = store.add_turn

    def flaky_add(meeting_id, **kwargs):
        turn_id = original_add(meeting_id, **kwargs)
        if armed["on"] and kwargs.get("kind") == "user_feedback":
            armed["on"] = False                        # 反馈发言刚落盘、状态还没移交：进程在这里被杀
            raise asyncio.CancelledError()
        return turn_id

    monkeypatch.setattr(store, "add_turn", flaky_add)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_meeting(_meeting_context(run_id, node_id)))
    monkeypatch.setattr(store, "add_turn", original_add)

    asyncio.run(run_meeting(_meeting_context(run_id, node_id)))           # 重入续开
    meeting_id = store.latest_meeting_for_node(node_id)["id"]
    feedbacks = [t for t in store.list_turns(meeting_id)
                 if t["kind"] == "user_feedback" and "补充成本约束" in t["content"]]
    assert len(feedbacks) == 1


def test_a_proposal_missing_required_sections_is_rejected(monkeypatch) -> None:
    install(monkeypatch, _replies(proposal="## 目标与成功标准\n\n两句话。"))
    run_id, node_id = _meeting_run({"automation": {"level": "L3"}})

    with pytest.raises(NodeFailure, match="方案缺少这些章节"):
        drive(run_id, node_id, _never, idea=IDEA)


def test_structured_output_validators_reject_bad_shapes() -> None:
    from app.meeting.protocols.discussion import (_validate_agenda, _validate_stances,
                                                  _validate_summaries)

    assert _validate_summaries(["D1"])({
        "summaries": [{"index": "D1", "summary": "x"}, "junk"]})
    assert _validate_agenda({"points": [{"title": 5, "options": 3}]})
    issues = _validate_stances(["D2"])({
        "stances": [{"index": "D3", "level": 1}],
        "new_points": [{"title": "x", "level": "high", "options": 3}]})
    assert any("缺少这些未了结决策点的表态" in issue for issue in issues)
    assert any("level" in issue for issue in issues)
    assert any("options" in issue for issue in issues)


def test_metrics_after_a_real_discussion_meeting(monkeypatch) -> None:
    """真实跑完一场讨论会议之后，指标的会议一段反映这场会议。

    放在本文件是因为驱动真实会议需要这里的替身脚本（测试模块之间不能互相导入）。
    """
    install(monkeypatch, _replies(), None)
    run_id, node_id = _meeting_run()

    result = drive(run_id, node_id, _approve, idea=IDEA)

    assert result["done"] is True
    metrics = run_metrics(run_id)

    assert metrics["meetings"]["total"] == 1
    assert metrics["meetings"]["turns"] > 0
    assert metrics["meetings"]["checkpoints"]["human"] == 2    # 澄清与名单都是人答的
    assert metrics["review"]["total"] == 0                     # 讨论会议不产出评审报告


# ---------------- S5.5：补齐此前未被测试执行到的分支 ----------------
def test_structured_output_validators_reject_the_remaining_bad_shapes() -> None:
    """结构化输出的校验器逐个报错分支：坏的形状都要带着明确原因被拒。"""
    from app.meeting.protocols.discussion import (_validate_answers, _validate_brief,
                                                  _validate_check, _validate_reopen,
                                                  _validate_rulings, _validate_stances,
                                                  _validate_summaries)
    from app.meeting.protocols.discussion import _validate_agenda, _validate_position

    assert _validate_brief({"brief": "目标", "questions": ["合法", 5]})
    assert _validate_brief({"brief": "目标", "questions": "导出格式"})
    assert _validate_answers(1)({"answers": []})
    assert _validate_answers(1)({"answers": [{"text": ""}]})
    assert _validate_position({"must": "单一", "risks": [], "route": "路线"})
    assert _validate_position({"must": [], "risks": [], "route": "  "})
    assert _validate_agenda({"points": []})
    assert _validate_agenda({"points": ["不是对象", {"title": "T", "question": 5}]})
    assert _validate_stances(["D1"])({"stances": "不是数组"})
    assert _validate_stances(["D1"])({"stances": []})
    assert _validate_stances(["D1"])({"stances": ["不是对象"]})
    assert _validate_stances(["D1"])({"stances": [{"level": 1}]})            # 缺 index
    assert _validate_stances([])({"stances": [{"index": "D1", "level": 9}]})
    assert _validate_stances([])({"stances": [{"index": "D1", "level": 2}]})  # 有保留没写顾虑
    assert _validate_stances([])({"stances": [{"index": "D1", "level": 1}],
                                  "new_points": ["不是对象"]})
    assert _validate_stances([])({"stances": [{"index": "D1", "level": 1}],
                                  "new_points": [{"level": 3}]})             # 缺 title
    assert _validate_summaries(["D1"])({"summaries": "不是数组"})
    assert _validate_summaries(["D1"])({"summaries": [{"index": "D9", "summary": "x"}]})
    assert _validate_rulings(["D1"])({"rulings": "不是数组"})
    assert _validate_rulings(["D1"])({"rulings": ["不是对象"]})
    assert _validate_rulings(["D1"])({"rulings": []})
    assert _validate_rulings(["D1"])({"rulings": [
        {"index": "D1", "conclusion": "", "rationale": "理由"}]})
    assert _validate_check({"ok": "是"})
    assert _validate_check({"ok": False, "issues": []})
    assert _validate_reopen({"points": []})
    assert _validate_reopen({"points": ["不是对象", {"index": 5}, {"title": 6}, {"options": 3}]})
    assert _validate_reopen({"points": [{"question": "只有问题"}]})


def test_roster_validator_rejects_bad_rosters_and_accepts_a_temporary_role() -> None:
    """名单校验：空表、重复、非对象、不存在的角色、不完整的临时角色都报错。"""
    from app.meeting.protocols.discussion import _validate_roster

    library = {"architect": {"role_key": "architect", "name": "架构师", "meta": {}}}
    validator = _validate_roster(library, 2, 2)
    assert validator({"participants": "不是数组"})
    assert validator({"participants": []})
    assert validator({"participants": [{"role_key": "architect"}, {"role_key": "architect"}]})
    assert validator({"participants": ["不是对象", {"role_key": "architect"},
                                      {"role_key": "ghost"}]})
    assert validator({"participants": [{"display_name": "只有名字，没有 persona"}]})
    assert _validate_roster(library, 1, 1)({"participants": [
        {"role_key": "", "display_name": "只有名字"}]}) == [
        "临时角色必须同时给出 display_name 与 persona"]
    ranged = _validate_roster(library, 2, 5)
    assert ranged({"participants": [{"role_key": "architect"}]})      # 人数不在 2 到 5 之间
    assert not _validate_roster(library, 2, 2)({"participants": [
        {"role_key": "", "display_name": "运维顾问", "persona": "你关心部署与回滚"},
        {"role_key": "architect", "reason": "把握技术路线"}]})


def test_a_new_point_raised_in_a_stance_enters_the_ledger(monkeypatch) -> None:
    """表态里补提的决策点当场入账：提出者、候选项、表态等级都记进账本，随后照常交锋。"""
    def architect_stance(text: str) -> str:
        return ARCHITECT_NEW if "架构视角" in text else UX_NO_D3

    def architect_restance(text: str) -> str:
        return ARCHITECT_RESTANCE if "架构视角" in text else UX

    install(monkeypatch, _replies(
        stance=architect_stance, restance=architect_restance,
        exchange_summary=_summary_by_ledger))
    run_id, node_id = _meeting_run()

    result = drive(run_id, node_id, _approve, idea=IDEA)

    assert result["done"] is True and result["decision_points"] == 3
    ledger = _ledger(node_id)
    assert ledger["D3"]["raised_by"] == "架构师"
    assert ledger["D3"]["options"] == ["支持", "不支持"]
    architect = next(stance for stance in ledger["D3"]["stances"].values()
                     if stance["name"] == "架构师")
    assert architect["level"] == 5
    assert ledger["D3"]["origin_round"] == 1            # 第 1 轮表态时当场入账
    assert ledger["D3"]["status"] == "deferred"          # 只有一人表态，直到最后也没谈拢
    assert ledger["D2"]["status"] == "agreed"


def test_a_temporary_role_proposed_by_the_facilitator_can_sit_in_the_meeting(
        monkeypatch) -> None:
    """主持人提库里没有的临时角色（display_name + persona）：照常入会，人设作为系统提示。"""
    install(monkeypatch, _replies(roster=ROSTER_TEMP))
    run_id, node_id = _meeting_run({"rosters": {"participants": {"count": 1}}})

    result = drive(run_id, node_id, _approve, idea=IDEA)

    assert result["done"] is True
    meeting_id = store.latest_meeting_for_node(node_id)["id"]
    rows = [row for row in store.list_participants(meeting_id) if row["mode"] == "participant"]
    assert [(row["role_key"], row["display_name"], row["system_prompt"]) for row in rows] == [
        (None, "运维顾问", "你关心部署与回滚，升级前必须能一键回滚")]
    assert "- 运维顾问（参会者）：你关心部署与回滚" in _charter(node_id)["参会名单"]


def test_feedback_at_the_roster_stage_is_recorded_and_not_lost(monkeypatch) -> None:
    """名单阶段的反馈：记成一条用户发言和名单意见，名单照常确认，不跳复议也不重挑。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    run_id, node_id = _meeting_run()
    first = _run_once(run_id, node_id)
    assert first["checkpoint"]["kind"] == "clarify"
    decide_gate(gate_id=first["checkpoint"]["id"], action="approve",
                data={"answers": ["Markdown 即可"]})
    second = _run_once(run_id, node_id)
    assert second["checkpoint"]["kind"] == "roster"
    _set_feedback(node_id, "补一名安全与合规的视角", "user")

    result = drive(run_id, node_id, _approve, idea=IDEA)

    assert result["done"] is True
    meeting_id = store.latest_meeting_for_node(node_id)["id"]
    feedback_turns = [t for t in store.list_turns(meeting_id) if t["kind"] == "user_feedback"]
    assert [t["content"] for t in feedback_turns] == ["补一名安全与合规的视角"]
    assert store.get_state(meeting_id).get("roster_feedback") == "补一名安全与合规的视角"
    assert Counter(call["kind"] for call in seen)["roster"] == 1   # 名单已有提议：只重新确认


def test_a_brief_without_questions_skips_the_clarify_checkpoint(monkeypatch) -> None:
    """简报没有列出要澄清的问题：澄清确认点不开，章程如实写"没有需要澄清的问题"。"""
    install(monkeypatch, _replies(brief=BRIEF_NOQ))
    run_id, node_id = _meeting_run()
    asked: list[str] = []

    def answer(checkpoint: dict) -> tuple:
        asked.append(checkpoint["kind"])
        return "approve", {}, ""

    result = drive(run_id, node_id, answer, idea=IDEA)

    assert result["done"] is True and asked == ["roster"]
    assert _charter(node_id)["已澄清的问题"] == "（没有需要澄清的问题）"


def test_background_materials_are_read_into_the_brief(monkeypatch) -> None:
    """运行带了背景材料：会前准备的提示里要有材料一段。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    run_id, node_id = _meeting_run()
    save_material(run_id, "竞品笔记", "竞品 X 只做收集不做导出。")

    result = drive(run_id, node_id, _approve, idea=IDEA)

    assert result["done"] is True
    brief_prompt = next(call["text"] for call in seen if call["kind"] == "brief")
    assert "## 背景材料" in brief_prompt and "竞品 X 只做收集不做导出" in brief_prompt


def test_proxy_answers_are_recorded_with_evidence_or_as_assumptions(monkeypatch) -> None:
    """需求方代理答澄清：带出处的记为代理回答并留下出处，没有出处的降级为假设。"""
    install(monkeypatch, _replies(brief=BRIEF_TWOQ, answer=ANSWER_TWOQ))
    run_id, node_id = _meeting_run({"automation": {"level": "L3"}})

    result = drive(run_id, node_id, _never, idea=IDEA)

    assert result["done"] is True
    meeting_id = store.latest_meeting_for_node(node_id)["id"]
    clarifications = store.get_state(meeting_id)["clarifications"]
    assert clarifications[0]["source"] == "proxy"
    assert clarifications[0]["evidence"] == "简报：两周内可用"
    assert clarifications[1]["source"] == "assumption"
    answered = _charter(node_id)["已澄清的问题"]
    assert "需求方代理，依据背景材料或用户回答" in answered
    assert "需要导出周报" in answered and "按假设处理" in answered
    assert "出处：简报：两周内可用" in answered                        # 详设 §7.6：出处原文进章程


def _bare_meeting(run_id: str, node_id: int, phase: str) -> tuple[int, MeetingSession]:
    """手工建一场空会议，用来直接驱动协议的单个步骤（不经 run_meeting）。"""
    meeting_id = store.create_meeting(run_id=run_id, node_id=node_id, node_key="discuss",
                                      protocol="discussion", charter={"sections": []}, config={})
    store.update_meeting(meeting_id, phase=phase, round=1)
    with session_scope() as s:
        node = s.get(Node, node_id)
        ctx = NodeContext(run_id=run_id, node_id=node_id, node_key=node.node_key, type=node.type,
                          name=node.name, phase=phase, config=dict(node.config_json or {}),
                          assignee={}, attempt_id=0, attempt_no=1, feedback=None, idea=IDEA)
    return meeting_id, MeetingSession(meeting_id=meeting_id, ctx=ctx)


def test_resuming_skips_steps_whose_results_are_already_in_state(monkeypatch) -> None:
    """简报/澄清/名单/议题的结果已写进状态时重入：直接推进，不再调模型。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    run_id, node_id = _meeting_run()
    _meeting_id, mtg = _bare_meeting(run_id, node_id, "prepare")
    protocol = discussion_module.DiscussionProtocol()

    store.set_state(mtg.meeting_id, {"brief": "已有简报"})
    assert asyncio.run(protocol.step_prepare(mtg)).action == "advance"
    store.set_state(mtg.meeting_id, {"clarifications": [
        {"question": "q", "answer": "a", "source": "user"}]})
    assert asyncio.run(protocol.step_clarify(mtg)).action == "advance"
    store.set_state(mtg.meeting_id, {"roster_done": True})
    assert asyncio.run(protocol.step_roster(mtg)).action == "advance"
    store.set_state(mtg.meeting_id, {"agenda_done": True})
    assert asyncio.run(protocol.step_agenda(mtg)).action == "advance"

    assert seen == []                                   # 四个短路都没有调模型


def test_positions_fail_without_participants(monkeypatch) -> None:
    """名单是空的还推进到立场：带着明确的错误失败，不空转成稿。"""
    install(monkeypatch, _replies(), None)
    run_id, node_id = _meeting_run()
    _meeting_id, mtg = _bare_meeting(run_id, node_id, "positions")

    with pytest.raises(NodeFailure, match="讨论会议没有参会者"):
        asyncio.run(discussion_module.DiscussionProtocol().step_positions(mtg))


def test_exchange_resumes_without_repeating_the_recorded_claim(monkeypatch) -> None:
    """交锋的每段发言都按认领找回：质疑落盘、认领编号还没写上时被杀，重入主张与质疑都不重复。"""
    install(monkeypatch, _replies(), None)
    run_id, node_id = _meeting_run()
    armed = {"on": True}
    from app.meeting import calls as calls_module

    original_emit = calls_module.emit

    async def flaky_emit(run_id_, type_, node_id_, payload):
        if armed["on"] and type_ == "meeting_turn_finished" \
                and payload.get("kind") == "challenge":
            armed["on"] = False                        # 质疑已落盘、认领编号还没写上：进程在这里被杀
            raise asyncio.CancelledError()
        return await original_emit(run_id_, type_, node_id_, payload)

    monkeypatch.setattr(calls_module, "emit", flaky_emit)
    with pytest.raises(asyncio.CancelledError):
        drive(run_id, node_id, _approve, idea=IDEA)

    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    result = drive(run_id, node_id, _approve, idea=IDEA)

    assert result["done"] is True
    kinds = Counter(call["kind"] for call in seen)
    assert kinds["claim"] == 1 and kinds["challenge"] == 1 and kinds["respond"] == 2


def test_reopen_reuses_the_recorded_plan_and_ignores_unknown_indexes(monkeypatch) -> None:
    """复议的计划落盘后被中断：重入复用它，不再让主持人重讲；账本里没有的编号不建点。"""
    install(monkeypatch, _replies(restance=_by_round(_agree, _agree), reopen=REOPEN_WITH_D99))
    run_id, node_id = _meeting_run()
    drive(run_id, node_id, _approve, idea=IDEA)
    _set_feedback(node_id, "评审认为登录方案需要复议", "reroute")

    original = discussion_module.speak
    armed = {"on": True}

    async def flaky(mtg_, participant, **kwargs):
        result = await original(mtg_, participant, **kwargs)
        if armed["on"] and kwargs.get("kind") == "reopen":
            armed["on"] = False                        # 复议计划刚落盘：进程在这里被杀
            raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(discussion_module, "speak", flaky)
    with pytest.raises(asyncio.CancelledError):
        drive(run_id, node_id, _approve, idea=IDEA)

    seen: list[dict] = []
    install(monkeypatch, _replies(restance=_by_round(_agree, _agree)), seen)
    result = drive(run_id, node_id, _approve, idea=IDEA)

    assert result["done"] is True
    assert Counter(call["kind"] for call in seen)["reopen"] == 0    # 计划复用，不重新调用
    ledger = _ledger(node_id)
    assert ledger["D2"]["status"] == "agreed" and ledger["D2"]["closed_round"] == 2
    assert "D99" not in ledger                                # 账本里没有的编号不计入
    meeting_id = store.latest_meeting_for_node(node_id)["id"]
    feedback = [t for t in store.list_turns(meeting_id) if t["kind"] == "user_feedback"]
    assert [t["content"] for t in feedback] == ["评审认为登录方案需要复议"]  # 反馈发言不重复


def test_a_revision_without_change_notes_is_rejected(monkeypatch) -> None:
    """复议后的成稿是修订版：缺少「## 修改说明」要带着明确的错误失败，方案停在旧版。"""
    install(monkeypatch, _replies(restance=_by_round(_agree, _agree)))
    run_id, node_id = _meeting_run()
    drive(run_id, node_id, _approve, idea=IDEA)
    assert _proposal(run_id)[0]["version"] == 1
    _set_feedback(node_id, "评审认为登录方案需要复议", "reroute")

    install(monkeypatch, _replies(restance=_by_round(_agree, _agree),
                                  proposal=PROPOSAL, proposal_revise=PROPOSAL))
    with pytest.raises(NodeFailure, match="修订版缺少"):
        drive(run_id, node_id, _approve, idea=IDEA)

    assert _proposal(run_id)[0]["version"] == 1


def test_rounds_max_sends_the_discussion_to_consensus_at_the_limit(monkeypatch) -> None:
    """轮次到上限仍有未决点：带着 rounds_max 的原因开共识确认点，由人决定怎么办。"""
    install(monkeypatch, _replies(restance=_split))
    run_id, node_id = _meeting_run({"params": {"discussion": {"rounds_max": 1}}})
    asked: list[dict] = []

    def answer(checkpoint: dict) -> tuple:
        asked.append(checkpoint)
        if checkpoint["kind"] == "consensus":
            return "approve", {"option": "integrate"}, ""
        return "approve", {}, ""

    result = drive(run_id, node_id, answer, idea=IDEA)

    assert result["done"] is True and result["rounds"] == 1
    consensus = next(item for item in asked if item["kind"] == "consensus")
    assert consensus["payload"]["reason"] == "rounds_max"
    assert _ledger(node_id)["D1"]["status"] == "agreed"
    assert _ledger(node_id)["D2"]["status"] == "deferred"      # 带着分歧整合


def test_consensus_falls_back_to_a_reason_from_the_ledger(monkeypatch) -> None:
    """consensus_reason 丢失（崩溃窗口）时按账本现况兜底：全部了结与仍有未决各走各的。"""
    install(monkeypatch, _replies(), None)
    run_id, node_id = _meeting_run({"automation": {"level": "L1"}})
    meeting_id, mtg = _bare_meeting(run_id, node_id, "consensus")
    store.upsert_decision_point(meeting_id, "D1", title="已了结的点", question="q",
                                options_json=[], status="agreed", conclusion="结论")
    protocol = discussion_module.DiscussionProtocol()

    result = asyncio.run(protocol.step_consensus(mtg))

    assert result.action == "suspend"
    assert result.checkpoint["title"] == "本轮的决策点已全部了结"        # all_closed 兜底

    store.upsert_decision_point(meeting_id, "D2", title="没谈拢的点", question="q",
                                options_json=[], status="open")
    result = asyncio.run(protocol.step_consensus(mtg))

    assert result.checkpoint["payload"]["reason"] == "stall"            # stall 兜底
    assert "讨论陷入停滞" in result.checkpoint["title"]


def test_brief_speak_is_salvaged_when_the_finish_write_is_lost(monkeypatch) -> None:
    """简报发言落盘、状态还没写时被杀：重入按认领找回，不重复调用模型。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    run_id, node_id = _meeting_run()
    _meeting_id, mtg = _bare_meeting(run_id, node_id, "prepare")
    protocol = discussion_module.DiscussionProtocol()
    armed = {"on": True}
    from app.meeting import calls as calls_module

    original_emit = calls_module.emit

    async def flaky_emit(run_id_, type_, node_id_, payload):
        if armed["on"] and type_ == "meeting_turn_finished" and payload.get("kind") == "brief":
            armed["on"] = False                    # 简报已落盘、状态还没写：进程在这里被杀
            raise asyncio.CancelledError()
        return await original_emit(run_id_, type_, node_id_, payload)

    monkeypatch.setattr(calls_module, "emit", flaky_emit)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(protocol.step_prepare(mtg))
    monkeypatch.setattr(calls_module, "emit", original_emit)

    asyncio.run(protocol.step_prepare(mtg))

    assert Counter(call["kind"] for call in seen)["brief"] == 1


def test_proxy_answer_is_salvaged_when_the_finish_write_is_lost(monkeypatch) -> None:
    """澄清的代理回答落盘后被杀：重入按认领找回，不重复调用模型。"""
    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    run_id, node_id = _meeting_run({"automation": {"level": "L3"}})
    _meeting_id, mtg = _bare_meeting(run_id, node_id, "clarify")
    store.add_participant(mtg.meeting_id, role_key="requester_proxy", display_name="需求方代理",
                          mode="proxy", system_prompt="你是需求方代理")
    store.set_state(mtg.meeting_id, {"questions": ["需要导出哪些格式？"]})
    protocol = discussion_module.DiscussionProtocol()
    armed = {"on": True}
    from app.meeting import calls as calls_module

    original_emit = calls_module.emit

    async def flaky_emit(run_id_, type_, node_id_, payload):
        if armed["on"] and type_ == "meeting_turn_finished" and payload.get("kind") == "answer":
            armed["on"] = False                    # 回答已落盘、状态还没写：进程在这里被杀
            raise asyncio.CancelledError()
        return await original_emit(run_id_, type_, node_id_, payload)

    monkeypatch.setattr(calls_module, "emit", flaky_emit)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(protocol.step_clarify(mtg))
    monkeypatch.setattr(calls_module, "emit", original_emit)

    asyncio.run(protocol.step_clarify(mtg))

    assert Counter(call["kind"] for call in seen)["answer"] == 1


def test_clarify_auto_decision_is_not_recorded_twice_after_a_crash(monkeypatch) -> None:
    """审计门落库后、澄清结果入状态前被杀：重入不再重复落一条已决定的门。"""
    install(monkeypatch, _replies(), None)
    run_id, node_id = _meeting_run({"automation": {"level": "L3"}})
    _meeting_id, mtg = _bare_meeting(run_id, node_id, "clarify")
    store.add_participant(mtg.meeting_id, role_key="requester_proxy", display_name="需求方代理",
                          mode="proxy", system_prompt="你是需求方代理")
    store.set_state(mtg.meeting_id, {"questions": ["需要导出哪些格式？"]})
    protocol = discussion_module.DiscussionProtocol()
    armed = {"on": True}
    from app.meeting import checkpoints as checkpoints_module

    original_emit = checkpoints_module.emit

    async def flaky_emit(run_id_, type_, node_id_, payload):
        if armed["on"] and type_ == "checkpoint_decided":
            armed["on"] = False                    # 审计门已落库、结果还没入状态：进程在这里被杀
            raise asyncio.CancelledError()
        return await original_emit(run_id_, type_, node_id_, payload)

    monkeypatch.setattr(checkpoints_module, "emit", flaky_emit)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(protocol.step_clarify(mtg))
    monkeypatch.setattr(checkpoints_module, "emit", original_emit)

    asyncio.run(protocol.step_clarify(mtg))

    with session_scope() as s:
        gates = s.query(Gate).filter(Gate.meeting_id == mtg.meeting_id,
                                     Gate.kind == "clarify").all()
    assert len(gates) == 1


def test_roster_rows_are_not_duplicated_after_a_crash(monkeypatch) -> None:
    """名单提议落库后、名单入状态前被杀：重入先认领找回提议，参会者行不再翻倍。"""
    install(monkeypatch, _replies(), None)
    run_id, node_id = _meeting_run()
    _meeting_id, mtg = _bare_meeting(run_id, node_id, "roster")
    store.add_participant(mtg.meeting_id, role_key="facilitator", display_name="主持人",
                          mode="facilitator", system_prompt="你是主持人")
    protocol = discussion_module.DiscussionProtocol()
    armed = {"on": True}
    original_set_state = mtg.set_state

    def flaky_set_state(**changes):
        if armed["on"] and "roster" in changes:
            armed["on"] = False                    # 参会者行已建、名单还没入状态：进程在这里被杀
            raise asyncio.CancelledError()
        return original_set_state(**changes)

    monkeypatch.setattr(mtg, "set_state", flaky_set_state)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(protocol._propose(mtg, {}, 2, 2))
    asyncio.run(protocol._propose(mtg, {}, 2, 2))

    names = [row["display_name"] for row in store.list_participants(mtg.meeting_id)
             if row["mode"] == "participant"]
    assert len(names) == 2                         # 架构师与用户体验各一行，不是四行


def test_clarify_decision_is_consumed_in_the_same_write(monkeypatch) -> None:
    """澄清的人工回答与决定认领一次写完：旧的“结果入状态、认领分离”窗口不再存在。"""
    install(monkeypatch, _replies(), None)
    run_id, node_id = _meeting_run()
    _meeting_id, mtg = _bare_meeting(run_id, node_id, "clarify")
    store.set_state(mtg.meeting_id, {"questions": ["需要导出哪些格式？"]})
    protocol = discussion_module.DiscussionProtocol()
    suspended = asyncio.run(protocol.step_clarify(mtg))
    gate_id = suspended.checkpoint["id"]
    decide_gate(gate_id=gate_id, action="approve", data={"answers": ["Markdown 即可"]})
    raised = {"on": True}
    original_ack = mtg.ack_decision

    def flaky_ack(decision=None, disposition=None):
        if raised["on"]:
            raised["on"] = False                   # 旧实现在这里单独 ack：中间被杀决定就滞留
            raise asyncio.CancelledError()
        return original_ack(decision, disposition)

    monkeypatch.setattr(mtg, "ack_decision", flaky_ack)
    result = asyncio.run(protocol.step_clarify(mtg))

    state = store.get_state(mtg.meeting_id)
    assert result.action == "advance"
    assert state.get("handled_gate_id") == gate_id
    assert state.get("clarifications")


def test_roster_approval_consumes_the_decision_atomically(monkeypatch) -> None:
    """名单通过：名单生效与决定认领一次写完，认领窗口被杀决定不滞留。"""
    install(monkeypatch, _replies(), None)
    run_id, node_id = _meeting_run()
    _meeting_id, mtg = _bare_meeting(run_id, node_id, "roster")
    store.add_participant(mtg.meeting_id, role_key="facilitator", display_name="主持人",
                          mode="facilitator", system_prompt="你是主持人")
    protocol = discussion_module.DiscussionProtocol()
    suspended = asyncio.run(protocol.step_roster(mtg))
    gate_id = suspended.checkpoint["id"]
    decide_gate(gate_id=gate_id, action="approve")
    raised = {"on": True}
    original_ack = mtg.ack_decision

    def flaky_ack(decision=None, disposition=None):
        if raised["on"]:
            raised["on"] = False                   # 旧实现在这里单独 ack：中间被杀决定就滞留
            raise asyncio.CancelledError()
        return original_ack(decision, disposition)

    monkeypatch.setattr(mtg, "ack_decision", flaky_ack)
    result = asyncio.run(protocol.step_roster(mtg))

    state = store.get_state(mtg.meeting_id)
    assert result.action == "advance"
    assert state.get("handled_gate_id") == gate_id
    assert state.get("roster_done") is True




def test_exchange_claim_is_salvaged_when_the_claim_number_is_lost(monkeypatch) -> None:
    """主张落盘、认领编号还没写上时被杀：重入按发言行找回，不再重复同一段主张。"""
    install(monkeypatch, _replies(), None)
    run_id, node_id = _meeting_run()
    armed = {"on": True}
    from app.meeting import calls as calls_module

    original_emit = calls_module.emit

    async def flaky_emit(run_id_, type_, node_id_, payload):
        if armed["on"] and type_ == "meeting_turn_finished" and payload.get("kind") == "claim":
            armed["on"] = False                    # 第一条主张已落盘：进程在这里被杀
            raise asyncio.CancelledError()
        return await original_emit(run_id_, type_, node_id_, payload)

    monkeypatch.setattr(calls_module, "emit", flaky_emit)
    with pytest.raises(asyncio.CancelledError):
        drive(run_id, node_id, _approve, idea=IDEA)

    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    result = drive(run_id, node_id, _approve, idea=IDEA)

    assert result["done"] is True
    kinds = Counter(call["kind"] for call in seen)
    assert kinds["claim"] == 1 and kinds["challenge"] == 2 and kinds["respond"] == 2


def test_draft_speak_is_salvaged_after_a_crash(monkeypatch) -> None:
    """成稿发言落盘后、方案落库前被杀：重入按认领找回发言，不再重复调用模型。"""
    install(monkeypatch, _replies(), None)
    run_id, node_id = _meeting_run()
    armed = {"on": True}
    from app.meeting import calls as calls_module

    original_emit = calls_module.emit

    async def flaky_emit(run_id_, type_, node_id_, payload):
        if armed["on"] and type_ == "meeting_turn_finished" and payload.get("kind") == "proposal":
            armed["on"] = False                    # 成稿已落盘：进程在这里被杀
            raise asyncio.CancelledError()
        return await original_emit(run_id_, type_, node_id_, payload)

    monkeypatch.setattr(calls_module, "emit", flaky_emit)
    with pytest.raises(asyncio.CancelledError):
        drive(run_id, node_id, _approve, idea=IDEA)

    seen: list[dict] = []
    install(monkeypatch, _replies(), seen)
    result = drive(run_id, node_id, _approve, idea=IDEA)

    assert result["done"] is True
    assert Counter(call["kind"] for call in seen)["proposal"] == 0


def test_check_revision_is_salvaged_after_a_crash(monkeypatch) -> None:
    """复核修订发言落盘后被杀：重入按认领找回，不再重复调用模型。"""
    install(monkeypatch, _replies(proposal_check=CHECK_ISSUE), None)
    run_id, node_id = _meeting_run()
    armed = {"on": True}
    from app.meeting import calls as calls_module

    original_emit = calls_module.emit

    async def flaky_emit(run_id_, type_, node_id_, payload):
        if armed["on"] and type_ == "meeting_turn_finished" \
                and payload.get("kind") == "proposal_revise":
            armed["on"] = False                    # 修订已落盘：进程在这里被杀
            raise asyncio.CancelledError()
        return await original_emit(run_id_, type_, node_id_, payload)

    monkeypatch.setattr(calls_module, "emit", flaky_emit)
    with pytest.raises(asyncio.CancelledError):
        drive(run_id, node_id, _approve, idea=IDEA)

    seen: list[dict] = []
    install(monkeypatch, _replies(proposal_check=CHECK_ISSUE), seen)
    result = drive(run_id, node_id, _approve, idea=IDEA)

    assert result["done"] is True
    assert Counter(call["kind"] for call in seen)["proposal_revise"] == 0

