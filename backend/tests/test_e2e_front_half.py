"""前半段的离线端到端：讨论 → 方案 → 评审（决策级回流 + 中断恢复）→ 需求 → 人工驳回 → 确认通过。

模型调用在进程内替换成脚本化的回复（按调用标识路由，见 scripts/mock_provider.py），
编排器与会议运行时都是真的。同一个运行里必须发生四件事：

1. 第一轮评审回一条决策级阻塞意见，评审退回、讨论会议复议、方案出第 2 版、评审第二轮通过；
2. 第二轮评审第一次汇总时抛出 CancelledError，运行按"被取消"处理；随后在应用自己的事件
   循环里调用恢复流程，评审会议从汇总处继续并完成；
3. 需求确认确认点上由人驳回，确认会议受理后不通过、回流需求分析师，需求规格出第 2 版；
4. 需求确认通过，设计节点开始执行。

另有一个仅兜底等级（L3）的运行：全程不出现人工确认点，直接跑到设计节点开始执行。
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time

from fastapi.testclient import TestClient

import scripts.mock_provider as mock_provider
from app.db.base import init_db, session_scope
from app.db.models import (Meeting, ModelEntry, Node, NodeAttempt, ProviderConfig, Run)
from app.engine import agent_loop
from app.engine.orchestrator import Orchestrator, orchestrator
from app.engine.templates import seed_defaults
from app.main import app
from app.models.crypto import encrypt_key
from app.models.types import Message, Usage

IDEA = ("做一个面向小团队的「客户反馈收集与归类」内部工具：成员提交反馈，"
        "系统按主题聚类并给出优先级建议，支持导出周报。")
CALL_TAG = re.compile(r"【调用类型：([a-z_]+)】")
PROVIDER_NAME = "离线端到端 Mock"
MODEL_NAME = "mock-e2e"

# 默认布局：两个参会者即两名评审者，评审与增量复审各两次调用
CONFIG = {
    "rosters": {"participants": {"count": 2}, "reviewers": {"count": 2}},
}


# ---------------------------------------------------------------- 夹具
def _seed_model() -> None:
    init_db()
    seed_defaults()
    with session_scope() as s:
        provider = s.query(ProviderConfig).filter(ProviderConfig.name == PROVIDER_NAME).first()
        if provider is None:
            provider = ProviderConfig(
                name=PROVIDER_NAME, protocol="openai_chat",
                base_url="http://127.0.0.1:9/v1",
                api_key_enc=encrypt_key("sk-e2e-0123456789abcdef"))
            s.add(provider)
            s.flush()
        model = (s.query(ModelEntry)
                 .filter(ModelEntry.provider_id == provider.id,
                         ModelEntry.model_name == MODEL_NAME)
                 .first())
        if model is None:
            s.add(ModelEntry(provider_id=provider.id, model_name=MODEL_NAME,
                             display_name="Mock E2E", context_window=32768,
                             max_output_tokens=4096, cap_tools=True, cap_json=True))


class ScriptedCalls:
    """按调用标识查表；评审的两种情形（回流、中断）由构造参数决定。"""

    def __init__(self, *, inject_review_issue: bool, cancel_second_merge: bool) -> None:
        self.inject_review_issue = inject_review_issue
        self.cancel_second_merge = cancel_second_merge
        self.review_calls = 0
        self.review_delta_calls = 0
        self.merge_calls = 0
        self.cancelled = threading.Event()

    async def stream(self, model, messages, schemas, params, *, run_id, node_id,
                     token_extra=None):
        dicts = [{"role": message.role, "content": message.content or ""}
                 for message in messages]
        text = "\n".join(item["content"] for item in dicts)
        tagged = list(CALL_TAG.finditer(text))
        kind = tagged[-1].group(1) if tagged else ""
        if kind == "review":
            self.review_calls += 1
            content = (self._review(text) if self.inject_review_issue
                       else mock_provider._meeting_reply(kind, text))
        elif kind == "review_delta":
            self.review_delta_calls += 1
            content = (self._review_delta(text) if self.inject_review_issue
                       else mock_provider._meeting_reply(kind, text))
        elif kind == "review_merge":
            self.merge_calls += 1
            if self.cancel_second_merge and self.merge_calls == 2:
                # 模拟进程在第二轮评审汇总时被取消：编排器按"运行被取消"处理
                assert "上一轮的决策级意见" in text, "取消点不在第二轮评审的汇总上"
                self.cancelled.set()
                raise asyncio.CancelledError()
            content = self._merge(text)
        elif kind == "reopen":
            content = json.dumps({"points": [{"index": "D1"}],
                                  "note": "按评审意见重开 D1"}, ensure_ascii=False)
        elif kind == "author_response":
            # 只回应当前这一步的意见：章程里还带着上一轮的意见，指令里也有示例编号
            listing = text.split("## 需要回应的评审意见", 1)[-1]
            ids = list(dict.fromkeys(re.findall(r"^- (I\d+)\b", listing, re.M)))
            content = json.dumps({"responses": [
                {"id": issue_id, "accepted": True, "reason": "采纳", "ask_requester": []}
                for issue_id in ids]}, ensure_ascii=False)
        else:
            content, _call = mock_provider._pick(dicts, schemas)
        return Message(role="assistant", content=content), Usage(120, 60, 40)

    def _review(self, text: str) -> str:
        # 两名评审者并行发言：架构师提一条决策级意见，用户体验不提
        persona = mock_provider._persona(text)
        if not persona:
            raise AssertionError("评审提示里没有角色人设，无法判断是哪名评审者")
        if "架构" not in persona:
            return json.dumps({"strengths": ["方案结构完整"], "issues": []},
                              ensure_ascii=False)
        return json.dumps({"strengths": ["方案覆盖了主要决策点"], "issues": [{
            "severity": "major", "category": "decision", "location": "D1",
            "problem": "方案把首版入口写成桌面版，与 D1 已了结的结论相反。",
            "suggestion": "按 D1 的结论改为网页版最小闭环，并写明依据。"}]},
            ensure_ascii=False)

    def _review_delta(self, text: str) -> str:
        # 只看"上一轮的决策级意见"这一段：指令里的示例编号不算数
        previous = text.split("## 上一轮的决策级意见", 1)[-1]
        ids = list(dict.fromkeys(re.findall(r"^- (I\d+)\b", previous, re.M)))
        if not ids:
            raise AssertionError("增量复审提示里没有上一轮的决策级意见")
        resolved = [{"id": issue_id, "handled": True, "note": "已按意见修改"}
                    for issue_id in ids]
        if "架构" not in mock_provider._persona(text):
            return json.dumps({"strengths": [], "issues": [], "resolved": resolved},
                              ensure_ascii=False)
        # 一条不到阻塞线的决策级意见：评审仍判"接受"，但汇总这一步要真的发生
        return json.dumps({"strengths": [], "resolved": resolved, "issues": [{
            "severity": "minor", "category": "decision", "location": "D2",
            "problem": "D2 的表述与账本结论略有出入，建议复核。",
            "suggestion": "核对 D2 的结论后保持或调整。"}]}, ensure_ascii=False)

    def _merge(self, text: str) -> str:
        if self.merge_calls == 1:
            # 汇总提示里必须带着评审者提的意见，否则合并这一环等于没验
            assert "桌面版" in text, "评审意见没有进入汇总提示"
            return json.dumps({"issues": [{
                "severity": "major", "category": "decision", "location": "D1",
                "problem": "方案把首版入口写成桌面版，与 D1 已了结的结论相反。",
                "suggestion": "按 D1 的结论改为网页版最小闭环，并写明依据。",
                "raised_by": ["架构师"], "disputed": False}]}, ensure_ascii=False)
        # 增量复审的意见同样要先进入待汇总清单
        assert "D2 的表述" in text, "增量复审意见没有进入汇总提示"
        return json.dumps({"issues": [{
            "severity": "minor", "category": "decision", "location": "D2",
            "problem": "D2 的表述与账本结论略有出入，建议复核。",
            "suggestion": "核对 D2 的结论后保持或调整。",
            "raised_by": ["架构师"], "disputed": False}]}, ensure_ascii=False)


def _install(monkeypatch, fake: ScriptedCalls) -> None:
    monkeypatch.setattr(agent_loop, "_call_stream", fake.stream)


# ---------------------------------------------------------------- 驱动
def _post_gate(client: TestClient, gate: dict, action: str, **body) -> None:
    body.setdefault("decided_by", "human")
    response = client.post(f"/api/gates/{gate['id']}/{action}", json=body)
    assert response.status_code == 200, response.text


def _snapshot(client: TestClient, run_id: str) -> dict:
    response = client.get(f"/api/runs/{run_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _answer_gate(client: TestClient, gate: dict, state: dict) -> None:
    kind = gate.get("kind") or ""
    node_key = gate.get("node_key") or ""
    if kind == "clarify":
        questions = (gate.get("payload") or {}).get("questions") or []
        _post_gate(client, gate, "approve",
                   data={"answers": ["以背景材料为准"] * max(1, len(questions))})
    elif kind == "roster":
        _post_gate(client, gate, "approve")
    elif kind == "consensus":
        _post_gate(client, gate, "approve", data={"option": "integrate"})
    elif kind == "escalation":
        _post_gate(client, gate, "approve", data={"option": "accept"})
    elif node_key in ("gate_proposal", "gate_review"):
        _post_gate(client, gate, "approve")
    elif node_key == "gate_requirement" and not state["rejected"]:
        state["rejected"] = True
        _post_gate(client, gate, "reject",
                   feedback="需求规格的 R1 缺少验收标准，请补充 Given/When/Then",
                   target_node="confirm")
    elif node_key == "gate_requirement":
        _post_gate(client, gate, "approve")
    else:
        raise AssertionError(f"未预期的确认点：{node_key}/{kind}：{gate}")


def _drive(client: TestClient, run_id: str, *, timeout: float,
           stop_at_design: bool = True, answer_gates: bool = True,
           cancelled: threading.Event | None = None,
           record: dict | None = None) -> dict:
    """轮询快照：按门的类型作答，直到设计节点开始执行（或中断信号出现）。"""
    state = {"rejected": False}
    deadline = time.monotonic() + timeout
    stuck = 0
    while time.monotonic() < deadline:
        if cancelled is not None and cancelled.is_set():
            return _snapshot(client, run_id)
        snap = _snapshot(client, run_id)
        run = snap["run"]
        if run["status"] in ("failed", "stopped"):
            raise AssertionError(f"运行在到达设计前结束：{run['status']} {run.get('error')}")
        if stop_at_design:
            design = next(node for node in snap["nodes"] if node["node_key"] == "design")
            if design["status"] != "pending" and design["attempts"] > 0:
                return snap
        open_gate = next((gate for gate in snap["gates"] if gate["status"] == "open"), None)
        if open_gate is not None:
            if answer_gates:
                if (open_gate.get("node_key") == "gate_review"
                        and record is not None and "review_version" not in record):
                    record["review_version"] = _version(snap, "review")
                _answer_gate(client, open_gate, state)
                continue
            # 仅兜底等级：模型判定可能短暂开门，但确认点不能一直开着
            time.sleep(0.1)
            snap2 = _snapshot(client, run_id)
            design = next(node for node in snap2["nodes"] if node["node_key"] == "design")
            if design["status"] != "pending" and design["attempts"] > 0:
                return snap2
            still_open = next((gate for gate in snap2["gates"] if gate["status"] == "open"), None)
            if still_open is not None and (still_open.get("node_key") or "") in (
                    "gate_review", "gate_requirement"):
                stuck += 1
                if stuck >= 100:                      # 约 10 秒仍是同一扇门：判定没有落定
                    raise AssertionError(f"模型判定的确认点一直没有关闭：{still_open}")
                continue
            if still_open is not None:
                raise AssertionError(
                    f"仅兜底等级的运行出现了人工确认点：{still_open}；"
                    f"design={design['status']}/{design['attempts']}")
            continue
        stuck = 0
        time.sleep(0.05)
    snap = _snapshot(client, run_id)
    open_gates = [gate for gate in snap["gates"] if gate["status"] == "open"]
    raise AssertionError(f"等待超时：status={snap['run']['status']} gates={open_gates}")


def _create_run(client: TestClient, config: dict) -> str:
    response = client.post("/api/runs", json={
        "idea": IDEA, "template_key": "sdlc_v1", "config": config,
    })
    assert response.status_code == 201, response.text
    return response.json()["run"]["id"]


def _events(client: TestClient, run_id: str) -> list[dict]:
    response = client.get(f"/api/runs/{run_id}/events/history")
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _version(snap: dict, kind: str) -> int:
    return max((item["version"] for item in snap["artifacts"] if item["kind"] == kind),
               default=0)


def _wait_until(predicate, *, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(message)


# ---------------------------------------------------------------- 用例
def test_offline_front_half_with_reroute_interrupt_and_rejection(monkeypatch) -> None:
    _seed_model()
    fake = ScriptedCalls(inject_review_issue=True, cancel_second_merge=True)
    _install(monkeypatch, fake)

    with TestClient(app) as client:
        run_id = _create_run(client, CONFIG)
        # 评审第二轮的第一次汇总抛出取消：先给出信号，等编排器把节点放回待调度
        _drive(client, run_id, timeout=60, stop_at_design=False,
               answer_gates=True, cancelled=fake.cancelled)

        def _settled() -> bool:
            snap = _snapshot(client, run_id)
            review = next(node for node in snap["nodes"] if node["node_key"] == "review")
            return review["status"] == "pending" and (
                not review["attempt"] or review["attempt"]["status"] != "running")

        _wait_until(_settled, timeout=10, message="中断后评审节点没有回到待调度")
        before = _snapshot(client, run_id)
        review_before = next(node for node in before["nodes"]
                             if node["node_key"] == "review")
        assert review_before["attempt"]["status"] == "failed", review_before["attempt"]
        assert "运行被取消" in (review_before["attempt"]["error"] or "")
        assert fake.merge_calls == 2, (
            f"取消后不应继续汇总（当前 {fake.merge_calls} 次）")

        # 恢复流程会拉起库里所有未结束的运行：先把别的运行停掉，只留下这一个
        with session_scope() as s:
            for row in s.query(Run).filter(
                    Run.status.in_(["created", "running", "waiting_human"]),
                    Run.id != run_id).all():
                row.status = "stopped"
        # 测试夹具替换的是实例上的 recover；这里在应用自己的事件循环里调用类上的原方法
        client.portal.call(Orchestrator.recover, orchestrator)

        record: dict = {}
        snap = _drive(client, run_id, timeout=180, stop_at_design=True,
                      answer_gates=True, record=record)
        stopped = client.post(f"/api/runs/{run_id}/stop")
        assert stopped.status_code == 200, stopped.text

        # ---- 五个必经事件 ----
        events = _events(client, run_id)
        types = [event["type"] for event in events]
        for required in ("meeting_suspended", "meeting_resumed", "review_verdict",
                         "node_rerouted", "run_recovered"):
            assert required in types, f"缺少事件 {required}：{sorted(set(types))}"
        recovered = next(event for event in events if event["type"] == "run_recovered")
        recovered_seq = recovered["seq"]
        assert recovered["payload"].get("status") == "running"
        assert recovered["payload"].get("reset_nodes") == 0

        # ---- 中断没有造成重复发言，也没有被当成普通失败重试 ----
        assert fake.review_calls == 2, f"评审发言调用 {fake.review_calls} 次"
        assert fake.review_delta_calls == 2, f"增量复审调用 {fake.review_delta_calls} 次"
        assert fake.merge_calls == 3, (
            f"评审汇总调用 {fake.merge_calls} 次（一次被取消、两次完成）")
        review_node_id = next(node["id"] for node in snap["nodes"]
                              if node["node_key"] == "review")
        assert not [event for event in events
                    if event["type"] == "node_failed"
                    and event.get("node_id") == review_node_id]

        # ---- 工件版本与版本内容 ----
        assert _version(snap, "discussion") >= 2
        assert _version(snap, "proposal") >= 2
        assert _version(snap, "requirement") == 2, _version(snap, "requirement")
        assert _version(snap, "review") >= 3, _version(snap, "review")
        assert record.get("review_version") == 1, record       # 第一轮退回不写报告，第二轮写的是第 1 版
        proposal = client.get(f"/api/runs/{run_id}/artifacts/proposal").json()
        assert "## 修改说明" in proposal["body"]                # 回流之后出的是修订版

        # ---- 三个会议节点各只有一场会议 ----
        meetings = client.get(f"/api/runs/{run_id}/meetings").json()["items"]
        by_key: dict[str, list[int]] = {}
        for meeting in meetings:
            by_key.setdefault(meeting["node_key"], []).append(meeting["id"])
        assert len(by_key["discuss"]) == 1
        assert len(by_key["review"]) == 1
        assert len(by_key["confirm"]) == 1
        review_meeting_id = by_key["review"][0]
        # 讨论复议后回到第 2 轮；评审第二轮判"接受"；确认会议先不通过、第三轮通过
        with session_scope() as s:
            meeting_rows = {row.node_key: row for row in
                            s.query(Meeting).filter(Meeting.run_id == run_id).all()}
            assert meeting_rows["discuss"].round == 2
            assert meeting_rows["review"].round == 2
            assert meeting_rows["review"].state_json.get("verdict") == "accept"
            assert meeting_rows["confirm"].round == 3
            assert meeting_rows["confirm"].state_json.get("verdict") == "pass"
            # 中断那一次的执行记录作废，恢复后的那次正常完成
            review_node = (s.query(Node)
                           .filter(Node.run_id == run_id, Node.node_key == "review").one())
            attempts = (s.query(NodeAttempt)
                        .filter(NodeAttempt.node_id == review_node.id)
                        .order_by(NodeAttempt.attempt_no.asc()).all())
            assert [attempt.status for attempt in attempts].count("failed") == 1
        # 讨论确实带着评审意见续开过
        discuss_id = by_key["discuss"][0]
        turns = client.get(f"/api/runs/{run_id}/meetings/{discuss_id}/turns").json()["items"]
        assert any(turn["kind"] == "user_feedback" and "复议" in turn["content"]
                   for turn in turns)
        discuss_points = client.get(
            f"/api/runs/{run_id}/meetings/{discuss_id}").json()["decision_points"]
        assert {point["index"]: point["status"] for point in discuss_points}["D1"] in (
            "agreed", "ruled")                        # 评审意见落在已了结的决策点上
        # 两名评审者各发言一次，没有同一人被重复调用
        review_turns = client.get(
            f"/api/runs/{run_id}/meetings/{review_meeting_id}/turns").json()["items"]
        for kind in ("review", "review_delta"):
            speakers = {turn["speaker_id"] for turn in review_turns
                        if turn["kind"] == kind and turn["content"]}
            assert len(speakers) == 2, (kind, speakers)

        # ---- 回流与恢复的事件载荷能定位到本次场景 ----
        reroutes = [event for event in events if event["type"] == "node_rerouted"]
        review_reroute = next(event for event in reroutes
                              if event["payload"].get("from") == "review")
        assert review_reroute["payload"].get("targets") == ["discuss"]
        assert review_reroute["payload"].get("escalated") is False
        verdicts = [event for event in events
                    if event["type"] == "review_verdict"
                    and event["payload"].get("meeting_id") == review_meeting_id]
        assert [event["payload"].get("round") for event in verdicts] == [1, 2]
        assert [event["payload"].get("verdict") for event in verdicts] == ["return", "accept"]
        assert verdicts[0]["seq"] < recovered_seq < verdicts[1]["seq"]
        first_suspend = next(event["seq"] for event in events
                             if event["type"] == "meeting_suspended")
        first_resume = next(event["seq"] for event in events
                            if event["type"] == "meeting_resumed")
        assert first_suspend < first_resume
        resumes = [event for event in events
                   if event["type"] == "meeting_resumed"
                   and event["payload"].get("meeting_id") == review_meeting_id
                   and event["seq"] > recovered_seq]
        assert resumes and resumes[0]["payload"].get("reason") == "resume"
        restarts = [event for event in events
                    if event["type"] == "node_started"
                    and event["payload"].get("node_key") == "review"
                    and event["seq"] > recovered_seq]
        assert restarts and restarts[-1]["payload"].get("attempt") == 3
        # run_recovered 之后评审会议继续并完成
        review_finished = [event["seq"] for event in events
                           if event["type"] == "meeting_finished"
                           and int((event["payload"] or {}).get("meeting_id") or 0)
                           == review_meeting_id]
        assert review_finished and max(review_finished) > recovered_seq

        # ---- 门记录：澄清、名单、方案确认、评审裁决、需求确认各有人工的决定 ----
        gates = snap["gates"]
        human = [gate for gate in gates if (gate.get("decided_by") or "") == "human"]
        by_kind = {(gate.get("kind") or "") for gate in human}
        assert "clarify" in by_kind and "roster" in by_kind
        by_node = {(gate.get("node_key") or "") for gate in human}
        assert {"gate_proposal", "gate_review", "gate_requirement"} <= by_node
        requirement_gates = sorted(
            (gate for gate in gates if gate.get("node_key") == "gate_requirement"),
            key=lambda gate: gate["id"])
        assert [(gate["status"], gate["decided_by"]) for gate in requirement_gates] == [
            ("rejected", "human"), ("approved", "human")], [
            (gate["status"], gate["decided_by"]) for gate in requirement_gates]
        assert "缺少验收标准" in (requirement_gates[0].get("feedback") or "")

        # ---- 设计节点确实开始执行 ----
        design = next(node for node in snap["nodes"] if node["node_key"] == "design")
        assert design["status"] != "pending" and design["attempts"] >= 1


def test_offline_front_half_fully_automatic(monkeypatch) -> None:
    """仅兜底等级（L3）：没有任何人工确认点，直接跑到设计节点。"""
    _seed_model()
    fake = ScriptedCalls(inject_review_issue=False, cancel_second_merge=False)
    _install(monkeypatch, fake)

    with TestClient(app) as client:
        run_id = _create_run(client, {**CONFIG, "automation": {"level": "L3"}})
        snap = _drive(client, run_id, timeout=120, stop_at_design=True, answer_gates=False)
        stopped = client.post(f"/api/runs/{run_id}/stop")
        assert stopped.status_code == 200, stopped.text
        events = _events(client, run_id)

    design_started = next(event for event in events
                          if event["type"] == "node_started"
                          and event["payload"].get("node_key") == "design")
    # 设计开始之前没有任何确认点需要人作答（模型判定的确认点不开门等人）
    assert not [event for event in events
                if event["type"] == "gate_requested"
                and event["seq"] < design_started["seq"]]
    # 前半段的确认点全部由程序或模型落定，没有人工决定
    front_nodes = {"discuss", "review", "confirm", "gate_proposal", "gate_review",
                   "gate_requirement"}
    front_gates = [gate for gate in snap["gates"] if gate.get("node_key") in front_nodes]
    assert front_gates
    assert all((gate.get("decided_by") or "") not in ("", "human")
               for gate in front_gates), [
        (gate.get("node_key"), gate.get("kind"), gate.get("decided_by"))
        for gate in front_gates]
    assert fake.review_calls == 2                 # 首轮评审两名评审者各一次
    assert fake.review_delta_calls == 0           # 没有回流，也就没有增量复审
    design = next(node for node in snap["nodes"] if node["node_key"] == "design")
    assert design["status"] != "pending" and design["attempts"] >= 1
