"""MeetingSession：一次执行里协议用得到的全部入口。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.conversation.builder import ContextBuilder
from app.engine.events import emit
from app.meeting import store
from app.meeting.checkpoints import get_decision, open_checkpoint

if TYPE_CHECKING:
    from app.engine.runners import NodeContext

# 节点配置里由编排器维护的键，不属于会议的生效参数
RUNTIME_KEYS = ("feedback", "feedback_kind", "feedback_seq", "retry_at", "retry_count",
                "escalation", "escalation_reason", "round")


@dataclass
class MeetingSession:
    meeting_id: int
    ctx: "NodeContext"
    builder: ContextBuilder = field(default_factory=ContextBuilder)

    @property
    def run_id(self) -> str:
        return self.ctx.run_id

    @property
    def node_id(self) -> int:
        return self.ctx.node_id

    @property
    def node_key(self) -> str:
        return self.ctx.node_key

    def config(self) -> dict:
        return self.ctx.config or {}

    def params(self) -> dict:
        """生效参数：模板默认值与运行级配置在新建运行时已合并进节点的 params。"""
        return dict(self.config().get("params") or {})

    def round_no(self) -> int:
        """节点级轮次：回流时由编排器加一。"""
        try:
            return max(1, int(self.config().get("round") or 1))
        except (TypeError, ValueError):
            return 1

    # ---------- 状态与步进 ----------
    def state(self) -> dict:
        return store.get_state(self.meeting_id)

    def set_state(self, **changes) -> None:
        state = self.state()
        state.update(changes)
        store.set_state(self.meeting_id, state)

    def current_step(self, steps: tuple[str, ...]) -> str:
        phase = store.get_meeting(self.meeting_id)["phase"]
        return phase if phase in steps else steps[0]

    async def advance(self, next_step: str | None, steps: tuple[str, ...]) -> None:
        index = steps.index(self.current_step(steps))
        if next_step is None and index + 1 >= len(steps):
            # 协议写错了：重试也不会好，所以按不可重试处理
            from app.engine.runners import NodeFailure  # 延迟导入：会话模块不在导入期牵动引擎

            raise NodeFailure(
                f"协议的最后一步「{steps[index]}」返回了 advance：最后一步必须返回 done、suspend 或 reroute",
                fatal=True)
        target = next_step or steps[index + 1]
        store.update_meeting(self.meeting_id, phase=target)
        await emit(self.run_id, "meeting_phase", self.node_id,
                   {"meeting_id": self.meeting_id, "phase": target, "round": self.round_no()})

    # ---------- 参会者、反馈、确认点 ----------
    def participants(self, mode: str | None = None) -> list[dict]:
        rows = store.list_participants(self.meeting_id)
        return [row for row in rows if mode is None or row["mode"] == mode]

    def max_rounds(self) -> int:
        """节点级的回流轮次上限。用完时发起回流的协议要转人工（见 reroute_or_escalate）。"""
        try:
            return max(1, int(self.config().get("max_rounds") or 3))
        except (TypeError, ValueError):
            return 3

    def consume_feedback(self) -> str | None:
        """取出并清空带着反馈续开时存下的反馈，并记下它的序号：同一条反馈不再当作新反馈。"""
        state = self.state()
        feedback = str(state.get("pending_feedback") or "").strip()
        if feedback:
            self.set_state(pending_feedback="", handled_feedback_seq=state.get("seen_feedback_seq"))
        return feedback or None

    def decision(self) -> dict | None:
        """读取当前等待的确认点的决定，读到后标记为已处理。"""
        state = self.state()
        gate_id = state.get("awaiting_gate_id")
        if not gate_id or state.get("handled_gate_id") == gate_id:
            return None
        decision = get_decision(gate_id)
        if decision is None:
            return None
        self.set_state(handled_gate_id=gate_id)
        return decision

    async def open_checkpoint(self, kind: str, *, title: str, description: str,
                              payload: dict | None = None) -> dict:
        return await open_checkpoint(self, kind, title=title, description=description,
                                     payload=payload)
