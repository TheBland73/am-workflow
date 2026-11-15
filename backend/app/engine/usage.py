"""Token 计量与 run 级预算（设计规格 §5.2）。"""
from __future__ import annotations

from sqlalchemy import func

from app.db.base import session_scope
from app.db.models import Node, NodeAttempt, Run
from app.models.types import Usage


class BudgetExceeded(RuntimeError):
    pass


def add_attempt_usage(attempt_id: int, usage: Usage) -> None:
    with session_scope() as s:
        attempt = s.get(NodeAttempt, attempt_id)
        if attempt:
            attempt.tokens_in += usage.input_tokens
            attempt.tokens_out += usage.output_tokens


def consume_run_budget(run_id: str, usage: Usage) -> int:
    """从预算扣减，返回剩余；预算耗尽时抛 BudgetExceeded（转人工）。"""
    with session_scope() as s:
        run = s.get(Run, run_id)
        if not run:
            return 0
        run.budget_tokens -= usage.total
        remaining = run.budget_tokens
    if remaining <= 0:
        raise BudgetExceeded(f"运行 token 预算已耗尽（剩余 {remaining}）")
    return remaining


def run_usage(run_id: str) -> dict:
    with session_scope() as s:
        used = (
            s.query(
                func.coalesce(
                    func.sum(NodeAttempt.tokens_in) + func.sum(NodeAttempt.tokens_out), 0
                )
            )
            .join(Node, Node.id == NodeAttempt.node_id)
            .filter(Node.run_id == run_id)
            .scalar()
            or 0
        )
        run = s.get(Run, run_id)
        remaining = run.budget_tokens if run else 0
    return {"used": int(used), "remaining": remaining}
