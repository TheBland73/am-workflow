"""上下文装配器：段的顺序、冻结校验、预算截断。"""
from __future__ import annotations

import threading

import pytest

from app.conversation.builder import ContextBuilder, Segment, SegmentOrderError, capped
from app.conversation.policy import ContextPolicy, validate_context_policy
from app.models.budget import estimate_tokens


def _cn(count: int) -> str:
    """约 count 个 token 的中文文本（估算规则：每个汉字 1 个 token）。"""
    return "字" * count


def test_rejects_wrong_order() -> None:
    with pytest.raises(SegmentOrderError):
        ContextBuilder().build(
            [Segment("append", "甲"), Segment("frozen", "乙", name="章程")],
            system="系统", input_budget=10_000)


def test_frozen_change_is_reported_once_per_change() -> None:
    builder = ContextBuilder()

    def segments(text: str) -> list[Segment]:
        return [Segment("frozen", text, name="章程"), Segment("tail", "指令")]

    assert builder.build(segments("A"), system="系统", input_budget=10_000).frozen_changed == []
    assert builder.build(segments("B"), system="系统", input_budget=10_000).frozen_changed == ["章程"]
    assert builder.build(segments("B"), system="系统", input_budget=10_000).frozen_changed == []


def test_messages_follow_segment_roles() -> None:
    built = ContextBuilder().build(
        [Segment("frozen", "章程", name="章程"),
         Segment("append", "对方发言"),
         Segment("append", "我方发言", role="assistant"),
         Segment("tail", "请回应")],
        system="系统提示", input_budget=10_000)
    assert [(m.role, m.content) for m in built.messages] == [
        ("system", "系统提示"), ("user", "章程"), ("user", "对方发言"),
        ("assistant", "我方发言"), ("user", "请回应")]


def test_truncates_largest_append_segment_to_fit_budget() -> None:
    built = ContextBuilder().build(
        [Segment("frozen", _cn(500), name="章程"),
         Segment("append", _cn(3000), name="交锋记录"),
         Segment("tail", "请回应")],
        system="系统", input_budget=2000)
    assert built.estimated_tokens <= 2000
    assert built.truncated == ["交锋记录"]
    assert built.messages[-1].content == "请回应"
    assert built.messages[1].content == "字" * 500


def test_enough_budget_leaves_everything_intact() -> None:
    text = _cn(3000)
    built = ContextBuilder().build([Segment("append", text, name="交锋记录")],
                                   system="系统", input_budget=10_000)
    assert built.truncated == []
    assert built.messages[1].content == text


def test_largest_append_segment_shrinks_first() -> None:
    built = ContextBuilder().build(
        [Segment("append", _cn(1000), name="较早的发言"),
         Segment("append", _cn(3000), name="最新的发言"),
         Segment("tail", "请回应")],
        system="系统", input_budget=3500)
    assert built.truncated == ["最新的发言"]
    assert built.messages[1].content == "字" * 1000


def test_last_capped_frozen_segment_is_touched_before_earlier_ones() -> None:
    built = ContextBuilder().build(
        [Segment("frozen", _cn(900), name="章程", limit=900),
         Segment("frozen", _cn(900), name="账本", limit=900),
         Segment("tail", "请回应")],
        system="系统", input_budget=1500)
    assert built.truncated == ["账本"]
    assert built.messages[1].content == "字" * 900


def test_append_segments_shrink_before_capped_frozen_ones() -> None:
    builder = ContextBuilder()
    frozen = Segment("frozen", _cn(900), name="账本", limit=900)
    append = Segment("append", _cn(600), name="交锋")

    small = builder.build([frozen, append, Segment("tail", "请回应")],
                          system="系统", input_budget=1500)
    assert small.truncated == ["交锋"]                 # 先缩追加段，冻结段逐字节不变
    assert small.messages[1].content == "字" * 900

    tight = builder.build([frozen, append, Segment("tail", "请回应")],
                          system="系统", input_budget=1000)
    assert tight.truncated == ["交锋", "账本"]         # 追加段缩到下限仍不够，才动冻结段
    assert tight.messages[1].content != "字" * 900


def test_capped_frozen_segment_shrinks_by_a_fifth_per_step() -> None:
    # 追加段已经缩不动时才动带上限的冻结段，而且每一步只缩 20%，不会一步截到下限
    built = ContextBuilder().build(
        [Segment("frozen", _cn(1000), name="账本", limit=1000),
         Segment("append", _cn(100), name="短发言"),
         Segment("tail", "请回应")],
        system="系统", input_budget=1000)
    assert built.truncated == ["账本"]
    size = estimate_tokens(built.messages[1].content)
    assert 256 < size <= 800


def test_stops_when_nothing_left_to_shrink() -> None:
    # 每一段都缩到下限仍然超预算时照常返回（结果仍然超预算），不会一直循环
    result: dict = {}

    def run() -> None:
        result["built"] = ContextBuilder().build(
            [Segment("frozen", _cn(300), name="账本", limit=300),
             Segment("append", _cn(300), name="交锋"),
             Segment("tail", "请回应")],
            system="系统", input_budget=10)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive(), "装配器在无可再缩时没有停下来"
    built = result["built"]
    assert built.estimated_tokens > 10
    assert built.truncated == ["交锋", "账本"]


def test_capped_is_deterministic() -> None:
    assert capped(_cn(100), None) == "字" * 100
    assert capped(_cn(100), 200) == "字" * 100
    assert capped(_cn(100), 50) == capped(_cn(100), 50)
    assert estimate_tokens(capped(_cn(100), 50)) < 100


def test_policy_rules() -> None:
    assert validate_context_policy(ContextPolicy()) == []
    assert validate_context_policy(ContextPolicy(compact_at=0.6, compact_to=0.5))
    assert validate_context_policy(ContextPolicy(compact_at=1.2))
