"""人工门决策：approve / reject（带反馈与目标节点）/ edit（编辑工件后通过）。

设计规格 §5.5：
- 门挂起时不占线程，只写状态与事件；
- 驳回必须带 feedback 与 target_node，目标节点 attempt+1，其后受影响节点重置 pending，
  反馈写入事件与目标工件，重新执行时由上下文组装注入。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.artifacts.store import parse_front_matter
from app.config import settings
from app.db.base import session_scope
from app.db.models import Artifact, DecisionPoint, Gate, Meeting, Node, Run
from app.engine.feedback import with_feedback
from app.engine.node_actions import downstream_nodes
from app.models.budget import truncate_to_tokens

PENDING_TITLE = "待确认问题（需要你决定）"
_MEETING_LABELS = {"discussion": "讨论会议", "review": "评审会议", "confirm": "需求确认会议"}
_REPORT_KINDS = {"discussion": "discussion", "review": "review", "confirm": "review"}
_VERDICT_NAMES = {"accept": "接受", "conditional": "有条件接受", "return": "退回",
                  "pass": "通过", "fail": "不通过"}
_RANK = {"blocker": 0, "major": 1, "minor": 2}


class GateError(ValueError):
    pass


def _pending_questions(s, run_id: str) -> list[str]:
    """账本里待定的决策点，供方案确认门的卡片展示。

    必须复用调用方 session（连接池 size=1，另开 session 会在池上死等）。
    取最近一场讨论会议的账本；讨论会议还没开过时返回空列表。
    """
    meeting = (s.query(Meeting)
               .filter(Meeting.run_id == run_id, Meeting.protocol == "discussion")
               .order_by(Meeting.id.desc())
               .first())
    if meeting is None:
        return []
    rows = (s.query(DecisionPoint)
            .filter(DecisionPoint.meeting_id == meeting.id,
                    DecisionPoint.status == "deferred")
            .order_by(DecisionPoint.id.asc())
            .all())
    return [f"- [ ] {row.index} {row.title}：{row.conclusion or '各方理由见会议纪要'}"
            for row in rows]


def _artifact_body_in_session(s, run_id: str, kind: str, limit: int = 2000) -> str:
    """在同一会话里读工件正文（连接池 size=1，不能在这里再开 session）。"""
    row = (
        s.query(Artifact)
        .filter(Artifact.run_id == run_id, Artifact.kind == kind)
        .order_by(Artifact.version.desc())
        .first()
    )
    if row is None:
        return ""
    path = Path(row.path)
    if not path.is_absolute():
        path = settings.base_dir / path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    _meta, body = parse_front_matter(text)
    return truncate_to_tokens(body.strip(), limit)


def _blocking_lines(meeting: Meeting) -> list[str]:
    state = dict(meeting.state_json or {})
    params = dict((meeting.config_json or {}).get("params") or {})
    if meeting.protocol == "review":
        line = str(params.get("min_blocking_severity") or "major")
        limit = _RANK.get(line, _RANK["major"])
        rows = [issue for issue in state.get("issues") or []
                if issue.get("standing", True)
                and _RANK.get(issue.get("severity"), _RANK["minor"]) <= limit]
    elif meeting.protocol == "confirm":
        rows = list(state.get("items") or [])
    else:
        rows = []
    lines = []
    for row in rows[:5]:
        location = row.get("location") or row.get("requirement") or row.get("index") or "—"
        lines.append(f"- {row.get('id', '')} [{location}] {row.get('problem') or ''}")
    return lines


def _upstream_lines(s, node: Node | None, run_id: str) -> list[str]:
    """上游会议的结论、轮次、前置检查，以及仍然成立的阻塞意见与报告要点。"""
    if node is None:
        return []
    by_key = {row.node_key: row for row in s.query(Node).filter(Node.run_id == run_id).all()}
    lines: list[str] = []
    for dep_key in node.deps_json or []:
        dep = by_key.get(dep_key)
        if dep is None or dep.type != "MEETING":
            continue
        meeting = (s.query(Meeting).filter(Meeting.node_id == dep.id)
                   .order_by(Meeting.id.desc()).first())
        if meeting is None:
            continue
        state = dict(meeting.state_json or {})
        label = _MEETING_LABELS.get(meeting.protocol, meeting.protocol)
        pieces = [f"{label}（第 {meeting.round or 1} 轮）"]
        verdict = str(state.get("verdict") or "")
        if verdict:
            pieces.append(f"结论：{_VERDICT_NAMES.get(verdict, verdict)}")
        if "precheck_ok" in state:
            pieces.append("前置检查通过" if state.get("precheck_ok")
                          else f"前置检查未通过：{state.get('precheck_reason') or ''}")
        lines.append("上游会议：" + "，".join(pieces) + "。")
        blocking = _blocking_lines(meeting)
        if blocking:
            lines.append("仍然成立且达到阻塞线的前五条意见：")
            lines.extend(blocking)
        report_kind = _REPORT_KINDS.get(meeting.protocol)
        if report_kind:
            excerpt = _artifact_body_in_session(s, run_id, report_kind)
            if excerpt:
                lines.append("报告要点：")
                lines.append(excerpt)
    return lines


def gate_payload(s, gate: Gate) -> dict:
    """门对外 payload：request_gate / get_gate / run 快照共用同一份字段。

    必须复用调用方 session（连接池 size=1，另开 session 会在池上死等）。
    会议内确认点（门上带类型）的标题与描述存在门的载荷里；工件确认点沿用节点配置，
    方案确认门的描述追加讨论里的未决议题。描述里已含该段落时不重复追加，保证幂等。
    """
    node = s.get(Node, gate.node_id)
    config = dict(node.config_json or {}) if node is not None else {}
    node_key = node.node_key if node is not None else ""
    payload = dict(gate.payload_json or {})
    description = str(payload.get("description") or "") if gate.kind else (config.get("description") or "")
    if node_key == "gate_proposal" and not gate.kind and PENDING_TITLE not in description:
        pending = _pending_questions(s, gate.run_id)
        if pending:
            description += f"\n\n{PENDING_TITLE}：\n" + "\n".join(pending)
    if config.get("check") and not gate.kind:
        upstream = _upstream_lines(s, node, gate.run_id)
        if upstream:
            description += "\n\n" + "\n".join(upstream)
    hint = str(config.get("hint") or "").strip()
    if hint:
        description = (description.rstrip() + "\n\n" + hint).strip()
    title = (payload.get("title") if gate.kind else None) or config.get("title")
    return {
        "id": gate.id,
        "run_id": gate.run_id,
        "node_id": gate.node_id,
        "node_key": node_key,
        "meeting_id": gate.meeting_id,       # 会议内确认点所属的会议；人工门为空
        "status": gate.status,
        "feedback": gate.feedback,
        "decided_by": gate.decided_by,
        "created_at": gate.created_at.isoformat() if gate.created_at else None,
        "decided_at": gate.decided_at.isoformat() if gate.decided_at else None,
        "title": title or (node.name if node is not None else ""),
        "description": description,
        "artifact": config.get("artifact"),
        "kind": gate.kind or "",
        "payload": payload,
        "decision": dict(gate.decision_json or {}),
    }


def request_gate(run_id: str, node_id: int, feedback: str | None = None) -> dict:
    with session_scope() as s:
        node = s.get(Node, node_id)
        if node is None or node.run_id != run_id:
            raise GateError("节点不存在")
        gate = Gate(run_id=run_id, node_id=node_id, status="open", feedback=feedback)
        s.add(gate)
        node.status = "waiting_human"
        run = s.get(Run, run_id)
        if run:
            run.status = "waiting_human"
        s.flush()
        # 原返回字段（id/run_id/node_id/status/title/description/artifact）全部保留，
        # 其余字段为新增，向后兼容。
        return gate_payload(s, gate)


def get_gate(gate_id: int) -> dict | None:
    with session_scope() as s:
        gate = s.get(Gate, gate_id)
        if gate is None:
            return None
        return gate_payload(s, gate)


def open_gate_for_run(run_id: str) -> dict | None:
    with session_scope() as s:
        gate = (
            s.query(Gate)
            .filter(Gate.run_id == run_id, Gate.status == "open")
            .order_by(Gate.id.asc())
            .first()
        )
        return {"id": gate.id, "node_id": gate.node_id} if gate else None


def downstream_of(node_id: int, nodes: list[Node]) -> list[Node]:
    """返回依赖图上 node 的所有下游节点（含间接）。

    公共实现：节点血缘遍历统一收敛到 ``engine.node_actions.downstream_nodes``，
    ``decide_gate`` 的 reject 重置与节点动作的重置共用同一份算法，避免两份逻辑漂移。
    """
    node = next((item for item in nodes if item.id == node_id), None)
    if node is None:
        return []
    return downstream_nodes(nodes, node.node_key)


# 兼容旧私有名（orchestrator / 历史调用点仍可按 _downstream 引用）。
_downstream = downstream_of


def _reset(node: Node) -> None:
    node.status = "pending"


def decide_gate(*, gate_id: int, action: str, feedback: str | None = None,
                target_node: str | None = None, decided_by: str = "human",
                data: dict | None = None) -> dict:
    """执行门决策。工件确认点驳回时必须提供 feedback 与 target_node；
    会议内确认点的通过和驳回都只是把决定记在门上，让会议续开。"""
    if action not in ("approve", "reject"):
        raise GateError("action 只能是 approve 或 reject")
    with session_scope() as s:
        gate = s.get(Gate, gate_id)
        if gate is None:
            raise GateError("门不存在")
        if gate.status != "open":
            raise GateError(f"门已处理（当前状态 {gate.status}）")
        gate_node = s.get(Node, gate.node_id)
        if gate_node is None:
            raise GateError("门节点不存在")
        if not str(decided_by or "").startswith("model:"):
            # 人做出任何决定：模型连续驳回的计数清零
            config = dict(gate_node.config_json or {})
            if "auto_rejects" in config:
                config.pop("auto_rejects", None)
                gate_node.config_json = config
        run = s.get(Run, gate.run_id)
        run_id = gate.run_id
        approve_artifact: str | None = None
        now = datetime.now(timezone.utc)
        gate.feedback = feedback
        gate.decided_by = decided_by
        gate.decided_at = now
        reset_keys: list[str] = []

        if gate_node.type == "MEETING" and gate.kind:
            # 会议内确认点（门上带类型）：决定存在门上，节点回到待调度；驳回不需要目标节点。
            # 会议节点上的失败转人工门、恢复时补开的门没有类型，仍走下面工件门的规则。
            gate.status = "approved" if action == "approve" else "rejected"
            gate.decision_json = {"action": action, "feedback": feedback or "", "data": data or {}}
            gate_node.status = "pending"
            if run:
                run.status = "running"
                run.error = None
            s.flush()
            return {"gate_id": gate.id, "run_id": run_id, "status": gate.status, "action": action,
                    "feedback": feedback, "target_node": None, "reset_nodes": [],
                    "checkpoint": True}

        if action == "approve":
            gate.status = "approved"
            gate.decision_json = {"action": "approve", "feedback": feedback or "",
                                  "data": data or {}}
            config = with_feedback(dict(gate_node.config_json or {}), None)
            if config.get("escalation"):
                # 失败转人工：人工确认后重试该节点，而不是把失败节点标记为完成
                gate_node.status = "pending"
                for key in ("escalation", "escalation_reason"):
                    config.pop(key, None)
            else:
                gate_node.status = "done"
                approve_artifact = config.get("artifact")
            gate_node.config_json = config
            if run:
                run.status = "running"
                run.error = None
        else:
            if not feedback or not feedback.strip():
                raise GateError("驳回必须填写反馈")
            if not target_node:
                raise GateError("驳回必须指定目标节点 target_node")
            nodes = s.query(Node).filter(Node.run_id == gate.run_id).all()
            target = next((n for n in nodes if n.node_key == target_node), None)
            if target is None:
                raise GateError(f"目标节点不存在：{target_node}")
            if target.id == gate_node.id:
                raise GateError("目标节点不能是人工门自身")
            gate.status = "rejected"
            gate.decision_json = {"action": "reject", "feedback": feedback or "",
                                  "data": data or {}}
            gate_node.status = "pending"
            target.status = "pending"
            target.config_json = with_feedback(target.config_json, feedback, "reject")
            reset_keys.append(target.node_key)
            for downstream in downstream_of(target.id, nodes):
                if downstream.id == gate_node.id:
                    continue
                if downstream.status in ("done", "failed", "waiting_human", "running", "ready"):
                    _reset(downstream)
                    reset_keys.append(downstream.node_key)
            if run:
                run.status = "running"
                run.error = None

        s.flush()
        result = {
            "gate_id": gate.id,
            "run_id": gate.run_id,
            "status": gate.status,
            "action": action,
            "feedback": feedback,
            "target_node": target_node,
            "reset_nodes": reset_keys,
        }
    if approve_artifact:
        # 人工门通过即视为该工件获批（单连接池：必须在事务外更新）
        from app.artifacts.store import set_status

        record = set_status(run_id, approve_artifact, "approved")
        if record:
            result["approved_artifact"] = {"kind": approve_artifact,
                                           "version": record["version"]}
    return result


def gate_to_dict(gate: Gate) -> dict:
    return {
        "id": gate.id, "run_id": gate.run_id, "node_id": gate.node_id,
        "status": gate.status, "feedback": gate.feedback,
        "decided_by": gate.decided_by,
        "decided_at": gate.decided_at.isoformat() if gate.decided_at else None,
    }
