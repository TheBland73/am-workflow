"""常见供应商预设（模型接入配置设计 §8）。"""
from __future__ import annotations

PRESETS = [
    {
        "key": "deepseek",
        "name": "DeepSeek",
        "protocol": "openai_chat",
        "base_url": "https://api.deepseek.com/v1",
        "models": [
            {"model_name": "deepseek-chat", "display_name": "DeepSeek Chat",
             "context_window": 65536, "max_output_tokens": 8192, "cap_tools": True},
            {"model_name": "deepseek-reasoner", "display_name": "DeepSeek Reasoner",
             "context_window": 65536, "max_output_tokens": 8192, "cap_tools": True},
        ],
    },
    {
        "key": "openai",
        "name": "OpenAI",
        "protocol": "openai_chat",
        "base_url": "https://api.openai.com/v1",
        "models": [
            {"model_name": "gpt-4o-mini", "display_name": "GPT-4o mini",
             "context_window": 128000, "max_output_tokens": 8192, "cap_tools": True},
            {"model_name": "gpt-4o", "display_name": "GPT-4o",
             "context_window": 128000, "max_output_tokens": 16384, "cap_tools": True},
        ],
    },
    {
        "key": "zhipu_glm",
        "name": "智谱 GLM",
        "protocol": "openai_chat",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": [
            {"model_name": "glm-4.6", "display_name": "GLM-4.6",
             "context_window": 200000, "max_output_tokens": 8192, "cap_tools": True},
        ],
    },
    {
        "key": "doubao",
        "name": "豆包（火山方舟）",
        "protocol": "openai_chat",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "models": [
            {"model_name": "doubao-pro-32k", "display_name": "豆包 Pro 32K",
             "context_window": 32768, "max_output_tokens": 4096, "cap_tools": True},
        ],
    },
    {
        "key": "kimi",
        "name": "Kimi（月之暗面）",
        "protocol": "openai_chat",
        "base_url": "https://api.moonshot.cn/v1",
        "models": [
            {"model_name": "kimi-k2-0905-preview", "display_name": "Kimi K2",
             "context_window": 131072, "max_output_tokens": 8192, "cap_tools": True},
        ],
    },
    {
        "key": "bailian",
        "name": "阿里百炼",
        "protocol": "openai_chat",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": [
            {"model_name": "qwen3-coder-plus", "display_name": "Qwen3 Coder Plus",
             "context_window": 131072, "max_output_tokens": 8192, "cap_tools": True},
        ],
    },
    {
        "key": "siliconflow",
        "name": "SiliconFlow",
        "protocol": "openai_chat",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": [
            {"model_name": "deepseek-ai/DeepSeek-V3.2", "display_name": "DeepSeek V3.2 (SF)",
             "context_window": 131072, "max_output_tokens": 8192, "cap_tools": True},
        ],
    },
    {
        "key": "openrouter",
        "name": "OpenRouter",
        "protocol": "openai_chat",
        "base_url": "https://openrouter.ai/api/v1",
        "models": [
            {"model_name": "anthropic/claude-sonnet-4.5", "display_name": "Claude Sonnet 4.5",
             "context_window": 200000, "max_output_tokens": 8192, "cap_tools": True},
        ],
    },
    {
        "key": "anthropic",
        "name": "Anthropic",
        "protocol": "anthropic_messages",
        "base_url": "https://api.anthropic.com/v1",
        "models": [
            {"model_name": "claude-sonnet-4-5", "display_name": "Claude Sonnet 4.5",
             "context_window": 200000, "max_output_tokens": 8192, "cap_tools": True},
        ],
    },
    {
        "key": "gemini",
        "name": "Google Gemini",
        "protocol": "gemini_generate",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "models": [
            {"model_name": "gemini-2.5-pro", "display_name": "Gemini 2.5 Pro",
             "context_window": 1000000, "max_output_tokens": 8192, "cap_tools": True},
        ],
    },
    {
        "key": "custom",
        "name": "自建 / 其它兼容服务",
        "protocol": "openai_chat",
        "base_url": "",
        "models": [],
    },
]
