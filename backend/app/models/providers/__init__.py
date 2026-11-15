from app.models.providers.anthropic_messages import AnthropicAdapter
from app.models.providers.gemini_generate import GeminiAdapter
from app.models.providers.openai_chat import OpenAIChatAdapter

ADAPTERS = {
    "openai_chat": OpenAIChatAdapter,
    "anthropic_messages": AnthropicAdapter,
    "gemini_generate": GeminiAdapter,
}

__all__ = ["ADAPTERS", "AnthropicAdapter", "GeminiAdapter", "OpenAIChatAdapter"]
