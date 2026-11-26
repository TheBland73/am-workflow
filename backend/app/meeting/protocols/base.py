"""协议基类：步进循环与步骤的返回值。"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.meeting.session import MeetingSession


@dataclass
class StepResult:
    action: str                                   # advance | suspend | reroute | done
    next: str | None = None                       # advance 时可指定下一步
    checkpoint: dict | None = None                # suspend：已创建的确认点
    reroute: dict | None = None                   # reroute：定点回流指令
    summary: str = ""
    extra: dict = field(default_factory=dict)     # done：随节点结果带回的内容，如工件记录


def advance(next: str | None = None) -> StepResult:
    return StepResult(action="advance", next=next)


def suspend(checkpoint: dict, summary: str = "") -> StepResult:
    return StepResult(action="suspend", checkpoint=checkpoint, summary=summary)


def reroute(targets: list[str], feedback: str, *, self_feedback: str | None = None,
            summary: str = "", extra_round: bool = False) -> StepResult:
    """发起定点回流。

    extra_round 为真表示：回流轮次已经用完，人在转人工的确认点上要求"再评一轮"，
    编排器据此把轮次上限放宽一轮。协议一般不直接调用它，而是用 reroute_or_escalate()。
    """
    return StepResult(action="reroute", summary=summary,
                      reroute={"targets": list(targets), "feedback": feedback,
                               "self_feedback": self_feedback, "extra_round": extra_round})


async def reroute_or_escalate(mtg: "MeetingSession", targets: list[str], feedback: str, *,
                              self_feedback: str | None = None, title: str,
                              description: str, summary: str = "") -> StepResult | None:
    """发起定点回流；回流轮次已用完时，先由人在一个确认点上决定怎么办。

    返回值就是调用它的步骤该返回的东西：
    - 轮次没用完：回流指令；
    - 轮次已用完、人还没决定：挂起（确认点类型是 escalation）；
    - 轮次已用完、人驳回并写了意见：带 extra_round 的回流指令，意见并入反馈，编排器放宽一轮；
    - 返回 None：人在确认点上选择通过，即接受现状、不再回流。调用它的步骤据此往下走。
    """
    if mtg.round_no() < mtg.max_rounds():
        return reroute(targets, feedback, self_feedback=self_feedback, summary=summary)
    decision = mtg.decision()
    if decision is None:
        return suspend(await mtg.open_checkpoint(
            "escalation", title=title, description=description,
            payload={"targets": list(targets), "feedback": feedback[:2000],
                     "round": mtg.round_no(), "max_rounds": mtg.max_rounds()}))
    if decision["action"] == "approve":
        return None
    note = str(decision.get("feedback") or "").strip()
    if note:
        feedback = f"{feedback}\n\n用户在确认点上的意见：{note}"
    return reroute(targets, feedback, self_feedback=self_feedback, summary=summary,
                   extra_round=True)


def done(summary: str, **extra: Any) -> StepResult:
    return StepResult(action="done", summary=summary, extra=extra)


class MeetingProtocol:
    name: str = ""
    steps: tuple[str, ...] = ()

    async def on_enter(self, mtg: "MeetingSession") -> None:
        """进入协议时调用一次。带着反馈续开的协议在这里用 mtg.consume_feedback() 取走反馈。"""

    async def run(self, mtg: "MeetingSession") -> StepResult:
        """从当前步骤跑到挂起、回流或完成。每一步返回 advance 时推进到下一步。"""
        await self.on_enter(mtg)
        while True:
            step = mtg.current_step(self.steps)
            handler = getattr(self, f"step_{step}")
            result = await handler(mtg)
            if result.action != "advance":
                return result
            await mtg.advance(result.next, self.steps)


_PROTOCOLS: dict[str, type["MeetingProtocol"]] = {}


def register_protocol(cls: type["MeetingProtocol"]) -> type["MeetingProtocol"]:
    """把协议类登记进注册表。协议模块在导入时用它装饰自己的协议类。"""
    _PROTOCOLS[cls.name] = cls
    return cls


# 协议名就是 protocols 包里的模块名
PROTOCOL_MODULES = ("discussion", "review", "confirm")


def get_protocol(name: str) -> "MeetingProtocol":
    """取协议的实例。协议模块按名字惰性导入，模块里的类用 register_protocol 登记。"""
    cls = _PROTOCOLS.get(name or "")
    if cls is None and name in PROTOCOL_MODULES:
        try:
            importlib.import_module(f"app.meeting.protocols.{name}")
        except ModuleNotFoundError as exc:
            from app.engine.runners import NodeFailure

            raise NodeFailure(f"会议协议 {name} 还没有实现", fatal=True) from exc
        cls = _PROTOCOLS.get(name)
    if cls is None:
        from app.engine.runners import NodeFailure  # 延迟导入：协议基类不在导入期牵动引擎

        raise NodeFailure(f"未知的会议协议：{name}", fatal=True)
    return cls()
