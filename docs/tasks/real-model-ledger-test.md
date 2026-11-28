# 真实模型验收测试：随手账本 `ledger-cli`

这份文档是一条**可以直接照做**的验收路径：用你自己的 API Key，让 AM-Workflow 从一句想法跑到
可执行的代码，途中经过 6 个运行级人工门（另有会议内的确认点：讨论会议的名单确认一定会停，有澄清问题时也会停）、8 类工件和一次真实 `unittest` 验证。

- 配套需求规格：[`docs/tasks/ledger-idea.txt`](ledger-idea.txt)（直接粘贴到“新建运行”的输入框）
- 预计耗时：20–40 分钟（取决于模型速度）
- 建议预算：`300000` tokens（约 20 个节点：模板 17 + 2–3 个 `dev_*` 展开，含讨论 / 评审 / 需求确认三场会议与重试）
- 建议并发：`AM_NODE_CONCURRENCY=2`，避免真实供应商限流

## 0. 一页速览

| # | 步骤 | 操作 | 预期 |
|---|------|------|------|
| 1 | 启动 | `uvicorn app.main:app --port 8099` | 打开 `http://127.0.0.1:8099` |
| 2 | 接模型 | 供应商页新增 DeepSeek + `deepseek-chat` | 连接测试通过 |
| 3 | 绑角色 | 角色页把全部角色绑定到该模型 | 无“尚未配置”报错 |
| 4 | 建运行 | 粘贴 `ledger-idea.txt`，预算 300000 | 运行进入 `idea` |
| 5 | 过 6 个门 | 方案 → 评审裁决 → 需求确认 → 设计 → 任务图 → 验收（讨论会议里的名单确认、澄清确认也会各停一次，照确认点卡片处理） | 每次通过后自动续跑 |
| 6 | 验收 | 手工跑 CLI + `unittest` | 全绿，工件齐 |

## 1. 启动 API

前端产物需要先存在（仓库里通常已有 `frontend/dist`；没有就先 `npm run build`）。

```powershell
cd <仓库根目录>
$env:AM_NODE_CONCURRENCY = "2"     # 真实供应商限流更稳
$env:AM_NODE_TIMEOUT_S   = "600"  # 单节点 10 分钟
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8099
```

浏览器打开 <http://127.0.0.1:8099>。不需要启动 Mock 供应商（`scripts/mock_provider.py` 只在离线演示时用）。

> 数据落在仓库 `data\am.db`，运行目录在 `runs\<run_id>\`；换一个干净库就换 `AM_DATA_DIR`。

## 2. 接入你的模型

### 2.1 界面方式（推荐）

1. 左侧「供应商」→「新增供应商」。
2. 「从预设填充」选 **DeepSeek**，Base URL 应为 `https://api.deepseek.com/v1`
   ——**必须带 `/v1`**，适配器会直接拼 `/chat/completions`。
3. 填 API Key → 保存。选中预设并保存时，系统会自动写入该预设的内置模型（DeepSeek 为
   `deepseek-chat` 与 `deepseek-reasoner`），供应商标「模型」列会变成 2。
4. 模型不在预设里？在该供应商展开区点「拉取模型」：会请求 `GET {Base URL}/models`，
   勾选后导入（已存在的自动跳过）。也可以点「新增模型」手动填，弹窗顶部同样有「从供应商读取」
   下拉可自动填入模型名。
5. 点供应商行的「测试」：有本地模型时会真发一句 8-token 对话，返回延迟与回复；模型行的 ⚗ 按钮
   可单独验证某个模型名（401 一般是 Key 错，404 一般是模型名或 Base URL 错）。
6. 左侧「角色」→ 把**全部角色**绑定到该模型（主持人 `facilitator`、评审主席 `review_chair`、参会者 `architect` / `ux` / `qa` / `security` / `ops_cost` / `business`、需求方代理 `requester_proxy`、判定 `judge`、需求分析师 `requirement`、设计师 `design`、任务分解 `task_breaker`、PM `pm`、评审 `review`、开发 `dev`、交付 `delivery`）。未绑定的角色会
   回退到第一个可用模型；只有一个模型时能跑通，但多模型环境建议全绑，避免角色串用。
   `facilitator`（讨论主持人）负责提炼决策点、整合成稿；`review_chair`（评审主席）负责合并评审意见并在讨论阶段主持。

### 2.2 脚本方式（可选，等价于上面的点击，一步到位）

仓库自带 [`backend/scripts/setup_real_model.py`](../../backend/scripts/setup_real_model.py)：建供应商 → 建模型 →
把全部角色绑到该模型 → 建运行。密钥只从环境变量读，不进命令行历史。

```powershell
```powershell
cd <仓库根目录>\backend
$env:DEEPSEEK_API_KEY = "sk-..."
..\.venv\Scripts\python.exe scripts\setup_real_model.py --idea ..\docs\tasks\ledger-idea.txt
```

成功后会打印运行 id 与 `http://127.0.0.1:8099/runs/<id>`。只配置不建运行用 `--no-run`；
换别家模型用 `--provider-name / --base-url / --model / --api-key-env` 覆盖（Base URL 同样要带 `/v1` 这类前缀）。

## 3. 创建运行

1. 「运行」→「新建运行」。
2. 模板选 `sdlc_v1`，预算填 `300000`。
3. 把 `docs\tasks\ledger-idea.txt` 全文粘进需求框（约 900 字，**不要**只写一句话，契约越具体，
   下游测试越可验）。
4. 创建后进入运行详情页：左侧是阶段圆盘与人工门，右侧是节点/工件/事件。

## 4. 六个门的检查点

每个门都有「通过 / 驳回」。本次验收按下表检查，**至少演示一次“编辑后通过”和一次“驳回重做”**。

| 门 | 看什么 | 本次建议动作 |
|----|--------|--------------|
| 方案确认 `gate_proposal` | `proposal.md` 是否有目标、范围、非目标、风险、验收标准 | 在弹窗里给“非目标”补一句“不做 Web/云同步”，编辑后通过 |
| 评审裁决 `gate_review` | `review` 工件的问题清单与处置；阻塞项是否回流 | 直接通过 |
| 需求确认 `gate_requirement` | 需求是否可测（WHEN/THEN 或 Given/When/Then），有无歧义；确认会议报告是否列出走样与遗漏 | 直接通过 |
| 设计审查 `gate_design` | 模块划分、公开接口、错误处理、测试策略是否覆盖契约 | 直接通过 |
| 任务图 `gate_tasks` | 任务是否为 3 个左右、依赖是否合理（A/B 可并行、C 依赖 A/B） | 直接通过 → 展开出 `dev_*` 并行节点 |
| 验收 `gate_accept` | `verify` 节点日志是否 `Ran N tests ... OK`；工件链是否齐全 | 直接通过 |

驳回演示（选做，放在需求门最省时间）：反馈写「补充 CSV 导出需求：`export(csv_path)`，空账本导出仅表头」，
目标节点选 `requirement`。该门与所有下游节点会被重置，`attempt` 递增，运行重新排队。

## 5. 验收清单

- [ ] 运行状态 `finished`，约 `20` 个节点全部 `done`（模板 17 个；2–3 个 `dev_*` 是展开后新增的）。
- [ ] 磁盘：`runs\<run_id>\artifacts\` 下有 idea/discussion/proposal/review/requirement/design/task/changelog 八类 `.md`；
      `runs\<run_id>\workspace\` 下有 `ledger.py`、`test_ledger.py`、`README.md`。
- [ ] 手工跑 CLI（路径按实际 `run_id` 替换）：

  ```powershell
  cd runs\<run_id>\workspace
  ..\..\..\.venv\Scripts\python.exe ledger.py add 12.5 餐饮 --note 午饭
  ..\..\..\.venv\Scripts\python.exe ledger.py add 30 交通
  ..\..\..\.venv\Scripts\python.exe ledger.py list
  ..\..\..\.venv\Scripts\python.exe ledger.py summary
  ..\..\..\.venv\Scripts\python.exe -m unittest discover -s . -p "test_*.py"
  ```

  预期：`summary` 输出总额 `42.5`、笔数 `2`、分类 `交通/餐饮`；`unittest` 至少 6 个测试全绿。
- [ ] 指标：`GET http://127.0.0.1:8099/api/runs/<run_id>/metrics`，能看到
      `execution.attempts/retries/p95_attempt_ms`、`gates.*`、`usage.estimated_cost`、`per_node`。
      前端运行页的 STATE 面板同时显示“重试/尝试、单节点 P95、人工门等待”。
- [ ] 事件：运行页事件带能看到 `node_started / node_finished / gate_requested / gate_decided`，
      没有重复 `seq`，刷新页面后历史仍完整（SSE 断线重连走 `Last-Event-ID`）。

## 6. 故障排查

| 现象 | 原因 | 处理 |
|------|------|------|
| 401 / 403 | Key 错误、过期或无额度 | 在供应商页「测试连接」 |
| 404（`/chat/completions`） | Base URL 少了 `/v1` | 改成 `https://api.deepseek.com/v1` |
| 400，日志提示 tool_calls 格式 | 模型不完全兼容 OpenAI 工具协议 | 模型取消勾选「工具调用」，走 JSON 降级协议 |
| 节点报“尚未配置模型” | 没有任何启用的供应商/模型 | 检查供应商与模型的启用开关，再点「测试连接」 |
| `节点执行超时（600s…）` | 单次调用太慢/卡死 | 提高 `AM_NODE_TIMEOUT_S`，或缩小需求 |
| 429 限流 | 并行度超过供应商配额 | `AM_NODE_CONCURRENCY=1`，模型 `max_concurrency=1` |
| `BudgetExceeded` | 预算耗尽 | 新建运行把 `budget_tokens` 提到 600000 |
| `dev_*` 完成但报“未完成” | 模型没调用 `finish` 工具 | 看事件里的 `stopped_by`；门驳回并反馈“必须调用 finish 结束” |
| `验证命令失败` | 测试或 CLI 有错 | 看 `node_log` 输出；驳回验收门，目标选对应 `dev_*`，反馈贴错误 |
| 会议节点（讨论 / 评审 / 需求确认）停在“转人工”的门上 | 模型某一步的输出不合格（JSON 不合法、空正文、方案缺章节）。会议每在一个确认点挂起一次就算一次尝试，默认最多 3 次：经过名单、澄清两个确认点之后，再出一次不合格就不会自动重试 | 点「通过」让会议从出错的那一步续开（那一步会重新问模型）；日志里有“可能被截断”的警告时，到模型设置里调大最大输出 |

## 7. 进阶观察

1. **重试与退避**：故意在需求里留一个矛盾（如“金额用 `Decimal`”又要求“字段 `amount` 为 float”），
   观察设计门驳回后的 `attempt` 递增与 `node_retry_scheduled` 事件。
2. **停机恢复**：`dev_*` 运行中按 Ctrl+C 杀掉 uvicorn 再启动。API 启动时会自动 `recover()`：
   未回执的 attempt 置 failed、running 节点回 pending，然后从断点继续，已完成的节点不会重跑。
3. **取消收据**：运行中点「停止」，事件应出现“运行被取消，attempt 未回执”，节点回到 `pending`。
4. **成本**：`metrics.usage.estimated_cost` 依赖模型价格，想看得准就把 `price_in/price_out` 填对。
