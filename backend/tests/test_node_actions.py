"""节点交互增量：GET 详情扩展、retry/reset/skip/feedback 状态机与审计事件。

写法沿用 test_durability.py / test_gates.py：直接建 Run/Node 行造状态，不真跑模型；
API 用例用 TestClient（同时屏蔽 orchestrator.kick/start，避免后台调度干扰断言）。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.db.base import init_db, session_scope
from app.db.models import (
    Artifact,
    Gate,
    Node,
    NodeAttempt,
    Run,
    Task,
    Workflow,
    new_run_id,
)
from app.engine import orchestrator as orch_mod
from app.engine.events import emit, load_events
from app.engine.gates import _downstream, downstream_of
from app.engine.node_actions import downstream_nodes
from app.engine.orchestrator import Orchestrator
from app.engine.templates import seed_defaults
from app.main import app


# ---------------- 造数与读取 helper ----------------
def _new_run(status: str = "running") -> str:
    init_db()
    seed_defaults()
    with session_scope() as s:
        wf = s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").one()
        run_id = new_run_id()
        s.add(Run(id=run_id, workflow_id=wf.id, status=status,
                  idea_text="节点交互测试"))
    return run_id


def _add_nodes(run_id: str, specs: list[dict]) -> dict[str, int]:
    """按 spec 建节点（不跑模型），返回 node_key → node_id。"""
    with session_scope() as s:
        ids: dict[str, int] = {}
        for index, spec in enumerate(specs):
            node = Node(
                run_id=run_id,
                node_key=spec["key"],
                type=spec.get("type", "AGENT"),
                name=spec.get("name", spec["key"]),
                phase=spec.get("phase", "test"),
                status=spec.get("status", "pending"),
                deps_json=list(spec.get("deps") or []),
                wave=spec.get("wave"),
                max_attempts=spec.get("max_attempts", 3),
                config_json=dict(spec.get("config") or {}),
                order_index=spec.get("order_index", index),
            )
            s.add(node)
            s.flush()
            ids[node.node_key] = node.id
        return ids


def _update_node(node_id: int, **fields) -> None:
    with session_scope() as s:
        node = s.get(Node, node_id)
        for key, value in fields.items():
            setattr(node, key, value)


def _node(node_id: int) -> Node:
    with session_scope() as s:
        return s.get(Node, node_id)


def _run_status(run_id: str) -> str:
    with session_scope() as s:
        return s.get(Run, run_id).status


def _gates(node_id: int) -> list[Gate]:
    with session_scope() as s:
        return (
            s.query(Gate)
            .filter(Gate.node_id == node_id)
            .order_by(Gate.id.asc())
            .all()
        )


def _attempt_count(node_id: int) -> int:
    with session_scope() as s:
        return (
            s.query(NodeAttempt).filter(NodeAttempt.node_id == node_id).count()
        )


def _events(run_id: str, type_: str | None = None) -> list[dict]:
    items = load_events(run_id)
    if type_ is not None:
        items = [item for item in items if item["type"] == type_]
    return items


def _finish(run_id: str) -> None:
    """把 run 置终态：避免后续用例的 lifespan recover 去调度本用例残留的 run。"""
    with session_scope() as s:
        run = s.get(Run, run_id)
        if run is not None and run.status not in ("finished", "failed", "stopped"):
            run.status = "finished"


@pytest.fixture
def api(monkeypatch):
    """TestClient + 屏蔽调度器唤醒，保证动作接口不会真的起后台执行。"""
    monkeypatch.setattr(orch_mod.orchestrator, "kick", lambda run_id: None)
    monkeypatch.setattr(orch_mod.orchestrator, "start", lambda run_id: None)
    with TestClient(app) as client:
        yield client


def _detail(client: TestClient, node_id: int, **params) -> dict:
    response = client.get(f"/api/nodes/{node_id}", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _error(response) -> tuple[str, dict]:
    assert response.status_code >= 400, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, dict), detail
    return detail["code"], detail


# ---------------- GET 详情扩展 ----------------
def test_node_detail_returns_extended_fields(api) -> None:
    run_id = _new_run()
    ids = _add_nodes(run_id, [
        {"key": "requirement", "name": "需求", "status": "done", "wave": 1},
        {"key": "design", "name": "设计", "status": "done", "deps": ["requirement"],
         "wave": 2, "max_attempts": 5},
        {"key": "review", "name": "评审", "status": "pending", "deps": ["design"],
         "wave": 3},
    ])
    with session_scope() as s:
        s.add(Artifact(run_id=run_id, kind="design", path="runs/x/design.md",
                       version=1, created_by_node="design", status="active"))
    detail = _detail(api, ids["design"])
    # 现有字段（只加不改）
    assert detail["node_key"] == "design"
    assert detail["type"] == "AGENT"
    assert detail["status"] == "done"
    assert detail["deps"] == ["requirement"]
    assert detail["attempts"] == [] and detail["messages"] == []
    # 新增字段
    assert detail["max_attempts"] == 5
    assert detail["order_index"] == 1
    assert detail["run_status"] == "running"
    assert detail["upstream"] == [
        {"node_key": "requirement", "name": "需求", "status": "done"}
    ]
    assert detail["downstream"] == [
        {"node_key": "review", "name": "评审", "status": "pending"}
    ]
    assert detail["open_gate"] is None
    assert detail["artifact_kinds"] == ["design"]
    # 矩阵推导（done + run 运行中）
    assert detail["can_retry"] is True
    assert detail["can_reset"] is True
    assert detail["can_skip"] is False
    assert detail["can_feedback"] is True
    assert detail["can_edit_config"] is True
    # 间接下游
    requirement = _detail(api, ids["requirement"])
    assert [item["node_key"] for item in requirement["downstream"]] == ["design", "review"]
    _finish(run_id)


def test_node_detail_flags_for_gate_node_and_terminal_run(api) -> None:
    run_id = _new_run(status="waiting_human")
    ids = _add_nodes(run_id, [
        {"key": "escalated", "status": "waiting_human", "wave": 1},
        {"key": "gate_accept", "type": "HUMAN_GATE", "status": "waiting_human",
         "wave": 2},
    ])
    with session_scope() as s:
        s.add(Gate(run_id=run_id, node_id=ids["escalated"], status="open",
                   feedback="需要人工"))
    escalated = _detail(api, ids["escalated"])
    assert escalated["open_gate"] == {
        "id": _gates(ids["escalated"])[0].id,
        "node_id": ids["escalated"],
        "node_key": "escalated",
        "status": "open",
    }
    assert escalated["can_skip"] is True
    assert escalated["can_retry"] is True
    assert escalated["can_feedback"] is True
    gate_node = _detail(api, ids["gate_accept"])
    assert gate_node["can_skip"] is False          # 人工门不可跳过
    assert gate_node["can_retry"] is True
    assert gate_node["can_edit_config"] is False   # 门挂起时走门审批接口
    _finish(run_id)

    finished_id = _new_run(status="finished")
    finished_ids = _add_nodes(finished_id, [{"key": "done_node", "status": "done"}])
    done = _detail(api, finished_ids["done_node"])
    assert done["run_status"] == "finished"
    for flag in ("can_retry", "can_reset", "can_skip", "can_feedback",
                 "can_edit_config"):
        assert done[flag] is False


def test_node_detail_events_filtered_by_node_and_ordered(api) -> None:
    run_id = _new_run()
    ids = _add_nodes(run_id, [{"key": "a"}, {"key": "b"}])

    async def scenario() -> None:
        for index in range(3):
            await emit(run_id, "node_started", ids["a"], {"i": index})
        for index in range(2):
            await emit(run_id, "node_started", ids["b"], {"i": index})

    asyncio.run(scenario())
    detail = _detail(api, ids["a"])
    assert len(detail["events"]) == 3
    assert all(item["node_id"] == ids["a"] for item in detail["events"])
    assert all(item["node_key"] == "a" for item in detail["events"])
    seqs = [item["seq"] for item in detail["events"]]
    assert seqs == sorted(seqs)
    limited = _detail(api, ids["a"], events=1)
    assert len(limited["events"]) == 1
    assert limited["events"][0]["seq"] == seqs[-1]   # 最近一条
    assert _detail(api, ids["a"], events=0)["events"] == []
    _finish(run_id)


# ---------------- retry ----------------
def test_retry_rejects_terminal_run(api) -> None:
    run_id = _new_run(status="finished")
    ids = _add_nodes(run_id, [{"key": "a", "status": "done"}])
    response = api.post(f"/api/nodes/{ids['a']}/retry", json={"feedback": "再来"})
    code, detail = _error(response)
    assert response.status_code == 409 and code == "run_terminal"
    assert detail["details"]["run_status"] == "finished"
    assert _node(ids["a"]).status == "done"
    assert _run_status(run_id) == "finished"
    assert _events(run_id, "node_retried") == []
    # 终态 run 上 reset / skip / feedback 同样 409 run_terminal（§2 最右列）
    for path, body in (
        ("reset", None),
        ("skip", {"reason": "跳过"}),
        ("feedback", {"feedback": "改一下"}),
    ):
        other = api.post(f"/api/nodes/{ids['a']}/{path}", json=body)
        code, _ = _error(other)
        assert other.status_code == 409 and code == "run_terminal", (path, other.text)
    assert _node(ids["a"]).status == "done"


def test_retry_rejects_running_node(api) -> None:
    run_id = _new_run()
    ids = _add_nodes(run_id, [{"key": "a", "status": "running"}])
    response = api.post(f"/api/nodes/{ids['a']}/retry")
    code, _ = _error(response)
    assert response.status_code == 409 and code == "node_running"
    assert _node(ids["a"]).status == "running"
    assert _events(run_id, "node_retried") == []
    _finish(run_id)


def test_retry_injects_feedback_closes_gate_and_emits(api) -> None:
    run_id = _new_run(status="waiting_human")
    ids = _add_nodes(run_id, [{
        "key": "a", "status": "waiting_human",
        "config": {"instruction": "保留", "retry_at": "2999-01-01T00:00:00+00:00",
                   "retry_count": 2, "escalation": True,
                   "escalation_reason": "连续失败"},
    }])
    with session_scope() as s:
        s.add(Gate(run_id=run_id, node_id=ids["a"], status="open",
                   feedback="连续失败"))
    response = api.post(f"/api/nodes/{ids['a']}/retry", json={
        "feedback": "补充导出周报的验收标准",
        "expected_status": "waiting_human",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["node_id"] == ids["a"]
    assert body["status"] == "pending"
    assert body["run_id"] == run_id
    assert body["reset_nodes"] == []

    node = _node(ids["a"])
    assert node.status == "pending"
    assert node.config_json["feedback"] == "补充导出周报的验收标准"
    assert node.config_json["instruction"] == "保留"
    for key in ("retry_at", "retry_count", "escalation", "escalation_reason"):
        assert key not in node.config_json
    gates = _gates(ids["a"])
    assert len(gates) == 1
    assert gates[0].status == "approved"
    assert gates[0].decided_by == "human-retry"
    assert gates[0].feedback == "补充导出周报的验收标准"
    assert _run_status(run_id) == "running"
    events = _events(run_id, "node_retried")
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["node_key"] == "a"
    assert payload["feedback"] == "补充导出周报的验收标准"
    assert payload["actor"] == "human"
    assert payload["reset_downstream"] is False
    assert payload["reset_nodes"] == []
    _finish(run_id)


def test_retry_expected_status_conflict(api) -> None:
    run_id = _new_run()
    ids = _add_nodes(run_id, [{"key": "a", "status": "done"}])
    response = api.post(f"/api/nodes/{ids['a']}/retry",
                        json={"expected_status": "pending"})
    code, detail = _error(response)
    assert response.status_code == 409 and code == "status_conflict"
    assert detail["details"] == {"expected_status": "pending",
                                 "actual_status": "done"}
    assert _node(ids["a"]).status == "done"
    assert _events(run_id, "node_retried") == []
    _finish(run_id)


def test_retry_reset_downstream_true(api) -> None:
    run_id = _new_run(status="waiting_human")
    ids = _add_nodes(run_id, [
        {"key": "a", "status": "done", "wave": 1},
        {"key": "b", "status": "done", "deps": ["a"], "wave": 2},
        {"key": "c", "status": "done", "deps": ["b"], "wave": 3},
    ])
    response = api.post(f"/api/nodes/{ids['a']}/retry",
                        json={"reset_downstream": True})
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body["reset_nodes"]) == {"b", "c"}
    assert _node(ids["a"]).status == "pending"
    assert _node(ids["b"]).status == "pending"
    assert _node(ids["c"]).status == "pending"
    payload = _events(run_id, "node_retried")[-1]["payload"]
    assert payload["reset_downstream"] is True
    assert set(payload["reset_nodes"]) == {"b", "c"}
    _finish(run_id)


def test_retry_default_keeps_downstream_done(api) -> None:
    run_id = _new_run(status="waiting_human")
    ids = _add_nodes(run_id, [
        {"key": "a", "status": "done", "wave": 1},
        {"key": "b", "status": "done", "deps": ["a"], "wave": 2},
    ])
    response = api.post(f"/api/nodes/{ids['a']}/retry")
    assert response.status_code == 200, response.text
    assert response.json()["reset_nodes"] == []
    assert _node(ids["b"]).status == "done"
    _finish(run_id)


# ---------------- reset ----------------
def test_reset_clears_scheduling_keys_and_resets_downstream(api) -> None:
    run_id = _new_run(status="waiting_human")
    ids = _add_nodes(run_id, [
        {"key": "b", "status": "done", "wave": 1,
         "config": {"instruction": "保留我", "feedback": "旧反馈",
                    "retry_at": "2999-01-01T00:00:00+00:00", "retry_count": 2,
                    "escalation": True, "escalation_reason": "连续失败"}},
        {"key": "c", "status": "done", "deps": ["b"], "wave": 2},
    ])
    response = api.post(f"/api/nodes/{ids['b']}/reset")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert set(body["reset_nodes"]) == {"c"}
    node = _node(ids["b"])
    assert node.status == "pending"
    assert node.config_json == {"instruction": "保留我"}
    assert _node(ids["c"]).status == "pending"
    assert _run_status(run_id) == "running"
    payload = _events(run_id, "node_reset")[-1]["payload"]
    assert payload["node_key"] == "b"
    assert payload["actor"] == "human"
    assert set(payload["reset_nodes"]) == {"c"}
    _finish(run_id)


def test_reset_rejects_running_node(api) -> None:
    run_id = _new_run()
    ids = _add_nodes(run_id, [{"key": "a", "status": "running"}])
    response = api.post(f"/api/nodes/{ids['a']}/reset")
    code, _ = _error(response)
    assert response.status_code == 409 and code == "node_running"
    assert _node(ids["a"]).status == "running"
    _finish(run_id)


# ---------------- skip ----------------
def test_skip_marks_skipped_and_unblocks_downstream(api, monkeypatch) -> None:
    run_id = _new_run()
    ids = _add_nodes(run_id, [
        {"key": "a", "wave": 1},
        {"key": "b", "deps": ["a"], "wave": 2},
    ])
    response = api.post(f"/api/nodes/{ids['a']}/skip",
                        json={"reason": "人工确认可跳过"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "skipped"
    assert body["closed_gates"] == 0
    assert _node(ids["a"]).status == "skipped"
    payload = _events(run_id, "node_skipped")[-1]["payload"]
    assert payload == {"node_key": "a", "reason": "人工确认可跳过",
                       "cascade": False, "actor": "human"}

    calls = {"n": 0}

    async def fake_execute(_ctx):
        calls["n"] += 1
        return {"ok": True}

    monkeypatch.setattr(orch_mod, "execute_node", fake_execute)
    asyncio.run(Orchestrator()._loop(run_id))   # skipped 视同依赖完成 → b 可执行
    assert calls["n"] == 1
    assert _node(ids["b"]).status == "done"
    assert _run_status(run_id) == "finished"


def test_skip_closes_open_gate_and_resumes_run(api) -> None:
    run_id = _new_run(status="waiting_human")
    ids = _add_nodes(run_id, [{"key": "a", "status": "waiting_human"}])
    with session_scope() as s:
        s.add(Gate(run_id=run_id, node_id=ids["a"], status="open",
                   feedback="模型失败转人工"))
    response = api.post(f"/api/nodes/{ids['a']}/skip", json={"reason": "可忽略"})
    assert response.status_code == 200, response.text
    assert response.json()["closed_gates"] == 1
    node = _node(ids["a"])
    assert node.status == "skipped"
    gates = _gates(ids["a"])
    assert gates[0].status == "approved"
    assert gates[0].decided_by == "human-skip"
    assert gates[0].feedback == "可忽略"
    assert _run_status(run_id) == "running"      # 已无其他 open 门 → 恢复运行
    _finish(run_id)


def test_skip_gate_node_protected(api) -> None:
    run_id = _new_run(status="waiting_human")
    ids = _add_nodes(run_id, [
        {"key": "gate_accept", "type": "HUMAN_GATE", "status": "waiting_human"},
    ])
    with session_scope() as s:
        s.add(Gate(run_id=run_id, node_id=ids["gate_accept"], status="open"))
    response = api.post(f"/api/nodes/{ids['gate_accept']}/skip")
    code, _ = _error(response)
    assert response.status_code == 400 and code == "gate_node_protected"
    assert _node(ids["gate_accept"]).status == "waiting_human"
    assert _gates(ids["gate_accept"])[0].status == "open"
    assert _events(run_id, "node_skipped") == []
    _finish(run_id)


def test_skip_syncs_dev_task_status(api) -> None:
    run_id = _new_run()
    ids = _add_nodes(run_id, [{"key": "dev_T1", "wave": 1}])
    with session_scope() as s:
        s.add(Task(run_id=run_id, task_uid="T1", title="任务 1",
                   deps_json=[], wave=1, status="pending"))
    response = api.post(f"/api/nodes/{ids['dev_T1']}/skip",
                        json={"reason": "任务取消"})
    assert response.status_code == 200, response.text
    with session_scope() as s:
        task = (
            s.query(Task)
            .filter(Task.run_id == run_id, Task.task_uid == "T1")
            .one()
        )
        assert task.status == "skipped"
    _finish(run_id)


def test_skip_is_idempotent(api) -> None:
    run_id = _new_run()
    ids = _add_nodes(run_id, [{"key": "a"}])
    first = api.post(f"/api/nodes/{ids['a']}/skip")
    assert first.status_code == 200, first.text
    second = api.post(f"/api/nodes/{ids['a']}/skip")
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "skipped"
    assert second.json()["closed_gates"] == 0
    assert _node(ids["a"]).status == "skipped"
    assert len(_events(run_id, "node_skipped")) == 1     # 幂等重放不重复发事件
    _finish(run_id)


def test_skip_cascade_resets_downstream(api) -> None:
    run_id = _new_run()
    ids = _add_nodes(run_id, [
        {"key": "a", "wave": 1},
        {"key": "b", "status": "done", "deps": ["a"], "wave": 2},
    ])
    response = api.post(f"/api/nodes/{ids['a']}/skip",
                        json={"cascade_downstream": True})
    assert response.status_code == 200, response.text
    assert set(response.json()["reset_nodes"]) == {"b"}
    assert _node(ids["a"]).status == "skipped"
    assert _node(ids["b"]).status == "pending"
    _finish(run_id)


# ---------------- feedback ----------------
def test_feedback_only_injects_without_rerun(api) -> None:
    run_id = _new_run()
    ids = _add_nodes(run_id, [{
        "key": "a", "status": "done",
        "config": {"instruction": "保留我"},
    }])
    response = api.post(f"/api/nodes/{ids['a']}/feedback",
                        json={"feedback": "下次输出请带导出周报"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["node_id"] == ids["a"]
    assert body["status"] == "done"          # 不重跑：状态不变
    assert body["feedback"] == "下次输出请带导出周报"
    node = _node(ids["a"])
    assert node.status == "done"
    assert node.config_json == {
        "instruction": "保留我",
        "feedback": "下次输出请带导出周报",
    }
    assert _attempt_count(ids["a"]) == 0
    assert _events(run_id, "node_retried") == []
    payload = _events(run_id, "node_feedback_injected")[-1]["payload"]
    assert payload["node_key"] == "a"
    assert payload["feedback"] == "下次输出请带导出周报"
    assert payload["actor"] == "human"
    _finish(run_id)


def test_feedback_requires_content(api) -> None:
    run_id = _new_run()
    ids = _add_nodes(run_id, [{"key": "a"}])
    missing = api.post(f"/api/nodes/{ids['a']}/feedback", json={})
    code, _ = _error(missing)
    assert missing.status_code == 400 and code == "feedback_required"
    blank = api.post(f"/api/nodes/{ids['a']}/feedback",
                     json={"feedback": "   "})
    assert _error(blank)[0] == "feedback_required"
    assert "feedback" not in (_node(ids["a"]).config_json or {})
    _finish(run_id)


def test_feedback_rejects_running_and_skipped_nodes(api) -> None:
    running_id = _new_run()
    running = _add_nodes(running_id, [{"key": "a", "status": "running"}])
    response = api.post(f"/api/nodes/{running['a']}/feedback",
                        json={"feedback": "改一下"})
    code, _ = _error(response)
    assert response.status_code == 409 and code == "node_running"
    assert _node(running["a"]).status == "running"
    _finish(running_id)

    skipped_id = _new_run()
    skipped = _add_nodes(skipped_id, [{"key": "a", "status": "skipped"}])
    response = api.post(f"/api/nodes/{skipped['a']}/feedback",
                        json={"feedback": "改一下"})
    code, _ = _error(response)
    assert response.status_code == 409 and code == "node_not_retryable"
    _finish(skipped_id)


# ---------------- 错误码与 404 ----------------
def test_action_error_codes_for_invalid_states(api) -> None:
    run_id = _new_run()
    ids = _add_nodes(run_id, [
        {"key": "pending_node"},
        {"key": "done_node", "status": "done"},
    ])
    # 旧 GET 404 保留字符串 detail（契约允许既有错误体不变）。
    missing_get = api.get("/api/nodes/999999")
    assert missing_get.status_code == 404
    assert missing_get.json()["detail"] == "节点不存在"
    # 新动作端点 404 → 结构化 node_not_found。
    missing_post = api.post("/api/nodes/999999/retry")
    assert missing_post.status_code == 404
    assert missing_post.json()["detail"]["code"] == "node_not_found"
    # pending 不可 retry；done 不可 skip。
    pending_retry = api.post(f"/api/nodes/{ids['pending_node']}/retry")
    assert pending_retry.status_code == 409
    assert pending_retry.json()["detail"]["code"] == "node_not_retryable"
    done_skip = api.post(f"/api/nodes/{ids['done_node']}/skip")
    assert done_skip.status_code == 409
    assert done_skip.json()["detail"]["code"] == "node_not_skippable"
    _finish(run_id)


# ---------------- 公共血缘函数（gates 重构等价性） ----------------
def test_downstream_helpers_share_single_implementation() -> None:
    run_id = _new_run()
    _add_nodes(run_id, [
        {"key": "a", "wave": 1},
        {"key": "b", "deps": ["a"], "wave": 2},
        {"key": "c", "deps": ["b"], "wave": 3},
        {"key": "d", "deps": ["a"], "wave": 2},
    ])
    with session_scope() as s:
        nodes = s.query(Node).filter(Node.run_id == run_id).all()
        by_key = {node.node_key: node for node in nodes}
        from_gates = [node.node_key for node in downstream_of(by_key["a"].id, nodes)]
        from_actions = [
            node.node_key for node in downstream_nodes(nodes, "a")
        ]
    assert from_gates == from_actions
    assert set(from_gates) == {"b", "c", "d"}
    assert _downstream is downstream_of   # 旧私有名兼容
    _finish(run_id)
