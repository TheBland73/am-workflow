"""需求追踪矩阵的解析与机械核对（详设 §9.1–§9.2）。

全部是纯函数：程序核对覆盖率、EARS 句式与验收标准，不调模型。
"""
from __future__ import annotations

import re

_REQUIREMENT = re.compile(r"^(#{2,4})[ \t]+(R\d+)\b[ \t]*(.*)$")
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*)$")
_SECTION_END = re.compile(r"^#{1,2}[ \t]+")
_POINT = re.compile(r"\bD\d+\b")
_PRIORITIES = ("P1", "P2", "P3")
_FAIL_KINDS = {"mechanical", "distorted", "missing", "added"}
_RANK = {"blocker": 0, "major": 1, "minor": 2}
_COLUMNS = {
    "requirement": ("需求编号", "需求", "编号"),
    "points": ("来源决策点", "决策点", "来源"),
    "section": ("方案章节", "章节", "位置"),
    "priority": ("优先级",),
}


def parse_requirements(text: str) -> list[dict]:
    """需求条目：标题形如 `### R1 标题`（2 到 4 级），正文到下一条需求或同级标题为止。"""
    lines = text.splitlines()
    headings = []
    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        if not match:
            continue
        requirement = _REQUIREMENT.match(line)
        headings.append({
            "line": index, "level": len(match.group(1)),
            "id": requirement.group(2) if requirement else None,
            "title": ((requirement.group(3) or "").strip() if requirement else "")})
    items = []
    for position, heading in enumerate(headings):
        if not heading["id"]:
            continue
        end = len(lines)
        for following in headings[position + 1:]:
            if following["id"] or following["level"] <= heading["level"]:
                end = following["line"]
                break
        items.append({"id": heading["id"], "title": heading["title"],
                      "body": "\n".join(lines[heading["line"] + 1:end]).strip()})
    return items


def _section_lines(text: str, title: str) -> list[str]:
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == title:
            start = index + 1
            break
    if start is None:
        return []
    end = len(lines)
    for index in range(start, len(lines)):
        if _SECTION_END.match(lines[index]):
            end = index
            break
    return lines[start:end]


def _table_rows(lines: list[str]) -> list[list[str]]:
    rows = []
    for line in lines:
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if all(not cell or re.fullmatch(r":?-{2,}:?", cell) for cell in cells):
            continue                                  # 分隔行
        rows.append(cells)
    return rows


def _column_order(header: list[str]) -> dict[str, int]:
    order: dict[str, int] = {}
    for key, aliases in _COLUMNS.items():
        for position, cell in enumerate(header):
            if any(alias in cell for alias in aliases):
                order[key] = position
                break
    return order


def parse_matrix(text: str) -> list[dict]:
    """`## 需求追踪矩阵` 下的表格行。列的顺序以表头为准，只读前四列（需求、来源、章节、优先级）。"""
    rows = _table_rows(_section_lines(text, "## 需求追踪矩阵"))
    if not rows:
        return []
    header = rows[0]
    named = _column_order(header)
    order = {key: named.get(key, position)
             for position, key in enumerate(("requirement", "points", "section", "priority"))}
    start = 1 if named else 0
    items = []
    for row in rows[start:]:
        def cell(key: str) -> str:
            position = order[key]
            return row[position] if position < len(row) else ""
        requirement = cell("requirement")
        if not requirement:
            continue
        items.append({"requirement": requirement,
                      "points": _POINT.findall(cell("points")),
                      "section": cell("section"), "priority": cell("priority")})
    return items


def parse_non_requirements(text: str) -> list[str]:
    """`## 非需求类决策` 的表格里出现的决策点编号。"""
    rows = _table_rows(_section_lines(text, "## 非需求类决策"))
    found: list[str] = []
    for row in rows[1:] if rows else []:
        for point in _POINT.findall(" | ".join(row)):
            if point not in found:
                found.append(point)
    return found


def closed_points(points: list[dict]) -> list[dict]:
    return [point for point in points if point.get("status") in ("agreed", "ruled")]


def mechanical_check(text: str, points: list[dict]) -> list[str]:
    """详设 §9.2 的七条机械核对规则。每条说明都写明需求编号或决策点编号。"""
    requirements = parse_requirements(text)
    if not requirements:
        return ["没有解析出需求条目（标题形如 ### R1 …）"]
    issues: list[str] = []
    seen: set[str] = set()
    reported_duplicates: set[str] = set()
    for requirement in requirements:
        if requirement["id"] in seen and requirement["id"] not in reported_duplicates:
            issues.append(f"需求编号重复：{requirement['id']}")
            reported_duplicates.add(requirement["id"])
        seen.add(requirement["id"])

    matrix = parse_matrix(text)
    matrix_ids = {row["requirement"] for row in matrix}
    for requirement_id in dict.fromkeys(item["id"] for item in requirements):
        if requirement_id not in matrix_ids:
            issues.append(f"{requirement_id} 没有出现在追踪矩阵里")

    ledger = {point["index"]: point for point in points}
    closed = {point["index"] for point in closed_points(points)}
    for row in matrix:
        if not row["points"]:
            issues.append(f"矩阵中 {row['requirement']} 没有来源决策点")
        for point in row["points"]:
            if point not in ledger:
                issues.append(f"矩阵中 {row['requirement']} 引用了不存在的决策点 {point}")
            elif point not in closed:
                issues.append(f"矩阵中 {row['requirement']} 引用了还没有了结的决策点 {point}")
        if row["priority"] not in _PRIORITIES:
            issues.append(f"矩阵中 {row['requirement']} 的优先级 "
                          f"{row['priority'] or '（空）'} 不合法（须是 P1、P2、P3 之一）")

    referenced = {point for row in matrix for point in row["points"]}
    non_requirements = set(parse_non_requirements(text))
    for point in points:
        if point["index"] in closed and point["index"] not in referenced \
                and point["index"] not in non_requirements:
            issues.append(f"决策点 {point['index']} 已了结，但既没有需求引用它，"
                          "也没有列入非需求类决策")

    for requirement in requirements:
        if "待定" in requirement["body"]:
            issues.append(f"{requirement['id']} 的正文里有「待定」")
    for requirement in requirements:
        if not re.search(r"WHEN\s+[^\n]+?\s+THE SYSTEM SHALL\s+\S", requirement["body"],
                         re.I):
            issues.append(f"{requirement['id']} 没有 EARS 句式（WHEN … THE SYSTEM SHALL …）")
        if not all(re.search(rf"\b{word}\b", requirement["body"], re.I)
                   for word in ("given", "when", "then")):
            issues.append(f"{requirement['id']} 没有 Given/When/Then 验收标准")
    return issues


def confirm_verdict(items: list[dict], *, blocking_line: str = "major") -> str:
    """汇总裁决：机械问题、走样/遗漏/凭空新增、达到阻塞线的质量问题，任一出现即不通过。"""
    limit = _RANK.get(blocking_line, _RANK["major"])
    for item in items:
        kind = item.get("kind")
        if kind in _FAIL_KINDS:
            return "fail"
        if kind == "quality" and _RANK.get(item.get("severity") or "minor", 2) <= limit:
            return "fail"
    return "pass"


def changed_requirements(before: dict, after: dict) -> list[str]:
    """正文变了的（或新加的）需求编号，按数字顺序。入参是 {编号: 正文}。"""
    def number(requirement_id: str) -> int:
        match = re.search(r"\d+", str(requirement_id))
        return int(match.group()) if match else 0

    changed = [requirement_id for requirement_id in set(before) | set(after)
               if before.get(requirement_id) != after.get(requirement_id)]
    return sorted(changed, key=number)
