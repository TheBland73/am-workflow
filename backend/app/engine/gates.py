"""人工门决策：approve / reject（带反馈与目标节点）/ edit（编辑工件后通过）。

设计规格 §5.5：
- 门挂起时不占线程，只写状态与事件；
- 驳回必须带 feedback 与 target_node，目标节点 attempt+1，其后受影响节点重置 pending，
  反馈写入事件与目标工件，重新执行时由上下文组装注入。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from app.artifacts.store import parse_front_matter
from app.config import settings
from app.db.base import session_scope
from app.db.models import Artifact, Gate, Node, Run
from app.engine.node_actions import downstream_nodes

PENDING_SECTION = "未决项"
PENDING_TITLE = "讨论未决项（需人工拍板）"
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


class GateError(ValueError):
    pass


def _extract_pending_questions(body: str) -> list[str]:
    """按段落作用域提取讨论稿的未决项（`- [ ]` 行）。

    优先定位标题恰为「未决项」的小节（规范是 `### 未决项`，兼容 `##` 等层级），只收集
    该标题之后、下一个同级或更高级标题之前的勾选项，避免把共识/证据/分歧里的勾选项
    误当未决项。找不到该标题时退回全文扫描，兼容没有该小节的旧工件。
    """
    lines = [line.strip() for line in body.splitlines()]
    start: int | None = None
    level = 0
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match and match.group(2) == PENDING_SECTION:
            start = index + 1
            level = len(match.group(1))
            break
    if start is None:
        return [line for line in lines if line.startswith("- [ ]")]
    items: list[str] = []
    for line in lines[start:]:
        match = _HEADING_RE.match(line)
        if match and len(match.group(1)) <= level:
            break
        if line.startswith("- [ ]"):
            items.append(line)
    return items


def _pending_questions(s, run_id: str) -> list[str]:
    """提取讨论稿蒸馏部分的未决项（`- [ ]` 行），供方案确认门卡片展示。

    必须复用调用方 session（连接池 size=1，另开 session 会在池上死等）。
    """
    row = (
        s.query(Artifact)
        .filter(Artifact.run_id == run_id, Artifact.kind == "discussion",
                Artifact.status == "active")
        .order_by(Artifact.version.desc())
        .first()
    )
    if row is None:
        return []
    path = Path(row.path)
    if not path.is_absolute():
        path = settings.base_dir / path
    if not path.exists():
        return []
    _meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
    return _extract_pending_questions(body)


def gate_payload(s, gate: Gate) -> dict:
    """人工门对外 payload：request_gate / get_gate / run 快照共用同一份字段。

    必须复用调用方 session（连接池 size=1，另开 session 会在池上死等）。
    方案确认门（gate_proposal）的 description 追加讨论稿未决项；描述里已含该段落时
    不重复追加，保证多次读取（刷新/重载）幂等。
    """
    node = s.get(Node, gate.node_id)
    config = dict(node.config_json or {}) if node is not None else {}
    node_key = node.node_key if node is not None else ""
    description = config.get("description") or ""
    if node_key == "gate_proposal" and PENDING_TITLE not in description:
        pending = _pending_questions(s, gate.run_id)
        if pending:
            description += f"\n\n{PENDING_TITLE}：\n" + "\n".join(pending)
    return {
        "id": gate.id,
        "run_id": gate.run_id,
        "node_id": gate.node_id,
        "node_key": node_key,
        "status": gate.status,
        "feedback": gate.feedback,
        "decided_by": gate.decided_by,
        "created_at": gate.created_at.isoformat() if gate.created_at else None,
        "decided_at": gate.decided_at.isoformat() if gate.decided_at else None,
        "title": config.get("title") or (node.name if node is not None else ""),
        "description": description,
        "artifact": config.get("artifact"),
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
                target_node: str | None = None, decided_by: str = "human") -> dict:
    """执行门决策。reject 时必须提供 feedback 与 target_node（节点 key）。"""
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
        run = s.get(Run, gate.run_id)
        run_id = gate.run_id
        approve_artifact: str | None = None
        now = datetime.now(timezone.utc)
        gate.feedback = feedback
        gate.decided_by = decided_by
        gate.decided_at = now
        reset_keys: list[str] = []

        if action == "approve":
            gate.status = "approved"
            config = dict(gate_node.config_json or {})
            if config.get("escalation"):
                # 失败转人工：人工确认后重试该节点，而不是把失败节点标记为完成
                gate_node.status = "pending"
                for key in ("feedback", "escalation", "escalation_reason"):
                    config.pop(key, None)
            else:
                gate_node.status = "done"
                config.pop("feedback", None)
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
            gate_node.status = "pending"
            target.status = "pending"
            target.config_json = {**(target.config_json or {}), "feedback": feedback}
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
