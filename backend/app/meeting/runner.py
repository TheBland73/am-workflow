"""会议运行器的入口：取或建会议、续开或重开的判定、把协议的结果翻译成节点结果。

进入一个会议节点时（详设 §6.2）：
- 该节点还没有会议 → 新建；
- 节点带着要复议的反馈（人工驳回、定点回流、用户带反馈的重试）→ 续开，反馈存进会议状态，
  由协议在入口处取走；每条反馈只处理一次。失败后的自动重试也带反馈，但只原地续开；
- 节点带着回流时写给下一轮的自我提示（reroute_self）→ 同样作为待处理反馈交给协议；
- 没有反馈：会议在进行中或等待返工 → 从当前步骤续开；
  会议在挂起且确认点已有决定 → 读取决定后续开；
  会议在挂起、确认点还开着 → 什么都不做，继续等这个决定；
  其余（已完成，或挂起但确认点被重置关闭）→ 旧会议归档，新建一场。
"""
from __future__ import annotations

from app.db.base import session_scope
from app.db.models import (MEETING_ABANDONED, MEETING_ACTIVE, MEETING_COMPLETED,
                           MEETING_SUSPENDED, MEETING_WAITING_REWORK, Gate, Node, Run)
from app.engine.events import emit
from app.engine.feedback import REOPEN_KINDS
from app.engine.gates import gate_payload
from app.engine.runners import NodeContext, NodeFailure
from app.meeting import store
from app.meeting.checkpoints import get_decision
from app.meeting.protocols.base import get_protocol
from app.meeting.session import RUNTIME_KEYS, MeetingSession


def _checkpoint_decided(state: dict) -> bool:
    gate_id = state.get("awaiting_gate_id")
    return bool(gate_id) and state.get("handled_gate_id") != gate_id and get_decision(gate_id) is not None


def _checkpoint_open(state: dict) -> bool:
    """会议在等的那个确认点是否还开着：既没有决定，也没有被重置关掉。"""
    gate_id = state.get("awaiting_gate_id")
    if not gate_id:
        return False
    with session_scope() as s:
        gate = s.get(Gate, gate_id)
        return gate is not None and gate.status == "open"


def _keep_waiting(ctx: NodeContext, meeting_id: int, state: dict) -> dict:
    """节点在确认点还开着时又被调度了一次：把节点与运行放回"等待人工"，原样返回那个确认点。"""
    with session_scope() as s:
        node = s.get(Node, ctx.node_id)
        if node is not None:
            node.status = "waiting_human"
        run = s.get(Run, ctx.run_id)
        if run is not None:
            run.status = "waiting_human"
        body = gate_payload(s, s.get(Gate, state["awaiting_gate_id"]))
    return {"suspended": True, "meeting_id": meeting_id, "checkpoint": body, "summary": ""}


def _enter(ctx: NodeContext, protocol: str) -> tuple[int, str]:
    """返回 (会议 id, 进入原因)。进入原因取 new / feedback / resume / decided / waiting。

    一条反馈算不算"新的"，要同时满足三点：来路属于 REOPEN_KINDS；节点配置里的序号
    （feedback_seq）与会议状态里已处理的序号不同；内容非空。
    """
    config = ctx.config or {}
    meeting_config = {k: v for k, v in config.items() if k not in RUNTIME_KEYS}
    existing = store.latest_meeting_for_node(ctx.node_id)

    def new() -> tuple[int, str]:
        meeting_id = store.create_meeting(
            run_id=ctx.run_id, node_id=ctx.node_id, node_key=ctx.node_key, protocol=protocol,
            charter={}, config=meeting_config)
        return meeting_id, "new"

    if existing is None:
        return new()
    feedback = str(config.get("feedback") or "").strip()
    seq = config.get("feedback_seq")
    # 要复议的来路，以及发起回流的节点写给下一轮的自我提示
    reopen = (bool(feedback)
              and config.get("feedback_kind") in (*REOPEN_KINDS, "reroute_self")
              and seq != existing["state"].get("handled_feedback_seq"))
    if reopen:
        store.update_meeting(
            existing["id"], status=MEETING_ACTIVE,
            state_json={**existing["state"], "pending_feedback": feedback,
                        "feedback_kind": config.get("feedback_kind"),
                        "seen_feedback_seq": seq})
        return existing["id"], "feedback"
    if existing["status"] in (MEETING_ACTIVE, MEETING_WAITING_REWORK):
        return existing["id"], "resume"
    if existing["status"] == MEETING_SUSPENDED:
        if _checkpoint_decided(existing["state"]):
            return existing["id"], "decided"
        if _checkpoint_open(existing["state"]):
            # 确认点还开着、还没有决定：继续等它。只有确认点被重置关闭才归档重开——
            # 否则一次多余的调度（例如有人重试了正在等待的节点）就会丢掉整场会议。
            return existing["id"], "waiting"
    store.update_meeting(existing["id"], status=MEETING_ABANDONED)
    return new()


async def run_meeting(ctx: NodeContext) -> dict:
    """MEETING 节点的执行入口。不改节点的最终状态，交给编排器。"""
    protocol = get_protocol((ctx.config or {}).get("protocol"))
    meeting_id, reason = _enter(ctx, protocol.name)
    if reason == "waiting":
        return _keep_waiting(ctx, meeting_id, store.get_state(meeting_id))
    mtg = MeetingSession(meeting_id=meeting_id, ctx=ctx)
    if reason == "new":
        await emit(ctx.run_id, "meeting_started", ctx.node_id,
                   {"meeting_id": meeting_id, "node_key": ctx.node_key, "protocol": protocol.name})
    else:
        await emit(ctx.run_id, "meeting_resumed", ctx.node_id,
                   {"meeting_id": meeting_id, "reason": reason})
    if reason != "new":
        store.update_meeting(meeting_id, status=MEETING_ACTIVE)
    from app.meeting.calls import CallOutputError

    try:
        result = await protocol.run(mtg)
    except CallOutputError as exc:
        # 结构化输出不合格：按普通节点失败处理，编排器会带反馈重试；重入时从当前步骤续开
        raise NodeFailure(f"模型输出不符合要求：{exc}") from exc
    if result.action == "reroute" and not result.reroute:
        raise NodeFailure("协议要求回流但没有给出回流指令", fatal=True)
    if result.action not in ("suspend", "reroute", "done"):
        raise NodeFailure(f"协议返回了未知动作：{result.action}", fatal=True)
    if result.action == "suspend":
        return {"suspended": True, "meeting_id": meeting_id, "checkpoint": result.checkpoint,
                "summary": result.summary}
    if result.action == "reroute":
        store.update_meeting(meeting_id, status=MEETING_WAITING_REWORK)
        return {"reroute": result.reroute, "meeting_id": meeting_id, "summary": result.summary}
    store.update_meeting(meeting_id, status=MEETING_COMPLETED, finished_at=True)
    await emit(ctx.run_id, "meeting_finished", ctx.node_id, {"meeting_id": meeting_id})
    return {"meeting_id": meeting_id, **result.extra, "summary": result.summary, "done": True}
