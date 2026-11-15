"""PM 任务分解生成：模型只产出候选任务，校验与编排交给 planner（ORCH-01）。"""
from __future__ import annotations

import json
import logging
import re

from app.db.base import session_scope
from app.db.models import Plan, PlanSnapshot, PlanTask
from app.engine.plan_bus import plan_bus
from app.engine.planner import extract_sections, validate_plan
from app.models.pool import pool
from app.models.types import GenParams, Message, ProviderError

log = logging.getLogger("am-workflow.plan")

SYSTEM_PROMPT = """你是软件开发计划分解器（PM Agent）。输入是一份设计规格，输出一份可被程序校验的任务候选清单。

硬性要求：
1. 只输出一个 JSON 对象，不要 Markdown 围栏，不要解释文字。
2. 结构：{"product": string, "summary": string, "tasks": [任务]}
3. 每个任务字段：
   - uid: "T" + 数字，唯一，如 T1；
   - title: 6–40 字，动词开头；
   - description: 20–200 字，写清输入、动作、输出；
   - deps: 依赖的 uid 数组，必须指向本清单内已存在的任务，不能自依赖；
   - design_section: 必须逐字引用设计规格里真实存在的章节标题；
   - acceptance: 1–5 条可测试的验收标准；
   - role: dev/test/design/pm/review/ops 之一；
   - effort: S/M/L/XL 之一；
   - priority: P0/P1/P2 之一。
4. 任务数量 8–40，覆盖设计规格的主要模块与测试、验收；依赖必须构成有向无环图。
5. 不得把具体产品名写进通用规则；只依据本次设计规格。
"""

REPAIR_PROMPT = """你上一次的输出不是可解析的 JSON 或不符合 schema。
错误：{error}
请只重新输出一个合法 JSON 对象（无围栏、无解释），保持同样的任务内容与字段约束。"""


def _extract_json(text: str) -> dict:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("输出中没有 JSON 对象")
    data = json.loads(cleaned[start:end + 1])
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise ValueError("JSON 缺少 tasks 数组")
    return data


def _design_brief(design_text: str) -> str:
    sections = extract_sections(design_text)
    index = "\n".join(
        f"- {item['title']}" for item in sections[:40]
    ) or "（未识别到章节标题）"
    body = design_text[:9000]
    return f"## 设计章节索引\n{index}\n\n## 设计规格正文（截断）\n{body}"


async def generate_plan(plan_id: str) -> None:
    with session_scope() as s:
        plan = s.get(Plan, plan_id)
        if plan is None or plan.status != "generating":
            return
        design_text = plan.design_text or ""
        product = plan.product_name or "未命名产品"

    model = pool.pick_role_model("pm") or pool.pick_role_model("task_breaker")
    if model is None:
        await _fail(plan_id, "没有可用模型：请先在供应商与模型中配置并启用一个模型。")
        return
    with session_scope() as s:
        plan = s.get(Plan, plan_id)
        if plan is None:
            return
        plan.model_id = model.id
    adapter = pool.adapter_for(model)
    messages = [
        Message(role="system", content=SYSTEM_PROMPT),
        Message(
            role="user",
            content=f"产品：{product}\n\n{_design_brief(design_text)}",
        ),
    ]
    text = ""
    try:
        sem = await pool.slot(model.provider)
        async with sem:
            async for event in adapter.stream(
                messages, None, GenParams(model=model.model_name, temperature=0.2,
                                           max_tokens=min(model.max_output_tokens, 4096))
            ):
                with session_scope() as s:
                    plan = s.get(Plan, plan_id)
                    if plan is None or plan.status != "generating":
                        return
                if event.kind == "text_delta" and event.text:
                    text += event.text
                    await plan_bus.publish(plan_id, "plan_token", {"text": event.text})
                elif event.kind == "done" and event.message and event.message.content:
                    text = event.message.content
        data = _extract_json(text)
    except Exception as exc:  # 解析失败先修复一次
        error_text = str(exc)
        await plan_bus.publish(plan_id, "plan_token", {"text": "\n[校验失败，尝试修复一次]\n"})
        try:
            repair = [
                *messages,
                Message(role="assistant", content=text[:6000]),
                Message(role="user", content=REPAIR_PROMPT.format(error=error_text[:500])),
            ]
            repair_sem = await pool.slot(model.provider)
            async with repair_sem:
                result = await adapter.chat(
                    repair, None,
                    GenParams(model=model.model_name, temperature=0.0,
                              max_tokens=min(model.max_output_tokens, 4096)),
                )
            text = result.content or ""
            data = _extract_json(text)
        except Exception as second:
            await _fail(plan_id, f"PM 输出解析失败：{error_text}；修复一次仍未通过：{second}")
            return

    with session_scope() as s:
        plan = s.get(Plan, plan_id)
        if plan is None or plan.status != "generating":
            return
        plan.raw_output = text[:200000]
        plan.sections_json = extract_sections(design_text)
        result = validate_plan(data.get("tasks") or [], design_text)
        # 用校验后的任务覆盖计划任务（无效也保留，便于人工修正）
        s.query(PlanTask).filter(PlanTask.plan_id == plan_id).delete()
        snapshot = result.get("snapshot")
        tasks = result.get("tasks") or []
        for index, task in enumerate(tasks):
            s.add(
                PlanTask(
                    plan_id=plan_id,
                    task_uid=task.get("task_uid") or f"T{index + 1}",
                    title=task.get("title") or "",
                    description=task.get("description") or "",
                    deps_json=task.get("deps") or [],
                    design_section=task.get("design_section"),
                    acceptance_json=task.get("acceptance") or [],
                    role_key=task.get("role_key") or "dev",
                    effort=task.get("effort") or "M",
                    priority=task.get("priority") or "P1",
                    order_index=index,
                )
            )
        last = (
            s.query(PlanSnapshot)
            .filter(PlanSnapshot.plan_id == plan_id)
            .order_by(PlanSnapshot.version.desc())
            .first()
        )
        version = (last.version + 1) if last else 1
        if snapshot:
            s.query(PlanSnapshot).filter(
                PlanSnapshot.plan_id == plan_id, PlanSnapshot.is_valid.is_(True)
            ).update({PlanSnapshot.is_valid: False})
            s.add(
                PlanSnapshot(
                    plan_id=plan_id, version=version, tasks_json=tasks,
                    dag_json=snapshot["dag"], waves_json=snapshot["waves"],
                    critical_path_json=snapshot["critical_path"],
                    metrics_json=snapshot["metrics"],
                    validation_json={"ok": True, "errors": []}, is_valid=True,
                )
            )
            plan.status = "valid"
            plan.error = None
        else:
            s.add(
                PlanSnapshot(
                    plan_id=plan_id, version=version, tasks_json=tasks,
                    dag_json={}, waves_json={}, critical_path_json=[], metrics_json={},
                    validation_json={"ok": False, "errors": result["errors"]},
                    is_valid=False,
                )
            )
            plan.status = "invalid"
            plan.error = result["errors"][0]["message"] if result["errors"] else "校验未通过"
        payload = {
            "plan_id": plan_id,
            "status": plan.status,
            "version": version,
            "metrics": snapshot["metrics"] if snapshot else {},
            "errors": result["errors"],
        }
    await plan_bus.publish(plan_id, "plan_generated", payload)


async def _fail(plan_id: str, message: str) -> None:
    with session_scope() as s:
        plan = s.get(Plan, plan_id)
        if plan is None:
            return
        if plan.status == "generating":
            plan.status = "failed"
        plan.error = message[:2000]
    await plan_bus.publish(plan_id, "plan_failed", {"plan_id": plan_id, "error": message})
    log.warning("plan %s failed: %s", plan_id, message)


__all__ = ["generate_plan", "SYSTEM_PROMPT", "ProviderError"]
