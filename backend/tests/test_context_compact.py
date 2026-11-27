"""条件压缩：到阈值才压缩，尽量压到目标以下；系统提示不动，最近的消息尽量原样保留。"""
from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest

from app.conversation import compact
from app.conversation.compact import compact_messages, needs_compaction
from app.conversation.policy import ContextPolicy
from app.db.base import init_db, session_scope
from app.db.models import ModelEntry, Node, NodeAttempt, ProviderConfig, Run, Workflow
from app.models.budget import estimate_tokens
from app.models.pool import ModelInfo, pool
from app.models.providers.openai_chat import OpenAIChatAdapter
from app.models.types import Message, ToolCall

POLICY = ContextPolicy(compact_at=0.8, compact_to=0.5, keep_recent=2)


def _messages(count: int, each: int = 1000) -> list[Message]:
    rest = [Message(role="user" if i % 2 == 0 else "assistant", content="字" * each)
            for i in range(count)]
    return [Message(role="system", content="系统")] + rest


async def _noop(text: str) -> str:
    return "摘要"


def test_not_triggered_below_threshold() -> None:
    assert needs_compaction(_messages(3), input_budget=10_000, policy=POLICY) is False


def test_compacts_older_messages_and_keeps_recent() -> None:
    messages = _messages(8, each=100)             # 约 800 token，窗口 800 → 到达阈值
    assert needs_compaction(messages, input_budget=800, policy=POLICY) is True
    seen: list[str] = []

    async def summarize(text: str) -> str:
        seen.append(text)
        return "摘要"

    result, before, after = asyncio.run(compact_messages(
        messages, summarize=summarize, input_budget=800, policy=POLICY))
    assert before >= 800
    assert after < 400                              # 压到目标占用（50%）以下
    assert result[0].role == "system"
    assert result[1].role == "user" and result[1].content.startswith("【较早对话的摘要】")
    assert [m.content for m in result[2:]] == [m.content for m in messages[-2:]]
    assert len(seen) == 1 and "字" in seen[0]


def test_long_recent_messages_are_capped_to_reach_target() -> None:
    """保留的最近几条本身很长：压完仍高于目标时，把它们逐级截短。"""
    messages = _messages(3, each=3000) + [Message(role="user", content="字" * 3000)]
    result, before, after = asyncio.run(compact_messages(
        messages, summarize=_noop, input_budget=4000, policy=POLICY))
    assert before > 0.8 * 4000
    assert after < 0.5 * 4000
    assert result[0].role == "system" and len(result) == 4      # 系统提示 + 摘要 + 最近两条


def test_leaves_short_conversations_alone() -> None:
    messages = _messages(2)
    result, before, after = asyncio.run(compact_messages(
        messages, summarize=_noop, input_budget=1000, policy=POLICY))
    assert result == messages and before == after


def test_kept_messages_never_start_with_an_orphaned_tool_result() -> None:
    # 工具结果必须紧跟在发起调用的那条助手消息后面。保留部分若以工具结果开头，
    # 它回应的消息已经被压进摘要，下一次请求就不合法：这样的工具结果一并交给摘要
    messages = _messages(3, each=100) + [
        Message(role="assistant", content="",
                tool_calls=[ToolCall(id="c1", name="查", args={}), ToolCall(id="c2", name="查", args={})]),
        Message(role="tool", content="字" * 100, tool_call_id="c1"),
        Message(role="tool", content="字" * 100, tool_call_id="c2"),
        Message(role="assistant", content="字" * 100),
    ]
    seen: list[str] = []

    async def summarize(text: str) -> str:
        seen.append(text)
        return "摘要"

    result, _, _ = asyncio.run(compact_messages(
        messages, summarize=summarize, input_budget=800, policy=POLICY))
    assert [m.role for m in result[2:]] == ["assistant"]          # 最近两条里的工具结果没有孤立保留
    assert "[工具结果]" in seen[0]


def test_long_history_is_summarized_from_oldest_to_newest_in_chunks() -> None:
    # 待压缩的内容超过一次摘要的容量时，从旧到新分块、滚动摘要。
    # 离保留窗口最近的部分最有用，不能像"整段截到上限"那样把它截掉
    messages = [Message(role="system", content="系统")]
    for index in range(6):
        messages.append(Message(role="user", content=f"第{index}段开始" + "字" * 10_000))
        messages.append(Message(role="assistant", content=f"第{index}段结束" + "字" * 10_000))
    messages += [Message(role="user", content="最近的问题"), Message(role="assistant", content="最近的回答")]
    seen: list[str] = []

    async def summarize(text: str) -> str:
        seen.append(text)
        return f"摘要{len(seen)}"

    result, _, _ = asyncio.run(compact_messages(
        messages, summarize=summarize, input_budget=1_000_000, policy=POLICY))
    assert len(seen) == 6                                     # 每块两万 token 左右，共六块
    assert "第0段开始" in seen[0] and "第5段结束" in seen[-1]
    assert "摘要1" in seen[1] and "摘要5" in seen[-1]          # 后一块连同前一块的摘要一起送入
    assert result[1].content == "【较早对话的摘要】\n摘要6"
    assert [m.content for m in result[2:]] == ["最近的问题", "最近的回答"]


def test_a_message_larger_than_one_chunk_is_summarized_alone_with_only_its_head_kept() -> None:
    # 单条消息就超过一次摘要能处理的量（例如一次很大的工具调用结果）：它单独成块，只保留开头。
    # 它前面的消息先落成自己的一块，不能顺手丢掉；后面的消息另起一块，照常进入摘要
    messages = [Message(role="system", content="系统"),
                Message(role="user", content="之前的提问" + "字" * 100),
                Message(role="assistant", content="", tool_calls=[
                    ToolCall(id="c1", name="read_file", args={"path": "big.log"})]),
                Message(role="tool", content="结果开头" + "字" * 60_000 + "结果结尾", tool_call_id="c1"),
                Message(role="assistant", content="之后的回答" + "字" * 100),
                Message(role="user", content="最近的问题"),
                Message(role="assistant", content="最近的回答")]
    seen: list[str] = []

    async def summarize(text: str) -> str:
        seen.append(text)
        return f"摘要{len(seen)}"

    result, _, _ = asyncio.run(compact_messages(
        messages, summarize=summarize, input_budget=1_000_000, policy=POLICY))

    assert len(seen) == 3
    assert seen[0].startswith("[用户] 之前的提问") and "read_file" in seen[0]    # 前面的消息先落成一块，没有丢
    assert "[工具结果] 结果开头" + "字" * 1000 in seen[1]       # 超大的那条单独成块，只保留开头
    assert "结果结尾" not in seen[1] and "之前的提问" not in seen[1] and "read_file" not in seen[1]
    assert seen[2].endswith("[助手] 之后的回答" + "字" * 100)    # 后面的消息另起一块
    assert "之前的提问" not in seen[2] and "[工具结果]" not in seen[2]          # 各块互不夹带
    assert result[1].content == "【较早对话的摘要】\n摘要3"
    assert [m.content for m in result[2:]] == ["最近的问题", "最近的回答"]


def test_usage_estimate_counts_tool_call_arguments() -> None:
    # 工具调用的参数也在输入里（写文件时参数就是整个文件内容），不计就会太晚压缩
    call = ToolCall(id="c1", name="write_file", args={"path": "a.py", "content": "字" * 3000})
    plain = [Message(role="system", content="系统"), Message(role="user", content="字" * 1000)]
    heavy = plain + [Message(role="assistant", content="", tool_calls=[call])]
    assert needs_compaction(plain, input_budget=4000, policy=POLICY) is False
    assert needs_compaction(heavy, input_budget=4000, policy=POLICY) is True

    seen: list[str] = []

    async def summarize(text: str) -> str:
        seen.append(text)
        return "摘要"

    asyncio.run(compact_messages(
        heavy + [Message(role="user", content="字" * 100)] * 3, summarize=summarize,
        input_budget=100_000, policy=POLICY))
    assert "write_file" in seen[0] and "a.py" in seen[0]       # 摘要的输入里看得到调用过什么、传了什么


def test_summarizer_waits_for_the_provider_slot(monkeypatch) -> None:
    # 摘要也是一次模型调用，要和正文调用一样受供应商并发数的约束
    init_db()
    order: list[str] = []
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        order.append("call")
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "摘要"}}]})

    class Gate:
        async def __aenter__(self) -> None:
            order.append("enter")

        async def __aexit__(self, *exc) -> None:
            order.append("exit")

    async def slot(provider):
        order.append("slot")
        return Gate()

    monkeypatch.setattr(compact.pool, "slot", slot)
    monkeypatch.setattr(compact.pool, "adapter_for",
                        lambda model: OpenAIChatAdapter(base_url="http://mock/v1", api_key=""))
    monkeypatch.setattr(
        OpenAIChatAdapter, "_client",
        lambda self: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    model = SimpleNamespace(id=None, model_name="m", max_output_tokens=4096,
                            provider=SimpleNamespace(id=1))

    asyncio.run(compact.make_summarizer(run_id="compact-slot", attempt_id=0, model=model)("旧对话"))
    assert order == ["slot", "enter", "call", "exit"]
    sent = json.loads(requests[0].content)
    assert [m["content"] for m in sent["messages"]] == [compact.SUMMARIZER_PROMPT, "旧对话"]


def test_folds_whole_tool_groups_into_the_summary_when_still_above_the_trigger() -> None:
    # 保留部分里带着很大的工具调用参数（截不掉），压完仍在触发线之上：下一步请求会立刻再次
    # 触发压缩。这时把保留部分里较早的几组整组并进摘要，直到落到触发线以下
    def write(index: int) -> Message:
        return Message(role="assistant", content="", tool_calls=[
            ToolCall(id=f"c{index}", name="write_file", args={"path": "a.py", "content": "字" * 3000})])

    messages = [Message(role="system", content="系统"), Message(role="user", content="任务")]
    for index in range(5):
        messages += [write(index), Message(role="tool", content="写好了", tool_call_id=f"c{index}")]
    seen: list[str] = []

    async def summarize(text: str) -> str:
        seen.append(text)
        return "摘要"

    result, before, after = asyncio.run(compact_messages(
        messages, summarize=summarize, input_budget=3000, policy=POLICY))
    assert before >= 0.8 * 3000
    assert after < 0.8 * 3000
    assert [m.role for m in result] == ["system", "user"]      # 最近的一组也整组并进了摘要
    assert len(seen) == 2 and "摘要" in seen[1] and "write_file" in seen[1]


def test_kept_part_stays_valid_when_only_some_groups_are_folded() -> None:
    # 只并入一部分时，留下的保留部分仍以"助手发起调用 → 工具结果"的完整一组开头
    def write(index: int, size: int) -> Message:
        return Message(role="assistant", content="", tool_calls=[
            ToolCall(id=f"c{index}", name="write_file",
                     args={"path": f"a{index}.py", "content": "字" * size})])

    messages = [Message(role="system", content="系统"), Message(role="user", content="任务")]
    for index in range(3):
        messages += [write(index, 3000), Message(role="tool", content=f"{index} 写好了", tool_call_id=f"c{index}")]
    messages += [write(3, 300), Message(role="tool", content="3 写好了", tool_call_id="c3")]
    seen: list[str] = []

    async def summarize(text: str) -> str:
        seen.append(text)
        return "摘要"

    keep_four = ContextPolicy(compact_at=0.8, compact_to=0.5, keep_recent=4)
    result, _, after = asyncio.run(compact_messages(
        messages, summarize=summarize, input_budget=3000, policy=keep_four))
    assert after < 0.8 * 3000
    kept = result[2:]
    assert [m.role for m in kept] == ["assistant", "tool"]      # 最后那一组留着，前一组整组并进了摘要
    assert kept[0].tool_calls[0].id == "c3"
    assert len(seen) == 2
    assert "a2.py" in seen[1] and "2 写好了" in seen[1]         # 并进去的是完整的一组
    assert "a3.py" not in seen[1]


def test_empty_summary_is_a_failure_not_a_summary(monkeypatch) -> None:
    # 思考型模型可能把输出上限耗在推理上、正文为空。拿一句占位话当摘要，滚动摘要里
    # 此前各块压好的内容就会被这一句替换掉，而且没有任何报错
    init_db()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "  "}}]})


    async def slot(provider):
        return asyncio.Semaphore(1)

    monkeypatch.setattr(compact.pool, "slot", slot)
    monkeypatch.setattr(compact.pool, "adapter_for",
                        lambda model: OpenAIChatAdapter(base_url="http://mock/v1", api_key=""))
    monkeypatch.setattr(
        OpenAIChatAdapter, "_client",
        lambda self: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    summarize = compact.make_summarizer(
        run_id="compact-empty", attempt_id=0,
        model=SimpleNamespace(id=None, model_name="m", max_output_tokens=4096,
                              provider=SimpleNamespace(id=1)))

    with pytest.raises(RuntimeError, match="空正文"):
        asyncio.run(summarize("旧对话"))


def _mock_chat(monkeypatch, reply: str) -> list[httpx.Request]:
    """把模型调用接到假的 HTTP 应答上，返回收到的请求。并发闸门与用量记账都用真的。"""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": reply}}]})

    monkeypatch.setattr(compact.pool, "adapter_for",
                        lambda model: OpenAIChatAdapter(base_url="http://mock/v1", api_key=""))
    monkeypatch.setattr(
        OpenAIChatAdapter, "_client",
        lambda self: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return requests


@pytest.fixture
def seed_model():
    """建一对供应商与模型行，返回连接池读出来的模型信息（可用窗口 7168：窗口 8192、输出上限 1024）。

    用完删掉这一对行：测试共用一个库，别的用例会给没绑模型的角色挑"库里第一个启用的模型"，
    留着一个窗口很小的模型，排在后面的端到端用例就会按它来算上下文预算。
    """
    providers: list[int] = []

    def seed(name: str) -> ModelInfo:
        with session_scope() as s:
            provider = ProviderConfig(name=f"压缩测试供应商 {uuid.uuid4().hex[:8]}",
                                      protocol="openai_chat",
                                      base_url="http://127.0.0.1:9/v1", api_key_enc=None)
            s.add(provider)
            s.flush()
            model = ModelEntry(provider_id=provider.id, model_name=name, display_name=name,
                               context_window=8192, max_output_tokens=1024)
            s.add(model)
            s.flush()
            provider_id, model_id = provider.id, model.id
        providers.append(provider_id)
        return pool.load_model(model_id)

    yield seed
    with session_scope() as s:
        for provider in s.query(ProviderConfig).filter(ProviderConfig.id.in_(providers)).all():
            s.delete(provider)                # 级联删掉它下面的模型行


def _new_run(run_id: str, *, budget: int) -> None:
    """一条带 token 预算的运行，挂在为本用例新建的工作流上。"""
    init_db()
    with session_scope() as s:
        workflow = Workflow(name=f"压缩测试 {run_id}",
                            template_key=f"compact-test-{uuid.uuid4().hex[:8]}")
        s.add(workflow)
        s.flush()
        s.add(Run(id=run_id, workflow_id=workflow.id, status="running", budget_tokens=budget))


def test_summarizer_usage_is_added_to_the_attempt_that_requested_it(monkeypatch, seed_model) -> None:
    # 给了执行记录编号：摘要调用的用量累加到这条执行记录上，实际使用的模型也记下来（成本归集）；
    # 运行预算照常扣减
    run_id = "compact-attempt"
    _new_run(run_id, budget=100_000)
    model = seed_model("m")
    with session_scope() as s:
        node = Node(run_id=run_id, node_key="dev", type="AGENT", name="开发", status="running")
        s.add(node)
        s.flush()
        attempt = NodeAttempt(node_id=node.id, attempt_no=1, status="running",
                              tokens_in=100, tokens_out=10)       # 正文调用已经用掉的
        s.add(attempt)
        s.flush()
        attempt_id = attempt.id
    requests = _mock_chat(monkeypatch, "摘要")

    summarize = compact.make_summarizer(run_id=run_id, attempt_id=attempt_id, model=model)
    assert asyncio.run(summarize("旧对话")) == "摘要"

    sent = json.loads(requests[0].content)["messages"]
    assert [item["content"] for item in sent] == [compact.SUMMARIZER_PROMPT, "旧对话"]
    used = sum(estimate_tokens(item["content"]) for item in sent)      # 送入摘要模型的 token
    with session_scope() as s:
        attempt = s.get(NodeAttempt, attempt_id)
        # 累加而不是覆盖；两个字的正文按三个字符一个 token 估不出一个，至少记 1
        assert (attempt.tokens_in, attempt.tokens_out) == (100 + used, 10 + 1)
        assert attempt.model_id == model.id                             # 成本归集到实际使用的模型
        assert s.get(Run, run_id).budget_tokens == 100_000 - used - 1  # 运行预算照常扣减


def test_summary_call_cap_drops_the_oldest_chunks_and_says_so() -> None:
    # 一次压缩的摘要调用数设上限：块数超过上限时只压最近的几块（离保留窗口最近的最有用），
    # 丢掉的更早内容在摘要里注明，不能悄悄少一截
    messages = [Message(role="system", content="系统")]
    for index in range(6):
        messages.append(Message(role="user", content=f"第{index}段开始" + "字" * 10_000))
        messages.append(Message(role="assistant", content=f"第{index}段结束" + "字" * 10_000))
    messages += [Message(role="user", content="最近的问题"), Message(role="assistant", content="最近的回答")]
    seen: list[str] = []

    async def summarize(text: str) -> str:
        seen.append(text)
        return f"摘要{len(seen)}"

    stats = compact.CompactionStats()
    result, _, _ = asyncio.run(compact_messages(
        messages, summarize=summarize, input_budget=1_000_000, policy=POLICY,
        max_calls=3, stats=stats))
    assert len(seen) == 3
    assert "第0段开始" not in seen[0] and "第3段开始" in seen[0]
    assert "第5段结束" in seen[-1]
    assert "未纳入摘要" in result[1].content
    assert stats.calls == 3


def test_the_call_cap_also_limits_folding_groups_into_the_summary() -> None:
    # 压完仍在触发线之上时本来会继续把整组并进摘要；上限用尽就停在那里，
    # 不能一边设上限一边又为合并多调几次
    def write(index: int) -> Message:
        return Message(role="assistant", content="", tool_calls=[
            ToolCall(id=f"c{index}", name="write_file", args={"path": "a.py", "content": "字" * 3000})])

    messages = [Message(role="system", content="系统"), Message(role="user", content="任务")]
    for index in range(5):
        messages += [write(index), Message(role="tool", content="写好了", tool_call_id=f"c{index}")]
    seen: list[str] = []

    async def summarize(text: str) -> str:
        seen.append(text)
        return "摘要"

    _, _, after = asyncio.run(compact_messages(
        messages, summarize=summarize, input_budget=3000, policy=POLICY, max_calls=1))
    assert len(seen) == 1
    assert after >= 0.8 * 3000                    # 没有继续并入，仍高于触发线


def test_skips_when_the_only_compressible_message_is_the_previous_summary() -> None:
    # 上一版摘要已经是全部可压内容：再摘要一次只是把摘要摘要一遍，白花一次调用
    messages = [Message(role="system", content="系统"),
                Message(role="user", content=compact.SUMMARY_PREFIX + "旧摘要"),
                Message(role="assistant", content="最近一"),
                Message(role="user", content="最近二")]
    seen: list[str] = []

    async def summarize(text: str) -> str:
        seen.append(text)
        return "新摘要"

    result, before, after = asyncio.run(compact_messages(
        messages, summarize=summarize, input_budget=1000, policy=POLICY))
    assert result is messages and seen == [] and before == after


def test_reports_calls_summarized_tokens_and_chunk_progress() -> None:
    messages = _messages(8, each=100)
    seen: list[str] = []
    progress: list[tuple[int, int]] = []
    stats = compact.CompactionStats()

    async def summarize(text: str) -> str:
        seen.append(text)
        return "摘要"

    async def on_progress(index: int, total: int) -> None:
        progress.append((index, total))

    asyncio.run(compact_messages(
        messages, summarize=summarize, input_budget=800, policy=POLICY,
        stats=stats, on_progress=on_progress))
    assert stats.calls == 1 and stats.cut == 6
    assert stats.summarized_tokens == sum(estimate_tokens(text) for text in seen)
    assert progress == [(1, 1)]


def test_maybe_compact_reports_no_compaction_when_there_is_nothing_to_compact() -> None:
    # 到了阈值、但非系统消息不超过保留条数：没有东西可压。后两个值必须是 -1（没压缩），
    # 否则调用方会把一条没发生的压缩记进会话日志
    messages = [Message(role="system", content="系统"),
                Message(role="user", content="字" * 3000),
                Message(role="assistant", content="字" * 3000)]
    model = SimpleNamespace(context_window=8192, max_output_tokens=1024)
    result, before, after = asyncio.run(compact.maybe_compact(
        run_id="x", node_id=1, model=model, policy=ContextPolicy(), messages=messages,
        summarize=_noop))
    assert result is messages and (before, after) == (-1, -1)


def test_maybe_compact_summarizes_with_the_same_model_when_no_summarizer_is_given(
        monkeypatch, seed_model) -> None:
    # 调用方不给 summarize：用传进来的那个模型做摘要。摘要的用量要记在运行预算上，所以先有一条运行
    requests = _mock_chat(monkeypatch, "模型给的摘要")
    _new_run("compact-default", budget=1_000_000)
    model = seed_model("m")
    messages = _messages(8, each=1000)             # 约 8000 token，可用窗口 7168 → 到达阈值
    stats = compact.CompactionStats()

    result, before, after = asyncio.run(compact.maybe_compact(
        run_id="compact-default", node_id=1, model=model, policy=POLICY, messages=messages,
        stats=stats))

    assert (before, after) == (8002, 2017)         # 系统提示 2 + 八条各 1000；压掉六条、换入一条摘要
    assert result[1].content == "【较早对话的摘要】\n模型给的摘要"      # 摘要正文是模型的回复
    assert [m.content for m in result[2:]] == [m.content for m in messages[-2:]]
    assert (stats.calls, stats.cut) == (1, 6)      # 调用次数与压掉的条数回报给了调用方
    assert len(requests) == 1
    sent = json.loads(requests[0].content)
    assert sent["model"] == "m"                    # 用的是传入的那个模型
    prompt, older = [item["content"] for item in sent["messages"]]
    assert prompt == compact.SUMMARIZER_PROMPT
    assert older.count("[用户] ") == 3 and older.count("[助手] ") == 3   # 较早的六条都送进了摘要
