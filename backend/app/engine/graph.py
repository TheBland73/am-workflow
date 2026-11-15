"""依赖图：拓扑、Waves 与环检测（设计规格 §5.2）。"""
from __future__ import annotations


class GraphError(ValueError):
    pass


def compute_waves(deps_map: dict[str, list[str]]) -> dict[str, int]:
    """同一 wave 可并发，wave 之间按依赖串行（汇合点=屏障）。"""
    waves: dict[str, int] = {}
    visiting: set[str] = set()

    def visit(key: str) -> int:
        if key in waves:
            return waves[key]
        if key in visiting:
            raise GraphError(f"依赖图存在环：{key}")
        visiting.add(key)
        deps = deps_map.get(key, [])
        missing = [d for d in deps if d not in deps_map]
        if missing:
            raise GraphError(f"节点 {key} 依赖不存在的节点：{', '.join(missing)}")
        wave = 1 + max((visit(d) for d in deps), default=0)
        visiting.discard(key)
        waves[key] = wave
        return wave

    for key in deps_map:
        visit(key)
    return waves


def topological_order(deps_map: dict[str, list[str]]) -> list[str]:
    waves = compute_waves(deps_map)
    return sorted(deps_map, key=lambda k: (waves[k], k))
