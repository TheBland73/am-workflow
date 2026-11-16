"""编排工作台：章节抽取、结构校验、波次/关键路径、两份 fixture 的差异。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from app.engine.plan_generation import _extract_json
from app.engine.planner import (
    build_snapshot,
    extract_sections,
    match_section,
    validate_plan,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]
FIXTURES = BACKEND_DIR / "tests" / "fixtures" / "designs"
sys.path.insert(0, str(BACKEND_DIR / "scripts"))


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_task(uid: str, deps: list[str], section: str, **overrides):
    task = {
        "uid": uid,
        "title": f"实现{uid}模块",
        "description": "依据设计章节完成实现、边界处理与自测，产出可运行代码并保证与上下游接口一致。",
        "deps": deps,
        "design_section": section,
        "acceptance": ["主流程有单元测试覆盖", "验收命令退出码为 0"],
        "role": "dev",
        "effort": "M",
        "priority": "P1",
    }
    task.update(overrides)
    return task


def mock_tasks(design: str, product: str) -> list[dict]:
    from mock_provider import _pm_plan

    sections = extract_sections(design)
    index = "\n".join(f"- {item['title']}" for item in sections)
    messages = [{
        "role": "user",
        "content": (
            f"产品：{product}\n\n## 设计章节索引\n{index}\n\n"
            f"## 设计规格正文（截断）\n{design[:2000]}"
        ),
    }]
    return json.loads(_pm_plan(messages))["tasks"]


def test_coverage_reports_uncovered_sections() -> None:
    design = read_fixture("chat-product.md")
    sections = [item["title"] for item in extract_sections(design)]
    tasks = [
        make_task(f"T{index + 1}", [f"T{index}"] if index else [], sections[index % 2])
        for index in range(8)
    ]
    result = validate_plan(tasks, design)
    assert result["ok"], result["errors"]
    coverage = result["snapshot"]["coverage"]
    assert coverage["total"] == len(sections)
    assert coverage["covered"] == 2
    assert coverage["ratio"] == round(2 / len(sections), 3)
    asserted = [entry for entry in coverage["sections"] if entry["tasks"]]
    assert {entry["title"] for entry in asserted} == set(sections[:2])
    assert len(coverage["uncovered"]) == len(sections) - 2
    codes = {warning["code"] for warning in result["warnings"]}
    assert "section_uncovered" in codes
    assert result["snapshot"]["validation"]["warnings"] == result["warnings"]


def test_coverage_full_has_no_warnings() -> None:
    design = read_fixture("chat-product.md")
    sections = [item["title"] for item in extract_sections(design)]
    tasks = [
        make_task(f"T{index + 1}", [f"T{index}"] if index else [], sections[index])
        for index in range(len(sections))
    ]
    result = validate_plan(tasks, design)
    assert result["ok"], result["errors"]
    coverage = result["snapshot"]["coverage"]
    assert coverage["covered"] == coverage["total"]
    assert coverage["uncovered"] == []
    assert result["warnings"] == []


def test_sections_and_match() -> None:
    sections = extract_sections(read_fixture("chat-product.md"))
    assert len(sections) == 10
    assert sections[0]["level"] == 1
    assert match_section("1 目标与范围", sections)["title"] == "1 目标与范围"
    assert match_section("目标与范围", sections) is not None
    assert match_section("不存在的章节", sections) is None


def test_validate_ok_waves_and_critical_path() -> None:
    design = read_fixture("chat-product.md")
    sections = [item["title"] for item in extract_sections(design)]
    tasks = [
        make_task(f"T{index + 1}", [f"T{index}"] if index else [], sections[index])
        for index in range(8)
    ]
    result = validate_plan(tasks, design)
    assert result["ok"] is True, result["errors"]
    snapshot = result["snapshot"]
    assert snapshot["waves"] == {str(index + 1): [f"T{index + 1}"] for index in range(8)}
    assert snapshot["critical_path"] == [f"T{index + 1}" for index in range(8)]
    assert snapshot["metrics"]["critical_path_points"] == 16
    assert snapshot["metrics"]["wave_count"] == 8
    assert snapshot["dag"]["edges"][0] == {"from": "T1", "to": "T2"}


def test_validate_cycle_returns_path() -> None:
    design = read_fixture("chat-product.md")
    tasks = [
        make_task("T1", ["T2"], "1 目标与范围"),
        make_task("T2", ["T1"], "2 会话与消息模型"),
    ]
    result = validate_plan(tasks, design)
    assert result["ok"] is False
    cycle = [error for error in result["errors"] if error["code"] == "dependency_cycle"]
    assert cycle, result["errors"]
    assert "→" in cycle[0]["message"]


def test_validate_catches_common_errors() -> None:
    design = read_fixture("chat-product.md")
    tasks = [
        make_task("T1", [], "1 目标与范围"),
        make_task("T1", [], "1 目标与范围"),
        make_task("T2", ["T9"], "不存在的章节", acceptance=[]),
        make_task("T3", ["T3"], "2 会话与消息模型", role="wizard", effort="XXL"),
    ]
    result = validate_plan(tasks, design)
    codes = {error["code"] for error in result["errors"]}
    assert {"uid_duplicate", "dependency_missing", "section_unmatched",
            "self_dependency", "role_invalid", "effort_invalid",
            "acceptance_count"} <= codes


def test_snapshot_metrics_for_parallel_groups() -> None:
    tasks = [
        make_task("T1", [], "1 目标与范围", effort="M"),
        make_task("T2", ["T1"], "2 会话与消息模型", effort="L"),
        make_task("T3", ["T1"], "3 检索与知识库", effort="S"),
        make_task("T4", ["T2", "T3"], "4 界面与交互", effort="XL"),
    ]
    snapshot = build_snapshot([
        {**task, "task_uid": task["uid"], "role_key": task["role"]} for task in tasks
    ])
    assert snapshot["metrics"]["max_parallel"] == 2
    assert snapshot["metrics"]["wave_count"] == 3
    assert snapshot["metrics"]["critical_path_points"] == 2 + 3 + 5


def test_fixture_products_produce_different_plans() -> None:
    chat = mock_tasks(read_fixture("chat-product.md"), "客户反馈收集与归类")
    content = mock_tasks(read_fixture("content-product.md"), "内容选题与素材库")
    chat_result = validate_plan(chat, read_fixture("chat-product.md"))
    content_result = validate_plan(content, read_fixture("content-product.md"))
    assert chat_result["ok"] is True, chat_result["errors"]
    assert content_result["ok"] is True, content_result["errors"]

    assert len(chat) == 10
    assert len(content) == 9
    chat_roles = {task["role"] for task in chat}
    content_roles = {task["role"] for task in content}
    assert "design" in chat_roles
    assert "design" not in content_roles
    assert chat_roles != content_roles
    assert chat_result["snapshot"]["waves"] != content_result["snapshot"]["waves"]


@pytest.mark.parametrize("payload", [
    '```json\n{"tasks": [{"uid": "T1"}]}\n```',
    '前缀 {"tasks": [{"uid": "T1"}]} 后缀',
])
def test_extract_json_tolerates_fences_and_noise(payload: str) -> None:
    data = _extract_json(payload)
    assert data["tasks"][0]["uid"] == "T1"


def test_design_brief_keeps_head_and_tail_for_long_docs() -> None:
    from app.engine.plan_generation import _design_brief

    doc = "# 长文档\n\n" + "\n\n".join(
        f"## {index} 章节 {index}\n\n" + (f"第 {index} 章正文。" * 100) for index in range(1, 31)
    )
    assert len(doc) > 16000
    brief = _design_brief(doc)
    assert "章节索引" in brief
    assert "中间省略" in brief
    assert "章节 30" in brief
    assert "第 1 章正文" in brief
    assert doc[-40:].strip() in brief
    # 不应超过提示预算太多（允许索引与标注的固定开销）。
    assert len(brief) < 20000

