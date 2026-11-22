"""三方讨论：append 版本链、蒸馏引用原文与方案确认门未决项联动。

复用 scripts/mock_provider.py 的 canned 与路由逻辑驱动真实引擎路径
（角色模板 → _run_doc_agent → 工件版本链），与 e2e 演示保持同一数据源。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.artifacts.store import parse_front_matter, read_artifact, write_artifact
from app.db.base import init_db, session_scope
from app.db.models import Node, Run, Workflow, new_run_id
from app.engine import runners
from app.engine.gates import _pending_questions, get_gate, request_gate
from app.engine.orchestrator import orchestrator
from app.engine.templates import instantiate_nodes, seed_defaults, workflow_definition
from app.main import app
from scripts.mock_provider import _pick

DISCUSSION_NODES = ("discuss_pro", "discuss_con", "discuss_response", "discuss_mod")

# 未决项段落作用域工件：段外（主张/证据）也放 `- [ ]`，用于验证只抓「未决项」段内。
SCOPED_DISCUSSION = """# 三方讨论

## 主张

- [ ] 段外勾选项：主张里的待办，不属于未决项

## 共识蒸馏

### 共识

- C1：先做单人闭环，实时协作延后。

### 未决项

- [ ] 段内一：人工门的具体位置
- [ ] 段内二：实时协作的排期

### 证据

- [ ] 段外勾选项：证据段里的待办
"""


def _make_run() -> str:
    init_db()
    seed_defaults()
    with session_scope() as s:
        wf = s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").one()
        run_id = new_run_id()
        s.add(Run(id=run_id, workflow_id=wf.id, status="running", idea_text="三方讨论测试想法"))
        s.flush()
    instantiate_nodes(run_id, workflow_definition("sdlc_v1"))
    write_artifact(run_id, "idea", "# 想法\n\n做一个面向小团队的客户反馈收集工具。",
                   created_by="idea", inputs=["idea"])
    return run_id


def _node_id(run_id: str, key: str) -> int:
    with session_scope() as s:
        return s.query(Node).filter(Node.run_id == run_id, Node.node_key == key).one().id


def _run_discussion_nodes(run_id: str, monkeypatch) -> None:
    model = SimpleNamespace(model_name="mock-fast", display_name="Mock Fast",
                            context_window=32768, max_output_tokens=4096)
    monkeypatch.setattr(runners.pool, "pick_role_model", lambda role: model)

    async def fake_agent(**kwargs):
        messages = [
            {"role": "system", "content": kwargs["system_prompt"]},
            {"role": "user", "content": kwargs["user_prompt"]},
        ]
        text, _call = _pick(messages, None)
        return SimpleNamespace(content=text)

    monkeypatch.setattr(runners, "run_agent", fake_agent)
    with session_scope() as s:
        specs = {
            n.node_key: {"id": n.id, "name": n.name, "phase": n.phase,
                         "config": dict(n.config_json or {}),
                         "assignee": dict(n.assignee_json or {})}
            for n in s.query(Node).filter(Node.run_id == run_id).all()
        }
    for key in DISCUSSION_NODES:
        spec = specs[key]
        ctx = runners.NodeContext(
            run_id=run_id, node_id=spec["id"], node_key=key, type="AGENT",
            name=spec["name"], phase=spec["phase"], config=spec["config"],
            assignee=spec["assignee"], attempt_id=0, attempt_no=1,
            idea="三方讨论测试想法",
        )
        result = asyncio.run(runners.execute_node(ctx))
        assert result["artifact"]["kind"] == "discussion"


def test_discussion_append_chain_reaches_v4(monkeypatch) -> None:
    run_id = _make_run()
    _run_discussion_nodes(run_id, monkeypatch)
    meta, body = parse_front_matter(read_artifact(run_id, "discussion"))
    assert meta["version"] == 4
    h2 = [ln.strip() for ln in body.splitlines() if ln.startswith("## ")]
    for title in ("## 主张", "## 质疑", "## 回应", "## 共识蒸馏"):
        assert title in h2, h2


def test_discussion_mod_quotes_original_text(monkeypatch) -> None:
    run_id = _make_run()
    _run_discussion_nodes(run_id, monkeypatch)
    _meta, body = parse_front_matter(read_artifact(run_id, "discussion"))
    quotes = [ln for ln in body.splitlines() if ln.strip().startswith('> "')]
    assert len(quotes) >= 2


def test_gate_proposal_description_includes_pending_questions(monkeypatch) -> None:
    run_id = _make_run()
    _run_discussion_nodes(run_id, monkeypatch)
    gate = request_gate(run_id, _node_id(run_id, "gate_proposal"))
    assert "讨论未决项" in gate["description"]
    assert "- [ ] 人工门的具体位置" in gate["description"]


def test_get_gate_cold_read_keeps_pending_questions(monkeypatch) -> None:
    """get_gate 走独立 session 冷读（模拟前端刷新/重载），未决项仍在。"""
    run_id = _make_run()
    _run_discussion_nodes(run_id, monkeypatch)
    gate = request_gate(run_id, _node_id(run_id, "gate_proposal"))

    cold = get_gate(gate["id"])
    assert cold is not None
    assert cold["title"] == gate["title"]
    assert "讨论未决项" in cold["description"]
    assert "- [ ] 人工门的具体位置" in cold["description"]
    # 只追加一次，冷读不会把未决项段落再拼一遍。
    assert cold["description"].count("讨论未决项（需人工拍板）：") == 1


def test_snapshot_gate_row_includes_pending_questions(monkeypatch) -> None:
    """GET /api/runs/{run_id} 快照的 gate 行带 description，刷新后卡片仍可见未决项。"""
    run_id = _make_run()
    _run_discussion_nodes(run_id, monkeypatch)
    gate = request_gate(run_id, _node_id(run_id, "gate_proposal"))

    async def no_recover() -> None:
        # 不启动编排器：否则本进程其他用例遗留的 running run 会被拉起并调用模型。
        return None

    monkeypatch.setattr(orchestrator, "recover", no_recover)
    with TestClient(app) as client:
        response = client.get(f"/api/runs/{run_id}")
    assert response.status_code == 200, response.text
    row = next(item for item in response.json()["gates"] if item["id"] == gate["id"])
    assert row["status"] == "open"
    assert row["node_key"] == "gate_proposal"
    assert row["title"] == gate["title"]
    assert row["created_at"]
    assert "讨论未决项" in row["description"]
    assert "- [ ] 人工门的具体位置" in row["description"]


def test_pending_questions_limited_to_pending_section() -> None:
    run_id = _make_run()
    write_artifact(run_id, "discussion", SCOPED_DISCUSSION, created_by="test", inputs=[])
    with session_scope() as s:
        items = _pending_questions(s, run_id)
    assert items == ["- [ ] 段内一：人工门的具体位置", "- [ ] 段内二：实时协作的排期"]


def test_pending_questions_falls_back_to_full_scan_without_heading() -> None:
    """旧工件没有「未决项」标题：退回全文扫描，保持原有行为。"""
    run_id = _make_run()
    body = "# 旧讨论稿\n\n## 待办\n\n- [ ] 旧格式问题一\n- [ ] 旧格式问题二\n"
    write_artifact(run_id, "discussion", body, created_by="test", inputs=[])
    with session_scope() as s:
        items = _pending_questions(s, run_id)
    assert items == ["- [ ] 旧格式问题一", "- [ ] 旧格式问题二"]
