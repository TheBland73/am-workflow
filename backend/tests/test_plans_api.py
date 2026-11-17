"""编排工作台 API：创建 / 校验 / 编辑 / 提交（save）/ 幂等 / 归档边界。"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.db.base import session_scope
from app.db.models import PlanTask
from app.main import app

BACKEND_DIR = Path(__file__).resolve().parents[1]
DESIGN = (BACKEND_DIR / "tests" / "fixtures" / "designs" / "chat-product.md").read_text(encoding="utf-8")

TASK_TITLES = [
    "梳理目标与范围", "建立会话模型", "实现检索与知识库", "实现界面与交互",
    "补齐权限与安全", "编写测试与回归", "配置部署与监控", "输出验收与文档",
]


def seed_tasks(plan_id: str, count: int = 8) -> None:
    from app.engine.planner import extract_sections

    sections = extract_sections(DESIGN)
    with session_scope() as session:
        for index in range(count):
            session.add(
                PlanTask(
                    plan_id=plan_id,
                    task_uid=f"T{index + 1}",
                    title=TASK_TITLES[index],
                    description=(
                        "依据设计章节完成实现、边界处理与自测，产出可运行代码并保证"
                        "与上下游接口一致，失败路径有明确提示。"
                    ),
                    deps_json=[f"T{index}"] if index else [],
                    design_section=sections[index]["title"],
                    acceptance_json=["主流程有单元测试覆盖", "验收命令退出码为 0"],
                    role_key="dev",
                    effort="M",
                    priority="P1",
                    order_index=index,
                )
            )


def test_plan_create_validate_commit_and_idempotent() -> None:
    with TestClient(app) as client:
        created = client.post("/api/plans", json={
            "product_name": "客户反馈收集与归类",
            "source_type": "text",
            "design_text": DESIGN,
        })
        assert created.status_code == 201, created.text
        detail = created.json()
        plan_id = detail["plan"]["id"]
        assert detail["plan"]["status"] == "draft"
        assert len(detail["plan"]["sections"]) == 10
        assert detail["tasks"] == []

        seed_tasks(plan_id)
        validated = client.post(f"/api/plans/{plan_id}/validate")
        assert validated.status_code == 200, validated.text
        body = validated.json()
        assert body["status"] == "valid"
        assert body["snapshot"]["metrics"]["task_count"] == 8
        assert body["snapshot"]["metrics"]["wave_count"] == 8
        assert body["snapshot"]["critical_path"][0] == "T1"
        coverage = body["snapshot"]["coverage"]
        assert coverage["total"] == 10
        assert coverage["covered"] == 8
        assert len(coverage["uncovered"]) == 2
        warning_codes = {item["code"] for item in body["snapshot"]["validation"]["warnings"]}
        assert "section_uncovered" in warning_codes
        assert body["warnings"] == body["snapshot"]["validation"]["warnings"]
        assert client.get(f"/api/plans/{plan_id}").json()["plan"]["status"] == "valid"
        persisted = client.get(f"/api/plans/{plan_id}").json()["snapshot"]["coverage"]
        assert persisted["covered"] == 8

        committed = client.post(f"/api/plans/{plan_id}/commit", json={"mode": "save"})
        assert committed.status_code == 200, committed.text
        assert committed.json()["plan"]["status"] == "committed"
        assert committed.json()["plan"]["run_id"] is None

        again = client.post(f"/api/plans/{plan_id}/commit", json={"mode": "save"})
        assert again.status_code == 200, again.text
        assert again.json()["plan"]["status"] == "committed"

        listed = client.get("/api/plans").json()["items"]
        assert any(item["id"] == plan_id and item["task_count"] == 8 for item in listed)


def test_plan_edit_delete_and_archive() -> None:
    with TestClient(app) as client:
        created = client.post("/api/plans", json={
            "product_name": "内容选题与素材库",
            "source_type": "text",
            "design_text": (BACKEND_DIR / "tests" / "fixtures" / "designs" / "content-product.md").read_text(encoding="utf-8"),
        })
        assert created.status_code == 201, created.text
        plan_id = created.json()["plan"]["id"]

        added = client.post(f"/api/plans/{plan_id}/tasks", json={
            "title": "实现选题池", "description": "覆盖录入、筛选与状态流转，失败路径给出提示。",
            "design_section": "选题池", "deps": [], "acceptance": ["主流程可运行"],
            "role_key": "dev", "effort": "M", "priority": "P1",
        })
        assert added.status_code in (200, 201), added.text
        uid = added.json()["task_uid"]

        patched = client.patch(f"/api/plans/{plan_id}/tasks/{uid}", json={"title": "实现选题池与筛选"})
        assert patched.status_code == 200, patched.text
        assert patched.json()["title"] == "实现选题池与筛选"

        removed = client.delete(f"/api/plans/{plan_id}/tasks/{uid}")
        assert removed.status_code == 200, removed.text
        assert removed.json()["removed_from"] == []

        archived = client.post(f"/api/plans/{plan_id}/archive")
        assert archived.status_code == 200, archived.text
        assert archived.json()["plan"]["status"] == "archived"


def test_plan_commit_run_creates_nodes(monkeypatch) -> None:
    from app.api import plans as plans_api

    started: list[str] = []
    monkeypatch.setattr(plans_api.orchestrator, "start", lambda run_id: started.append(run_id))

    with TestClient(app) as client:
        created = client.post("/api/plans", json={
            "product_name": "客户反馈收集与归类",
            "source_type": "text",
            "design_text": DESIGN,
        })
        plan_id = created.json()["plan"]["id"]
        seed_tasks(plan_id)
        validated = client.post(f"/api/plans/{plan_id}/validate")
        assert validated.status_code == 200, validated.text

        committed = client.post(
            f"/api/plans/{plan_id}/commit", json={"mode": "run", "budget_tokens": 350_000}
        )
        assert committed.status_code == 200, committed.text
        run_id = committed.json()["run_id"]
        assert run_id
        assert started == [run_id]

        detail = client.get(f"/api/runs/{run_id}").json()
        assert detail["run"]["budget_tokens"] == 350_000
        assert "编排工作台" in detail["run"]["idea"]
        node_keys = {node["node_key"] for node in detail["nodes"]}
        assert "dev_T1" in node_keys and "dev_T8" in node_keys and "verify" in node_keys
        first = next(node for node in detail["nodes"] if node["node_key"] == "dev_T1")
        assert first["config"]["role_key"] == "dev"
        assert first["config"]["effort"] == "M"
        assert first["config"]["priority"] == "P1"
        assert first["config"]["description"]
        assert first["config"]["phase"]
        assert first["config"]["phase_index"] >= 1
        assert "阶段" in detail["run"]["idea"]
        assert len(detail["tasks"]) == 8
        kinds = {artifact["kind"] for artifact in detail["artifacts"]}
        assert "design" in kinds

        bad = client.post(
            f"/api/plans/{plan_id}/commit", json={"mode": "save", "budget_tokens": 5_000}
        )
        assert bad.status_code == 400
        assert bad.json()["detail"]["code"] == "budget"

        again = client.post(f"/api/plans/{plan_id}/commit", json={"mode": "run"})
        assert again.status_code == 200
        assert again.json()["run_id"] == run_id
        assert started == [run_id]


def test_plan_rejects_short_design_and_missing_plan() -> None:
    with TestClient(app) as client:
        bad = client.post("/api/plans", json={
            "product_name": "太短", "source_type": "text", "design_text": "# 标题\n\n太短了",
        })
        assert bad.status_code == 400
        assert bad.json()["detail"]["code"] == "design_too_short"
        assert client.get("/api/plans/nope").status_code == 404


def test_plan_canvas_layout_and_dependency_edges() -> None:
    with TestClient(app) as client:
        created = client.post("/api/plans", json={
            "product_name": "客户反馈收集与归类",
            "source_type": "text",
            "design_text": DESIGN,
        })
        assert created.status_code == 201, created.text
        plan_id = created.json()["plan"]["id"]
        seed_tasks(plan_id)

        saved = client.patch(f"/api/plans/{plan_id}/layout", json={
            "positions": {"T1": {"x": 40, "y": 60}, "T2": {"x": 340.56, "y": 88.2}},
        })
        assert saved.status_code == 200, saved.text
        assert saved.json()["positions"]["T2"] == {"x": 340.6, "y": 88.2}
        assert saved.json()["positions"]["T1"] == {"x": 40, "y": 60}
        assert client.get(f"/api/plans/{plan_id}").json()["plan"]["layout"]["T2"]["x"] == 340.6

        # T3 原本只依赖 T2，新增 T1 -> T3 的手工连线。
        added = client.post(f"/api/plans/{plan_id}/deps", json={"source": "T1", "target": "T3"})
        assert added.status_code == 201, added.text
        assert "T1" in added.json()["task"]["deps"]

        duplicate = client.post(f"/api/plans/{plan_id}/deps", json={"source": "T1", "target": "T3"})
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"]["code"] == "dependency_exists"

        self_dep = client.post(f"/api/plans/{plan_id}/deps", json={"source": "T4", "target": "T4"})
        assert self_dep.status_code == 400
        assert self_dep.json()["detail"]["code"] == "self_dependency"

        # T3 前依赖新增后，T3 -> T1 会成环。
        cycle = client.post(f"/api/plans/{plan_id}/deps", json={"source": "T3", "target": "T1"})
        assert cycle.status_code == 400, cycle.text
        assert cycle.json()["detail"]["code"] == "dependency_cycle"

        removed = client.delete(f"/api/plans/{plan_id}/deps", params={"source": "T1", "target": "T3"})
        assert removed.status_code == 200, removed.text
        assert "T1" not in removed.json()["task"]["deps"]

        missing = client.delete(f"/api/plans/{plan_id}/deps", params={"source": "T1", "target": "T3"})
        assert missing.status_code == 404

        archived = client.post(f"/api/plans/{plan_id}/archive")
        assert archived.status_code == 200
        locked = client.patch(f"/api/plans/{plan_id}/layout", json={"positions": {}})
        assert locked.status_code == 400
        assert locked.json()["detail"]["code"] == "plan_locked"


def test_plan_phases_and_activity() -> None:
    with TestClient(app) as client:
        created = client.post("/api/plans", json={
            "product_name": "客户反馈收集与归类",
            "source_type": "text",
            "design_text": DESIGN,
        })
        assert created.status_code == 201, created.text
        plan_id = created.json()["plan"]["id"]
        seed_tasks(plan_id)
        assert client.post(f"/api/plans/{plan_id}/validate").status_code == 200

        phases = client.get(f"/api/plans/{plan_id}/phases")
        assert phases.status_code == 200, phases.text
        body = phases.json()
        assert body["total_tasks"] == 8
        assert body["phase_count"] == 8
        assert body["covered_sections"] == 8
        assert [phase["index"] for phase in body["phases"]] == list(range(1, 9))
        assert all(phase["task_count"] == 1 for phase in body["phases"])
        assert body["phases"][0]["tasks"][0]["uid"] == "T1"
        assert body["phases"][0]["points"] == 2
        assert len(body["critical_path"]) == 8
        assert all(edge["kind"] in {"normal", "handoff", "critical"} for edge in body["edges"])
        assert body["orphan_tasks"] == []

        activity = client.get(f"/api/plans/{plan_id}/activity")
        assert activity.status_code == 200, activity.text
        items = activity.json()["items"]
        kinds = [item["kind"] for item in items]
        assert "created" in kinds and "validate" in kinds
        latest = next(item for item in items if item["kind"] == "validate")
        assert latest["ok"] is True
        assert latest["stats"]["tasks"] == 8

        assert client.get("/api/plans/nope/phases").status_code == 404
        assert client.get("/api/plans/nope/activity").status_code == 404


def test_activity_lines_normalizes_legacy_objects():
    from app.api.plans import _activity_lines

    assert _activity_lines(None) == []
    assert _activity_lines("纯文本") == ["纯文本"]
    assert _activity_lines({"message": "单条对象"}) == ["单条对象"]
    assert _activity_lines([
        {"code": "title_length", "message": "标题太短", "hint": "写清交付物", "details": {"uid": "T1"}},
        "字符串也行",
        {"code": "description_length", "message": "描述太短"},
    ]) == ["[T1] 标题太短（写清交付物）", "字符串也行", "描述太短"]

