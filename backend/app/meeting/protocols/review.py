"""评审协议（详设 §8）。

步骤依次是：
  intake    受理：带着反馈续开时，把用户的反馈整理成评审意见，直接进入回应
  prepare   准备：建参会者、定检查单、写章程
  points    先写要点：评审者只读想法、简报与账本，写下"我会重点查什么"
  review    独立评审（第 1 轮）或增量复审（第 2 轮起）
  merge     汇总分级：主席去重、定级别与分类、标注提出者与分歧
  respond   作者回应：逐条采纳或说明不采纳的理由；可以向需求方代理提问
  discuss   会议讨论：有分歧的和被反驳的条目，提出者辩护、主席定夺
  verdict   裁决：程序按分类与级别得出整体结论；退回时定点回流讨论会议
  revise    文字级修订：作者按仍然成立且被采纳的文字级意见出新版方案
  verify    验证修订：主席逐条确认改好没有；没改好的再试一轮
  report    写评审报告

引擎掌控流程：何时裁决、何时回流、哪些条目需要讨论，全由程序判定；模型只负责内容。
"""
from __future__ import annotations

from typing import Callable

from app.artifacts.store import latest_artifact, parse_front_matter, read_artifact, \
    strip_outer_fence, write_artifact
from app.conversation.builder import Segment
from app.engine.events import emit
from app.engine.runners import NodeFailure
from app.engine.templates import role_library, role_suits_meeting, role_template
from app.engine.validate import validate_artifact
from app.meeting import minutes, proposal, render, review_rules, store
from app.meeting.calls import meeting_input_budget, speak, speak_many
from app.meeting.protocols.base import (MeetingProtocol, advance, done, register_protocol,
                                        reroute_or_escalate)

Validator = Callable[[dict], list[str]]

MODE_NAMES = {"chair": "评审主席", "reviewer": "评审者", "author": "执笔者",
              "proxy": "需求方代理，只回答事实问题"}

REVIEW_BRIEF_INSTRUCTION = (
    "请为这次评审做准备：确认方案与账本齐全，列出一份检查单——评审者应当逐项核对什么。"
    "只输出一个 JSON 对象：\n"
    '{"checklist": ["检查项"]}')
REVIEW_POINTS_INSTRUCTION = (
    "先不要读方案。依据想法、简报与决策点账本，写下你这次评审会重点查什么。"
    "只输出一个 JSON 对象：\n"
    '{"focus": ["我要重点查的点"]}')
REVIEW_INSTRUCTION = (
    "请对照方案与检查单做一次独立评审。先写你看到的优点，再逐条给出问题："
    "每条写明级别、分类、位置、问题与修改建议。"
    "级别取 blocker（阻塞）、major（重要）、minor（提示）；分类取 decision（决策级，"
    "要改动已了结的决策点）或 wording（文字级，只改表述与完整性）。"
    "只输出一个 JSON 对象：\n"
    '{"strengths": ["优点"], "issues": [{"severity": "major", "category": "wording", '
    '"location": "章节或决策点编号", "problem": "哪里有问题", "suggestion": "怎么改"}]}\n'
    "没有问题时 issues 给空数组。")
REVIEW_DELTA_INSTRUCTION = (
    "这是增量复审：只核对上一轮每条决策级意见的处理结果，以及改动过的决策点与章节。"
    "另外报告你新看到的问题。只输出一个 JSON 对象：\n"
    '{"strengths": [], "issues": [], "resolved": [{"id": "I1", "handled": true, "note": "怎么处理的"}]}')
REVIEW_MERGE_INSTRUCTION = (
    "请把评审者提出的意见去重、归并，确认每条的级别与分类，标注提出者；"
    "评审者之间有分歧的标 disputed。没有位置或没有修改建议的降为提示级。"
    "只输出一个 JSON 对象：\n"
    '{"issues": [{"severity": "major", "category": "wording", "location": "……", '
    '"problem": "……", "suggestion": "……", "raised_by": ["评审者"], "disputed": false}]}')
AUTHOR_RESPONSE_INSTRUCTION = (
    "请逐条回应评审意见：采纳，或说明不采纳的理由。不采纳必须有理由。"
    "如果有需要向需求方确认的事实问题，放进 ask_requester。"
    "只输出一个 JSON 对象：\n"
    '{"responses": [{"id": "I1", "accepted": true, "reason": "", "ask_requester": []}]}\n'
    "responses 必须恰好覆盖给你的每条意见。")
ANSWER_INSTRUCTION = (
    "需求方提出了下面这些事实问题。你只依据背景材料与用户给出过的回答作答。只输出一个 JSON 对象：\n"
    '{"answers": [{"text": "回答", "source": "出处"}]}\n'
    "answers 与问题逐条对应、数量相同。材料回答不了的，text 写明“材料中没有说明”，source 给空字符串。")
REVIEW_DEFEND_INSTRUCTION = (
    "你提出了这条评审意见，执笔者不采纳或与其他评审者有分歧。请为它辩护一段话："
    "为什么它成立、不改会有什么后果。只写一段话。")
REVIEW_RULE_INSTRUCTION = (
    "请对有分歧和被执笔者反驳的意见逐条定夺：keep 为 true 表示意见成立；"
    "同时给出最终分类、级别与理由；没有被采纳的意见记入 dissent。"
    "只输出一个 JSON 对象：\n"
    '{"rulings": [{"id": "I1", "keep": true, "category": "wording", "severity": "major", '
    '"note": "定夺理由", "dissent": "保留的异议"}]}')
AUTHOR_REVISE_INSTRUCTION = (
    "请按下面的文字级意见修订方案，只改被标记的内容；开头写一节“## 修改说明”逐条说明改了什么。"
    "输出完整的新版方案（Markdown），不要写“待确认问题”和“决策点索引”两节。")
REVISION_VERIFY_INSTRUCTION = (
    "请逐条确认这些文字级意见是否已经改好：fixed 为 true 表示已改好。"
    "只输出一个 JSON 对象：\n"
    '{"verified": [{"id": "I1", "fixed": true, "note": "你怎么核对的"}]}\n'
    "verified 必须恰好覆盖给你的每条意见。")
REVIEW_INTAKE_INSTRUCTION = (
    "用户对评审结论提出了意见。请把它整理成评审意见：每条写明级别、分类、位置、问题与修改建议。"
    "只输出一个 JSON 对象：\n"
    '{"issues": [{"severity": "major", "category": "wording", "location": "……", '
    '"problem": "……", "suggestion": "……"}]}')


# ---------------- 校验模型的结构化输出 ----------------
def _has_problem_rows(data: dict, *, allow_empty: bool) -> list[str]:
    rows = data.get("issues")
    if not isinstance(rows, list):
        return ["issues 须是数组"]
    issues = [] if allow_empty or rows else ["issues 须是非空数组"]
    for row in rows:
        if not isinstance(row, dict):
            issues.append("每条意见须是对象")
        elif not isinstance(row.get("problem"), str) or not row["problem"].strip():
            issues.append("每条意见须有 problem")
    return issues


def _validate_intake(data: dict) -> list[str]:
    return _has_problem_rows(data, allow_empty=False)


def _validate_checklist(data: dict) -> list[str]:
    rows = data.get("checklist")
    if not isinstance(rows, list) or not rows or not all(isinstance(x, str) and x.strip()
                                                         for x in rows):
        return ["checklist 须是非空字符串数组"]
    return []


def _validate_focus(data: dict) -> list[str]:
    rows = data.get("focus")
    if not isinstance(rows, list) or not rows or not all(isinstance(x, str) and x.strip()
                                                         for x in rows):
        return ["focus 须是非空字符串数组"]
    return []


def _validate_review(data: dict, *, delta: bool = False) -> list[str]:
    strengths = data.get("strengths")
    issues = [] if isinstance(strengths, list) and all(isinstance(x, str) for x in strengths) \
        else ["strengths 须是字符串数组"]
    issues += _has_problem_rows(data, allow_empty=True)
    if delta:
        rows = data.get("resolved")
        if not isinstance(rows, list):
            issues.append("resolved 须是数组")
        else:
            for row in rows:
                if (not isinstance(row, dict) or not str(row.get("id") or "").strip()
                        or not isinstance(row.get("handled"), bool)):
                    issues.append("resolved 的每一项须有 id 与布尔值 handled")
    return issues


def _validate_merge(data: dict) -> list[str]:
    return _has_problem_rows(data, allow_empty=True)


def _validate_responses(ids: list[str]) -> Validator:
    def validate(data: dict) -> list[str]:
        rows = data.get("responses")
        if not isinstance(rows, list):
            return ["responses 须是数组"]
        if any(not isinstance(row, dict) for row in rows):
            return ["responses 的每一项须是对象"]
        if {str(row.get("id")) for row in rows} != set(ids):
            return [f"responses 须恰好覆盖这些意见：{ids}"]
        issues: list[str] = []
        for row in rows:
            if not isinstance(row.get("accepted"), bool):
                issues.append(f"{row['id']}：accepted 须是布尔值")
            elif row["accepted"] is False and not str(row.get("reason") or "").strip():
                issues.append(f"{row['id']}：不采纳必须写明理由")
            asks = row.get("ask_requester")
            if asks is not None and (not isinstance(asks, list)
                                     or not all(isinstance(x, str) for x in asks)):
                issues.append(f"{row['id']}：ask_requester 须是字符串数组")
        return issues

    return validate


def _validate_answers(count: int) -> Validator:
    def validate(data: dict) -> list[str]:
        rows = data.get("answers")
        if not isinstance(rows, list) or len(rows) != count:
            return [f"answers 须恰好包含 {count} 项，逐条对应问题"]
        return [f"第 {position + 1} 条回答缺少 text"
                for position, row in enumerate(rows)
                if not isinstance(row, dict)
                or not isinstance(row.get("text"), str) or not row["text"].strip()]

    return validate


def _validate_rulings(ids: list[str]) -> Validator:
    def validate(data: dict) -> list[str]:
        rows = data.get("rulings")
        if not isinstance(rows, list):
            return ["rulings 须是数组"]
        if any(not isinstance(row, dict) for row in rows):
            return ["rulings 的每一项须是对象"]
        if {str(row.get("id")) for row in rows} != set(ids):
            return [f"rulings 须恰好覆盖这些意见：{ids}"]
        issues: list[str] = []
        for row in rows:
            if not isinstance(row.get("keep"), bool):
                issues.append(f"{row['id']}：keep 须是布尔值")
            if not str(row.get("note") or "").strip():
                issues.append(f"{row['id']}：note 不能为空")
        return issues

    return validate


def _validate_verified(ids: list[str]) -> Validator:
    def validate(data: dict) -> list[str]:
        rows = data.get("verified")
        if not isinstance(rows, list):
            return ["verified 须是数组"]
        if any(not isinstance(row, dict) for row in rows):
            return ["verified 的每一项须是对象"]
        if {str(row.get("id")) for row in rows} != set(ids):
            return [f"verified 须恰好覆盖这些意见：{ids}"]
        return [f"{row['id']}：fixed 须是布尔值" for row in rows
                if not isinstance(row.get("fixed"), bool)]

    return validate


# ---------------- 会议状态与账本 ----------------
def _flow(mtg) -> int:
    """评审轮次：第一次评审、每次带着反馈续开或从等待返工恢复都加一。"""
    return mtg.meeting_round()


def _issues(mtg) -> list[dict]:
    return list(mtg.state().get("issues") or [])


def _set_issues(mtg, issues: list[dict]) -> None:
    mtg.set_state(issues=issues)


def _done(mtg, step: str) -> bool:
    return (mtg.state().get("done") or {}).get(step) == _flow(mtg)


def _mark(mtg, step: str) -> None:
    done_map = dict(mtg.state().get("done") or {})
    done_map[step] = _flow(mtg)
    mtg.set_state(done=done_map)


def _by_mode(mtg, mode: str) -> list[dict]:
    return mtg.participants(mode)


def _first(mtg, mode: str, label: str) -> dict:
    rows = _by_mode(mtg, mode)
    if not rows:
        raise NodeFailure(f"评审会议缺少{label}，无法继续", fatal=True)
    return rows[0]


def _chair(mtg) -> dict:
    return _first(mtg, "chair", "评审主席")


def _author(mtg) -> dict:
    return _first(mtg, "author", "执笔者")


def _reviewers(mtg) -> list[dict]:
    return _by_mode(mtg, "reviewer")


def _proxy(mtg) -> dict | None:
    return next(iter(_by_mode(mtg, "proxy")), None)


def _discussion(mtg) -> dict | None:
    return store.latest_meeting(mtg.run_id, "discussion")


def _ledger_points(mtg) -> list[dict]:
    meeting = _discussion(mtg)
    return store.list_decision_points(meeting["id"]) if meeting else []


def _proposal_body(mtg) -> str:
    raw = read_artifact(mtg.run_id, "proposal")
    return parse_front_matter(raw)[1].strip() if raw else ""


def _ledger_snapshot(mtg) -> dict:
    return {point["index"]: {"status": point["status"], "conclusion": point["conclusion"]}
            for point in _ledger_points(mtg)}


def _changed_points(mtg) -> list[str]:
    snapshot = dict(mtg.state().get("ledger_snapshot") or {})
    if not snapshot:
        return []
    changed = []
    for point in _ledger_points(mtg):
        before = snapshot.get(point["index"])
        if before and (before.get("status") != point["status"]
                       or before.get("conclusion") != point["conclusion"]):
            changed.append(f"- {point['index']} {point.get('title') or ''}："
                           f"{render.STATUS_NAMES.get(before.get('status'), before.get('status'))}"
                           f"/{before.get('conclusion') or '—'} → "
                           f"{render.STATUS_NAMES.get(point['status'], point['status'])}"
                           f"/{point.get('conclusion') or '—'}")
    return changed


def _round_issues(mtg) -> list[dict]:
    return [issue for issue in _issues(mtg) if int(issue.get("round") or 0) < _flow(mtg)]


def _charter_dict(mtg, *, include_proposal: bool = True) -> dict:
    state = mtg.state()
    sections: list[dict] = []
    if include_proposal:
        sections.append({"title": "被评审的建设方案", "text": _proposal_body(mtg)})
    sections.append({
        "title": "决策点账本",
        "text": render.ledger_text(_ledger_points(mtg), round_no=_flow(mtg),
                                   budget=10 ** 9).rstrip()})
    if _flow(mtg) > 1:
        lines = []
        for issue in _round_issues(mtg):
            if issue.get("category") != "decision":
                continue
            state_text = "仍然成立" if issue.get("standing", True) else "已处理"
            lines.append(f"- {issue['id']} [{issue.get('location') or '—'}] "
                         f"{review_rules.SEVERITY_NAMES[issue['severity']]}："
                         f"{issue.get('problem')}（{state_text}；"
                         f"{issue.get('disposition') or '—'}）")
        sections.append({"title": "上一轮的决策级意见与处理情况",
                         "text": "\n".join(lines) or "（无）"})
        changed = _changed_points(mtg)
        sections.append({"title": "改动的决策点", "text": "\n".join(changed) or "（无）"})
    sections.append({"title": "检查单",
                     "text": "\n".join(f"- {item}" for item in state.get("checklist") or [])
                     or "（无）"})
    sections.append({
        "title": "参与者",
        "text": "\n".join(f"- {row['display_name']}（{MODE_NAMES.get(row['mode'], row['mode'])}）"
                          for row in mtg.participants()) or "（无）"})
    materials, _truncated = mtg.materials()
    return {"title": "评审会议章程", "sections": sections, "materials": materials}


def _charter(mtg, *, include_proposal: bool = True) -> list[Segment]:
    return render.charter_segments(_charter_dict(mtg, include_proposal=include_proposal),
                                   input_budget=meeting_input_budget(mtg))


def _idea_segments(mtg) -> list[Segment]:
    """先写要点时用的想法与简报：这次调用读它们与账本，不读方案。"""
    segments = []
    idea = (mtg.ctx.idea or "").strip()
    if idea:
        segments.append(Segment("append", f"## 想法原文\n{idea}", name="想法"))
    discussion = _discussion(mtg)
    brief = str((discussion["state"] if discussion else {}).get("brief") or "").strip()
    if brief:
        segments.append(Segment("append", f"## 想法简报\n{brief}", name="简报"))
    return segments


def _finish_step(mtg, step: str, **changes) -> None:
    """把这一步的产物与完成标记一次写完：崩溃重试不会把同一批意见写两遍。"""
    done_map = dict(mtg.state().get("done") or {})
    done_map[step] = _flow(mtg)
    mtg.set_state(done=done_map, **changes)


def _add_issues(mtg, rows: list[dict], *, origin: str, round_no: int) -> list[dict]:
    """把一批原始意见规范化、编号；返回写完的整份列表，由调用方与完成标记一起落盘。

    追加前按（轮次、出处、位置、问题）签名去重：崩溃重入按已落盘的发言重跑入账时，
    同一批意见不会以新编号重复入账。
    """
    issues = _issues(mtg)
    signatures = {(issue.get("round"), issue.get("origin"), issue.get("location"),
                   issue.get("problem")) for issue in issues}
    numbers = [int(issue["id"][1:]) for issue in issues
               if str(issue.get("id", "")).startswith("I") and str(issue.get("id"))[1:].isdigit()]
    number = max(numbers, default=0) + 1
    for raw in rows:
        normalized = review_rules.normalize_issue(raw)
        signature = (round_no, origin, normalized.get("location"), normalized.get("problem"))
        if signature in signatures:
            continue
        signatures.add(signature)
        issue_id = f"I{number}"
        number += 1
        issues.append({
            **normalized, "id": issue_id,
            "raised_by": list(raw.get("raised_by") or []),
            "disputed": bool(raw.get("disputed")),
            "standing": True, "accepted": None, "response": "", "disposition": "",
            "origin": origin, "round": round_no, "dissent": ""})
    return issues


def _update_issue(mtg, issue_id: str, **fields) -> None:
    issues = _issues(mtg)
    for issue in issues:
        if issue["id"] == issue_id:
            issue.update(fields)
    _set_issues(mtg, issues)


def _targets(mtg, predicate) -> list[dict]:
    return [issue for issue in _issues(mtg) if predicate(issue)]


def _issue_states(targets: list[dict]) -> str:
    """这组意见当前的处置状态：同一步骤重跑前与重跑后状态不同，键也必须不同。"""
    return "|".join(f"{row['id']}={row.get('disposition') or ''}"
                    for row in sorted(targets, key=lambda row: str(row["id"])))


def _step_key(step: str, mtg, ids: list[str], *, extra: str = "") -> str:
    """同一步骤、同一轮、同一组意见（同一状态）共用一个键：据此找回已落盘的发言。"""
    return f"{step}:{_flow(mtg)}:{','.join(sorted(ids))}:{extra}"


async def _reuse_or_speak(mtg, key: str, *, require_data: bool = False, **kwargs) -> dict:
    """步骤的模型调用只做一次：状态里记着这一步的发言就直接复用。

    复用键在发言之前先认领（记下当时的发言行数）：崩溃落在"发言落盘、编号写上"之间时，
    重入按"认领之后才落盘"找回这一次的发言，不重复调用模型。
    """
    turns = dict(mtg.state().get("step_turns") or {})
    remembered = turns.get(key)
    if isinstance(remembered, int):
        found = store.get_turn(mtg.meeting_id, remembered)
        if found is not None and (found.get("data") is not None or not require_data):
            return found
    elif isinstance(remembered, dict):
        found = store.find_turn(mtg.meeting_id, phase=mtg.meeting()["phase"],
                                round=int(kwargs.get("turn_round") or mtg.round_no()),
                                kind=kwargs.get("turn_kind"),
                                speaker_id=kwargs["participant"]["id"],
                                after_seq=int(remembered.get("seq") or 0))
        if found is not None and (found.get("data") is not None or not require_data):
            return found
    turns = dict(mtg.state().get("step_turns") or {})
    turns[key] = {"seq": store.count_turns(mtg.meeting_id)}     # 认领：只认这次调用之后的发言
    mtg.set_state(step_turns=turns)
    result = await speak(mtg, **kwargs)
    turns = dict(mtg.state().get("step_turns") or {})
    turns[key] = result["turn_id"]
    mtg.set_state(step_turns=turns)
    return result


def _speaker_for(mtg, issue: dict) -> dict | None:
    """这条意见的提出者：按 raised_by 里的显示名找评审者，找不到就用第一名评审者。"""
    names = set(issue.get("raised_by") or [])
    for reviewer in _reviewers(mtg):
        if reviewer["display_name"] in names:
            return reviewer
    reviewers = _reviewers(mtg)
    return reviewers[0] if reviewers else None


def _patch_round(mtg, step: str) -> None:
    """这一步没有状态产物，只记完成标记。"""
    _mark(mtg, step)


async def _round_started(mtg) -> None:
    """每一轮只发一次 review_round_started：发过的轮次号记进状态，重入时不重复发。

    正常轮次在"准备"里发；用户驳回后进入的轮次从"受理"起步、跳过准备，由受理那一步发。
    """
    flow = _flow(mtg)
    if int(mtg.state().get("round_started") or 0) == flow:
        return
    mtg.set_state(round_started=flow)
    await emit(mtg.run_id, "review_round_started", mtg.node_id, {
        "meeting_id": mtg.meeting_id, "round": flow,
        "incremental": flow > 1,
        "rounds_max": int(mtg.params().get("rounds_max") or 3)})


# ---------------- 协议 ----------------
@register_protocol
class ReviewProtocol(MeetingProtocol):
    name = "review"
    steps = ("intake", "prepare", "points", "review", "merge", "respond", "discuss",
             "verdict", "revise", "verify", "report")

    async def on_enter(self, mtg) -> None:
        state = mtg.state()
        mtg.consume_feedback()
        state = mtg.state()
        text = str(state.get("reopen_feedback") or "").strip()
        if text and not state.get("intake_feedback"):
            flow = _flow(mtg) + 1
            speaker = ("评审意见" if state.get("feedback_kind") in ("reroute", "reroute_self")
                       else "用户")
            # 断点保护：反馈发言已经落盘就不再重复写（按轮次与内容去重，复议分支的记法）
            existing = {turn["content"] for turn in store.round_turns(
                mtg.meeting_id, round=flow, kinds=["user_feedback"])}
            if text not in existing:
                store.add_turn(mtg.meeting_id, round=flow, phase="intake", speaker_id=None,
                               speaker_name=speaker, kind="user_feedback", content=text)
            # 轮次、反馈的移交与滞留标记、旧报告字段、旧处置记录的清理一次写完：中途崩溃不会重复加轮
            store.update_meeting(mtg.meeting_id, round=flow, phase="intake", state_json={
                **state, "reopen_feedback": "", "intake_feedback": text,
                "rework_pending": False,
                "verdict": None, "accepted_by_user": False,
                "summary": None, "blocking": None, "gate_disposition": None,
                "unfixed": [], "discussed": [], "verify_retry_done": False,
                "review_batch": []})
            mtg.reset_builder()
            return
        if state.get("rework_pending"):
            flow = _flow(mtg) + 1
            store.update_meeting(mtg.meeting_id, round=flow, phase="prepare", state_json={
                **state, "rework_pending": False,
                "verdict": None, "accepted_by_user": False, "summary": None, "blocking": None,
                "gate_disposition": None,
                "unfixed": [], "discussed": [], "verify_retry_done": False,
                "review_batch": []})
            mtg.reset_builder()

    # ---------- 受理 ----------
    async def step_intake(self, mtg):
        if _done(mtg, "intake"):
            return advance("prepare")
        text = str(mtg.state().get("intake_feedback") or "").strip()
        if not text:
            _mark(mtg, "intake")
            return advance("prepare")
        if not mtg.participants("author"):            # 还没建过参会者：先补准备
            _mark(mtg, "intake")
            return advance("prepare")
        found = store.find_turn(mtg.meeting_id, phase="intake", round=_flow(mtg),
                                kind="review_intake", speaker_id=_chair(mtg)["id"])
        data = found["data"] if found else (await speak(
            mtg, _chair(mtg), kind="review_intake", turn_kind="review_intake",
            charter=_charter(mtg),
            segments=[Segment("append", f"## 用户的反馈\n{text}", name="反馈")],
            instruction=REVIEW_INTAKE_INSTRUCTION, structured=_validate_intake,
            turn_round=_flow(mtg)))["data"]
        _add_issues_done = _add_issues(mtg, data["issues"], origin="user", round_no=_flow(mtg))
        for issue in _add_issues_done:
            if issue["origin"] == "user" and not issue.get("raised_by"):
                issue["raised_by"] = ["用户"]
        _finish_step(mtg, "intake", issues=_add_issues_done, intake_feedback="")
        await _round_started(mtg)
        return advance("respond")

    # ---------- 准备 ----------
    async def step_prepare(self, mtg):
        if _done(mtg, "prepare"):
            return advance("points")
        if not read_artifact(mtg.run_id, "proposal"):
            raise NodeFailure("评审会议需要建设方案工件，但方案还没有产出", fatal=True)
        self._ensure_participants(mtg)
        state = mtg.state()
        if not state.get("checklist"):
            found = store.find_turn(mtg.meeting_id, phase="prepare", round=_flow(mtg),
                                    kind="review_brief", speaker_id=_chair(mtg)["id"])
            data = found["data"] if found else (await speak(
                mtg, _chair(mtg), kind="review_brief", turn_kind="review_brief",
                charter=_charter(mtg), segments=[], instruction=REVIEW_BRIEF_INSTRUCTION,
                structured=_validate_checklist, turn_round=_flow(mtg)))["data"]
            mtg.set_state(checklist=list(data["checklist"]))
        store.update_meeting(mtg.meeting_id, charter_json=_charter_dict(mtg))
        mtg.reset_builder()
        await _round_started(mtg)
        _patch_round(mtg, "prepare")
        return advance("points")

    def _ensure_participants(self, mtg) -> None:
        library = {row["role_key"]: row for row in role_library()}
        discussion = _discussion(mtg)
        base = store.list_participants(discussion["id"]) if discussion else []
        desired: list[dict] = []

        chair = next((row for row in base if row["mode"] == "chair"), None)
        if chair is not None:
            desired.append({"role_key": chair["role_key"], "display_name": chair["display_name"],
                            "mode": "chair", "system_prompt": chair["system_prompt"],
                            "model_id": chair["model_id"]})
        else:
            template = role_template("review_chair")
            desired.append({"role_key": "review_chair", "display_name": template["name"],
                            "mode": "chair", "system_prompt": template["system_prompt"],
                            "model_id": None})

        author = next((row for row in base if row["mode"] == "facilitator"), None)
        if author is not None:
            desired.append({"role_key": author["role_key"],
                            "display_name": author["display_name"], "mode": "author",
                            "system_prompt": author["system_prompt"],
                            "model_id": author["model_id"]})
        else:
            template = role_template("facilitator")
            desired.append({"role_key": "facilitator", "display_name": template["name"],
                            "mode": "author", "system_prompt": template["system_prompt"],
                            "model_id": None})

        spec = (mtg.run_config().get("rosters") or {}).get("reviewers") or {}
        configured = [str(key) for key in (spec.get("role_keys") or [])]
        if configured:
            problems = [f"已不存在：{key}" for key in configured if key not in library]
            problems += [f"不适合参加评审会议：{key}" for key in configured
                         if key in library and not role_suits_meeting(library[key], "review")]
            if problems:
                raise NodeFailure("运行级配置点名的评审角色有问题：" + "；".join(problems)
                                  + "，请修改运行级配置", fatal=True)
            reviewers = [{"role_key": key, "display_name": library[key]["name"], "mode": "reviewer",
                          "system_prompt": library[key]["system_prompt"], "model_id": None}
                         for key in configured]
        else:
            reviewers = [{"role_key": row["role_key"], "display_name": row["display_name"],
                          "mode": "reviewer", "system_prompt": row["system_prompt"],
                          "model_id": row["model_id"]}
                         for row in base if row["mode"] == "participant"]
        limit = int(mtg.params().get("reviewers_max") or 0)     # 0 表示不限制
        count = int(spec.get("count") or 0)                     # 运行级配置的固定人数
        if count:
            reviewers = reviewers[:count]
        if limit:
            reviewers = reviewers[:limit]
        if not reviewers:
            suitable = [row for row in library.values()
                        if "review" in ((row.get("meta") or {}).get("meeting_roles") or [])]
            reviewers = [{"role_key": row["role_key"], "display_name": row["name"],
                          "mode": "reviewer", "system_prompt": row["system_prompt"],
                          "model_id": None} for row in suitable[:3]]
            if count:
                reviewers = reviewers[:count]
        minimum = int(mtg.params().get("reviewers_min") or 0)
        if len(reviewers) < max(1, minimum):
            raise NodeFailure(f"评审者人数不足：需要至少 {max(1, minimum)} 名，"
                              f"当前只有 {len(reviewers)} 名", fatal=True)
        desired += reviewers[:limit or None]

        proxy = next((row for row in base if row["mode"] == "proxy"), None)
        if proxy is not None:
            desired.append({"role_key": proxy["role_key"], "display_name": proxy["display_name"],
                            "mode": "proxy", "system_prompt": proxy["system_prompt"],
                            "model_id": proxy["model_id"]})

        existing = {(row["mode"], row["role_key"], row["display_name"])
                    for row in mtg.participants()}
        for row in desired:
            key = (row["mode"], row["role_key"], row["display_name"])
            if key in existing:
                continue
            store.add_participant(
                mtg.meeting_id, role_key=row["role_key"], display_name=row["display_name"],
                mode=row["mode"], system_prompt=row["system_prompt"],
                model_id=row["model_id"], source="template")

    # ---------- 先写要点、独立评审 ----------
    async def step_points(self, mtg):
        if _done(mtg, "points"):
            return advance("review")
        if not bool(mtg.params().get("pre_brief", True)):
            _patch_round(mtg, "points")
            return advance("review")
        charter = _charter(mtg, include_proposal=False)
        context = _idea_segments(mtg)
        await speak_many(mtg, [
            {"participant": reviewer, "kind": "review_points", "turn_kind": "review_points",
             "charter": charter, "segments": context, "instruction": REVIEW_POINTS_INSTRUCTION,
             "structured": _validate_focus, "turn_round": _flow(mtg)}
            for reviewer in _reviewers(mtg)])
        _patch_round(mtg, "points")
        return advance("review")

    async def step_review(self, mtg):
        if _done(mtg, "review"):
            return advance("merge")
        incremental = _flow(mtg) > 1
        kind = "review_delta" if incremental else "review"
        structured = (lambda data: _validate_review(data, delta=True)) if incremental \
            else _validate_review
        charter = _charter(mtg)
        segments = ([Segment("append", "## 上一轮的决策级意见\n" + "\n".join(
            f"- {issue['id']} [{issue.get('location') or '—'}] {issue.get('problem')}；"
            f"处置：{issue.get('disposition') or '—'}"
            for issue in _round_issues(mtg) if issue.get("category") == "decision"), name="上一轮意见")]
            if incremental else [])
        results = await speak_many(mtg, [
            {"participant": reviewer, "kind": kind, "turn_kind": kind,
             "charter": charter, "segments": segments,
             "instruction": REVIEW_DELTA_INSTRUCTION if incremental else REVIEW_INSTRUCTION,
             "structured": structured, "turn_round": _flow(mtg)}
            for reviewer in _reviewers(mtg)])
        strengths = dict(mtg.state().get("strengths") or {})
        batch = list(mtg.state().get("review_batch") or [])
        for reviewer, result in zip(_reviewers(mtg), results):
            data = result["data"]
            if data.get("strengths"):
                strengths[reviewer["display_name"]] = (
                    list(strengths.get(reviewer["display_name"]) or [])
                    + list(data["strengths"]))
            for raw in data.get("issues") or []:
                batch.append({**raw, "raised_by": [reviewer["display_name"]],
                              "origin": "reviewer"})
            for row in data.get("resolved") or []:
                issue_id = str(row.get("id"))
                handled = bool(row.get("handled"))
                note = str(row.get("note") or "").strip()
                _update_issue(mtg, issue_id, standing=not handled,
                              disposition="已处理" + (f"：{note}" if note else "")
                              if handled else "未处理")
        _finish_step(mtg, "review", strengths=strengths, review_batch=batch)
        return advance("merge")

    # ---------- 汇总、回应、讨论 ----------
    async def step_merge(self, mtg):
        if _done(mtg, "merge"):
            return advance("respond")
        batch = list(mtg.state().get("review_batch") or [])
        if not batch:
            mtg.set_state(review_batch=[])
            _patch_round(mtg, "merge")
            return advance("respond")
        listing = "\n".join(
            f"- [{raw.get('severity')}/{raw.get('category')}] {raw.get('problem')}"
            f"（提出者：{'、'.join(raw.get('raised_by') or [])}）" for raw in batch)
        found = store.find_turn(mtg.meeting_id, phase="merge", round=_flow(mtg),
                                kind="review_merge", speaker_id=_chair(mtg)["id"])
        data = found["data"] if found else (await speak(
            mtg, _chair(mtg), kind="review_merge", turn_kind="review_merge",
            charter=_charter(mtg),
            segments=[Segment("append", f"## 待汇总的评审意见\n{listing}", name="意见清单")],
            instruction=REVIEW_MERGE_INSTRUCTION, structured=_validate_merge,
            turn_round=_flow(mtg)))["data"]
        issues = _add_issues(mtg, data["issues"], origin="reviewer", round_no=_flow(mtg))
        _finish_step(mtg, "merge", issues=issues, review_batch=[])
        return advance("respond")

    async def step_respond(self, mtg):
        if _done(mtg, "respond"):
            return advance("discuss")
        targets = _targets(mtg, lambda issue: issue.get("standing", True)
                           and issue.get("accepted") is None)
        if not targets:
            _patch_round(mtg, "respond")
            return advance("discuss")
        ids = [issue["id"] for issue in targets]
        listing = "\n".join(
            f"- {issue['id']} [{issue.get('location') or '—'}] {issue['problem']}；"
            f"建议：{issue.get('suggestion') or '—'}" for issue in targets)
        found = store.find_turn(mtg.meeting_id, phase="respond", round=_flow(mtg),
                                kind="author_response", speaker_id=_author(mtg)["id"])
        data = found["data"] if found else (await speak(
            mtg, _author(mtg), kind="author_response", turn_kind="author_response",
            charter=_charter(mtg),
            segments=[Segment("append", f"## 需要回应的评审意见\n{listing}", name="意见清单")],
            instruction=AUTHOR_RESPONSE_INSTRUCTION, structured=_validate_responses(ids),
            turn_round=_flow(mtg)))["data"]
        asked = {str(row["id"]): list(row.get("ask_requester") or []) for row in data["responses"]
                 if row.get("ask_requester")}
        proxy = _proxy(mtg)
        if asked and proxy is not None:
            questions = [question for rows in asked.values() for question in rows]
            listing = "\n".join(f"{number}. {question}"
                                 for number, question in enumerate(questions, start=1))
            found = store.find_turn(mtg.meeting_id, phase="respond", round=_flow(mtg),
                                    kind="answer", speaker_id=proxy["id"])
            answers = found["data"]["answers"] if found else (await speak(
                mtg, proxy, kind="answer", turn_kind="answer",
                segments=[Segment("append", f"## 待澄清的问题\n{listing}", name="问题")],
                instruction=ANSWER_INSTRUCTION, structured=_validate_answers(len(questions)),
                turn_round=_flow(mtg)))["data"]["answers"]
            qa_lines = []
            answer_iter = iter(answers)
            for issue_id, rows in asked.items():
                parts = []
                for _question in rows:
                    row = next(answer_iter, {})
                    parts.append(str(row.get("text") or "").strip() or "材料中没有说明")
                qa_lines.append(f"- {issue_id}：需求方代理的回答——" + "；".join(parts))
            follow_text = "\n".join(qa_lines)
            follow_ids = list(asked)
            # 再回应与第一次回应同为 author_response 调用，落盘类型分开：
            # 重入按类型各找各的，不会拿再回应顶替第一次回应，也不重复发言
            second = store.find_turn(mtg.meeting_id, phase="respond", round=_flow(mtg),
                                     kind="author_response_followup",
                                     speaker_id=_author(mtg)["id"])
            if second is None:
                second = await speak(
                    mtg, _author(mtg), kind="author_response",
                    turn_kind="author_response_followup",
                    charter=_charter(mtg),
                    segments=[Segment("append", f"## 需要回应的评审意见\n{follow_text}",
                                      name="需求方代理的回答")],
                    instruction=AUTHOR_RESPONSE_INSTRUCTION,
                    structured=_validate_responses(follow_ids), turn_round=_flow(mtg))
            by_id = {str(row["id"]): row for row in second["data"]["responses"]}
            for issue_id in follow_ids:
                row = by_id.get(issue_id) or {}
                _update_issue(mtg, issue_id, accepted=bool(row.get("accepted")),
                              response=str(row.get("reason") or "").strip()
                              or "需求方代理已回答")
            answered = set(follow_ids)
            for row in data["responses"]:
                if str(row["id"]) not in answered:
                    _update_issue(mtg, str(row["id"]), accepted=bool(row.get("accepted")),
                                  response=str(row.get("reason") or "").strip())
        else:
            for row in data["responses"]:
                _update_issue(mtg, str(row["id"]), accepted=bool(row.get("accepted")),
                              response=str(row.get("reason") or "").strip())
        _patch_round(mtg, "respond")
        return advance("discuss")

    async def step_discuss(self, mtg):
        if _done(mtg, "discuss"):
            return advance("verdict")
        if not bool(mtg.params().get("conference", True)):
            _patch_round(mtg, "discuss")
            return advance("verdict")
        discussed = set(mtg.state().get("discussed") or [])
        targets = _targets(mtg, lambda issue: issue.get("standing", True)
                           and (issue.get("disputed") or issue.get("accepted") is False)
                           and issue["id"] not in discussed)
        if not targets:
            _patch_round(mtg, "discuss")
            return advance("verdict")
        for issue in targets:
            speaker = _speaker_for(mtg, issue)
            if speaker is None:
                continue
            # 已辩护过的意见按发言行自证身份（每条辩护一个落盘类型，代理回答的记法）：
            # 重入不再重复发言，也没有"标记在发言提交之后单独写"的事务窗口。
            found = store.find_turn(mtg.meeting_id, phase=mtg.meeting()["phase"],
                                    round=_flow(mtg), kind=f"review_defend_{issue['id']}",
                                    speaker_id=speaker["id"])
            if found is not None:
                continue
            await speak(
                mtg, speaker, kind="review_defend", turn_kind=f"review_defend_{issue['id']}",
                charter=_charter(mtg),
                segments=[Segment("append",
                                  f"## 要辩护的意见\n{issue['id']}：{issue['problem']}；"
                                  f"执笔者的回应：{issue.get('response') or '未采纳'}",
                                  name="意见")],
                instruction=REVIEW_DEFEND_INSTRUCTION, turn_round=_flow(mtg))
        ids = [issue["id"] for issue in targets]
        listing = "\n".join(f"- {issue['id']}：{issue['problem']}；执笔者回应："
                            f"{issue.get('response') or '未采纳'}" for issue in targets)
        data = (await _reuse_or_speak(
            mtg, _step_key("rule", mtg, ids, extra=_issue_states(targets)),
            require_data=True,
            participant=_chair(mtg), kind="review_rule", turn_kind="review_rule",
            charter=_charter(mtg),
            segments=[Segment("append", f"## 需要定夺的意见\n{listing}", name="意见清单")],
            instruction=REVIEW_RULE_INSTRUCTION, structured=_validate_rulings(ids),
            turn_round=_flow(mtg)))["data"]
        for row in data["rulings"]:
            keep = bool(row.get("keep"))
            normalized = review_rules.normalize_issue({
                "severity": row.get("severity"), "category": row.get("category"),
                "location": next((issue.get("location") for issue in targets
                                  if issue["id"] == str(row.get("id"))), ""),
                "problem": next((issue.get("problem") for issue in targets
                                 if issue["id"] == str(row.get("id"))), ""),
                "suggestion": next((issue.get("suggestion") for issue in targets
                                    if issue["id"] == str(row.get("id"))), "")})
            _update_issue(
                mtg, str(row.get("id")), standing=keep, accepted=True if keep else False,
                severity=normalized["severity"], category=normalized["category"],
                disposition="主席定夺：成立" if keep else "主席定夺：否决",
                dissent=str(row.get("dissent") or "").strip())
        mtg.set_state(discussed=sorted(discussed | set(ids)))
        _patch_round(mtg, "discuss")
        return advance("verdict")

    # ---------- 裁决、修订、验证 ----------
    async def step_verdict(self, mtg):
        if _done(mtg, "verdict"):
            return advance("revise")
        line = str(mtg.params().get("min_blocking_severity") or "major")
        issues = _issues(mtg)
        verdict = review_rules.verdict(issues, blocking_line=line)
        blocking = review_rules.blocking(issues, line=line)
        already = int(mtg.state().get("verdict_round") or 0) == _flow(mtg)
        mtg.set_state(verdict=verdict, verdict_round=_flow(mtg))
        if not already:                               # 升级确认点恢复时不再重复发事件
            await emit(mtg.run_id, "review_verdict", mtg.node_id, {
                "meeting_id": mtg.meeting_id, "round": _flow(mtg), "verdict": verdict,
                "blocking": len(blocking), "issues": len(issues)})
        if verdict != "return":
            _patch_round(mtg, "verdict")
            return advance("revise")
        feedback = ("评审会议要求复议以下决策级问题：\n"
                    + review_rules.feedback_text(issues, blocking_line=line))
        result = await reroute_or_escalate(
            mtg, ["discuss"], feedback,
            title="评审意见仍为阻塞", description=feedback[:3000],
            summary="评审结论：退回讨论会议复议")
        if result is None:                            # 人在确认点上接受现状
            mtg.set_state(accepted_by_user=True)
            _patch_round(mtg, "verdict")
            return advance("revise")
        if result.action == "suspend":                # 回流轮次用尽：等人决定，决定前不推进
            return result
        # 回流标记（rework_pending/awaiting_rework）由编排器在接受回流之后写进会议状态：
        # 这里提前写的话，进程在编排器接受之前中断，重入会加一轮去等一场没有发起的返工。
        # 裁决这步也不记完成标记：重入时从本步重跑，程序重新裁决并再次发出回流指令。
        mtg.set_state(ledger_snapshot=_ledger_snapshot(mtg))
        return result

    async def step_revise(self, mtg):
        if _done(mtg, "revise"):
            return advance("verify")
        verdict = str(mtg.state().get("verdict") or "")
        if verdict == "return" and not mtg.state().get("accepted_by_user"):
            _patch_round(mtg, "revise")               # 退回时由讨论会议复议，评审这里不修订
            return advance("verify")
        targets = _targets(mtg, lambda issue: issue.get("standing", True)
                           and issue.get("accepted") is True
                           and issue.get("category") == "wording"
                           and issue.get("disposition") not in ("已修订并验证", "已修订，待验证"))
        if not targets:
            _patch_round(mtg, "revise")
            return advance("verify")
        await self._revise(mtg, targets)
        _patch_round(mtg, "revise")
        return advance("verify")

    async def _revise(self, mtg, targets: list[dict]) -> None:
        ids = [issue["id"] for issue in targets]
        listing = "\n".join(f"- {issue['id']} [{issue.get('location') or '—'}] "
                            f"{issue['problem']}；建议：{issue.get('suggestion') or '—'}"
                            for issue in targets)
        result = await _reuse_or_speak(
            mtg, _step_key("revise", mtg, ids, extra=_issue_states(targets)),
            participant=_author(mtg), kind="author_revise", turn_kind="author_revise",
            charter=_charter(mtg),
            segments=[Segment("append", f"## 文字级修改意见\n{listing}", name="修改意见")],
            instruction=AUTHOR_REVISE_INSTRUCTION, turn_round=_flow(mtg))
        draft = strip_outer_fence(result["content"]).strip()
        issues = validate_artifact("proposal", draft)
        missing = [title for title in proposal.PROPOSAL_SECTIONS if title not in draft]
        if missing:
            issues.append("方案缺少这些章节：" + "、".join(missing))
        if "## 修改说明" not in draft:
            issues.append("修订版缺少「## 修改说明」一节")
        if issues:
            raise NodeFailure("修订后的方案校验未通过：" + "；".join(issues))
        discussion = _discussion(mtg)
        clarifications = (discussion["state"].get("clarifications") or []) if discussion else []
        body = proposal.compose(draft, points=_ledger_points(mtg),
                                clarifications=clarifications)
        current = read_artifact(mtg.run_id, "proposal")
        existing = parse_front_matter(current)[1].strip() if current else ""
        if existing != body.strip():              # 重试用同一份稿：不再写出重复的一版
            write_artifact(mtg.run_id, "proposal", body, created_by="author",
                           inputs=["discussion"], status="draft")
        for issue_id in ids:
            _update_issue(mtg, issue_id, disposition="已修订，待验证", revised_round=_flow(mtg))
        store.update_meeting(mtg.meeting_id, charter_json=_charter_dict(mtg))
        mtg.reset_builder()

    async def step_verify(self, mtg):
        if _done(mtg, "verify"):
            return advance("report")
        pending = _targets(mtg, lambda issue: issue.get("disposition") == "已修订，待验证")
        if not pending:
            pending = _targets(mtg, lambda issue: issue.get("disposition") == "未改好")
            if not pending or mtg.state().get("verify_retry_done"):
                _patch_round(mtg, "verify")
                return advance("report")
        await self._verify(mtg, pending)
        remaining = _targets(mtg, lambda issue: issue.get("disposition") == "未改好")
        if remaining and not mtg.state().get("verify_retry_done"):
            await self._revise(mtg, remaining)        # 只对没改好的再修订一次
            await self._verify(mtg, remaining)
            remaining = _targets(mtg, lambda issue: issue.get("disposition") == "未改好")
            mtg.set_state(verify_retry_done=True)
        if remaining:
            mtg.set_state(unfixed=[issue["id"] for issue in remaining])
        _patch_round(mtg, "verify")
        return advance("report")

    async def _verify(self, mtg, targets: list[dict]) -> None:
        ids = [issue["id"] for issue in targets]
        listing = "\n".join(f"- {issue['id']} [{issue.get('location') or '—'}] "
                            f"{issue['problem']}" for issue in targets)
        version = int((latest_artifact(mtg.run_id, "proposal") or {}).get("version") or 0)
        data = (await _reuse_or_speak(
            mtg, _step_key("verify", mtg, ids, extra=f"v{version}"), require_data=True,
            participant=_chair(mtg), kind="revision_verify", turn_kind="revision_verify",
            charter=_charter(mtg),
            segments=[Segment("append", f"## 本轮的修订意见\n{listing}", name="修订意见")],
            instruction=REVISION_VERIFY_INSTRUCTION, structured=_validate_verified(ids),
            turn_round=_flow(mtg)))["data"]
        for row in data["verified"]:
            _update_issue(mtg, str(row["id"]),
                          disposition="已修订并验证" if row.get("fixed") else "未改好")

    # ---------- 报告 ----------
    async def step_report(self, mtg):
        issues = _issues(mtg)
        verdict = str(mtg.state().get("verdict") or "")
        accepted_by_user = bool(mtg.state().get("accepted_by_user"))
        unfixed = list(mtg.state().get("unfixed") or [])
        precheck_ok, precheck_reason = self._precheck(verdict, accepted_by_user, unfixed)
        mtg.set_state(precheck_ok=precheck_ok, precheck_reason=precheck_reason)
        report = self._write_report(mtg, issues, verdict, accepted_by_user, unfixed)
        blocking = review_rules.blocking(
            issues, line=str(mtg.params().get("min_blocking_severity") or "major"))
        summary = (f"评审会议完成：结论 {review_rules.VERDICT_NAMES.get(verdict, verdict)}，"
                   f"{len(issues)} 条意见，{len(blocking)} 条达到阻塞线")
        # 纪要接口按会议记下的版本取；摘要与阻塞数留给 /runs/{id}/review 与指标按报告取
        mtg.set_state(minutes_version=report["version"], summary=summary, blocking=len(blocking))
        return done(summary, verdict=verdict, precheck_ok=precheck_ok,
                    precheck_reason=precheck_reason, accepted_by_user=accepted_by_user,
                    report=report, issues=len(issues), blocking=len(blocking))

    def _precheck(self, verdict: str, accepted_by_user: bool,
                  unfixed: list[str]) -> tuple[bool, str]:
        if unfixed:
            return False, "有意见没有改好：" + "、".join(unfixed)
        if verdict == "return" and not accepted_by_user:
            return False, "评审结论为退回，讨论会议需要先复议"
        return True, ""

    def _write_report(self, mtg, issues: list[dict], verdict: str,
                      accepted_by_user: bool, unfixed: list[str]) -> dict:
        latest = latest_artifact(mtg.run_id, "proposal") or {}
        strengths = []
        for rows in (mtg.state().get("strengths") or {}).values():
            strengths += [f"- {item}" for item in rows]
        conclusion = [f"结论：{review_rules.VERDICT_NAMES.get(verdict, verdict)}。"]
        if accepted_by_user:
            conclusion.append("人在回流轮次用尽的确认点上接受了现状。")
        if unfixed:
            conclusion.append("有意见没有改好：" + "、".join(unfixed) + "。")
        dissent = [f"- {issue['id']}：{issue['dissent']}" for issue in issues
                   if issue.get("dissent")]
        sections = [
            ("评了什么", f"- 建设方案 v{latest.get('version', '?')}；评审 {_flow(mtg)} 轮"),
            ("谁评的", "\n".join(f"- {row['display_name']}（{MODE_NAMES.get(row['mode'], row['mode'])}）"
                                for row in mtg.participants()) or "（无）"),
            ("优点", "\n".join(strengths) or "（无）"),
            ("结论", "\n".join(conclusion)),
            ("问题清单及处置状态", "\n".join(review_rules.issue_lines(issues))),
            ("保留的异议", "\n".join(dissent) or "（无）"),
        ]
        return minutes.write_minutes(
            mtg.run_id, kind="review", created_by="chair", inputs=["proposal"],
            meeting=mtg.meeting(), participants=mtg.participants(),
            points=_ledger_points(mtg), turns=store.list_turns(mtg.meeting_id, limit=100_000),
            extra_sections=sections, meta={"review_type": "proposal"})
