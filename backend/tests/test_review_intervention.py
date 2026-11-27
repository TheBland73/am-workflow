"""用户的运行中介入：等待人工时改判评审意见、导出会议纪要；
运行指标增加上下文与会议两段。"""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.api.runs import run_metrics
from app.artifacts.store import write_artifact
from app.db.base import init_db, session_scope
from app.db.models import (Gate, Meeting, MeetingTurn, Node, NodeAttempt, Run, Workflow,
                           new_run_id)
from app.engine.events import emit
from app.engine.templates import instantiate_nodes, seed_defaults, workflow_definition
from app.main import app

IDEA = "给小团队做一个反馈收集工具"
ISSUE = {
    "id": "I1", "severity": "major", "category": "wording", "location": "## 范围",
    "problem": "范围写得不够清楚", "suggestion": "把必做与不做分别列清楚",
    "standing": True, "accepted": False, "response": "", "disposition": "",
    "origin": "reviewer", "round": 1, "dissent": "", "raised_by": ["架构师"],
}


def _make_run(status: str = "waiting_human") -> str:
    init_db()
    seed_defaults()
    run_id = new_run_id()
    with session_scope() as s:
        wf = s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").one()
        s.add(Run(id=run_id, workflow_id=wf.id, status=status, idea_text=IDEA, config_json={}))
        s.flush()
    instantiate_nodes(run_id, workflow_definition("sdlc_v1"))
    return run_id


def _node_id(run_id: str, key: str) -> int:
    with session_scope() as s:
        return s.query(Node).filter(Node.run_id == run_id, Node.node_key == key).one().id


def _meeting(run_id: str, node_key: str, protocol: str, *, status: str = "suspended",
             state: dict | None = None) -> int:
    node_id = _node_id(run_id, node_key)
    with session_scope() as s:
        row = Meeting(run_id=run_id, node_id=node_id, node_key=node_key, protocol=protocol,
                      status=status, phase="merge", round=1, charter_json={},
                      config_json={"params": {}}, state_json=state or {})
        s.add(row)
        s.flush()
        return row.id


def _state_issue(meeting_id: int, issue_id: str) -> dict:
    from app.meeting import store

    issues = list(store.get_state(meeting_id).get("issues") or [])
    return next(item for item in issues if str(item.get("id")) == issue_id)


def _minutes_body(run_id: str, meeting_id: int) -> str:
    """与评审协议写报告同一条路径：纪要正文 + 问题清单及处置状态。"""
    from app.meeting import minutes, review_rules, store

    meeting = store.get_meeting(meeting_id)
    issues = list(meeting["state"].get("issues") or [])
    return minutes.minutes_markdown(
        meeting=meeting, participants=[], points=[], turns=[],
        extra_sections=[("问题清单及处置状态", "\n".join(review_rules.issue_lines(issues)))])


def test_patch_a_review_issue_while_waiting_for_human() -> None:
    run_id = _make_run("waiting_human")
    meeting_id = _meeting(run_id, "review", "review", state={"issues": [dict(ISSUE)]})

    with TestClient(app) as client:
        response = client.patch(
            f"/api/runs/{run_id}/meetings/{meeting_id}/issues/I1",
            json={"accepted": True})
        assert response.status_code == 200, response.text

    issue = _state_issue(meeting_id, "I1")
    assert issue["accepted"] is True
    assert issue["disposition"] == "用户改判：采纳"
    # 下一轮写报告时用同一份账本：处置跟着变
    assert "用户改判：采纳" in _minutes_body(run_id, meeting_id)


def test_patch_is_refused_when_the_run_is_not_waiting_for_human() -> None:
    run_id = _make_run("running")
    meeting_id = _meeting(run_id, "review", "review", state={"issues": [dict(ISSUE)]})

    with TestClient(app) as client:
        response = client.patch(
            f"/api/runs/{run_id}/meetings/{meeting_id}/issues/I1",
            json={"accepted": True})
        assert response.status_code == 400

    issue = _state_issue(meeting_id, "I1")
    assert issue["accepted"] is False and issue["disposition"] == ""


def test_patch_an_unknown_issue_is_404() -> None:
    run_id = _make_run("waiting_human")
    meeting_id = _meeting(run_id, "review", "review", state={"issues": [dict(ISSUE)]})

    with TestClient(app) as client:
        response = client.patch(
            f"/api/runs/{run_id}/meetings/{meeting_id}/issues/I9",
            json={"accepted": True})
        assert response.status_code == 404


def test_minutes_export_for_a_discussion_meeting() -> None:
    run_id = _make_run("finished")
    # 协议写纪要时会把版本记进会议状态，接口只认这个版本
    meeting_id = _meeting(run_id, "discuss", "discussion", status="completed",
                          state={"minutes_version": 1})
    write_artifact(run_id, "discussion", "# 会议纪要（讨论会议）\n\nMINUTES-MARKER\n",
                   created_by="chair", inputs=[])

    with TestClient(app) as client:
        response = client.get(f"/api/runs/{run_id}/meetings/{meeting_id}/minutes")
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["kind"] == "discussion" and data["version"] == 1
        assert "MINUTES-MARKER" in data["body"]

    # 还没有纪要 → 404
    empty_run = _make_run("finished")
    empty_meeting = _meeting(empty_run, "discuss", "discussion", status="completed")
    with TestClient(app) as client:
        assert client.get(
            f"/api/runs/{empty_run}/meetings/{empty_meeting}/minutes").status_code == 404


def test_metrics_include_context_and_meetings() -> None:
    run_id = _make_run("running")
    node_id = _node_id(run_id, "discuss")
    with session_scope() as s:
        meeting = Meeting(run_id=run_id, node_id=node_id, node_key="discuss",
                          protocol="discussion", status="completed", phase="done", round=1,
                          charter_json={}, config_json={"params": {}}, state_json={})
        s.add(meeting)
        s.flush()
        meeting_id = meeting.id
        for seq in (1, 2):
            s.add(MeetingTurn(meeting_id=meeting_id, seq=seq, round=1, phase="stances",
                              speaker_name="架构师", kind="position", content="发言内容"))
        s.add(NodeAttempt(node_id=node_id, attempt_no=1, status="done",
                          tokens_in=100, tokens_out=20, cached_tokens=40))
        for decided_by, status in (("program", "approved"), ("model:Mock", "approved"),
                                   ("user-delegated", "approved"), (None, "open")):
            s.add(Gate(run_id=run_id, node_id=node_id, status=status, decided_by=decided_by))
    asyncio.run(emit(run_id, "context_compacted", node_id,
                     {"before_tokens": 1000, "after_tokens": 400}))

    metrics = run_metrics(run_id)

    assert metrics["context"] == {"compactions": 1, "cached_tokens": 40,
                                  "cache_hit_rate": 0.4}
    assert metrics["meetings"]["total"] == 1
    assert metrics["meetings"]["turns"] == 2
    assert metrics["meetings"]["checkpoints"] == {"human": 1, "program": 1,
                                                  "model": 1, "open": 1}
