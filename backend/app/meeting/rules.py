"""决策点账本的规则：表态等级、闭合规则、停滞判定。

全部是纯函数，由程序计算，不调模型（详设 §7.3、§7.4）。
一个决策点是存取层返回的字典，其中 stances 形如
{"<参会者 id>": {"level": 1–5, "reason": str, "round": int, "name": str, "history": [...]}}。
"""
from __future__ import annotations

SUPPORT, RESERVED, ABSTAIN, NEED_INFO, OPPOSE = 1, 2, 3, 4, 5
LEVEL_NAMES = {SUPPORT: "支持", RESERVED: "有保留支持", ABSTAIN: "弃权",
               NEED_INFO: "需更多信息", OPPOSE: "反对"}

OPEN_STATUSES = ("open", "reopened")       # 讨论中
CLOSED_STATUSES = ("agreed", "ruled")      # 了结
DEFERRED = "deferred"                      # 待定：带进方案的"待确认问题"，不丢


def level_of(point: dict, voter) -> int | None:
    return ((point.get("stances") or {}).get(str(voter)) or {}).get("level")


def point_agreed(point: dict, rule: str, voters: list[int]) -> bool:
    """按闭合规则，这个决策点在当前表态下算不算达成一致。"""
    levels = [level_of(point, voter) for voter in voters]
    if not voters or any(level is None for level in levels):
        return False                                  # 还有人没表态
    if rule == "no_objection":
        return all(level not in (NEED_INFO, OPPOSE) for level in levels)
    return all(level in (SUPPORT, RESERVED) for level in levels)


def settle(points: list[dict], *, rule: str, voters: list[int],
           before: dict | None) -> dict:
    """一轮全员表态之后结算一次。只计算，不改账本；落盘由协议做。

    before 是这一轮开始时的表态快照（snapshot() 的返回值）。
    返回 open（仍未了结的编号）、agreed（这一轮达成一致的编号）、
    changed（有没有人改动未了结决策点上的表态等级——停滞判定看它）。
    """
    working = [point for point in points if point["status"] in OPEN_STATUSES]
    agreed = [point["index"] for point in working if point_agreed(point, rule, voters)]
    changed = False
    for point in working:
        for voter, level in ((before or {}).get(point["index"]) or {}).items():
            if level_of(point, voter) != level:
                changed = True
    return {"open": [point["index"] for point in working if point["index"] not in agreed],
            "agreed": agreed, "changed": changed}


def to_rule(points: list[dict], exchanges: dict[str, int], exchanges_per_point: int) -> list[str]:
    """闭合规则是"主持人裁决"时：交锋次数已满、仍未一致的决策点要交给主持人裁决。"""
    return [point["index"] for point in points
            if point["status"] in OPEN_STATUSES
            and exchanges.get(point["index"], 0) >= exchanges_per_point]


def relax(points: list[dict], voters: list[int]) -> dict:
    """全自动模式下的退让：先按"无反对即可"了结，仍不行的记为待定。只计算，不落盘。

    与 point_agreed 同一口径：缺表态不算"无人反对"，该点记为待定而不是自动关闭。
    """
    agreed: list[str] = []
    deferred: list[str] = []
    for point in points:
        if point["status"] not in OPEN_STATUSES:
            continue
        if point_agreed(point, "no_objection", voters):
            agreed.append(point["index"])
        else:
            deferred.append(point["index"])
    return {"agreed": agreed, "deferred": deferred}


def snapshot(points: list[dict]) -> dict:
    """未了结决策点当前的表态等级 {编号: {参会者 id: 等级}}，供下一轮判断有没有人改立场。"""
    return {point["index"]: {pid: (stance or {}).get("level")
                             for pid, stance in (point.get("stances") or {}).items()}
            for point in points if point["status"] in OPEN_STATUSES}


def rank_speakers(point: dict, voters: list[int]) -> list[int]:
    """把参会者按立场从最支持排到最反对；没表态的排在最后。"""
    return sorted(voters, key=lambda voter: (level_of(point, voter) is None,
                                             level_of(point, voter) or 0))
