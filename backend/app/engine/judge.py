"""确认点的模型判定（详设 §5.3）。

裁决模型先写要点再判：第一次调用只给判据、标题与描述，让它写下"我会重点核对什么"；
第二次调用给判据、它自己写的要点、被审工件的全文与上游会议的结论，再给出结论。
判不了、输出不合格、连续驳回达到上限时，门留给人——这条路径不可配置关闭。

确认点先用程序做前置检查：上游会议留下了 precheck_ok 且为假时，不调模型直接驳回。

异构默认：judge 角色没有显式绑定模型时，在可用模型里优先选与全部参会者都不同家族的；
用户绑定了模型就照用。裁决模型与全部参会者同家族时判定照常生效，但在决定、事件与日志里
标注，提示用户复核（同一家族的模型评审自己的产出不可靠，见详设 §3.1 的 E6）。
"""
from __future__ import annotations

from app.artifacts.store import parse_front_matter, read_artifact, strip_outer_fence
from app.db.base import session_scope
from app.db.models import AgentTemplate, Gate, Meeting, MeetingParticipant, Node
from app.engine.agent_loop import _extract_json, run_agent
from app.engine.context import build_system
from app.engine.events import emit
from app.engine.gates import request_gate
from app.engine.params import decider_for, definition_of, effective_params, run_of
from app.meeting.prompts import call_tag
from app.models.budget import ContextBudget, estimate_tokens, truncate_to_tokens
from app.models.pool import ModelInfo, pool

# 模型名 → 家族。认不出的家族就是模型名本身（详设 §5.3）。
_FAMILIES = (
    ("openai", ("gpt", "o1", "o2", "o3", "o4")),
    ("anthropic", ("claude",)),
    ("google", ("gemini",)),
    ("zhipu", ("glm", "chatglm")),
    ("deepseek", ("deepseek",)),
    ("alibaba", ("qwen", "通义")),
    ("bytedance", ("doubao",)),
    ("moonshot", ("kimi", "moonshot")),
    ("meta", ("llama",)),
)
_MEETING_LABELS = {"discussion": "讨论会议", "review": "评审会议", "confirm": "需求确认会议"}
_REPORT_KINDS = {"discussion": "discussion", "review": "review", "confirm": "review"}
_VERDICT_NAMES = {"accept": "接受", "conditional": "有条件接受", "return": "退回",
                  "pass": "通过", "fail": "不通过"}
_RANK = {"blocker": 0, "major": 1, "minor": 2}

GATE_POINTS_INSTRUCTION = (
    "先不要评审工件本身。依据判据与确认点的说明，写下你这次会重点核对什么，"
    "作为下一步核对的清单。只输出一个 JSON 对象：\n"
    '{"points": ["我会重点核对什么"]}')
GATE_VERDICT_INSTRUCTION = (
    "请给出结论：approve（通过）、reject（驳回）或 abstain（判不了）。"
    "驳回时必须写 feedback，说明作者应当怎么改；reasons 与 concerns 写你核对到的依据与疑点。"
    "只输出一个 JSON 对象：\n"
    '{"verdict": "approve", "reasons": ["…"], "concerns": ["…"], "feedback": ""}')


def checkpoint_key(config: dict, node_key: str) -> str:
    """确认点的键：门配置了 check 就用它，否则用节点键。"""
    return str((config or {}).get("check") or node_key)


def gate_decider(run_id: str, config: dict, node_key: str) -> str:
    """这个门由谁判定：逐点覆盖 > 自动化等级预设 > 默认的人。"""
    return decider_for(run_of(run_id).get("config") or {}, definition_of(run_id),
                       checkpoint_key(config, node_key))


SAME_FAMILY = "same_family"
SAME_FAMILY_HINT = "裁决模型与全部参会者属于同一家族，这次判定请复核；有异议可以重置节点否决"


def family_of(model_name: str) -> str:
    name = str(model_name or "").strip().lower()
    for family, keywords in _FAMILIES:
        if any(keyword in name for keyword in keywords):
            return family
    return name


def same_family(participant_models: list[str], judge_model: str) -> bool:
    """裁决模型与全部参会者同属一个家族。参会者或裁决模型不明时不算。"""
    names = [str(name) for name in participant_models or [] if str(name or "").strip()]
    judge = str(judge_model or "").strip()
    if not names or not judge:
        return False
    return all(family_of(name) == family_of(judge) for name in names)


def choose_judge_model(candidates: list[ModelInfo],
                       participant_models: list[str]) -> ModelInfo | None:
    """在候选里优先取与全部参会者都不同家族的模型；都不满足时取排在最前的那个，没有候选返回 None。"""
    if not candidates:
        return None
    families = {family_of(name) for name in participant_models or [] if str(name or "").strip()}
    for model in candidates:
        if family_of(model.model_name) not in families:
            return model
    return candidates[0]


def pick_judge_model(participant_models: list[str]) -> ModelInfo | None:
    """裁决模型：用户给 judge 角色绑了模型就照用；没绑定时异构优先。"""
    if pool.role_default_model_id("judge") is not None:
        return pool.pick_role_model("judge")
    return choose_judge_model(pool.role_model_candidates("judge"), participant_models)


def rubric_for(definition: dict, config: dict) -> str:
    """取与确认点键同名的判据；没有（设计之后的门）取通用的工件判据。"""
    rubrics = dict((definition or {}).get("rubrics") or {})
    return rubrics.get(checkpoint_key(config, "")) or rubrics.get("artifact") or ""


def heterogeneity_hint(participant_models: list[str], judge_model: str) -> str:
    """裁决模型与全部参会者同一家族时给一句提示，不阻断判定。"""
    return "裁决模型与全部参会者属于同一家族，建议换一家的模型做裁决" if same_family(
        participant_models, judge_model) else ""


def hint_for_gate(run_id: str, node: dict) -> str:
    """人工门打开时的"同一家族"提示。取上游会议全部参会者的模型与裁决模型比较。"""
    names = _participant_models(run_id, node)
    model = pick_judge_model(names)
    if model is None:
        return ""
    return heterogeneity_hint(names, model.model_name)


def _participant_models(run_id: str, node: dict) -> list[str]:
    deps = set(node.get("deps") or [])
    wanted: list[tuple[int | None, str | None]] = []
    role_defaults: dict[str, int] = {}
    with session_scope() as s:
        nodes = s.query(Node).filter(Node.run_id == run_id).all()
        dep_ids = [row.id for row in nodes if row.node_key in deps and row.type == "MEETING"]
        for node_id in dep_ids:
            meeting = (s.query(Meeting).filter(Meeting.node_id == node_id)
                       .order_by(Meeting.id.desc()).first())
            if meeting is None:
                continue
            for participant in (s.query(MeetingParticipant)
                                .filter(MeetingParticipant.meeting_id == meeting.id).all()):
                wanted.append((participant.model_id, participant.role_key))
        for role_key in {key for _, key in wanted if key}:
            row = s.query(AgentTemplate).filter(AgentTemplate.role_key == role_key).first()
            if row is not None and row.default_model_id:
                role_defaults[role_key] = int(row.default_model_id)
    names: list[str] = []
    for model_id, role_key in wanted:
        target = model_id or role_defaults.get(role_key or "")
        if target is None:
            continue
        try:
            names.append(pool.load_model(int(target)).model_name)
        except Exception:
            continue
    return names


async def judge_gate(run_id: str, node: dict, deps: list[dict], attempt_id: int) -> dict:
    """判定一个开着的确认点。返回 {"action": "approve"|"reject"|"open", "reason": str}。

    判定前先建一条开着的门（已经有一条开着的就复用）；判定有效就落决定，无效就留着给人。
    """
    config = dict(node.get("config") or {})
    node_id = node["id"]
    gate_id = _reuse_or_open_gate(run_id, node)

    async def open_(reason: str) -> dict:
        _remember_hint(node_id, reason)
        await emit(run_id, "node_log", node_id, {
            "level": "warn", "message": f"确认点转交人工：{reason}"[:300]})
        return {"action": "open", "reason": reason}

    participants = _participant_models(run_id, node)
    model = pick_judge_model(participants)
    if model is None:
        return await open_("没有可用的裁决模型：请先配置供应商与模型")
    homogeneous = same_family(participants, model.model_name)

    definition = definition_of(run_id)
    rubric = rubric_for(definition, config)
    precheck = _precheck_failure(deps)
    if precheck:
        if not config.get("reject_target"):
            return await open_(precheck)
        # 前置检查是程序的结论，不是模型的：不算一次模型驳回
        return await _decide(run_id, node_id, gate_id, model, config, "reject", precheck,
                             reasons=[precheck], concerns=[], rubric=rubric,
                             kind=checkpoint_key(config, node["node_key"]), decided_by="program",
                             count=False)

    max_rejects = _max_auto_rejects(run_id)
    rejects = int(config.get("auto_rejects") or 0)
    if rejects >= max_rejects:
        return await open_(f"模型已连续驳回 {rejects} 次，达到上限（{max_rejects}），"
                           "这次交给人决定")

    title = str(config.get("title") or node.get("name") or node["node_key"])
    description = str(config.get("description") or "")
    points, points_raw = await _write_points(run_id, node, attempt_id, model,
                                            rubric, title, description)
    result = await _verdict(run_id, node, attempt_id, model, config,
                            rubric=rubric, points=points, points_raw=points_raw)
    if result is None:
        return await open_("裁决模型没有返回合法的 JSON")
    data, verdict = result
    reasons = [str(item).strip() for item in data.get("reasons") or []
               if str(item).strip()]
    concerns = [str(item).strip() for item in data.get("concerns") or []
                if str(item).strip()]
    feedback = str(data.get("feedback") or "").strip()
    if verdict == "abstain":
        return await open_("裁决模型表示判不了（弃权），需要人来决定")
    if verdict not in ("approve", "reject"):
        return await open_("裁决模型的结论无效，需要人来决定")
    if verdict == "reject":
        if not feedback:
            return await open_("裁决模型驳回，但没有给出反馈，需要人来决定")
        if not config.get("reject_target"):
            return await open_("裁决模型建议驳回，但这个确认点没有配置退回的目标节点")
    return await _decide(run_id, node_id, gate_id, model, config, verdict, feedback,
                         reasons=reasons, concerns=concerns, rubric=rubric,
                         kind=checkpoint_key(config, node["node_key"]),
                         same_family_judge=homogeneous)


# ---------------- 判定路径 ----------------
def _reuse_or_open_gate(run_id: str, node: dict) -> int:
    with session_scope() as s:
        row = (s.query(Gate).filter(Gate.node_id == node["id"], Gate.status == "open")
               .order_by(Gate.id.desc()).first())
        if row is not None:
            return row.id
    return request_gate(run_id, node["id"])["id"]


def _remember_hint(node_id: int, reason: str) -> None:
    with session_scope() as s:
        row = s.get(Node, node_id)
        if row is not None:
            row.config_json = {**(row.config_json or {}), "hint": reason}


def _precheck_failure(deps: list[dict]) -> str:
    """上游会议留下了 precheck_ok 且为假：返回原因；没有这个字段就不做前置检查。"""
    for dep in deps or []:
        if dep.get("type") != "MEETING":
            continue
        with session_scope() as s:
            row = (s.query(Meeting).filter(Meeting.node_id == dep["id"])
                   .order_by(Meeting.id.desc()).first())
            state = dict(row.state_json or {}) if row is not None else {}
        if "precheck_ok" in state and not state.get("precheck_ok"):
            return str(state.get("precheck_reason") or "上游会议的前置检查未通过")
    return ""


def _max_auto_rejects(run_id: str) -> int:
    try:
        params = effective_params(definition_of(run_id), run_of(run_id).get("config") or {})
        value = (params.get("gates") or {}).get("max_auto_rejects")
        return max(1, int(value if value is not None else 2))
    except (TypeError, ValueError):
        return 2


def _judge_system() -> str:
    with session_scope() as s:
        row = s.query(AgentTemplate).filter(AgentTemplate.role_key == "judge").first()
        prompt = row.system_prompt if row is not None else "你是确认点的裁决模型。"
    return build_system(prompt)


async def _write_points(run_id: str, node: dict, attempt_id: int, model: ModelInfo,
                        rubric: str, title: str, description: str) -> tuple[list[str], str]:
    prompt = "\n\n".join([
        f"## 判据\n{rubric}",
        f"## 本次确认点\n{title}\n{description}".strip(),
        GATE_POINTS_INSTRUCTION,
        call_tag("gate_points"),
    ])
    result = await run_agent(
        run_id=run_id, node_id=node["id"], attempt_id=attempt_id, model=model,
        system_prompt=_judge_system(), user_prompt=prompt, toolbox=None,
        max_steps=1, temperature=0.2, quiet=True)
    data = _extract_json(strip_outer_fence(result.content))
    points = []
    if isinstance(data, dict):
        points = [str(item).strip() for item in data.get("points") or []
                  if str(item).strip()]
    return points, result.content


def artifact_budget(model: ModelInfo, fixed_tokens: int) -> int:
    """判定调用里分给被审工件的 token 上限。

    可用窗口减去系统提示、判据、核对要点、上游结论与指令之后剩下的部分：大窗口模型
    不必再按固定的 6000 token 截断，小窗口模型也不会被塞进比整个输入预算还大的工件。
    """
    input_budget = ContextBudget(model.context_window, model.max_output_tokens).input_budget
    return max(1000, input_budget - max(0, int(fixed_tokens)) - 200)


async def _verdict(run_id: str, node: dict, attempt_id: int, model: ModelInfo,
                   config: dict, *, rubric: str, points: list[str],
                   points_raw: str) -> tuple[dict, str] | None:
    artifact_kind = str(config.get("artifact") or "")
    upstream = _upstream_text(run_id, node) or "（上游没有会议结论）"
    head = [f"## 判据\n{rubric}",
            "## 你写下的核对要点\n" + ("\n".join(f"- {item}" for item in points)
                                         or points_raw.strip() or "（没有写要点）")]
    tail = [f"## 上游会议的结论\n{upstream}",
            GATE_VERDICT_INSTRUCTION, call_tag("gate_verdict")]
    fixed = (estimate_tokens(_judge_system()) + estimate_tokens("\n\n".join(head + tail))
             + estimate_tokens(f"## 被审工件（{artifact_kind or '无'}）\n"))
    artifact_text = (_artifact_excerpt(run_id, artifact_kind, artifact_budget(model, fixed))
                     if artifact_kind else "（这个确认点没有配置被审工件）")
    prompt = "\n\n".join([
        head[0],
        head[1],
        f"## 被审工件（{artifact_kind or '无'}）\n{artifact_text}",
        tail[0],
        tail[1],
        tail[2],
    ])
    result = await run_agent(
        run_id=run_id, node_id=node["id"], attempt_id=attempt_id, model=model,
        system_prompt=_judge_system(), user_prompt=prompt, toolbox=None,
        max_steps=1, temperature=0.2, quiet=True)
    data = _extract_json(strip_outer_fence(result.content))
    if not isinstance(data, dict):
        return None
    return data, str(data.get("verdict") or "").strip().lower()


async def _decide(run_id: str, node_id: int, gate_id: int, model: ModelInfo, config: dict,
                  action: str, feedback: str, *, reasons: list[str],
                  concerns: list[str], rubric: str, kind: str,
                  decided_by: str | None = None, count: bool = True,
                  same_family_judge: bool = False) -> dict:
    """落下判定并留痕。每次判定都推一条 checkpoint_decided（与程序自动通过、会议内确认点同口径）。

    same_family_judge：裁决模型与全部参会者同家族。决定照常生效，但在决定、事件与日志里标注，
    提示用户复核（详设 §5.3 的"异构默认"：标注并提示，不静默通过，也不阻断）。
    """
    from app.engine.orchestrator import orchestrator       # 延迟导入：避免与编排器循环导入

    decider = decided_by or f"model:{model.display_name}"
    decision: dict = {"action": action, "feedback": feedback or "",
                      "reasons": reasons, "concerns": concerns}
    if same_family_judge:
        decision["heterogeneity"] = SAME_FAMILY
        decision["heterogeneity_hint"] = SAME_FAMILY_HINT
    await orchestrator.decide_gate(
        gate_id=gate_id, action=action, feedback=feedback or None,
        target_node=(config.get("reject_target") if action == "reject" else None),
        decided_by=decider,
        data={"reasons": reasons, "concerns": concerns, "rubric": rubric,
              **({key: decision[key] for key in ("heterogeneity", "heterogeneity_hint")}
                 if same_family_judge else {})})
    await emit(run_id, "checkpoint_decided", node_id, {
        "gate_id": gate_id, "kind": kind, "decided_by": decider, "decision": decision})
    if same_family_judge:
        await emit(run_id, "node_log", node_id, {
            "level": "warn",
            "message": f"裁决模型与全部参会者属于同一家族（{model.display_name}），请复核这次判定"[:300]})
    with session_scope() as s:
        row = s.get(Node, node_id)
        if row is not None:
            cfg = dict(row.config_json or {})
            cfg.pop("hint", None)
            if count and action == "reject":
                cfg["auto_rejects"] = int(cfg.get("auto_rejects") or 0) + 1
            elif count:
                cfg.pop("auto_rejects", None)
            row.config_json = cfg
    return {"action": action, "reason": feedback}


# ---------------- 判据里的上游结论 ----------------
def _artifact_excerpt(run_id: str, kind: str, limit_tokens: int = 6000) -> str:
    raw = read_artifact(run_id, kind)
    if not raw:
        return "（没有找到工件）"
    _meta, body = parse_front_matter(raw)
    body = body.strip()
    if estimate_tokens(body) <= limit_tokens:
        return body
    # 超预算时显式标注，不把截断伪装成全文
    return (truncate_to_tokens(body, limit_tokens)
            + "\n\n（工件超出判定预算，后面部分被截断）")


def _upstream_text(run_id: str, node: dict) -> str:
    deps = set(node.get("deps") or [])
    lines: list[str] = []
    with session_scope() as s:
        dep_ids = [row.id for row in s.query(Node).filter(Node.run_id == run_id).all()
                   if row.node_key in deps and row.type == "MEETING"]
        meetings = []
        for node_id in dep_ids:
            row = (s.query(Meeting).filter(Meeting.node_id == node_id)
                   .order_by(Meeting.id.desc()).first())
            if row is not None:
                meetings.append({
                    "protocol": row.protocol, "round": row.round or 1,
                    "state": dict(row.state_json or {}),
                    "params": dict((row.config_json or {}).get("params") or {}),
                })
    for meeting in meetings:
        label = _MEETING_LABELS.get(meeting["protocol"], meeting["protocol"])
        pieces = [f"{label}（第 {meeting['round']} 轮）"]
        state = meeting["state"]
        verdict = str(state.get("verdict") or "")
        if verdict:
            pieces.append(f"结论：{_VERDICT_NAMES.get(verdict, verdict)}")
        if "precheck_ok" in state:
            pieces.append("前置检查通过" if state.get("precheck_ok")
                          else f"前置检查未通过：{state.get('precheck_reason') or ''}")
        lines.append("，".join(pieces) + "。")
        blocking = _blocking_lines(meeting)
        if blocking:
            lines.append("仍然成立且达到阻塞线的前几条意见：")
            lines.extend(blocking)
        report_kind = _REPORT_KINDS.get(meeting["protocol"])
        if report_kind:
            excerpt = _artifact_excerpt(run_id, report_kind, 2000)
            if excerpt and excerpt != "（没有找到工件）":
                lines.append(f"报告要点：\n{excerpt}")
    return "\n".join(lines)


def _blocking_lines(meeting: dict) -> list[str]:
    state = meeting["state"]
    if meeting["protocol"] == "review":
        line = str(meeting["params"].get("min_blocking_severity") or "major")
        limit = _RANK.get(line, _RANK["major"])
        rows = [issue for issue in state.get("issues") or []
                if issue.get("standing", True)
                and _RANK.get(issue.get("severity"), _RANK["minor"]) <= limit]
    elif meeting["protocol"] == "confirm":
        rows = list(state.get("items") or [])
    else:
        rows = []
    out = []
    for row in rows[:5]:
        location = (row.get("location") or row.get("requirement")
                    or row.get("index") or "—")
        out.append(f"- {row.get('id', '')} [{location}] {row.get('problem') or ''}")
    return out
