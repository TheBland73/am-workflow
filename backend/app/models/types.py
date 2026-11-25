"""内部归一结构：Message / ToolCall / ToolResult / StreamEvent（模型接入配置设计 §4、§7）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    id: str
    output: str
    is_error: bool = False


@dataclass
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0   # 输入里命中前缀缓存的 token 数；供应商不回报时为 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class GenParams:
    model: str = ""
    temperature: float = 0.4
    max_tokens: int = 4096
    stop: list[str] | None = None


@dataclass
class StreamEvent:
    """流式事件：text_delta / tool_call_delta / usage / done。

    done 事件携带完整归一 Message，工具调用参数已由适配器拼装完毕，
    编排核无需理解任何协议的线格式。
    """

    kind: Literal["text_delta", "tool_call_delta", "usage", "done"]
    text: str = ""
    tool_name: str = ""
    usage: Usage | None = None
    message: Message | None = None


class ProviderError(RuntimeError):
    """供应商调用失败（已转成可读错误）。"""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code
