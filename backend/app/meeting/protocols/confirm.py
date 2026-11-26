"""需求确认协议（详设 §9）。

步骤依次是：
  intake    受理：用户在需求确认确认点上驳回时，把意见整理成核对清单里的条目
  prepare   准备：建参会者、跑机械核对、把当前需求正文存进状态、写章程
  expect    写预期：核对者只读账本，逐个已了结的决策点写下"需求里必须出现什么"
  fidelity  忠实性核对：对照需求规格，逐个决策点给出忠实、走样或遗漏
  quality   质量评审：按需求质量特性逐项检查，问题定位到需求编号
  merge     汇总：程序把三处发现整理成清单，主席补全走样与遗漏落在哪条需求上
  verdict   裁决：程序按清单得出结论；不通过时定点回流需求分析师
  report    写确认报告（评审工件的新版本）

引擎掌控流程：机械核对不过就不调模型、清单由程序生成、结论由程序得出；
模型只负责写预期、核对、评审与归类。全部开新上下文，依据是决策点账本。
"""
from __future__ import annotations

from typing import Callable

from app.artifacts.store import latest_artifact, parse_front_matter, read_artifact
from app.conversation.builder import Segment
from app.engine.events import emit
from app.engine.runners import NodeFailure
from app.engine.templates import role_library, role_template
from app.meeting import confirm_rules, minutes, render, review_rules, store
from app.meeting.calls import speak, speak_many
from app.meeting.protocols.base import (MeetingProtocol, advance, done, register_protocol,
                                        reroute_or_escalate)

Validator = Callable[[dict], list[str]]

MODE_NAMES = {"chair": "评审主席", "checker": "语义核对者", "quality_reviewer": "质量评审者"}
KIND_NAMES = {"mechanical": "机械核对", "distorted": "走样", "missing": "遗漏",
              "added": "凭空新增", "quality": "质量评审"}
RESULT_NAMES = {"faithful": "忠实", "distorted": "走样", "missing": "遗漏"}
_FAIL_KINDS = ("mechanical", "distorted", "missing", "added")
_QUALITY_CRITERIA = ("正确", "无歧义", "完整", "一致", "有优先级", "可验证", "可修改", "可追踪")
_RANK = {"blocker": 0, "major": 1, "minor": 2}

EXPECT_INSTRUCTION = (
    "只依据决策点账本，逐个已了结的决策点写下：需求里必须出现什么内容，才算忠实于这个决策。"
    "不要评价现状，也不要引用别的内容。只输出一个 JSON 对象：\n"
    '{"expected": [{"index": "D1", "content": "需求里必须出现什么"}]}')
FIDELITY_INSTRUCTION = (
    "对照章程里的需求规格，逐个决策点判断需求是否忠实体现了账本里的结论："
    "faithful（忠实）、distorted（走样）、missing（遗漏）。走样和遗漏必须写 reason。"
    "如果某条需求的内容在账本里找不到依据，放进 added。只输出一个 JSON 对象：\n"
    '{"checks": [{"index": "D1", "result": "faithful", "reason": ""}], '
    '"added": [{"requirement": "R7", "reason": "为什么没有依据"}]}')
QUALITY_INSTRUCTION = (
    "按需求质量特性逐项检查需求规格：" + "、".join(_QUALITY_CRITERIA)
    + "；另外检查是否越界写了实现细节。每条意见定位到需求编号。只输出一个 JSON 对象：\n"
    '{"issues": [{"requirement": "R1", "criterion": "正确", "severity": "major", '
    '"problem": "……", "suggestion": "……"}]}\n'
    "没有问题的时候 issues 给空数组。")
MERGE_INSTRUCTION = (
    "请把下面这些核对条目归类去重，并给走样与遗漏的条目补上它应当落在哪条需求上。"
    "只输出一个 JSON 对象：\n"
    '{"items": [{"kind": "distorted", "index": "D2", "requirement": "R3", '
    '"problem": "……", "suggestion": "……"}]}')
INTAKE_INSTRUCTION = (
    "用户对需求确认结论提出了意见。请把它整理成核对清单里的条目：kind 取 quality，"
    "写明对应的需求编号、问题与修改建议。只输出一个 JSON 对象：\n"
    '{"items": [{"kind": "quality", "requirement": "R1", "problem": "……", '
    '"suggestion": "……"}]}')


# ---------------- 校验模型的结构化输出 ----------------
def _validate_expected(indexes: list[str]) -> Validator:
    def validate(data: dict) -> list[str]:
        rows = data.get("expected")
        if not isinstance(rows, list):
            return ["expected 须是数组"]
        if any(not isinstance(row, dict) for row in rows):
            return ["expected 的每一项须是对象"]
        if {str(row.get("index")) for row in rows} != set(indexes):
            return [f"expected 须恰好覆盖这些决策点：{indexes}"]
        return [f"{row.get('index')}：content 不能为空" for row in rows
                if not isinstance(row.get("content"), str) or not row["content"].strip()]

    return validate


def _validate_fidelity(indexes: list[str]) -> Validator:
    def validate(data: dict) -> list[str]:
        rows = data.get("checks")
        if not isinstance(rows, list):
            return ["checks 须是数组"]
        if any(not isinstance(row, dict) for row in rows):
            return ["checks 的每一项须是对象"]
        if {str(row.get("index")) for row in rows} != set(indexes):
            return [f"checks 须恰好覆盖这些决策点：{indexes}"]
        issues: list[str] = []
        for row in rows:
            result = row.get("result")
            if result not in RESULT_NAMES:
                issues.append(f"{row.get('index')}：result 须是 faithful、distorted、missing 之一")
            elif result != "faithful" and not str(row.get("reason") or "").strip():
                issues.append(f"{row.get('index')}：走样和遗漏必须写 reason")
        added = data.get("added")
        if added is not None:
            if not isinstance(added, list):
                issues.append("added 须是数组")
            else:
                for item in added:
                    if (not isinstance(item, dict)
                            or not str(item.get("requirement") or "").strip()
                            or not str(item.get("reason") or "").strip()):
                        issues.append("added 的每一项须有 requirement 与 reason")
        return issues

    return validate


def _validate_quality(data: dict) -> list[str]:
    rows = data.get("issues")
    if not isinstance(rows, list):
        return ["issues 须是数组"]
    return [f"第 {position + 1} 条意见缺少 requirement 或 problem"
            for position, row in enumerate(rows)
            if not isinstance(row, dict)
            or not str(row.get("requirement") or "").strip()
            or not str(row.get("problem") or "").strip()]


def _validate_items(*, allow_empty: bool) -> Validator:
    def validate(data: dict) -> list[str]:
        rows = data.get("items")
        if not isinstance(rows, list):
            return ["items 须是数组"]
        if not allow_empty and not rows:
            return ["items 须是非空数组"]
        return [f"第 {position + 1} 条缺少 problem"
                for position, row in enumerate(rows)
                if not isinstance(row, dict)
                or not str(row.get("problem") or "").strip()]

    return validate


# ---------------- 会议状态与账本 ----------------
def _flow(mtg) -> int:
    """确认轮次：第一次确认、每次带着反馈续开或从等待返工恢复都加一。"""
    return mtg.meeting_round()


def _done(mtg, step: str) -> bool:
    return (mtg.state().get("done") or {}).get(step) == _flow(mtg)


def _mark(mtg, step: str) -> None:
    done_map = dict(mtg.state().get("done") or {})
    done_map[step] = _flow(mtg)
    mtg.set_state(done=done_map)


def _finish_step(mtg, step: str, **changes) -> None:
    """产物与完成标记一次写完：崩溃重试不会把同一批条目写两遍。"""
    done_map = dict(mtg.state().get("done") or {})
    done_map[step] = _flow(mtg)
    mtg.set_state(done=done_map, **changes)


def _items(mtg) -> list[dict]:
    return list(mtg.state().get("items") or [])


def _chair(mtg) -> dict:
    rows = mtg.participants("chair")
    if not rows:
        raise NodeFailure("需求确认会议缺少评审主席，无法继续", fatal=True)
    return rows[0]


def _checkers(mtg) -> list[dict]:
    return mtg.participants("checker")


def _quality_reviewers(mtg) -> list[dict]:
    return mtg.participants("quality_reviewer")


def _discussion(mtg) -> dict | None:
    return store.latest_meeting(mtg.run_id, "discussion")


def _review_meeting(mtg) -> dict | None:
    return store.latest_meeting(mtg.run_id, "review")


def _ledger_points(mtg) -> list[dict]:
    meeting = _discussion(mtg)
    return store.list_decision_points(meeting["id"]) if meeting else []


def _spec_text(mtg) -> str:
    raw = read_artifact(mtg.run_id, "requirement")
    return parse_front_matter(raw)[1].strip() if raw else ""


def _spec_bodies(text: str) -> dict:
    return {item["id"]: item["body"] for item in confirm_rules.parse_requirements(text)}


def _closed_indexes(points: list[dict]) -> list[str]:
    return [point["index"] for point in confirm_rules.closed_points(points)]


def _indexes_line(verb: str, indexes: list[str]) -> str:
    return f"\n\n必须{verb}这些决策点：" + "、".join(indexes) + "。"


def _where(item: dict) -> str:
    parts = []
    if item.get("requirement"):
        parts.append(f"需求 {item['requirement']}")
    if item.get("index"):
        parts.append(f"决策点 {item['index']}")
    return "、".join(parts) or "—"


def _cell(value) -> str:
    text = str(value or "").replace("|", "/").replace("\n", " ").strip()
    return text or "—"


def _next_number(items: list[dict], last_items: list[dict]) -> int:
    numbers = []
    for item in list(items) + list(last_items):
        value = str(item.get("id") or "")
        if value.startswith("C") and value[1:].isdigit():
            numbers.append(int(value[1:]))
    return max(numbers, default=0) + 1


def _items_text(items: list[dict]) -> str:
    lines = []
    for item in items:
        label = KIND_NAMES.get(item.get("kind"), item.get("kind") or "")
        location = _where(item)
        line = f"- {item.get('id', '')} [{label}] "
        line += f"{location}：" if location != "—" else ""
        line += item.get("problem") or ""
        if item.get("suggestion"):
            line += f"。建议：{item['suggestion']}"
        lines.append(line)
    return "\n".join(lines)


def _blocking(items: list[dict], line: str) -> list[dict]:
    limit = _RANK.get(line, _RANK["major"])
    return [item for item in items
            if item.get("kind") in _FAIL_KINDS
            or (item.get("kind") == "quality"
                and _RANK.get(item.get("severity") or "minor", _RANK["minor"]) <= limit)]


# ---------------- 章程 ----------------
def _last_items_text(mtg) -> str:
    state = mtg.state()
    last = list(state.get("last_items") or [])
    if not last:
        return "（无）"
    changed = set(state.get("changed") or [])
    lines = []
    for item in last:
        requirement = str(item.get("requirement") or "")
        if requirement:
            handled = "已改动" if requirement in changed else "未改动"
        elif item.get("kind") == "mechanical":
            handled = "按当前规格重新核对"
        else:
            handled = "本轮重新核对"
        label = KIND_NAMES.get(item.get("kind"), item.get("kind") or "")
        location = _where(item)
        line = f"- {item.get('id', '')} [{label}] "
        line += f"{location}：" if location != "—" else ""
        line += f"{item.get('problem') or ''}（处理情况：{handled}）"
        lines.append(line)
    return "\n".join(lines)


def _charter_dict(mtg, *, include_spec: bool = True) -> dict:
    state = mtg.state()
    sections: list[dict] = []
    if include_spec:                                  # 写预期时只看账本
        sections.append({"title": "需求规格（当前版本）", "text": _spec_text(mtg)})
    sections.append({
        "title": "决策点账本",
        "text": render.ledger_text(_ledger_points(mtg), round_no=_flow(mtg),
                                   budget=10 ** 9).rstrip()})
    if _flow(mtg) > 1:
        sections.append({"title": "上一轮的核对清单与处理情况", "text": _last_items_text(mtg)})
        sections.append({
            "title": "改动过的需求",
            "text": "\n".join(f"- {requirement}"
                              for requirement in state.get("changed") or []) or "（无）"})
    sections.append({
        "title": "参与者",
        "text": "\n".join(f"- {row['display_name']}（{MODE_NAMES.get(row['mode'], row['mode'])}）"
                          for row in mtg.participants()) or "（无）"})
    materials, _truncated = mtg.materials()
    return {"title": "需求确认会议章程", "sections": sections, "materials": materials}


def _charter(mtg, *, include_spec: bool = True) -> list[Segment]:
    return render.charter_segments(_charter_dict(mtg, include_spec=include_spec))


# ---------------- 程序生成的清单 ----------------
def _dedupe(rows: list[dict]) -> list[dict]:
    """同一处的发现合并：走样与遗漏按决策点，凭空新增按需求，质量意见按需求与问题。"""
    merged: dict[tuple, dict] = {}
    order: list[tuple] = []
    for row in rows:
        kind = row["kind"]
        if kind in ("distorted", "missing"):
            key = (kind, row.get("index") or "")
        elif kind == "added":
            key = (kind, row.get("requirement") or "")
        elif kind == "quality":
            key = (kind, row.get("requirement") or "", row.get("problem") or "")
        else:
            key = (kind, row.get("problem") or "")
        if key in merged:
            existing = merged[key]
            problem = row.get("problem") or ""
            if problem and problem not in existing["problem"]:
                existing["problem"] = (existing["problem"] + "；" + problem
                                       if existing["problem"] else problem)
            if not existing.get("suggestion") and row.get("suggestion"):
                existing["suggestion"] = row["suggestion"]
            continue
        merged[key] = dict(row)
        order.append(key)
    return [merged[key] for key in order]


def _program_items(mtg) -> list[dict]:
    """清单里的程序部分：机械核对、走样与遗漏、凭空新增、达到阻塞线的质量意见。"""
    state = mtg.state()
    line = str(mtg.params().get("min_blocking_severity") or "major")
    limit = _RANK.get(line, _RANK["major"])
    rows: list[dict] = []
    for finding in state.get("mechanical") or []:
        rows.append({"kind": "mechanical", "requirement": "", "index": "",
                     "severity": "blocker", "problem": str(finding), "suggestion": ""})
    for row in state.get("fidelity") or []:
        if row.get("result") in ("distorted", "missing"):
            rows.append({"kind": row["result"], "requirement": "",
                         "index": str(row.get("index") or ""), "severity": "blocker",
                         "problem": str(row.get("reason") or ""), "suggestion": ""})
    for row in state.get("added") or []:
        rows.append({"kind": "added", "requirement": str(row.get("requirement") or ""),
                     "index": "", "severity": "blocker",
                     "problem": str(row.get("reason") or ""), "suggestion": ""})
    for row in state.get("quality_issues") or []:
        severity = row.get("severity") or "minor"
        if _RANK.get(severity, _RANK["minor"]) > limit:
            continue
        rows.append({"kind": "quality", "requirement": row.get("location") or "",
                     "index": "", "severity": severity,
                     "problem": row.get("problem") or "",
                     "suggestion": row.get("suggestion") or ""})
    return _dedupe(rows)


# ---------------- 协议 ----------------
@register_protocol
class ConfirmProtocol(MeetingProtocol):
    name = "confirm"
    steps = ("intake", "prepare", "expect", "fidelity", "quality", "merge", "verdict",
             "report")

    async def on_enter(self, mtg) -> None:
        state = mtg.state()
        mtg.consume_feedback()
        state = mtg.state()
        text = str(state.get("reopen_feedback") or "").strip()
        if text and not state.get("intake_feedback"):
            flow = _flow(mtg) + 1
            speaker = "评审意见" if state.get("feedback_kind") == "reroute" else "用户"
            store.add_turn(mtg.meeting_id, round=flow, phase="intake", speaker_id=None,
                           speaker_name=speaker, kind="user_feedback", content=text)
            # 轮次、反馈的移交与上一轮清单一次写完：中途崩溃不会重复加轮
            store.update_meeting(mtg.meeting_id, round=flow, phase="intake", state_json={
                **state, "reopen_feedback": "", "intake_feedback": text,
                "last_items": list(state.get("items") or []),
                "verdict": None, "accepted_by_user": False, "awaiting_rework": False})
            mtg.reset_builder()
            return
        if state.get("rework_pending"):
            flow = _flow(mtg) + 1
            store.update_meeting(mtg.meeting_id, round=flow, phase="prepare", state_json={
                **state, "rework_pending": False, "verdict": None,
                "accepted_by_user": False, "awaiting_rework": False})
            mtg.reset_builder()

    # ---------- 受理 ----------
    async def step_intake(self, mtg):
        if _done(mtg, "intake"):
            return advance("prepare")
        text = str(mtg.state().get("intake_feedback") or "").strip()
        if not text:
            _mark(mtg, "intake")
            return advance("prepare")
        if not mtg.participants("chair"):             # 还没建过参会者：先补准备
            _mark(mtg, "intake")
            return advance("prepare")
        chair = _chair(mtg)
        found = store.find_turn(mtg.meeting_id, phase="intake", round=_flow(mtg),
                                kind="confirm_intake", speaker_id=chair["id"])
        data = found["data"] if found else (await speak(
            mtg, chair, kind="confirm_intake", turn_kind="confirm_intake",
            charter=_charter(mtg),
            segments=[Segment("append", f"## 用户的反馈\n{text}", name="反馈")],
            instruction=INTAKE_INSTRUCTION, structured=_validate_items(allow_empty=False),
            turn_round=_flow(mtg)))["data"]
        items = _items(mtg)
        number = _next_number(items, list(mtg.state().get("last_items") or []))
        for raw in data["items"]:
            kind = str(raw.get("kind") or "quality")
            if kind not in KIND_NAMES:
                kind = "quality"
            severity = raw.get("severity") if raw.get("severity") in _RANK else "blocker"
            items.append({
                "id": f"C{number}", "kind": kind,
                "requirement": str(raw.get("requirement") or "").strip(),
                "index": str(raw.get("index") or "").strip(),
                "severity": severity if kind == "quality" else "blocker",
                "problem": str(raw.get("problem") or "").strip(),
                "suggestion": str(raw.get("suggestion") or "").strip(),
                "origin": "user", "round": _flow(mtg)})
            number += 1
        _finish_step(mtg, "intake", items=items, intake_feedback="")
        return advance("prepare")

    # ---------- 准备 ----------
    async def step_prepare(self, mtg):
        if _done(mtg, "prepare"):
            state = mtg.state()
            return advance("merge" if state.get("mechanical") else "expect")
        if not read_artifact(mtg.run_id, "requirement"):
            raise NodeFailure("需求确认会议需要需求规格工件，但规格还没有产出", fatal=True)
        self._ensure_participants(mtg)
        state = mtg.state()
        flow = _flow(mtg)
        text = _spec_text(mtg)
        points = _ledger_points(mtg)
        bodies = _spec_bodies(text)
        changes: dict = {
            "mechanical": confirm_rules.mechanical_check(text, points),
            "spec": bodies,
            # 每一轮的三处核对结果全量重来，上一轮的不带进本轮
            "fidelity": [], "added": [], "quality_issues": [],
        }
        if flow > 1:
            previous = dict(state.get("spec") or {})
            changes["changed"] = confirm_rules.changed_requirements(previous, bodies)
            changes["last_spec"] = previous
        mtg.set_state(**changes)
        store.update_meeting(mtg.meeting_id, charter_json=_charter_dict(mtg))
        mtg.reset_builder()
        await emit(mtg.run_id, "review_round_started", mtg.node_id, {
            "meeting_id": mtg.meeting_id, "round": flow, "protocol": "confirm",
            "incremental": flow > 1, "rounds_max": mtg.max_rounds()})
        _mark(mtg, "prepare")
        return advance("merge" if changes["mechanical"] else "expect")

    def _ensure_participants(self, mtg) -> None:
        existing = {(row["mode"], row["role_key"], row["display_name"])
                    for row in mtg.participants()}

        def add(row: dict, mode: str) -> None:
            key = (mode, row.get("role_key"), row["display_name"])
            if key in existing:
                return
            store.add_participant(
                mtg.meeting_id, role_key=row.get("role_key"),
                display_name=row["display_name"], mode=mode,
                system_prompt=row.get("system_prompt") or "",
                model_id=row.get("model_id"), source="template")
            existing.add(key)

        if not mtg.participants("chair"):
            template = role_template("review_chair")
            if template is None:
                raise NodeFailure("角色库里没有评审主席（review_chair）", fatal=True)
            add({"role_key": "review_chair", "display_name": template["name"],
                 "system_prompt": template["system_prompt"], "model_id": None}, "chair")

        discussion = _discussion(mtg)
        if discussion is None:
            raise NodeFailure("需求确认会议缺少讨论会议，无法取得核对者与账本", fatal=True)
        base = store.list_participants(discussion["id"])
        for row in base:
            if row["mode"] in ("participant", "proxy"):
                add(row, "checker")
        if not mtg.participants("checker"):
            raise NodeFailure("讨论会议没有可用的核对者，无法进行需求确认", fatal=True)
        for row in self._quality_sources(mtg, base):
            add(row, "quality_reviewer")
        if not mtg.participants("quality_reviewer"):
            raise NodeFailure("需求确认会议缺少质量评审者，无法继续", fatal=True)

    def _quality_sources(self, mtg, base: list[dict]) -> list[dict]:
        """质量评审者取评审会议的评审者各行；没有评审会议时按评审协议的名单规则取。"""
        review = _review_meeting(mtg)
        if review is not None:
            reviewers = [row for row in store.list_participants(review["id"])
                         if row["mode"] == "reviewer"]
            if reviewers:
                return reviewers
        library = {row["role_key"]: row for row in role_library()}
        spec = (mtg.run_config().get("rosters") or {}).get("reviewers") or {}
        configured = [str(key) for key in (spec.get("role_keys") or [])]
        count = int(spec.get("count") or 0)
        if configured:
            missing = [key for key in configured if key not in library]
            if missing:
                raise NodeFailure("运行级配置点名的评审角色已不存在：" + "、".join(missing),
                                  fatal=True)
            return [{"role_key": key, "display_name": library[key]["name"],
                     "system_prompt": library[key]["system_prompt"], "model_id": None}
                    for key in configured]
        reviewers = [row for row in base if row["mode"] == "participant"]
        if reviewers:
            return reviewers[:count] if count else reviewers
        suitable = [row for row in library.values()
                    if "review" in ((row.get("meta") or {}).get("meeting_roles") or [])]
        picked = suitable[:count] if count else suitable[:3]
        return [{"role_key": row["role_key"], "display_name": row["name"],
                 "system_prompt": row["system_prompt"], "model_id": None}
                for row in picked]

    # ---------- 写预期 ----------
    async def step_expect(self, mtg):
        if _done(mtg, "expect"):
            return advance("fidelity")
        state = mtg.state()
        if state.get("mechanical"):
            return advance("merge")                   # 机械核对不过：不调模型
        if mtg.state().get("expected"):               # 写下的预期各轮沿用，不再重写
            _mark(mtg, "expect")
            return advance("fidelity")
        if not bool(mtg.params().get("expected_first", True)):
            _mark(mtg, "expect")
            return advance("fidelity")
        indexes = _closed_indexes(_ledger_points(mtg))
        if not indexes:
            _mark(mtg, "expect")
            return advance("fidelity")
        charter = _charter(mtg, include_spec=False)     # 只读账本，不看需求规格
        checkers = _checkers(mtg)
        results = await speak_many(mtg, [
            {"participant": checker, "kind": "expect", "turn_kind": "expect",
             "charter": charter, "segments": [],
             "instruction": EXPECT_INSTRUCTION + _indexes_line("覆盖", indexes),
             "structured": _validate_expected(indexes), "turn_round": _flow(mtg)}
            for checker in checkers])
        expected = dict(state.get("expected") or {})
        for checker, result in zip(checkers, results):
            expected[str(checker["id"])] = list(result["data"]["expected"])
        _finish_step(mtg, "expect", expected=expected)
        return advance("fidelity")

    # ---------- 忠实性核对与质量评审 ----------
    async def step_fidelity(self, mtg):
        if _done(mtg, "fidelity"):
            return advance("quality")
        if mtg.state().get("mechanical"):
            return advance("merge")
        points = _ledger_points(mtg)
        coverage = str(mtg.params().get("coverage") or "full")
        checkers = _checkers(mtg)
        assignments = self._assignments(mtg, checkers, points, coverage)
        if _flow(mtg) > 1:
            assignments = self._incremental_assignments(mtg, assignments)
        active = [checker for checker in checkers if assignments.get(checker["id"])]
        if not active:
            _finish_step(mtg, "fidelity", fidelity=[], added=[])
            return advance("quality")
        expected = dict(mtg.state().get("expected") or {})
        charter = _charter(mtg)
        calls = []
        for checker in active:
            indexes = assignments[checker["id"]]
            own = [row for row in expected.get(str(checker["id"]), [])
                   if str(row.get("index")) in set(indexes)]
            segments = []
            if own:
                text = "\n".join(f"- {row['index']}：{row['content']}" for row in own)
                segments.append(Segment("append", "## 你写下的预期\n" + text, name="预期"))
            calls.append({
                "participant": checker, "kind": "fidelity", "turn_kind": "fidelity",
                "charter": charter, "segments": segments,
                "instruction": FIDELITY_INSTRUCTION + _indexes_line("核对", indexes),
                "structured": _validate_fidelity(indexes), "turn_round": _flow(mtg)})
        results = await speak_many(mtg, calls)
        rows: list[dict] = []
        added: list[dict] = []
        for checker, result in zip(active, results):
            data = result["data"]
            for item in data.get("checks") or []:
                rows.append({"index": str(item.get("index")), "result": item.get("result"),
                             "reason": str(item.get("reason") or ""),
                             "checker": checker["display_name"]})
            for item in data.get("added") or []:
                added.append({"requirement": str(item.get("requirement")),
                              "reason": str(item.get("reason")),
                              "checker": checker["display_name"]})
        _finish_step(mtg, "fidelity", fidelity=rows, added=added)
        return advance("quality")
    def _assignments(self, mtg, checkers: list[dict], points: list[dict],
                     coverage: str) -> dict[int, list[str]]:
        """每个核对者本轮要核对的决策点。

        full 是全部已了结的决策点；partitioned 是它在讨论会议的账本里表过态的那些，
        需求方代理仍然核对全部（确认会议的参会者是讨论会议那几行的副本，按角色键对应回去）。
        """
        closed = _closed_indexes(points)
        if coverage != "partitioned":
            return {checker["id"]: list(closed) for checker in checkers}
        discussion = _discussion(mtg)
        base = store.list_participants(discussion["id"]) if discussion else []
        assignments: dict[int, list[str]] = {}
        for checker in checkers:
            source = next((row for row in base
                           if row["role_key"] == checker["role_key"]
                           and row["display_name"] == checker["display_name"]), None)
            if source is None:
                # 对应不回去（临时角色等）：按全员核对处理，宁可多查也不漏查
                assignments[checker["id"]] = list(closed)
                continue
            if source["mode"] == "proxy":
                assignments[checker["id"]] = list(closed)
                continue
            assignments[checker["id"]] = [
                point["index"] for point in points
                if point["index"] in closed
                and str(source["id"]) in (point.get("stances") or {})]
        return assignments

    def _incremental_assignments(self, mtg,
                                 assignments: dict[int, list[str]]) -> dict[int, list[str]]:
        """第 2 轮起只看上一轮标为走样或遗漏的决策点，以及改动过的需求所引用的决策点。"""
        state = mtg.state()
        targets: set[str] = set()
        for item in state.get("last_items") or []:
            if item.get("kind") in ("distorted", "missing") and item.get("index"):
                targets.add(str(item["index"]))
        changed = set(state.get("changed") or [])
        if changed:
            for row in confirm_rules.parse_matrix(_spec_text(mtg)):
                if row["requirement"] in changed:
                    targets |= set(row["points"])
        return {pid: [index for index in indexes if index in targets]
                for pid, indexes in assignments.items()}

    async def step_quality(self, mtg):
        if _done(mtg, "quality"):
            return advance("merge")
        state = mtg.state()
        if state.get("mechanical"):
            return advance("merge")
        requirement_ids = [item["id"]
                           for item in confirm_rules.parse_requirements(_spec_text(mtg))]
        allowed = requirement_ids
        if _flow(mtg) > 1:
            marked = {item.get("requirement") for item in state.get("last_items") or []
                      if item.get("kind") == "quality" and item.get("requirement")}
            wanted = marked | set(state.get("changed") or [])
            allowed = [requirement for requirement in requirement_ids if requirement in wanted]
            if not allowed:
                _finish_step(mtg, "quality", quality_issues=[])
                return advance("merge")
        reviewers = _quality_reviewers(mtg)
        instruction = QUALITY_INSTRUCTION
        if _flow(mtg) > 1:
            instruction += "\n本轮只看这些需求：" + "、".join(allowed) + "。"
        results = await speak_many(mtg, [
            {"participant": reviewer, "kind": "quality", "turn_kind": "quality",
             "charter": _charter(mtg), "segments": [], "instruction": instruction,
             "structured": _validate_quality, "turn_round": _flow(mtg)}
            for reviewer in reviewers])
        issues: list[dict] = []
        seen: set[tuple] = set()
        for result in results:
            for raw in result["data"].get("issues") or []:
                requirement = str(raw.get("requirement") or "").strip()
                if requirement not in requirement_ids:
                    continue                          # 不是现有需求编号：不计入
                if _flow(mtg) > 1 and requirement not in allowed:
                    continue
                normalized = review_rules.normalize_issue({
                    "severity": raw.get("severity"), "category": "wording",
                    "location": requirement, "problem": raw.get("problem"),
                    "suggestion": raw.get("suggestion")})
                key = (requirement, normalized["problem"], normalized["severity"])
                if key in seen:
                    continue
                seen.add(key)
                normalized["criterion"] = str(raw.get("criterion") or "")
                issues.append(normalized)
        _finish_step(mtg, "quality", quality_issues=issues)
        return advance("merge")

    # ---------- 汇总与裁决 ----------
    async def step_merge(self, mtg):
        if _done(mtg, "merge"):
            return advance("verdict")
        state = mtg.state()
        flow = _flow(mtg)
        intake_items = [item for item in state.get("items") or []
                        if item.get("origin") == "user"
                        and int(item.get("round") or 0) == flow]
        program = _program_items(mtg)
        if any(item["kind"] in ("distorted", "missing") for item in program):
            # 主席只做归类：给走样与遗漏的条目补上它应当落在哪条需求上
            listing = "\n".join(f"- [{KIND_NAMES.get(item['kind'], item['kind'])}] "
                                f"{_where(item)}：{item.get('problem') or ''}"
                                for item in program)
            found = store.find_turn(mtg.meeting_id, phase="merge", round=flow,
                                    kind="confirm_merge", speaker_id=_chair(mtg)["id"])
            data = found["data"] if found else (await speak(
                mtg, _chair(mtg), kind="confirm_merge", turn_kind="confirm_merge",
                charter=_charter(mtg),
                segments=[Segment("append", f"## 待归类的核对条目\n{listing}", name="条目清单")],
                instruction=MERGE_INSTRUCTION, structured=_validate_items(allow_empty=True),
                turn_round=flow))["data"]
            fills: dict[str, str] = {}
            for row in data.get("items") or []:
                index = str(row.get("index") or "").strip()
                requirement = str(row.get("requirement") or "").strip()
                if index and requirement:
                    fills.setdefault(index, requirement)
            for item in program:
                if item["kind"] in ("distorted", "missing") and not item["requirement"]:
                    item["requirement"] = fills.get(item["index"], "")
        number = _next_number(state.get("items") or [], state.get("last_items") or [])
        for item in program:
            item["id"] = f"C{number}"
            item["origin"] = "program"
            item["round"] = flow
            number += 1
        _finish_step(mtg, "merge", items=list(intake_items) + program)
        return advance("verdict")

    async def step_verdict(self, mtg):
        if _done(mtg, "verdict"):
            return advance("report")
        line = str(mtg.params().get("min_blocking_severity") or "major")
        items = _items(mtg)
        verdict = confirm_rules.confirm_verdict(items, blocking_line=line)
        already = int(mtg.state().get("verdict_round") or 0) == _flow(mtg)
        mtg.set_state(verdict=verdict, verdict_round=_flow(mtg))
        if not already:                               # 升级确认点恢复时不再重复发事件
            await emit(mtg.run_id, "review_verdict", mtg.node_id, {
                "meeting_id": mtg.meeting_id, "round": _flow(mtg), "protocol": "confirm",
                "verdict": verdict, "blocking": len(_blocking(items, line)),
                "issues": len(items)})
        if verdict == "pass":
            _mark(mtg, "verdict")
            return advance("report")
        feedback = "需求确认不通过，请按清单逐条修改：\n" + _items_text(items)
        result = await reroute_or_escalate(
            mtg, ["requirement"], feedback,
            title="需求确认不通过", description=feedback[:3000],
            summary="需求确认结论：不通过，回流需求分析师")
        if result is None:                            # 人在确认点上接受现状
            mtg.set_state(accepted_by_user=True)
            _mark(mtg, "verdict")
            return advance("report")
        if result.action == "suspend":                # 回流轮次用尽：等人决定，决定前不推进
            return result
        mtg.set_state(awaiting_rework=True, rework_pending=True, last_items=items)
        _mark(mtg, "verdict")
        return result

    # ---------- 报告 ----------
    async def step_report(self, mtg):
        state = mtg.state()
        items = _items(mtg)
        verdict = str(state.get("verdict") or "")
        accepted_by_user = bool(state.get("accepted_by_user"))
        precheck_ok, precheck_reason = self._precheck(verdict, accepted_by_user)
        mtg.set_state(precheck_ok=precheck_ok, precheck_reason=precheck_reason)
        report = self._write_report(mtg, items, verdict, accepted_by_user)
        line = str(mtg.params().get("min_blocking_severity") or "major")
        blocking = _blocking(items, line)
        summary = (f"需求确认会议完成：结论 {'通过' if verdict == 'pass' else '不通过'}，"
                   f"{len(items)} 条待办，{len(blocking)} 条阻塞")
        return done(summary, verdict=verdict, precheck_ok=precheck_ok,
                    precheck_reason=precheck_reason, accepted_by_user=accepted_by_user,
                    report=report, items=len(items), blocking=len(blocking))

    def _precheck(self, verdict: str, accepted_by_user: bool) -> tuple[bool, str]:
        if verdict == "pass" or accepted_by_user:
            return True, ""
        return False, "需求确认不通过，需求分析师需要按清单修改"

    def _write_report(self, mtg, items: list[dict], verdict: str,
                      accepted_by_user: bool) -> dict:
        state = mtg.state()
        latest = latest_artifact(mtg.run_id, "requirement") or {}
        mechanical = list(state.get("mechanical") or [])
        fidelity = list(state.get("fidelity") or [])
        quality = list(state.get("quality_issues") or [])
        conclusion = [f"结论：{'通过' if verdict == 'pass' else '不通过'}。"]
        if accepted_by_user:
            conclusion.append("人在回流轮次用尽的确认点上接受了现状。")
        sections = [
            ("程序核对结果", "\n".join(f"- {finding}" for finding in mechanical) or "全部通过"),
            ("逐决策点的忠实性结论", _fidelity_table(fidelity)),
            ("质量评审意见", _quality_table(quality)),
            ("结论", "\n".join(conclusion)),
            ("核对清单", _items_table(items)),
        ]
        return minutes.write_minutes(
            mtg.run_id, kind="review", created_by="chair", inputs=["requirement"],
            meeting=mtg.meeting(), participants=mtg.participants(),
            points=_ledger_points(mtg), turns=store.list_turns(mtg.meeting_id, limit=100_000),
            extra_sections=sections)


def _fidelity_table(rows: list[dict]) -> str:
    if not rows:
        return "（无）"
    lines = ["| 决策点 | 结论 | 理由 | 核对者 |", "|---|---|---|---|"]
    for row in rows:
        lines.append(f"| {_cell(row.get('index'))} | "
                     f"{RESULT_NAMES.get(row.get('result'), row.get('result'))} | "
                     f"{_cell(row.get('reason'))} | {_cell(row.get('checker'))} |")
    return "\n".join(lines)


def _quality_table(rows: list[dict]) -> str:
    if not rows:
        return "（无）"
    lines = ["| 需求 | 检查维度 | 级别 | 问题 | 建议 |", "|---|---|---|---|---|"]
    for row in rows:
        severity = review_rules.SEVERITY_NAMES.get(row.get("severity"), row.get("severity"))
        lines.append(f"| {_cell(row.get('location'))} | {_cell(row.get('criterion'))} | "
                     f"{severity} | {_cell(row.get('problem'))} | "
                     f"{_cell(row.get('suggestion'))} |")
    return "\n".join(lines)


def _items_table(items: list[dict]) -> str:
    if not items:
        return "（无）"
    lines = ["| 编号 | 类别 | 需求 | 决策点 | 问题 | 建议 |", "|---|---|---|---|---|---|"]
    for item in items:
        lines.append(f"| {item.get('id', '')} | "
                     f"{KIND_NAMES.get(item.get('kind'), item.get('kind'))} | "
                     f"{_cell(item.get('requirement'))} | {_cell(item.get('index'))} | "
                     f"{_cell(item.get('problem'))} | {_cell(item.get('suggestion'))} |")
    return "\n".join(lines)
