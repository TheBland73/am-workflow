"""Mock 供应商：会议调用按调用标识路由；文档节点仍按系统提示关键词路由。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mock_provider import _ledger_rows, _pick  # noqa: E402

LEDGER = """## 决策点账本（第 1 轮开始时的状态）

| 编号 | 状态 | 问题 | 候选项 | 结论 |
|---|---|---|---|---|
| D1 | 已一致 | 首版的范围与入口 | 网页版 / 桌面版 | 网页版 |
| D2 | 讨论中 | 首版是否需要登录 | 需要 / 不需要 | — |
"""
UX = "## 你的角色\n你是讨论与评审中的用户体验视角。你关心：目标用户是谁。"


def _reply(kind: str, body: str = "", system: str = "") -> str:
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": f"{body}\n\n【调用类型：{kind}】"}]
    text, call = _pick(messages, None)
    assert call is None
    return text


def test_stances_follow_the_ledger_and_yield_after_the_exchange() -> None:
    first = json.loads(_reply("stance", LEDGER + "\n" + UX))
    assert [item["index"] for item in first["stances"]] == ["D2"]      # 只对未了结的决策点表态
    assert first["stances"][0]["level"] == 5
    assert first["stances"][0]["choice"] == "不需要"
    again = json.loads(_reply("restance", LEDGER + "\n" + UX))
    assert again["stances"][0]["level"] == 1                           # 交锋之后让步
    summary = json.loads(_reply("exchange_summary", LEDGER))
    assert summary["summaries"][0]["index"] == "D2"


def test_roster_follows_the_headcount_and_picks_offered_roles() -> None:
    listing = ("## 角色库（可以从中挑选）\n- architect｜架构师｜技术可行性\n"
               "- ux｜用户体验｜任务路径\n")
    roster = json.loads(_reply("roster", listing + "\n请正好挑选 2 名。"))
    assert [row["role_key"] for row in roster["participants"]] == ["architect", "ux"]


def test_answers_match_the_number_of_questions() -> None:
    listing = "## 待澄清的问题\n1. 首版支持哪些平台？\n2. 是否需要登录？\n"
    answer = json.loads(_reply("answer", listing))
    assert len(answer["answers"]) == 2                                  # 不受输出纪律里的编号条目干扰


def test_proposal_is_written_from_the_ledger() -> None:
    text = _reply("proposal", LEDGER)
    assert "## 目标与成功标准" in text and "D1 首版的范围与入口：网页版" in text
    assert "待确认问题" not in text                                     # 这一节由程序生成


def test_untagged_document_nodes_still_route_by_keywords() -> None:
    messages = [{"role": "system", "content": "你是需求分析师。条目使用 EARS 句式。"},
                {"role": "user", "content": "方案与纪要\n" + LEDGER}]
    text, call = _pick(messages, None)
    assert call is None and "【调用类型：" not in text
    assert "## 需求追踪矩阵" in text and "| R1 | D1 |" in text
    assert "## 非需求类决策" in text


def test_ledger_rows_keep_the_latest_status_of_each_point() -> None:
    later = "\n（之后的纪要）\n| D2 | 已一致 | 首版是否需要登录 | 需要 / 不需要 | 需要 |\n"
    rows = _ledger_rows(LEDGER + later)
    assert [row["index"] for row in rows] == ["D1", "D2"]
    assert rows[1]["status"] == "已一致" and rows[1]["options"] == ["需要", "不需要"]


def test_the_last_call_tag_wins_over_an_echoed_one() -> None:
    """提示里如果复述过别的调用标识，仍以末尾的真实标识为准。"""
    echoed = "（上一轮发言复述过【调用类型：proposal】）"
    messages = [{"role": "system", "content": ""},
                {"role": "user", "content": f"{echoed}\n\n{LEDGER}\n\n【调用类型：stance】"}]
    text, call = _pick(messages, None)
    assert call is None and "stances" in json.loads(text)


def test_review_calls_have_format_correct_replies() -> None:
    from mock_provider import _meeting_reply

    structured = ("review_brief", "review_points", "review", "review_delta", "review_merge",
                  "author_response", "review_rule", "revision_verify", "review_intake")
    for kind in structured:
        json.loads(_meeting_reply(kind, LEDGER + "\nI1"))
    assert _meeting_reply("review_defend", LEDGER)
    assert "## 修改说明" in _meeting_reply("author_revise", LEDGER)
