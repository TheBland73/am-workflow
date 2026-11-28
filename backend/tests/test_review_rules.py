"""评审意见的规范形态与裁决规则：整体结论由程序得出。"""
from __future__ import annotations

from app.meeting.proposal import compose
from app.meeting.review_rules import feedback_text, normalize_issue, verdict

DECISION = {"severity": "blocker", "category": "decision", "location": "D2",
            "problem": "结论与账本不符", "suggestion": "按账本的结论改写"}
WORDING = {"severity": "major", "category": "wording", "location": "4.2",
           "problem": "术语不统一", "suggestion": "统一为“反馈”"}


def test_issues_without_location_or_suggestion_are_downgraded() -> None:
    assert normalize_issue(DECISION) == DECISION
    vague = normalize_issue({**WORDING, "location": "", "suggestion": ""})
    assert vague["severity"] == "minor" and vague["category"] == "wording"
    unknown = normalize_issue({**WORDING, "severity": "critical", "category": "style"})
    assert unknown["severity"] == "minor" and unknown["category"] == "wording"


def test_verdict_follows_categories_and_severities() -> None:
    assert verdict([]) == "accept"
    assert verdict([normalize_issue(WORDING)]) == "conditional"
    assert verdict([normalize_issue(DECISION)]) == "return"
    assert verdict([normalize_issue({**WORDING, "severity": "minor"})]) == "accept"
    assert verdict([normalize_issue(WORDING)], blocking_line="blocker") == "accept"


def test_overruled_issues_do_not_count() -> None:
    overruled = {**normalize_issue(DECISION), "standing": False}
    assert verdict([overruled]) == "accept"


def test_feedback_for_the_discussion_lists_blocking_decision_issues() -> None:
    issues = [{**normalize_issue(DECISION), "id": "I1", "response": "同意复议"},
              {**normalize_issue(WORDING), "id": "I2"}]
    text = feedback_text(issues)
    assert "I1" in text and "[D2]" in text and "按账本的结论改写" in text
    assert "执笔者回应：同意复议" in text and "I2" not in text


def test_compose_appends_index_and_pending_questions() -> None:
    points = [{"index": "D1", "status": "agreed", "title": "范围", "conclusion": "网页版"},
              {"index": "D2", "status": "deferred", "title": "登录", "conclusion": "",
               "stances": {"1": {"name": "架构", "reason": "成本太高", "level": 5}}}]
    clarifications = [{"question": "支持哪些平台", "answer": "未知", "source": "assumption"}]

    body = compose("# 建设方案\n\n正文。", points=points, clarifications=clarifications)

    # 详设 §7.8 的节顺序：正文、待确认问题、决策点索引
    assert body.startswith("# 建设方案\n\n正文。\n\n## 待确认问题")
    assert "| D1 | 已一致 | 网页版 |" in body
    assert "- [ ] D2 登录：架构：成本太高" in body
    assert "- [ ] 假设：支持哪些平台——未知" in body
    assert body.index("## 待确认问题") < body.index("## 决策点索引")
