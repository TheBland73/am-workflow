"""需求追踪矩阵的解析与机械核对、需求确认的汇总裁决规则。"""
from __future__ import annotations

from app.meeting.confirm_rules import (changed_requirements, closed_points,
                                       confirm_verdict, mechanical_check, parse_matrix,
                                       parse_non_requirements, parse_requirements)

SPEC = """# 需求规格

### R1 收集反馈
WHEN 团队成员提交反馈 THE SYSTEM SHALL 保存反馈内容。
- Given 一个登录用户
- When 提交一条反馈
- Then 反馈出现在列表里

### R2 整理反馈
WHEN 负责人打开列表 THE SYSTEM SHALL 按时间排序。
- Given 已有反馈
- When 打开列表
- Then 按时间倒序显示

### R3 导出周报
WHEN 负责人点击导出 THE SYSTEM SHALL 生成周报文件。
- Given 已有反馈
- When 点击导出
- Then 下载到本地

## 需求追踪矩阵

| 需求编号 | 来源决策点 | 方案章节 | 优先级 |
|---|---|---|---|
| R1 | D1 | 总体方案 | P1 |
| R2 | D1 | 关键流程 | P1 |
| R3 | D2 | 里程碑与验收 | P2 |

## 非需求类决策

| 决策点 | 不产出需求的理由 |
|---|---|
| D3 | 过程性决策 |
"""

POINTS = [{"index": "D1", "status": "agreed"},
          {"index": "D2", "status": "agreed"},
          {"index": "D3", "status": "ruled"}]


def test_parsers_read_requirements_matrix_and_non_requirements() -> None:
    requirements = parse_requirements(SPEC)
    assert [item["id"] for item in requirements] == ["R1", "R2", "R3"]
    assert requirements[0]["title"] == "收集反馈"
    assert "保存反馈内容" in requirements[0]["body"]
    matrix = parse_matrix(SPEC)
    assert matrix[2] == {"requirement": "R3", "points": ["D2"],
                         "section": "里程碑与验收", "priority": "P2"}
    assert parse_non_requirements(SPEC) == ["D3"]
    assert [point["index"] for point in closed_points(POINTS)] == ["D1", "D2", "D3"]


def test_a_complete_spec_passes_the_mechanical_check() -> None:
    assert mechanical_check(SPEC, POINTS) == []


def test_duplicate_requirement_ids_are_reported() -> None:
    duplicated = SPEC.replace("### R3 导出周报", """### R2 重复的整理
WHEN 负责人再次打开列表 THE SYSTEM SHALL 仍然按时间排序。
- Given 已有反馈
- When 再次打开
- Then 仍然倒序

### R3 导出周报""")
    issues = mechanical_check(duplicated, POINTS)
    assert any("R2" in issue and "重复" in issue for issue in issues)


def test_a_requirement_missing_from_the_matrix_is_reported() -> None:
    extra = SPEC.replace("## 需求追踪矩阵", """### R4 提醒
WHEN 反馈积压 THE SYSTEM SHALL 提醒负责人。
- Given 反馈积压
- When 超过一天
- Then 收到提醒

## 需求追踪矩阵""")
    issues = mechanical_check(extra, POINTS)
    assert any("R4" in issue and "追踪矩阵" in issue for issue in issues)


def test_a_matrix_row_pointing_at_an_open_point_is_reported() -> None:
    points = POINTS + [{"index": "D9", "status": "open"}]
    spec = SPEC.replace("| R3 | D2 | 里程碑与验收 | P2 |",
                        "| R3 | D2、D9 | 里程碑与验收 | P2 |")
    issues = mechanical_check(spec, points)
    assert any("R3" in issue and "D9" in issue for issue in issues)


def test_a_closed_point_without_a_landing_is_reported() -> None:
    spec = SPEC.split("## 非需求类决策")[0]
    issues = mechanical_check(spec, POINTS)
    assert any("D3" in issue and "非需求类决策" in issue for issue in issues)


def test_pending_wording_in_a_requirement_is_reported() -> None:
    spec = SPEC.replace("WHEN 负责人打开列表 THE SYSTEM SHALL 按时间排序。",
                        "WHEN 负责人打开列表 THE SYSTEM SHALL 按时间排序（字段待定）。")
    issues = mechanical_check(spec, POINTS)
    assert any("R2" in issue and "待定" in issue for issue in issues)


def test_missing_ears_and_acceptance_are_two_separate_issues() -> None:
    spec = SPEC.replace(
        "WHEN 团队成员提交反馈 THE SYSTEM SHALL 保存反馈内容。\n"
        "- Given 一个登录用户\n- When 提交一条反馈\n- Then 反馈出现在列表里",
        "收集反馈。")
    issues = mechanical_check(spec, POINTS)
    assert len(issues) == 2
    assert all("R1" in issue for issue in issues)


def test_an_invalid_priority_is_reported() -> None:
    spec = SPEC.replace("| R1 | D1 | 总体方案 | P1 |", "| R1 | D1 | 总体方案 | P9 |")
    issues = mechanical_check(spec, POINTS)
    assert any("R1" in issue and "P1" in issue for issue in issues)


def test_verdict_follows_the_item_kinds() -> None:
    assert confirm_verdict([{"kind": "quality", "severity": "minor"}]) == "pass"
    assert confirm_verdict([{"kind": "quality", "severity": "major"}]) == "fail"
    assert confirm_verdict([{"kind": "quality", "severity": "major"}],
                           blocking_line="blocker") == "pass"
    for kind in ("mechanical", "distorted", "missing", "added"):
        assert confirm_verdict([{"kind": kind, "severity": "minor"}]) == "fail"


def test_changed_requirements_lists_only_changed_bodies() -> None:
    assert changed_requirements({"R1": "甲", "R2": "乙"}, {"R1": "甲", "R2": "丙"}) == ["R2"]
