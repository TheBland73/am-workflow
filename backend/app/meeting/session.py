"""MeetingSession：一次执行里协议用得到的全部入口。"""
from __future__ import annotations

import asyncio
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
    _compaction_lock: asyncio.Lock | None = field(default=None, repr=False, compare=False)
    _lock_loop: object | None = field(default=None, repr=False, compare=False)

    def compaction_lock(self) -> asyncio.Lock:
        """会议上下文压缩的互斥锁：并行发言串行进出，后来者直接复用已存下的摘要。

        锁按正在运行的事件循环惰性创建：测试里同一场会议常用多次 asyncio.run，
        跨循环复用一个锁会报"绑定到另一个事件循环"。
        """
        loop = asyncio.get_running_loop()
        if self._compaction_lock is None or self._lock_loop is not loop:
            self._compaction_lock = asyncio.Lock()
            self._lock_loop = loop
        return self._compaction_lock

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
        if next_step is not None and next_step not in steps:
            from app.engine.runners import NodeFailure  # 延迟导入：会话模块不在导入期牵动引擎

            raise NodeFailure(f"协议指定了不存在的下一步：{next_step}", fatal=True)
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
                   {"meeting_id": self.meeting_id, "phase": target,
                    "round": self.meeting_round()})      # 事件契约里的轮次是会议内部轮次

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
        """取出带着反馈续开时存下的反馈，移到 reopen_feedback 并记下序号。

        同一条反馈不再当作新反馈；reopen_feedback 在协议真正处理完它之后才清空。
        "清空待处理反馈"与"把它交给协议"在同一个事务里完成：中途崩溃不会两头落空。
        """
        state = self.state()
        feedback = str(state.get("pending_feedback") or "").strip()
        if not feedback:
            return None
        store.update_meeting(self.meeting_id, state_json={
            **state, "pending_feedback": "", "reopen_feedback": feedback,
            "handled_feedback_seq": state.get("seen_feedback_seq")})
        return feedback

    def decision(self) -> dict | None:
        """读取当前等待的确认点的决定。读到后不立即标记：由协议在步骤成功返回前调用 ack_decision。

        这样同一步骤里"读到决定之后的动作"失败重试时，人的决定还在，不会被重新开一扇门。
        """
        state = self.state()
        gate_id = state.get("awaiting_gate_id")
        if not gate_id or state.get("handled_gate_id") == gate_id:
            return None
        decision = get_decision(gate_id)
        if decision is None:
            return None
        return decision

    def ack_decision(self, decision: dict | None = None) -> None:
        """确认这个确认点的决定已经被消费掉（步骤已经按它做完事）。"""
        state = self.state()
        gate_id = (decision or {}).get("gate_id") or state.get("awaiting_gate_id")
        if gate_id and state.get("handled_gate_id") != gate_id:
            self.set_state(handled_gate_id=gate_id)

    async def open_checkpoint(self, kind: str, *, title: str, description: str,
                              payload: dict | None = None) -> dict:
        return await open_checkpoint(self, kind, title=title, description=description,
                                     payload=payload)

    # ---------- 会议自身、运行级配置与背景材料 ----------
    def meeting(self) -> dict:
        return store.get_meeting(self.meeting_id)

    def meeting_round(self) -> int:
        """会议内部的轮次，例如讨论的交锋轮次。它与节点级的回流轮次 round_no() 不是一回事。"""
        return int(self.meeting()["round"] or 1)

    def reset_builder(self) -> None:
        """新一轮开始时调用。账本快照每轮更新一次，这是预期内的变化，不该报冻结段被改。"""
        self.builder = ContextBuilder()

    def run_config(self) -> dict:
        from app.engine.params import run_of

        return dict(run_of(self.run_id)["config"] or {})

    def decider(self, checkpoint: str) -> str:
        """某个确认点由谁判定：human / program / program_fallback / proxy / auto / model。"""
        from app.engine.params import decider_for, definition_of

        return decider_for(self.run_config(), definition_of(self.run_id), checkpoint)

    def materials(self) -> tuple[str, bool]:
        """背景材料（已按预算截好的一段文本）和它是否被截断。"""
        from app.config import settings
        from app.engine.materials import read_materials

        return read_materials(self.run_id, settings.materials_context_tokens)
