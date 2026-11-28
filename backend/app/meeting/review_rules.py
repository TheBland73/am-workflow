"""评审意见的规范形态与裁决规则（详设 §8.3、§8.4）。

整体结论由程序根据各条意见的分类和级别得出；评审主席只负责归类，
以及对有分歧条目的定夺。
"""
from __future__ import annotations

SEVERITIES = ("blocker", "major", "minor")
CATEGORIES = ("decision", "wording")
SEVERITY_NAMES = {"blocker": "阻塞", "major": "重要", "minor": "提示"}
CATEGORY_NAMES = {"decision": "决策级", "wording": "文字级"}
VERDICT_NAMES = {"accept": "接受", "conditional": "有条件接受", "return": "退回"}
_RANK = {"blocker": 0, "major": 1, "minor": 2}


def normalize_issue(raw: dict) -> dict:
    """一条评审意见的规范形态。

    级别或分类不合法的，按最轻、最不动决策的方式处理（提示级、文字级）；
    缺位置或缺建议的意见降为提示级（详设 §8.3）。
    """
    severity = raw.get("severity") if raw.get("severity") in SEVERITIES else "minor"
    category = raw.get("category") if raw.get("category") in CATEGORIES else "wording"
    location = str(raw.get("location") or "").strip()
    suggestion = str(raw.get("suggestion") or "").strip()
    if not location or not suggestion:
        severity = "minor"
    return {"severity": severity, "category": category, "location": location,
            "problem": str(raw.get("problem") or "").strip(), "suggestion": suggestion}


def blocking(issues: list[dict], *, category: str | None = None,
             line: str = "major") -> list[dict]:
    """达到阻塞线、并且仍然成立的意见。category 给出时只看这一类。"""
    return [issue for issue in issues
            if issue.get("standing", True)
            and (category is None or issue.get("category") == category)
            and _RANK.get(issue.get("severity"), _RANK["minor"]) <= _RANK.get(line, _RANK["major"])]


def verdict(issues: list[dict], *, blocking_line: str = "major") -> str:
    """整体结论：退回、有条件接受、接受（详设 §8.4）。"""
    if blocking(issues, category="decision", line=blocking_line):
        return "return"
    if blocking(issues, category="wording", line=blocking_line):
        return "conditional"
    return "accept"


def feedback_text(issues: list[dict], *, blocking_line: str = "major") -> str:
    """给讨论会议的回流反馈：达到阻塞线的决策级意见，连同执笔者的回应。"""
    lines = []
    for issue in blocking(issues, category="decision", line=blocking_line):
        line = (f"- {issue.get('id', '')} [{issue['location']}] "
                f"{SEVERITY_NAMES[issue['severity']]}：{issue['problem']}。建议：{issue['suggestion']}")
        if issue.get("response"):
            line += f"（执笔者回应：{issue['response']}）"
        lines.append(line)
    return "\n".join(lines) or "（没有需要复议的决策级意见）"


def issue_lines(issues: list[dict]) -> list[str]:
    """报告里的"问题清单及处置状态"（Markdown 表格的各行）。"""
    lines = ["| 编号 | 级别 | 分类 | 位置 | 问题 | 处置 |", "|---|---|---|---|---|---|"]
    for issue in issues:
        state = "已否决" if not issue.get("standing", True) else issue.get("disposition", "—")
        lines.append(f"| {issue.get('id', '')} | {SEVERITY_NAMES[issue['severity']]} | "
                     f"{CATEGORY_NAMES[issue['category']]} | {issue['location'] or '—'} | "
                     f"{_cell(issue['problem'])} | {_cell(state)} |")
    return lines


def _cell(value) -> str:
    text = str(value or "").replace("|", "/").replace("\n", " ").strip()
    return text or "—"
