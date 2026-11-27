"""节点详情与人工动作（查看 / 重试 / 重置 / 跳过 / 注入反馈）。

动作矩阵与状态迁移在 ``engine.node_actions``（公共模块 1）；本文件只负责 schema 校验、
领域异常 → 结构化 HTTPException 映射，以及「短事务提交后 emit + kick」的接线。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.schemas import NodeFeedbackIn, NodeResetIn, NodeSkipIn, RetryIn
from app.db.base import session_scope
from app.db.models import Node, Run
from app.engine.events import emit
from app.engine.node_actions import (
    NodeActionError,
    apply_feedback,
    apply_reset,
    apply_retry,
    apply_skip,
    build_node_detail,
    load_node,
)
from app.engine.orchestrator import orchestrator

router = APIRouter(tags=["nodes"])

# 契约 §4 错误码 → HTTP 状态码。
_ACTION_ERROR_STATUS = {
    "node_not_found": 404,
    "run_not_found": 404,
    "node_running": 409,
    "node_not_retryable": 409,
    "node_not_skippable": 409,
    "gate_node_protected": 400,
    "run_terminal": 409,
    "status_conflict": 409,
    "feedback_required": 400,
}


def _action_http(exc: NodeActionError) -> HTTPException:
    """领域异常 → 统一结构化错误体：{"code","message","hint","details"}。"""
    return HTTPException(
        _ACTION_ERROR_STATUS.get(exc.code, 400),
        detail={
            "code": exc.code,
            "message": exc.message,
            "hint": exc.hint,
            "details": exc.details,
        },
    )


def _load_target(s, node_id: int, expected_status: str | None = None):
    """加载节点 + 所属 run；expected_status 是动作的幂等保护（不一致 → 409）。"""
    try:
        node = load_node(s, node_id)
    except NodeActionError as exc:
        raise _action_http(exc) from exc
    run = s.get(Run, node.run_id)
    if run is None:
        raise _action_http(
            NodeActionError(
                "run_not_found",
                f"run 不存在：{node.run_id}",
                hint="节点所属运行已被清理，刷新运行快照后重试",
                details={"run_id": node.run_id, "node_id": node.id},
            )
        )
    if expected_status is not None and node.status != expected_status:
        raise _action_http(
            NodeActionError(
                "status_conflict",
                f"节点状态已变化：期望 {expected_status}，实际 {node.status}",
                hint="刷新节点详情后重试；省略 expected_status 可跳过幂等保护",
                details={"expected_status": expected_status,
                         "actual_status": node.status},
            )
        )
    return node, run


@router.get("/nodes/{node_id}")
def node_detail(node_id: int, messages: int = 40, events: int = 20) -> dict:
    """节点详情：现有字段全部保留 + §3 增量字段（只加不改）。"""
    with session_scope() as s:
        node = s.get(Node, node_id)
        if node is None:
            # 旧端点保留字符串 404（契约允许既有错误体不变）。
            raise HTTPException(404, "节点不存在")
        return build_node_detail(s, node, messages=messages, events=events)


@router.post("/nodes/{node_id}/retry")
async def retry_node(node_id: int, payload: RetryIn | None = None) -> dict:
    """人工重试：done/waiting_human/skipped → pending，可带反馈、可级联重置下游。"""
    feedback = (payload.feedback or "").strip() if payload and payload.feedback else None
    reset_down = bool(payload.reset_downstream) if payload else False
    expected = payload.expected_status if payload else None
    with session_scope() as s:
        node, run = _load_target(s, node_id, expected)
        try:
            result = apply_retry(node, run, feedback=feedback,
                                 reset_downstream=reset_down)
        except NodeActionError as exc:
            raise _action_http(exc) from exc
        run_id, node_key = run.id, node.node_key
        reset_nodes = result["reset_nodes"]
    try:
        await emit(run_id, "node_retried", node_id, {
            "node_key": node_key,
            "feedback": feedback or "",
            "actor": "human",
            "reset_downstream": reset_down,
            "reset_nodes": reset_nodes,
        })
    finally:
        orchestrator.kick(run_id)
    return {"node_id": node_id, "status": "pending", "run_id": run_id,
            "reset_nodes": reset_nodes}


@router.post("/nodes/{node_id}/reset")
async def reset_node(node_id: int, payload: NodeResetIn | None = None) -> dict:
    """重置：清 feedback/retry_at/retry_count/escalation → pending（默认级联重置下游）。"""
    reset_down = payload.reset_downstream if payload else True
    expected = payload.expected_status if payload else None
    with session_scope() as s:
        node, run = _load_target(s, node_id, expected)
        try:
            result = apply_reset(s, node, run, reset_downstream=reset_down)
        except NodeActionError as exc:
            raise _action_http(exc) from exc
        run_id, node_key = run.id, node.node_key
        reset_nodes = result["reset_nodes"]
    try:
        await emit(run_id, "node_reset", node_id, {
            "node_key": node_key,
            "reset_nodes": reset_nodes,
            "actor": "human",
        })
    finally:
        orchestrator.kick(run_id)
    return {"node_id": node_id, "status": "pending", "run_id": run_id,
            "reset_nodes": reset_nodes}


@router.post("/nodes/{node_id}/skip")
async def skip_node(node_id: int, payload: NodeSkipIn | None = None) -> dict:
    """跳过：skipped + 关闭该节点 open 门 + 同步 dev Task + 必要时恢复 run。"""
    reason = ((payload.reason or "").strip() or None) if payload else None
    cascade = bool(payload.cascade_downstream) if payload else False
    expected = payload.expected_status if payload else None
    with session_scope() as s:
        node, run = _load_target(s, node_id, expected)
        try:
            result = apply_skip(s, node, run, reason=reason,
                                cascade_downstream=cascade)
        except NodeActionError as exc:
            raise _action_http(exc) from exc
        run_id, node_key = run.id, node.node_key
        status = result["after"]
        closed_gates = result["closed_gates"]
        reset_nodes = result["reset_nodes"]
        changed = result.get("changed", True)
    # 幂等重放（已是 skipped）不重复发事件，也不再唤醒调度器。
    if changed:
        try:
            await emit(run_id, "node_skipped", node_id, {
                "node_key": node_key,
                "reason": reason or "",
                "cascade": cascade,
                "actor": "human",
            })
        finally:
            orchestrator.kick(run_id)
    return {"node_id": node_id, "status": status, "run_id": run_id,
            "closed_gates": closed_gates, "reset_nodes": reset_nodes}


@router.post("/nodes/{node_id}/feedback")
async def inject_feedback(node_id: int, payload: NodeFeedbackIn | None = None) -> dict:
    """注入反馈：只写 config.feedback（下次 attempt 生效），不重跑、不唤醒调度器。"""
    text = ((payload.feedback if payload else "") or "").strip()
    if not text:
        raise _action_http(
            NodeActionError(
                "feedback_required",
                "反馈内容不能为空",
                hint="请填写 1..4000 字的修正反馈，将在该节点下次执行时注入",
                details={"node_id": node_id},
            )
        )
    with session_scope() as s:
        node, run = _load_target(s, node_id)
        try:
            result = apply_feedback(node, run, feedback=text)
        except NodeActionError as exc:
            raise _action_http(exc) from exc
        run_id, node_key, status = run.id, node.node_key, result["after"]
    await emit(run_id, "node_feedback_injected", node_id, {
        "node_key": node_key,
        "feedback": text,
        "actor": "human",
    })
    return {"node_id": node_id, "status": status, "feedback": text}
