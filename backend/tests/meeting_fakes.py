"""会议测试的替身：让模型调用变成按调用标识查表。"""
from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

from app.models.types import Usage

CALL_TAG = re.compile(r"【调用类型：([a-z_]+)】")
MODEL = SimpleNamespace(id=1, model_name="mock-fast", display_name="Mock Fast",
                        context_window=32768, max_output_tokens=4096, cap_tools=True)


def install(monkeypatch, replies: dict, calls: list | None = None):
    """replies：调用标识 → 模型回复，或者一个"完整提示文本 → 回复"的函数。
    calls：若给出，记录每次调用的标识与完整提示文本（系统提示、用户消息、各段依次拼接）。"""
    from app.meeting import calls as calls_module

    async def fake_run_agent(**kwargs):
        text = "\n".join([kwargs["system_prompt"], kwargs.get("user_prompt") or "",
                          *(m.content for m in (kwargs.get("extra_messages") or []))])
        match = CALL_TAG.search(text)
        kind = match.group(1) if match else ""
        if calls is not None:
            calls.append({"kind": kind, "text": text})
        if kind not in replies:
            raise AssertionError(f"测试没有为调用类型 {kind!r} 准备回复")
        reply = replies[kind]
        content = reply(text) if callable(reply) else reply
        return SimpleNamespace(content=content, usage=Usage(100, 50, 60), steps=1,
                               stopped_by="final")

    monkeypatch.setattr(calls_module, "run_agent", fake_run_agent)
    monkeypatch.setattr(calls_module, "_model_for", lambda participant: MODEL)
    try:
        from app.engine import judge as judge_module
    except ModuleNotFoundError:                 # 判定模块还没实现时（任务初期）忽略
        judge_module = None
    if judge_module is not None:
        monkeypatch.setattr(judge_module, "run_agent", fake_run_agent)


def drive(run_id: str, node_id: int, answer, *, idea: str = "") -> dict:
    """反复执行会议节点，自动回答路上遇到的人工确认点，直到会议完成或发起回流。

    answer(确认点的门载荷) -> (action, data, feedback)。每次执行都按节点当前的状态新建上下文，
    与编排器恢复节点时的做法一致。
    """
    from app.db.base import session_scope
    from app.db.models import Node
    from app.engine.gates import decide_gate
    from app.engine.runners import NodeContext
    from app.meeting.runner import run_meeting

    def context() -> NodeContext:
        with session_scope() as s:
            node = s.get(Node, node_id)
            return NodeContext(
                run_id=run_id, node_id=node_id, node_key=node.node_key, type=node.type,
                name=node.name, phase=node.phase, config=dict(node.config_json or {}),
                assignee=dict(node.assignee_json or {}), attempt_id=0, attempt_no=1,
                feedback=(node.config_json or {}).get("feedback"), idea=idea)

    while True:
        result = asyncio.run(run_meeting(context()))
        if result.get("done") or result.get("reroute"):
            return result
        action, data, feedback = answer(result["checkpoint"])
        decide_gate(gate_id=result["checkpoint"]["id"], action=action,
                    feedback=feedback, data=data)
