"""会议相关的表与加列：建表幂等，行能写能读，旧库启动时补列。"""
from __future__ import annotations

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool

from app.db import base
from app.db.base import init_db, session_scope
from app.db.models import (DecisionPoint, Gate, Meeting, MeetingIntervention, MeetingParticipant,
                           MeetingTurn, Node, Run, Workflow)
from app.engine.templates import seed_defaults


def test_init_db_is_idempotent_and_tables_exist() -> None:
    init_db()
    init_db()
    seed_defaults()
    with session_scope() as s:
        workflow_id = s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").one().id
        run = Run(id="model-run", workflow_id=workflow_id, status="created",
                  config_json={"automation": {"level": "L2"}})
        s.add(run)
        s.flush()
        node = Node(node_key="discuss", type="MEETING", name="讨论会议", run_id="model-run",
                    status="pending")
        s.add(node)
        s.flush()
        gate = Gate(run_id="model-run", node_id=node.id, status="open", kind="consensus",
                    payload_json={"open": ["D2"]}, decision_json={})
        s.add(gate)
        meeting = Meeting(run_id="model-run", node_id=node.id, node_key="discuss",
                          protocol="discussion", status="active", phase="prepare", round=1,
                          charter_json={"idea": "x"}, config_json={}, state_json={})
        s.add(meeting)
        s.flush()
        speaker = MeetingParticipant(meeting_id=meeting.id, role_key="architect",
                                     display_name="架构", mode="participant", source="template")
        s.add(speaker)
        s.flush()
        turn = MeetingTurn(meeting_id=meeting.id, seq=1, round=1, phase="prepare",
                           speaker_id=speaker.id, speaker_name="架构", kind="position",
                           content="立场", structured_json={"must": ["可验证"]})
        s.add(turn)
        point = DecisionPoint(meeting_id=meeting.id, index="D1", title="先做哪个平台",
                              status="open", stances_json={})
        s.add(point)
        s.add(MeetingIntervention(meeting_id=meeting.id, content="注意成本", status="queued"))
        s.flush()
        ids = (run.id, gate.id, meeting.id, speaker.id, turn.id, point.id)
    with session_scope() as s:
        assert s.get(Run, ids[0]).config_json == {"automation": {"level": "L2"}}
        assert s.get(Gate, ids[1]).kind == "consensus"
        assert s.get(Meeting, ids[2]).status == "active"
        assert s.get(MeetingTurn, ids[4]).structured_json == {"must": ["可验证"]}
        assert s.get(DecisionPoint, ids[5]).index == "D1"


def test_old_database_gets_new_columns_on_startup(monkeypatch) -> None:
    # 开发库是在加列之前建的：新列靠启动时的 _ensure_columns 补上（建表语句管不到已有的表）
    old_engine = create_engine("sqlite:///:memory:", poolclass=StaticPool)
    with old_engine.begin() as conn:
        conn.execute(text("CREATE TABLE runs (id VARCHAR(24) PRIMARY KEY, workflow_id INTEGER, "
                          "status VARCHAR(30))"))
        conn.execute(text("CREATE TABLE gates (id INTEGER PRIMARY KEY, run_id VARCHAR(24), "
                          "node_id INTEGER, status VARCHAR(30))"))
        conn.execute(text("CREATE TABLE agent_templates (id INTEGER PRIMARY KEY, "
                          "role_key VARCHAR(80), name VARCHAR(120))"))
        conn.execute(text("INSERT INTO gates (id, run_id, node_id, status) "
                          "VALUES (1, 'old-run', 1, 'approved')"))
    monkeypatch.setattr(base, "engine", old_engine)
    try:
        base._ensure_columns()
        base._ensure_columns()                      # 幂等：第二次不会重复加列
        inspector = inspect(old_engine)
        names = lambda table: {column["name"] for column in inspector.get_columns(table)}
        assert {"config_json"} <= names("runs")
        assert {"kind", "payload_json", "decision_json"} <= names("gates")
        assert {"meta_json"} <= names("agent_templates")
        with old_engine.connect() as conn:
            old_gate = conn.execute(
                text("SELECT kind, payload_json, decision_json FROM gates WHERE id = 1")).one()
    finally:
        old_engine.dispose()
    # 旧行不会填默认值：读这三列的地方要自己兜底
    assert tuple(old_gate) == (None, None, None)
