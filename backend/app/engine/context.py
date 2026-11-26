"""上下文组装与预算截断（模型接入配置设计 §6）。"""
from __future__ import annotations

from app.models.budget import ContextBudget, estimate_tokens, truncate_to_tokens

PLATFORM_RULES = """你是 AM-Workflow 多智能体研发工作流中的角色 Agent。

平台规则（必须遵守）：
1. 只完成当前节点指派的工作，不擅自扩大范围、不越权修改上游工件。
2. 你没有审批权：不能批准、驳回或评价自己的产出；人工门由人决定。
3. 结论必须基于给定输入工件；缺少信息时明确写出假设，不得臆造需求。
4. 交接有损：引用已有工件时给出章节或编号，不要复述全文。
5. 使用中文输出，保留必要的英文术语与代码标识符。
"""


def build_system(role_prompt: str, memory: str = "", output_rules: str = "") -> str:
    parts = [PLATFORM_RULES, f"## 你的角色\n{role_prompt.strip()}"]
    if output_rules:
        parts.append(f"## 输出要求\n{output_rules.strip()}")
    if memory:
        parts.append(f"## 记忆（仅作背景）\n{memory}")
    return "\n\n".join(parts)


def fit_artifacts(artifacts: list[tuple[str, str]], token_budget: int) -> list[tuple[str, str]]:
    if not artifacts:
        return []
    per = max(1200, token_budget // len(artifacts))
    return [(label, truncate_to_tokens(text, per)) for label, text in artifacts]


def build_user(*, brief: str, artifacts: list[tuple[str, str]] | None = None,
               feedback: str | None = None, budget: ContextBudget | None = None,
               previous: tuple[str, str] | None = None) -> str:
    """拼用户消息。previous 是修订时的上一版：它要完整保留，不能和其他输入均分预算。

    上一版放不进模型预算时抛 ValueError——截断后当作全文写回会静默丢掉后半篇。
    """
    sections: list[str] = [f"## 任务\n{brief.strip()}"]
    limit = budget.section("artifact") if budget else 8000
    if previous is not None:
        label, text = previous
        others = list(artifacts or [])
        cost = estimate_tokens(text)
        if cost + 1200 * len(others) > limit:
            raise ValueError(
                f"上一版太大（约 {cost} token），在模型预算内无法连同其他输入一起修订")
        if others:
            fitted = fit_artifacts(others, max(1, limit - cost))
            blocks = [f"### {item_label}\n\n{item_text}" for item_label, item_text in fitted]
            sections.append("## 输入工件\n\n" + "\n\n".join(blocks))
        sections.append(f"## 上一版（修订基础）\n\n### {label}\n\n{text}")
    elif artifacts:
        fitted = fit_artifacts(list(artifacts), limit)
        blocks = [f"### {label}\n\n{text}" for label, text in fitted]
        sections.append("## 输入工件\n\n" + "\n\n".join(blocks))
    if feedback:
        sections.append(
            "## 人工/系统反馈（必须逐条处理，不能只做表面修改）\n" + feedback.strip()
        )
    return "\n\n".join(sections)


def doc_output_rules(kind: str, *, min_chars: int = 600) -> str:
    base = (
        "最终回复就是工件正文：直接输出 Markdown，不要问候、不要解释你做了什么、"
        "不要用 ```markdown 包裹整篇。正文至少 %d 字。" % min_chars
    )
    if kind == "requirement":
        base += (
            "\n需求条目必须使用 EARS 句式：`WHEN <触发> THE SYSTEM SHALL <行为>`，"
            "每条需求附 Given/When/Then 验收标准，编号从 R1 开始。"
        )
    elif kind == "task":
        base += (
            "\n任务清单必须使用规定行格式：`- [ ] 标题 <!-- id: T1.1; deps: ; "
            "design-section: 3.1; test-cases: TC-01; assignee: dev; wave: 1; status: pending -->`，"
            "deps 为空时留空，多个依赖用逗号分隔。"
        )
    elif kind == "design":
        base += "\n使用编号章节（如 `## 3.1 模块划分`），供任务分解引用 design-section。"
    return base


def dev_workspace_rules(workspace_hint: str) -> str:
    return f"""## 工作区与工具
你有一个隔离的工作区：{workspace_hint}。只能用相对路径操作它。
- write_file：写代码和测试文件（会自动建目录）
- read_file / list_files：查看已有内容
- run_command：执行命令，必须看到退出码；测试失败要修到通过
- finish：声明完成并总结（包含改了哪些文件、测试命令与结果）

交付标准：代码能运行、附单元测试、`python -m unittest discover -s . -p "test_*.py"` 通过。
不要修改工作区之外的文件，不要执行删除系统目录等危险命令。"""
