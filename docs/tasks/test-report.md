# 测试报告（Test Report）

| 项 | 内容 |
| --- | --- |
| 项目 | AM-Workflow 多智能体软件开发工作流平台 |
| 分支 | `testing-and-evaluation`（仅在该分支改动） |
| 配套清单 | [`test-case-checklist.md`](./test-case-checklist.md) |
| 报告状态 | A 组自动化回归、B 组离线 E2E 已回填；C 组真实模型部分执行（文档段通过、执行段阻塞）；结论见 §7 |
| 测试日期 | 2026-11-23（自动化回归 + 离线 Mock E2E + 真实模型链路） |
| E2E 运行 | 离线 Mock：run `4fc784fd76f9`；真实模型：run `3b7fa6e12575`（DeepSeek `deepseek-flash`，未终态，挂起于执行段） |

## 1. 测试概述

### 1.1 测试目标

在 `testing-and-evaluation` 分支上，对当前 W1 最小原型进行测试验证：确认现有自动化回归保持通过，并通过离线 Mock 端到端流程核验编排引擎、人工门、工件链、事件流与运行指标等核心链路是否符合设计规格。

### 1.2 本次测试范围

- 后端现有 pytest 自动化回归（A 组，对应 `backend/tests/`）。
- 离线 Mock 供应商驱动的端到端流程（B 组，5 个人工门、驳回重做、工件链、事件与指标）。
- 真实大模型链路（C 组）：供应商接入、角色模型绑定、真实模型下的文档链与人工门交互；dev 执行段在本次阻塞，未闭环。

### 1.3 本次不在范围

- 真实模型端到端终态验收与产物全量手工验证（C 组 TC-C03 / TC-C04）：dev 执行段受模型收尾行为阻塞，未跑到 `finished` 与 CLI 演示，留待后续迭代。
- 前端自动化测试：项目当前未引入前端测试栈（见 README §8），本次仅随 E2E 做人工界面观察。
- 多 worker / 分布式执行、容器化、MCP 接入等未实现或封存设计项。

## 2. 测试环境

| 项 | 配置 |
| --- | --- |
| 操作系统 | Windows 11 家庭版 中文版（内部版本 10.0.26300） |
| Python | 3.11.18（conda 环境 `am-workflow`，路径 `D:\miniconda3\envs\am-workflow`） |
| 后端框架 | FastAPI 0.165.0 / Starlette 1.10.0 / Uvicorn 0.54.0 |
| 数据层 | SQLAlchemy 2.1.9 / SQLite（WAL），`cryptography` 50.0.2 / PyYAML 6.0.4 |
| 测试框架 | pytest 9.2.0 / pytest-asyncio 1.5.0 / httpx 0.28.1 |
| 前端工具链 | Node v22.23.2 / npm 10.9.8（本次未构建 `frontend/dist`；API 驱动的 E2E 不依赖前端产物） |
| 运行方式 | 本地单机；Mock 供应商 `127.0.0.1:8100`，主应用 `127.0.0.1:8099` |
| 真实模型 | 供应商 DeepSeek，Base URL `https://api.deepseek.com/v1`，协议 `openai_chat`，模型 `deepseek-flash`（DeepSeek-V4.1-Flash），并发 1、单节点超时 600s |
| 备注 | 运行数据落在仓库 `data/`、`runs/`、`logs/`，均被 `.gitignore` 忽略；关键证据需摘录进本报告方可入库 |

## 3. 测试执行记录

### 3.1 自动化回归（已执行）

- 执行目录：`backend/`
- 执行命令：

```powershell
D:\miniconda3\envs\am-workflow\python.exe -m pytest tests -q
```

- 执行结果：**87 passed, 1 warning, 4.67s**（2026-11-23）。
- 与 README 记录的基线（87 passed）一致，无失败用例。

分组统计：

| 编号 | 测试文件 | 用例数 | 结果 |
| --- | --- | --- | --- |
| TC-A01 | `test_api.py` | 2 | passed |
| TC-A02 | `test_core.py` | 10 | passed |
| TC-A03 | `test_discussion.py` | 7 | passed |
| TC-A04 | `test_durability.py` | 7 | passed |
| TC-A05 | `test_gates.py` | 4 | passed |
| TC-A06 | `test_model_discovery.py` | 7 | passed |
| TC-A07 | `test_node_actions.py` | 22 | passed |
| TC-A08 | `test_planner.py` | 11 | passed |
| TC-A09 | `test_plans_api.py` | 7 | passed |
| TC-A10 | `test_review.py` | 5 | passed |
| TC-A11 | `test_seed_templates.py` | 5 | passed |
| **合计** | — | **87** | **87 passed** |

**Warning 分析**：

- 内容：`StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead.`，来自 `fastapi/testclient.py`。
- 性质：测试客户端依赖链的弃用提示，不影响任何断言与通过结论。
- 处置建议：后续版本跟进 Starlette/FastAPI 测试客户端的迁移提示；本次不作为缺陷登记。

### 3.2 离线 Mock 端到端（已执行）

启动方式：先起 Mock 供应商（:8100）与主应用（:8099），再运行 `e2e_demo.py --auto --demo-reject`。本次运行：run `4fc784fd76f9`，需求为脚本内置的「客户反馈收集与归类」内部工具。

| 编号 | 用例 | 预期结果 | 状态 | 实际结果 / 证据 |
| --- | --- | --- | --- | --- |
| TC-B01 | 服务启动与连接测试 | 供应商连接通过，主应用可访问 | passed | Mock 连接测试成功（119ms，发现 2 模型）；`GET /api/runs` 正常返回，两端口正常监听 |
| TC-B02 | 创建运行与 idea 落盘 | 运行创建成功，idea 工件落盘 | passed | run `4fc784fd76f9` 创建；`idea.v1.md` 落盘，节点进入 `idea` |
| TC-B03 | 方案确认门 | proposal 工件产出，批准后续跑 | passed | `proposal.v1.md` 产出；`approve -> approved` 后自动续跑 |
| TC-B04 | 需求门驳回重做 | 下游重置、attempt 递增、反馈注入 | passed | 需求门 `reject -> rejected`，重置 `['requirement']`；`requirement` attempts=2，重跑出 `requirement.v2.md` 后 approve；`nodes_reset`×1，metrics `retries=1` |
| TC-B05 | 设计审查门 | design 工件产出，批准后续跑 | passed | `design.v1.md` 产出；`approve -> approved` 后续跑 |
| TC-B06 | 任务图门与并行展开 | 任务依赖合理，展开 `dev_*` | passed | `task.v1.md` 产出；T1、T2 同 wave 1 并行，展开 `dev_T1`、`dev_T2` 均 `done` |
| TC-B07 | 验收门与 verify | verify 退出码 0，验收通过 | passed | `verify` 跑 `unittest discover`，attempt 1 `done`、`error=None`；`gate_accept` `done` |
| TC-B08 | 运行终态 | `finished`，节点全部 `done` | passed | 终态 `finished`、`error=None`；节点 total 20 / done 20 / failed 0 / running 0 / pending 0 |
| TC-B09 | 工件链完整性 | 七类以上工件落盘，过门后 approved | passed | 落盘 idea / discussion(v1–v4) / proposal / requirement(v1–v2) / design / task / changelog 共 7 类；固定模板未挂评审节点，无 `review` 工件（预期内） |
| TC-B10 | token 用量与预算 | usage 字段齐全，未超预算 | passed | in 23359 / out 3564 / 合计 26923，剩余 1173077/1200000，未超预算 |
| TC-B11 | SSE 事件流 | 事件类型齐全，seq 不重复，重连可补发 | passed（部分） | 事件 113 条，seq 113/113 唯一；含 `node_started`(16)/`node_finished`(16)/`gate_requested`(6)/`gate_decided`(6)/`nodes_reset`(1) 等；断线重连补发未在真实断连下实测 |
| TC-B12 | 运行指标端点 | metrics 字段齐全 | passed | 返回 attempts 16、retries 1、busy_ms 1819、p95 557ms、gates(approved 5/rejected 1)、usage.estimated_cost、per_node 20 条 |
| TC-B13 | 门挂起不占协程 / 重启保留 | 挂起不卡死，重启保留待决门 | 未执行 | 日志可见挂起期 `waiting_human`；本次未杀进程重启，重启保留未验证 |
| TC-B14 | 编排工作台冒烟（可选） | 计划生成/校验/提交指标正常 | 未执行 | `smoke_plan.py` 硬编码 8375 端口，本次应用在 8099，连接被拒；规划 API 已由 TC-A09 自动化覆盖 |
| TC-B15 | 节点动作保护（可选） | 运行中/终态节点动作受 409 保护 | 未复核 | 409 保护已由 TC-A07（22 条自动化用例）覆盖，本次 E2E 未重复手工验证 |

### 3.3 真实模型链路（部分执行，执行段阻塞）

真实模型运行 run `3b7fa6e12575`，需求为 [`ledger-idea.txt`](./ledger-idea.txt)「随手账本」；供应商 DeepSeek，模型 `deepseek-flash`。

**接入与绑定（TC-C01 / TC-C02，通过）**

- 供应商 #3「DeepSeek」创建，Base URL `https://api.deepseek.com/v1`，协议 `openai_chat`，密钥加密入库（掩码 `sk-****4fcf`）；连接测试 `ok=true`、延迟 869ms。
- 模型条目 #4 `deepseek-flash` 录入；11 个角色模板 `default_model_id` 全部绑定模型 #4，无「尚未配置模型」报错。
- 模型名备注：首填 `DeepSeek-V4.1-Flash` 被服务端 400 拒绝（服务端支持名 `deepseek-flash`、`deepseek-v4-pro`），回退 `deepseek-flash` 后连通。

**文档链与人工门（TC-C03 前段，通过）**

- 工件链落盘：`idea.v1.md`、`discussion.v1–v4.md`、`proposal.v1.md`、`requirement.v1.md`、`design.v1.md`、`task.v1.md`。
- 前 4 道门均 `approved`：gate_proposal、gate_requirement、gate_design、gate_tasks。

**dev 执行段（TC-C03 后段 / TC-C04，阻塞）**

- `dev_T1.1`（存储层 `_load` / `_save` 与统一异常）一次通过；配套 `test_storage.py` **34 个用例全绿**。
- `dev_T2.1`（金额解析 / 校验器 / 时间戳工具）连续 3 次尝试均失败：模型读完既有文件后空回复，未调用 `finish` 工具声明完成，引擎判「Agent 没有调用 finish 声明完成，视为未完成」；挂起门 #12，`attempts=3`、`retries=2`。
- `dev_T3.1`（领域层三方法）、`dev_T4.1`（CLI / 渲染 / README）未执行；运行停在 `waiting_human` / `execution`，未到 `finished`，无 `verify`、`gate_accept`、`delivery` 产物。
- 产物手工验证（TC-C04）未闭环：账本 CLI（`add` / `list` / `summary`）与 `test_ledger.py` 全流程未完成，未现场演示 summary 总额 42.5 / 2 笔 / 交通+餐饮。

**本次运行指标（metrics 端点）**

| 指标 | 数值 |
| --- | --- |
| 节点 | total 22 / done 16 / waiting_human 1 / pending 5 / failed 0 |
| 执行 | attempts 15、retries 2、busy_ms 328285、p95_attempt_ms 78969 |
| 用量 | tokens_in 272609、tokens_out 56720、合计 329329 |
| 门 | total 6 / approved 5 / open 1 / rejected 0 |
| 事件 | 157 条，seq 157/157 唯一；含 node_started 15、node_finished 12、gate_requested 6、gate_decided 5、node_failed 3、node_tool_call/result 各 21 |

> 附带说明：首次真实模型尝试使用智谱 GLM `glm-4.7-flash`，因 dev 节点 600s 超时后重试触发 429 限流而中断；该运行的数据库关联与产物目录已清理，不计入本次验收统计。

## 4. 结果汇总

| 用例组 | 用例总数 | 通过 | 部分通过 | 失败 | 未执行/阻塞 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| A 自动化回归 | 87 | 87 | 0 | 0 | 0 | 1 条弃用 warning，不影响结论 |
| B 离线 E2E | 15 | 11 | 1 | 0 | 3 | B11 事件 seq 与类型通过、断线重连未实测；B13/B14/B15 未执行或未复核 |
| C 真实模型 | 4 | 2 | 0 | 0 | 2 | C01 接入连通、C02 角色绑定通过；C03 全流程、C04 产物验收在 dev 执行段阻塞 |
| **合计** | **106** | **100** | **1** | **0** | **5** | 无失败用例；C 组执行段阻塞见 §5 缺陷 D-01 与 §6 遗留风险 |

## 5. 缺陷清单

> 发现失败时按条目登记，字段对齐 [`../评审与测试体系设计.md`](../评审与测试体系设计.md) §5.1：现象 / 故障假设 / 严重级别 / 复现步骤 / 预期 / 实际 / 关联 TC / 状态。严重级别：Sev1 崩溃 / 数据丢失 / 安全问题；Sev2 部分功能阻塞；Sev3 轻微问题。

| 缺陷编号 | 关联 TC | 现象 | 级别 | 复现步骤 | 预期 | 实际 | 状态 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D-01 | TC-C03 / TC-C04 | `deepseek-flash` 在 dev 节点读完既有文件后空回复，未调用 `finish` 工具声明完成，引擎连续 3 次判未完成，执行段阻塞 | Sev2 | run `3b7fa6e12575`，节点 `dev_T2.1`：attempt 1/3 末轮 assistant 无 `tool_calls`、`content` 为空；三次 `last_error` 均为「Agent 没有调用 finish 声明完成，视为未完成」（attempt 2 另叠加一次预算耗尽） | dev 节点写完代码后调用 `finish` 收尾，返回改动文件与测试结果，节点 `done` 并展开下游 | 未调 finish，节点 `waiting_human`（门 #12，attempts=3、retries=2），下游 `dev_T3.1` / `dev_T4.1` 未执行 | 未修复，遗留 | 非预算耗尽、非限流；同模型 `dev_T1.1` 一次通过，疑为 flash 模型多轮工具后的收尾遵循度不稳。复测方向：换工具遵循更强模型（如 `deepseek-v4-pro`），或在 dev 提示词中强化 finish 收尾约束后重跑 |

**观察项（非缺陷）**：

1. 本次固定 SDLC 模板未挂评审节点，运行产物中无 `review` 工件；评审节点为计划 Run 的可选追加项，属预期设计，非缺失缺陷。
2. 离线 Mock 下 `dev_*` 节点 `tokens_out` 仅个位数（Mock 响应为占位内容），产物为演示级骨架，不代表真实模型产出质量。
3. 真实模型 `dev_T1.1` 单节点输入 token 达 187693，上下文膨胀偏高；初始 30 万预算在执行段耗尽（剩余 -1938），本次已将运行预算补至 120 万后重试，预算阈值与上下文裁剪策略需后续评估。

## 6. 遗留问题与风险

1. **真实模型执行段未闭环**：C 组已验证供应商接入（TC-C01）与角色绑定（TC-C02），文档链与前 4 道门通过；但 dev 执行段因模型收尾未调 `finish` 阻塞（缺陷 D-01），全流程跑通（TC-C03）与产物 CLI / unittest 验收（TC-C04）待后续复测。复测前需确认模型选择或 dev 提示的 finish 约束，并评估上下文膨胀与预算阈值。
2. **B 组未覆盖项**：TC-B13 重启后待决门保留未实测；TC-B14 编排工作台冒烟因脚本端口（8375）与本次应用端口（8099）不一致未执行；TC-B15 节点动作 409 保护未手工复核（已由自动化用例覆盖）；TC-B11 断线重连 `Last-Event-ID` 补发未在真实断连场景下验证。
3. **前端无自动化测试栈**：界面交互依赖人工观察，回归保护较弱；已知限制见 README §8。
4. **测试客户端弃用 warning**：Starlette 提示测试客户端后续需迁移到 `httpx2` 方案，当前不影响运行。
5. **已知实现限制**（摘自 README §8，测试时需注意区分「有意设计取舍」与「缺陷」）：单进程语义、整节点重试无 checkpoint、评审为启发式静态评审、并行任务共享 workspace 无文件隔离等。

## 7. 测试结论

- 结论选项：□ 通过　☑ 有条件通过　□ 不通过
- 结论说明：自动化回归 87 条全部通过；离线 Mock 端到端核心链路 11 项通过、1 项部分通过。真实模型链路已完成供应商接入、角色绑定与文档链 / 前 4 道门验证，但 dev 执行段因模型收尾未调 `finish` 阻塞（缺陷 D-01），全流程终态与产物 CLI / unittest 验收尚未闭环。离线 Mock 与自动化回归范围可视为通过；真实模型全量通过需待 C 组执行段复测闭环。
- 放行条件 / 遗留风险：
  1. C 组 TC-C03（全流程跑通）、TC-C04（产物手工验证）阻塞：需在修复缺陷 D-01 后重跑真实模型运行，跑到 `finished` 并现场演示账本 CLI 与 `unittest` 全绿，方可视为全量通过；
  2. TC-B11 断线重连补发、TC-B13 重启保留、TC-B14 工作台冒烟、TC-B15 动作保护手工复核为本次未覆盖项，需在后续迭代补测；
  3. 其余遗留风险与已知限制见 §6。

## 附录：参考文档

- [`../../README.md`](../../README.md)：项目说明、快速开始、§7 测试、§8 已知限制
- [`./test-case-checklist.md`](./test-case-checklist.md)：测试用例清单
- [`../评审与测试体系设计.md`](../评审与测试体系设计.md)：三级评审 + 四级测试 + 缺陷闭环
- [`../设计规格说明书.md`](../设计规格说明书.md)：设计基线与 test.md 用例格式
- [`./real-model-ledger-test.md`](./real-model-ledger-test.md)：真实模型验收案例（后续 C 组参考）
