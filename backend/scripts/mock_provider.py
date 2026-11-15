r"""离线 OpenAI 兼容 Mock 服务（仅用于本地演示与端到端测试）。

启动：
    .\.venv\Scripts\python.exe scripts\mock_provider.py --port 8100

它按系统提示词判断当前角色，返回结构合规的工件正文；对开发者角色则模拟
write_file → write_file → run_command → finish 的工具调用序列，让平台无需
真实 API Key 也能跑通完整的 SDLC 流程。

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
def _idea_of(messages: list[dict]) -> str:
    for m in messages:
        if m.get("role") != "user":
            continue
        text = m.get("content") or ""
        match = re.search(r"### idea\n\n(.+?)(?:\n\n### |\Z)", text, re.S)
        if match:
            return match.group(1).strip()[:400]
    return "一个用于团队协作的软件项目"


def _discussion(messages: list[dict], counter: bool) -> str:
    idea = _idea_of(messages)
    if counter:
        return f"""## 质疑与反方意见

- **范围偏大**：主张中的「{idea[:40]}…」在第一版里同时做实时协作与权限体系，交付风险高；
  建议 W1 只做单人流程 + 只读共享，把实时协作放到 M1。
- **验收标准不清**：主张只写了"跑通"，没有可观测证据；每个阶段必须落到工件与退出码。
- **模型依赖**：讨论中没有定义供应商不可用时的降级路径，应保留离线 mock 与重试预算。
- **人工门缺失**：方案、需求、设计三处一旦无人确认，返工成本会指数上升。

## 收敛建议

1. 保留主张的「阶段化流水线 + 工件链」主线；
2. 把实时协作、细粒度权限降级为后续迭代；
3. 每个阶段增加人工确认门与失败回流路径；
4. 先以 2 个任务的并行开发验证编排器，再扩展模板。
"""
    return f"""## 主张

针对「{idea[:60]}」，建议采用**阶段化多智能体流水线**：

1. 想法先落成工件，再进入讨论，避免口头需求丢失；
2. 讨论采用正反两方各一轮，形成可引用的讨论稿；
3. 方案、需求、设计、任务四类工件逐级收敛，每级设人工确认门；
4. 任务图按依赖分波，同波并行执行，验证节点统一跑测试命令；
5. 全流程事件写入 events 表，前端只消费事件，支持断线重放。

## 预期收益

- 每个阶段都有可审计工件，返工范围可控；
- 模型只是节点，控制流由编排器掌握，失败可重试、可转人工；
- 离线 mock 与真实供应商共用同一适配器，演示与生产一致。

## 风险

- 上下文超窗需要预算裁剪；
- 并发写 SQLite 必须单连接 + WAL。
"""


def _proposal(messages: list[dict]) -> str:
    idea = _idea_of(messages)
    return f"""# 建设方案：AM-Workflow 最小可用版本

## 1. 背景与目标

用户想法：{idea[:200]}

目标是用一个可配置的工作流引擎，把「想法 → 讨论 → 方案 → 需求 → 设计 → 任务 → 执行 → 交付」
固化为可审计、可中断、可恢复的流程；W1 先交付可演示的最小闭环。

## 2. 总体架构

- 前端：Vite + React + Ant Design，画布只读展示阶段与节点，SSE 驱动状态；
- 后端：FastAPI + SQLAlchemy，SQLite（WAL）单连接，事件表 append-only；
- 编排器：拓扑分波、同波并发、汇合屏障、失败回流、人工门挂起；
- 模型层：Provider → Model → Binding 三层解耦，openai_chat 协议优先；
- 工件层：Markdown + YAML front-matter，版本化落盘，旧版本标记 superseded。

## 3. 关键流程

1. 创建 run 时固化模板节点并写入 idea 工件；
2. 讨论节点串行产出主张与质疑，合并为一份讨论稿；
3. 方案/需求/设计/任务四个文档节点各设人工门；
4. 任务图解析后展开为并行开发节点，VERIFY 节点统一跑单元测试；
5. 交付节点汇总工件链与执行证据，生成 changelog。

## 4. 风险与对策

- 模型不稳定：适配器重试一次，失败转人工；
- 上下文超窗：按系统/记忆/工件/历史配额裁剪；
- 进程重启：启动时作废未回执 attempt，重排 ready 节点；
- 预算耗尽：run 级 token 预算，超限暂停并请求人工。

## 5. 验收标准

- 8 个阶段全部产出工件，7 类工件齐备；
- 5 个人工门可批准、驳回（带反馈与目标节点）、编辑后批准；
- 并行开发真实调用模型并通过单元测试；
- 杀进程重启后挂起的 run 可继续。
"""


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

WHEN 节点产出工件且到达人工门 THE SYSTEM SHALL 暂停运行并生成待审批记录。

验收标准：
- Given 方案门待审批，When 点击通过，Then 运行继续且门状态为 approved。
- Given 需求门待审批，When 填写反馈并驳回并指定目标节点，Then 目标节点重跑且下游重置。

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
def _pick(messages: list[dict], tools: list[dict] | None) -> tuple[str, dict | None]:
    system = next((m.get("content") or "" for m in messages if m.get("role") == "system"), "")
    if tools:
        call = _dev_tool_call(messages)
        return "", call
    if "EARS" in system:
        return _requirement(messages), None
    if "任务清单必须使用规定行格式" in system:
        return _task_doc(messages), None
    if "编号章节" in system:
        return _design(messages), None
    if "changelog" in system.lower():
        return _changelog(messages), None
    if "质疑者" in system or "反方" in system:
        return _discussion(messages, counter=True), None
    if "主张者" in system:
        return _discussion(messages, counter=False), None
    return _proposal(messages), None


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
