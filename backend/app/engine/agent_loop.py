"""Agent 循环：流式输出 → 工具调用 → 观测 → 再推理（设计规格 §5.2）。"""
from __future__ import annotations

import json
from dataclasses import dataclass

from app.config import settings
from app.conversation.compact import (CompactionStats, make_summarizer, maybe_compact,
                                      summary_of)
from app.conversation.policy import DEFAULT_POLICY, policy_for_run
from app.conversation.store import record_compaction
from app.db.base import session_scope
from app.db.models import Message as MessageRow
from app.engine.events import emit, emit_token
from app.engine.usage import BudgetExceeded, add_attempt_usage, consume_run_budget
from app.models.budget import ContextBudget
from app.models.pool import ModelInfo, pool
from app.models.types import GenParams, Message, StreamEvent, Usage
from app.tools.registry import Toolbox, ToolOutcome

_COMPACT_COOLDOWN_STEPS = 3      # 压完仍在触发线之上时，隔几步再试，不每一步都重压


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
                    token_extra: dict | None = None,
                    extra_messages: list[Message] | None = None) -> AgentResult:
    if toolbox is not None and not model.cap_tools:
        return await _run_json_protocol(
            run_id=run_id, node_id=node_id, attempt_id=attempt_id, model=model,
            system_prompt=system_prompt, user_prompt=user_prompt, toolbox=toolbox,
            max_steps=max_steps, temperature=temperature,
        )

    schemas = toolbox.schemas() if toolbox else None
    messages = [Message(role="system", content=system_prompt)]
    messages += extra_messages if extra_messages is not None else [
        Message(role="user", content=user_prompt)]
    total = Usage()
    cooldown_until = 0
    for step in range(1, max_steps + 1):
        budget = ContextBudget(model.context_window, model.max_output_tokens)
        params = GenParams(
            model=model.model_name,
            temperature=temperature,
            # 单次输出上限跟模型配置走：模型的输出上限与窗口 15% 取小，输入预算已为它留位
            max_tokens=budget.max_output,
        )
        if extra_messages is None and len(messages) > 2 and step >= cooldown_until:
            # 压缩钩子：占用不到阈值时原样返回（此时不会留下任何记录）。
            # 调用方给了现成的消息列表（会议）时跳过：冻结区由调用方自己管理，这里压了会破坏前缀。
            # 摘要失败时按原消息继续——压缩只是优化，不该让已经跑了很久的节点作废、从头重来。
            # 预算耗尽是例外：那是要由人处理的事，照常上抛。
            try:
                policy = policy_for_run(run_id)
            except Exception:
                policy = DEFAULT_POLICY       # 读策略失败按默认策略压，压缩辅助步骤不杀死节点
            input_budget = budget.input_budget
            stats = CompactionStats()

            async def report_progress(index: int, total: int | None) -> None:
                done = f"/{total}" if total else ""
                await emit(run_id, "node_log", node_id, {
                    "level": "info", "message": f"压缩上下文：第 {index}{done} 块"})

            try:
                compacted, before_tokens, after_tokens = await maybe_compact(
                    run_id=run_id, node_id=node_id, model=model, policy=policy,
                    messages=messages, stats=stats, on_progress=report_progress,
                    summarize=make_summarizer(run_id=run_id, attempt_id=attempt_id, model=model))
            except BudgetExceeded:
                raise
            except Exception as exc:
                compacted, before_tokens, after_tokens = messages, -1, -1
                # 摘要失败也进入冷却：供应商对这类请求持续失败时，不必每一步都再试一次
                cooldown_until = step + _COMPACT_COOLDOWN_STEPS
                await emit(run_id, "node_log", node_id, {
                    "level": "warn",
                    "message": f"上下文压缩失败，按原内容继续：{exc}"[:300]})
            if compacted is not messages:
                try:
                    await record_compaction(
                        run_id=run_id, node_id=node_id, attempt_id=attempt_id,
                        scope="agent_loop", summary=summary_of(compacted) or "",
                        before_tokens=before_tokens, after_tokens=after_tokens,
                        calls=stats.calls, summarized_tokens=stats.summarized_tokens)
                except Exception as exc:
                    # 留痕失败不该让已经压好的内容丢失：照常换用压缩后的视图
                    await emit(run_id, "node_log", node_id, {
                        "level": "warn",
                        "message": f"压缩留痕失败（照常使用压缩后的内容）：{exc}"[:300]})
                messages = compacted
                if after_tokens >= policy.compact_at * max(1, input_budget):
                    # 压不到触发线以下：冷却几轮再试，避免每一步都把最近的消息重新摘要一遍
                    cooldown_until = step + _COMPACT_COOLDOWN_STEPS
        if not quiet:
            await emit(run_id, "node_log", node_id,
                       {"level": "info",
                        "message": f"第 {step}/{max_steps} 步：请求模型 {model.display_name}"})
        message, usage = await _call_stream(model, messages, schemas, params,
                                            run_id=run_id, node_id=node_id,
                                            token_extra=token_extra)
        total.input_tokens += usage.input_tokens
        total.output_tokens += usage.output_tokens
        total.cached_tokens += usage.cached_tokens
        add_attempt_usage(attempt_id, usage, model_id=model.id)
        consume_run_budget(run_id, usage)
        if usage.total and not quiet:
            await emit(run_id, "node_log", node_id,
                       {"level": "debug",
                        "message": "tokens in/out = "
                                  f"{usage.input_tokens}/{usage.output_tokens}"})
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
