"""上下文策略与默认值。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContextPolicy:
    compact_at: float = 0.80   # 估算占用达到可用窗口的这个比例才压缩
    compact_to: float = 0.50   # 压缩后的目标占用
    keep_recent: int = 6       # 压缩时至多原样保留最近多少条消息（压完仍超预算时会被截短，见 compact.py）


DEFAULT_POLICY = ContextPolicy()


def policy_from_config(config: dict | None) -> ContextPolicy:
    """由运行级配置的 context 段构造策略，缺项取默认值。"""
    config = config or {}
    return ContextPolicy(
        compact_at=float(config.get("compact_at", DEFAULT_POLICY.compact_at)),
        compact_to=float(config.get("compact_to", DEFAULT_POLICY.compact_to)),
        keep_recent=int(config.get("keep_recent", DEFAULT_POLICY.keep_recent)),
    )


def validate_context_policy(policy: ContextPolicy) -> list[str]:
    issues: list[str] = []
    if not 0.50 <= policy.compact_at <= 0.95:
        issues.append("压缩触发阈值须在 50%–95% 之间")
    if not 0.20 <= policy.compact_to <= 0.60:
        issues.append("压缩后目标占用须在 20%–60% 之间")
    if policy.compact_to > policy.compact_at - 0.20:
        issues.append("压缩后目标占用须比触发阈值至少低 20 个百分点")
    if policy.keep_recent < 2:
        issues.append("压缩时保留的最近消息数至少为 2")
    return issues
