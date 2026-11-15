# AM-Workflow · W1 最小原型

网页端多智能体软件开发工作流平台。引擎负责控制流、上下文与状态，模型只是可替换的节点；
人工审批是一等公民，完成与否由证据（退出码、schema、工件）判定，而不是由模型自称完成。

本仓库是**设计规格 W1（2026-11-17 验收）的最小可运行实现**：
固定 SDLC 模板跑通「想法 → 讨论 → 方案 → 需求 → 设计 → 任务 → 并行开发 → 交付」，
含 5 个人工门、SSE 实时事件、工件链落盘、断点恢复，以及一个可离线演示的 Mock 供应商。

设计依据（已合入 main，见 `docs/`）：

- `docs/设计规格说明书.md`：实现基线（§4 数据模型、§5 API、§7 W1 范围）
- `docs/research/前沿技术调研与技术选型.md`：选型论据
- `docs/模型接入配置设计.md`：Provider / Model / Binding 与上下文预算

---

## 1. 当前状态

| 项 | 状态 |
| --- | --- |
| 后端 API（FastAPI + SQLAlchemy + SQLite WAL） | ✅ 可运行 |
| 编排引擎（拓扑分波、并行、重试、门禁、恢复） | ✅ |
| 固定 SDLC 模板（8 阶段 / 16+2 节点） | ✅ |
| 人工门（通过 / 驳回+反馈+回退 / 编辑后通过） | ✅ 5 个门 |
| SSE 事件流 + Last-Event-ID 补发 | ✅ |
| 工件链（idea/discussion/proposal/requirement/design/task/changelog） | ✅ 7 类 |
| L0 / L1 记忆最小实现 | ✅ |
| 前端（终末地控制台风格：运行列表 / 运行详情 / 人工门 / 工件编辑 / 供应商 / 角色模板） | ✅ 已构建 |
| 离线 Mock 供应商（流式 + 工具调用） | ✅ `scripts/mock_provider.py` |
| 单元测试 | ✅ 14 passed |

一次完整端到端运行（Mock 模型，含需求门驳回重做）：

```
run 68588a9c31c4  finished
18 个节点全部 done；requirement attempt=2（反馈被采纳，产出 R6 导出周报）
7 类工件 active（过门后 front-matter 状态为 approved）；2 个开发任务完成；verify 退出码 0
tokens: in 16,999 / out 3,015，预算 800,000 内
```

---

## 2. 快速开始

要求：Python 3.11+（本机验证 3.13）、Node 18+（本机 24）、npm。

### 2.1 安装

```powershell
cd TeamWk\am-workflow

# 后端
python -m venv .venv
uv pip install --python .\.venv\Scripts\python.exe -r backend\requirements.txt
# 没有 uv 时：.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt

# 前端
cd frontend
npm install
npm run build          # 产物 frontend/dist，由 FastAPI 直接托管
```

> 本机 `pip` 的 resolver 会空转（安装卡 30 分钟），推荐 `uv`；
> 若 npm 跳过了 esbuild 的安装脚本，构建前执行 `npm rebuild esbuild`。

### 2.2 一键启动（Windows）

首次按 2.1 装好依赖后，直接双击根目录的 `start-am-workflow.bat`：脚本会在后台拉起
Mock 供应商（:8100）与 AM-Workflow（:8099），等健康检查通过后自动打开浏览器。
停止时双击 `stop-am-workflow.bat`。

- 日志：`logs\api.log`、`logs\api.err.log`、`logs\mock.log`
- 已在运行时重复双击只会再打开一个浏览器标签，不会重复起进程
- 实际逻辑在 `launcher.ps1` / `stopper.ps1`，Windows 专用；其他平台按下节手动启动

### 2.3 手动启动

两个进程，都在 `backend` 目录下：

```powershell
# 终端 1：离线 Mock 模型供应商（OpenAI 兼容，:8100）
..\.venv\Scripts\python.exe -m uvicorn scripts.mock_provider:app --host 127.0.0.1 --port 8100

# 终端 2：AM-Workflow（API + 前端静态站点，:8099）
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8099
```

打开 <http://127.0.0.1:8099/>。开发前端时改用：

```powershell
cd frontend
npm run dev            # http://127.0.0.1:5173，/api 代理到 8099
```

### 2.4 离线演示（一条命令跑完整个流程）

```powershell
cd backend
..\.venv\Scripts\python.exe scripts\e2e_demo.py --auto --demo-reject
```

脚本会：创建「Mock 供应商」+ `mock-fast` 模型 → 建 run → 自动过门；
在需求门先驳回一次（反馈要求补 R6 导出周报），验证退回重做，再依次通过其余门。
最后打印工件、任务、节点与 token 用量。

也可以只驱动已有 run：`scripts\drive_run.py --latest --demo-reject`；
手工在网页上逐门审批则直接打开运行详情页，审批卡会自动弹出。

### 2.5 接入真实模型

在「供应商与模型」页新增供应商（可选预设：DeepSeek / OpenAI / GLM / 豆包 / Kimi / 百炼 /
SiliconFlow / OpenRouter / Anthropic / Gemini / 自建），填 Base URL 与 API Key，
然后在「角色模板」里把 `dev` 等角色绑定到该模型。密钥用 Fernet 加密落盘，接口只回显末四位。

### 2.6 界面

前端采用「终末地控制台」视觉：纸白 `#F4F4F2`、墨 `#161616`、单一信号黄 `#FFFA00`（只做填充，
表示当前位置/可操作/提醒），直角与 1px 发丝线，切角只给可点击元素；运行详情页由命令栏、
阶段圆盘、执行图、运行账本与内联人工门裁决卡组成，运行列表/供应商/角色模板共用同一套 token。

设计资产来自 [`hhikr/endfield-ui-skill`](https://github.com/hhikr/endfield-ui-skill)（`endfield.css`、
`dial.js`、等距线稿），字体 Saira 以 SIL Open Font License 随仓库分发（`frontend/src/styles/fonts/`）。
开发前端时，`AM_API_TARGET` 可覆盖 `/api` 代理目标（默认 `http://127.0.0.1:8099`）：

```powershell
$env:AM_API_TARGET="http://127.0.0.1:8099"
npm run dev
```

---

## 3. 目录结构

```
am-workflow/
├── backend/
│   ├── app/
│   │   ├── main.py            FastAPI 入口、lifespan、静态托管
│   │   ├── config.py          路径与环境配置
│   │   ├── db/                engine/session（SQLite WAL + 单连接池）与 ORM（13 张表）
│   │   ├── api/               providers/models/agent-templates/runs/artifacts/gates/tasks/nodes/memories
│   │   ├── engine/
│   │   │   ├── orchestrator.py 调度循环、重试、门禁决策、恢复
│   │   │   ├── runners.py      节点执行器（PASS/AGENT/VERIFY/TASKGRAPH/HUMAN_GATE…）
│   │   │   ├── gates.py        人工门状态迁移与下游重置
│   │   │   ├── templates.py    模板加载、seed、节点固化
│   │   │   ├── tasks.py        任务清单行格式解析与校验
│   │   │   ├── events.py       事件总线（seq 单调、内存直推、SSE 格式）
│   │   │   ├── agent_loop.py   模型↔工具循环、JSON 协议降级
│   │   │   └── context.py      提示词拼装、平台规则、角色输出约束
│   │   ├── models/            类型、加密、预算估算、Provider 适配器与模型池
│   │   ├── tools/registry.py  文件读写、命令执行、finish
│   │   ├── artifacts/store.py 工件版本、front-matter、文件落盘
│   │   ├── memory/store.py    L0/L1/L2 记忆
│   │   └── agents/templates/roles.py  8 个角色的提示词与工具集
│   ├── scripts/               mock_provider / e2e_demo / drive_run / check_sse
│   ├── tests/                 core / gates / api
│   └── requirements.txt
├── frontend/                  Vite + React 18 + TS + AntD 5 + @xyflow/react + zustand
├── templates/sdlc_workflow.json  固定 SDLC 模板（8 阶段 5 门）
├── docs/                      设计文档（调研 / 设计规格 / 模型接入）
├── runs/                      运行产物（工件、工作区），git 忽略
└── data/                      SQLite 与密钥，git 忽略
```

---

## 4. 关键设计

### 4.1 编排循环

- 节点依赖来自模板 `deps`；`compute_waves` 拓扑分层，同波并发（`asyncio.gather`），波间串行，汇合即屏障。
- `orchestrator.start(run_id)` 幂等：同一 run 已在跑时只记一次唤醒，不重复起循环。
- 每个节点执行前写 `node_attempts`（attempt+1），事件先于状态落库可见；
  失败非 fatal 且未达 `max_attempts` 时自动带反馈重试，达到上限转人工门（`escalation`）。
- 无任何可执行节点且未全部完成时判定死锁，run 置 failed 并记录原因。

### 4.2 人工门

| 动作 | 效果 |
| --- | --- |
| approve | 门节点 done；关联工件状态 → approved；若该门是失败升级门，则节点回 pending 重试 |
| reject | 必填 `feedback` + `target_node`；目标节点与全部下游重置 pending，反馈注入目标节点配置，重跑时进入提示词 |
| edit | 审批卡内直接改工件正文 → 写入新版本（status=approved）→ 再 approve |

门挂起时 run 置 `waiting_human`，**不占协程**；重启后待决门原样保留。

### 4.3 事件与 SSE

- `events` 表 `(run_id, seq)` 唯一，seq 单调；状态迁移先写事件。
- `node_token` 只走内存广播、不落库（高频、可丢失）。
- SSE 支持 `Last-Event-ID`：断线重连先补发历史，再订阅实时队列；15s 心跳注释帧；
  终态事件后服务端主动关闭连接。生成器**不持有**数据库 session。
- 前端所有视图由快照 + 事件驱动：结构化事件触发快照刷新，token 事件只更新日志流。

### 4.4 工件链

- 9 类工件，W1 产出 7 类；每类最多一个 active 版本，人工编辑/重做自动 `superseded`。
- 文件落盘 `runs/<run_id>/artifacts/<kind>.vN.md`，并维护 `<kind>.md` 指向当前版本。
- 每份工件带 YAML front-matter：`id/kind/status/version/inputs/created`。
- 需求强制 EARS（`WHEN … THE SYSTEM SHALL …`）+ 验收标准 Given/When/Then；
  任务清单强制行内元数据（`id/deps/design-section/test-cases/assignee/wave/status`），
  由 `engine/tasks.py` 解析成 Task 与执行节点。

### 4.5 上下文预算

系统 5% / 记忆 10% / 工件 25% / 历史 30%，输出按 `max_output_tokens` 硬预留，
剩余约 30% 作为工具结果与压缩缓冲；超窗按「工具截断 → 历史摘要 → 工件降为引用 → 转人工」降级。
run 级 token 预算在每次调用后扣减，超限抛 `BudgetExceeded` 并升级人工门。

### 4.6 模型层

- **Provider**（怎么连：协议/URL/密钥/并发）→ **Model**（调什么：名称/窗口/能力/价格）→ **Binding**（角色模板绑定）。
- 协议适配器：`openai_chat` 完整流式 + 工具调用；`anthropic_messages` / `gemini_generate` 可工作但暂为伪流式（M1 补齐）。
- 能力降级：模型无 `cap_tools` 时自动改用提示词 JSON 协议（`{"tool":…}` / `{"final":…}`）。
- 429/5xx/网络错误重试 1 次（指数退避）；错误信息中文化，401/403/404/429/5xx 分别提示。
- 供应商并发用进程内 `Semaphore` 限制；密钥 Fernet 加密，`AM_SECRET_KEY` 支持任意口令（SHA-256 派生）。

### 4.7 恢复

启动时 `orchestrator.recover()`：

1. `running` 的 attempt 标记失败（"进程重启，未回执的 attempt 作废"），节点回 `pending`；
2. `waiting_human` 的 run 保持门不动，若等待节点缺 open gate 则补建；
3. 其余未终态 run 重新入队。

验收演示：跑到门 → 杀 API 进程 → 重启 → 页面刷新，门仍在，继续通过即可跑完。

---

## 5. API 一览（前缀 `/api`）

| 分组 | 端点 |
| --- | --- |
| 供应商 | `GET /presets`；`GET/POST /providers`；`PATCH/DELETE /providers/{id}`；`POST /providers/{id}/test` |
| 模型 | `GET/POST /models`；`PATCH/DELETE /models/{id}` |
| 角色模板 | `GET /agent-templates`；`PATCH /agent-templates/{id}` |
| 运行 | `POST /runs`；`GET /runs`；`GET /runs/{id}`；`POST /runs/{id}/stop` |
| 事件 | `GET /runs/{id}/events`（SSE）；`GET /runs/{id}/events/history` |
| 工件 | `GET /runs/{id}/artifacts`；`GET/PATCH /runs/{id}/artifacts/{kind}` |
| 门 | `GET /gates`；`POST /gates/{id}/approve`；`POST /gates/{id}/reject` |
| 任务 | `GET/POST /runs/{id}/tasks`；`PATCH/DELETE /runs/{id}/tasks/{uid}` |
| 节点 | `GET /nodes/{id}`；`POST /nodes/{id}/retry` |
| 记忆 | `GET /memories`；`PATCH /memories/{id}` |

---

## 6. 配置

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `AM_DATABASE_URL` | `sqlite:///data/am.db` | 数据库连接串 |
| `AM_DATA_DIR` | `<repo>/data` | 密钥与 DB 目录 |
| `AM_RUNS_DIR` | `<repo>/runs` | 运行产物目录 |
| `AM_SECRET_KEY` | 自动生成 | Fernet 口令；不设则首次启动写入 `data/secret.key` |
| `AM_NODE_CONCURRENCY` | 4 | 同时执行的节点数上限 |
| `AM_MAX_REWORK_ROUNDS` | 3 | 失败自动重做上限，超过转人工 |
| `VITE_API_BASE` | `/api` | 前端 API 前缀（开发代理已配置） |

SQLite 约束：`pool_size=1`、`check_same_thread=False`、`busy_timeout=5000`、WAL + `synchronous=NORMAL`；
因此**禁止在 `session_scope()` 内再开启 session**（工件、记忆、模型池读取都在事务外做）。

---

## 7. 测试

```powershell
cd backend
..\.venv\Scripts\python.exe -m pytest tests -q
```

覆盖：图/任务解析/front-matter/预算估算、门禁通过/驳回/升级重试、供应商与模型 CRUD、模板绑定校验。

---

## 8. 已知限制（M1 计划）

- Anthropic / Gemini 适配器暂为伪流式；M1 补真流式与工具调用。
- 未做：向量检索、记忆面板、执行看板、Twin 数字分身、多 run 调度面板、容器化执行、MCP 工具接入。
- 前端单包体积 ~1.4MB（AntD + xyflow），未做路由级代码分割。
- 任务清单暂不支持独立 `TASKGRAPH validate` 节点（解析时已校验唯一性与依赖合法性）。
- Windows 下命令执行把 `python` 前缀替换为当前解释器；Linux 行为一致但未在 CI 验证。

---

## 9. 与设计文档的差异（勘误）

1. 文档 §3.3 引用的 `./research/...` 与实际同级目录不符，实际为同级三份 Markdown。
2. §7.1 写「4 个人工门」，但 §7.3 demo 十步包含验收门；本实现落地 **5 个门**：
   方案确认 / 需求审查 / 设计审查 / 任务图确认 / 验收确认。
3. bug：W1 要求「2 个任务并行」，实现为任务图展开后同波并发，验收时两个 dev 节点确实并行执行
   （事件时间戳交错），但受 Mock 响应速度影响，单次演示中肉眼不易分辨。
