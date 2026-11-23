# 测试用例清单（Test Case Checklist）

| 项 | 内容 |
| --- | --- |
| 项目 | AM-Workflow 多智能体软件开发工作流平台 |
| 分支 | `testing-and-evaluation`（仅在该分支改动） |
| 配套报告 | [`test-report.md`](./test-report.md) |
| 基线依据 | [`../../README.md`](../../README.md) §7；[`../评审与测试体系设计.md`](../评审与测试体系设计.md) §4；[`../设计规格说明书.md`](../设计规格说明书.md) §4.2 |
| 清单状态 | A 组（自动化回归）87 passed；B 组（离线 Mock E2E）11 passed、1 部分、3 未执行/未复核；C 组（真实模型）已接入 DeepSeek 并跑通文档段，执行段阻塞：2 项通过、2 项阻塞 |
| E2E 运行 | 离线 Mock：run `4fc784fd76f9`；真实模型：run `3b7fa6e12575`（DeepSeek `deepseek-flash`，2026-11-23，未终态） |

## 1. 字段与状态说明

用例行内联元数据沿用项目规范（见设计规格 §4.2）：

```markdown
- [ ] 用例描述 <!-- id: TC-B01; maps: 功能域/验收点; type: system; status: pending -->
```

| 字段 | 取值 | 含义 |
| --- | --- | --- |
| `id` | `TC-Axx / TC-Bxx / TC-Cxx` | 用例唯一编号，A=自动化回归，B=离线 E2E，C=真实模型验收 |
| `maps` | 需求编号 / AC / 功能域 | 该用例追溯到的需求或验收点 |
| `type` | `unit` / `integration` / `system` / `acceptance` | 测试分层：单元 / 集成 / 系统 / 验收 |
| `status` | `pending` / `passed` / `failed` / `blocked` | 执行状态：待执行 / 通过 / 失败 / 阻塞 |

状态图例：

- `pending`：尚未执行
- `passed`：执行通过，实际结果与预期一致
- `failed`：执行失败，需登记缺陷并进入缺陷闭环
- `blocked`：受前置条件限制本次无法执行（如缺少真实模型 API Key）

## 2. A 组：自动化回归用例（pytest）

执行命令（在 `backend/` 目录，conda 环境 `am-workflow`）：

```powershell
D:\miniconda3\envs\am-workflow\python.exe -m pytest tests -q
```

基线结果：**87 passed, 1 warning, 4.67s**（2026-11-23 执行）。warning 为 Starlette/httpx 弃用提示，不影响断言结论，详见测试报告 §3.1。

| 编号 | 测试文件 | 覆盖域 | type | 用例数 | 状态 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-A01 | `tests/test_api.py` | API 基础冒烟 | integration | 2 | passed | TestClient 基础请求链路 |
| TC-A02 | `tests/test_core.py` | 图/任务解析、front-matter、预算估算、令牌截断 | unit | 10 | passed | 纯函数与解析校验 |
| TC-A03 | `tests/test_discussion.py` | 三方讨论：主张/质疑/回应/主持人蒸馏 | integration | 7 | passed | 讨论节点与工件读写 |
| TC-A04 | `tests/test_durability.py` | 并发闸门、节点超时、退避重试、seq 缓存、取消收据、metrics 端点 | integration | 7 | passed | 可靠执行与持久化 |
| TC-A05 | `tests/test_gates.py` | 人工门状态迁移、请求/裁决 | integration | 4 | passed | approve / reject 路径 |
| TC-A06 | `tests/test_model_discovery.py` | 预设建模、远程列表导入、导入去重、协议不支持报错 | integration | 7 | passed | 供应商/模型发现 |
| TC-A07 | `tests/test_node_actions.py` | 节点详情投影、动作矩阵、错误码、门关闭、下游重置、skip 幂等、审计事件 | integration | 22 | passed | 节点交互 v1 主覆盖 |
| TC-A08 | `tests/test_planner.py` | 计划校验、波次、关键路径、成环/缺依赖路径校验 | unit | 11 | passed | 编排规划确定性计算 |
| TC-A09 | `tests/test_plans_api.py` | 计划生成、编辑、提交（仅保存/创建运行）、幂等、归档、活动时间线 | integration | 7 | passed | 编排工作台 API |
| TC-A10 | `tests/test_review.py` | 评审节点、阻塞回退、轮次用尽升级、评审 API 与指标、JSON 归一化 | integration | 5 | passed | 代码评审与验收门 |
| TC-A11 | `tests/test_seed_templates.py` | 内置角色模板指纹同步、保留用户修改、模型绑定保留、幂等 | unit | 5 | passed | 模板 seed 升级逻辑 |
| **合计** | — | — | — | **87** | **passed** | 1 条弃用 warning，无失败 |

## 3. B 组：离线 Mock 端到端用例（system / acceptance）

前置条件：启动 Mock 供应商（:8100）与主应用（:8099），再用演示脚本驱动全流程。演示脚本走 HTTP API，不依赖前端 `dist` 产物。

启动命令（均在 `backend/` 目录）：

```powershell
# 终端 1：Mock 供应商
D:\miniconda3\envs\am-workflow\python.exe -m uvicorn scripts.mock_provider:app --host 127.0.0.1 --port 8100

# 终端 2：主应用
D:\miniconda3\envs\am-workflow\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8099

# 终端 3：自动过门，并在需求门演示一次驳回 + 反馈 + 重跑
D:\miniconda3\envs\am-workflow\python.exe scripts\e2e_demo.py --auto --demo-reject
```

> 演示需求为脚本内置的「客户反馈收集与归类」内部工具；`ledger-idea.txt` 账本需求属于 C 组真实模型验收场景，本次离线 Mock 不使用。

| 编号 | 用例 | maps | type | 预期结果 | 状态 | 实际结果 / 证据 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-B01 | 服务启动与连接测试 | 部署/健康检查 | system | Mock 供应商在 :8100 可被测试连接；主应用 :8099 可访问，无启动报错 | passed | Mock 连接测试成功（119ms，发现 2 个模型）；`GET /api/runs` 正常返回，两个端口均正常监听 |
| TC-B02 | 创建运行与 idea 落盘 | 运行初始化 | system | 提交 idea 后运行创建成功，idea 工件落盘，节点进入 `idea` | passed | run `4fc784fd76f9` 创建成功；`idea.v1.md` 落盘，节点进入 `idea` |
| TC-B03 | 方案确认门 `gate_proposal` | 方案工件 / 方案评审 | acceptance | 产出 `proposal` 工件（目标、范围、非目标、风险、验收标准）；批准后运行自动续跑 | passed | `proposal.v1.md` 产出；门 `approve -> approved` 后自动续跑至需求阶段 |
| TC-B04 | 需求门驳回重做 | 人工门 reject / 反馈注入 / 下游重置 | integration | 在 `gate_requirement` 驳回并填写反馈与 `target_node=requirement`；目标节点及下游重置 `pending`，`attempt` 递增，反馈进入重跑提示词，节点重新排队 | passed | 需求门首次 `reject -> rejected`，重置节点 `['requirement']`；`requirement` attempts=2，重跑出 `requirement.v2.md` 后 `approve`；事件 `nodes_reset`×1，metrics `retries=1` |
| TC-B05 | 设计审查门 `gate_design` | 设计工件 / 设计评审 | acceptance | 产出 `design` 工件（模块划分、公开接口、错误处理、测试策略）；批准后续跑 | passed | `design.v1.md` 产出；门 `approve -> approved` 后续跑至任务阶段 |
| TC-B06 | 任务图门与并行展开 | 任务分解 / 并行调度 | system | 任务清单合理（约 3 个、依赖正确，无依赖任务可并行）；批准后展开 `dev_*` 节点 | passed | `task.v1.md` 产出；T1、T2 同处 wave 1 并行，展开 `dev_T1`、`dev_T2`，两者均 `done` |
| TC-B07 | 验收门与 verify 验证 | 验收门 / 验证退出码 | acceptance | `verify` 节点退出码 0；验收门 `gate_accept` 通过后运行正常结束 | passed | `verify` 命令 `python -m unittest discover -s . -p "test_*.py"`，attempt 1 `done`、`error=None`（退出码 0）；`gate_accept` `done` |
| TC-B08 | 运行终态 | 运行状态机 | system | 运行终态为 `finished`；全部节点 `done`，无 `failed` / 卡死节点 | passed | 终态 `finished`，`error=None`；metrics 节点 total 20 / done 20 / failed 0 / running 0 / pending 0 |
| TC-B09 | 工件链完整性 | 工件链落盘 | system | `artifacts/` 下 idea / discussion / proposal / requirement / design / task / review / changelog 等 active 工件落盘；过门后状态为 `approved` | passed | 落盘 7 类：idea、discussion(v1–v4)、proposal、requirement(v1–v2)、design、task、changelog；本次固定模板未挂评审节点，故无 `review` 工件（预期内，非缺陷） |
| TC-B10 | token 用量与预算 | 用量统计 / 预算控制 | system | `usage` 返回 `tokens_in / tokens_out / remaining / budget`，运行未超预算 | passed | `tokens_in=23359`、`tokens_out=3564`、合计 26923，剩余 `1173077/1200000`，未超预算 |
| TC-B11 | SSE 事件流 | 事件协议 / 断线重连 | system | 事件含 `node_started / node_finished / gate_requested / gate_decided`；`seq` 单调不重复；重连带 `Last-Event-ID` 可补发历史事件 | passed（部分） | 事件 113 条，`seq` 113/113 全部唯一；含 `node_started`(16)、`node_finished`(16)、`gate_requested`(6)、`gate_decided`(6)、`nodes_reset`(1)、`artifact_written` 等；断线重连 `Last-Event-ID` 补发未在真实断连场景下实测 |
| TC-B12 | 运行指标端点 | 运行 metrics | system | `GET /runs/{id}/metrics` 返回 `attempts / retries / p95_attempt_ms / gates / usage.estimated_cost / per_node` 等字段 | passed | metrics 返回 `attempts=16`、`retries=1`、`busy_ms=1819`、`p95_attempt_ms=557`、`gates`(approved 5 / rejected 1)、`usage.estimated_cost`、`per_node` 20 条明细 |
| TC-B13 | 门挂起不占协程 / 重启保留 | 人工门挂起 / 断点恢复 | system | 等待人工时运行置 `waiting_human`，进程不卡死；待决门在重启后原样保留 | 未执行 | 日志可见挂起期 run 置 `waiting_human`；本次未杀进程重启，重启后待决门保留未验证 |
| TC-B14 | 编排工作台冒烟（可选） | 计划生成 / 校验 / 提交 | system | `scripts/smoke_plan.py` 可建计划 → 生成 → 校验 → 仅保存提交，任务数、波次、关键路径指标正常 | 未执行 | 脚本硬编码 `8375` 端口，本次应用起在 `8099`，连接被拒绝；计划规划 API 已由 TC-A09 自动化覆盖 |
| TC-B15 | 节点动作保护（可选） | 节点动作矩阵 / 状态保护 | integration | 运行中节点不可 `retry/reset/skip`（409）；终态运行 `retry` 返回 409 `run_terminal`，运行状态与 `error` 不变 | 未复核 | 运行中/终态节点动作的 409 保护已由 TC-A07（22 条自动化用例）覆盖，本次 E2E 未重复手工验证 |

## 4. C 组：真实模型验收用例

真实模型运行：run `3b7fa6e12575`，供应商 DeepSeek（`https://api.deepseek.com/v1`，协议 `openai_chat`），模型 `deepseek-flash`（展示名 DeepSeek-V4.1-Flash），并发 1、单节点超时 600s；需求文本取自 [`ledger-idea.txt`](./ledger-idea.txt)。验收参考路径见 [`real-model-ledger-test.md`](./real-model-ledger-test.md)。

| 编号 | 用例 | maps | type | 预期结果 | 状态 | 实际结果 / 证据 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-C01 | 接入真实供应商与模型 | 供应商 / 模型连接 | integration | 新增供应商并填写 Base URL / API Key；连接测试通过，模型可被拉取或手动录入 | passed | 供应商 #3「DeepSeek」创建，密钥加密入库（掩码 `sk-****4fcf`）；连接测试 `ok=true`、延迟 869ms；模型条目 #4 `deepseek-flash` 录入。注：模型名 `DeepSeek-V4.1-Flash` 被服务端 400 拒绝（支持名 `deepseek-flash` / `deepseek-v4-pro`），回退为 `deepseek-flash` 后连通 |
| TC-C02 | 角色模型绑定 | 角色模板 / 模型绑定 | integration | 全部角色绑定到可用模型，无「尚未配置模型」报错 | passed | 11 个角色模板（discuss_pro / discuss_con / discuss_mod / proposal / requirement / design / task_breaker / pm / review / dev / delivery）`default_model_id` 全部绑定到模型 #4 |
| TC-C03 | 真实模型全流程跑通 | 端到端验收 | acceptance | 从一句想法经 5 道门跑到可执行产物，运行 `finished`，工件链齐全 | blocked | 文档链与前 4 道门通过：idea、discussion(v1–v4)、proposal、requirement、design、task 工件落盘，gate_proposal / gate_requirement / gate_design / gate_tasks 均 `approved`；dev 段 `dev_T1.1` 一次通过，`dev_T2.1` 三次尝试均因未调用 finish 收尾被判未完成（挂起门 #12，attempts=3、retries=2），`dev_T3.1` / `dev_T4.1` 未执行；运行停在 `waiting_human` / `execution`，未到 `finished`，无 verify、gate_accept、delivery 产物 |
| TC-C04 | 产物手工验证 | 交付物验收 | acceptance | 现场跑 CLI 命令与 `unittest`，测试全绿、输出与契约一致 | blocked | 仅 T1.1 存储层配套 `test_storage.py` 落地并 **34 个用例全绿**；账本 CLI（`add` / `list` / `summary`）与 `test_ledger.py` 全流程未完成，未现场演示 summary 总额 42.5 / 2 笔 / 交通+餐饮，契约验收未闭环 |

## 5. 覆盖与追溯说明

- A 组覆盖后端自动化可判定的功能域；B 组覆盖离线 Mock 下的系统级流程与人工门交互；C 组覆盖真实模型链路。
- 本次验收的 AC→TC 追溯以 B 组用例为主：方案 / 需求 / 设计 / 任务图 / 验收五道门分别对应 TC-B03、TC-B04、TC-B05、TC-B06、TC-B07。
- C 组本次仅完成供应商接入（TC-C01）与角色绑定（TC-C02）；端到端跑通（TC-C03）与产物手工验证（TC-C04）在 dev 执行段阻塞，详见报告 §3.3、§5、§6。
- 前端目前无自动化测试栈（见 README §8 已知限制），本次不编排前端自动化用例；界面级核对随 B 组 E2E 人工观察记录。
