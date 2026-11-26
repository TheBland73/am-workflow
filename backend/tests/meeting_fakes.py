"""会议测试的替身：让模型调用变成按调用标识查表。"""
from __future__ import annotations

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
