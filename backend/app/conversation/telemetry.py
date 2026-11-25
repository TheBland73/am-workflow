"""用量与缓存命中统计。"""
from __future__ import annotations


def parse_cached_tokens(usage: dict | None) -> int:
    """从各家协议的 usage 对象里取缓存命中的输入 token 数，取不到时返回 0。

    OpenAI 兼容（含智谱）在 prompt_tokens_details.cached_tokens；
    DeepSeek 在 prompt_cache_hit_tokens；Anthropic 在 cache_read_input_tokens；
    Gemini 在 usageMetadata.cachedContentTokenCount。
    """
    usage = usage or {}
    details = usage.get("prompt_tokens_details") or {}
    for value in (details.get("cached_tokens"), usage.get("prompt_cache_hit_tokens"),
                  usage.get("cache_read_input_tokens"),
                  (usage.get("usageMetadata") or {}).get("cachedContentTokenCount")):
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return 0


def cache_hit_rate(cached_tokens: int, input_tokens: int) -> float:
    return round(cached_tokens / input_tokens, 4) if input_tokens else 0.0
