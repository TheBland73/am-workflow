"""节点运行时：按节点类型执行（设计规格 §5.2、§5.6、§5.7）。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.artifacts.store import (
    latest_artifact,
    parse_front_matter,
    read_artifact,
    strip_outer_fence,
    workspace_dir,
    write_artifact,
)
from app.db.base import session_scope
from app.db.models import AgentTemplate, Node, Run, Task
from app.engine.agent_loop import AgentExceeded, _extract_json, run_agent
from app.engine.context import build_system, build_user, dev_workspace_rules, doc_output_rules
from app.engine.events import emit
from app.engine.graph import GraphError, compute_waves
from app.engine.tasks import parse_tasks, validate_tasks
from app.engine.validate import validate_artifact
from app.engine.usage import BudgetExceeded
from app.memory.store import memory_block, record_lesson
from app.models.budget import ContextBudget
from app.models.pool import pool
from app.models.types import ProviderError
from app.tools.registry import Toolbox


class NodeFailure(RuntimeError):
    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


@dataclass
class NodeContext:
    run_id: str
    node_id: int
    node_key: str
    type: str
    name: str
    phase: str
    config: dict
    assignee: dict
    attempt_id: int
    attempt_no: int
    feedback: str | None = None
    idea: str = ""
    extra: dict = field(default_factory=dict)


DEFAULT_INPUTS: dict[str, list[str]] = {
    "discussion": ["idea"],
    "proposal": ["idea", "discussion"],
    "review": ["proposal"],
    "requirement": ["proposal"],
    "design": ["requirement", "proposal"],
    "task": ["design"],
    "test": ["design"],
    "changelog": ["idea", "discussion", "proposal", "requirement", "design", "task"],
}


# ---------------- 通用读取 ----------------
def _agent_template(role_key: str) -> dict | None:
    with session_scope() as s:
        row = s.query(AgentTemplate).filter(AgentTemplate.role_key == role_key).first()
        if row is None:
            return None
        return {
            "role_key": row.role_key,
            "name": row.name,
            "system_prompt": row.system_prompt,
            "tools_json": list(row.tools_json or []),
            "max_steps": row.max_steps,
        }


def _artifact_body(run_id: str, kind: str) -> str:
    raw = read_artifact(run_id, kind)
    if raw is None:
        return ""
    _meta, body = parse_front_matter(raw)
    return body.strip()


def _input_artifacts(run_id: str, kinds: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for kind in kinds:
        body = _artifact_body(run_id, kind)
        if body:
            out.append((kind, body))
    return out


def _task_row(run_id: str, task_uid: str) -> dict | None:
    with session_scope() as s:
        row = (
            s.query(Task)
            .filter(Task.run_id == run_id, Task.task_uid == task_uid)
            .one_or_none()
        )
        if row is None:
            return None
        return {
            "task_uid": row.task_uid, "title": row.title, "deps": list(row.deps_json or []),
            "design_section": row.design_section, "test_cases": list(row.test_cases_json or []),
            "wave": row.wave, "status": row.status,
            "role_key": (row.assignee_json or {}).get("role_key") or "dev",
        }


def _mark_task(run_id: str, task_uid: str, status: str) -> None:
    with session_scope() as s:
        row = (
            s.query(Task)
            .filter(Task.run_id == run_id, Task.task_uid == task_uid)
            .one_or_none()
        )
        if row:
            row.status = status


# ---------------- 入口 ----------------
async def execute_node(ctx: NodeContext) -> dict:
    if ctx.type == "PASS":
        return {"summary": "输入已记录"}
    if ctx.type == "AGENT":
        if (ctx.config or {}).get("task_uid"):
            return await _run_dev(ctx)
        return await _run_doc_agent(ctx)
    if ctx.type == "VERIFY":
        return await _run_verify(ctx)
    if ctx.type == "REVIEW":
        return await _run_review(ctx)
    if ctx.type == "MEETING":
        from app.meeting.runner import run_meeting

        return await run_meeting(ctx)
    if ctx.type == "TASKGRAPH":
        return await _run_taskgraph(ctx)
    if ctx.type in ("DECISION",):
        return {"summary": "无分支条件，直接通过"}
    if ctx.type in ("RESTORE", "CHECKPOINT"):
        return {"summary": f"{ctx.type} 节点已通过"}
    raise NodeFailure(f"暂不支持的节点类型：{ctx.type}", fatal=True)


# ---------------- 文档类 Agent ----------------
async def _run_doc_agent(ctx: NodeContext) -> dict:
    role = (ctx.assignee or {}).get("role")
    if not role:
        raise NodeFailure("AGENT 节点缺少 assignee.role", fatal=True)
    template = _agent_template(role)
    if template is None:
        raise NodeFailure(f"角色模板不存在：{role}", fatal=True)
    model = pool.pick_role_model(role)
    if model is None:
        raise NodeFailure(
            "尚未配置可用模型：请先在「设置 → 供应商」添加供应商与模型并测试连接", fatal=True
        )

    kind = (ctx.config or {}).get("artifact")
    if not kind:
        raise NodeFailure("AGENT 节点缺少 config.artifact", fatal=True)
    inputs = list((ctx.config or {}).get("inputs") or DEFAULT_INPUTS.get(kind, []))
    artifacts = _input_artifacts(ctx.run_id, inputs)
    missing = [k for k in inputs if k not in {label for label, _ in artifacts}]
    if missing:
        await emit(ctx.run_id, "node_log", ctx.node_id,
                   {"level": "warn", "message": f"输入工件缺失：{', '.join(missing)}"})

    previous = _artifact_body(ctx.run_id, kind) if (ctx.config or {}).get("append") else ""
    memory = memory_block(role_key=role, run_id=ctx.run_id)
    budget = ContextBudget(model.context_window, model.max_output_tokens)
    system_prompt = build_system(template["system_prompt"], memory, doc_output_rules(kind))
    instruction = (ctx.config or {}).get("instruction") or f"完成「{ctx.name}」节点的工作。"
    revised: tuple[str, str] | None = None
    if ctx.feedback and not (ctx.config or {}).get("append"):
        # 带着反馈重跑是修订，不是重写：把上一版完整交给模型，让它逐条处理反馈
        last_version = _artifact_body(ctx.run_id, kind)
        if last_version:
            revised = (f"{kind}（上一版）", last_version)
            instruction += (
                "\n这是一次修订：在上一版的基础上逐条处理反馈，不重写反馈没有涉及的内容；"
                "每处修改的下面用一行“修改说明：”写明对应哪条意见。")
    try:
        user_prompt = build_user(
            brief=instruction, artifacts=artifacts, feedback=ctx.feedback, budget=budget,
            previous=revised,
        )
    except ValueError as exc:
        raise NodeFailure(str(exc), fatal=True) from exc
    result = await run_agent(
        run_id=ctx.run_id, node_id=ctx.node_id, attempt_id=ctx.attempt_id, model=model,
        system_prompt=system_prompt, user_prompt=user_prompt, toolbox=None,
        max_steps=max(1, int(template["max_steps"] or 3)),
    )
    body = strip_outer_fence(result.content)
    if previous:
        body = previous.rstrip() + "\n\n---\n\n" + body
    issues = validate_artifact(kind, body)
    if issues:
        raise NodeFailure("工件校验未通过：" + "；".join(issues))
    record = write_artifact(
        ctx.run_id, kind, body, created_by=role, inputs=inputs, status="draft"
    )
    return {
        "summary": f"{kind} v{record['version']} 已生成（{len(body)} 字）",
        "artifact": record,
    }


# ---------------- 开发 Agent ----------------
# 编排工作台把任务按角色（dev/test/design/pm/review/ops）提交；执行仍走 dev 工具链，
# 但用一句话把该角色的交付偏好讲清楚，避免「测试任务写实现、文档任务改代码」。
ROLE_INTENT: dict[str, str] = {
    "test": "本任务偏测试：补齐 test_*.py 用例与回归覆盖，不要把业务实现塞进测试文件。",
    "design": "本任务偏设计：落地样式、组件结构与交互细节，保持与既有设计语言一致。",
    "pm": "本任务偏文档与协作：产出 README/说明与可直接运行的脚本，不改业务实现。",
    "review": "本任务偏评审：阅读现有实现，修改要小而明确，并在 finish 里列出问题清单。",
    "ops": "本任务偏运维：产出部署/监控/健康检查配置与说明，避免引入新依赖。",
}


async def _run_dev(ctx: NodeContext) -> dict:
    task_uid = str((ctx.config or {}).get("task_uid"))
    task = _task_row(ctx.run_id, task_uid)
    if task is None:
        raise NodeFailure(f"任务不存在：{task_uid}", fatal=True)
    role = str(task.get("role_key") or "dev")
    template = _agent_template("dev")
    if template is None:
        raise NodeFailure("角色模板不存在：dev", fatal=True)
    model = pool.pick_role_model(role) or pool.pick_role_model("dev")
    if model is None:
        raise NodeFailure(
            "尚未配置可用模型：请先在「设置 → 供应商」添加供应商与模型并测试连接", fatal=True
        )

    workspace = workspace_dir(ctx.run_id)
    design = _artifact_body(ctx.run_id, "design")
    requirement = _artifact_body(ctx.run_id, "requirement")
    section = task["design_section"] or ""
    description = str((ctx.config or {}).get("description") or "").strip()
    intent = ROLE_INTENT.get(role, "")
    brief = (
        f"开发任务 {task_uid}：{task['title']}\n"
        f"角色：{role}\n"
        f"任务说明：{description or '（未提供，按标题与章节理解）'}\n"
        f"设计章节：{section or '（未指定）'}\n"
        f"验收用例：{', '.join(task['test_cases']) or '（未指定）'}\n\n"
        "要求：只实现该任务范围内的代码与单元测试；测试必须实际运行并通过。"
        + (f"\n{intent}" if intent else "")
    )
    budget = ContextBudget(model.context_window, model.max_output_tokens)
    memory = memory_block(role_key="dev", run_id=ctx.run_id)
    system_prompt = build_system(
        template["system_prompt"], memory, dev_workspace_rules(str(workspace))
    )
    user_prompt = build_user(
        brief=brief,
        artifacts=[("design", design), ("requirement", requirement)],
        feedback=ctx.feedback,
        budget=budget,
    )
    toolbox = Toolbox(workspace, allow_shell=True)
    result = await run_agent(
        run_id=ctx.run_id, node_id=ctx.node_id, attempt_id=ctx.attempt_id, model=model,
        system_prompt=system_prompt, user_prompt=user_prompt, toolbox=toolbox,
        max_steps=max(1, int(template["max_steps"] or 16)),
    )
    if result.stopped_by != "finish":
        raise NodeFailure("Agent 没有调用 finish 声明完成，视为未完成")
    _mark_task(ctx.run_id, task_uid, "done")
    return {"summary": result.content[:1000], "task_uid": task_uid, "steps": result.steps}


# ---------------- 验证 ----------------
async def _run_verify(ctx: NodeContext) -> dict:
    command = (ctx.config or {}).get("command")
    if not command:
        raise NodeFailure("VERIFY 节点缺少 config.command", fatal=True)
    workspace = workspace_dir(ctx.run_id)
    toolbox = Toolbox(workspace, allow_shell=True)
    outcome = await toolbox.execute(
        "run_command",
        {"command": command, "timeout_s": int((ctx.config or {}).get("timeout_s") or 180)},
    )
    await emit(ctx.run_id, "node_log", ctx.node_id,
               {"level": "info" if not outcome.is_error else "error",
                "message": outcome.output[-2000:]})
    if outcome.is_error:
        raise NodeFailure(f"验证命令失败：\n{outcome.output[-2000:]}")
    return {"summary": f"验证通过：{command}", "command": command,
            "output": outcome.output[-1500:]}


# ---------------- 代码评审 ----------------
_TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".json", ".toml", ".yaml", ".yml", ".ini", ".cfg",
    ".ts", ".tsx", ".js", ".jsx", ".css", ".html", ".sql", ".sh", ".ps1",
}
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", ".pytest_cache"}


def _workspace_digest(workspace: Path, *, max_files: int = 16,
                      per_file: int = 5000, total: int = 26000) -> tuple[str, list[str]]:
    """返回（文件清单 + 内容摘要，文件名列表）。跳过二进制/缓存/构建产物。"""
    entries: list[tuple[str, Path]] = []
    for path in sorted(workspace.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(workspace).as_posix()
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > 300_000:
            continue
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        entries.append((rel, path))
    names = [rel for rel, _ in entries]
    if not entries:
        return "（workspace 为空）", names
    # 代码优先、然后文档，避免把预算全花在 README 上。
    entries.sort(key=lambda item: (0 if item[0].endswith(".py") else 1, item[0]))
    chunks: list[str] = []
    used = 0
    for rel, path in entries[:max_files]:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        text = text[:per_file]
        remaining = total - used
        if remaining <= 0:
            break
        text = text[:remaining]
        used += len(text)
        chunks.append(f"### {rel}\n```\n{text}\n```")
    inventory = "\n".join(f"- {rel}" for rel in names[:120]) or "（空）"
    return inventory + "\n\n" + "\n\n".join(chunks), names


_REVIEW_SYSTEM = (
    "你是资深代码评审（reviewer）。你不修改代码，只评审。\n"
    "根据任务验收标准与实际文件内容，判断交付是否可接受。\n"
    "必须只输出一个 JSON 对象，不要 Markdown 围栏、说明文字或额外字段：\n"
    '{"verdict":"pass" 或 "block","summary":"一句话结论",'
    '"issues":[{"severity":"blocker"|"major"|"minor","task_uid":"T1",'
    '"file":"相对路径","message":"问题是什么","suggestion":"怎么改"}]}\n'
    "规则：\n"
    "1. 只有真正确认的阻塞问题才标 blocker；可改进但能接受标 minor。\n"
    "2. blocker/major 必须对应到任务 uid（task_uid），并给出文件路径。\n"
    "3. 文件不存在或没看到实现时，用 severity=blocker 说明缺失项。\n"
    "4. 不要编造未在清单中出现的文件。"
)


async def _run_review(ctx: NodeContext) -> dict:
    model = pool.pick_role_model("review") or pool.pick_role_model("dev")
    if model is None:
        raise NodeFailure(
            "尚未配置可用模型：请先在「设置 → 供应商」添加供应商与模型并测试连接", fatal=True
        )

    workspace = workspace_dir(ctx.run_id)
    digest, names = _workspace_digest(workspace)
    with session_scope() as s:
        tasks = (
            s.query(Task)
            .filter(Task.run_id == ctx.run_id)
            .order_by(Task.wave.asc(), Task.id.asc())
            .all()
        )
        task_rows = [
            {
                "uid": t.task_uid, "title": t.title,
                "acceptance": list(t.test_cases_json or []),
                "role": (t.assignee_json or {}).get("role_key") or "dev",
                "status": t.status,
            }
            for t in tasks
        ]
    task_lines = "\n".join(
        f"- {row['uid']} [{row['status']}] {row['title']}｜验收："
        f"{'；'.join(row['acceptance']) or '（未填写）'}"
        for row in task_rows
    ) or "（没有任务）"
    round_no = int((ctx.config or {}).get("round") or 1)
    max_rounds = max(1, int((ctx.config or {}).get("max_rounds") or 2))
    feedback = (ctx.feedback or "").strip()
    user_prompt = (
        f"运行目标：{ctx.idea}\n"
        f"评审轮次：{round_no}/{max_rounds}\n\n"
        f"任务与验收标准：\n{task_lines}\n\n"
        f"验证命令结果：verify 已通过（测试命令执行成功）。\n\n"
        f"workspace 文件：\n{digest}\n"
        + (f"\n上一轮人工反馈：{feedback}\n" if feedback else "")
        + "\n请按系统要求输出评审 JSON。"
    )
    result = await run_agent(
        run_id=ctx.run_id, node_id=ctx.node_id, attempt_id=ctx.attempt_id, model=model,
        system_prompt=_REVIEW_SYSTEM, user_prompt=user_prompt, toolbox=None,
        max_steps=1, temperature=0.2,
    )
    data = _extract_json(result.content)
    if data is None:
        raise NodeFailure("评审模型没有返回合法 JSON，请重试或检查模型输出能力")

    valid_uids = {row["uid"] for row in task_rows}
    issues: list[dict] = []
    for item in list(data.get("issues") or [])[:30]:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or "minor").lower()
        if severity not in {"blocker", "major", "minor"}:
            severity = "minor"
        uid = str(item.get("task_uid") or "").strip().upper()
        issues.append({
            "severity": severity,
            "task_uid": uid if uid in valid_uids else "",
            "file": str(item.get("file") or "")[:240],
            "message": str(item.get("message") or "")[:800],
            "suggestion": str(item.get("suggestion") or "")[:800],
        })
    blocking = [
        item for item in issues
        if item["severity"] in {"blocker", "major"} and item["task_uid"]
    ]
    verdict = "block" if str(data.get("verdict") or "").lower() != "pass" or blocking else "pass"
    targets: list[str] = []
    for item in blocking:
        if item["task_uid"] not in targets:
            targets.append(item["task_uid"])
    summary = str(data.get("summary") or ("评审发现阻塞问题" if verdict == "block" else "评审通过"))[:1500]

    lines = [
        "# 代码评审报告",
        f"- 结论：{'阻塞' if verdict == 'block' else '通过'}",
        f"- 轮次：{round_no}/{max_rounds}",
        f"- 问题：{len(issues)}（阻塞 {len(blocking)}）",
        "",
        "## 摘要",
        summary,
        "",
    ]
    if issues:
        lines.append("## 问题清单")
        for item in issues:
            lines.append(
                f"### [{item['severity'].upper()}] "
                f"{item['task_uid'] or '运行级'} · {item['file'] or '（未标注文件）'}"
            )
            lines.append(item["message"] or "（未提供描述）")
            if item["suggestion"]:
                lines.append(f"建议：{item['suggestion']}")
            lines.append("")
    body = "\n".join(lines).strip()
    record = write_artifact(
        ctx.run_id, "review", body, created_by="review",
        inputs=[row["uid"] for row in task_rows], status="approved" if verdict == "pass" else "draft",
    )
    reviewed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report = {
        "verdict": verdict, "summary": summary, "issues": issues,
        "blocking": len(blocking), "targets": targets, "round": round_no,
        "max_rounds": max_rounds, "reviewed_at": reviewed_at,
        "model": model.display_name, "files": names[:120], "artifact_version": record["version"],
    }
    with session_scope() as s:
        node = s.get(Node, ctx.node_id)
        if node is not None:
            config = dict(node.config_json or {})
            history = list(config.get("review_history") or [])
            history.append({
                "round": round_no, "verdict": verdict, "issues": len(issues),
                "blocking": len(blocking), "at": reviewed_at, "summary": summary[:300],
            })
            config["review"] = report
            config["review_history"] = history[-8:]
            node.config_json = config
    return {
        "summary": f"评审{'阻塞' if verdict == 'block' else '通过'}："
                   f"{len(issues)} 个问题 / {len(blocking)} 个阻塞",
        "verdict": verdict, "issues": len(issues), "blocking": len(blocking),
        "targets": targets, "artifact": record, "round": round_no,
    }


# ---------------- 任务图 ----------------
async def _run_taskgraph(ctx: NodeContext) -> dict:
    mode = (ctx.config or {}).get("mode", "parse")
    if mode == "parse":
        return await _taskgraph_parse(ctx)
    if mode == "expand":
        return await _taskgraph_expand(ctx)
    raise NodeFailure(f"未知 TASKGRAPH 模式：{mode}", fatal=True)


async def _taskgraph_parse(ctx: NodeContext) -> dict:
    raw = read_artifact(ctx.run_id, "task")
    if raw is None:
        raise NodeFailure("未找到 task.md 工件", fatal=True)
    _meta, body = parse_front_matter(raw)
    parsed = parse_tasks(body)
    try:
        validate_tasks(parsed)
    except GraphError as exc:
        raise NodeFailure(f"任务图非法：{exc}") from exc

    created: list[dict] = []
    updated: list[dict] = []
    with session_scope() as s:
        existing = {
            row.task_uid: row
            for row in s.query(Task).filter(Task.run_id == ctx.run_id).all()
        }
        wanted = {t["task_uid"] for t in parsed}
        for uid, row in existing.items():
            if uid not in wanted:
                s.delete(row)
        for item in parsed:
            row = existing.get(item["task_uid"])
            if row is None:
                row = Task(
                    run_id=ctx.run_id, task_uid=item["task_uid"], title=item["title"],
                    deps_json=item["deps"], design_section=item["design_section"],
                    test_cases_json=item["test_cases"],
                    assignee_json={"role": item["assignee"] or "dev"},
                    wave=item["wave"], status=item["status"],
                )
                s.add(row)
                created.append({"task_uid": item["task_uid"], "title": item["title"],
                                "wave": item["wave"], "deps": item["deps"]})
            else:
                row.title = item["title"]
                row.deps_json = item["deps"]
                row.design_section = item["design_section"]
                row.test_cases_json = item["test_cases"]
                row.wave = item["wave"]
                updated.append({"task_uid": item["task_uid"], "title": item["title"],
                                "wave": item["wave"], "deps": item["deps"]})
    for item in created:
        await emit(ctx.run_id, "task_created", ctx.node_id, item)
    for item in updated:
        await emit(ctx.run_id, "task_updated", ctx.node_id, item)
    return {"summary": f"任务图已构建：{len(parsed)} 个任务", "tasks": created or updated}


async def _taskgraph_expand(ctx: NodeContext) -> dict:
    with session_scope() as s:
        tasks = (
            s.query(Task).filter(Task.run_id == ctx.run_id).order_by(Task.wave, Task.id).all()
        )
        task_list = [
            {"task_uid": t.task_uid, "title": t.title, "deps": list(t.deps_json or []),
             "wave": t.wave, "design_section": t.design_section}
            for t in tasks
        ]
    if not task_list:
        raise NodeFailure("没有可展开的任务", fatal=True)

    created: list[dict] = []
    with session_scope() as s:
        nodes = {n.node_key: n for n in s.query(Node).filter(Node.run_id == ctx.run_id).all()}
        for task in task_list:
            key = f"dev_{task['task_uid']}"
            deps = ["expand_exec"] + [f"dev_{d}" for d in task["deps"]]
            config = {"task_uid": task["task_uid"], "task_title": task["title"],
                      "design_section": task["design_section"]}
            node = nodes.get(key)
            if node is None:
                node = Node(
                    run_id=ctx.run_id, node_key=key, type="AGENT",
                    name=f"开发 {task['task_uid']} {task['title']}", phase="execution",
                    status="pending", assignee_json={"role": "dev"}, deps_json=deps,
                    config_json=config, max_attempts=3, order_index=100 + len(nodes),
                )
                s.add(node)
                s.flush()
                nodes[key] = node
                created.append({"node_key": key, "name": node.name})
            else:
                node.deps_json = deps
                node.config_json = {**config, "feedback": (node.config_json or {}).get("feedback")}
                node.status = "pending"
                node.wave = None
        verify = nodes.get("verify")
        if verify is not None:
            verify.deps_json = [f"dev_{t['task_uid']}" for t in task_list]
        s.flush()
        all_nodes = s.query(Node).filter(Node.run_id == ctx.run_id).all()
        deps_map = {n.node_key: list(n.deps_json or []) for n in all_nodes}
        waves = compute_waves(deps_map)
        for node in all_nodes:
            node.wave = waves.get(node.node_key, node.wave)
        task_count = len(task_list)
    for item in created:
        await emit(ctx.run_id, "node_created", ctx.node_id, item)
    return {"summary": f"已展开 {task_count} 个并行开发节点", "nodes": created}


# ---------------- 供调度器使用 ----------------
def classify_failure(exc: Exception) -> tuple[str, bool]:
    message = str(exc) or exc.__class__.__name__
    if isinstance(exc, BudgetExceeded):
        return message, True
    if isinstance(exc, ProviderError) and "尚未配置" in message:
        return message, True
    if isinstance(exc, NodeFailure):
        return message, exc.fatal
    if isinstance(exc, AgentExceeded):
        return message, False
    return message, False


def lesson_for(ctx: NodeContext, message: str) -> None:
    role = (ctx.assignee or {}).get("role") or ctx.node_key
    try:
        record_lesson(
            role_key=role, run_id=ctx.run_id, subject=f"{ctx.node_key} 失败经验",
            content=message[:600], created_by="orchestrator",
        )
    except Exception:  # 经验卡写入失败不应影响主流程
        pass


def latest_artifact_info(run_id: str, kind: str) -> dict | None:
    return latest_artifact(run_id, kind)
