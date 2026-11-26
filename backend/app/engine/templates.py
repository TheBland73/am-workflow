"""模板装载：默认工作流与角色模板落库（设计规格 §4.3）。"""
from __future__ import annotations

import hashlib
import json

from app.agents.templates.roles import ROLES
from app.config import settings
from app.db.base import session_scope
from app.db.models import AgentTemplate, Node, Workflow
from app.engine.graph import compute_waves

WORKFLOW_FILES = {"sdlc_v1": "sdlc_workflow.json"}

# 参与「内置指纹」的字段：用户可通过 PATCH /agent-templates/{id} 编辑，
# 变化即视为自定义。default_model_id 是本机模型绑定、created_at 是行元数据，
# 二者不参与指纹，升级时永远原样保留。
BUILTIN_FIELDS = (
    "name",
    "system_prompt",
    "tools_json",
    "memory_scope",
    "max_steps",
    "stop_condition",
    "meta_json",
)

# 不再内置的角色：启动时清理没被用户改过的；改过的和旧库里没有指纹的保留为自定义角色
RETIRED_ROLES = ("discuss_pro", "discuss_con", "discuss_mod", "proposal")


def load_definition(template_key: str = "sdlc_v1") -> dict:
    filename = WORKFLOW_FILES.get(template_key)
    if not filename:
        raise ValueError(f"未知工作流模板：{template_key}")
    path = settings.templates_dir / filename
    if not path.exists():
        raise FileNotFoundError(f"模板文件缺失：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_json(data: dict) -> str:
    """稳定序列化：键排序 + 紧凑分隔符 + 保留中文，同一份内容必得同一串。"""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def builtin_fingerprint(fields: dict) -> str:
    """七个可编辑字段的 sha256 指纹（规范化 JSON 的 UTF-8 字节）。"""
    return hashlib.sha256(_canonical_json(fields).encode("utf-8")).hexdigest()


def role_fields(role: dict) -> dict:
    """内置角色定义 → 落库字段；默认值与建新行时的取值保持一致。"""
    return {
        "name": role["name"],
        "system_prompt": role["system_prompt"],
        "tools_json": list(role.get("tools_json") or []),
        "memory_scope": role.get("memory_scope", "agent"),
        "max_steps": int(role.get("max_steps") or 3),
        "stop_condition": list(role.get("stop_condition") or []),
        "meta_json": dict(role.get("meta") or {}),
    }


def template_fields(row: AgentTemplate) -> dict:
    """DB 行 → 同样的七个字段（JSON 列已反序列化为 Python 对象，可直接哈希）。"""
    return {
        "name": row.name,
        "system_prompt": row.system_prompt,
        "tools_json": list(row.tools_json or []),
        "memory_scope": row.memory_scope,
        "max_steps": int(row.max_steps or 0),
        "stop_condition": list(row.stop_condition or []),
        "meta_json": dict(row.meta_json or {}),
    }


def role_fingerprint(role: dict) -> str:
    return builtin_fingerprint(role_fields(role))


def template_fingerprint(row: AgentTemplate) -> str:
    """按行当前字段重算指纹；与 row.builtin_hash 不等即说明用户改过。"""
    return builtin_fingerprint(template_fields(row))


def _refresh_builtin_fields(row: AgentTemplate, fields: dict) -> None:
    for name in BUILTIN_FIELDS:
        setattr(row, name, fields[name])


def _sync_builtin_row(row: AgentTemplate, role: dict) -> None:
    """把已有行升级到当前内置定义，三种判定（幂等）：

    a) ``builtin_hash is None``：旧库（本列引入前落库）无法区分「用户改动」与
       「旧内置定义」，只能接受一次性覆盖——刷新七字段并写入当前指纹；
    b) ``template_fingerprint(row) == row.builtin_hash``：用户没改过，内置定义
       变了就刷新字段并更新指纹；
    c) 其余：用户经 PATCH 改过，完全不动（含 system_prompt 与 builtin_hash）。

    任何分支都不碰 default_model_id（本机模型绑定）与 created_at。
    """
    fields = role_fields(role)
    target = builtin_fingerprint(fields)
    if row.builtin_hash is None:
        _refresh_builtin_fields(row, fields)
        row.builtin_hash = target
        return
    if template_fingerprint(row) != row.builtin_hash:
        return
    if row.builtin_hash != target:
        _refresh_builtin_fields(row, fields)
        row.builtin_hash = target


def seed_defaults() -> None:
    """启动时确保默认工作流与角色模板存在；工作流定义每次从文件刷新。

    角色模板按 role_key 幂等：缺行则连同内置指纹一起插入，已有行交给
    ``_sync_builtin_row`` 判定是升级还是保留用户自定义。
    """
    definition = load_definition()
    with session_scope() as s:
        row = s.query(Workflow).filter(Workflow.template_key == definition["key"]).one_or_none()
        if row is None:
            s.add(Workflow(name=definition.get("name", definition["key"]),
                           template_key=definition["key"], definition_json=definition))
        else:
            row.name = definition.get("name", row.name)
            row.definition_json = definition
        for role_key in RETIRED_ROLES:
            retired = s.query(AgentTemplate).filter(AgentTemplate.role_key == role_key).first()
            if retired is None:
                continue
            unmodified = (retired.builtin_hash
                          and template_fingerprint(retired) == retired.builtin_hash)
            if unmodified:
                s.delete(retired)                # 没改过：清理
                continue
            retired.builtin_hash = None          # 改过，或旧库没有指纹：保留为自定义角色，之后不再触碰
        s.flush()
        existing = {r.role_key: r for r in s.query(AgentTemplate).all()}
        for role in ROLES:
            template = existing.get(role["role_key"])
            if template is None:
                s.add(AgentTemplate(
                    role_key=role["role_key"],
                    builtin_hash=role_fingerprint(role),
                    **role_fields(role),
                ))
            else:
                _sync_builtin_row(template, role)


def workflow_definition(template_key: str) -> dict:
    with session_scope() as s:
        row = s.query(Workflow).filter(Workflow.template_key == template_key).one_or_none()
        if row is None:
            return load_definition(template_key)
        return dict(row.definition_json or {})


def phase_names(definition: dict) -> dict[str, str]:
    return {p["key"]: p.get("name", p["key"]) for p in definition.get("phases", [])}


def role_library() -> list[dict]:
    """角色库：每个角色的键、名称、提示词与元数据。主持人挑名单、前端列角色都用它。"""
    with session_scope() as s:
        return [
            {"role_key": r.role_key, "name": r.name, "system_prompt": r.system_prompt,
             "meta": dict(r.meta_json or {}), "builtin": bool(r.builtin_hash),
             "default_model_id": r.default_model_id}
            for r in s.query(AgentTemplate).order_by(AgentTemplate.id.asc()).all()
        ]


def role_template(role_key: str) -> dict | None:
    """取一个角色；不存在时返回 None。"""
    with session_scope() as s:
        row = s.query(AgentTemplate).filter(AgentTemplate.role_key == role_key).first()
        if row is None:
            return None
        return {"role_key": row.role_key, "name": row.name, "system_prompt": row.system_prompt,
                "meta": dict(row.meta_json or {}), "default_model_id": row.default_model_id}


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
