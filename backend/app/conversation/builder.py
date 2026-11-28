"""视图装配：带标记的段 → 发给模型的消息列表。

段有三种标记，必须按 冻结 → 追加 → 尾部 的顺序排列：
- frozen：在一次执行内逐字节不变。内容变了会记入 BuiltContext.frozen_changed，
  调用方据此推送告警事件；
- append：只追加；
- tail：每次不同，很短。

预算不够时每次把一个段缩小 20%：先缩追加段里最长的；追加段都缩到下限仍不够时，
才动设了上限的冻结段，从最后一个开始。系统提示、尾部、没设上限的冻结段不动。
冻结段应当在构造时就用 capped() 截好：这样它在每次调用里的内容都相同，不破坏前缀缓存。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.models.budget import estimate_tokens, truncate_to_tokens
from app.models.types import Message

SegmentKind = Literal["frozen", "append", "tail"]
_ORDER = {"frozen": 0, "append": 1, "tail": 2}
_MIN_SEGMENT_TOKENS = 256


class SegmentOrderError(ValueError):
    """段的排列违反了 冻结 → 追加 → 尾部 的顺序。"""


@dataclass
class Segment:
    kind: SegmentKind
    text: str
    role: str = "user"           # user / assistant
    name: str = ""               # 冻结段的名字，用于冻结校验
    limit: int | None = None     # 预算不够时这个段允许缩到的基准（估算）。装配器不会主动按它截断，
                                 # 要预先截断请先用 capped()；不给的冻结段永远不会被缩小
    shrinkable: bool = True      # 常规缩段不碰这个段；只有其它段都缩不动时才轮到它（如会议摘要段）


@dataclass
class BuiltContext:
    messages: list[Message]
    estimated_tokens: int
    truncated: list[str] = field(default_factory=list)
    frozen_changed: list[str] = field(default_factory=list)


def capped(text: str, limit: int | None) -> str:
    """把一段文本预先截到上限。只取决于它自己，所以同样的输入永远得到同样的结果。"""
    if limit is None or estimate_tokens(text) <= limit:
        return text
    return truncate_to_tokens(text, limit)


def _shrink_once(texts: list[str], limits: list[int | None], segments: list[Segment],
                 truncated: list[str]) -> bool:
    """把一个段缩小 20%，返回是否缩小了。

    先缩追加段里最长的（不可缩的段除外）。追加段都缩到下限仍不够时，才动设了上限的冻结段，
    从最后一个开始（它排得最靠后，变动它让出的前缀最少）。还缩不动时，最后才轮到标了
    shrinkable=False 的追加段（如会议摘要段——它是早期内容的唯一载体，能保就保）。
    已经缩到下限的段不再动，所以每返回一次 True，总占用都会变小；都缩不动时返回 False。
    """
    best, best_size = -1, 0
    capped_frozen: list[int] = []
    protected: list[int] = []
    for index, (text, limit) in enumerate(zip(texts, limits)):
        segment = segments[index]
        if segment.kind == "tail":
            continue
        if segment.kind == "frozen":
            if limit is not None:
                capped_frozen.append(index)
            continue
        if not segment.shrinkable:
            protected.append(index)
            continue
        size = estimate_tokens(text)
        if size <= _MIN_SEGMENT_TOKENS or size <= best_size:
            continue
        best, best_size = index, size
    if best < 0:
        for index in reversed(capped_frozen):
            size = estimate_tokens(texts[index])
            if size > _MIN_SEGMENT_TOKENS:
                best, best_size = index, size
                break
    if best < 0:
        protected.sort(key=lambda index: estimate_tokens(texts[index]), reverse=True)
        for index in protected:
            size = estimate_tokens(texts[index])
            if size > _MIN_SEGMENT_TOKENS:
                best, best_size = index, size
                break
    if best < 0:
        return False
    limit = max(_MIN_SEGMENT_TOKENS, int(min(limits[best] or best_size, best_size) * 0.8))
    texts[best] = truncate_to_tokens(texts[best], limit)
    limits[best] = limit
    name = segments[best].name or f"segment-{best}"
    if name not in truncated:
        truncated.append(name)
    return True


class ContextBuilder:
    """一次执行用一个实例：记住冻结段的内容，供之后的调用比对。"""

    def __init__(self) -> None:
        self._frozen: dict[str, str] = {}

    def build(self, segments: list[Segment], *, system: str,
              input_budget: int) -> BuiltContext:
        orders = [_ORDER[segment.kind] for segment in segments]
        if orders != sorted(orders):
            raise SegmentOrderError("段必须按 冻结 → 追加 → 尾部 的顺序排列")
        result = BuiltContext(messages=[], estimated_tokens=0)
        for segment in segments:
            if segment.kind == "frozen" and segment.name:
                previous = self._frozen.get(segment.name)
                if previous is not None and previous != segment.text:
                    result.frozen_changed.append(segment.name)
                self._frozen[segment.name] = segment.text
        texts = [segment.text for segment in segments]
        limits = [segment.limit for segment in segments]
        system_tokens = estimate_tokens(system)
        while system_tokens + sum(estimate_tokens(t) for t in texts) > input_budget:
            if not _shrink_once(texts, limits, segments, result.truncated):
                break
        result.estimated_tokens = system_tokens + sum(estimate_tokens(t) for t in texts)
        result.messages = [Message(role="system", content=system)]
        result.messages += [Message(role=s.role, content=t) for s, t in zip(segments, texts)]
        return result
