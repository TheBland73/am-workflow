"""Agent 循环：流式输出 → 工具调用 → 观测 → 再推理（设计规格 §5.2）。"""
from __future__ import annotations

import json
from dataclasses import dataclass

from app.config import settings
from app.conversation.compact import make_summarizer, maybe_compact
from app.conversation.policy import DEFAULT_POLICY
from app.conversation.store import record_compaction
from app.db.base import session_scope
from app.db.models import Message as MessageRow
from app.engine.events import emit, emit_token
from app.engine.usage import add_attempt_usage, consume_run_budget
from app.models.pool import ModelInfo, pool
from app.models.types import GenParams, Message, StreamEvent, Usage
from app.tools.registry import Toolbox, ToolOutcome


class AgentExceeded(RuntimeError):
    pass


@dataclass
class AgentResult:
    content: str
    usage: Usage
    steps: int
    stopped_by: str


def _save_message(node_id: int, attempt_id: int, role: str, content: str,
                  tool_calls: list | None = None, tool_call_id: str | None = None) -> None:
    with session_scope() as s:
        s.add(
            MessageRow(
                node_id=node_id,
                attempt_id=attempt_id,
                role=role,
                content_json={
                    "content": content,
                    "tool_calls": tool_calls or [],
                    "tool_call_id": tool_call_id,
                },
            )
        )


def _brief(value, limit: int = 600) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[:limit] + "…"


async def _call_stream(model: ModelInfo, messages: list[Message], schemas: list[dict] | None,
                       params: GenParams, *, run_id: str, node_id: int,
                       token_extra: dict | None = None) -> tuple[Message, Usage]:
    adapter = pool.adapter_for(model)
    provider = model.provider
    assert provider is not None
    sem = await pool.slot(provider)
    usage = Usage()
    message: Message | None = None
    async with sem:
        async for event in adapter.stream(messages, schemas, params):
            if event.kind == "text_delta":
                if settings.stream_tokens:
                    emit_token(run_id, node_id, event.text, token_extra)
            elif event.kind == "usage" and event.usage:
                usage = event.usage
            elif event.kind == "done":
                message = event.message
                if event.usage and event.usage.total:
                    usage = event.usage
    if message is None:
        raise AgentExceeded("模型流意外结束，未收到完整回复")
    return message, usage


async def run_agent(*, run_id: str, node_id: int, attempt_id: int, model: ModelInfo,
                    system_prompt: str, user_prompt: str, toolbox: Toolbox | None,
                    max_steps: int = 12, temperature: float = 0.4, quiet: bool = False,
                    token_extra: dict | None = None) -> AgentResult:
    if toolbox is not None and not model.cap_tools:
        return await _run_json_protocol(
            run_id=run_id, node_id=node_id, attempt_id=attempt_id, model=model,
            system_prompt=system_prompt, user_prompt=user_prompt, toolbox=toolbox,
            max_steps=max_steps, temperature=temperature,
        )

    schemas = toolbox.schemas() if toolbox else None
    messages = [
        Message(role="system", content=system_prompt),
        Message(role="user", content=user_prompt),
    ]
    total = Usage()
    for step in range(1, max_steps + 1):
        params = GenParams(
            model=model.model_name,
            temperature=temperature,
            max_tokens=min(4096, model.max_output_tokens or 4096),
        )
        if len(messages) > 2:
            # 压缩钩子：占用不到阈值时原样返回（此时不会留下任何记录）
            compacted, before_tokens, after_tokens = await maybe_compact(
                run_id=run_id, node_id=node_id, model=model,
                policy=DEFAULT_POLICY, messages=messages,
                summarize=make_summarizer(run_id=run_id, attempt_id=attempt_id, model=model))
            if compacted is not messages:
                await record_compaction(
                    run_id=run_id, node_id=node_id, attempt_id=attempt_id, scope="agent_loop",
                    summary=compacted[1].content, before_tokens=before_tokens,
                    after_tokens=after_tokens)
                messages = compacted
        if not quiet:
            await emit(run_id, "node_log", node_id,
                       {"level": "info",
                        "message": f"第 {step}/{max_steps} 步：请求模型 {model.display_name}"})
        message, usage = await _call_stream(model, messages, schemas, params,
                                            run_id=run_id, node_id=node_id,
                                            token_extra=token_extra)
        total.input_tokens += usage.input_tokens
        total.output_tokens += usage.output_tokens
        add_attempt_usage(attempt_id, usage, model_id=model.id)
        consume_run_budget(run_id, usage)
        if usage.total:
            await emit(run_id, "node_log", node_id,
                       {"level": "debug",
                        "message": f"tokens in/out = {usage.input_tokens}/{usage.output_tokens}"})
        _save_message(node_id, attempt_id, "assistant", message.content,
                      [{"id": c.id, "name": c.name, "args": c.args} for c in message.tool_calls])
        messages.append(message)

        if not message.tool_calls:
            return AgentResult(content=message.content, usage=total, steps=step,
                               stopped_by="final")

        for call in message.tool_calls:
            await emit(run_id, "node_tool_call", node_id,
                       {"tool": call.name, "args": _brief(call.args)})
            outcome = await toolbox.execute(call.name, call.args) if toolbox else ToolOutcome(
                "无可用工具", True
            )
            await emit(run_id, "node_tool_result", node_id,
                       {"tool": call.name, "output": _brief(outcome.output, 1500),
                        "is_error": outcome.is_error})
            tool_message = Message(role="tool", content=outcome.output,
                                   tool_call_id=call.id, name=call.name)
            messages.append(tool_message)
            _save_message(node_id, attempt_id, "tool", outcome.output, tool_call_id=call.id)
            if outcome.finished:
                summary = outcome.output
                return AgentResult(content=summary, usage=total, steps=step,
                                   stopped_by="finish")
    raise AgentExceeded(f"超过最大步数 {max_steps}，Agent 未声明完成")


async def _run_json_protocol(*, run_id: str, node_id: int, attempt_id: int, model: ModelInfo,
                             system_prompt: str, user_prompt: str, toolbox: Toolbox,
                             max_steps: int, temperature: float) -> AgentResult:
    """无原生工具能力时的降级：提示词 JSON 协议（模型接入配置设计 §6）。"""
    protocol = (
        "\n\n你没有原生工具调用能力。每轮必须只输出一个 JSON 对象，不要输出解释或 Markdown：\n"
        '{"tool": "<工具名>", "args": {...}}\n'
        '或 {"final": "<最终答案/总结>"}\n'
        f"可用工具：{', '.join(t['name'] for t in toolbox.schemas())}"
    )
    messages = [
        Message(role="system", content=system_prompt + protocol),
        Message(role="user", content=user_prompt),
    ]
    adapter = pool.adapter_for(model)
    total = Usage()
    for step in range(1, max_steps + 1):
        params = GenParams(model=model.model_name, temperature=temperature,
                           max_tokens=min(4096, model.max_output_tokens or 4096))
        reply = await adapter.chat(messages, None, params)
        step_usage = Usage(
            input_tokens=adapter.count_tokens_estimate(messages),
            output_tokens=max(1, len(reply.content) // 3),
        )
        total.input_tokens += step_usage.input_tokens
        total.output_tokens += step_usage.output_tokens
        add_attempt_usage(attempt_id, step_usage, model_id=model.id)
        consume_run_budget(run_id, step_usage)
        _save_message(node_id, attempt_id, "assistant", reply.content)
        messages.append(reply)
        data = _extract_json(reply.content)
        if data is None:
            messages.append(Message(role="user", content="格式错误：请只输出一个合法 JSON 对象。"))
            continue
        if "final" in data:
            return AgentResult(content=str(data["final"]), usage=total, steps=step,
                               stopped_by="final")
        name = str(data.get("tool") or "")
        args = data.get("args") if isinstance(data.get("args"), dict) else {}
        outcome = await toolbox.execute(name, args)
        messages.append(
            Message(role="user", content=f"工具 {name} 的结果：\n{outcome.output}")
        )
        if outcome.finished:
            return AgentResult(content=outcome.output, usage=total, steps=step,
                               stopped_by="finish")
    raise AgentExceeded(f"超过最大步数 {max_steps}（JSON 协议）")


def _extract_json(text: str) -> dict | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None
