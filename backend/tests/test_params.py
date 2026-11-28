"""参数表：硬限校验、默认值合并、跨字段规则；判定者的解析。"""
from __future__ import annotations

from app.engine.params import (decider_for, effective_params, merge_params, validate_rosters,
                               validate_run_config)

SCHEMA = {
    "rounds_max": {"type": "int", "default": 20, "min": 1, "max": 99, "title": "讨论轮次上限"},
    "closure_rule": {"type": "str", "default": "unanimous",
                     "choices": ["unanimous", "no_objection", "facilitator_rules"],
                     "title": "闭合规则"},
    "participants_min": {"type": "int", "default": 3, "min": 1, "max": 99, "title": "参会人数下限"},
    "participants_max": {"type": "int", "default": 5, "min": 1, "max": 99, "title": "参会人数上限"},
}
DEFINITION = {
    "params": {"discussion": SCHEMA},
    "automation_levels": {
        "L1": {"consensus": "human", "proposal": "human"},
        "L2": {"consensus": "program", "proposal": "human"},
        "L3": {"consensus": "program_fallback", "proposal": "auto"},
    },
}


def test_defaults_when_no_override() -> None:
    values, errors = merge_params(SCHEMA, None)
    assert errors == []
    assert values == {"rounds_max": 20, "closure_rule": "unanimous",
                      "participants_min": 3, "participants_max": 5}


def test_override_within_bounds_and_unknown_keys_ignored() -> None:
    values, errors = merge_params(SCHEMA, {"rounds_max": "7", "not_a_param": 1})
    assert errors == [] and values["rounds_max"] == 7


def test_out_of_bounds_and_bad_choice_are_errors() -> None:
    _, errors = merge_params(SCHEMA, {"rounds_max": 0, "closure_rule": "whatever",
                                      "participants_max": "abc"})
    assert any("讨论轮次上限" in e and "1" in e for e in errors)
    assert any("闭合规则" in e for e in errors)
    assert any("参会人数上限" in e for e in errors)


def test_min_greater_than_max_is_an_error() -> None:
    _, errors = merge_params(SCHEMA, {"participants_min": 6, "participants_max": 4})
    assert errors == ["参会人数的下限不能大于上限"]


def test_effective_params_and_run_config_validation() -> None:
    effective = effective_params(DEFINITION, {"params": {"discussion": {"rounds_max": 9}}})
    assert effective["discussion"]["rounds_max"] == 9
    assert validate_run_config(DEFINITION, {"params": {"discussion": {"rounds_max": 9}}}) == []
    assert validate_run_config(DEFINITION, {"automation": {"level": "L9"}})
    assert validate_run_config(DEFINITION, {"params": {"nope": {}}})


def test_decider_resolution_prefers_override_then_level() -> None:
    assert decider_for(None, DEFINITION, "consensus") == "program"          # 默认 L2
    assert decider_for({"automation": {"level": "L1"}}, DEFINITION, "consensus") == "human"
    assert decider_for({"automation": {"checkpoints": {"consensus": "human"}}},
                       DEFINITION, "consensus") == "human"
    assert decider_for({"automation": {"level": "L3"}}, DEFINITION, "proposal") == "auto"
    assert decider_for({"automation": {"level": "L3"}}, DEFINITION, "unknown_point") == "human"


def test_gates_of_the_template_can_opt_into_model_judging() -> None:
    definition = {**DEFINITION, "nodes": [{"key": "gate_design", "type": "HUMAN_GATE"}]}
    assert validate_run_config(definition, {"automation": {"checkpoints": {"gate_design": "model"}}}) == []
    assert validate_run_config(definition, {"automation": {"checkpoints": {"gate_design": "auto"}}})
    assert validate_run_config(definition, {"automation": {"checkpoints": {"gate_tasks": "model"}}})


def test_bad_config_shapes_are_reported_as_errors() -> None:
    assert validate_run_config(DEFINITION, {"automation": "L2"})
    assert validate_run_config(DEFINITION, {"automation": {"checkpoints": "bad"}})
    assert validate_run_config(DEFINITION, {"params": "bad"})
    assert validate_run_config(DEFINITION, {"rosters": ["x"]})


def test_roster_groups_must_be_objects_and_respect_the_hard_limit() -> None:
    assert validate_rosters({"participants": "architect"})
    assert validate_rosters({"participants": {"role_keys": [f"role_{index}" for index in range(100)]}})
    assert validate_rosters({"reviewers": {"count": True}})


def test_proposal_gate_does_not_accept_the_judge_model() -> None:
    """方案确认点在模型模式下直接送评审，不调用裁决模型（详设 §5.3）。"""
    definition = {**DEFINITION, "nodes": [{"key": "gate_proposal", "type": "HUMAN_GATE"}]}
    assert validate_run_config(
        definition, {"automation": {"checkpoints": {"gate_proposal": "model"}}})
    assert validate_run_config(
        definition, {"automation": {"checkpoints": {"gate_proposal": "auto"}}}) == []


def test_numeric_params_reject_truncation_and_non_finite_values() -> None:
    """整型参数不接受带小数的值，浮点参数不接受 NaN / inf（范围比较对它们失效）。"""
    from app.engine.params import _coerce

    assert _coerce("rounds", {"type": "int", "title": "轮次"}, 2.5)[1] == "轮次须是整数"
    assert _coerce("rounds", {"type": "int", "title": "轮次"}, 2.0)[0] == 2
    for bad in (float("nan"), float("inf"), float("-inf")):
        assert _coerce("threshold", {"type": "float", "title": "阈值"}, bad)[1] == "阈值须是数字"
