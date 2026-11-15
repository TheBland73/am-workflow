"""工件校验：程序的判定权（设计规格 §5.6）。"""
from __future__ import annotations

import re


def validate_artifact(kind: str, body: str) -> list[str]:
    issues: list[str] = []
    text = body.strip()
    if len(text) < 200:
        issues.append("正文过短（少于 200 字），请给出完整内容")
    if kind == "requirement":
        ears = re.findall(r"WHEN\s+.+?\s+THE SYSTEM SHALL\s+.+", text)
        if len(ears) < 3:
            issues.append(
                "EARS 需求条目不足 3 条：每条写成 `WHEN <触发> THE SYSTEM SHALL <行为>`"
            )
        if "Given" not in text and "GIVEN" not in text:
            issues.append("缺少 Given/When/Then 验收标准")
    elif kind == "task":
        ids = re.findall(r"<!--[^>]*\bid:\s*([A-Za-z0-9.\-]+)", text)
        if len(ids) < 1:
            issues.append("没有符合格式的任务行：`- [ ] 标题 <!-- id: T1.1; deps: ... -->`")
        if len(ids) != len(set(ids)):
            issues.append("任务 id 重复")
    elif kind == "design":
        sections = re.findall(r"^#{2,3}\s*\d+(?:\.\d+)*", text, re.M)
        if len(sections) < 3:
            issues.append("设计文档需要至少 3 个编号章节（如 `## 3.1 模块划分`）")
    elif kind == "proposal":
        if "##" not in text:
            issues.append("方案至少需要章节标题（##）")
    return issues
