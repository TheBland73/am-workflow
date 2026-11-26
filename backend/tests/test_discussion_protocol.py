"""讨论协议：人工确认点、程序结算、停滞退让、主持人裁决、复议、复核、断点续跑。"""
from __future__ import annotations

from collections import Counter

import pytest

from app.artifacts.store import parse_front_matter, read_artifact, write_artifact
from app.db.base import init_db, session_scope
from app.db.models import Gate, Node, Run, Workflow
from app.engine.events import load_events
from app.engine.feedback import with_feedback
from app.engine.gates import get_gate, request_gate
from app.engine.params import apply_run_config
from app.engine.runners import NodeFailure
from app.engine.templates import instantiate_nodes, seed_defaults, workflow_definition
from app.meeting import store
from tests.meeting_fakes import drive, install

IDEA = "给小团队做一个反馈收集工具"
_counter = iter(range(1, 1000))

BRIEF = ('{"brief": "目标：让小团队收集并整理反馈。用户：团队负责人。约束：两周内可用。'
         '假设：单机部署。", "questions": ["需要导出哪些格式？"]}')
ANSWER = '{"answers": [{"text": "需要导出周报", "source": ""}]}'
ROSTER = ('{"participants": [{"role_key": "architect", "reason": "把握技术路线"}, '
          '{"role_key": "ux", "reason": "把关核心任务路径"}]}')
ROSTER_ONE = '{"participants": [{"role_key": "architect", "reason": "唯一参会者"}]}'
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
    assert "## 决策点账本" in parse_front_matter(read_artifact(run_id, "discussion"))[1]
    kinds = Counter(call["kind"] for call in seen)
    assert kinds["position"] == 2 and kinds["claim"] == 4 and kinds["exchange_summary"] == 2
    types = [event["type"] for event in load_events(run_id)]
    assert "meeting_ledger_updated" in types and "meeting_turn_finished" in types


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
    seen: list[dict] = []
    install(monkeypatch, _replies(restance=_split), seen)
    run_id, node_id = _meeting_run({"params": {"discussion": {"closure_rule": "facilitator_rules"}}})

    result = drive(run_id, node_id, _approve, idea=IDEA)

    assert result["done"] is True and _ledger(node_id)["D2"]["status"] == "ruled"
    assert [call["kind"] for call in seen].count("ruling") == 1
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


def test_a_fixed_headcount_is_enforced(monkeypatch) -> None:
    install(monkeypatch, _replies())                         # 主持人提了 2 名，用户要的是 3 名
    run_id, node_id = _meeting_run({"rosters": {"participants": {"count": 3}}})
    with pytest.raises(NodeFailure, match="固定人数"):
        drive(run_id, node_id, _approve, idea=IDEA)


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
    prompts = [call["text"] for call in seen if call["kind"] == "proposal"]
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
