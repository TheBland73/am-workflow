"""把会议状态渲染成文本：发给模型的段，以及给人看的表格。

两件事都只由程序做，模型不负责复述账本。发给模型的部分注意两点（详设 §10）：
要截断的内容在构造段的时候用 capped() 一次截好，同样的输入永远得到同样的文本；
措辞固定，不随调用变化。这样冻结段在两次调用之间逐字节不变，前缀缓存才命中。
"""
from __future__ import annotations

import json

from app.conversation.builder import Segment, capped
from app.meeting.rules import CLOSED_STATUSES, LEVEL_NAMES, OPEN_STATUSES
from app.models.budget import estimate_tokens

MATERIALS_LIMIT = 6000       # 背景材料段的 token 上限
CHARTER_LIMIT = 8000         # 章程正文段的 token 上限
LEDGER_LIMIT = 4000          # 账本快照段的 token 上限

STATUS_NAMES = {"open": "讨论中", "reopened": "复议中", "agreed": "已一致",
                "ruled": "主持人裁决", "deferred": "待定"}
TURN_LABELS = {"position": "立场", "stance": "表态", "restance": "重新表态", "claim": "主张",
               "challenge": "质疑", "respond": "回应", "exchange_summary": "交锋摘要",
               "question": "提问", "answer": "回答", "facilitator_note": "主持人",
               "user_intervention": "用户插话", "user_feedback": "用户反馈",
               "user_guidance": "用户指导", "ruling": "裁决",
               "review_intake": "受理意见", "review_brief": "评审准备",
               "review_points": "评审要点", "review": "独立评审", "review_delta": "增量复审",
               "review_merge": "汇总分级", "author_response": "作者回应",
               "review_defend": "意见辩护", "review_rule": "主席定夺",
               "author_revise": "方案修订", "revision_verify": "修订验证"}


def _cell(value) -> str:
    """表格单元：竖线会破坏 Markdown 表格，换成斜线；换行换成空格。"""
    text = "" if value is None else str(value)
    return (text.replace("|", "/").replace("\n", " ").strip()) or "—"


def charter_segments(charter: dict) -> list[Segment]:
    """会议章程：正文一段，背景材料另有上限的一段。都是冻结段，名字固定。"""
    parts = [f"## {charter.get('title') or '会议章程'}"]
    for section in charter.get("sections") or []:
        parts.append(f"### {section['title']}\n{section.get('text') or ''}".rstrip())
    segments = [Segment("frozen", capped("\n\n".join(parts) + "\n", CHARTER_LIMIT),
                        name="章程", limit=CHARTER_LIMIT)]
    materials = str(charter.get("materials") or "")
    if materials:
        segments.append(Segment("frozen", capped(materials, MATERIALS_LIMIT),
                                name="背景材料", limit=MATERIALS_LIMIT))
    return segments


def ledger_text(points: list[dict], *, round_no: int, budget: int = LEDGER_LIMIT) -> str:
    """账本快照（本轮开始时的状态）。超预算时分两级降级：
    先去已了结决策点的理由与交锋摘要，再把进行中决策点的表态压成只剩等级。
    进行中决策点的交锋摘要保留——它正是下一轮表态最需要的内容。"""
    full = _ledger(points, round_no, reasons=True)
    if estimate_tokens(full) <= budget:
        return full
    slimmer = _ledger(points, round_no, reasons=False)
    if estimate_tokens(slimmer) <= budget:
        return slimmer
    return capped(_ledger(points, round_no, reasons=False, levels_only=True), budget)


def ledger_segment(points: list[dict], *, round_no: int) -> Segment:
    return Segment("frozen", ledger_text(points, round_no=round_no), name="账本",
                   limit=LEDGER_LIMIT)


def _ledger(points: list[dict], round_no: int, *, reasons: bool,
            levels_only: bool = False) -> str:
    lines = [f"## 决策点账本（第 {round_no} 轮开始时的状态）"]
    if not points:
        return "\n".join(lines + ["", "（还没有决策点）"])
    lines += ["", "| 编号 | 状态 | 问题 | 候选项 | 结论 |", "|---|---|---|---|---|"]
    for point in points:
        options = " / ".join(str(option) for option in (point.get("options") or []))
        status = STATUS_NAMES.get(point["status"], point["status"])
        lines.append(f"| {point['index']} | {status} | {_cell(point.get('title'))} | "
                     f"{_cell(options)} | {_cell(point.get('conclusion'))} |")
    working = [point for point in points if point["status"] in OPEN_STATUSES]
    if working:
        lines += ["", "### 讨论中的决策点：各方最新的表态"]
        for point in working:
            lines += ["", f"**{_cell(str(point['index']) + ' ' + (point.get('title') or ''))}**"]
            stances = sorted((point.get("stances") or {}).items(), key=lambda item: int(item[0]))
            for pid, stance in stances:
                level = LEVEL_NAMES.get((stance or {}).get("level"), "未表态")
                reason = (stance or {}).get("reason")
                if levels_only or not reason:
                    lines.append(f"- {(stance or {}).get('name') or f'参会者{pid}'}：{level}")
                else:
                    lines.append(f"- {(stance or {}).get('name') or f'参会者{pid}'}：{level}——{reason}")
    exchanged = [point for point in points if point.get("exchange_summary")
                 and (reasons or point["status"] in OPEN_STATUSES)]
    if exchanged:
        lines += ["", "### 各决策点的交锋摘要"]
        lines += [f"- {point['index']}：{point['exchange_summary']}" for point in exchanged]
    closed = [point for point in points if point["status"] in CLOSED_STATUSES]
    if closed:
        lines += ["", "### 已了结的决策点：结论与理由"]
        for point in closed:
            line = f"- {point['index']} {point.get('title') or ''}：{point.get('conclusion') or '—'}"
            if reasons and point.get("rationale"):
                line += f"（理由：{point['rationale']}）"
            lines.append(line)
    return "\n".join(lines) + "\n"


def turn_text(turn: dict) -> str:
    """一条发言的正文。结构化的发言渲染成几行文字，不直接给 JSON。"""
    kind = turn.get("kind") or ""
    data = turn.get("structured") or {}
    if kind in ("stance", "restance") and data.get("stances"):
        lines = []
        for item in data["stances"]:
            reason = f"——{item['reason']}" if item.get("reason") else ""
            lines.append(f"{item.get('index')}：{LEVEL_NAMES.get(item.get('level'), '未表态')}{reason}")
        return "\n".join(lines)
    if kind == "exchange_summary" and data.get("summaries"):
        return "\n".join(f"{item.get('index')}：{item.get('summary')}" for item in data["summaries"])
    if kind == "position":
        lines = []
        for label, key in (("必须保证", "must"), ("主要风险", "risks"),
                           ("疑问", "questions")):
            lines += [f"- {label}：{item}" for item in (data.get(key) or []) if str(item).strip()]
        if str(data.get("route") or "").strip():
            lines.append(f"- 建议路线：{data['route']}")
        if lines:
            return "\n".join(lines)
    if kind in ("review", "review_delta"):
        lines = [f"- 优点：{item}" for item in (data.get("strengths") or [])
                 if str(item).strip()]
        for item in data.get("issues") or []:
            location = f"[{item['location']}] " if item.get("location") else ""
            suggestion = f"（建议：{item['suggestion']}）" if item.get("suggestion") else ""
            lines.append(f"- 问题：{location}{item.get('problem') or ''}{suggestion}")
        for item in data.get("resolved") or []:
            lines.append(f"- 上一轮 {item.get('id')}："
                         f"{'已处理' if item.get('handled') else '未处理'}"
                         + (f"（{item['note']}）" if item.get("note") else ""))
        if lines:
            return "\n".join(lines)
    if data:                                          # 其余结构化输出：缩进的 JSON
        return json.dumps(data, ensure_ascii=False, indent=2)
    return str(turn.get("content") or "")


def turn_segment(turn: dict) -> Segment:
    """本轮的一条逐字发言，作为追加段。"""
    label = TURN_LABELS.get(turn.get("kind") or "", turn.get("kind") or "发言")
    about = f"，关于 {turn['decision_point']}" if turn.get("decision_point") else ""
    text = f"### {turn.get('speaker_name') or '？'}（{label}{about}）\n{turn_text(turn)}".rstrip()
    return Segment("append", text, name=f"发言{turn['seq']}")


def table_cells(points: list[dict], participants: list[dict]) -> list[str]:
    """表态矩阵（Markdown 表格的各行）：每个有表决权的角色一列。"""
    names = [participant["display_name"] for participant in participants]
    lines = ["| 编号 | " + " | ".join(names) + " |", "|" + "---|" * (len(names) + 1)]
    for point in points:
        stances = point.get("stances") or {}
        cells = []
        for participant in participants:
            stance = stances.get(str(participant["id"])) or {}
            cells.append(LEVEL_NAMES.get(stance.get("level"), "—") if stance else "—")
        lines.append(f"| {point['index']} | " + " | ".join(cells) + " |")
    return lines
