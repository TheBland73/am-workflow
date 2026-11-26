"""讨论协议（详设 §7）。

步骤依次是：
  prepare     会前准备：主持人把想法与背景材料整理成简报，列出需要澄清的问题
  clarify     澄清：用户或需求方代理逐条回答
  roster      名单：用户指定的照用；否则主持人提议，用户确认或自动生效
  positions   独立立场：全体并行，互不可见
  agenda      议题提炼：主持人把立场合并成决策点清单，由程序落盘
  stances     全员表态：读全部立场和完整清单，对每个决策点表态
  exchange    交锋：逐个未了结的决策点"主张 → 质疑 → 回应"
  summary     交锋摘要：主持人为每个未了结的决策点写一段摘要
  restance    全员重新表态：改立场必须写理由；程序结算、判停滞、决定下一轮去哪
  consensus   轮次共识：处理未决议题（全部了结且由程序判定时直接往下走）
  reopen      复议：带着反馈续开时，把反馈转成要重开的决策点
  draft       整合成稿：主持人按账本写建设方案，程序附上决策点索引与待确认问题
  check       全员复核：读初稿，逐个确认已了结的决策点被如实反映

引擎掌控流程：谁在何时发言、何时算了结、何时停，全由程序按规则判定；模型只负责内容。
"""
from __future__ import annotations

from collections import Counter
from typing import Callable

from app.artifacts.store import (latest_artifact, parse_front_matter, read_artifact,
                                 strip_outer_fence, write_artifact)
from app.conversation.builder import Segment
from app.engine.events import emit
from app.engine.runners import NodeFailure
from app.engine.templates import role_library, role_template
from app.engine.validate import validate_artifact
from app.meeting import minutes, proposal, render, store
from app.meeting.calls import speak, speak_many
from app.meeting.checkpoints import record_auto_decision
from app.meeting.protocols.base import MeetingProtocol, advance, done, register_protocol, suspend
from app.meeting.rules import (DEFERRED, LEVEL_NAMES, OPEN_STATUSES, relax, rank_speakers, settle,
                               snapshot, to_rule)

Validator = Callable[[dict], list[str]]

CLOSURE_TEXT = {
    "unanimous": "全体一致：一个决策点要等所有参会者都表态“支持”或“有保留支持”才算了结。",
    "no_objection": "无反对即可：没有人反对、也没有人需要更多信息就算了结；弃权不阻塞。",
    "facilitator_rules": ("主持人裁决：交锋达到规定次数后仍未一致的决策点，由主持人裁决并写明理由，"
                          "反对意见记入异议。"),
}
CONSENSUS_OPTIONS = [
    {"key": "integrate", "label": "带着分歧整合", "action": "approve",
     "description": "未了结的决策点记为“待确认问题”，写进方案，由你在方案确认时决定。"},
    {"key": "continue", "label": "给指导语继续讨论", "action": "reject",
     "description": "驳回并写下你的意见，会议带着它再讨论一轮。"},
    {"key": "delegate", "label": "授权主持人裁决", "action": "approve",
     "description": "主持人为未了结的决策点做裁决并写明理由，反对意见记入异议。"},
]
REASON_TEXT = {
    "all_closed": "按闭合规则，本轮的决策点已经全部了结。",
    "round_end": "这一轮结束了，还有决策点没有达成一致。",
    "stall": "连续几轮没有人改变立场，讨论陷入停滞。",
    "rounds_max": "讨论轮次已到上限，仍有决策点没有达成一致。",
}
CONSENSUS_TITLES = {
    "all_closed": "本轮的决策点已全部了结",
    "round_end": "一轮讨论结束，请确认共识",
    "stall": "讨论陷入停滞，请决定怎么处理",
    "rounds_max": "讨论轮次已到上限，请决定怎么处理",
}
MODE_NAMES = {"facilitator": "主持人", "participant": "参会者", "proxy": "需求方代理，只回答事实问题"}
SOURCE_NAMES = {"user": "用户回答", "proxy": "需求方代理，依据背景材料或用户回答", "assumption": "假设"}
_EXCHANGE_KINDS = ["claim", "challenge", "respond", "question", "answer"]
_FEEDBACK_KINDS = ["user_feedback", "user_guidance", "user_intervention"]
_ROUND_KINDS = _EXCHANGE_KINDS + ["exchange_summary"]

BRIEF_INSTRUCTION = (
    "\n\n做会前准备。依据上面的想法原文与背景材料，写一份简报，并列出需要向需求方澄清的问题。"
    "想法过于笼统时，把说不清的地方列成问题；材料里已经写明的不要再问。只输出一个 JSON 对象：\n"
    '{"brief": "目标、用户、约束、假设", "questions": ["需要澄清的问题"]}\n'
    "没有要澄清的问题时，questions 给空数组。")
ANSWER_INSTRUCTION = (
    "需求方提出了下面这些澄清问题。你只依据背景材料里写明的内容回答。只输出一个 JSON 对象：\n"
    '{"answers": [{"text": "回答", "source": "出处：引用背景材料的片段"}]}\n'
    "answers 与问题逐条对应、数量相同。材料回答不了的，text 写明“材料中没有说明”，source 给空字符串。")
ROSTER_INSTRUCTION = (
    "请为这场讨论提议参会名单：从角色库里挑选视角互补的角色，覆盖这个想法涉及的主要风险。"
    "库里没有合适的视角时，可以提临时角色：role_key 留空，并写出 display_name 与 persona"
    "（人设写具体的偏好，不写标签）。只输出一个 JSON 对象：\n"
    '{"participants": [{"role_key": "architect", "display_name": "可省略", '
    '"persona": "临时角色的人设", "reason": "为什么请它"}]}')
POSITION_INSTRUCTION = (
    "请写下你的立场简报。从你的专业视角出发，写具体的偏好和理由；这一步你看不到别人的立场。"
    "只输出一个 JSON 对象：\n"
    '{"must": ["必须保证的事项"], "risks": ["主要风险"], "route": "你建议的路线", "questions": ["疑问"]}')
AGENDA_INSTRUCTION = (
    "请把各方的立场合并成一份决策点清单：需要了结才能往下走的问题。每个决策点有一个明确的问题"
    "和几个候选项，并注明是谁提出来的。立场一致、无需选择的不要列。只输出一个 JSON 对象：\n"
    '{"points": [{"title": "短标题", "question": "要决定什么", "options": ["候选一", "候选二"], '
    '"raised_by": "提出者"}]}')
STANCE_INSTRUCTION = (
    "请阅读全部立场和完整的决策点清单，对每个决策点表态。不要因为别人都同意就附和。"
    "只输出一个 JSON 对象：\n"
    '{"stances": [{"index": "D1", "level": 1, "reason": "理由", "choice": "你支持的候选项"}], '
    '"new_points": [{"title": "清单遗漏的决策点", "question": "", "options": [], "level": 5, "reason": ""}]}\n'
    "level：1 支持，2 有保留支持（写明顾虑），3 弃权（写明原因），4 需更多信息（写明缺什么），"
    "5 反对（写理由和替代建议）。new_points 可省略。")
RESTANCE_INSTRUCTION = (
    "本轮交锋结束了，请对账本里每个未了结的决策点重新表态，格式同上。"
    "如果你改变了立场，reason 必须写明是因为哪条意见；没有改变就沿用原来的理由。")
SUMMARY_INSTRUCTION = (
    "请为每个未了结的决策点写一段交锋摘要：各方的主张、各自的理由、有没有人改变立场、还差什么。"
    "忠实于发言原文，分歧不得抹平。只输出一个 JSON 对象：\n"
    '{"summaries": [{"index": "D1", "summary": "交锋摘要"}]}')
RULING_INSTRUCTION = (
    "请为这些决策点做出裁决并写明理由。裁决要回应当前各方的理由，不偏向任何一方；"
    "没有被采纳的意见记入 dissent，不要抹掉。只输出一个 JSON 对象：\n"
    '{"rulings": [{"index": "D1", "conclusion": "结论", "rationale": "裁决理由", "dissent": "保留的异议"}]}')
PROPOSAL_INSTRUCTION = (
    "请按决策点账本写一份《建设方案》，只输出 Markdown 正文，不要围栏。依次包含：\n"
    "## 目标与成功标准\n## 范围（必做 / 不做）\n"
    "## 总体方案（逐个决策点写结论和依据，标注决策点编号，如 D1）\n"
    "## 关键流程\n## 里程碑与验收\n## 风险与缓解\n"
    "已了结的决策点按账本的结论写；待定的决策点按各方的理由如实写出分歧，不要替用户决定。"
    "不要写“待确认问题”和“决策点索引”两节，它们由程序附在后面。")
REVISION_INSTRUCTION = (
    "这份方案需要修订。在上一版的基础上处理账本里改动的决策点：开头先写一节“## 修改说明”，"
    "逐条写明改了哪个决策点、改成了什么、依据是什么；没有改动的部分原样保留。"
    "其余要求同首次成稿：不要写“待确认问题”和“决策点索引”两节。")
CHECK_INSTRUCTION = (
    "请阅读这份建设方案初稿，逐个确认已了结的决策点是否被如实反映：结论对不对、依据有没有被改写、"
    "你此前标记的顾虑有没有交代。只输出一个 JSON 对象：\n"
    '{"ok": true, "issues": []}\n'
    '发现问题时写 {"ok": false, "issues": [{"index": "D1", "problem": "哪里没有如实反映"}]}。'
    "只看事实，不借机重开已经了结的讨论。")
REVISE_INSTRUCTION = (
    "参会者对方案初稿提出了复核意见。请只修改被标记的决策点涉及的内容，其他部分原样保留；"
    "开头写一节“## 修改说明”逐条回应。输出完整的新版方案，不要写“待确认问题”和“决策点索引”两节。")
REOPEN_INSTRUCTION = (
    "会议带着一份反馈续开。请判断这份反馈要求重开哪些决策点：已有的给出 index；"
    "反馈涉及账本里没有的问题时新建，给出 title、question、options。至少处理出一个决策点。"
    "只输出一个 JSON 对象：\n"
    '{"points": [{"index": "D2", "title": "", "question": "", "options": []}], "note": "你对反馈的理解"}')
PROPOSAL_SECTIONS = proposal.PROPOSAL_SECTIONS


# ---------------- 校验模型的结构化输出 ----------------
def _need(ok: bool, message: str) -> list[str]:
    return [] if ok else [message]


def _validate_brief(data: dict) -> list[str]:
    issues = _need(isinstance(data.get("brief"), str) and bool(data["brief"].strip()),
                   "brief 须是非空的文本")
    questions = data.get("questions", [])
    if not isinstance(questions, list) or not all(isinstance(item, str) for item in questions):
        issues.append("questions 须是字符串数组；没有要澄清的就给空数组")
    return issues


def _validate_answers(count: int) -> Validator:
    def validate(data: dict) -> list[str]:
        rows = data.get("answers")
        if not isinstance(rows, list) or len(rows) != count:
            return [f"answers 须恰好包含 {count} 项，逐条对应问题"]
        return [f"第 {position + 1} 条回答缺少 text"
                for position, row in enumerate(rows)
                if not isinstance(row, dict) or not str(row.get("text") or "").strip()]
    return validate


def _validate_roster(library: dict, low: int, high: int) -> Validator:
    def validate(data: dict) -> list[str]:
        rows = data.get("participants")
        if not isinstance(rows, list) or not rows:
            return ["participants 须是非空数组"]
        issues: list[str] = []
        keys = [str(row.get("role_key") or "") for row in rows if isinstance(row, dict)]
        keys = [key for key in keys if key]
        if len(set(keys)) != len(keys):
            issues.append("participants 里的 role_key 不能重复")
        for row in rows:
            if not isinstance(row, dict):
                issues.append("participants 的每一项须是对象")
                continue
            key = str(row.get("role_key") or "")
            if key and key not in library:
                issues.append(f"role_key 不存在：{key}")
            if not key and not (str(row.get("display_name") or "").strip()
                                and str(row.get("persona") or "").strip()):
                issues.append("临时角色必须同时给出 display_name 与 persona")
        if low == high:
            issues += _need(len(rows) == low, f"用户指定了固定人数：必须正好 {low} 名")
        else:
            issues += _need(low <= len(rows) <= high, f"人数须在 {low} 到 {high} 名之间")
        return issues
    return validate


def _validate_position(data: dict) -> list[str]:
    issues = []
    for key in ("must", "risks"):
        if not isinstance(data.get(key), list) or not all(isinstance(x, str) for x in data[key]):
            issues.append(f"{key} 须是字符串数组")
    issues += _need(isinstance(data.get("route"), str) and bool(data["route"].strip()),
                    "route 须是非空的文本")
    return issues


def _validate_agenda(data: dict) -> list[str]:
    rows = data.get("points")
    if not isinstance(rows, list) or not rows:
        return ["points 须是非空数组：至少提炼出一个决策点"]
    issues: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            issues.append("points 的每一项须是对象")
            continue
        if not isinstance(row.get("title"), str) or not row["title"].strip():
            issues.append("每个决策点须有 title")
        if row.get("question") is not None and not isinstance(row["question"], str):
            issues.append(f"{row.get('title') or '决策点'}：question 须是文本")
        options = row.get("options")
        if options is not None and (not isinstance(options, list)
                                    or not all(isinstance(item, str) for item in options)):
            issues.append(f"{row.get('title') or '决策点'}：options 须是字符串数组")
    return issues


def _validate_stances(required: list[str] | None = None) -> Validator:
    """表态的校验。required 给出时要求覆盖这些未了结决策点（复议后尤其重要）。"""
    required_indexes = list(required or [])

    def validate(data: dict) -> list[str]:
        rows = data.get("stances")
        if not isinstance(rows, list):
            return ["stances 须是数组"]
        if required_indexes and not rows:
            return ["stances 须是非空数组"]
        issues: list[str] = []
        required_fields = {2: "顾虑", 3: "弃权的原因", 4: "缺什么信息", 5: "反对的理由和替代建议"}
        covered: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                issues.append("stances 的每一项须是对象")
                continue
            index = str(row.get("index") or "").strip()
            if not index:
                issues.append("stances 的每一项须有 index")
                continue
            covered.add(index)
            level = row.get("level")
            if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 5:
                issues.append(f"{index}：level 须是 1 到 5 的整数")
            elif level >= 2 and not str(row.get("reason") or "").strip():
                issues.append(f"{index}：{LEVEL_NAMES[level]}必须写明{required_fields[level]}")
        missing = [index for index in required_indexes if index not in covered]
        if missing:
            issues.append("缺少这些未了结决策点的表态：" + "、".join(missing))
        for extra in data.get("new_points") or []:
            if not isinstance(extra, dict):
                issues.append("new_points 的每一项须是对象")
                continue
            if not isinstance(extra.get("title"), str) or not extra["title"].strip():
                issues.append("new_points 的每一项须有 title")
            level = extra.get("level")
            if level is not None and (isinstance(level, bool) or not isinstance(level, int)
                                      or not 1 <= level <= 5):
                issues.append("new_points 的 level 须是 1 到 5 的整数")
            options = extra.get("options")
            if options is not None and (not isinstance(options, list)
                                        or not all(isinstance(item, str) for item in options)):
                issues.append("new_points 的 options 须是字符串数组")
        return issues

    return validate


def _validate_summaries(open_indexes: list[str]) -> Validator:
    def validate(data: dict) -> list[str]:
        rows = data.get("summaries")
        if not isinstance(rows, list):
            return [f"summaries 须恰好覆盖这些决策点：{open_indexes}"]
        if any(not isinstance(row, dict) for row in rows):
            return ["summaries 的每一项须是对象"]
        covered = {str(row.get("index")) for row in rows}
        if covered != set(open_indexes):
            return [f"summaries 须恰好覆盖这些决策点：{open_indexes}"]
        return [f"{row['index']} 的 summary 不能为空" for row in rows
                if not isinstance(row.get("summary"), str) or not row["summary"].strip()]
    return validate


def _validate_rulings(open_indexes: list[str]) -> Validator:
    def validate(data: dict) -> list[str]:
        rows = data.get("rulings")
        if not isinstance(rows, list):
            return [f"rulings 须恰好覆盖这些决策点：{open_indexes}"]
        if any(not isinstance(row, dict) for row in rows):
            return ["rulings 的每一项须是对象"]
        covered = {str(row.get("index")) for row in rows}
        if covered != set(open_indexes):
            return [f"rulings 须恰好覆盖这些决策点：{open_indexes}"]
        issues: list[str] = []
        for row in rows:
            for field in ("conclusion", "rationale"):
                if not isinstance(row.get(field), str) or not row[field].strip():
                    issues.append(f"{row['index']} 的 {field} 不能为空")
        return issues
    return validate


def _validate_check(data: dict) -> list[str]:
    if not isinstance(data.get("ok"), bool):
        return ["ok 须是 true 或 false"]
    if data["ok"]:
        return []
    rows = data.get("issues")
    if not isinstance(rows, list) or not rows:
        return ["ok 为 false 时 issues 须列出至少一条意见"]
    return ["每条复核意见须有 problem" for row in rows
            if not isinstance(row, dict)
            or not isinstance(row.get("problem"), str) or not row["problem"].strip()]


def _validate_reopen(data: dict) -> list[str]:
    rows = data.get("points")
    if not isinstance(rows, list) or not rows:
        return ["points 须是非空数组：至少重开或新建一个决策点"]
    issues: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            issues.append("points 的每一项须是对象")
            continue
        index = row.get("index")
        title = row.get("title")
        if index is not None and not isinstance(index, str):
            issues.append("index 须是文本")
        if title is not None and not isinstance(title, str):
            issues.append("title 须是文本")
        if not (str(index or "").strip() or str(title or "").strip()):
            issues.append("已有的决策点给 index；新的决策点给 title")
        options = row.get("options")
        if options is not None and (not isinstance(options, list)
                                    or not all(isinstance(item, str) for item in options)):
            issues.append("options 须是字符串数组")
    return issues


# ---------------- 读写账本与发言 ----------------
def _artifact_body(run_id: str, kind: str) -> str:
    raw = read_artifact(run_id, kind)
    return parse_front_matter(raw)[1].strip() if raw else ""


def _facilitator(mtg) -> dict | None:
    return next(iter(mtg.participants("facilitator")), None)


def _proxy(mtg) -> dict | None:
    return next(iter(mtg.participants("proxy")), None)


def _proxy_needed(mtg) -> bool:
    return bool(mtg.params().get("proxy_enabled")) or mtg.decider("clarify") == "proxy"


def _points(mtg) -> list[dict]:
    return store.list_decision_points(mtg.meeting_id)


def _open_points(points: list[dict]) -> list[dict]:
    return [point for point in points if point["status"] in OPEN_STATUSES]


def _voters(mtg) -> list[int]:
    return [member["id"] for member in mtg.participants("participant")]


def _rounds_max(mtg) -> int:
    return max(1, int(mtg.params().get("rounds_max") or 20))


def _charter(mtg) -> list[Segment]:
    return render.charter_segments(mtg.meeting()["charter"])


def _accounted_points(mtg) -> list[dict]:
    """账本里当前还作数的表态：复议轮只认重开之后写下的表态。

    旧表态留在 history 与 stances_json 里供人回看，但不再参与结算——
    否则复议后模型漏答某个点，旧等级会把它直接判为"已一致"。
    """
    floors = {str(index): int(round_no)
              for index, round_no in (mtg.state().get("reopen_rounds") or {}).items()}
    accounted = []
    for point in _points(mtg):
        floor = floors.get(point["index"])
        if floor:
            point = {**point, "stances": {
                pid: stance for pid, stance in (point.get("stances") or {}).items()
                if int((stance or {}).get("round") or 0) >= floor}}
        accounted.append(point)
    return accounted


def _feedback_segments(mtg) -> list[Segment]:
    """用户反馈、指导语与插话：每一轮的角色都该看到，不随轮次过滤掉。"""
    return [render.turn_segment(turn)
            for turn in store.turns_upto(mtg.meeting_id, upto_round=mtg.meeting_round(),
                                         kinds=_FEEDBACK_KINDS)]


def _view(mtg, kinds: list[str] | None = None) -> list[Segment]:
    """人设之后发给模型的段：账本快照在前，用户反馈/插话随后，本轮的逐字发言最后。"""
    points = _points(mtg)
    index_of = {point["id"]: point["index"] for point in points}
    segments = [render.ledger_segment(points, round_no=mtg.meeting_round())]
    segments += _feedback_segments(mtg)
    for turn in store.round_turns(mtg.meeting_id, round=mtg.meeting_round(), kinds=kinds):
        turn["decision_point"] = index_of.get(turn["decision_point_id"])
        segments.append(render.turn_segment(turn))
    return segments


def _positions(mtg) -> list[Segment]:
    return [render.turn_segment(turn)
            for turn in store.round_turns(mtg.meeting_id, round=1, kinds=["position"])]


def _stance_view(mtg) -> list[Segment]:
    """表态的上下文：账本 + 第 1 轮的全体立场 + 用户反馈（复议轮同样看得到原始立场）。"""
    return ([render.ledger_segment(_points(mtg), round_no=mtg.meeting_round())]
            + _positions(mtg) + _feedback_segments(mtg))


async def _ledger_updated(mtg) -> None:
    version = int(mtg.state().get("ledger_version") or 0) + 1
    mtg.set_state(ledger_version=version)
    await emit(mtg.run_id, "meeting_ledger_updated", mtg.node_id, {
        "meeting_id": mtg.meeting_id, "version": version,
        "decision_points": [{"index": point["index"], "status": point["status"]}
                            for point in _points(mtg)]})


def _mark(mtg, key: str) -> None:
    """记下交锋里已经完成的哪一次发言，重入时据此跳过。"""
    done = set(mtg.state().get("exchange_done") or [])
    mtg.set_state(exchange_done=sorted(done | {key}))


def _close(mtg, index: str, status: str, conclusion: str, rationale: str = "") -> None:
    for point in _points(mtg):
        if point["index"] == index:
            store.update_decision_point(point["id"], status=status, conclusion=conclusion,
                                        rationale=rationale or point["rationale"],
                                        closed_round=mtg.meeting_round())


def _conclusion_of(point: dict) -> str:
    """达成一致的决策点，结论取支持者里最常见的选择。"""
    choices = Counter((stance.get("choice") or "").strip()
                      for stance in (point.get("stances") or {}).values()
                      if stance.get("level") in (1, 2) and (stance.get("choice") or "").strip())
    return choices.most_common(1)[0][0] if choices else "见交锋摘要"


def _settle_round(mtg) -> dict:
    """一轮表态之后结算：达成一致的落盘。返回结算结果（open、agreed、changed）。

    复议轮只认重开之后写下的表态（_accounted_points），重开前的旧等级不参与结算。
    """
    points = _accounted_points(mtg)
    result = settle(points, rule=str(mtg.params().get("closure_rule") or "unanimous"),
                    voters=_voters(mtg), before=mtg.state().get("stance_snapshot"))
    by_index = {point["index"]: point for point in points}
    for index in result["agreed"]:
        point = by_index[index]
        supporters = [f"{stance.get('name')}：{stance.get('reason')}"
                      for stance in (point.get("stances") or {}).values()
                      if stance.get("level") in (1, 2) and stance.get("reason")]
        _close(mtg, index, "agreed", _conclusion_of(point), "；".join(supporters)[:600])
    return result


def _apply_stances(mtg, participant: dict, data: dict) -> None:
    """把一次表态写进账本。表态里补充的遗漏决策点先建成新的决策点。"""
    by_index = {point["index"]: point for point in _points(mtg)}
    number = len(by_index) + 1
    round_no = mtg.meeting_round()
    rows = list(data["stances"])
    for extra in data.get("new_points") or []:
        index = f"D{number}"
        number += 1
        point_id = store.upsert_decision_point(
            mtg.meeting_id, index, title=extra["title"].strip(),
            question=str(extra.get("question") or ""), options_json=list(extra.get("options") or []),
            raised_by=participant["display_name"], origin_round=round_no)
        by_index[index] = {"id": point_id, "index": index}
        rows.append({"index": index, "level": extra.get("level", 3),
                     "reason": extra.get("reason", ""), "choice": extra.get("choice", "")})
    for row in rows:
        point = by_index.get(str(row.get("index")))
        if point is None:
            continue                                   # 账本里没有的编号：不计入
        store.set_stance(point["id"], participant["id"], {
            "level": int(row["level"]), "reason": str(row.get("reason") or ""),
            "choice": str(row.get("choice") or ""), "round": round_no,
            "name": participant["display_name"]})


def _can_join_discussion(role: dict) -> bool:
    """能当讨论参会者的角色：元数据标了适合讨论，或者没有标会议角色（用户自建、没填）。"""
    roles = (role.get("meta") or {}).get("meeting_roles")
    return "discussion" in roles if roles is not None else True


def _write_charter(mtg) -> None:
    """会议章程：整场会议不变，所有角色共用。"""
    state = mtg.state()
    params = mtg.params()
    library = {row["role_key"]: row for row in role_library()}
    roster = []
    for row in mtg.participants():
        focus = ((library.get(row["role_key"] or "") or {}).get("meta") or {}).get("description")
        roster.append(f"- {row['display_name']}（{MODE_NAMES.get(row['mode'], row['mode'])}）："
                      f"{focus or row['system_prompt'][:60]}")
    clarifications = state.get("clarifications") or []
    answered = "\n".join(
        f"- 问：{item['question']}\n  答：{item['answer']}（{SOURCE_NAMES[item['source']]}）"
        for item in clarifications) or "（没有需要澄清的问题）"
    rule = str(params.get("closure_rule") or "unanimous")
    materials, _truncated = mtg.materials()
    charter = {
        "title": "讨论会议章程",
        "sections": [
            {"title": "想法原文", "text": (mtg.ctx.idea or "").strip()},
            {"title": "想法简报", "text": state.get("brief") or ""},
            {"title": "已澄清的问题", "text": answered},
            {"title": "参会名单", "text": "\n".join(roster)},
            {"title": "闭合规则", "text": CLOSURE_TEXT.get(rule, CLOSURE_TEXT["unanimous"])},
            {"title": "轮次", "text": (
                f"讨论最多进行 {_rounds_max(mtg)} 轮；"
                f"连续 {params.get('stall_rounds') or 2} 轮没有人改变立场时，进入共识确认。")},
        ],
        "materials": materials,
    }
    store.update_meeting(mtg.meeting_id, charter_json=charter)


def _roster_description(proposed: list[dict], low: int, high: int,
                        fixed: int | None) -> str:
    lines = ["主持人提议的参会名单："]
    lines += [f"- {row['display_name']}：{row.get('reason') or ''}" for row in proposed]
    lines += ["", "用户指定了固定人数。" if fixed else f"人数要求：{low} 到 {high} 名。",
              "可以直接通过；也可以先增删角色、改人设或模型，再通过；",
              "或者驳回并写明意见，让主持人重新提议。"]
    return "\n".join(lines)


def _open_brief(point: dict) -> dict:
    stances = sorted((point.get("stances") or {}).items(), key=lambda item: int(item[0]))
    return {"index": point["index"], "title": point["title"], "question": point["question"],
            "options": list(point.get("options") or []),
            "stances": [{"name": stance.get("name"),
                         "level": LEVEL_NAMES.get(stance.get("level"), "未表态"),
                         "reason": stance.get("reason") or ""} for _, stance in stances]}


def _consensus_description(mtg, open_points: list[dict], reason: str) -> str:
    rule = str(mtg.params().get("closure_rule") or "unanimous")
    lines = [REASON_TEXT.get(reason, ""),
             f"已进行 {mtg.meeting_round()} 轮。闭合规则：{CLOSURE_TEXT.get(rule, '')}"]
    if open_points:
        lines += ["", "未了结的决策点："]
        for point in open_points:
            lines.append(f"- {point['index']} {point['title']}")
            for stance in _open_brief(point)["stances"]:
                lines.append(f"  - {stance['name']}：{stance['level']}"
                             + (f"——{stance['reason']}" if stance["reason"] else ""))
    lines += ["", "请选择："]
    lines += [f"{number}. {option['label']}：{option['description']}"
              for number, option in enumerate(CONSENSUS_OPTIONS, start=1)]
    return "\n".join(lines)


@register_protocol
class DiscussionProtocol(MeetingProtocol):
    name = "discussion"
    steps = ("prepare", "clarify", "roster", "positions", "agenda", "stances", "exchange",
             "summary", "restance", "consensus", "reopen", "draft", "check")

    async def on_enter(self, mtg) -> None:
        state = mtg.state()
        mtg.consume_feedback()
        state = mtg.state()
        feedback = str(state.get("reopen_feedback") or "").strip()
        if not feedback:
            return
        # 只有已经推进到议题与表态阶段，反馈才走"复议"。澄清、名单阶段的反馈
        # 不能把会议直接跳到复议：否则名单、立场、议题都不会执行，会议空着就成稿。
        if not (state.get("roster_done") and _points(mtg)):
            if not state.get("early_feedback"):
                speaker = ("评审意见" if state.get("feedback_kind") == "reroute"
                           else "用户")
                phase = store.get_meeting(mtg.meeting_id)["phase"]
                store.add_turn(mtg.meeting_id, round=mtg.meeting_round(), phase=phase,
                               speaker_id=None, speaker_name=speaker, kind="user_feedback",
                               content=feedback)
                changes = {"early_feedback": feedback, "reopen_feedback": ""}
                if phase == "roster":
                    changes["roster_feedback"] = feedback
                mtg.set_state(**changes)
            return
        mtg.set_state(reopen_feedback=feedback)
        store.update_meeting(mtg.meeting_id, phase="reopen")

    # ---------- 会前准备与澄清 ----------
    async def step_prepare(self, mtg):
        if _facilitator(mtg) is None:
            template = role_template("facilitator")
            store.add_participant(mtg.meeting_id, role_key="facilitator",
                                  display_name=template["name"], mode="facilitator",
                                  system_prompt=template["system_prompt"])
        if _proxy_needed(mtg) and _proxy(mtg) is None:
            template = role_template("requester_proxy")
            store.add_participant(mtg.meeting_id, role_key="requester_proxy",
                                  display_name=template["name"], mode="proxy",
                                  system_prompt=template["system_prompt"])
        if mtg.state().get("brief"):
            return advance()                          # 简报已经写过（续开时）
        materials, _truncated = mtg.materials()       # 截断说明已由 read_materials 附在文本里
        segments = []
        if materials:
            segments.append(Segment("append", f"## 背景材料\n{materials}", name="背景材料"))
        result = await speak(mtg, _facilitator(mtg), kind="brief", turn_kind="brief",
                             segments=segments, structured=_validate_brief,
                             turn_round=mtg.meeting_round(),
                             instruction=f"## 想法原文\n{(mtg.ctx.idea or '').strip()}"
                                         f"{BRIEF_INSTRUCTION}")
        brief = result["data"]["brief"].strip()
        questions = [str(item).strip() for item in result["data"].get("questions") or []
                     if str(item).strip()]
        write_artifact(mtg.run_id, "idea",
                       f"# 想法\n\n{(mtg.ctx.idea or '').strip()}\n\n---\n\n# 想法简报\n\n{brief}\n",
                       created_by="facilitator", inputs=["idea"], status="approved")
        await emit(mtg.run_id, "artifact_written", mtg.node_id,
                   latest_artifact(mtg.run_id, "idea"))
        mtg.set_state(brief=brief, questions=questions)
        return advance()

    async def step_clarify(self, mtg):
        state = mtg.state()
        if "clarifications" in state:
            return advance()
        questions = state.get("questions") or []
        if not questions:
            mtg.set_state(clarifications=[])
            return advance()
        listing = "\n".join(f"{number}. {question}"
                            for number, question in enumerate(questions, start=1))
        if mtg.decider("clarify") == "proxy":
            proxy = _proxy(mtg)
            rows = [{"text": "", "source": ""} for _ in questions]   # 没有代理可用：全部记为假设
            if proxy is not None:
                materials, _truncated = mtg.materials()
                segments = ([Segment("append", f"## 背景材料\n{materials}", name="背景材料")]
                            if materials else [])
                rows = (await speak(
                    mtg, proxy, kind="answer", turn_kind="answer", segments=segments,
                    instruction=f"{ANSWER_INSTRUCTION}\n\n## 待澄清的问题\n{listing}",
                    structured=_validate_answers(len(questions)),
                    turn_round=mtg.meeting_round()))["data"]["answers"]
            clarifications = []
            for question, row in zip(questions, rows):
                text = str(row.get("text") or "").strip()
                evidence = str(row.get("source") or "").strip()
                if text and evidence:
                    clarifications.append({"question": question, "answer": text,
                                           "source": "proxy", "evidence": evidence})
                else:                                  # 没有出处的回答：标为假设
                    clarifications.append({
                        "question": question, "source": "assumption", "evidence": "",
                        "answer": f"{text}（没有出处，按假设处理）" if text
                        else "材料中没有说明，按假设处理"})
            await record_auto_decision(
                mtg, "clarify", decided_by="proxy" if proxy else "program",
                decision={"action": "approve", "answered": len(questions),
                          "assumptions": sum(1 for item in clarifications
                                             if item["source"] == "assumption")},
                title="澄清问题已回答")
            mtg.set_state(clarifications=clarifications)
            return advance()
        decision = mtg.decision()
        if decision is None:
            return suspend(await mtg.open_checkpoint(
                "clarify", title="请回答几个澄清问题", description=listing,
                payload={"questions": questions}))
        answers = [str(item).strip() for item in decision["data"].get("answers") or []]
        clarifications = []
        for number, question in enumerate(questions, start=1):
            text = answers[number - 1] if number <= len(answers) else ""
            if text:
                source = "user"
            else:
                text, source = "用户没有回答，按假设处理", "assumption"
            clarifications.append({"question": question, "answer": text,
                                   "source": source, "evidence": ""})
            store.add_turn(mtg.meeting_id, round=mtg.meeting_round(), phase="clarify",
                           speaker_id=None, speaker_name="用户", kind="answer",
                           content=f"问：{question}\n答：{text}")
        mtg.set_state(clarifications=clarifications)
        mtg.ack_decision(decision)
        return advance()

    # ---------- 名单 ----------
    async def step_roster(self, mtg):
        state = mtg.state()
        if state.get("roster_done"):
            return advance()
        params = mtg.params()
        minimum = max(1, int(params.get("participants_min") or 1))
        maximum = max(minimum, int(params.get("participants_max") or minimum))
        spec = (mtg.run_config().get("rosters") or {}).get("participants") or {}
        fixed = int(spec["count"]) if spec.get("count") else None
        low, high = (minimum, maximum) if fixed is None else (fixed, fixed)
        if not state.get("roster"):
            await self._propose(mtg, spec, low, high)
        source = mtg.state()["roster_source"]
        decider = mtg.decider("roster")
        if source == "user" or decider == "auto":
            if source != "user":
                await record_auto_decision(
                    mtg, "roster", decided_by="program", title="参会名单自动生效",
                    decision={"action": "approve",
                              "participants": [row["display_name"]
                                               for row in mtg.participants("participant")]})
            return self._finish_roster(mtg)
        decision = mtg.decision()
        if decision is None:
            return suspend(await mtg.open_checkpoint(
                "roster", title="请确认参会名单",
                description=_roster_description(mtg.state()["roster"], low, high, fixed),
                payload={"proposed": mtg.state()["roster"], "min": low, "max": high,
                         "fixed": fixed}))
        if decision["action"] == "approve" and mtg.participants("participant"):
            result = self._finish_roster(mtg)
            mtg.ack_decision(decision)
            return result
        # 驳回，或者通过时名单是空的：带着意见重新提议
        for row in mtg.participants("participant"):
            store.update_participant(row["id"], status="removed")
        mtg.set_state(roster=None,
                      roster_feedback=decision.get("feedback") or "至少需要一名参会角色")
        mtg.ack_decision(decision)
        return advance("roster")

    async def _propose(self, mtg, spec: dict, low: int, high: int) -> None:
        """名单的优先级：用户点名 > 主持人在人数要求内挑选。结果写成参会者。"""
        library = {row["role_key"]: row for row in role_library()}
        if spec.get("role_keys"):
            keys = [key for key in spec["role_keys"] if key in library]
            if not keys:
                raise NodeFailure("运行级配置里点名的参会角色都已不存在，请修改运行级配置")
            picked = [{"role_key": key, "display_name": library[key]["name"], "reason": "用户指定"}
                      for key in keys]
            source = "user"
        else:
            offered = [row for row in library.values() if _can_join_discussion(row)]
            listing = "\n".join(f"- {row['role_key']}｜{row['name']}｜"
                               f"{row['meta'].get('description') or ''}" for row in offered)
            count_text = f"请正好挑选 {low} 名" if low == high else f"请挑选 {low} 到 {high} 名"
            segments = [Segment("append", f"## 想法\n{mtg.ctx.idea or ''}\n\n"
                                          f"## 想法简报\n{mtg.state().get('brief') or ''}",
                                name="想法与简报"),
                        Segment("append", f"## 角色库（可以从中挑选）\n{listing}", name="角色库")]
            feedback = mtg.state().get("roster_feedback")
            if feedback:
                segments.append(Segment("append", f"## 用户对上一份名单的意见\n{feedback}",
                                        name="名单意见"))
            picked = (await speak(
                mtg, _facilitator(mtg), kind="roster", turn_kind="roster_proposal",
                segments=segments, instruction=f"{ROSTER_INSTRUCTION}\n{count_text}。",
                structured=_validate_roster(library, low, high),
                turn_round=mtg.meeting_round()))["data"]["participants"]
            source = "moderator"
        normalized: list[dict] = []
        for row in picked:
            key = str(row.get("role_key") or "")
            template = library.get(key)
            display = str(row.get("display_name")
                          or (template["name"] if template else "")).strip()
            normalized.append({**row, "role_key": key or None, "display_name": display})
        for row in normalized:
            key = str(row.get("role_key") or "")
            template = library.get(key)
            store.add_participant(
                mtg.meeting_id, role_key=key or None,
                display_name=str(row.get("display_name") or "").strip(),
                mode="participant",
                system_prompt=template["system_prompt"] if template
                else str(row.get("persona") or ""),
                source=source, expertise=list((template or {}).get("meta", {}).get("tags") or []))
        mtg.set_state(roster=normalized, roster_source=source)

    def _finish_roster(self, mtg):
        _write_charter(mtg)
        mtg.set_state(roster_done=True)
        return advance()

    # ---------- 立场、议题、表态 ----------
    async def step_positions(self, mtg):
        members = mtg.participants("participant")
        if not members:
            raise NodeFailure("讨论会议没有参会者")
        charter = _charter(mtg)
        segments = _feedback_segments(mtg)             # 早期阶段收到的用户反馈/插话
        await speak_many(mtg, [
            {"participant": member, "kind": "position", "turn_kind": "position",
             "charter": charter, "segments": segments, "instruction": POSITION_INSTRUCTION,
             "structured": _validate_position, "turn_round": mtg.meeting_round()}
            for member in members])
        return advance()

    async def step_agenda(self, mtg):
        if mtg.state().get("agenda_done"):
            return advance()
        facilitator = _facilitator(mtg)
        # 断点保护：模型这一步已落盘就复用，不重复调用；决策点按位置编号，重写幂等
        found = store.find_turn(mtg.meeting_id, phase=mtg.meeting()["phase"],
                                round=mtg.meeting_round(), kind="agenda",
                                speaker_id=facilitator["id"])
        data = found["data"] if found else (await speak(
            mtg, facilitator, kind="agenda", turn_kind="agenda",
            charter=_charter(mtg), segments=_positions(mtg),
            instruction=AGENDA_INSTRUCTION, structured=_validate_agenda,
            turn_round=mtg.meeting_round()))["data"]
        for offset, item in enumerate(data["points"]):
            store.upsert_decision_point(
                mtg.meeting_id, f"D{offset + 1}", title=item["title"].strip(),
                question=str(item.get("question") or ""),
                options_json=list(item.get("options") or []),
                raised_by=str(item.get("raised_by") or ""), origin_round=mtg.meeting_round())
        mtg.set_state(agenda_done=True)
        await _ledger_updated(mtg)
        return advance()

    async def step_stances(self, mtg):
        members = mtg.participants("participant")
        open_points = _open_points(_points(mtg))
        if len(members) <= 1:
            # 只有一名参会者：立场即共识，不经过表态和交锋
            for point in open_points:
                _close(mtg, point["index"], "agreed", "唯一参会者的立场", "只有一名参会者")
            await _ledger_updated(mtg)
            return advance("reopen")
        charter = _charter(mtg)
        view = _stance_view(mtg)
        results = await speak_many(mtg, [
            {"participant": member, "kind": "stance", "turn_kind": "stance", "charter": charter,
             "segments": view, "instruction": STANCE_INSTRUCTION,
             "structured": _validate_stances([point["index"] for point in open_points]),
             "turn_round": mtg.meeting_round()} for member in members])
        for member, result in zip(members, results):
            _apply_stances(mtg, member, result["data"])
        _settle_round(mtg)
        remaining = _open_points(_points(mtg))
        await _ledger_updated(mtg)
        mtg.reset_builder()                           # 账本变了，下一次调用以新账本为基线
        if not remaining:
            mtg.set_state(consensus_reason="all_closed")
            return advance("consensus")
        mtg.set_state(stance_snapshot=snapshot(remaining), stall=0, exchange_done=[], exchanges={})
        return advance()

    # ---------- 交锋轮次 ----------
    async def step_exchange(self, mtg):
        members = mtg.participants("participant")
        limit = max(1, int(mtg.params().get("exchanges_per_point") or 2))
        round_no = mtg.meeting_round()
        charter = _charter(mtg)
        by_id = {member["id"]: member for member in members}
        done = set(mtg.state().get("exchange_done") or [])
        exchanges: dict[str, int] = {}
        for point in _open_points(_points(mtg)):
            ranked = rank_speakers(point, [member["id"] for member in members])
            claimer = by_id[ranked[0]]
            # 质疑者从最反对的一端开始轮换；交锋次数大于人数时按环取，不会越界
            opponents = [voter for voter in reversed(ranked) if voter != ranked[0]]
            subject = f"决策点 {point['index']}「{point['title']}」：{point['question']}"
            exchanges[point["index"]] = 0
            for number in range(1, limit + 1):
                mark = f"{point['index']}:{number}"
                opponent = (opponents[(number - 1) % len(opponents)] if opponents
                            else ranked[0])
                challenger = by_id[opponent]
                instructions = {
                    "claim": f"关于{subject}，请主张你支持的立场，给出理由和依据。只写一段话。",
                    "challenge": (f"关于{subject}，请质疑刚才的主张：指出它站不住的地方并给出理由；"
                                  "如果你的立场不同，说明你支持什么。只写一段话。"),
                    "respond": (f"关于{subject}，请回应刚才的质疑：采纳并说明你如何调整立场，"
                                "或不采纳并说明理由。只写一段话。"),
                }
                for kind, speaker in (("claim", claimer), ("challenge", challenger),
                                      ("respond", claimer)):
                    if f"{mark}:{kind}" in done:
                        continue
                    await speak(mtg, speaker, kind=kind, turn_kind=kind, charter=charter,
                                segments=_view(mtg, _EXCHANGE_KINDS),
                                decision_point_id=point["id"], turn_round=round_no,
                                instruction=instructions[kind])
                    _mark(mtg, f"{mark}:{kind}")
                exchanges[point["index"]] += 1
        mtg.set_state(exchanges={**(mtg.state().get("exchanges") or {}), **exchanges})
        return advance()

    async def step_summary(self, mtg):
        round_no = mtg.meeting_round()
        facilitator = _facilitator(mtg)
        open_indexes = [point["index"] for point in _open_points(_points(mtg))]
        if not open_indexes:                          # 没有未决点（全部了结后"继续讨论"）：跳过
            return advance()
        found = store.find_turn(mtg.meeting_id, phase="summary", round=round_no,
                                kind="exchange_summary", speaker_id=facilitator["id"])
        data = found["data"] if found else (await speak(
            mtg, facilitator, kind="exchange_summary", turn_kind="exchange_summary",
            charter=_charter(mtg), segments=_view(mtg, _EXCHANGE_KINDS),
            instruction=SUMMARY_INSTRUCTION, structured=_validate_summaries(open_indexes),
            turn_round=round_no))["data"]
        index_of = {point["index"]: point["id"] for point in _points(mtg)}
        for row in data["summaries"]:
            if str(row["index"]) in index_of:
                store.update_decision_point(index_of[str(row["index"])],
                                            exchange_summary=str(row["summary"]).strip())
        mtg.reset_builder()                           # 交锋摘要写进了账本，下一次调用以新账本为基线
        return advance()

    async def _rule(self, mtg, indexes: list[str]) -> None:
        """请主持人为这些决策点裁决：写结论、理由，并记下保留的异议。"""
        facilitator = _facilitator(mtg)
        found = store.find_turn(mtg.meeting_id, phase=mtg.meeting()["phase"],
                                round=mtg.meeting_round(), kind="ruling",
                                speaker_id=facilitator["id"])
        data = found["data"] if found else (await speak(
            mtg, facilitator, kind="ruling", turn_kind="ruling", charter=_charter(mtg),
            segments=[render.ledger_segment(_points(mtg), round_no=mtg.meeting_round())],
            instruction=RULING_INSTRUCTION, structured=_validate_rulings(indexes),
            turn_round=mtg.meeting_round()))["data"]
        rulings = {str(row["index"]): row for row in data["rulings"]}
        for index in indexes:
            row = rulings.get(index)
            if row is None:
                continue
            rationale = str(row["rationale"]).strip()
            dissent = str(row.get("dissent") or "").strip()
            if dissent:
                rationale = f"{rationale}（保留的异议：{dissent}）"
            _close(mtg, index, "ruled", str(row["conclusion"]).strip(), rationale)
        await _ledger_updated(mtg)

    async def step_restance(self, mtg):
        members = mtg.participants("participant")
        round_no = mtg.meeting_round()
        params = mtg.params()
        # 闭合规则是"主持人裁决"：交锋次数已满、仍未一致的交给主持人裁决
        if str(params.get("closure_rule") or "unanimous") == "facilitator_rules":
            used_up = to_rule(_points(mtg), mtg.state().get("exchanges") or {},
                              int(params.get("exchanges_per_point") or 2))
            if used_up:
                await self._rule(mtg, used_up)
                mtg.reset_builder()                   # 裁决改写了账本，下一次调用以新账本为基线
        remaining = _open_points(_points(mtg))
        if not remaining:
            # 裁决已经把最后的未决点关闭：不再向模型要一次"对零个点重新表态"的回复
            mtg.set_state(consensus_reason="all_closed")
            await _ledger_updated(mtg)
            return advance("consensus")
        charter, view = _charter(mtg), _view(mtg, _ROUND_KINDS)
        results = await speak_many(mtg, [
            {"participant": member, "kind": "restance", "turn_kind": "restance",
             "charter": charter, "segments": view, "instruction": RESTANCE_INSTRUCTION,
             "structured": _validate_stances([point["index"] for point in remaining]),
             "turn_round": round_no} for member in members])
        for member, result in zip(members, results):
            _apply_stances(mtg, member, result["data"])
        outcome = _settle_round(mtg)
        remaining = _open_points(_points(mtg))
        await _ledger_updated(mtg)
        mtg.reset_builder()                           # 结算可能关闭了决策点
        if not remaining:
            mtg.set_state(consensus_reason="all_closed")
            return advance("consensus")
        stall = 0 if outcome["changed"] else int(mtg.state().get("stall") or 0) + 1
        mtg.set_state(stall=stall)
        if mtg.decider("consensus") == "human":       # 全程人审：每轮结束都由人判
            mtg.set_state(consensus_reason="round_end")
            return advance("consensus")
        if stall >= int(params.get("stall_rounds") or 2):
            mtg.set_state(consensus_reason="stall")
            return advance("consensus")
        if round_no >= int(mtg.state().get("rounds_limit") or _rounds_max(mtg)):
            mtg.set_state(consensus_reason="rounds_max")
            return advance("consensus")
        # 进入下一轮：账本快照会更新，这是预期内的变化，不该报冻结段被改
        store.update_meeting(mtg.meeting_id, round=round_no + 1)
        mtg.set_state(stance_snapshot=snapshot(remaining), exchange_done=[], exchanges={})
        mtg.reset_builder()
        return advance("exchange")

    # ---------- 轮次共识 ----------
    async def step_consensus(self, mtg):
        points = _accounted_points(mtg)
        open_points = _open_points(points)
        decider = mtg.decider("consensus")
        reason = str(mtg.state().get("consensus_reason")
                     or ("all_closed" if not open_points else "stall"))
        if not open_points and decider != "human":
            return advance()                          # 程序判定：全部了结，直接成稿
        if open_points and decider == "program_fallback":
            # 全自动模式下的退让：先按"无反对即可"了结，仍不行的记为待定
            outcome = relax(points, _voters(mtg))
            by_index = {point["index"]: point for point in points}
            for index in outcome["agreed"]:
                _close(mtg, index, "agreed", _conclusion_of(by_index[index]),
                       "全自动模式下按“无反对即可”了结")
            for index in outcome["deferred"]:
                _close(mtg, index, DEFERRED, "各方未达成一致，待用户决定")
            await record_auto_decision(
                mtg, "consensus", decided_by="program", title="轮次共识由程序判定",
                decision={"action": "approve", "option": "relax", **outcome},
                payload={"reason": reason})
            await _ledger_updated(mtg)
            mtg.reset_builder()
            return advance()
        decision = mtg.decision()
        if decision is None:
            return suspend(await mtg.open_checkpoint(
                "consensus", title=CONSENSUS_TITLES.get(reason, "轮次共识"),
                description=_consensus_description(mtg, open_points, reason),
                payload={"reason": reason, "round": mtg.meeting_round(),
                         "options": CONSENSUS_OPTIONS,
                         "open": [_open_brief(point) for point in open_points]}))
        data = decision["data"] or {}
        option = str(data.get("option") or ("integrate" if decision["action"] == "approve"
                                            else "continue"))
        if option == "continue" and not open_points:
            # 全部了结时没有可继续讨论的点：忽略"继续讨论"，直接成稿
            mtg.ack_decision(decision)
            mtg.set_state(consensus_reason=None)
            return advance()
        if option == "continue":
            # 再讨论一轮：指导语作为一条用户发言进入纪要，算在下一轮里
            following = mtg.meeting_round() + 1
            store.add_turn(mtg.meeting_id, round=following, phase="consensus", speaker_id=None,
                           speaker_name="用户", kind="user_guidance",
                           content=decision.get("feedback") or "")
            store.update_meeting(mtg.meeting_id, round=following)
            mtg.ack_decision(decision)
            mtg.set_state(consensus_reason=None, stall=0, exchange_done=[], exchanges={},
                          stance_snapshot=snapshot(open_points),
                          rounds_limit=max(_rounds_max(mtg), following))
            mtg.reset_builder()
            return advance("exchange")
        if option == "delegate" and open_points:
            await self._rule(mtg, [point["index"] for point in open_points])
            mtg.reset_builder()
        else:
            for point in open_points:                 # 带着分歧整合：未了结的记为待定
                _close(mtg, point["index"], DEFERRED, "各方未达成一致，待用户决定")
            await _ledger_updated(mtg)
            mtg.reset_builder()
        mtg.ack_decision(decision)
        mtg.set_state(consensus_reason=None)
        return advance()

    # ---------- 复议 ----------
    async def step_reopen(self, mtg):
        state = mtg.state()
        feedback = str(state.get("reopen_feedback") or "").strip()
        if not feedback:
            return advance()
        facilitator = _facilitator(mtg)
        round_no = mtg.meeting_round()
        following = round_no + 1
        speaker = "评审意见" if state.get("feedback_kind") == "reroute" else "用户"
        # 断点保护：reopen 的回复已经落盘就复用；反馈发言也按轮次与内容去重
        found = store.find_turn(mtg.meeting_id, phase="reopen", round=round_no,
                                kind="reopen_plan", speaker_id=facilitator["id"])
        if found is None:
            existing = {turn["content"] for turn in store.round_turns(
                mtg.meeting_id, round=following, kinds=["user_feedback"])}
            if feedback not in existing:
                store.add_turn(mtg.meeting_id, round=following, phase="reopen",
                               speaker_id=None, speaker_name=speaker,
                               kind="user_feedback", content=feedback)
            data = (await speak(
                mtg, facilitator, kind="reopen", turn_kind="reopen_plan",
                charter=_charter(mtg),
                segments=[render.ledger_segment(_points(mtg), round_no=round_no),
                          Segment("append", f"## 需要处理的反馈\n{feedback}", name="反馈")],
                instruction=REOPEN_INSTRUCTION, structured=_validate_reopen,
                turn_round=round_no))["data"]
        else:
            data = found["data"]
        by_index = {point["index"]: point for point in _points(mtg)}
        number = len(by_index) + 1
        reopened = dict(state.get("reopen_rounds") or {})
        for row in data["points"]:
            index = str(row.get("index") or "").strip()
            if index in by_index:                     # 已有的决策点：置为复议中
                store.update_decision_point(by_index[index]["id"], status="reopened",
                                            conclusion="", closed_round=None)
                reopened[index] = following
            elif index:
                continue                              # 账本里没有的编号：不计入
            else:                                     # 反馈带出了账本里没有的问题：新建
                store.upsert_decision_point(
                    mtg.meeting_id, f"D{number}", title=str(row.get("title") or "").strip(),
                    question=str(row.get("question") or ""),
                    options_json=list(row.get("options") or []),
                    raised_by=speaker, origin_round=following)
                number += 1
        store.update_meeting(mtg.meeting_id, round=following)
        # 反馈处理完之后才清空标记：模型调用或落盘失败重试时，这条反馈还在
        mtg.set_state(stance_snapshot={}, exchange_done=[], exchanges={}, stall=0,
                      consensus_reason=None, rounds_limit=None, reopen_feedback="",
                      reopen_rounds=reopened)
        mtg.reset_builder()
        await _ledger_updated(mtg)
        return advance("stances")

    # ---------- 成稿与复核 ----------
    async def step_draft(self, mtg):
        previous = _artifact_body(mtg.run_id, "proposal")
        segments = [render.ledger_segment(_points(mtg), round_no=mtg.meeting_round()),
                    *_positions(mtg)]
        if previous:
            segments.append(Segment("append", f"## 上一版建设方案\n{previous}", name="上一版方案"))
        result = await speak(
            mtg, _facilitator(mtg), kind="proposal_revise" if previous else "proposal",
            turn_kind="proposal", charter=_charter(mtg),
            segments=segments, turn_round=mtg.meeting_round(),
            instruction=REVISION_INSTRUCTION if previous else PROPOSAL_INSTRUCTION)
        self._save_proposal(mtg, strip_outer_fence(result["content"]),
                            revision=bool(previous))
        return advance()

    def _save_proposal(self, mtg, draft: str, notes: str = "",
                       revision: bool = False) -> dict:
        """先校验模型写的正文，再由程序在末尾附上决策点索引与待确认问题。

        程序追加的两节会让"正文够长"变得廉价，所以正文必须自己具备六个必需章节；
        修订版还必须写明修改说明。
        """
        draft = draft.strip()
        issues = [] if len(draft) >= 100 else ["正文过短，请给出完整的建设方案"]
        missing = [title for title in PROPOSAL_SECTIONS if title not in draft]
        if missing:
            issues.append("方案缺少这些章节：" + "、".join(missing))
        if revision and "## 修改说明" not in draft:
            issues.append("修订版缺少「## 修改说明」一节")
        if issues:
            raise NodeFailure("建设方案正文校验未通过：" + "；".join(issues))
        points = _points(mtg)
        body = proposal.compose(draft, points=points,
                                clarifications=mtg.state().get("clarifications") or [])
        issues = validate_artifact("proposal", body)
        if issues:
            raise NodeFailure("建设方案校验未通过：" + "；".join(issues))
        record = write_artifact(mtg.run_id, "proposal", body, created_by="facilitator",
                                inputs=["idea", "discussion"], status="draft")
        self._write_minutes(mtg, notes)
        mtg.set_state(proposal_version=record["version"])
        return record

    def _write_minutes(self, mtg, notes: str = "") -> dict:
        extra = [("复核遗留意见", notes)] if notes else []
        return minutes.write_minutes(
            mtg.run_id, kind="discussion", created_by="facilitator", inputs=["idea"],
            meeting=mtg.meeting(), participants=mtg.participants(), points=_points(mtg),
            turns=store.list_turns(mtg.meeting_id, limit=100_000), extra_sections=extra)

    async def step_check(self, mtg):
        state = mtg.state()
        check_round = int(state.get("check_round") or 1)
        limit = max(1, int(mtg.params().get("proposal_check_rounds") or 2))
        wanted = set(state.get("check_pending") or [])
        members = [member for member in mtg.participants("participant")
                   if not wanted or member["id"] in wanted]
        view = [render.ledger_segment(_points(mtg), round_no=mtg.meeting_round()),
                Segment("append", "## 建设方案（待复核）\n"
                        + _artifact_body(mtg.run_id, "proposal"), name="建设方案")]
        results = await speak_many(mtg, [
            {"participant": member, "kind": "proposal_check", "turn_kind": "proposal_check",
             "charter": _charter(mtg), "segments": view, "instruction": CHECK_INSTRUCTION,
             "structured": _validate_check, "turn_round": check_round} for member in members])
        flagged = [(member, result["data"]) for member, result in zip(members, results)
                   if result["data"]["ok"] is False]
        if not flagged:
            return self._finish(mtg)
        notes = "\n".join(
            f"- {member['display_name']}（{issue.get('index') or '未标注决策点'}）："
            f"{issue.get('problem') or ''}"
            for member, data in flagged for issue in data.get("issues") or [])
        if check_round >= limit:                      # 复核轮次用完：遗留意见记进纪要，交给方案确认
            self._write_minutes(mtg, notes)
            mtg.set_state(review_notes=notes, check_pending=[])
            return self._finish(mtg)
        result = await speak(
            mtg, _facilitator(mtg), kind="proposal_revise", turn_kind="proposal_revise",
            charter=_charter(mtg), turn_round=check_round,
            segments=view + [Segment("append", f"## 复核意见\n{notes}", name="复核意见")],
            instruction=REVISE_INSTRUCTION)
        self._save_proposal(mtg, strip_outer_fence(result["content"]), revision=True)
        mtg.set_state(check_round=check_round + 1,
                      check_pending=[member["id"] for member, _data in flagged])
        return advance("check")                      # 只让提出意见的人再复核一遍

    def _finish(self, mtg):
        points = _points(mtg)
        deferred = [point for point in points if point["status"] == DEFERRED]
        summary = (f"讨论会议完成：{len(points)} 个决策点，{len(deferred)} 个待确认，"
                   f"共 {mtg.meeting_round()} 轮")
        return done(summary, artifact=latest_artifact(mtg.run_id, "proposal"),
                    minutes=latest_artifact(mtg.run_id, "discussion"),
                    decision_points=len(points), deferred=len(deferred),
                    rounds=mtg.meeting_round())
