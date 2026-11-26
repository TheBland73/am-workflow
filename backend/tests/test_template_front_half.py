"""模板的前半段：节点、依赖、参数表、自动化等级预设；运行级配置写进会议节点。"""
from __future__ import annotations

from app.db.base import init_db, session_scope
from app.db.models import Node, Run, Workflow
from app.engine.params import apply_run_config
from app.engine.templates import instantiate_nodes, seed_defaults, workflow_definition


def _definition() -> dict:
    """读的是已落库的模板：先确保库与默认模板存在，单独运行本文件也能通过。"""
    init_db()
    seed_defaults()
    return workflow_definition("sdlc_v1")


def test_front_half_nodes_and_dependencies() -> None:
    definition = _definition()
    nodes = {node["key"]: node for node in definition["nodes"]}
    assert nodes["discuss"]["type"] == "MEETING"
    assert nodes["discuss"]["config"]["protocol"] == "discussion"
    assert nodes["review"]["deps"] == ["gate_proposal", "discuss"]
    assert nodes["review"]["config"]["protocol"] == "review"
    assert nodes["confirm"]["deps"] == ["requirement"]
    assert nodes["design"]["deps"] == ["gate_requirement"]
    for key in ("discuss_pro", "discuss_con", "discuss_response", "discuss_mod"):
        assert key not in nodes
    # 后半段的节点保持不变
    for key in ("design", "tasks", "taskgraph", "gate_tasks", "expand_exec", "verify",
                "gate_accept", "delivery"):
        assert key in nodes
    keys = [phase["key"] for phase in definition["phases"]]
    assert keys == ["idea", "discussion", "proposal", "review", "requirement",
                    "design", "tasks", "execution", "delivery"]


def test_params_and_levels_are_declared() -> None:
    definition = _definition()
    discussion = definition["params"]["discussion"]
    assert discussion["rounds_max"]["default"] == 20
    assert discussion["participants_min"]["default"] == 3
    assert discussion["participants_max"]["max"] == 99
    assert definition["automation_levels"]["L2"]["consensus"] == "program"
    assert set(definition["rubrics"]) == {"proposal", "review", "requirement", "artifact"}


def test_run_config_is_applied_to_meeting_nodes() -> None:
    init_db()
    seed_defaults()
    config = {"params": {"discussion": {"rounds_max": 6}}}
    with session_scope() as s:
        workflow_id = s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").one().id
        s.add(Run(id="params-run", workflow_id=workflow_id, status="created", config_json=config))
        s.flush()
    definition = workflow_definition("sdlc_v1")
    instantiate_nodes("params-run", definition)
    assert apply_run_config("params-run", definition, config) == []
    with session_scope() as s:
        nodes = {n.node_key: n for n in s.query(Node).filter(Node.run_id == "params-run").all()}
    assert nodes["discuss"].config_json["params"]["rounds_max"] == 6
    assert nodes["discuss"].config_json["params"]["closure_rule"] == "unanimous"
    assert nodes["review"].config_json["max_rounds"] == 3       # 复审轮次上限取自参数表
    assert "params" not in nodes["design"].config_json           # 非会议节点不受影响
