"""task.md 解析与任务图落库（设计规格 §5.6）。"""
from __future__ import annotations

import re

from app.engine.graph import GraphError, compute_waves

TASK_LINE = re.compile(
    r"^\s*-\s*\[( |x|X)\]\s+(?P<title>.+?)\s*<!--\s*(?P<meta>.+?)\s*-->\s*$", re.M
)


def parse_tasks(markdown: str) -> list[dict]:
    tasks: list[dict] = []
    for match in TASK_LINE.finditer(markdown):
        meta_raw = match.group("meta")
        meta: dict[str, str] = {}
        for part in meta_raw.split(";"):
            if ":" not in part:
                continue
            key, value = part.split(":", 1)
            meta[key.strip().lower()] = value.strip()
        task_uid = meta.get("id") or f"T{len(tasks) + 1}"
        deps_raw = meta.get("deps") or ""
        deps = [d.strip() for d in deps_raw.split(",") if d.strip()]
        tests = [t.strip() for t in (meta.get("test-cases") or "").split(",") if t.strip()]
        try:
            wave = int(meta.get("wave") or 0)
        except ValueError:
            wave = 0
        tasks.append(
            {
                "task_uid": task_uid,
                "title": match.group("title").strip(),
                "deps": deps,
                "design_section": meta.get("design-section") or None,
                "test_cases": tests,
                "assignee": meta.get("assignee") or "dev",
                "wave": wave,
                "status": "done" if match.group(1).lower() == "x" else "pending",
            }
        )
    return tasks


def validate_tasks(tasks: list[dict]) -> dict[str, int]:
    if not tasks:
        raise GraphError("task.md 中没有解析到任务行（需要 `- [ ] 标题 <!-- id: ... -->` 格式）")
    ids = [t["task_uid"] for t in tasks]
    if len(ids) != len(set(ids)):
        raise GraphError("任务 id 重复")
    deps_map = {t["task_uid"]: t["deps"] for t in tasks}
    try:
        waves = compute_waves(deps_map)
    except GraphError as exc:
        raise GraphError(f"任务依赖非法：{exc}") from exc
    for task in tasks:
        if not task["wave"] or task["wave"] != waves[task["task_uid"]]:
            task["wave"] = waves[task["task_uid"]]
    return waves
