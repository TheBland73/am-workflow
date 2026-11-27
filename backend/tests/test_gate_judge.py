"""确认点的模型判定：程序自动通过、模型先写要点再判、前置检查、连续驳回上限、
无效转人工、通过时选去向、把挂起的确认点交给模型判。"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.artifacts.store import parse_front_matter, read_artifact, write_artifact
from app.db.base import init_db, session_scope
from app.db.models import (AgentTemplate, Gate, Meeting, MeetingParticipant, ModelEntry, Node,
                           NodeAttempt, ProviderConfig, Run, Workflow, new_run_id)
from app.engine import judge
from app.engine.events import load_events
from app.engine.gates import GateError, request_gate
from app.engine.orchestrator import orchestrator
from app.engine.params import apply_run_config
from app.engine.templates import instantiate_nodes, seed_defaults, workflow_definition
from app.main import app
from app.models.crypto import encrypt_key
from app.models.pool import ModelInfo
from tests.meeting_fakes import install

IDEA = "给小团队做一个反馈收集工具"
PROPOSAL = ("# 建设方案\n\nPROPOSAL-MARKER 目标：两周内可用。\n\n"
            "## 范围\n\n收集、整理、导出。\n\n## 风险\n\n范围蔓延。\n")
REVIEW_REPORT = "# 评审报告\n\nREPORT-MARKER\n\n结论：接受。\n"
POINTS = json.dumps({"points": ["目标是否明确", "范围是否写明", "决策点是否有结论"]},
                    ensure_ascii=False)
APPROVE = json.dumps({"verdict": "approve", "reasons": ["逐条符合判据"], "concerns": [],
                      "feedback": ""}, ensure_ascii=False)
REJECT = json.dumps({"verdict": "reject", "reasons": ["方案与账本不符"],
                     "concerns": ["D2 没有落点"], "feedback": "按账本结论改写方案"}, ensure_ascii=False)

_counter = iter(range(1, 1000))


# ---------------- 夹具 ----------------
def _make_run(config: dict | None = None) -> str:
    init_db()
    seed_defaults()
    run_id = new_run_id()
    with session_scope() as s:
        wf = s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").one()
        s.add(Run(id=run_id, workflow_id=wf.id, status="running", idea_text=IDEA,
                  config_json=config or {}))
        s.flush()
    definition = workflow_definition("sdlc_v1")
    instantiate_nodes(run_id, definition)
    apply_run_config(run_id, definition, config or {})
    return run_id


def _seed_model(name: str = "mock-fast", *, bind_judge: bool = True,
                context_window: int = 32768, max_output_tokens: int = 4096) -> int:
    init_db()
    seed_defaults()
    with session_scope() as s:
        provider = ProviderConfig(
            name=f"判定供应商-{name}-{next(_counter)}", protocol="openai_chat",
            base_url="http://127.0.0.1:9/v1", api_key_enc=encrypt_key("sk-test-1234567890"))
        s.add(provider)
        s.flush()
        model = ModelEntry(provider_id=provider.id, model_name=name, display_name=name,
                           context_window=context_window, max_output_tokens=max_output_tokens)
        s.add(model)
        s.flush()
        if bind_judge:
            row = s.query(AgentTemplate).filter(AgentTemplate.role_key == "judge").one()
            row.default_model_id = model.id
        return model.id


def _bind_judge(model_id: int) -> None:
    with session_scope() as s:
        row = s.query(AgentTemplate).filter(AgentTemplate.role_key == "judge").one()
        row.default_model_id = model_id


def _artifact(run_id: str, kind: str, body: str) -> None:
    write_artifact(run_id, kind, body, created_by="test", inputs=[], status="draft")


def _node(run_id: str, key: str) -> dict:
    _, nodes = orchestrator._load(run_id)
    return next(node for node in nodes if node["node_key"] == key)


def _node_id(run_id: str, key: str) -> int:
    return _node(run_id, key)["id"]


def _status(run_id: str, key: str) -> str:
    return _node(run_id, key)["status"]


def _config(run_id: str, key: str) -> dict:
    return dict(_node(run_id, key)["config"])


def _run_node(run_id: str, key: str) -> None:
    node = _node(run_id, key)
    asyncio.run(orchestrator._run_node(run_id, node))


def _meeting(run_id: str, node_key: str, protocol: str, *, state: dict | None = None,
             round_no: int = 1, participants: list[dict] | None = None) -> int:
    node_id = _node_id(run_id, node_key)
    with session_scope() as s:
        row = Meeting(run_id=run_id, node_id=node_id, node_key=node_key, protocol=protocol,
                      status="completed", phase="done", round=round_no,
                      charter_json={}, config_json={"params": {}}, state_json=state or {})
        s.add(row)
        s.flush()
        for index, item in enumerate(participants or []):
            s.add(MeetingParticipant(
                meeting_id=row.id, role_key=item.get("role_key"),
                display_name=item["display_name"], mode=item.get("mode", "participant"),
                system_prompt="", model_id=item.get("model_id"), position=index + 1))
        return row.id


def _events(run_id: str, type_: str) -> list[dict]:
    return [event for event in load_events(run_id) if event["type"] == type_]


def _open_gates(run_id: str) -> list[dict]:
    with session_scope() as s:
        rows = (s.query(Gate)
                .filter(Gate.run_id == run_id, Gate.status == "open")
                .order_by(Gate.id.asc()).all())
        return [{"id": row.id, "node_id": row.node_id, "kind": row.kind} for row in rows]


def _gate(gate_id: int) -> dict | None:
    with session_scope() as s:
        row = s.get(Gate, gate_id)
        if row is None:
            return None
        return {"id": row.id, "status": row.status, "decided_by": row.decided_by,
                "decision": dict(row.decision_json or {}), "kind": row.kind}


def _no_kick(monkeypatch) -> None:
    """判定会落决定并 kick 调度器；单元测试只驱动一个节点，不让整场运行跑起来。"""
    monkeypatch.setattr(orchestrator, "kick", lambda run_id: None)


def _wait(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# ---------------- 程序自动通过 ----------------
def test_fallback_level_auto_passes_proposal(monkeypatch) -> None:
    seen: list[dict] = []
    install(monkeypatch, {}, seen)
    _no_kick(monkeypatch)
    run_id = _make_run({"automation": {"level": "L3"}})
    _artifact(run_id, "proposal", PROPOSAL)

    _run_node(run_id, "gate_proposal")

    assert seen == []                                  # 不调模型
    assert _status(run_id, "gate_proposal") == "done"
    assert _open_gates(run_id) == []                   # 也不开门
    decided = _events(run_id, "checkpoint_decided")
    assert decided and decided[-1]["payload"]["decided_by"] == "program"
    meta, _body = parse_front_matter(read_artifact(run_id, "proposal"))
    assert meta["status"] == "approved"


# ---------------- 模型判定 ----------------
def test_model_approve_writes_points_then_judges_the_full_artifact(monkeypatch) -> None:
    seen: list[dict] = []
    install(monkeypatch, {"gate_points": POINTS, "gate_verdict": APPROVE}, seen)
    _no_kick(monkeypatch)
    _seed_model()
    run_id = _make_run({"automation": {"level": "L3"}})
    _artifact(run_id, "proposal", PROPOSAL)
    _artifact(run_id, "review", REVIEW_REPORT)
    _meeting(run_id, "review", "review", state={"verdict": "accept", "precheck_ok": True})

    gate_node_id = _node_id(run_id, "gate_review")
    _run_node(run_id, "gate_review")

    kinds = [call["kind"] for call in seen]
    assert kinds == ["gate_points", "gate_verdict"]
    first, second = seen[0]["text"], seen[1]["text"]
    assert "PROPOSAL-MARKER" not in first              # 第一次不给工件
    assert "目标是否明确" in second                     # 带上自己写的要点
    assert "PROPOSAL-MARKER" in second                 # 工件全文
    assert "接受" in second and "REPORT-MARKER" in second   # 上游会议的结论与报告要点
    with session_scope() as s:
        gate = (s.query(Gate).filter(Gate.node_id == gate_node_id)
                .order_by(Gate.id.desc()).first())
        assert gate.status == "approved"
        assert gate.decided_by.startswith("model:")
        assert gate.decision_json["data"]["rubric"]
        # 这次判定留下一条执行记录（用量记在它上面）
        attempt = (s.query(NodeAttempt).filter(NodeAttempt.node_id == gate_node_id)
                   .order_by(NodeAttempt.id.desc()).first())
        assert attempt is not None and attempt.status == "done"
    assert _status(run_id, "gate_review") == "done"
    _, nodes = orchestrator._load(run_id)
    requirement = next(node for node in nodes if node["node_key"] == "requirement")
    assert requirement["status"] == "pending" and orchestrator._deps_done(requirement, nodes)
    with session_scope() as s:
        assert s.get(Run, run_id).status == "running"


def test_a_large_artifact_is_judged_in_full_when_the_model_window_allows_it(monkeypatch) -> None:
    # 固定 6000 token 的上限会把大窗口模型装得下的工件截断，"被审工件全文"就成了空话
    seen: list[dict] = []
    install(monkeypatch, {"gate_points": POINTS, "gate_verdict": APPROVE}, seen)
    _no_kick(monkeypatch)
    _seed_model(context_window=200_000, max_output_tokens=8192)
    run_id = _make_run({"automation": {"level": "L3"}})
    _artifact(run_id, "proposal", "# 建设方案\n\n" + "字" * 30_000 + "\nTAIL-MARKER\n")
    _artifact(run_id, "review", REVIEW_REPORT)
    _meeting(run_id, "review", "review", state={"verdict": "accept", "precheck_ok": True})

    _run_node(run_id, "gate_review")

    text = seen[1]["text"]
    assert "TAIL-MARKER" in text
    assert "工件超出判定预算" not in text


def test_a_small_window_truncates_the_artifact_to_the_judge_budget(monkeypatch) -> None:
    # 窗口只有 4k 时，6000 token 的工件比整个输入预算还大：按模型预算收紧，并显式注明截断
    seen: list[dict] = []
    install(monkeypatch, {"gate_points": POINTS, "gate_verdict": APPROVE}, seen)
    _no_kick(monkeypatch)
    _seed_model(context_window=4096, max_output_tokens=1024)
    run_id = _make_run({"automation": {"level": "L3"}})
    _artifact(run_id, "proposal", "# 建设方案\n\n" + "字" * 4000 + "\nTAIL-MARKER\n")
    _artifact(run_id, "review", REVIEW_REPORT)
    _meeting(run_id, "review", "review", state={"verdict": "accept", "precheck_ok": True})

    _run_node(run_id, "gate_review")

    text = seen[1]["text"]
    assert "TAIL-MARKER" not in text
    assert "工件超出判定预算" in text


def test_model_reject_resets_the_target_and_counts(monkeypatch) -> None:
    seen: list[dict] = []
    install(monkeypatch, {"gate_points": POINTS, "gate_verdict": REJECT}, seen)
    _no_kick(monkeypatch)
    _seed_model()
    run_id = _make_run({"automation": {"level": "L3"}})
    _artifact(run_id, "proposal", PROPOSAL)
    _meeting(run_id, "review", "review", state={"verdict": "accept", "precheck_ok": True})

    gate_node_id = _node_id(run_id, "gate_review")
    _run_node(run_id, "gate_review")

    assert _status(run_id, "review") == "pending"
    assert _config(run_id, "review")["feedback"] == "按账本结论改写方案"
    assert _config(run_id, "gate_review")["auto_rejects"] == 1
    with session_scope() as s:
        gate = (s.query(Gate).filter(Gate.node_id == gate_node_id)
                .order_by(Gate.id.desc()).first())
        assert gate.status == "rejected"
        assert gate.decision_json["data"]["concerns"] == ["D2 没有落点"]

    # 人做出任何决定都清零
    gate = request_gate(run_id, gate_node_id)
    asyncio.run(orchestrator.decide_gate(gate_id=gate["id"], action="approve",
                                        decided_by="human"))
    assert "auto_rejects" not in _config(run_id, "gate_review")


def test_consecutive_rejects_hand_the_gate_to_a_human(monkeypatch) -> None:
    seen: list[dict] = []
    install(monkeypatch, {"gate_points": POINTS, "gate_verdict": REJECT}, seen)
    _no_kick(monkeypatch)
    _seed_model()
    run_id = _make_run({"automation": {"level": "L3"}})
    _artifact(run_id, "proposal", PROPOSAL)
    _meeting(run_id, "review", "review", state={"verdict": "accept", "precheck_ok": True})

    for _ in range(2):
        _run_node(run_id, "gate_review")
    assert _config(run_id, "gate_review")["auto_rejects"] == 2
    assert len(seen) == 4                              # 两次判定，各两次调用

    _run_node(run_id, "gate_review")

    assert _status(run_id, "gate_review") == "waiting_human"
    assert len(_open_gates(run_id)) == 1
    hint = _config(run_id, "gate_review")["hint"]
    assert "连续驳回 2 次" in hint and "上限" in hint
    assert len(seen) == 4                              # 第三次没调模型


def test_precheck_failure_rejects_without_calling_the_model(monkeypatch) -> None:
    seen: list[dict] = []
    install(monkeypatch, {}, seen)
    _no_kick(monkeypatch)
    _seed_model()
    run_id = _make_run({"automation": {"level": "L3"}})
    _artifact(run_id, "proposal", PROPOSAL)
    _meeting(run_id, "review", "review", state={
        "verdict": "return", "precheck_ok": False,
        "precheck_reason": "评审仍有阻塞意见没有处理"})

    _run_node(run_id, "gate_review")

    assert seen == []
    assert _status(run_id, "review") == "pending"
    assert _config(run_id, "review")["feedback"] == "评审仍有阻塞意见没有处理"


@pytest.mark.parametrize("reply,reason", [
    ("这不是 JSON", "JSON"),
    (json.dumps({"verdict": "abstain", "reasons": [], "concerns": [], "feedback": ""}),
     "弃权"),
    (json.dumps({"verdict": "reject", "reasons": ["不符合"], "concerns": [], "feedback": ""}),
     "反馈"),
])
def test_invalid_output_leaves_the_gate_to_a_human(monkeypatch, reply, reason) -> None:
    seen: list[dict] = []
    install(monkeypatch, {"gate_points": POINTS, "gate_verdict": reply}, seen)
    _no_kick(monkeypatch)
    _seed_model()
    run_id = _make_run({"automation": {"level": "L3"}})
    _artifact(run_id, "proposal", PROPOSAL)

    _run_node(run_id, "gate_review")

    assert _status(run_id, "gate_review") == "waiting_human"
    assert len(_open_gates(run_id)) == 1
    config = _config(run_id, "gate_review")
    assert reason in config["hint"]
    with session_scope() as s:
        gate = s.query(Gate).filter(Gate.node_id == _node_id(run_id, "gate_review")).one()
        assert not (gate.decision_json or {}).get("action")


# ---------------- 通过时的去向 ----------------
def test_approve_with_route_skips_the_review_nodes(monkeypatch) -> None:
    _no_kick(monkeypatch)
    run_id = _make_run()
    gate = request_gate(run_id, _node_id(run_id, "gate_proposal"))

    result = asyncio.run(orchestrator.decide_gate(
        gate_id=gate["id"], action="approve", data={"route": "skip_review"}))

    assert result["skipped_nodes"] == ["review", "gate_review"]
    assert _status(run_id, "review") == "skipped"
    assert _status(run_id, "gate_review") == "skipped"
    skipped = [event for event in _events(run_id, "node_finished")
               if event["payload"].get("skipped")]
    assert {event["payload"]["node_key"] for event in skipped} == {"review", "gate_review"}
    assert all(event["payload"]["route"] == "skip_review" for event in skipped)
    assert _status(run_id, "requirement") == "pending"


def test_unknown_route_is_refused_and_the_gate_stays_open(monkeypatch) -> None:
    _no_kick(monkeypatch)
    run_id = _make_run()
    gate = request_gate(run_id, _node_id(run_id, "gate_proposal"))

    with pytest.raises(GateError, match="未知的去向"):
        asyncio.run(orchestrator.decide_gate(
            gate_id=gate["id"], action="approve", data={"route": "nope"}))

    assert _gate(gate["id"])["status"] == "open"


# ---------------- 交给模型判 ----------------
def test_delegate_an_artifact_gate_to_the_model(monkeypatch) -> None:
    seen: list[dict] = []
    install(monkeypatch, {"gate_points": POINTS, "gate_verdict": APPROVE}, seen)
    _no_kick(monkeypatch)
    _seed_model()
    run_id = _make_run({"automation": {"level": "L3"}})
    _artifact(run_id, "proposal", PROPOSAL)
    _meeting(run_id, "review", "review", state={"verdict": "accept", "precheck_ok": True})
    gate = request_gate(run_id, _node_id(run_id, "gate_review"))

    with TestClient(app) as client:
        response = client.post(f"/api/gates/{gate['id']}/delegate")
        assert response.status_code == 202, response.text
        assert _wait(lambda: (_gate(gate["id"]) or {}).get("status") != "open"), \
            "模型判定没有在预期时间内落下决定"

    row = _gate(gate["id"])
    assert row["status"] == "approved" and row["decided_by"].startswith("model:")
    assert _status(run_id, "gate_review") == "done"


def test_delegate_meeting_checkpoints_lands_the_fallback_decision() -> None:
    run_id = _make_run()
    node_id = _node_id(run_id, "discuss")
    with session_scope() as s:
        node = s.get(Node, node_id)
        node.status = "waiting_human"
        run = s.get(Run, run_id)
        run.status = "waiting_human"
        gate = Gate(run_id=run_id, node_id=node_id, status="open", kind="consensus",
                    payload_json={"title": "轮次共识"})
        s.add(gate)
        s.flush()
        gate_id = gate.id

    with TestClient(app) as client:
        response = client.post(f"/api/gates/{gate_id}/delegate")
        assert response.status_code == 202, response.text
        row = _gate(gate_id)
        assert row["status"] == "approved"
        assert row["decided_by"] == "user-delegated"
        # 会议从 decision_json 的 data 里读决定（与人工路径一致）
        from app.meeting.checkpoints import get_decision

        assert get_decision(gate_id)["data"]["option"] == "delegate"
        # 已经决定过的门不能再交
        assert client.post(f"/api/gates/{gate_id}/delegate").status_code == 400

    # 会议节点上不带类型的安全网确认点不能交
    with session_scope() as s:
        node = s.get(Node, node_id)
        node.status = "waiting_human"
        gate = Gate(run_id=run_id, node_id=node_id, status="open", kind=None,
                    payload_json={"title": "失败转人工"})
        s.add(gate)
        s.flush()
        plain_gate_id = gate.id
    with TestClient(app) as client:
        assert client.post(f"/api/gates/{plain_gate_id}/delegate").status_code == 400


# ---------------- 同一家族的提示 ----------------
def test_same_family_hint_is_shown_on_the_gate() -> None:
    model_id = _seed_model("gpt-4o")
    _bind_judge(model_id)
    run_id = _make_run()
    meeting_id = _meeting(run_id, "review", "review", participants=[
        {"role_key": "architect", "display_name": "架构师", "model_id": model_id}])

    asyncio.run(orchestrator._open_human_gate(run_id, _node(run_id, "gate_review")))

    gate = _open_gates(run_id)[0]
    with session_scope() as s:
        from app.engine.gates import gate_payload
        description = gate_payload(s, s.get(Gate, gate["id"]))["description"]
    assert "同一家族" in description and "换一家的模型" in description


def test_different_family_has_no_hint() -> None:
    model_id = _seed_model("gpt-4o", bind_judge=False)
    other_id = _seed_model("deepseek-chat")
    _bind_judge(other_id)
    run_id = _make_run()
    _meeting(run_id, "review", "review", participants=[
        {"role_key": "architect", "display_name": "架构师", "model_id": model_id}])

    asyncio.run(orchestrator._open_human_gate(run_id, _node(run_id, "gate_review")))

    gate = _open_gates(run_id)[0]
    with session_scope() as s:
        from app.engine.gates import gate_payload
        description = gate_payload(s, s.get(Gate, gate["id"]))["description"]
    assert "同一家族" not in description


# ---------------- 异构默认（详设 §5.3） ----------------
def _info(model_id: int, name: str) -> ModelInfo:
    return ModelInfo(id=model_id, provider_id=1, model_name=name, display_name=name)


def _judge_gate_review(monkeypatch, *, judge_model: str) -> tuple[str, dict]:
    """裁决模型绑定 judge_model，参会者全用 gpt-4o；由模型判定评审裁决点，返回 (运行 id, 决定的 data)。"""
    seen: list[dict] = []
    install(monkeypatch, {"gate_points": POINTS, "gate_verdict": APPROVE}, seen)
    _no_kick(monkeypatch)
    participant_model = _seed_model("gpt-4o", bind_judge=False)
    _seed_model(judge_model)                               # 这一个绑定给 judge
    run_id = _make_run({"automation": {"level": "L3"}})
    _artifact(run_id, "proposal", PROPOSAL)
    _artifact(run_id, "review", REVIEW_REPORT)
    _meeting(run_id, "review", "review", state={"verdict": "accept", "precheck_ok": True},
             participants=[{"role_key": "architect", "display_name": "架构师",
                            "model_id": participant_model}])

    _run_node(run_id, "gate_review")

    with session_scope() as s:
        gate = (s.query(Gate).filter(Gate.node_id == _node_id(run_id, "gate_review"))
                .order_by(Gate.id.desc()).first())
        assert gate.status == "approved"                    # 两种情况都不阻断：决定照常生效
        return run_id, dict(gate.decision_json["data"])


def test_judge_prefers_a_candidate_from_another_family() -> None:
    # 裁决模型没显式绑定时：只要有一个候选与全部参会者都不同家族，就选它，而不是排在最前的那个
    gpt, deepseek = _info(1, "gpt-4o"), _info(2, "deepseek-chat")
    assert judge.choose_judge_model([gpt, deepseek], ["gpt-4o", "gpt-4o-mini"]) is deepseek


def test_judge_falls_back_to_the_first_candidate_when_all_share_the_family() -> None:
    gpt, mini = _info(1, "gpt-4o"), _info(2, "gpt-4o-mini")
    assert judge.choose_judge_model([gpt, mini], ["gpt-4o"]) is gpt
    assert judge.choose_judge_model([], ["gpt-4o"]) is None


def test_explicitly_bound_judge_is_not_replaced() -> None:
    # 用户给 judge 角色绑了模型就照用：即使它与全部参会者同家族，也只标注、不擅自换人
    bound_id = _seed_model("gpt-4o")
    _seed_model("deepseek-chat", bind_judge=False)

    model = judge.pick_judge_model(["gpt-4o"])

    assert model is not None and model.id == bound_id


def test_same_family_model_decision_is_flagged_for_review(monkeypatch) -> None:
    run_id, data = _judge_gate_review(monkeypatch, judge_model="gpt-4o")

    assert data["heterogeneity"] == "same_family"
    assert "同一家族" in data["heterogeneity_hint"]
    decided = _events(run_id, "checkpoint_decided")
    assert decided[-1]["payload"]["decided_by"].startswith("model:")
    assert decided[-1]["payload"]["decision"]["heterogeneity"] == "same_family"
    assert any("同一家族" in str(event["payload"].get("message"))
               for event in _events(run_id, "node_log"))


def test_different_family_model_decision_is_not_flagged(monkeypatch) -> None:
    run_id, data = _judge_gate_review(monkeypatch, judge_model="deepseek-chat")

    assert "heterogeneity" not in data
    assert not any("同一家族" in str(event["payload"].get("message"))
                   for event in _events(run_id, "node_log"))


# ---------------- 设计之后的门 ----------------
def test_design_gate_uses_the_generic_artifact_rubric(monkeypatch) -> None:
    seen: list[dict] = []
    install(monkeypatch, {"gate_points": POINTS, "gate_verdict": APPROVE}, seen)
    _no_kick(monkeypatch)
    _seed_model()
    run_id = _make_run({"automation": {"checkpoints": {"gate_design": "model"}}})
    _artifact(run_id, "design", "# 设计规格\n\nDESIGN-MARKER 总体架构。\n")

    _run_node(run_id, "gate_design")

    assert _status(run_id, "gate_design") == "done"
    assert [call["kind"] for call in seen] == ["gate_points", "gate_verdict"]
    assert all("判断这份工件是否完整" in call["text"] for call in seen)
    assert "DESIGN-MARKER" in seen[1]["text"]
