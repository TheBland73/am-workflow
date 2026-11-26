"""建设方案的程序部分：决策点索引与待确认问题由程序附在模型写的正文后面。

模型不写这两节（提示词里明确要求），所以它们永远完整、与账本一致。
"""
from __future__ import annotations

from app.meeting import render
from app.meeting.rules import DEFERRED

# 模型写的正文必须有的六节：修订与初稿用同一份校验
PROPOSAL_SECTIONS = ("## 目标与成功标准", "## 范围", "## 总体方案", "## 关键流程",
                     "## 里程碑与验收", "## 风险与缓解")


def index_section(points: list[dict]) -> str:
    lines = ["## 决策点索引", "", "| 编号 | 状态 | 结论 |", "|---|---|---|"]
    lines += [f"| {point['index']} | {render.STATUS_NAMES.get(point['status'], point['status'])} | "
              f"{_cell(point.get('conclusion'))} |" for point in points]
    return "\n".join(lines) + "\n"


def pending_section(clarifications: list[dict], points: list[dict]) -> str:
    """待确认问题：待定的决策点带着各方理由；没有出处的澄清结果按假设列出。"""
    items = []
    for point in points:
        if point["status"] != DEFERRED:
            continue
        reasons = "；".join(f"{stance.get('name')}：{stance.get('reason')}"
                           for stance in (point.get("stances") or {}).values()
                           if stance.get("reason"))
        items.append(f"- [ ] {point['index']} {point['title']}：{reasons or '各方未达成一致'}")
    for item in clarifications or []:
        if item.get("source") == "assumption":
            items.append(f"- [ ] 假设：{item['question']}——{item['answer']}")
    return "\n".join(["## 待确认问题"] + (items or ["（无）"])) + "\n"


def compose(draft: str, *, points: list[dict], clarifications: list[dict]) -> str:
    """模型写的正文 + 决策点索引 + 待确认问题。"""
    return (draft.strip() + "\n\n" + index_section(points) + "\n"
            + pending_section(clarifications, points))


def _cell(value) -> str:
    text = "" if value is None else str(value)
    return (text.replace("|", "/").replace("\n", " ").strip()) or "—"
