# AM-Workflow · W1 最小原型

网页端多智能体软件开发工作流平台。引擎负责控制流、上下文与状态，模型只是可替换的节点；
人工审批是一等公民，完成与否由证据（退出码、schema、工件）判定，而不是由模型自称完成。

本仓库是**设计规格 W1（2026-11-17 验收）的最小可运行实现**：
固定 SDLC 模板跑通「想法 → 讨论 → 方案 → 需求 → 设计 → 任务 → 并行开发 → 交付」，
含 5 个人工门、SSE 实时事件、工件链落盘、断点恢复，以及一个可离线演示的 Mock 供应商。

设计依据（已合入 main，见 `docs/`）：

- `docs/设计规格说明书.md`：设计基线 v0.2（W1 已实现 8 阶段 / 18 节点；评审、测试、部署为后续迭代设计）
- `docs/research/前沿技术调研与技术选型.md`：选型论据（§11 增补：评审/测试/分布式专项）
- `docs/评审与测试体系设计.md`：三级评审 + 四级测试 + 缺陷闭环 + 质量钩子 + 调度拆屏障（模块详设）
- `docs/分布式执行模式设计.md`：多 PC 协作模式（**封存仅设计**，单机模式是主线）
- `docs/模型接入配置设计.md`：Provider / Model / Binding 与上下文预算
- `docs/迭代优化-可靠执行.md`：并发闸门 / 超时 / 退避 / 心跳 / 指标（对标 Temporal、Airflow、Prefect）
- `docs/迭代优化-可交互编排画布.md`：React Flow 画布、坐标/依赖落库、设计覆盖率
- `docs/编排工作台-迭代记录.md`：R1–R6 体验迭代（正确性 / 视觉语义 / 可编辑 / 提交运行 / 生成链路 / 阶段视图）
- `docs/评审与验收门.md`：代码评审 Agent、阻塞自动回退、验收门、评审面板与指标
- `docs/节点交互设计.md`：节点动作矩阵、状态迁移、接口契约与审计事件（节点交互增量 v1，模块文档 1）
- `docs/运行图交互设计.md`：运行图节点选中/血缘、节点检查器、`graphLayout` 共享模块（节点交互增量 v1，模块文档 2）
- `docs/tasks/real-model-ledger-test.md`：真实模型测试案例（「随手账本」需求规格与 5 门检查点）

---

## 1. 当前状态

| 项 | 状态 |
| --- | --- |
| 后端 API（FastAPI + SQLAlchemy + SQLite WAL） | ✅ 可运行 |
| 编排引擎（依赖就绪并发、重试、门禁、恢复） | ✅ |
| 可靠执行（全局并发闸门 / 节点超时 / 指数退避 / attempt 心跳 / 取消收据 / 运行指标） | ✅ 迭代一期 |
| 固定 SDLC 模板（8 阶段 / 18 节点，讨论含主张/质疑/回应/蒸馏 4 节点，展开后追加 `dev_*`） | ✅ |
| 人工门（通过 / 驳回+反馈+回退 / 编辑后通过） | ✅ 5 个门 |
| SSE 事件流 + Last-Event-ID 补发 | ✅ |
| 工件链（idea/discussion/proposal/requirement/design/task/review/changelog） | ✅ 8 类 |
| L0 / L1 记忆最小实现 | ✅ |
| 前端（终末地控制台风格：运行列表 / 运行详情 / 人工门 / 工件编辑 / 供应商 / 角色模板） | ✅ 已构建 |
| 编排工作台（设计规格 → PM 提议 → 校验/波次/关键路径 → 阶段导航 + 四视图 → 编辑/撤销 → 按角色提交运行） | ✅ ORCH-01 |
| 代码评审与验收门（REVIEW 节点 / 阻塞自动回退 / 验收人工门 / 评审面板与指标） | ✅ 迭代二期 |
| 节点交互（运行图节点选中/血缘 + 节点检查器 + 重试/重置/跳过/注入反馈 + 审计事件） | ✅ 迭代三期 |
| 离线 Mock 供应商（流式 + 工具调用） | ✅ `scripts/mock_provider.py` |
| 单元测试 | ✅ 87 passed |

> 本轮「可靠执行」迭代的对标项目、改动清单与验收方式见
> [docs/迭代优化-可靠执行.md](docs/迭代优化-可靠执行.md)。

一次完整端到端运行（Mock 模型，含需求门驳回重做）：

```
run 4e02c0f8928b  finished
20 个节点全部 done（含三方讨论 4 节点）；requirement attempt=2（反馈被采纳，产出 R6 导出周报）
7 类工件 active（过门后 front-matter 状态为 approved；discussion 随发言节点递增至 v4）；2 个开发任务完成；verify 退出码 0
tokens: in 23,098 / out 3,564，预算 1,200,000 内
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

> 若 Windows 的端口排除范围占用了 8099/8100（`netsh int ipv4 show excludedportrange protocol=tcp`），
> 可改用 8375/8376：启动命令加 `--port 8375` / `--port 8376`，并把供应商 Base URL 与
> `AM_API_TARGET` 同步改成对应端口。

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

模型名不必手抄：选中预设时创建供应商会自动写入该预设的内置模型；也可以在供应商展开区点
「拉取模型」，从供应商的 `GET /models` 拉取列表并勾选导入（已存在的自动跳过）。「测试」按钮在有
本地模型时会带该模型真发一句对话，返回延迟与回复；模型行的 ⚗ 按钮可单独验证某一个模型名。

想完整跑一遍真实模型？照 [`docs/tasks/real-model-ledger-test.md`](docs/tasks/real-model-ledger-test.md)
做：一份「随手账本」需求规格 + 5 个门的检查点 + 验收清单 + 故障排查；
也可以用 `backend\scripts\setup_real_model.py` 一步完成「建供应商/模型 → 绑角色 → 建运行」。

### 2.6 界面

前端采用「终末地控制台」视觉：纸白 `#F4F4F2`、墨 `#161616`、单一信号黄 `#FFFA00`（只做填充，
表示当前位置/可操作/提醒），直角与 1px 发丝线，切角只给可点击元素；运行详情页由命令栏、
阶段圆盘、执行图、运行账本与内联人工门裁决卡组成，运行列表/供应商/角色模板共用同一套 token。

设计资产来自 [`hhikr/endfield-ui-skill`](https://github.com/hhikr/endfield-ui-skill)（`endfield.css`、
`dial.js`、等距线稿），字体 Saira 以 SIL Open Font License 随仓库分发（`frontend/src/styles/fonts/`）。
开发前端时，`AM_API_TARGET` 可覆盖 `/api` 代理目标（默认 `http://127.0.0.1:8099`）：

```powershell
$env:AM_API_TARGET="http://127.0.0.1:8375"
npm run dev
```

### 2.7 编排工作台（ORCH-01）

「编排 / PLANS」把一段设计规格变成可编辑、可校验、可提交的执行计划：

1. 新建计划：粘贴 Markdown、上传 `.md`，或从某次运行的 `design` 工件导入；
2. 点「生成任务」：PM Agent 只负责提议任务；依赖校验、波次、并行组、关键路径全部由服务端确定性计算；
3. 计划页左侧是按设计章节聚合的「阶段」导航（PROGRESS、章节/任务/阶段计数、未承接章节），右侧四个视图：
   - **任务详情**：内嵌编辑表单，可改标题、描述、角色、effort、priority、章节、依赖与验收，400ms 防抖自动重新校验；
     成环、缺依赖、章节不可追溯都会给出错误码、位置与修复提示；草稿按任务记忆，切换节点不丢未保存内容；
   - **流程监控**：Start → Phase 01..N → Verify → End 的只读阶段链，标注 CRITICAL 阶段与角色/点数，点阶段定位首个任务；
   - **活动**：创建 / 生成 / 校验 / 提交 / 归档时间线，带版本、通过标记、错误摘要与运行链接；
   - **执行图**：可拖拽的画布。拖节点自动保存坐标，右侧手柄拖到目标左侧即新增依赖，点击连线确认后删除依赖；
     泳道按波次分列，普通依赖、关键路径、跨角色交接、校验错误四种连线有不同线型；
     选中节点会高亮它的祖先/后代链路，其余节点退到背景（只是视觉聚焦，仍参与依赖执行与提交运行）；
     页面默认选中第一个任务，点击其他卡片即切换聚焦，再点章节 chip 可取消章节过滤；
     顶部「设计覆盖」列出每个章节承接的任务数，点章节只看这条链路，未覆盖章节用黄黑斜纹标出；
     双击卡片可直接改名；页头「全览 / 重置布局 / 历史」分别用于适配全图、恢复自动布局、查看并回滚校验快照；
     `Ctrl+Z`（macOS 为 `Cmd+Z`）撤销最近一次图编辑：移动节点、双击改名、依赖增删、
     任务详情保存、新建任务，最多 30 步；输入框内保留浏览器自带的文字撤销，
     计划提交或归档后撤销锁定。删除任务暂不可撤销（重建会丢 `task_uid` 与入边依赖）；
   - 已提交或归档的计划，任务详情只读，状态条提供运行链接。
4. 有效后可提交：
   - **仅保存**：计划置 `committed`，不创建 Run；
   - **创建运行**：可填运行预算（10,000–20,000,000 tokens），并选择是否追加「代码评审 Agent」与「验收人工门」（默认都开）。
     写入 `Task` 后按任务生成 `dev_*` 节点、一个 `verify` 节点、可选的 `review` 评审节点与 `gate_accept` 验收门；
     节点 config 携带 `role_key/description/effort/priority`，执行时按角色选模型与追加提示词，注入 design 工件并立即开始执行；
     评审发现 blocker/major 问题时自动把对应任务与 verify 退回重跑，默认最多返工 2 轮，用尽后升级人工门；
   - 验收门通过后运行才结束；门通过会把 `review` 工件置为 `approved`，可直接在运行页阅读评审报告。
5. 生成过程在「设计覆盖」面板显示 `PM STREAM`（实时 token/字数/进度）；生成结束后可在「设计来源」查看原始输出。
   设计规格超过 16,000 字时保留开头与结尾章节，中段标注省略，避免长文档后半段没有任务；
6. 已提交计划只读，状态条提供运行链接。

离线演示（Mock）：

```powershell
cd backend
..\.venv\Scripts\python.exe scripts\smoke_plan.py
```

脚本会用 `tests/fixtures/designs/chat-product.md` 创建计划、生成、校验并仅保存提交，打印任务数与波次/关键路径指标。
产品名只存在于 `tests/fixtures/designs/`；页面、API、模板与断言不依赖具体产品。

### 2.8 升级说明：内置角色模板

内置角色模板在应用启动时按**内容指纹**（名称 / 提示词 / 工具集 / 记忆作用域 / 步数 / 停止条件）同步：

- 未在「角色模板」页改过的内置角色会随版本自动刷新；
- 用户在「角色模板」里改过的角色会保留，不会被覆盖；模型绑定与创建时间始终保留；
- 旧库首次升级时没有指纹可比对，会一次性刷新全部内置字段（此前的本地修改会被覆盖）。

---

## 3. 目录结构

```
am-workflow/
├── backend/
│   ├── app/
│   │   ├── main.py            FastAPI 入口、lifespan、静态托管
│   │   ├── config.py          路径与环境配置
│   │   ├── db/                engine/session（SQLite WAL + 单连接池）与 ORM（16 张表，含 plans/plan_tasks/plan_snapshots）
│   │   ├── api/               providers/models/agent-templates/runs/artifacts/gates/tasks/nodes/memories/plans
│   │   ├── engine/
│   │   │   ├── orchestrator.py 调度循环、并发闸门、超时、退避、心跳、门禁决策、恢复
│   │   │   ├── runners.py      节点执行器（PASS/AGENT/VERIFY/TASKGRAPH/HUMAN_GATE…）
│   │   │   ├── gates.py        人工门状态迁移与下游重置
│   │   │   ├── templates.py    模板加载、seed、节点固化
│   │   │   ├── tasks.py        任务清单行格式解析与校验
│   │   │   ├── planner.py      计划校验、波次、关键路径、快照
│   │   │   ├── plan_generation.py PM Agent 生成、JSON 修复、SSE
│   │   │   ├── plan_bus.py     计划生成事件总线
│   │   │   ├── events.py       事件总线（seq 进程内缓存、内存直推、SSE 格式）
│   │   │   ├── agent_loop.py   模型↔工具循环、JSON 协议降级
│   │   │   └── context.py      提示词拼装、平台规则、角色输出约束
│   │   ├── models/            类型、加密、预算估算、Provider 适配器与模型池
│   │   ├── tools/registry.py  文件读写、命令执行、finish
│   │   ├── artifacts/store.py 工件版本、front-matter、文件落盘
│   │   ├── memory/store.py    L0/L1/L2 记忆
│   │   └── agents/templates/roles.py  11 个角色的提示词与工具集（含 pm 与讨论主持人）
│   ├── scripts/               mock_provider / e2e_demo / drive_run / check_sse / smoke_plan / setup_real_model
│   ├── tests/                 core / gates / api / planner / plans_api / durability / model_discovery / fixtures
│   └── requirements.txt
├── frontend/                  Vite + React 18 + TS + AntD 5 + @xyflow/react + zustand
├── templates/sdlc_workflow.json  固定 SDLC 模板（8 阶段 5 门）
├── docs/                      设计文档与迭代记录
│   ├── 设计规格说明书.md / 模型接入配置设计.md
│   ├── 迭代优化-可靠执行.md / 迭代优化-可交互编排画布.md / 编排工作台-迭代记录.md
│   ├── 节点交互设计.md / 运行图交互设计.md（节点交互增量 v1 模块文档）
│   ├── research/前沿技术调研与技术选型.md
│   └── tasks/                 需求规格与测试案例（ledger-idea、real-model-ledger-test、orchestration-workbench）
├── runs/                      运行产物（工件、工作区），git 忽略
└── data/                      SQLite 与密钥，git 忽略
```

---

## 4. 关键设计

### 4.1 编排循环

- 节点依赖来自模板 `deps`；调度循环每轮取「依赖全部完成」的节点并发执行（`asyncio.gather`），
  受全局并发闸门限流。**勘误（2026-11-22）**：当前实现按「最小波次批次」gather，存在隐式批内屏障——
  下游节点要等同批最慢者结束才进 ready；此前"没有波次屏障"的表述与代码不符。
  评审/测试钩子需要穿插进依赖图，M1 将改为"依赖就绪即跑、单节点完成即唤醒调度"的事件驱动解锁
  （见 [docs/评审与测试体系设计.md](docs/评审与测试体系设计.md) §7）。
- `orchestrator.start(run_id)` 幂等：同一 run 已在跑时只记一次唤醒，不重复起循环。
- **并发**：进程级 `asyncio.Semaphore(AM_NODE_CONCURRENCY)`（挂在 event loop 上，跨 run 共享）；
  闸门在创建 attempt 前获取，排队节点保持 `pending`。
- **超时**：每次 attempt 受 `AM_NODE_TIMEOUT_S` 约束（默认 900s，0=不限制），节点 `config.timeout_s` 可覆盖；
  超时按可重试失败处理。
- **重试**：指数退避 + 抖动（`AM_RETRY_BASE_S` 3s 起、`AM_RETRY_MAX_S` 60s 封顶），
  `retry_at` 写入节点 config，等待期间不占并发槽；新增事件 `node_retry_scheduled`。
- 每个节点执行前写 `node_attempts`（attempt+1），事件先于状态落库可见；
  失败非 fatal 且未达 `max_attempts` 时自动带反馈重试，达到上限转人工门（`escalation`）。
- **心跳**：执行中的 attempt 每 `AM_HEARTBEAT_S`（15s）刷新 `heartbeat_at`，便于区分长任务与进程僵死；
  `GET /runs/{id}/metrics` 会按 `AM_ATTEMPT_STALE_S` 统计 `stale_attempts`。
- **取消**：`stop` 时进行中 attempt 记 failed（“未回执”）、节点回 `pending`，不留下幽灵 running。
- 无任何可执行节点且未全部完成时判定死锁，run 置 failed 并记录原因。
- **运行指标**：`GET /runs/{id}/metrics` 即时聚合 wall_ms、节点状态计数、attempt 次数/重试/忙碌时长/P95、
  token 用量与估算成本、人工门决策耗时与等待、逐节点明细；运行详情页 STATE 面板直接展示重试/尝试、
  单节点 P95 与门等待，用于定位慢节点与卡点。

### 4.2 人工门

| 动作 | 效果 |
| --- | --- |
| approve | 门节点 done；关联工件状态 → approved；若该门是失败升级门，则节点回 pending 重试 |
| reject | 必填 `feedback` + `target_node`；目标节点与全部下游重置 pending，反馈注入目标节点配置，重跑时进入提示词 |
| edit | 审批卡内直接改工件正文 → 写入新版本（status=approved）→ 再 approve |

门挂起时 run 置 `waiting_human`，**不占协程**；重启后待决门原样保留。

### 4.3 事件与 SSE

- `events` 表 `(run_id, seq)` 唯一，seq 单调；状态迁移先写事件。
- seq 在进程内按 run 缓存分配（`threading.Lock` 串行），DB 唯一约束兜底，避免每事件 `max(seq)` 扫描。
- `node_token` 只走内存广播、不落库（高频、可丢失）。
- SSE 支持 `Last-Event-ID`：断线重连先补发历史，再订阅实时队列；15s 心跳注释帧；
  终态事件后服务端主动关闭连接。生成器**不持有**数据库 session。
- 前端所有视图由快照 + 事件驱动：结构化事件触发快照刷新，token 事件只更新日志流。

### 4.4 工件链

- 9 类工件，W1 产出 8 类；每类最多一个 active 版本，人工编辑/重做自动 `superseded`。
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
- 模型发现：`openai_chat` 走 `GET /models` 拉取可选模型，可批量导入；预设创建时自动补建内置模型。
- 连接测试：带模型时真发 1 轮对话（8 tokens）并回传延迟与回复，不带模型时只探测 `GET /models`。
- 供应商并发用进程内 `Semaphore` 限制；密钥 Fernet 加密，`AM_SECRET_KEY` 支持任意口令（SHA-256 派生）。

### 4.7 恢复

启动时 `orchestrator.recover()`：

1. `running` 的 attempt 标记失败（"进程重启，未回执的 attempt 作废"），节点回 `pending`；
2. `waiting_human` 的 run 保持门不动，若等待节点缺 open gate 则补建；
3. 其余未终态 run 重新入队。

验收演示：跑到门 → 杀 API 进程 → 重启 → 页面刷新，门仍在，继续通过即可跑完。

### 4.8 编排工作台

- **提议与判定分离**：PM 只输出候选任务（严格 JSON）；uid 唯一性、依赖存在性、环、章节可追溯、任务数、
  验收数量与质量全部由 `engine/planner.py` 校验。环错误返回具体路径（如 `T3→T5→T3`）。
- **确定性编排**：`compute_waves` 拓扑分层；关键路径按 effort 点数（S1/M2/L3/XL5）做最长路 DP；
  指标包含任务数、波次数、最大并行、关键路径任务/点数、总点数、角色分布。
- **快照可追溯**：每次校验写一条 `plan_snapshots`，同一计划只保留一个 `is_valid`；编辑保留上一版，
  支持按版本恢复。PM 原始输出、设计原文、模型 ID 一并保存。
- **提交两种模式**：`save` 只锁定计划；`run` 复用任务依赖写入 `Task`，按任务生成 `dev_*` AGENT 节点、
  追加 `verify` 节点，再按开关追加 `review`（REVIEW）与 `gate_accept`（HUMAN_GATE），落盘 design 工件后启动现有编排器。
  提交幂等，重复提交返回同一 Run；运行预算可填（10,000–20,000,000），节点 config 携带角色/说明/effort/priority，
  执行时按角色选模型与追加提示词。
- **阶段与四视图**：`GET /plans/{id}/phases` 按 `design_section` 聚合阶段（只收有任务的章节，
  index 连续编号），返回每阶段的任务/角色/点数/跨阶段交接与关键标记；`GET /plans/{id}/activity`
  聚合创建/生成/校验/提交/归档时间线。页面左侧是阶段导航（PROGRESS、未承接章节），
  右侧「任务详情 / 流程监控 / 活动 / 执行图」四个视图：详情内嵌编辑表单，监控是
  Start → Phase → Verify → End 的只读链，活动是时间线，执行图是可编辑画布。
- **可交互画布**：坐标存 `plans.layout_json`（`PATCH /plans/{id}/layout`），依赖边可增删
  （`POST/DELETE /plans/{id}/deps`，服务端做环检测并返回路径）；默认按 0.62 缩放展示，
  **不自动 fitView**，避免整图缩到看不清，需要全貌时点「全览」；编辑器里的校验刷新不会打断视野。
  双击卡片改名，草稿按任务记忆，快照可回滚；`Ctrl+Z` 是前端撤销栈（最多 30 步、按计划隔离，
  覆盖移动/改名/依赖增删/详情保存/新建，提交后锁定）。选中节点只是视觉聚焦，其余卡片变暗但仍参与执行。
  详见 `docs/迭代优化-可交互编排画布.md` 与 `docs/编排工作台-迭代记录.md`。
- **设计覆盖率**：校验时按 `design_section` 与章节索引求覆盖，写进 `snapshot.coverage`
  （total/covered/ratio/每章任务/uncovered/orphan_tasks）与 `plan_snapshots.coverage_json`；
  未覆盖章节以 `section_uncovered` 警告返回，提示“有章节没有任务承接”，不阻断提交。
- **长文档与生成流**：设计规格超过 16,000 字时保留开头与结尾章节、中段标注省略；生成过程经 SSE
  `plan_token` 实时显示在 `PM STREAM` 面板，结束后原文可在「设计来源」查看。
- **人工确认**：生成结果默认待确认，只有校验通过才能提交；提交弹窗展示任务数、节点数与预算。

### 4.9 代码评审与验收门

计划 Run 在 `verify` 通过后进入评审，评审通过后再进入验收人工门（三道都是可选开关，默认开启）：

- **REVIEW 节点**：读取 idea、任务验收清单、`verify` 结果与 workspace 摘要（最多 16 个文本文件 / 26,000 字，
  跳过二进制、`.git`、`node_modules`、虚拟环境与构建产物），用评审角色模型输出严格 JSON；
  规范化后写 `review` 工件（通过=`approved`，阻塞=`draft`），并把结构化报告写进节点 `config.review`。
- **阻塞判定**：`severity ∈ {blocker, major}` 且带合法 `task_uid` 才算阻塞。编排器把目标 `dev_*`、`verify`
  与 `review` 节点重置为 `pending`，反馈注入任务配置，下一轮把上一轮问题带进提示词；默认 `max_rounds=2`，
  用尽后评审节点升级人工门，由人决定驳回或直接通过。
- **验收门**：`gate_accept` 依赖评审（未开评审时依赖 `verify`），驳回会按 `target_node` 重置目标节点；
  通过后 run 才 `finished`，并把 `review` 工件置为 `approved`。
- **运行页评审面板**：`GET /runs/{id}/review` 返回结论、轮次、阻塞数、问题列表（严重级别 / 任务 / 文件 /
  建议）、`can_rework` 与报告正文；面板可勾选任务 + 填返工说明，`POST /runs/{id}/review/rework`
  只允许在 `waiting_human` / `failed` 时人工回退。
- **评审指标**：`GET /runs/{id}/metrics` 的 `review` 段包含一次通过、返工次数、阻塞/问题按严重级别分布、
  评审均值与每个评审节点的历史；运行详情页直接展示一次通过、返工次数与问题分布。

详见 [docs/评审与验收门.md](docs/评审与验收门.md)。

---

## 5. API 一览（前缀 `/api`）

| 分组 | 端点 |
| --- | --- |
| 供应商 | `GET /presets`；`GET/POST /providers`；`PATCH/DELETE /providers/{id}`；`GET /providers/{id}/models`；`POST /providers/{id}/models/import`；`POST /providers/{id}/test`（可带 `model_id`） |
| 模型 | `GET/POST /models`；`PATCH/DELETE /models/{id}` |
| 角色模板 | `GET /agent-templates`；`PATCH /agent-templates/{id}` |
| 运行 | `POST /runs`；`GET /runs`；`GET /runs/{id}`；`POST /runs/{id}/stop`；`GET /runs/{id}/metrics`；`GET /runs/{id}/review`；`POST /runs/{id}/review/rework` |
| 事件 | `GET /runs/{id}/events`（SSE）；`GET /runs/{id}/events/history`；`GET /events/{id}` |
| 工件 | `GET /runs/{id}/artifacts`；`GET/PATCH /runs/{id}/artifacts/{kind}` |
| 门 | `GET /gates`；`GET /gates/{id}`；`POST /gates/{id}/approve`；`POST /gates/{id}/reject` |
| 任务 | `GET/POST /runs/{id}/tasks`；`PATCH/DELETE /runs/{id}/tasks/{uid}` |
| 节点 | `GET /nodes/{id}?messages=40&events=20`（详情扩展：`max_attempts`/上下游/`open_gate`/`can_*`/节点事件/产出工件）；`POST /nodes/{id}/retry`；`POST /nodes/{id}/reset`；`POST /nodes/{id}/skip`；`POST /nodes/{id}/feedback` |
| 记忆 | `GET /memories`；`PATCH /memories/{id}` |
| 编排 | `POST/GET /plans`；`GET /plans/{id}`；`GET /plans/{id}/phases`；`GET /plans/{id}/activity`；`POST /plans/{id}/generate`；`GET /plans/{id}/events`（SSE）；`POST /plans/{id}/validate`；`POST /plans/{id}/commit`；`POST /plans/{id}/cancel`；`POST /plans/{id}/archive`；`PATCH /plans/{id}/layout`；`POST/DELETE /plans/{id}/deps`；`GET /plans/{id}/snapshots`；`POST /plans/{id}/restore/{version}`；`POST /plans/{id}/tasks`；`PATCH/DELETE /plans/{id}/tasks/{uid}` |

---

## 6. 配置

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `AM_DATABASE_URL` | `sqlite:///data/am.db` | 数据库连接串 |
| `AM_DATA_DIR` | `<repo>/data` | 密钥与 DB 目录 |
| `AM_RUNS_DIR` | `<repo>/runs` | 运行产物目录 |
| `AM_SECRET_KEY` | 自动生成 | Fernet 口令；不设则首次启动写入 `data/secret.key` |
| `AM_NODE_CONCURRENCY` | 4 | 同时执行的节点数上限（进程级，跨 run 共享） |
| `AM_NODE_TIMEOUT_S` | 900 | 单次 attempt 超时秒数；0=不限制；节点 `config.timeout_s` 可覆盖 |
| `AM_RETRY_BASE_S` | 3 | 指数退避起始延迟（秒） |
| `AM_RETRY_MAX_S` | 60 | 指数退避延迟上限（秒） |
| `AM_HEARTBEAT_S` | 15 | 执行中 attempt 的心跳间隔（秒） |
| `AM_ATTEMPT_STALE_S` | 120 | attempt 心跳陈旧判定阈值（秒） |
| `AM_MAX_REWORK_ROUNDS` | 3 | 预留；当前失败重做上限由节点 `max_attempts` 决定（模板默认 3），本变量尚未接入 |
| `AM_RUN_BUDGET_TOKENS` | 1200000 | 新建运行的默认 token 预算（计划提交时可覆盖） |
| `AM_STREAM_TOKENS` | 1 | 设为 `0` 关闭 token 级流式事件（`node_token`） |
| `AM_MEMORY_EXTRACT` | 1 | 预留；记忆抽取开关尚未接入执行链路 |
| `AM_CORS_ORIGINS` | `http://127.0.0.1:5173,http://localhost:5173` | 开发模式允许的前端来源 |
| `AM_DEMO_BASE_URL` | `http://127.0.0.1:8100/v1` | 预留；`e2e_demo.py` 当前仍写死同一地址 |
| `VITE_API_BASE` | `/api` | 前端 API 前缀（开发代理已配置） |

SQLite 约束：`pool_size=1`、`check_same_thread=False`、`busy_timeout=5000`、WAL + `synchronous=NORMAL`；
因此**禁止在 `session_scope()` 内再开启 session**（工件、记忆、模型池读取都在事务外做）。

---

## 7. 测试

```powershell
cd backend
..\.venv\Scripts\python.exe -m pytest tests -q
```

覆盖：图/任务解析/front-matter/预算估算、门禁通过/驳回/升级重试、供应商与模型 CRUD、模板绑定校验；
编排工作台的章节索引/校验错误码/波次/关键路径/环路径、两份 fixture 的差异、计划 API 的生成/编辑/提交
（save 与 run）/幂等/归档；以及 `tests/test_durability.py` 的并发闸门、节点超时、退避重试、
seq 缓存、取消收据与 metrics 端点，`tests/test_model_discovery.py` 的预设建模型、远程列表导入、
导入去重与协议不支持报错，`tests/test_review.py` 的评审节点/阻塞回退/轮次用尽/评审 API 与指标，
以及 `tests/test_node_actions.py` 的节点详情投影（`max_attempts`/上下游/门/事件/`can_*`）、动作矩阵与错误码、
open 门关闭、下游重置、skip 幂等与审计事件（本轮合计 **87 passed**）。

---

## 8. 已知限制（M1 计划）

- 可靠执行是**单进程语义**：并发闸门、seq 缓存、attempt 心跳都在进程内，未做跨进程租约/仲裁；
  多 worker 部署需要外部锁或 DB 租约（M2）。
- `GET /runs/{id}/metrics` 是即时聚合（attempt/门/事件全表扫描该 run），未落库、无历史趋势。
- 重试是「整节点重来」，没有 attempt 级 checkpoint/幂等键；崩溃恢复会丢弃未回执的 attempt。
- Anthropic / Gemini 适配器暂为伪流式；M1 补真流式与工具调用。
- 未做：向量检索、记忆面板、执行看板、Twin 数字分身、多 run 调度面板、容器化执行、MCP 工具接入。
- 前端单包体积 ~1.35MB（AntD + xyflow），未做路由级代码分割。
- 任务清单暂不支持独立 `TASKGRAPH validate` 节点（解析时已校验唯一性与依赖合法性）。
- 编排工作台 v1：一个 plan 只对应一个 run；任务数上限 8–40；提交 run 后按节点 `role_key` 选择执行器
  （dev/test/design/pm/review/ops 会注入不同角色意图），但所有角色仍共用项目根的 `dev` 执行器与提示词模板；
  生成失败自动修复一次，仍失败保留原文与错误；单次生成输出上限 6000 tokens（也受模型 `max_output_tokens` 约束）。
- 并行任务（同一波或跨波被同时解锁的）共享 `runs/<run_id>/workspace`，没有文件级隔离/锁：多个 agent 并行改同一文件时后写覆盖先写。
  当前建议把任务按文件拆开；worktree/分支隔离与合并节点在 M2。
- `Ctrl+Z` 是画布的前端撤销栈：随页面刷新、切换计划清空，只在当前浏览器生效；删除任务不可撤销；
  计划提交/归档后撤销锁定。
- 页面级 `ErrorBoundary` 兜底渲染异常；活动时间线会把旧快照里的对象型 `errors` 归一化成字符串，
  避免 React 渲染对象导致白屏（旧数据也兼容）。
- 画布坐标不进 `plan_snapshots`：恢复历史版本只恢复任务与依赖，不恢复节点位置；连线操作尚未适配触屏。
- 1440 宽度下工作台为「阶段导航 292px + 内容 1fr」两栏，任务详情、流程监控、活动、执行图共用右侧内容区；
  执行图默认 0.62 缩放下 5 个波次约 904px，可落在 1000px 左右的画布内，更多波次需要横向滚动或点「全览」。
- 评审是**启发式静态评审**：只把 workspace 的文本摘要（最多 16 个文件 / 26,000 字）连同任务验收清单交给模型，
  没有真正的 diff、AST 或测试覆盖分析；阻塞问题必须带 `task_uid` 才会触发回退，模型漏标就会放行。
  评审与开发共用同一个项目根执行器，评审模型由 `review` 角色的默认模型决定，未绑定回退 dev 模型。
- 验收门是计划 Run 的**单点门**（review 之后一道），不是固定模板里的 5 道 SDLC 门；验收门驳回只按
  `target_node` 重置一个节点，批量回退请用运行页评审面板的勾选返工。
- Windows 下命令执行把 `python` 前缀替换为当前解释器；Linux 行为一致但未在 CI 验证。
- 节点交互 v1：**HUMAN_GATE 节点不可跳过**（skip 返回 400 `gate_node_protected`，人工门只能走 approve/reject）；
  运行中节点不可 retry/reset/skip（409 `node_running`），单节点 cancel/pause 与 run 级 resume 均未做
  （设计取舍见 [`docs/节点交互设计.md`](docs/节点交互设计.md) §10）。
- 节点动作的审计**只落 `events` 表**（`node_skipped`/`node_reset`/`node_feedback_injected` 与扩展后的 `node_retried`，
  payload 带 `actor="human"`、`reason`、`reset_nodes`），没有独立动作历史表与多用户权限审计；动作按单进程语义执行，
  多 worker 部署需要 DB 租约（M2）。
- 级联重置下游（retry/reset 的 `reset_downstream`、skip 的 `cascade_downstream`）与门 reject 行为一致：
  只重置 `done/failed/waiting_human/running/ready` 下游节点，**不取消**其已在执行的 attempt；在并发中的运行上使用需谨慎。
- `GET /nodes/{id}` 只加字段、旧请求体仍可用；节点详情在运行中由 SSE 精准刷新 + 8s 兜底轮询，终态后停止刷新。
- 前端仍**没有自动化测试栈**：本轮把血缘/门阻挡/相位分列抽成 `frontend/src/canvas/graphLayout.ts` 纯函数，
  以 `npm run typecheck` + 手工验收守护（见 [`docs/运行图交互设计.md`](docs/运行图交互设计.md) §10），未引入 vitest。

---

## 9. 与设计文档的差异（勘误）

1. 文档 §3.3 引用的 `./research/...` 与实际同级目录不符，实际为同级三份 Markdown。
2. §7.1 写「4 个人工门」，但 §7.3 demo 十步包含验收门；本实现落地 **5 个门**：
   方案确认 / 需求审查 / 设计审查 / 任务图确认 / 验收确认。
3. bug：W1 要求「2 个任务并行」，实现为任务图展开后依赖就绪即并发，验收时两个无依赖的 dev 节点
   确实并行执行（事件时间戳交错），但受 Mock 响应速度影响，单次演示中肉眼不易分辨。
4. **勘误（2026-11-22）**：此前 `POST /nodes/{id}/retry` 不校验 run 状态，会把 `finished/failed/stopped`
   的 run 无条件改回 `running`（终态 run 可被"复活"）。节点交互增量后，retry 对终态 run 返回 409 `run_terminal`，
   run 状态与 `error` 保持不变；如需恢复终态运行，应另做 run 级 `POST /runs/{id}/resume`（未实现，见
   [`docs/节点交互设计.md`](docs/节点交互设计.md) §10）。
