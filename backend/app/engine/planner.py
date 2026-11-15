"""计划编排：结构校验、波次、并行组、关键路径与指标（ORCH-01）。

原则：模型只提议任务，依赖、波次、关键路径、角色可行性全部由这里确定性计算。
"""
from __future__ import annotations

import re
from typing import Any

from app.engine.graph import GraphError, compute_waves, topological_order

ROLE_KEYS = {"dev", "test", "design", "pm", "review", "ops"}
EFFORT_POINTS = {"S": 1, "M": 2, "L": 3, "XL": 5}
PRIORITIES = {"P0", "P1", "P2"}
UID_RE = re.compile(r"^T\d+$")
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)
NUMBER_PREFIX_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)*)[.、)）]?\s*")


def normalize_section(value: str | None) -> str:
    text = (value or "").strip().strip("`").strip()
    text = NUMBER_PREFIX_RE.sub("", text)
    return re.sub(r"\s+", "", text).lower()


def extract_sections(design_text: str) -> list[dict[str, Any]]:
    """把设计规格里的 Markdown 标题抽成章节索引。"""
    sections: list[dict[str, Any]] = []
    for match in HEADING_RE.finditer(design_text or ""):
        title = match.group(2).strip()
        line = design_text.count("\n", 0, match.start()) + 1
        sections.append(
            {
                "level": len(match.group(1)),
                "title": title,
                "line": line,
                "key": normalize_section(title),
            }
        )
    return sections


def match_section(value: str | None, sections: list[dict[str, Any]]) -> dict[str, Any] | None:
    key = normalize_section(value)
    if not key:
        return None
    for section in sections:
        if section["key"] == key:
            return section
    # 允许模型省略/保留编号前缀、或多写层级名，取包含关系最接近的一项
    candidates = [
        section
        for section in sections
        if key in section["key"] or section["key"] in key
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: len(item["key"]))


def _error(code: str, message: str, hint: str = "", **details: Any) -> dict[str, Any]:
    return {"code": code, "message": message, "hint": hint, "details": details}


def _find_cycle(deps_map: dict[str, list[str]]) -> list[str] | None:
    color: dict[str, int] = {}
    stack: list[str] = []
    position: dict[str, int] = {}

    def dfs(node: str) -> list[str] | None:
        color[node] = 1
        position[node] = len(stack)
        stack.append(node)
        for dep in deps_map.get(node, []):
            if dep not in deps_map:
                continue
            if color.get(dep) == 1:
                return stack[position[dep]:] + [dep]
            if color.get(dep, 0) == 0:
                found = dfs(dep)
                if found:
                    return found
        stack.pop()
        position.pop(node, None)
        color[node] = 2
        return None

    for node in deps_map:
        if color.get(node, 0) == 0:
            found = dfs(node)
            if found:
                return found
    return None


def _critical_path(
    deps_map: dict[str, list[str]], points: dict[str, int]
) -> tuple[list[str], int]:
    order = topological_order(deps_map)
    distance: dict[str, int] = {}
    previous: dict[str, str | None] = {}
    for node in order:
        best = points.get(node, 1)
        best_from: str | None = None
        for dep in deps_map.get(node, []):
            candidate = distance[dep] + points.get(node, 1)
            if candidate > best or (candidate == best and best_from is None):
                best = candidate
                best_from = dep
        distance[node] = best
        previous[node] = best_from
    if not order:
        return [], 0
    end = max(order, key=lambda item: (distance[item], item))
    path: list[str] = []
    cursor: str | None = end
    while cursor:
        path.append(cursor)
        cursor = previous[cursor]
    path.reverse()
    return path, distance[end]


def _normalize_tasks(raw_tasks: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_tasks):
        item = raw if isinstance(raw, dict) else {}
        deps = item.get("deps") or []
        acceptance = item.get("acceptance") or []
        normalized.append(
            {
                "task_uid": str(item.get("uid") or item.get("task_uid") or "").strip(),
                "title": str(item.get("title") or "").strip(),
                "description": str(item.get("description") or "").strip(),
                "deps": [str(dep).strip() for dep in deps if str(dep).strip()],
                "design_section": str(item.get("design_section") or "").strip(),
                "acceptance": [
                    str(entry).strip() for entry in acceptance if str(entry).strip()
                ],
                "role_key": str(item.get("role") or item.get("role_key") or "dev").strip(),
                "effort": str(item.get("effort") or "M").strip().upper(),
                "priority": str(item.get("priority") or "P1").strip().upper(),
                "order_index": index,
            }
        )
    return normalized


def build_snapshot(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """任务已通过校验时，计算波次、并行组、关键路径、图与指标。"""
    deps_map = {task["task_uid"]: task["deps"] for task in tasks}
    waves = compute_waves(deps_map)
    groups: dict[str, list[str]] = {}
    for uid, wave in sorted(waves.items(), key=lambda item: (item[1], item[0])):
        groups.setdefault(str(wave), []).append(uid)
    points = {task["task_uid"]: EFFORT_POINTS[task["effort"]] for task in tasks}
    critical, critical_points = _critical_path(deps_map, points)
    edges = [
        {"from": dep, "to": task["task_uid"]}
        for task in tasks
        for dep in task["deps"]
    ]
    roles: dict[str, int] = {}
    for task in tasks:
        roles[task["role_key"]] = roles.get(task["role_key"], 0) + 1
    metrics = {
        "task_count": len(tasks),
        "wave_count": len(groups),
        "max_parallel": max((len(group) for group in groups.values()), default=0),
        "critical_path_tasks": len(critical),
        "critical_path_points": critical_points,
        "total_points": sum(points.values()),
        "role_distribution": roles,
    }
    dag = {
        "nodes": [
            {
                "uid": task["task_uid"],
                "title": task["title"],
                "wave": waves[task["task_uid"]],
                "role": task["role_key"],
                "effort": task["effort"],
                "priority": task["priority"],
                "deps": task["deps"],
                "design_section": task["design_section"],
            }
            for task in tasks
        ],
        "edges": edges,
    }
    return {
        "tasks": tasks,
        "dag": dag,
        "waves": groups,
        "critical_path": critical,
        "metrics": metrics,
    }


def validate_plan(raw_tasks: list[Any], design_text: str) -> dict[str, Any]:
    """返回 `{ok, errors, snapshot}`；errors 可直接给前端定位。"""
    tasks = _normalize_tasks(raw_tasks or [])
    errors: list[dict[str, Any]] = []
    sections = extract_sections(design_text)
    if len(tasks) < 8 or len(tasks) > 40:
        errors.append(
            _error(
                "task_count",
                f"任务数量必须在 8–40 之间，当前 {len(tasks)} 条",
                "把范围拆到可独立验收的粒度，或合并过细任务。",
                count=len(tasks),
            )
        )
    if not sections:
        errors.append(
            _error(
                "design_sections",
                "设计规格中没有可识别的 Markdown 章节标题",
                "用 `## 1 模块划分` 这样的编号章节组织设计文档。",
            )
        )
    seen: set[str] = set()
    for task in tasks:
        uid = task["task_uid"]
        if not uid:
            errors.append(_error("uid_missing", "任务缺少 uid", "uid 形如 T1、T2。"))
            continue
        if not UID_RE.match(uid):
            errors.append(
                _error("uid_format", f"任务 uid 不合法：{uid}", "uid 使用 T 加数字，如 T7。", uid=uid)
            )
        if uid in seen:
            errors.append(_error("uid_duplicate", f"任务 uid 重复：{uid}", "每个任务 uid 唯一。", uid=uid))
        seen.add(uid)
        if not (6 <= len(task["title"]) <= 40):
            errors.append(
                _error(
                    "title_length",
                    f"{uid} 标题长度需 6–40 字，当前 {len(task['title'])}",
                    "标题以动词开头，写清交付物。",
                    uid=uid,
                )
            )
        if not (20 <= len(task["description"]) <= 200):
            errors.append(
                _error(
                    "description_length",
                    f"{uid} 描述长度需 20–200 字，当前 {len(task['description'])}",
                    "补充输入、动作与输出。",
                    uid=uid,
                )
            )
        if task["role_key"] not in ROLE_KEYS:
            errors.append(
                _error(
                    "role_invalid",
                    f"{uid} 角色不在允许集合：{task['role_key']}",
                    f"允许：{', '.join(sorted(ROLE_KEYS))}",
                    uid=uid,
                )
            )
        if task["effort"] not in EFFORT_POINTS:
            errors.append(
                _error("effort_invalid", f"{uid} 工作量必须是 S/M/L/XL", uid=uid)
            )
        if task["priority"] not in PRIORITIES:
            errors.append(_error("priority_invalid", f"{uid} 优先级必须是 P0/P1/P2", uid=uid))
        if not (1 <= len(task["acceptance"]) <= 5):
            errors.append(
                _error("acceptance_count", f"{uid} 需要 1–5 条验收标准", uid=uid)
            )
        for entry in task["acceptance"]:
            if len(entry) < 4:
                errors.append(
                    _error("acceptance_quality", f"{uid} 的验收标准太短：{entry}", uid=uid)
                )
        if not task["design_section"]:
            errors.append(
                _error("section_missing", f"{uid} 未引用设计章节", "填写真实存在的章节标题。", uid=uid)
            )
        elif sections and match_section(task["design_section"], sections) is None:
            errors.append(
                _error(
                    "section_unmatched",
                    f"{uid} 引用的章节不存在：{task['design_section']}",
                    "只能引用设计规格中真实出现的章节。",
                    uid=uid,
                    section=task["design_section"],
                )
            )

    deps_map = {task["task_uid"]: task["deps"] for task in tasks}
    for task in tasks:
        uid = task["task_uid"]
        for dep in task["deps"]:
            if dep == uid:
                errors.append(
                    _error("self_dependency", f"{uid} 依赖了自己", "删除自依赖。", uid=uid)
                )
            elif dep not in deps_map:
                errors.append(
                    _error(
                        "dependency_missing",
                        f"{uid} 依赖不存在的任务：{dep}",
                        "依赖必须指向本次任务清单内的 uid。",
                        uid=uid,
                        dependency=dep,
                    )
                )
    if not any(error["code"] in {"uid_missing", "uid_duplicate", "dependency_missing"} for error in errors):
        cycle = _find_cycle(deps_map)
        if cycle:
            errors.append(
                _error(
                    "dependency_cycle",
                    "依赖成环：" + " → ".join(cycle),
                    "断掉环中的一条依赖；编排必须是有向无环图。",
                    cycle=cycle,
                )
            )

    if errors:
        return {"ok": False, "errors": errors, "snapshot": None, "tasks": tasks}
    try:
        snapshot = build_snapshot(tasks)
    except GraphError as exc:
        return {
            "ok": False,
            "errors": [_error("graph_invalid", str(exc), "检查依赖关系。")],
            "snapshot": None,
        }
    snapshot["validation"] = {"ok": True, "errors": []}
    return {"ok": True, "errors": [], "snapshot": snapshot, "tasks": tasks}
