"""人工门决策：approve / reject（带反馈与目标节点）/ edit（编辑工件后通过）。

设计规格 §5.5：
- 门挂起时不占线程，只写状态与事件；
- 驳回必须带 feedback 与 target_node，目标节点 attempt+1，其后受影响节点重置 pending，
  反馈写入事件与目标工件，重新执行时由上下文组装注入。
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.db.base import session_scope
from app.db.models import Gate, Node, Run


class GateError(ValueError):
    pass


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
        return {"id": gate.id, "run_id": run_id, "node_id": node_id, "status": "open",
                "title": (node.config_json or {}).get("title") or node.name,
                "description": (node.config_json or {}).get("description") or "",
                "artifact": (node.config_json or {}).get("artifact")}


def get_gate(gate_id: int) -> dict | None:
    with session_scope() as s:
        gate = s.get(Gate, gate_id)
        if gate is None:
            return None
        node = s.get(Node, gate.node_id)
        return {
            "id": gate.id,
            "run_id": gate.run_id,
            "node_id": gate.node_id,
            "node_key": node.node_key if node else "",
            "status": gate.status,
            "feedback": gate.feedback,
            "decided_by": gate.decided_by,
            "decided_at": gate.decided_at.isoformat() if gate.decided_at else None,
            "title": ((node.config_json or {}).get("title") or node.name) if node else "",
            "description": ((node.config_json or {}).get("description") or "") if node else "",
            "artifact": (node.config_json or {}).get("artifact") if node else None,
        }


def open_gate_for_run(run_id: str) -> dict | None:
    with session_scope() as s:
        gate = (
            s.query(Gate)
            .filter(Gate.run_id == run_id, Gate.status == "open")
            .order_by(Gate.id.asc())
            .first()
        )
        return {"id": gate.id, "node_id": gate.node_id} if gate else None


def _downstream(node_id: int, nodes: list[Node]) -> list[Node]:
    """返回依赖图上 node 的所有下游节点（含间接）。"""
    by_id = {n.id: n for n in nodes}
    children: dict[int, list[int]] = {}
    for n in nodes:
        for dep_key in n.deps_json or []:
            parent = next((x for x in nodes if x.node_key == dep_key), None)
            if parent is not None:
                children.setdefault(parent.id, []).append(n.id)
    result: list[int] = []
    stack = list(children.get(node_id, []))
    while stack:
        current = stack.pop()
        if current in result:
            continue
        result.append(current)
        stack.extend(children.get(current, []))
    return [by_id[i] for i in result if i in by_id]


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
            for downstream in _downstream(target.id, nodes):
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
