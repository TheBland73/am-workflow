"""会议纪要：由程序从账本和发言生成，不经过模型（详设 §7.8）。

写进工件（discussion 或 review）的同时，在同目录另存一份账本的 JSON，
供之后的节点和外部工具读取。
"""
from __future__ import annotations

import json

from app.artifacts.store import artifacts_dir, write_artifact
from app.meeting import render
from app.meeting.rules import DEFERRED

PROTOCOL_NAMES = {"discussion": "讨论会议", "review": "评审会议", "confirm": "需求确认会议"}
LEDGER_FIELDS = ("id", "index", "title", "question", "options", "status", "conclusion",
                 "rationale", "exchange_summary", "stances", "origin_round", "closed_round")


def _one_line(text: str, limit: int) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[:limit] + "…"


def minutes_markdown(*, meeting: dict, participants: list[dict], points: list[dict],
                     turns: list[dict], extra_sections: list[tuple[str, str]] = ()) -> str:
    """会议纪要正文：会议信息、账本、表态矩阵、立场摘要、交锋摘要、未决议题，再加协议自己要附的几节。"""
    charter = dict(meeting.get("charter") or {})
    protocol = meeting.get("protocol") or ""
    voters = [p for p in participants if p.get("mode") == "participant"]
    lines = [f"# 会议纪要（{PROTOCOL_NAMES.get(protocol, protocol)}）", "", "## 会议信息"]
    for section in charter.get("sections") or []:
        lines.append(f"- {section['title']}：{_one_line(section.get('text'), 300)}")
    lines.append("- 参会者：" + ("、".join(p["display_name"] for p in participants) or "（无）"))
    lines.append(f"- 轮次：{meeting.get('round') or 1}")
    lines += ["", "## 决策点账本", render.ledger_text(points, round_no=meeting.get("round") or 1,
                                                       budget=10 ** 9).rstrip()]
    if voters:
        lines += ["", "## 表态矩阵"] + render.table_cells(points, voters)
    positions = [t for t in turns if t.get("kind") == "position" and t.get("content")]
    if positions:
        lines += ["", "## 各角色立场摘要"]
        for turn in positions:
            lines += [f"### {turn['speaker_name']}", "", str(turn["content"]).strip(), ""]
    exchanged = [p for p in points if p.get("exchange_summary")]
    if exchanged:
        lines += ["", "## 各决策点的交锋记录摘要"]
        lines += [f"- {p['index']} {p.get('title') or ''}：{p['exchange_summary']}" for p in exchanged]
    pending = [p for p in points if p["status"] == DEFERRED]
    if pending:
        lines += ["", "## 未决议题（需要用户决定）"]
        lines += [f"- {p['index']} {p.get('title') or ''}：{p.get('conclusion') or '各方理由见账本'}"
                  for p in pending]
    for title, text in extra_sections:
        lines += ["", f"## {title}", str(text).rstrip()]
    return "\n".join(lines).strip() + "\n"


def write_minutes(run_id: str, *, kind: str, created_by: str, inputs: list[str],
                  meeting: dict, participants: list[dict], points: list[dict],
                  turns: list[dict], extra_sections: list[tuple[str, str]] = ()) -> dict:
    """写会议纪要工件（新版本），并另存账本的 JSON。返回工件的记录。"""
    body = minutes_markdown(meeting=meeting, participants=participants, points=points,
                            turns=turns, extra_sections=extra_sections)
    record = write_artifact(run_id, kind, body, created_by=created_by, inputs=inputs,
                            status="reviewed")
    ledger = [{field: point.get(field) for field in LEDGER_FIELDS} for point in points]
    path = artifacts_dir(run_id) / f"{kind}.ledger.json"
    path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    return record
