"""纯函数单测：拓扑分波、任务清单解析、工件 front-matter、校验、预算估算。"""
from __future__ import annotations

import pytest

from app.artifacts.store import parse_front_matter, render_front_matter, strip_outer_fence
from app.engine.graph import GraphError, compute_waves, topological_order
from app.engine.tasks import parse_tasks, validate_tasks
from app.engine.validate import validate_artifact
from app.models.budget import estimate_tokens, truncate_to_tokens


def test_compute_waves_parallel_and_barrier() -> None:
    deps = {"a": [], "b": [], "c": ["a"], "d": ["a", "b"], "e": ["c", "d"]}
    waves = compute_waves(deps)
    assert waves["a"] == waves["b"] == 1
    assert waves["c"] == 2
    assert waves["d"] == 2
    assert waves["e"] == 3
    assert topological_order(deps)[-1] == "e"


def test_compute_waves_detects_cycle_and_missing_dep() -> None:
    with pytest.raises(GraphError):
        compute_waves({"a": ["b"], "b": ["a"]})
    with pytest.raises(GraphError):
        compute_waves({"a": ["missing"]})


TASK_MD = """# 任务清单

- [ ] 实现接口 <!-- id: T1; deps: ; design-section: 2; test-cases: TC-01; assignee: dev; wave: 1; status: pending -->
- [ ] 写测试 <!-- id: T2; deps: T1; design-section: 3; test-cases: TC-02; assignee: dev; wave: 2; status: pending -->
"""


def test_parse_tasks_metadata() -> None:
    tasks = parse_tasks(TASK_MD)
    assert [t["task_uid"] for t in tasks] == ["T1", "T2"]
    assert tasks[1]["deps"] == ["T1"]
    assert tasks[0]["test_cases"] == ["TC-01"]
    assert tasks[0]["assignee"] == "dev"


def test_validate_tasks_duplicates_and_unknown_dep() -> None:
    dup = TASK_MD.replace("id: T2", "id: T1")
    with pytest.raises(ValueError):
        validate_tasks(parse_tasks(dup))
    unknown = TASK_MD.replace("deps: T1", "deps: T9")
    with pytest.raises(ValueError):
        validate_tasks(parse_tasks(unknown))


def test_front_matter_roundtrip() -> None:
    meta = {"id": "r1-idea", "kind": "idea", "status": "draft", "version": 1,
            "inputs": ["idea"], "created": "2026-11-14T00:00:00+00:00"}
    text = render_front_matter(meta, "# 标题\n\n正文")
    parsed, body = parse_front_matter(text)
    assert parsed["kind"] == "idea"
    assert parsed["version"] == 1
    assert body.strip().startswith("# 标题")


def test_strip_outer_fence() -> None:
    assert strip_outer_fence("```markdown\n# x\n```") == "# x"
    assert strip_outer_fence("# x") == "# x"


def test_validate_requirement_requires_ears_and_ac() -> None:
    bad = "# 需求\n\n只有一句话。"
    assert validate_artifact("requirement", bad)
    good = (
        "# 需求规格\n\n## 1. 范围\n\n覆盖想法到交付的最小闭环，不含向量记忆与多 run 并行。\n\n"
        "## 2. 功能需求\n\n"
        "### R1 导出\n\nWHEN 用户点击导出 THE SYSTEM SHALL 生成文件。\n\n"
        "验收标准：\n- Given 有数据，When 导出，Then 成功。\n\n"
        "### R2 创建\n\nWHEN 用户提交想法 THE SYSTEM SHALL 创建运行。\n\n"
        "验收标准：\n- Given 想法非空，When 提交，Then 返回快照。\n\n"
        "### R3 审批\n\nWHEN 运行到达人工门 THE SYSTEM SHALL 挂起等待审批。\n\n"
        "验收标准：\n- Given 门待审批，When 通过，Then 继续执行。\n"
    )
    assert validate_artifact("requirement", good) == []


def test_budget_estimate_and_truncate() -> None:
    assert estimate_tokens("你好世界") == 4
    assert estimate_tokens("abcdefgh") == 2
    truncated = truncate_to_tokens("你" * 100, 10)
    assert truncated.startswith("你" * 9) and "截断" in truncated
