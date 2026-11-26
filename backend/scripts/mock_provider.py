r"""离线 OpenAI 兼容 Mock 服务（仅用于本地演示与端到端测试）。

启动：
    .\.venv\Scripts\python.exe scripts\mock_provider.py --port 8100

路由规则：
- 会议里的每一次调用，提示末尾都带一个调用标识（形如【调用类型：stance】），
  按标识查表回复，不靠提示词里的关键词猜；
- 文档节点按系统提示词里的角色关键词路由；
- 开发者角色模拟 write_file → write_file → run_command → finish 的工具调用序列，
  让平台无需真实 API Key 也能跑通完整的 SDLC 流程。

生产使用真实供应商时不需要启动本服务。
"""
from __future__ import annotations

import argparse
import json
import re
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="AM-Workflow Mock Provider")

MODELS = [
    {"id": "mock-fast", "object": "model", "owned_by": "mock"},
    {"id": "mock-coder", "object": "model", "owned_by": "mock"},
]


# ---------------------------------------------------------------- 文本工件
def _requirement(messages: list[dict]) -> str:
    text = """# 需求规格：AM-Workflow W1

## 1. 范围

覆盖想法到交付的最小闭环，不含向量记忆、多 run 并行与容器化执行。

## 2. 功能需求

### R1 供应商配置

WHEN 用户打开供应商设置页 THE SYSTEM SHALL 展示已配置的供应商、协议、脱敏密钥与模型数量。

验收标准：
- Given 已存在一个 openai_chat 供应商，When 打开设置页，Then 密钥仅显示末四位。
- Given 填写非法 base_url，When 点击测试连接，Then 返回可读错误且不写入数据库。

### R2 创建运行

WHEN 用户提交想法并选择模板 THE SYSTEM SHALL 创建 run、固化节点并写入 idea 工件。

验收标准：
- Given 想法非空，When 提交，Then 返回 run 快照且状态为 running。
- Given 模板不存在，When 提交，Then 返回 400 与明确提示。

### R3 阶段推进与人工门

WHEN 节点产出工件且到达人工确认点 THE SYSTEM SHALL 暂停运行并等待决定。

验收标准：
- Given 方案确认点待处理，When 通过，Then 运行继续且门状态为 approved。
- Given 需求确认点待处理，When 填写反馈并驳回，Then 目标节点带着反馈重跑。

### R4 任务图与并行执行

WHEN 任务分解完成 THE SYSTEM SHALL 解析任务清单、校验依赖并按波次并行执行。

验收标准：
- Given 两个无依赖任务，When 进入执行阶段，Then 两个开发节点同时处于 running。
- Given 任务依赖成环，When 保存任务图，Then 返回 400 且不落库。

### R5 验证与交付

WHEN 开发节点全部完成 THE SYSTEM SHALL 运行验证命令并据退出码判定结果。

验收标准：
- Given 单元测试通过，When 执行验证节点，Then 节点 done 且记录输出。
- Given 测试失败，When 执行验证节点，Then 节点失败并进入回流或转人工。

## 3. 非功能需求

- 事件写入必须 append-only，SSE 断线可按 seq 补发；
- SQLite 使用 WAL 与单连接，SSE 生成器不得持有 session；
- 所有模型调用受 run 级 token 预算约束。
"""
    joined = "\n".join(str(m.get("content") or "") for m in messages)
    if "R6" in joined or "周报" in joined or "导出" in joined:
        text = text.replace("## 3. 非功能需求", """### R6 周报导出

WHEN 成员在反馈列表点击「导出周报」THE SYSTEM SHALL 生成当前筛选结果的 Markdown 周报并下载。

验收标准：
- Given 已筛选出至少一条反馈，When 点击导出周报，Then 下载文件包含主题聚类与优先级建议。
- Given 筛选结果为空，When 点击导出周报，Then 提示没有可导出的内容且不生成文件。

## 3. 非功能需求""")
    points = _ledger_rows(joined)
    if points:
        count = 6 if "### R6" in text else 5
        text += ("\n\n## 需求追踪矩阵\n\n| 需求编号 | 来源决策点 | 方案章节 | 优先级 |\n"
                 "|---|---|---|---|\n")
        text += "".join(f"| R{number} | {points[(number - 1) % len(points)]['index']} | 总体方案 | P1 |\n"
                        for number in range(1, count + 1))
        text += ("\n## 非需求类决策\n\n| 决策点 | 不产出需求的理由 |\n|---|---|\n"
                 "| （无） | 每个已了结的决策点都有对应的需求 |\n")
    return text


def _design(messages: list[dict]) -> str:
    return """# 设计规格：AM-Workflow W1

## 1. 模块划分

- `api`：REST 路由与 SSE，负责鉴权占位、参数校验、序列化；
- `engine`：图调度、节点执行、门禁、事件、上下文、预算；
- `models`：供应商适配器、模型池、加密、token 估算；
- `artifacts`：工件读写、版本管理、front-matter；
- `memory`：L0/L1/L2 记忆写入与检索；
- `tools`：工作区文件与命令工具。

## 2. 数据模型

核心表：provider_configs、model_entries、agent_templates、workflows、runs、nodes、
node_attempts、tasks、events、artifacts、gates、messages、memories。
事件表以 (run_id, seq) 唯一，节点状态迁移必须先写事件再更新快照。

## 3. 调度与并发

1. 读取节点依赖，计算拓扑波次；
2. 同波节点用 asyncio.gather 并发，受供应商信号量限制；
3. 汇合节点等待全部上游 done；
4. 失败节点按 max_attempts 重试，超限转人工门；
5. 人工门挂起不占用协程，由决策事件唤醒。

## 4. 接口设计

- `POST /api/runs` 创建运行；
- `GET /api/runs/{id}` 返回快照，`GET /api/runs/{id}/events` 返回 SSE；
- `POST /api/gates/{id}/approve|reject` 处理人工门；
- `PATCH /api/runs/{id}/artifacts/{kind}` 人工编辑工件；
- `POST /api/nodes/{id}/retry` 人工重试失败节点。

## 5. 上下文与预算

按系统 5%、记忆 10%、工件 25%、历史 30% 分配输入预算，输出按 max_output_tokens
硬预留；超窗时依次截断工具结果、摘要历史、把工件降为引用，仍超则报错转人工。

## 6. 错误处理与恢复

- 适配器对 429/5xx/网络错误重试一次；
- 进程重启后作废未回执 attempt，把 running 节点重置为 pending；
- 待决门保持不变，恢复后继续等待人工决策。
"""


def _task_doc(messages: list[dict]) -> str:
    return """# 任务清单：AM-Workflow W1

- [ ] 实现运行创建与快照接口 <!-- id: T1; deps: ; design-section: 4; test-cases: TC-01; assignee: dev; wave: 1; status: pending -->
- [ ] 实现事件总线与 SSE 推送 <!-- id: T2; deps: ; design-section: 3; test-cases: TC-02; assignee: dev; wave: 1; status: pending -->

## 说明

两个任务无相互依赖，可同波并行；验证节点在两者完成后统一运行
`python -m unittest discover -s . -p "test_*.py"`。
"""


def _changelog(messages: list[dict]) -> str:
    return """# Changelog：AM-Workflow W1

## 交付内容

- 供应商与模型配置：CRUD、预设、测试连接、Fernet 加密；
- 编排引擎：拓扑分波、并行执行、失败重试、人工门、断点恢复；
- 固定 SDLC 模板：8 阶段、5 个人工门、任务图解析与展开；
- 工件链：idea / discussion / proposal / requirement / design / task / changelog；
- 前端：阶段列表、节点状态、日志抽屉、审批卡、工件查看与编辑；
- 记忆：L0 会话与 L1 角色经验卡最小实现。

## 验证证据

- 单元测试：`python -m unittest discover -s . -p "test_*.py"` 退出码 0；
- 端到端：mock 供应商下完整跑通 8 阶段；
- 恢复：杀进程重启后挂起 run 可继续，待决门不丢失。

## 已知限制

- Anthropic / Gemini 适配器暂不支持真流式（M1 补齐）；
- 向量检索、多 run 并行、容器化执行按计划延后。
"""


# ---------------------------------------------------------------- 会议调用
_CALL_TAG = re.compile(r"【调用类型：([a-z_]+)】")
_LEDGER_ROW = re.compile(
    r"^\|\s*(D\d+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*$",
    re.M,
)
_OPEN_STATUSES = ("讨论中", "复议中")


def _ledger_rows(text: str) -> list[dict]:
    """从提示文本里读出决策点账本表格的各行：编号、状态、标题、候选项、结论。

    同一个决策点可能出现多次（账本快照、纪要）；以最后一次为准。
    """
    rows: dict[str, dict] = {}
    for match in _LEDGER_ROW.finditer(text):
        conclusion = match.group(5).strip()
        rows[match.group(1)] = {
            "index": match.group(1), "status": match.group(2).strip(),
            "title": match.group(3).strip(),
            "options": [option.strip() for option in match.group(4).split("/")
                        if option.strip()],
            "conclusion": "" if conclusion in ("—", "") else conclusion,
        }
    return list(rows.values())


def _persona(text: str) -> str:
    """取"## 你的角色"那一段的第一句，用来区分发言人。"""
    match = re.search(r"## 你的角色\n(.+)", text)
    return match.group(1).strip()[:40] if match else ""


def _stances(text: str, *, yielding: bool) -> str:
    """对每个未了结的决策点表态。用户体验视角对最后一个未了结的决策点持反对意见，交锋之后让步。"""
    rows = [row for row in _ledger_rows(text) if row["status"] in _OPEN_STATUSES]
    items = []
    for position, row in enumerate(rows):
        opposed = position == len(rows) - 1 and "体验" in _persona(text) and not yielding
        choice = (row["options"][-1] if opposed else row["options"][0]) if row["options"] else ""
        items.append({"index": row["index"], "level": 5 if opposed else 1,
                      "reason": "多人共用时需要能区分反馈是谁的" if opposed else "同意目前的结论",
                      "choice": choice})
    return json.dumps({"stances": items}, ensure_ascii=False)


def _proposal_from_ledger(rows: list[dict], *, revised: bool) -> str:
    lines = ["# 建设方案"]
    if revised:
        lines += ["", "## 修改说明", "", "- 按反馈重开的决策点已重新讨论，结论以账本为准。"]
    lines += ["", "## 目标与成功标准", "",
              "把想法落成首版可用的最小闭环；每个阶段都有可验证的产出。",
              "", "## 范围", "", "必做：反馈的提交、归类与导出。不做：实时协作、细粒度权限。",
              "", "## 总体方案", ""]
    lines += [f"- {row['index']} {row['title']}：{row['conclusion'] or '尚有分歧，待用户决定'}"
              f"（{row['status']}）" for row in rows] or ["- （账本里还没有决策点）"]
    lines += ["", "## 关键流程", "", "提交反馈 → 归类 → 导出。",
              "", "## 里程碑与验收", "", "第一周：最小闭环可用，验收以可复现的检查为准。",
              "", "## 风险与缓解", "", "- 范围蔓延：首版功能冻结。"]
    return "\n".join(lines) + "\n"


def _fallback(text: str) -> str:
    """没有专门准备内容的调用：给一份格式正确的通用文档。"""
    return (
        "# 说明\n\n## 背景\n\n这是一份由离线 Mock 生成的通用文档，"
        "用来让流程在没有真实模型时也能走下去。它不包含针对当前想法的具体判断。\n\n"
        "## 要点\n\n- 范围：只覆盖最小闭环，不做的部分明确列出；\n"
        "- 验收：每一步都有可复现的检查；\n"
        "- 风险：范围蔓延，以及关键决策没有回退方案；对应的缓解是首版功能冻结、关键决策写明回退路径。\n\n"
        "## 后续\n\n接入真实模型后，这份文档会由模型按节点要求撰写，内容以实际输入的工件与反馈为准。\n"
    )


def _meeting_reply(kind: str, text: str) -> str:
    """会议里的一次调用。按调用标识给出格式正确的回复，内容尽量贴着提示里的账本与问题。"""
    rows = _ledger_rows(text)
    open_rows = [row for row in rows if row["status"] in _OPEN_STATUSES]
    if kind == "brief":
        return json.dumps({
            "brief": "目标：把想法落成首版可用的最小闭环。用户：提出想法的人及其团队。"
                     "约束：首版范围要小、每一步可验证。假设：小团队内部使用。",
            "questions": ["首版必须支持哪些平台或入口？"]}, ensure_ascii=False)
    if kind == "answer":
        listing = text.split("## 待澄清的问题", 1)[-1]
        count = len(re.findall(r"^\d+\.\s", listing, re.M))
        return json.dumps({"answers": [{"text": "材料中没有说明", "source": ""}
                                       for _ in range(count)]}, ensure_ascii=False)
    if kind == "roster":
        wanted = re.search(r"挑选 (\d+)", text)
        offered = re.findall(r"^- ([a-z_]+)｜", text, re.M)
        preferred = [key for key in ("architect", "ux", "qa") if key in offered]
        picked = (preferred + [key for key in offered if key not in preferred])
        picked = picked[:int(wanted.group(1)) if wanted else 2]
        return json.dumps({"participants": [
            {"role_key": key, "reason": "覆盖想法涉及的一类主要风险"} for key in picked]},
            ensure_ascii=False)
    if kind == "position":
        return json.dumps({"must": ["每一步都可验证"], "risks": ["范围蔓延", "关键决策没有回退方案"],
                           "route": "先做能跑通的最小闭环", "questions": []}, ensure_ascii=False)
    if kind == "agenda":
        return json.dumps({"points": [
            {"title": "首版的范围与入口", "question": "首版做哪些功能、从哪里进入",
             "options": ["网页版最小闭环", "桌面版最小闭环"], "raised_by": "架构师"},
            {"title": "多人共用时如何区分反馈", "question": "首版是否需要登录",
             "options": ["需要", "不需要"], "raised_by": "用户体验"}]}, ensure_ascii=False)
    if kind in ("stance", "restance"):
        return _stances(text, yielding=kind == "restance")
    subject = re.search(r"决策点 (D\d+)「([^」]*)」", text)
    label = f"{subject.group(1)}「{subject.group(2)}」" if subject else "这个决策点"
    if kind == "claim":
        return f"我支持{label}目前的结论：它覆盖了最主要的场景，实现成本最低。"
    if kind == "challenge":
        return f"关于{label}，我担心它忽略了多人共用时的区分问题，建议补上最小的区分手段。"
    if kind == "respond":
        return f"这个质疑有道理。我调整立场：{label}按质疑方的建议补上最小区分手段。"
    if kind == "exchange_summary":
        return json.dumps({"summaries": [
            {"index": row["index"],
             "summary": f"围绕{row['title']}，一方主张成本最低的做法，另一方担心多人共用时的区分问题，"
                        "主张方同意补上最小区分手段。"} for row in open_rows]}, ensure_ascii=False)
    if kind == "ruling":
        return json.dumps({"rulings": [
            {"index": row["index"], "conclusion": row["options"][0] if row["options"] else "按最小闭环处理",
             "rationale": "在成本与风险之间取折中", "dissent": "有参会者认为应当采用另一种做法"}
            for row in open_rows]}, ensure_ascii=False)
    if kind == "reopen":
        last = rows[-1] if rows else None
        point = ({"index": last["index"], "title": "", "question": "", "options": []} if last
                 else {"index": "", "title": "反馈提出的新问题", "question": "", "options": []})
        return json.dumps({"points": [point], "note": "按反馈重开最近的一个决策点"}, ensure_ascii=False)
    if kind in ("proposal", "proposal_revise"):
        return _proposal_from_ledger(rows, revised=kind == "proposal_revise")
    if kind == "proposal_check":
        return '{"ok": true, "issues": []}'
    if kind == "summarize":
        return ("较早的发言围绕首版范围与是否登录展开：一方强调成本最低的最小闭环，"
                "另一方强调多人共用时要能区分反馈；结论以账本为准。")
    if kind == "intervention_ack":
        return "收到。我会在接下来的讨论里先处理这一点。"
    return _fallback(text)


# ---------------------------------------------------------------- 开发工具序列
def _dev_tool_call(messages: list[dict]) -> dict:
    user = next((m.get("content") or "" for m in messages if m.get("role") == "user"), "")
    match = re.search(r"开发任务\s+([A-Za-z0-9_.\-]+)：(.+)", user)
    uid = (match.group(1) if match else "T1").strip()
    title = (match.group(2).splitlines()[0] if match else "示例任务").strip()[:60]
    slug = re.sub(r"[^0-9A-Za-z_]", "_", uid.replace(".", "_"))
    module = f"task_{slug}"
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    step = len(tool_msgs)

    if step == 0:
        code = f'''"""{title}（{uid}）的最小实现。"""

TASK_UID = "{uid}"
TASK_TITLE = "{title}"


def solution() -> str:
    """返回该任务的稳定标识，供接口层与测试复用。"""
    return f"{{TASK_UID}}:{{TASK_TITLE}}"


def describe() -> dict:
    return {{"uid": TASK_UID, "title": TASK_TITLE, "ok": True}}
'''
        return {"name": "write_file", "args": {"path": f"{module}.py", "content": code}}
    if step == 1:
        test = f'''import unittest

from {module} import TASK_UID, describe, solution


class Test{slug}(unittest.TestCase):
    def test_solution_contains_uid(self):
        self.assertIn(TASK_UID, solution())

    def test_describe_ok(self):
        self.assertTrue(describe()["ok"])


if __name__ == "__main__":
    unittest.main()
'''
        return {"name": "write_file", "args": {"path": f"test_{module}.py", "content": test}}
    if step == 2:
        return {"name": "run_command",
                "args": {"command": 'python -m unittest discover -s . -p "test_*.py"'}}
    return {"name": "finish",
            "args": {"summary": f"完成任务 {uid}：新增 {module}.py 与测试 test_{module}.py，"
                                "本地 unittest 全部通过。"}}


# ---------------------------------------------------------------- 响应构造
def _pm_plan(messages: list[dict]) -> str:
    """把设计章节索引变成一份可校验的任务候选 JSON（离线演示用）。"""
    joined = "\n".join(str(m.get("content") or "") for m in messages)
    product = "示例产品"
    match = re.search(r"^产品：(.+)$", joined, re.M)
    if match:
        product = match.group(1).strip()[:60]
    sections: list[str] = []
    index_block = re.search(r"## 设计章节索引\n(.+?)\n\n## 设计规格正文", joined, re.S)
    if index_block:
        for line in index_block.group(1).splitlines():
            line = line.strip()
            if line.startswith("- "):
                title = line[2:].strip()
                if title and title != "（未识别到章节标题）":
                    sections.append(title[:40])
    if len(sections) < 8:
        base = sections[0] if sections else "总体设计"
        filler = [
            "数据模型与存储", "接口与错误处理", "前端交互", "权限与安全",
            "测试与回归", "部署与监控", "验收与文档", "边界与降级",
        ]
        for title in filler:
            if len(sections) >= 8:
                break
            sections.append(f"{base}·{title}")
    sections = sections[:10]
    tasks = []
    for i, section in enumerate(sections):
        uid = f"T{i + 1}"
        if i == 0:
            deps: list[str] = []
        elif i in (1, 2):
            deps = ["T1"]
        elif i == 3:
            deps = ["T2"]
        elif i == 4:
            deps = ["T3"]
        elif i == 5:
            deps = ["T4", "T5"]
        elif i in (6, 7):
            deps = ["T6"]
        else:
            deps = ["T7"]
        role = "dev"
        if any(word in section for word in ("测试", "回归", "验收")):
            role = "test" if "测试" in section or "回归" in section else "review"
        elif any(word in section for word in ("设计", "交互", "界面")):
            role = "design"
        elif any(word in section for word in ("部署", "监控", "运维")):
            role = "ops"
        if i == 0:
            role = "pm"
        effort = ["M", "L", "M", "S", "L", "M", "L", "S", "M", "M"][i]
        priority = "P0" if i < 2 else ("P2" if i >= 7 else "P1")
        title = f"实现{section}"[:40]
        if len(title) < 6:
            title = f"实现章节{section}"[:40]
        tasks.append(
            {
                "uid": uid,
                "title": title,
                "description": (
                    f"依据设计章节「{section}」完成该模块的实现、边界处理与自测，"
                    "产出可运行代码、测试与必要的说明，确保与上下游接口一致。"
                )[:200],
                "deps": deps,
                "design_section": section,
                "acceptance": [
                    f"「{section}」的实现可运行，主流程有单元测试覆盖",
                    "失败路径有明确错误提示，验收命令退出码为 0",
                ],
                "role": role,
                "effort": effort,
                "priority": priority,
            }
        )
    payload = {
        "product": product,
        "summary": f"依据设计规格拆分出 {len(tasks)} 个可独立验收的任务，按依赖分波执行。",
        "tasks": tasks,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _pick(messages: list[dict], tools: list[dict] | None) -> tuple[str, dict | None]:
    if tools:
        return "", _dev_tool_call(messages)
    text = "\n".join(str(m.get("content") or "") for m in messages)
    tagged = _CALL_TAG.search(text)
    if tagged:                                   # 会议里的调用：只按调用标识路由
        return _meeting_reply(tagged.group(1), text), None
    system = next((m.get("content") or "" for m in messages if m.get("role") == "system"), "")
    if "计划分解器" in system or "只输出一个 JSON 对象" in system:
        return _pm_plan(messages), None
    if "EARS" in system:
        return _requirement(messages), None
    if "任务清单必须使用规定行格式" in system:
        return _task_doc(messages), None
    if "编号章节" in system:
        return _design(messages), None
    if "changelog" in system.lower():
        return _changelog(messages), None
    return _fallback(text), None


def _usage(messages: list[dict], text: str) -> dict:
    prompt = sum(len(m.get("content") or "") for m in messages) // 2
    completion = max(1, len(text) // 2)
    return {"prompt_tokens": max(1, prompt), "completion_tokens": completion,
            "total_tokens": max(1, prompt) + completion}


def _chunks(text: str, size: int = 28) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


@app.get("/v1/models")
def models() -> dict:
    return {"object": "list", "data": MODELS}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    body = await request.json()
    messages = body.get("messages") or []
    tools = body.get("tools") or None
    text, call = _pick(messages, tools)
    base = {"id": f"mock-{int(time.time() * 1000)}", "object": "chat.completion",
            "created": int(time.time()), "model": body.get("model") or "mock-fast"}
    if not body.get("stream"):
        message: dict = {"role": "assistant", "content": text or None}
        if call:
            message["tool_calls"] = [{
                "id": "call_mock_1", "type": "function",
                "function": {"name": call["name"],
                             "arguments": json.dumps(call["args"], ensure_ascii=False)},
            }]
        return JSONResponse({**base, "choices": [{"index": 0, "message": message,
                                                  "finish_reason": "tool_calls" if call else "stop"}],
                             "usage": _usage(messages, text)})

    async def event_stream():
        def send(payload: dict) -> str:
            return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

        first = {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                                      "finish_reason": None}]}
        yield send(first)
        if call:
            arguments = json.dumps(call["args"], ensure_ascii=False)
            delta = {"tool_calls": [{"index": 0, "id": "call_mock_1", "type": "function",
                                     "function": {"name": call["name"], "arguments": ""}}]}
            yield send({**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
            for piece in _chunks(arguments, 120):
                delta = {"tool_calls": [{"index": 0,
                                         "function": {"arguments": piece}}]}
                yield send({**base, "choices": [{"index": 0, "delta": delta,
                                                 "finish_reason": None}]})
            finish = "tool_calls"
        else:
            for piece in _chunks(text):
                yield send({**base, "choices": [{"index": 0, "delta": {"content": piece},
                                                 "finish_reason": None}]})
            finish = "stop"
        yield send({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                    "usage": _usage(messages, text)})
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
