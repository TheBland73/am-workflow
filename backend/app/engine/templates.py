"""模板装载：默认工作流与角色模板落库（设计规格 §4.3）。"""
from __future__ import annotations

import json

from app.agents.templates.roles import ROLES
from app.config import settings
from app.db.base import session_scope
from app.db.models import AgentTemplate, Node, Workflow
from app.engine.graph import compute_waves

WORKFLOW_FILES = {"sdlc_v1": "sdlc_workflow.json"}


def load_definition(template_key: str = "sdlc_v1") -> dict:
    filename = WORKFLOW_FILES.get(template_key)
    if not filename:
        raise ValueError(f"未知工作流模板：{template_key}")
    path = settings.templates_dir / filename
    if not path.exists():
        raise FileNotFoundError(f"模板文件缺失：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def seed_defaults() -> None:
    """启动时确保默认工作流与角色模板存在；工作流定义每次从文件刷新。"""
    definition = load_definition()
    with session_scope() as s:
        row = s.query(Workflow).filter(Workflow.template_key == definition["key"]).one_or_none()
        if row is None:
            s.add(Workflow(name=definition.get("name", definition["key"]),
                           template_key=definition["key"], definition_json=definition))
        else:
            row.name = definition.get("name", row.name)
            row.definition_json = definition
        existing = {r.role_key for r in s.query(AgentTemplate).all()}
        for role in ROLES:
            if role["role_key"] in existing:
                continue
            s.add(AgentTemplate(
                role_key=role["role_key"],
                name=role["name"],
                system_prompt=role["system_prompt"],
                tools_json=role.get("tools_json") or [],
                memory_scope=role.get("memory_scope", "agent"),
                max_steps=int(role.get("max_steps") or 3),
                stop_condition=role.get("stop_condition") or [],
            ))


def workflow_definition(template_key: str) -> dict:
    with session_scope() as s:
        row = s.query(Workflow).filter(Workflow.template_key == template_key).one_or_none()
        if row is None:
            return load_definition(template_key)
        return dict(row.definition_json or {})


def phase_names(definition: dict) -> dict[str, str]:
    return {p["key"]: p.get("name", p["key"]) for p in definition.get("phases", [])}


def instantiate_nodes(run_id: str, definition: dict) -> int:
    nodes_def = definition.get("nodes", [])
    deps_map = {n["key"]: list(n.get("deps") or []) for n in nodes_def}
    waves = compute_waves(deps_map)
    with session_scope() as s:
        for index, item in enumerate(nodes_def):
            s.add(Node(
                run_id=run_id,
                node_key=item["key"],
                type=item["type"],
                name=item.get("name", item["key"]),
                phase=item.get("phase", ""),
                status="pending",
                assignee_json=dict(item.get("assignee") or {}),
                deps_json=list(item.get("deps") or []),
                max_attempts=int(item.get("max_attempts") or 3),
                config_json=dict(item.get("config") or {}),
                order_index=index,
                wave=waves[item["key"]],
            ))
    return len(nodes_def)
