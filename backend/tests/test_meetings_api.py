"""会议的查询与介入接口。"""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.artifacts.store import write_artifact
from app.db.base import init_db, session_scope
from app.db.models import Node, Run, Workflow
from app.engine.events import load_events
from app.engine.runners import NodeContext
from app.engine.templates import seed_defaults
from app.main import app
from app.meeting import store
from app.meeting.checkpoints import open_checkpoint
from app.meeting.session import MeetingSession

_counter = iter(range(1, 1000))


def _meeting(phase: str = "roster", suspend: bool = True) -> MeetingSession:
    init_db()
    seed_defaults()
    index = next(_counter)
    with session_scope() as s:
        workflow_id = s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").one().id
        run_id = f"api-run-{index}"
        s.add(Run(id=run_id, workflow_id=workflow_id, status="running"))
        s.flush()
        node = Node(node_key="discuss", type="MEETING", name="讨论会议", run_id=run_id,
                    status="running", config_json={"protocol": "discussion"})
        s.add(node)
        s.flush()
        ctx = NodeContext(run_id=run_id, node_id=node.id, node_key="discuss", type="MEETING",
                          name="讨论会议", phase="", config={"protocol": "discussion"},
                          assignee={}, attempt_id=0, attempt_no=1)
    meeting_id = store.create_meeting(run_id=run_id, node_id=node.id, node_key="discuss",
                                      protocol="discussion", charter={}, config={})
    store.update_meeting(meeting_id, phase=phase)
    store.add_participant(meeting_id, role_key="facilitator", display_name="主持人",
                          mode="facilitator")
    mtg = MeetingSession(meeting_id=meeting_id, ctx=ctx)
    if suspend:
        asyncio.run(open_checkpoint(mtg, "roster", title="请确认名单", description="",
                                    payload={"proposed": []}))
    return mtg


def _client(monkeypatch) -> TestClient:
    return TestClient(app)


def test_detail_lists_participants_and_checkpoint(monkeypatch) -> None:
    mtg = _meeting()
    with _client(monkeypatch) as client:
        response = client.get(f"/api/runs/{mtg.run_id}/meetings/{mtg.meeting_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["meeting"]["protocol"] == "discussion"
    assert body["meeting"]["status"] == "suspended"
    assert [p["display_name"] for p in body["participants"]] == ["主持人"]
    assert body["checkpoints"][0]["kind"] == "roster"
    assert body["node_status"] == "waiting_human"


def test_list_meetings(monkeypatch) -> None:
    mtg = _meeting()
    with _client(monkeypatch) as client:
        body = client.get(f"/api/runs/{mtg.run_id}/meetings").json()
    assert [m["id"] for m in body["items"]] == [mtg.meeting_id]


def test_missing_meeting_returns_404(monkeypatch) -> None:
    mtg = _meeting()
    with _client(monkeypatch) as client:
        response = client.get(f"/api/runs/{mtg.run_id}/meetings/999999")
    assert response.status_code == 404


def test_replace_participants_at_roster_checkpoint(monkeypatch) -> None:
    mtg = _meeting()
    store.upsert_decision_point(mtg.meeting_id, "D1", title="先做哪个平台", status="agreed")
    payload = {"participants": [
        {"display_name": "架构", "persona": "你关注技术可行性。", "mode": "participant"},
        {"display_name": "合规顾问", "persona": "你关注数据合规。", "mode": "participant"},
    ]}
    with _client(monkeypatch) as client:
        response = client.put(f"/api/runs/{mtg.run_id}/meetings/{mtg.meeting_id}/participants",
                              json=payload)
    assert response.status_code == 200, response.text
    rows = store.list_participants(mtg.meeting_id)
    # 主持人与需求方代理保留，参会者被整体替换
    assert [(r["display_name"], r["mode"]) for r in rows] == [
        ("主持人", "facilitator"), ("架构", "participant"), ("合规顾问", "participant")]
    assert rows[2]["source"] == "user" and rows[2]["role_key"] is None
    assert rows[2]["system_prompt"] == "你关注数据合规。"
    # 账本事件带当时的账本，不是一个空占位；改名单不动账本，版本号不变
    updates = [e["payload"] for e in load_events(mtg.run_id)
               if e["type"] == "meeting_ledger_updated"]
    assert updates[-1]["roster"] is True
    assert updates[-1]["decision_points"] == [{"index": "D1", "status": "agreed"}]
    assert updates[-1]["version"] == 0


def test_invalid_participant_mode_changes_nothing(monkeypatch) -> None:
    mtg = _meeting()
    store.add_participant(mtg.meeting_id, role_key="qa", display_name="测试",
                          mode="participant", system_prompt="你是测试")
    payload = {"participants": [{"display_name": "冒充主持人", "mode": "facilitator"}]}
    with _client(monkeypatch) as client:
        response = client.put(f"/api/runs/{mtg.run_id}/meetings/{mtg.meeting_id}/participants",
                              json=payload)
    assert response.status_code == 400
    assert [r["display_name"] for r in store.list_participants(mtg.meeting_id)] == ["主持人", "测试"]


def test_participants_cannot_be_changed_after_roster(monkeypatch) -> None:
    mtg = _meeting(phase="stances")
    with _client(monkeypatch) as client:
        response = client.put(f"/api/runs/{mtg.run_id}/meetings/{mtg.meeting_id}/participants",
                              json={"participants": []})
    assert response.status_code == 400


def test_invalid_model_id_keeps_roster(monkeypatch) -> None:
    mtg = _meeting()
    store.add_participant(mtg.meeting_id, role_key="qa", display_name="测试",
                          mode="participant", system_prompt="你是测试")
    payload = {"participants": [{"display_name": "架构", "persona": "x",
                                 "mode": "participant", "model_id": 999_999}]}
    with _client(monkeypatch) as client:
        response = client.put(f"/api/runs/{mtg.run_id}/meetings/{mtg.meeting_id}/participants",
                              json=payload)
    assert response.status_code == 400
    assert [r["display_name"] for r in store.list_participants(mtg.meeting_id)] == ["主持人", "测试"]


def test_unknown_role_key_keeps_roster(monkeypatch) -> None:
    mtg = _meeting()
    store.add_participant(mtg.meeting_id, role_key="qa", display_name="测试",
                          mode="participant", system_prompt="你是测试")
    payload = {"participants": [{"role_key": "no_such_role", "display_name": "架构",
                                 "mode": "participant"}]}
    with _client(monkeypatch) as client:
        response = client.put(f"/api/runs/{mtg.run_id}/meetings/{mtg.meeting_id}/participants",
                              json=payload)
    assert response.status_code == 400
    assert [r["display_name"] for r in store.list_participants(mtg.meeting_id)] == ["主持人", "测试"]


def test_patch_decision_point_while_suspended(monkeypatch) -> None:
    mtg = _meeting()
    point_id = store.upsert_decision_point(mtg.meeting_id, "D1", title="先做哪个平台",
                                           status="open")
    with _client(monkeypatch) as client:
        response = client.patch(
            f"/api/runs/{mtg.run_id}/meetings/{mtg.meeting_id}/decision-points/{point_id}",
            json={"status": "deferred", "conclusion": "留待用户决定"})
    assert response.status_code == 200, response.text
    assert store.list_decision_points(mtg.meeting_id)[0]["status"] == "deferred"
    # 用户改了账本要发账本事件：事件流里看得到，前端据此刷新会议详情
    updates = [e["payload"] for e in load_events(mtg.run_id)
               if e["type"] == "meeting_ledger_updated"]
    assert updates[-1]["version"] == 1
    assert updates[-1]["decision_points"] == [{"index": "D1", "status": "deferred"}]


def test_a_decision_point_patch_that_changes_nothing_emits_no_event(monkeypatch) -> None:
    mtg = _meeting()
    point_id = store.upsert_decision_point(mtg.meeting_id, "D1", title="先做哪个平台",
                                           status="open", conclusion="选网页版")
    with _client(monkeypatch) as client:
        response = client.patch(
            f"/api/runs/{mtg.run_id}/meetings/{mtg.meeting_id}/decision-points/{point_id}",
            json={"status": "open", "conclusion": "选网页版"})
    assert response.status_code == 200, response.text
    assert not [e for e in load_events(mtg.run_id) if e["type"] == "meeting_ledger_updated"]


def test_decision_point_patch_requires_suspension(monkeypatch) -> None:
    mtg = _meeting(phase="debate", suspend=False)
    point_id = store.upsert_decision_point(mtg.meeting_id, "D1", title="先做哪个平台",
                                           status="open")
    with _client(monkeypatch) as client:
        response = client.patch(
            f"/api/runs/{mtg.run_id}/meetings/{mtg.meeting_id}/decision-points/{point_id}",
            json={"status": "deferred"})
    assert response.status_code == 400


def test_meeting_of_another_run_is_404(monkeypatch) -> None:
    mtg = _meeting()
    other = _meeting()
    with _client(monkeypatch) as client:
        response = client.get(f"/api/runs/{other.run_id}/meetings/{mtg.meeting_id}")
    assert response.status_code == 404


def test_intervention_queue_has_a_limit(monkeypatch) -> None:
    mtg = _meeting(phase="debate", suspend=False)
    for _ in range(20):
        store.queue_intervention(mtg.meeting_id, content="一直说")
    with _client(monkeypatch) as client:
        response = client.post(f"/api/runs/{mtg.run_id}/meetings/{mtg.meeting_id}/intervene",
                               json={"content": "第二十一条"})
    assert response.status_code == 400


def test_intervene_queues_request(monkeypatch) -> None:
    mtg = _meeting(phase="debate", suspend=False)
    with _client(monkeypatch) as client:
        response = client.post(f"/api/runs/{mtg.run_id}/meetings/{mtg.meeting_id}/intervene",
                               json={"content": "注意成本"})
        assert response.status_code == 201, response.text
        taken = store.take_queued_interventions(mtg.meeting_id)
    assert [t["content"] for t in taken] == ["注意成本"]


def _two_report_meetings() -> tuple[str, int, int]:
    """同一条运行上的评审、确认两个会议节点。两种会议的报告共用 review 工件。"""
    init_db()
    seed_defaults()
    run_id = f"api-run-{next(_counter)}"
    node_ids: list[int] = []
    with session_scope() as s:
        workflow_id = s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").one().id
        s.add(Run(id=run_id, workflow_id=workflow_id, status="running"))
        s.flush()
        for node_key, name in (("review", "评审会议"), ("confirm", "需求确认会议")):
            node = Node(node_key=node_key, type="MEETING", name=name, run_id=run_id,
                        status="running", config_json={"protocol": node_key})
            s.add(node)
            s.flush()
            node_ids.append(node.id)
    review, confirm = [
        store.create_meeting(run_id=run_id, node_id=node_id, node_key=node_key,
                             protocol=node_key, charter={}, config={})
        for node_key, node_id in zip(("review", "confirm"), node_ids)]
    return run_id, review, confirm


def _minutes(client: TestClient, run_id: str, meeting_id: int):
    return client.get(f"/api/runs/{run_id}/meetings/{meeting_id}/minutes")


def test_minutes_are_404_until_the_meeting_writes_its_own_report(monkeypatch) -> None:
    """确认会议还没写报告时，不能把评审会议的报告当成它的纪要。"""
    run_id, _review, confirm = _two_report_meetings()
    write_artifact(run_id, "review", "评审会议的报告", created_by="chair", inputs=["proposal"],
                   status="reviewed", extra_meta={"review_type": "proposal"})

    with _client(monkeypatch) as client:
        response = _minutes(client, run_id, confirm)

    assert response.status_code == 404, response.text


def test_minutes_are_the_version_the_meeting_wrote_not_the_latest(monkeypatch) -> None:
    run_id, review, confirm = _two_report_meetings()
    for version, (text, review_type) in enumerate(
            [("评审会议的报告", "proposal"), ("确认会议的报告", "requirement")], start=1):
        write_artifact(run_id, "review", text, created_by="chair", inputs=[], status="reviewed",
                       extra_meta={"review_type": review_type})
        store.set_state(review if version == 1 else confirm, {"minutes_version": version})

    with _client(monkeypatch) as client:
        first = _minutes(client, run_id, review)
        second = _minutes(client, run_id, confirm)

    # 评审会议写的是第 1 版：第 2 版出现之后它取到的仍是自己那一版
    assert first.status_code == 200, first.text
    assert (first.json()["kind"], first.json()["version"]) == ("review", 1)
    assert first.json()["body"] == "评审会议的报告"
    assert second.status_code == 200, second.text
    assert (second.json()["kind"], second.json()["version"]) == ("review", 2)
    assert second.json()["body"] == "确认会议的报告"
