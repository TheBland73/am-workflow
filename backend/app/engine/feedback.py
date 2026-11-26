"""节点反馈的统一写法：凡是要带着反馈重开一个节点，都经过 with_feedback()。

- feedback_seq：每写一次加一。会议运行器靠它认出"这是一条还没处理过的反馈"。
  带着反馈续开的会议如果在人工确认点挂起，恢复时节点配置里还留着同一条反馈，不该再处理一遍。
- feedback_kind：反馈的来路。要复议的来路（人工驳回、定点回流、用户带反馈的重试或注入）
  会议会把它当作一条意见处理；失败后的自动重试也带反馈，但会议只原地续开。
"""
from __future__ import annotations

# 要复议的反馈来路
REOPEN_KINDS = ("reject", "reroute", "user")


def with_feedback(config: dict | None, feedback: str | None,
                  kind: str | None = None) -> dict:
    """返回写入反馈之后的新配置，不改入参。feedback 为 None 时把反馈清掉。"""
    config = dict(config or {})
    if feedback is None:
        config.pop("feedback", None)
        config.pop("feedback_kind", None)
        return config
    config["feedback"] = feedback
    config["feedback_seq"] = int(config.get("feedback_seq") or 0) + 1
    if kind:
        config["feedback_kind"] = kind
    else:
        config.pop("feedback_kind", None)
    return config
