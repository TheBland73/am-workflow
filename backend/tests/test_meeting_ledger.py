"""账本规则、渲染与纪要：全部由程序完成，不调模型。"""
from __future__ import annotations

import json

from app.artifacts.store import artifacts_dir, parse_front_matter, read_artifact
from app.conversation.builder import Segment
from app.db.base import init_db, session_scope
from app.db.models import Node, Run, Workflow
from app.engine.templates import seed_defaults
from app.meeting import minutes, render
from app.meeting.rules import (ABSTAIN, NEED_INFO, OPPOSE, RESERVED, SUPPORT, point_agreed,
                               rank_speakers, relax, settle, snapshot, to_rule)
from app.models.budget import estimate_tokens


def _stance(level: int, reason: str = "理由") -> dict:
    return {"level": level, "reason": reason, "round": 1, "name": f"参会者", "history": []}


def _point(index: str, levels: dict[int, int], status: str = "open") -> dict:
    return {"id": len(index), "index": index, "status": status, "title": f"问题{index}",
            "question": "怎么做", "options": ["甲", "乙"], "conclusion": "", "rationale": "",
            "exchange_summary": "",
            "stances": {str(pid): _stance(level) for pid, level in levels.items()}}


# ---------------- 规则 ----------------
def test_unanimous_requires_every_voter_to_support() -> None:
    voters = [1, 2, 3]
    assert point_agreed(_point("D1", {1: SUPPORT, 2: RESERVED, 3: SUPPORT}), "unanimous", voters)
    assert not point_agreed(_point("D2", {1: SUPPORT, 2: ABSTAIN, 3: SUPPORT}), "unanimous", voters)
    assert not point_agreed(_point("D3", {1: SUPPORT, 2: SUPPORT}), "unanimous", voters)   # 有人没表态


def test_no_objection_lets_abstention_through() -> None:
    voters = [1, 2, 3]
    agreed = _point("D1", {1: SUPPORT, 2: ABSTAIN, 3: RESERVED})
    blocked = _point("D2", {1: SUPPORT, 2: NEED_INFO, 3: RESERVED})
    opposed = _point("D3", {1: SUPPORT, 2: OPPOSE, 3: RESERVED})
    assert point_agreed(agreed, "no_objection", voters)
    assert not point_agreed(blocked, "no_objection", voters)
    assert not point_agreed(opposed, "no_objection", voters)


def test_settle_reports_open_points_and_stance_changes() -> None:
    voters = [1, 2]
    points = [_point("D1", {1: SUPPORT, 2: SUPPORT}), _point("D2", {1: SUPPORT, 2: OPPOSE}),
              _point("D3", {1: RESERVED, 2: NEED_INFO}),
              _point("D4", {1: SUPPORT, 2: SUPPORT}, status="deferred")]
    before = {"D2": {"1": RESERVED, "2": OPPOSE}, "D3": {"1": RESERVED, "2": NEED_INFO}}

    result = settle(points, rule="unanimous", voters=voters, before=before)
    assert result == {"open": ["D2", "D3"], "agreed": ["D1"], "changed": True}

    again = settle(points, rule="unanimous", voters=voters, before=snapshot(points))
    assert again["changed"] is False


def test_to_rule_flags_points_whose_exchanges_are_used_up() -> None:
    points = [_point("D1", {1: SUPPORT, 2: OPPOSE}), _point("D2", {1: SUPPORT, 2: SUPPORT})]
    assert to_rule(points, {"D1": 2, "D2": 1}, exchanges_per_point=2) == ["D1"]


def test_relax_closes_points_nobody_opposes_and_defers_the_rest() -> None:
    voters = [1, 2]
    points = [_point("D1", {1: RESERVED, 2: ABSTAIN}), _point("D2", {1: SUPPORT, 2: OPPOSE}),
              _point("D3", {1: SUPPORT, 2: SUPPORT}, status="agreed")]
    assert relax(points, voters) == {"agreed": ["D1"], "deferred": ["D2"]}


def test_rank_speakers_puts_supporters_first() -> None:
    point = _point("D1", {1: OPPOSE, 2: SUPPORT, 3: NEED_INFO})
    assert rank_speakers(point, [1, 2, 3, 4]) == [2, 3, 1, 4]


# ---------------- 渲染 ----------------
def test_charter_segments_keep_materials_in_their_own_capped_segment() -> None:
    charter = {"title": "讨论会议章程",
               "sections": [{"title": "想法原文", "text": "做一个工具"}],
               "materials": "### 材料：片段\n" + "字" * 20_000}
    segments = render.charter_segments(charter)
    assert [(s.kind, s.name) for s in segments] == [("frozen", "章程"), ("frozen", "背景材料")]
    assert "## 讨论会议章程" in segments[0].text and "做一个工具" in segments[0].text
    assert estimate_tokens(segments[1].text) <= render.MATERIALS_LIMIT + 40
    assert render.charter_segments(charter)[1].text == segments[1].text     # 同样的输入，同样的文本


def test_charter_and_materials_caps_grow_with_the_model_budget() -> None:
    """固定 8000/6000 的上限会把真实模型装得下的方案与规格截掉：给定预算时按比例放大。"""
    body = "## 需求\n" + "字" * 20_000
    charter = {"title": "需求确认会议章程",
               "sections": [{"title": "需求规格（当前版本）", "text": body}],
               "materials": "### 材料\n" + "字" * 20_000}
    default = render.charter_segments(charter)
    roomy = render.charter_segments(charter, input_budget=200_000)
    assert "（已按上下文预算截断）" in default[0].text
    assert roomy[0].text.count("字") == 20_000                 # 大窗口下规格全文保留
    assert len(default[1].text) < len(roomy[1].text)
    assert roomy[1].text.count("字") == 20_000


def test_ledger_degrades_in_steps_when_over_budget() -> None:
    closed = _point("D1", {1: SUPPORT, 2: SUPPORT}, status="agreed")
    closed.update(conclusion="选甲", rationale="很长" * 2000, exchange_summary="很长" * 2000)
    open_point = _point("D2", {1: OPPOSE, 2: SUPPORT})
    full = render.ledger_text([closed, open_point], round_no=3, budget=10 ** 9)
    slim = render.ledger_text([closed, open_point], round_no=3, budget=600)
    assert "理由：很长" in full                       # 预算够：已了结决策点带理由
    assert "### 各决策点的交锋摘要" in full and "- D1：很长" in full
    assert "理由：很长" not in slim and "交锋摘要" not in slim   # 预算不够：先降级这两样
    assert estimate_tokens(slim) <= 600 + 40
    assert "| D1 | 已一致 | 问题D1 | 甲 / 乙 | 选甲 |" in slim
    assert "| D2 | 讨论中 | 问题D2 | 甲 / 乙 | — |" in slim
    assert "反对" in slim                                        # 进行中决策点的表态仍在


def test_turn_segment_renders_a_stance_in_words() -> None:
    turn = {"seq": 4, "kind": "restance", "speaker_name": "架构师", "content": '{"stances": [...]}',
            "decision_point": None, "structured": {"stances": [
                {"index": "D1", "level": 5, "reason": "成本太高"},
                {"index": "D2", "level": 2, "reason": ""}]}}
    segment = render.turn_segment(turn)
    assert segment.kind == "append" and segment.name == "发言4"
    assert segment.text == ("### 架构师（重新表态）\n"
                            "D1：反对——成本太高\nD2：有保留支持")


def test_table_cells_show_levels_for_every_voter() -> None:
    points = [_point("D1", {1: SUPPORT, 2: OPPOSE})]
    voters = [{"id": 1, "display_name": "架构"}, {"id": 2, "display_name": "体验"},
              {"id": 3, "display_name": "测试"}]
    assert render.table_cells(points, voters) == [
        "| 编号 | 架构 | 体验 | 测试 |", "|---|---|---|---|", "| D1 | 支持 | 反对 | — |"]


# ---------------- 纪要 ----------------
def _run() -> str:
    init_db()
    seed_defaults()
    with session_scope() as s:
        workflow_id = s.query(Workflow).filter(Workflow.template_key == "sdlc_v1").one().id
        s.add(Run(id="minutes-run", workflow_id=workflow_id, status="running", idea_text="做一个工具"))
        s.flush()
        s.add(Node(node_key="discuss", type="MEETING", name="讨论会议", run_id="minutes-run",
                   status="running"))
    return "minutes-run"


def _fixture() -> dict:
    agreed = _point("D1", {1: SUPPORT, 2: SUPPORT}, status="agreed")
    agreed.update(conclusion="选甲", rationale="成本低", exchange_summary="体验担心工期，架构给出分批方案")
    deferred = _point("D2", {1: SUPPORT, 2: OPPOSE}, status="deferred")
    deferred.update(title="是否要登录", conclusion="待用户决定")
    return {
        "meeting": {"id": 1, "protocol": "discussion", "round": 2, "charter": {"title": "讨论会议章程",
                   "sections": [{"title": "想法原文", "text": "做一个反馈收集工具"}]}},
        "participants": [{"id": 1, "display_name": "架构", "mode": "participant"},
                         {"id": 2, "display_name": "体验", "mode": "participant"}],
        "points": [agreed, deferred],
        "turns": [{"seq": 1, "kind": "position", "speaker_name": "架构", "content": "必须可验证。",
                   "structured": None, "decision_point": None}],
    }


def test_minutes_lists_info_ledger_matrix_and_pending_points() -> None:
    body = minutes.minutes_markdown(**_fixture())
    for expected in ("## 会议信息", "想法原文：做一个反馈收集工具", "## 决策点账本",
                     "| D1 | 已一致 | 问题D1 | 甲 / 乙 | 选甲 |", "## 表态矩阵",
                     "| 编号 | 架构 | 体验 |", "| D1 | 支持 | 支持 |",
                     "## 各角色立场摘要", "必须可验证。", "## 各决策点的交锋记录摘要",
                     "体验担心工期", "## 未决议题", "D2 是否要登录：待用户决定"):
        assert expected in body, expected


def test_write_minutes_writes_artifact_and_ledger_json() -> None:
    run_id = _run()
    fixture = _fixture()
    record = minutes.write_minutes(run_id, kind="discussion", created_by="facilitator",
                                   inputs=["idea"], **fixture)
    assert record["kind"] == "discussion" and record["version"] == 1
    _meta, body = parse_front_matter(read_artifact(run_id, "discussion"))
    assert "## 决策点账本" in body
    ledger = json.loads((artifacts_dir(run_id) / "discussion.ledger.json").read_text(encoding="utf-8"))
    assert [point["index"] for point in ledger] == ["D1", "D2"]


# ---------------- 断点评审的回归用例 ----------------
def test_relax_defers_points_someone_has_not_answered() -> None:
    """缺表态不是"没人反对"：全自动退让也只关闭全员表过态且无反对的点。"""
    voters = [1, 2]
    point = _point("D1", {1: SUPPORT})                  # 2 号没有表态
    assert relax([point], voters) == {"agreed": [], "deferred": ["D1"]}


def test_slim_ledger_keeps_open_exchange_summaries() -> None:
    """预算不够时降级：已了结点的理由与摘要可以丢，未决点的交锋摘要必须留。"""
    closed = _point("D1", {1: SUPPORT, 2: SUPPORT}, status="agreed")
    closed.update(conclusion="选甲", rationale="很长" * 2000, exchange_summary="很长" * 500)
    open_point = _point("D2", {1: OPPOSE, 2: SUPPORT})
    open_point.update(exchange_summary="未决点的交锋摘要")

    slim = render.ledger_text([closed, open_point], round_no=2, budget=600)

    assert "未决点的交锋摘要" in slim
    assert "很长" not in slim


def test_minutes_render_position_as_readable_text() -> None:
    fixture = _fixture()
    fixture["turns"] = [{
        "seq": 1, "kind": "position", "speaker_name": "架构",
        "content": '{"must": ["每一步可验证"], "risks": [], "route": "先做最小闭环", "questions": []}',
        "structured": {"must": ["每一步可验证"], "risks": [], "route": "先做最小闭环",
                       "questions": []},
        "decision_point": None}]

    body = minutes.minutes_markdown(**fixture)

    assert '{"must"' not in body
    assert "必须保证：每一步可验证" in body
    assert "建议路线：先做最小闭环" in body
