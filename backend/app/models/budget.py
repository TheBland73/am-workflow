"""Token 估算与上下文预算（模型接入配置设计 §6）。"""
from __future__ import annotations

from dataclasses import dataclass

# 上下文预算比例（系统 5% / 记忆 10% / 工件 25% / 历史 30%，余下为工具结果与压缩缓冲）
SECTION_RATIOS = {
    "system": 0.05,
    "memory": 0.10,
    "artifact": 0.25,
    "history": 0.30,
}
RESERVE_OUTPUT_RATIO = 0.15


def estimate_tokens(text: str) -> int:
    """保守估算：CJK 字符按 1 token，其他字符按 4 字符 1 token。"""
    if not text:
        return 0
    cjk = 0
    for ch in text:
        code = ord(ch)
        if 0x4E00 <= code <= 0x9FFF or 0x3000 <= code <= 0x30FF or 0xFF00 <= code <= 0xFFEF:
            cjk += 1
    return max(1, cjk + (len(text) - cjk) // 4)


def truncate_to_tokens(text: str, limit: int, marker: str = "\n…（已按上下文预算截断）") -> str:
    if estimate_tokens(text) <= limit:
        return text
    ratio = limit / max(1, estimate_tokens(text))
    keep = max(0, int(len(text) * ratio * 0.9))
    head = text[:keep]
    return (head + marker) if head else marker


@dataclass
class ContextBudget:
    context_window: int
    max_output_tokens: int

    @property
    def input_budget(self) -> int:
        reserve = min(self.max_output_tokens, int(self.context_window * RESERVE_OUTPUT_RATIO))
        return max(1024, self.context_window - reserve)

    def section(self, name: str) -> int:
        return max(256, int(self.input_budget * SECTION_RATIOS.get(name, 0.05)))
