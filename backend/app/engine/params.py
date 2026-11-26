"""参数表与运行级配置。

参数表、自动化等级预设、各类确认点的判据文本都写在模板 JSON 里：
- params：按协议分组，每项含 type、default，以及 min、max、choices、title、recommend、hint
- automation_levels：自动化等级 → 各确认点的判定者
- rubrics：确认点的判据文本
运行级配置（runs.config_json）：
- automation: {"level": "L2", "checkpoints": {"consensus": "human", ...}}
- params: {"discussion": {"rounds_max": 10}, ...}
- rosters: {"participants": {...}, "reviewers": [...]}
"""
from __future__ import annotations

from app.conversation.policy import policy_from_config, validate_context_policy
from app.db.base import session_scope
from app.db.models import Node, Run, Workflow
from app.engine.templates import workflow_definition

DEFAULT_LEVEL = "L2"

# 每个确认点允许的判定者
VALID_DECIDERS: dict[str, tuple[str, ...]] = {
    "clarify": ("human", "proxy"),
    "roster": ("human", "auto"),
    "consensus": ("human", "program", "program_fallback"),
    "proposal": ("human", "auto"),
    "review": ("human", "model"),
    "requirement": ("human", "model"),
}

_KIND_LABEL = {"int": "整数", "float": "数字", "bool": "布尔值"}


def _coerce(key: str, spec: dict, raw) -> tuple[object | None, str | None]:
    kind = spec.get("type", "str")
    title = spec.get("title", key)
    try:
        if kind == "int":
            if isinstance(raw, bool):
                raise ValueError
            value: object = int(raw)
        elif kind == "float":
            value = float(raw)
        elif kind == "bool":
            value = (str(raw).strip().lower() in ("1", "true", "yes", "on")) if isinstance(raw, str) else bool(raw)
        else:
            value = str(raw)
    except (TypeError, ValueError):
        return None, f"{title}须是{_KIND_LABEL.get(kind, '文本')}"
    if kind in ("int", "float"):
        if "min" in spec and value < spec["min"]:
            return None, f"{title}不能小于 {spec['min']}"
        if "max" in spec and value > spec["max"]:
            return None, f"{title}不能大于 {spec['max']}"
    if spec.get("choices") and value not in spec["choices"]:
        return None, f"{title}只能是 {'、'.join(map(str, spec['choices']))} 之一"
    return value, None


def _cross_rules(values: dict) -> list[str]:
    errors: list[str] = []
    pairs = (("participants_min", "participants_max", "参会人数"),
             ("reviewers_min", "reviewers_max", "评审者人数"))
    for low, high, title in pairs:
        if low in values and high in values and values[low] > values[high]:
            errors.append(f"{title}的下限不能大于上限")
    if "compact_at" in values or "compact_to" in values:
        # 压缩参数之间的联动规则与通用上下文模块保持同一份实现
        errors += validate_context_policy(policy_from_config(values))
    return errors


def merge_params(schema: dict, overrides: dict | None) -> tuple[dict, list[str]]:
    """返回 (生效值, 错误列表)。不在参数表里的覆盖项被忽略。"""
    overrides = overrides or {}
    values: dict = {}
    errors: list[str] = []
    for key, spec in schema.items():
        value, error = _coerce(key, spec, overrides.get(key, spec.get("default")))
        if error:
            errors.append(error)
            value = spec.get("default")
        values[key] = value
    return values, errors + _cross_rules(values)


def effective_params(definition: dict, config: dict | None) -> dict[str, dict]:
    """每个参数分组的生效值：模板默认值叠加运行级配置。有错误的项回落到默认值。"""
    config = config or {}
    overrides = config.get("params") or {}
    return {section: merge_params(schema, overrides.get(section))[0]
            for section, schema in (definition.get("params") or {}).items()}


def _deciders_for(definition: dict, checkpoint: str) -> tuple[str, ...] | None:
    """某个确认点允许的判定者；不是确认点时返回 None。

    模板里的人工门节点也可以按节点键逐个配置（设计之后的门想用模型判定时这样做），
    这类确认点的判定者只能是人或模型。
    """
    if checkpoint in VALID_DECIDERS:
        return VALID_DECIDERS[checkpoint]
    is_gate = any(node.get("type") == "HUMAN_GATE" and node.get("key") == checkpoint
                  for node in definition.get("nodes") or [])
    return ("human", "model") if is_gate else None


def validate_run_config(definition: dict, config: dict | None) -> list[str]:
    config = config or {}
    errors: list[str] = []
    automation = dict(config.get("automation") or {})
    level = str(automation.get("level") or DEFAULT_LEVEL)
    if level not in (definition.get("automation_levels") or {}):
        errors.append(f"未知的自动化等级：{level}")
    for checkpoint, decider in dict(automation.get("checkpoints") or {}).items():
        allowed = _deciders_for(definition, checkpoint)
        if allowed is None:
            errors.append(f"未知的确认点：{checkpoint}")
        elif decider not in allowed:
            errors.append(f"确认点 {checkpoint} 的判定者只能是 {allowed} 之一")
    params = definition.get("params") or {}
    for section, values in dict(config.get("params") or {}).items():
        if section not in params:
            errors.append(f"未知的参数分组：{section}")
            continue
        errors += merge_params(params[section], values)[1]
    return errors + validate_rosters(config.get("rosters"))


def decider_for(run_config: dict | None, definition: dict | None, checkpoint: str,
                default: str = "human") -> str:
    """逐确认点覆盖 > 自动化等级预设 > 默认值。"""
    automation = dict((run_config or {}).get("automation") or {})
    override = dict(automation.get("checkpoints") or {}).get(checkpoint)
    if override:
        return str(override)
    levels = dict((definition or {}).get("automation_levels") or {})
    preset = levels.get(str(automation.get("level") or DEFAULT_LEVEL)) or {}
    return str(preset.get(checkpoint) or default)


def apply_run_config(run_id: str, definition: dict, config: dict | None,
                     *, only_pending: bool = False) -> list[str]:
    """把生效参数写进会议节点的 config.params；复审与需求复核的轮次上限写进 max_rounds。

    only_pending 为真时只处理还没开始执行的会议节点。返回参数校验的错误列表。
    """
    params = effective_params(definition, config)
    errors = [error for section in params for error in merge_params(
        (definition.get("params") or {}).get(section, {}),
        ((config or {}).get("params") or {}).get(section))[1]]
    with session_scope() as s:
        for node in s.query(Node).filter(Node.run_id == run_id, Node.type == "MEETING").all():
            if only_pending and node.status not in ("pending", "ready"):
                continue
            protocol = (node.config_json or {}).get("protocol")
            if protocol not in params:
                continue
            config_json = dict(node.config_json or {})
            config_json["params"] = params[protocol]
            if protocol in ("review", "confirm"):
                config_json["max_rounds"] = params[protocol]["rounds_max"]
            node.config_json = config_json
    return errors


def run_of(run_id: str) -> dict:
    """取运行的模板键与运行级配置。"""
    with session_scope() as s:
        run = s.get(Run, run_id)
        if run is None:
            return {"template_key": "", "config": {}}
        workflow = s.get(Workflow, run.workflow_id)
        return {"template_key": workflow.template_key if workflow else "",
                "config": dict(run.config_json or {})}


def definition_of(run_id: str) -> dict:
    """运行的模板定义：自动化等级预设与判据文本从这里读。"""
    template_key = run_of(run_id)["template_key"]
    return workflow_definition(template_key) if template_key else {}


_ROSTER_GROUPS = ("participants", "reviewers")


def validate_rosters(rosters: dict | None) -> list[str]:
    """名单配置的结构检查：每一组要么点名具体角色，要么给固定人数，要么不写。"""
    errors: list[str] = []
    for group, item in dict(rosters or {}).items():
        if group not in _ROSTER_GROUPS:
            errors.append(f"未知的名单分组：{group}")
            continue
        item = item if isinstance(item, dict) else {}
        label = "参会者" if group == "participants" else "评审者"
        keys, count = item.get("role_keys"), item.get("count")
        if keys is not None and count is not None:
            errors.append(f"{label}只能指定具体角色或固定人数之一")
        if keys is not None and not (isinstance(keys, list) and keys
                                     and all(isinstance(key, str) and key for key in keys)):
            errors.append(f"{label}的具体角色须是非空的字符串数组")
        if count is not None and (isinstance(count, bool) or not isinstance(count, int)
                                  or not 1 <= count <= 99):
            errors.append(f"{label}的固定人数须是 1 到 99 的整数")
    return errors
