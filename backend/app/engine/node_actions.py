"""节点级人工动作：状态矩阵、状态迁移、下游重置与详情投影（节点交互增量 · 公共模块 1）。

设计约束（见 CONTRACT.md §1/§2）：
- 本模块只做「矩阵校验 + 事务内 ORM 变更」；``emit()`` 与 ``orchestrator.kick()`` 由 API 层
  在事务提交之后执行（短事务，避免单连接池上的长事务）；
- 不改调度器语义：动作只把节点改回 ``pending`` / 置 ``skipped``，让既有 ``_loop`` 继续推进；
- 不新增表/列（零迁移）：审计复用 ``events``；
- ``config_json`` 同时承载执行参数与调度状态，本模块只清理调度键，绝不碰执行参数。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session, object_session

from app.db.models import (
    Artifact,
    Event,
    Gate,
    Message,
    Node,
    NodeAttempt,
    Run,
    Task,
)

TERMINAL_RUN_STATUS = frozenset({"finished", "failed", "stopped"})

# config_json 中的调度状态键：人工动作不得让它们残留（§2「reset 清调度键」）。
_SCHEDULING_KEYS = ("feedback", "retry_at", "retry_count", "escalation", "escalation_reason")

# 契约 §1 常量：config_json 里由调度器独占、外部（未来的配置编辑）永不可写的键。
PROTECTED_CONFIG_KEYS = {
    "feedback",
    "retry_at",
    "retry_count",
    "escalation",
    "escalation_reason",
    "round",
    "review",
    "review_history",
}

# reset_downstream 需要重置的状态（与 gates.decide_gate reject 分支保持一致）。
_RESETTABLE_STATUSES = ("done", "failed", "waiting_human", "ready")


class NodeActionError(ValueError):
    """节点动作的领域异常：`code` 供 API 映射结构化 HTTPException。"""

    def __init__(self, code: str, message: str, hint: str | None = None,
                 details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.details: dict = dict(details or {})


# ---------------- 内部辅助 ----------------
def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _session_of(node: Node) -> Session:
    session = object_session(node)
    if session is None:
        raise RuntimeError("节点对象未绑定会话：apply_* 必须在 session_scope() 内调用")
    return session


def _node_ref(node: Node) -> dict:
    return {"node_key": node.node_key, "name": node.name, "status": node.status}


def _clean_scheduling_config(config: dict | None) -> dict:
    """复制 config 并剔除调度状态键；执行参数（instruction/artifact/timeout_s…）原样保留。"""
    cleaned = dict(config or {})
    for key in _SCHEDULING_KEYS:
        cleaned.pop(key, None)
    return cleaned


def _children_by_key(nodes: list[Node]) -> dict[str, list[str]]:
    children: dict[str, list[str]] = {node.node_key: [] for node in nodes}
    for node in nodes:
        for dep_key in node.deps_json or []:
            if dep_key in children:
                children[dep_key].append(node.node_key)
    return children


def _sync_task_status(s: Session, run_id: str, node_key: str, status: str) -> None:
    """dev_<task_uid> 节点与 Task 表双向绑定：同步任务状态（review 报告会读 Task.status）。"""
    if not node_key.startswith("dev_"):
        return
    task_uid = node_key[len("dev_"):]
    row = (
        s.query(Task)
        .filter(Task.run_id == run_id, Task.task_uid == task_uid)
        .one_or_none()
    )
    if row is not None:
        row.status = status


def _reset_downstream_impl(s: Session, run_id: str, node_key: str, *,
                           sync_tasks: bool = True) -> list[str]:
    nodes = (
        s.query(Node)
        .filter(Node.run_id == run_id)
        .order_by(Node.order_index.asc(), Node.id.asc())
        .all()
    )
    reset_keys: list[str] = []
    for node in downstream_nodes(nodes, node_key):
        if node.status not in _RESETTABLE_STATUSES:
            continue
        node.status = "pending"
        node.config_json = _clean_scheduling_config(node.config_json)
        reset_keys.append(node.node_key)
        if sync_tasks:
            _sync_task_status(s, run_id, node.node_key, "pending")
    return reset_keys


def _ensure_run_active(run: Run) -> None:
    if run.status in TERMINAL_RUN_STATUS:
        raise NodeActionError(
            "run_terminal",
            f"运行已结束（{run.status}），不能再修改节点",
            hint="终态运行不可复活；如需重跑请新建运行",
            details={"run_id": run.id, "run_status": run.status},
        )


def _ensure_node_not_running(node: Node) -> None:
    if node.status == "running":
        raise NodeActionError(
            "node_running",
            "节点正在运行，不能执行该操作",
            hint="等待当前 attempt 结束，或先停止整个运行",
            details={"node_id": node.id, "node_key": node.node_key},
        )


def _resume_run(s: Session, run: Run) -> None:
    """动作之后把 run 从等待态唤回 running；仍有其他待决门时保持 waiting_human。"""
    if run.status in TERMINAL_RUN_STATUS:
        return
    # 会话是 autoflush=False：先 flush 让本事务内关闭门的状态对下面的 count 可见。
    s.flush()
    open_gates = (
        s.query(Gate).filter(Gate.run_id == run.id, Gate.status == "open").count()
    )
    if open_gates:
        return
    run.status = "running"
    run.error = None


# ---------------- 读取与投影 ----------------
def load_node(s: Session, node_id: int) -> Node:
    node = s.get(Node, node_id)
    if node is None:
        raise NodeActionError(
            "node_not_found",
            f"节点不存在：{node_id}",
            hint="确认节点 id 是否正确，或刷新运行快照后重试",
            details={"node_id": node_id},
        )
    return node


def downstream_nodes(nodes: list[Node], node_key: str) -> list[Node]:
    """依赖图上 node_key 的所有下游节点（含间接）。

    遍历顺序与 gates._downstream 完全一致（同一 BFS 实现），保证 decide_gate 的
    reject 重置行为逐字节等价。
    """
    by_key = {node.node_key: node for node in nodes}
    children = _children_by_key(nodes)
    result: list[str] = []
    stack = list(children.get(node_key, []))
    while stack:
        current = stack.pop()
        if current in result:
            continue
        result.append(current)
        stack.extend(children.get(current, []))
    return [by_key[key] for key in result if key in by_key]


def upstream_keys(node: Node, nodes: list[Node]) -> list[str]:
    """node 的所有上游节点 key（含间接，按依赖深度优先顺序）。"""
    by_key = {item.node_key: item for item in nodes}
    result: list[str] = []
    stack = [key for key in (node.deps_json or []) if key in by_key]
    while stack:
        current = stack.pop()
        if current in result:
            continue
        result.append(current)
        stack.extend(key for key in (by_key[current].deps_json or []) if key in by_key)
    return result


def collect_open_gates(s: Session, node_id: int) -> list[Gate]:
    return (
        s.query(Gate)
        .filter(Gate.node_id == node_id, Gate.status == "open")
        .order_by(Gate.id.asc())
        .all()
    )


def action_flags(node: Node, run: Run) -> dict:
    """按 §2 矩阵推导 can_*；run 终态时全部为 False。"""
    run_terminal = run is None or run.status in TERMINAL_RUN_STATUS
    status = node.status
    is_gate = node.type == "HUMAN_GATE"
    session = object_session(node)
    gate_open = bool(session and is_gate and collect_open_gates(session, node.id))

    def allowed(action: str) -> bool:
        if run_terminal:
            return False
        if action == "retry":
            # pending 尚未执行（改反馈请用 feedback）；running 不可重入
            if gate_open:
                return False
            return status not in ("pending", "running")
        if action == "reset":
            if gate_open:
                return False
            return status != "running"
        if action == "skip":
            # HUMAN_GATE 一律不可跳过；skipped 幂等重放
            if is_gate:
                return False
            return status in ("pending", "waiting_human", "skipped")
        if action == "feedback":
            return status not in ("running", "skipped")
        if action == "edit_config":
            if status == "running":
                return False
            # 人工门挂起时配置走门审批接口，节点级配置编辑不可用
            return not (is_gate and status == "waiting_human")
        return False

    return {
        "can_retry": allowed("retry"),
        "can_reset": allowed("reset"),
        "can_skip": allowed("skip"),
        "can_feedback": allowed("feedback"),
        "can_edit_config": allowed("edit_config"),
    }


def build_node_detail(s: Session, node: Node, *, messages: int = 40,
                      events: int = 20) -> dict:
    """GET /nodes/{id} 的响应投影：现有字段全部保留 + §3 新增字段。"""
    run = s.get(Run, node.run_id)
    attempts = (
        s.query(NodeAttempt)
        .filter(NodeAttempt.node_id == node.id)
        .order_by(NodeAttempt.attempt_no.asc())
        .all()
    )
    msg_limit = max(1, min(int(messages), 200))
    rows = (
        s.query(Message)
        .filter(Message.node_id == node.id)
        .order_by(Message.id.desc())
        .limit(msg_limit)
        .all()
    )
    nodes = (
        s.query(Node)
        .filter(Node.run_id == node.run_id)
        .order_by(Node.order_index.asc(), Node.id.asc())
        .all()
    )
    upstream_set = set(upstream_keys(node, nodes))
    upstream = [_node_ref(item) for item in nodes if item.node_key in upstream_set]
    downstream = downstream_nodes(nodes, node.node_key)
    downstream.sort(key=lambda item: (item.order_index or 0, item.id))
    open_gates = collect_open_gates(s, node.id)
    open_gate = None
    if open_gates:
        gate = open_gates[0]
        open_gate = {
            "id": gate.id,
            "node_id": gate.node_id,
            "node_key": node.node_key,
            "status": gate.status,
        }
    event_limit = max(0, min(int(events), 200))
    event_rows: list[Event] = []
    if event_limit:
        # 取最近 N 条再按 seq 升序输出：运行中节点的事件流更有用，且满足契约「seq 升序」。
        event_rows = (
            s.query(Event)
            .filter(Event.run_id == node.run_id, Event.node_id == node.id)
            .order_by(Event.seq.desc())
            .limit(event_limit)
            .all()
        )
        event_rows.reverse()
    artifact_kinds = [
        kind
        for (kind,) in (
            s.query(Artifact.kind)
            .filter(Artifact.run_id == node.run_id,
                    Artifact.created_by_node == node.node_key)
            .distinct()
            .order_by(Artifact.kind.asc())
            .all()
        )
    ]
    return {
        # —— 现有字段全部保留 ——
        "id": node.id, "run_id": node.run_id, "node_key": node.node_key,
        "type": node.type, "name": node.name, "phase": node.phase,
        "status": node.status, "deps": list(node.deps_json or []),
        "wave": node.wave, "config": node.config_json or {},
        "assignee": node.assignee_json or {},
        "attempts": [
            {
                "id": attempt.id, "attempt_no": attempt.attempt_no,
                "model_id": attempt.model_id, "status": attempt.status,
                "error": attempt.error,
                "started_at": _iso(attempt.started_at),
                "finished_at": _iso(attempt.finished_at),
                "tokens_in": attempt.tokens_in, "tokens_out": attempt.tokens_out,
            }
            for attempt in attempts
        ],
        "messages": [
            {
                "role": row.role,
                "content": (row.content_json or {}).get("content", ""),
                "tool_call_id": (row.content_json or {}).get("tool_call_id"),
            }
            for row in reversed(rows)
        ],
        # —— 新增字段（§3）——
        "max_attempts": node.max_attempts,
        "order_index": node.order_index,
        "run_status": run.status if run is not None else "",
        "upstream": upstream,
        "downstream": [_node_ref(item) for item in downstream],
        "open_gate": open_gate,
        **action_flags(node, run),
        "events": [
            {
                "seq": row.seq,
                "ts": _iso(row.ts),
                "type": row.type,
                "node_id": row.node_id,
                "node_key": node.node_key,
                "payload": row.payload_json or {},
            }
            for row in event_rows
        ],
        "artifact_kinds": artifact_kinds,
    }


# ---------------- 下游 / 门 ----------------
def reset_downstream(s: Session, run_id: str, node_key: str, *,
                     sync_tasks: bool = True) -> list[str]:
    """把 node_key 的下游节点（含间接）重置为 pending，返回被重置的 node_key 列表。

    只重置「已产出/在途」的状态（done/failed/waiting_human/running/ready），
    pending/skipped 原样保留；reset 的语义与 gates.decide_gate 的 reject 分支一致。
    """
    return _reset_downstream_impl(s, run_id, node_key, sync_tasks=sync_tasks)


def close_open_gates(s: Session, node_id: int, *, decided_by: str,
                     feedback: str | None) -> int:
    """关闭节点上所有待决门（复用 approved 状态 + decided_by 记录人工动作用途），返回关闭数。"""
    gates = collect_open_gates(s, node_id)
    if not gates:
        return 0
    now = datetime.now(timezone.utc)
    for gate in gates:
        gate.status = "approved"
        gate.decided_by = decided_by
        gate.decided_at = now
        if feedback is not None:
            gate.feedback = feedback
    return len(gates)


# ---------------- 状态迁移（事务内） ----------------
def _ensure_no_open_gate_bypass(s: Session, node: Node) -> None:
    """人工门上仍有待决门时，禁止用 retry/reset 绕过审批（质量门红线）。"""
    if node.type != "HUMAN_GATE":
        return
    if collect_open_gates(s, node.id):
        raise NodeActionError(
            "gate_open",
            "该人工门仍有待决审批，不能重试或重置",
            hint="请先通过门审批接口 approve/reject 处理，再操作节点",
            details={"node_id": node.id, "node_key": node.node_key},
        )


def apply_retry(node: Node, run: Run, *, feedback: str | None,
                reset_downstream: bool) -> dict:
    """重试：done/waiting_human/skipped → pending；关该节点 open 门；清升级/退避状态。"""
    s = _session_of(node)
    _ensure_run_active(run)
    _ensure_node_not_running(node)
    _ensure_no_open_gate_bypass(s, node)
    if node.status == "pending":
        raise NodeActionError(
            "node_not_retryable",
            "节点仍在待执行状态，无需重试",
            hint="如需补充反馈请用「注入反馈」；如需重跑请用「重置」",
            details={"node_id": node.id, "status": node.status},
        )
    before = node.status
    closed = close_open_gates(
        s, node.id, decided_by="human-retry", feedback=feedback
    )
    config = _clean_scheduling_config(node.config_json)
    if feedback:
        config["feedback"] = feedback
    node.config_json = config
    node.status = "pending"
    _resume_run(s, run)
    reset_keys = (
        _reset_downstream_impl(s, run.id, node.node_key) if reset_downstream else []
    )
    return {
        "before": before,
        "after": "pending",
        "reset_nodes": reset_keys,
        "closed_gates": closed,
    }


def apply_reset(s: Session, node: Node, run: Run, *, reset_downstream: bool = True) -> dict:
    """重置：清调度键 → pending（默认级联重置下游，避免旧产物污染）。"""
    _ensure_run_active(run)
    _ensure_node_not_running(node)
    _ensure_no_open_gate_bypass(s, node)
    before = node.status
    node.config_json = _clean_scheduling_config(node.config_json)
    node.status = "pending"
    # 保留门的原始反馈（升级原因）供审计，人工动作用 decided_by 区分。
    closed = close_open_gates(s, node.id, decided_by="human-reset", feedback=None)
    _resume_run(s, run)
    reset_keys = (
        _reset_downstream_impl(s, run.id, node.node_key) if reset_downstream else []
    )
    return {
        "before": before,
        "after": "pending",
        "reset_nodes": reset_keys,
        "closed_gates": closed,
    }


def apply_skip(s: Session, node: Node, run: Run, *, reason: str | None,
               cascade_downstream: bool = False) -> dict:
    """跳过：置 skipped（下游视同依赖完成）；关 open 门；同步 Task；必要时恢复 run。

    人工门（HUMAN_GATE）一律拒绝：跳过门等于绕过质量门（契约 §2 红线）。
    """
    if node.type == "HUMAN_GATE":
        raise NodeActionError(
            "gate_node_protected",
            "人工门节点不可跳过",
            hint="请通过门审批接口 approve/reject 处理该节点",
            details={"node_id": node.id, "node_key": node.node_key},
        )
    _ensure_run_active(run)
    _ensure_node_not_running(node)
    if node.status not in ("pending", "waiting_human", "skipped"):
        raise NodeActionError(
            "node_not_skippable",
            "当前状态不能跳过",
            hint="只有 pending / waiting_human / skipped 节点可以跳过",
            details={"node_id": node.id, "status": node.status},
        )
    before = node.status
    if before == "skipped":
        # 幂等重放：不改状态、不重复发事件（由 API 依据 changed 判定）
        return {
            "before": "skipped",
            "after": "skipped",
            "reset_nodes": [],
            "closed_gates": 0,
            "changed": False,
        }
    node.config_json = _clean_scheduling_config(node.config_json)
    node.status = "skipped"
    # 有 reason 时覆盖门的反馈，否则保留原始升级原因供审计。
    closed = close_open_gates(
        s, node.id, decided_by="human-skip", feedback=reason
    )
    _sync_task_status(s, run.id, node.node_key, "skipped")
    reset_keys = (
        _reset_downstream_impl(s, run.id, node.node_key) if cascade_downstream else []
    )
    _resume_run(s, run)
    return {
        "before": before,
        "after": "skipped",
        "reset_nodes": reset_keys,
        "closed_gates": closed,
        "changed": True,
    }


def apply_feedback(node: Node, run: Run, *, feedback: str) -> dict:
    """注入反馈：只写 config.feedback，下次 attempt 生效；不重跑、不改节点状态。"""
    _ensure_run_active(run)
    _ensure_node_not_running(node)
    if node.status == "skipped":
        raise NodeActionError(
            "node_not_retryable",
            "节点已跳过，不能注入反馈",
            hint="skipped 节点不会再次执行；如需恢复请先 retry/reset",
            details={"node_id": node.id, "status": node.status},
        )
    if node.status == "done":
        raise NodeActionError(
            "node_not_retryable",
            "节点已完成，注入反馈不会再生效",
            hint="如需按反馈重跑，请先 retry/reset",
            details={"node_id": node.id, "status": node.status},
        )
    config = dict(node.config_json or {})
    config["feedback"] = feedback
    node.config_json = config
    return {"before": node.status, "after": node.status, "reset_nodes": []}
