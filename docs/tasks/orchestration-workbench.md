# 任务书：编排工作台（设计规格 → 任务分解 → 编排 → 可视化）

| 项 | 内容 |
|---|---|
| 任务 ID | ORCH-01 |
| 版本 | v0.1（草案，待确认） |
| 日期 | 2026-11-15 |
| 仓库 | `D:\Project\SoftwareProj\TeamWk\am-workflow` |
| 分支 | `feat/orchestration-workbench`（从 `main` 切出） |
| 依赖 | 现有 W1 引擎、`TASKGRAPH`、`Task/Node/Workflow`、endfield-ui 前端 |
| 参考文档 | `TeamWk/styx/设计规格说明书.md`、`docs/设计规格说明书.md`、`docs/模型接入配置设计.md` |
| 交付物 | 可运行代码 + API + 页面 + 测试 + 两份设计规格演示 + README 章节 + PR |
| 当前状态 | 仅完成任务书；**未开始写代码** |

---

## 0. 一句话目标

实现通用 SE 工作流中的一段垂直切片：**输入任意产品的设计规格，由 PM Agent 产出可验证的开发任务分解，由确定性编排器生成依赖图/波次/并行组/关键路径，并在一个可编辑、可重算、可提交执行的可视化工作台中呈现**。

微信、小红书、内容社区等只是测试案例，不得写死进代码、模板、断言或演示数据生成逻辑。

---

## 1. 背景与项目定位

AM-Workflow 是“通用、可定制的多智能体软件工程工作流平台”，不是某个具体项目的定制系统。完整流程为：

```text
想法 → 多体讨论 → 建设方案 → 多方评价 → 需求规格 → 设计规格
     → 任务分解 → 编排执行 → 交付
```

当前 W1 已具备：

- 固定 `sdlc_v1` 模板，8 个阶段，5 个人工门；
- `TASKGRAPH` 节点：`parse` 把 task.md 解析成 `Task`，`expand` 按任务依赖生成 `dev_*` 节点并重算 wave；
- 确定性调度：拓扑排序、wave 分批、同波并行、失败重试、人工门、断点恢复；
- 工件链：`idea / discussion / proposal / requirement / design / task / changelog`；
- SSE 事件、运行详情页、执行图、审批卡、工件编辑；
- 终末地风格控制台 UI + 可折叠左侧栏；
- 模型层 Provider/Model/Binding、能力降级、Mock 供应商。

本任务只做其中“设计规格 → 任务分解 → 编排 → 可视化”这一段，但必须作为**可复用阶段**接入，而不是一次性页面。

---

## 2. 核心原则（不可违背）

1. **PM Agent 只提议，编排器做决定。**
   - PM Agent 输出任务清单；
   - 依赖校验、环检测、wave、并行度、关键路径、角色/模型分配全部由程序计算；
   - 不允许模型直接生成或改写运行时 DAG。

2. **通用优先。**
   - 代码、提示词、模板、测试断言中不得出现具体产品名（微信、小红书等）；
   - 具体产品只允许出现在 `tests/fixtures/designs/` 与演示脚本中；
   - 任务数量、任务标题、角色分布不得硬编码。

3. **证据判定。**
   - 每份任务必须引用设计规格的具体章节；
   - 每个任务必须有可验证的验收标准或测试用例；
   - 校验失败必须给出结构化错误，不得“看起来成功”。

4. **可追溯。**
   - 保存输入设计规格、PM Agent 原始输出、校验后的任务清单、每次编排快照；
   - 编辑后可回滚到上一版有效快照。

5. **人工门。**
   - 生成结果默认进入“待确认”状态；
   - 用户确认后才能提交为 Run 或标记为可执行。

6. **不破坏现有能力。**
   - 运行详情页、既有 `TASKGRAPH`、既有 API 行为保持兼容；
   - 新增表/字段必须说明迁移与 SQLite 单连接池影响。

---

## 2.1 现有系统保护与允许的优化（分级约束）

本任务在现有 W1 代码上**增量实现**，目标不是“重做一版”；但允许在**有测试、有截图对比、有说明**的前提下，对现有设计做有边界的优化。

### 开工基线（Phase 0 必须记录）

- 记录 `main` HEAD SHA（当前 `0d39b11`），从它切 `feat/orchestration-workbench`；
- 记录基线测试结果：`pytest`（当前 14 passed）、`npm run typecheck`、`npm run build`；
- 保存基线截图：`/runs`、`/runs/:id`（完成态 + 等待态）、`/providers`、`/templates`；
- 记录一个可复现的 Mock 完整 run（7 类工件、5 个门、verify 退出码 0）。

### 变更分级

#### A 级：直接做（新增为主，不触碰旧逻辑）

- 新增 `plans / plan_tasks / plan_snapshots` 表；
- 新增 `/api/plans/*` 路由；
- 新增 `engine/planner.py`、`engine/plan_generation.py`；
- 新增 `/plans`、`/plans/:id` 页面与 `plan.css`；
- `App.tsx` 加一条导航与一个路由；
- Mock 增加 PM 角色分支；
- 新增测试、fixture、文档。

#### B 级：可自主优化，但必须保留行为并回归

允许做，但 PR 里必须写清“改了什么、为什么、影响面”，并附前后截图/测试：

- 从 `ExecutionMap`、`RunPage` 中抽取可复用的布局/连线/测量逻辑，供 `PlanGraph` 复用；旧页面功能与观感不得退化；
- 从 `engine/graph.py`、`engine/tasks.py` 抽取通用校验/拓扑函数；保留原函数签名与行为，补回归测试；
- 修复实现中撞见的既有小问题（如 board 高度裁切、连线过短、编号错位），带回归验证；
- 对现有页面做小幅视觉优化（间距、字号、状态色一致性），但必须四页 + 1080 全部复验；
- API 只做**向后兼容的字段新增**或错误信息改进；
- 优化 Mock 的共享工具函数，但旧角色输出与旧 Demo 结果保持稳定。

B 级改动的验收门槛：

1. 原有测试全绿；
2. 受影响页面的前后截图对比；
3. 不改现有路由路径、不改状态码语义、不删响应字段；
4. 不改变 `sdlc_v1` 的调度、门、回流行为；
5. PR 描述中有“优化说明”。

#### C 级：先确认再做

以下改动必须先说明原因、影响面、回归方案，等用户确认：

- 修改 `templates/sdlc_workflow.json` 的阶段/节点/门/依赖；
- 改现有路由路径、请求/响应语义、状态码；
- 删除或重命名现有页面、组件、路由、公开函数；
- 现有表做非增量迁移（删列、改主键、改类型）；
- 全局改 AntD token / 主题，可能影响所有页面；
- 替换调度器语义、状态机语义；
- 新增图布局库或大版本依赖升级。

#### D 级：不做

- 整体重写前端或后端；
- 换掉 React/AntD/FastAPI/SQLAlchemy；
- 删除既有测试来“让 CI 变绿”；
- 为了新页面删掉运行详情页或旧执行图；
- 把 Plan 强行塞进 Run 表、绕过状态机。

### 变更边界

| 对象 | 允许（A/B） | 需确认（C） |
|---|---|---|
| 现有 API 路由 | 加字段、改错误文案、加只读端点 | 改路径、改语义、删字段、改状态码 |
| `RunPage.tsx`、`ExecutionMap.tsx`、`GateConsole.tsx`、`FlowCanvas.tsx`、现有 panels | 抽取共享逻辑、修复 bug、小幅视觉优化 | 整体重写、删除、替换交互模型 |
| `engine/graph.py`、`engine/tasks.py`、`engine/runners.py`、`engine/orchestrator.py` | 抽函数、补校验、修 bug、加测试 | 改 `sdlc_v1` 调度/门/回流语义 |
| `templates/sdlc_workflow.json` | 只读引用 | 改阶段、节点、门、依赖 |
| 现有表 | 加可空列、加索引 | 删表、删列、改主键、破坏性迁移 |
| `endfield.css`、`console.css` | 追加作用域规则、修局部样式、统一 token（需四页复验） | 全局重置、影响所有页的主题替换 |
| `scripts/mock_provider.py` | 加 PM 分支、抽共享工具 | 改旧角色输出导致旧 Demo 变化 |
| 依赖 | 复用现有 React/AntD/xyflow | 新增图布局库、大版本升级、换框架 |

### 新代码放哪里

- 新增表：`plans`、`plan_tasks`、`plan_snapshots`；
- 新增路由：`backend/app/api/plans.py`，在 `main.py` 注册；
- 新增引擎：`engine/planner.py`、`engine/plan_generation.py`；
- 新增页面：`pages/PlansPage.tsx`、`pages/PlanPage.tsx`、`canvas/PlanGraph.tsx`、`panels/PlanInspector.tsx`、`panels/PlanSourcePanel.tsx`、`styles/plan.css`；
- 共享逻辑优先抽到 `canvas/graphLayout.ts`、`engine/graph.py` 等中性位置，旧组件改为调用共享函数；
- 旧组件保留导出与行为，不因新页面删除。

### 建议的 B 级优化（非强制，按收益排序）

1. 把 `ExecutionMap` 的“节点测量 + 正交连线 + wave 定位”抽成共享布局函数，`PlanGraph` 直接复用，避免两套图逻辑漂移；
2. 把 wave/拓扑校验从 `engine/tasks.py`、`engine/graph.py` 抽成单一实现，运行图与编排图共用；
3. 统一 `status-pill`、`ef-tag`、账本行的状态色与字号，减少两页之间的视觉不一致；
4. 修复已知的 board 高度裁切、账本编号、窄屏连线过短问题，并补回归截图；
5. 把 AntD 的 Table/Button/Input token 收敛到 `console.css` 一处，避免页面各写一套覆盖。

这些优化如果做，按 B 级门槛执行：测试全绿、前后截图、PR 说明。

### 提交前回归清单（缺一不可）

1. `pytest` 全绿（原有 14 项 + 新增项），不得跳过/删除原测试；
2. `npm run typecheck`、`npm run build` 通过；
3. `/runs`、`/runs/:id`（完成态/等待态）、`/providers`、`/templates` 四个页面人工过一遍；
4. 门审批：通过、驳回+反馈+指定节点、编辑工件后通过，三种都正常；
5. SSE：断线后带 Last-Event-ID 能补发；
6. 左栏展开/折叠、≤1080 自动折叠正常；
7. `/runs/:id` 深链接直接打开 200；
8. Mock 完整 run 仍能产出 7 类工件、5 个门、verify 退出码 0；
9. 任何 B 级优化都有前后截图与说明；C 级改动已获确认；
10. `git diff` 中没有“未经说明的整体重写”“大段格式化”“删除既有测试”。

### 回滚

- 所有改动在独立分支，未合并前可整支丢弃；
- 新表只增不改，回滚不需要数据迁移；
- 不改 `data/` 与 `runs/` 中已有内容；
- 若合并后发现回归，优先 revert 合并提交，不做“边修边删”。

> 一句话：**允许优化，不允许失控重写。** A 级直接做；B 级可自主做但必须带测试、截图和说明；C 级先停下来问；D 级不做。判断标准不是“有没有碰旧代码”，而是“旧功能是否仍然成立、是否可回滚”。

## 3. 非目标

本期不做：

- 完整 SDLC 的其它阶段重做；
- 自动写代码、自动跑测试（沿用现有开发节点）；
- 多用户/权限/审计；
- 把执行图改成自由拖拽编辑器（v1 只做表单编辑 + 自动布局）；
- 引入新的图布局库（优先复用现有 SVG 测量 + 正交连线方案）；
- 真实产品的完整需求分析；
- MCP、向量记忆、多 run 队列。

---

## 4. 用户角色与典型场景

### 4.1 角色

- **产品/项目负责人**：给出一份设计规格，希望快速看到“要做什么、先做什么、能并行什么”。
- **PM/架构师**：检查任务拆分是否合理，补依赖、改角色、调优先级。
- **开发/评审**：查看自己被分配的任务、依赖与验收标准。
- **课程演示者**：用两份不同产品的设计规格，证明这是通用工作流，而不是单项目脚本。

### 4.2 主场景

1. 用户从已有 run 的 `design` 工件选择设计规格，或上传/粘贴一份设计规格。
2. 点击“生成任务分解”，页面流式显示 PM Agent 的生成过程。
3. 生成完成后，页面同时展示任务表、依赖图/波次泳道、关键路径与摘要指标。
4. 用户点开某个任务，查看来源章节、验收标准、分配角色与依赖。
5. 用户修改依赖或新增任务，系统实时校验并重算 wave；若成环，图与表同时高亮环并禁止提交。
6. 用户确认后选择“仅保存编排”或“创建运行并启动”。
7. 换成另一份设计规格，重复以上流程，不需要改代码。

---

## 5. 端到端流程与状态机

### 5.1 Plan 状态

```text
draft → generating → generated → validating → valid
                                   ↘ invalid
valid → editing → validating → valid | invalid
valid → committed（终态）
任意状态 → archived（人工归档）
generating → failed（模型/超时/解析失败）
```

### 5.2 页面状态

- 空态：没有输入设计规格；
- 输入态：已选规格，未生成；
- 生成中：SSE token 流 + 骨架屏 + 可中止；
- 校验中：短暂 loading；
- 有效态：任务表/图/摘要可交互；
- 无效态：错误横幅 + 出错任务/边高亮 + 修复建议；
- 已提交：只读展示 + 跳转 Run；
- 失败态：模型错误/超时/解析失败，保留输入与部分输出。

---

## 6. 功能需求

### 6.1 设计规格输入

支持三种来源，优先级从高到低：

1. **现有工件**：从某个 run 的 active `design` 工件读取；展示 run id、版本、字数、章节标题。
2. **上传**：`.md` / `.txt`，限制 1 MB；读取 UTF-8。
3. **粘贴**：直接输入文本，实时显示字数与识别到的章节数。

要求：

- 输入为空或少于 200 字时禁止生成，提示“设计规格过短，无法可靠分解”；
- 解析 Markdown 标题（`#`/`##`/`###`），生成章节索引 `{id, title, level, line}`；
- 章节索引用于后续“任务必须引用真实章节”的校验；
- 保存原始文本与来源，便于追溯；
- 不允许在浏览器端做“假生成”。

### 6.2 PM Agent

- 角色：新增 `pm`（或在 `task_breaker` 上扩展，见待确认项），显示名“PM Agent / 任务分解器”；
- 输入：产品名 + 设计规格全文 + 章节索引 + 约束（最大任务数、角色枚举、字段说明）；
- 输出：**严格 JSON**，不允许 Markdown 代码围栏、不允许解释性前后缀；
- 通过现有模型层调用，支持流式 token；
- 必须有 Mock 路径，保证离线可演示；
- 输出解析失败：自动修复一次（提取 JSON、补字段），仍失败则 `failed`，展示原始输出与错误位置；
- 生成中可通过 `plan_token` 事件推送文本；
- 生成结果原文与解析结果都入库/入快照。

#### PM Agent 输出 Schema

```json
{
  "product": "产品名",
  "summary": "一句话说明拆分依据",
  "tasks": [
    {
      "uid": "T1",
      "title": "任务标题",
      "description": "做什么、边界是什么",
      "deps": [],
      "design_section": "3.2 数据模型",
      "acceptance": ["可验证标准 1", "可验证标准 2"],
      "role": "dev",
      "effort": "M",
      "priority": "P0"
    }
  ]
}
```

字段约束：

| 字段 | 约束 |
|---|---|
| `uid` | `T` + 数字，唯一，建议按依赖顺序 |
| `title` | 6–40 字，动词开头 |
| `description` | 20–200 字，说明边界与不做的事 |
| `deps` | 数组，元素必须存在于本清单；允许空 |
| `design_section` | 必须匹配章节索引中的标题（允许编号前缀差异） |
| `acceptance` | 1–5 条，可测试，不接受“体验良好”这类空话 |
| `role` | `dev / test / design / pm / review / ops` 之一 |
| `effort` | `S / M / L / XL`，映射 1/2/3/5 点 |
| `priority` | `P0 / P1 / P2` |

任务数量：8–40；少于 8 视为拆分不足，多于 40 视为过度拆分，均返回可修复错误。

#### PM Agent 系统提示词（草案）

```text
你是 AM-Workflow 的 PM Agent，只负责把设计规格拆成开发任务，不负责排期算法，也不写代码。

规则：
1. 只依据给定设计规格，不得编造章节、功能或技术选型。
2. 每个任务必须引用一个真实章节标题；无法归入任何章节的内容不要拆。
3. 任务粒度：一个开发者能在 0.5–2 天内完成并独立验证；避免“做整个前端”这类大任务。
4. 依赖只能指向本清单中的任务，且必须构成有向无环图；优先让无依赖任务并行。
5. 每个任务给出 1–5 条可执行验收标准，必须能被测试或人工检查。
6. 只输出 JSON，不要 Markdown 围栏，不要解释。
7. 任务数控制在 8–40；产品名只用于 summary，不得写进任务标题。
```

### 6.3 编排器

确定性程序，输入校验后的任务清单，输出编排快照。

#### 校验

1. `uid` 非空、唯一、格式合法；
2. 标题/描述/角色/工作量/优先级合法；
3. `deps` 元素存在，禁止自依赖；
4. Kahn 拓扑排序检测环，返回环路径（如 `T3 → T5 → T3`）；
5. `design_section` 能在章节索引中匹配（允许“3.2”与“3.2 数据模型”互相匹配）；
6. 任务数 8–40；
7. `acceptance` 至少 1 条，且不是空串。

错误结构：

```json
{
  "code": "DEP_CYCLE",
  "message": "任务依赖存在环",
  "tasks": ["T3", "T5"],
  "cycle": ["T3", "T5", "T3"],
  "hint": "删除 T5 → T3 的依赖，或拆分 T3"
}
```

#### 计算

- `wave[t] = 0`（无依赖）；否则 `max(wave[dep]) + 1`；
- 同 wave 任务进入同一并行组；
- 关键路径：按 `effort` 点值做最长路径 DP，返回任务序列与总点数；
- 指标：任务数、wave 数、最大并行度、关键路径长度、总点数、角色分布；
- 角色分配：按 `role` 查 `AgentTemplate`；未绑定模型时回退第一个 enabled 模型，并在结果中标记 `fallback: true`；
- 生成不可变 `PlanSnapshot`，`version` 自增；编辑只改草稿，重新校验通过后生成新快照。

#### 伪代码

```python
def build_plan(tasks, sections, templates, models):
    errors = validate(tasks, sections)
    if errors:
        return {"status": "invalid", "errors": errors}

    order = kahn(tasks)                 # 已保证无环
    wave = {t.uid: 0 for t in tasks}
    for uid in order:
        deps = task_by_uid[uid].deps
        if deps:
            wave[uid] = max(wave[d] for d in deps) + 1

    groups = group_by_wave(tasks, wave)
    critical = longest_path(tasks, effort_points)
    metrics = summarize(tasks, wave, groups, critical)
    assignments = assign_roles(tasks, templates, models)

    return {
        "status": "valid",
        "dag": edges(tasks),
        "waves": groups,
        "critical_path": critical,
        "metrics": metrics,
        "assignments": assignments,
    }
```

### 6.4 可视化

页面必须同时回答四个问题：

1. 总共有多少事？（任务数、总点数）
2. 先做什么、能并行什么？（wave 泳道、并行组）
3. 哪条链最慢？（关键路径）
4. 每个任务具体做什么、依据哪一章？（详情面板）

#### 布局

```text
┌──────────────────────────────────────────────────────────────┐
│ 页面头：产品名 / 状态 / 主要操作（生成、校验、提交）          │
├──────────────────────────────────────────────────────────────┤
│ 摘要条：任务数 · 波次 · 最大并行 · 关键路径 · 总点数          │
├───────────────┬──────────────────────────────┬───────────────┤
│ 左：输入与来源 │ 中：图/表切换 + 画布/任务表   │ 右：任务详情   │
│ 设计规格摘要   │ wave 列 + 依赖边 + 关键路径   │ 字段/验收/依赖 │
│ 章节索引       │ 或可排序任务表                │ 编辑/删除/新增 │
└───────────────┴──────────────────────────────┴───────────────┘
```

- 中栏默认“图”，可切“表”；两种视图共享选中任务；
- 图按 wave 从左到右分列，节点显示 `uid + 标题（截断）+ 角色 + effort`；
- 依赖边使用正交折线；关键路径边加粗并着色；环/错误边用红色虚线；
- 悬停/选中节点时，右栏同步详情；点击空白取消选中；
- 任务多时支持缩放/横向滚动，不把节点压成不可读；
- 1080 宽度：左栏折叠为抽屉，图可横向滚动，右栏改为底部抽屉或覆盖层；
- 空态/生成中/无效态都要有独立视觉，不能只显示空白画布。

#### 摘要指标

- 任务数 / 波次数 / 最大并行度 / 关键路径长度 / 总点数；
- 角色分布（dev/test/design/review…）；
- 模型回退数量（有回退时显示警示）；
- 校验状态与最后校验时间。

### 6.5 编辑、重算与提交

- 编辑任务字段：标题、描述、章节、验收标准、角色、effort、priority；
- 依赖编辑：多选已有任务；禁止选自己；前端即时提示，后端最终校验；
- 新增任务：默认无依赖，uid 自动生成（可改），进入当前 wave 计算；
- 删除任务：若有下游依赖，提示影响范围并要求确认；
- 每次编辑后 400ms 防抖调用 `POST /plans/{id}/validate`，返回新快照或错误；
- 无效状态下禁止提交；保留上一版有效快照，可“恢复上一版”；
- 提交：
  - **仅保存编排**：状态 `committed`，不建 Run；
  - **创建运行**：把任务写入 `Task`，按任务生成 `dev_*` 节点，复用 `instantiate_nodes`/`compute_waves`，创建 Run 并写 design 工件引用；
  - 提交前二次确认，展示将创建的任务数/节点数/预计预算；
- 提交后跳转运行详情页，plan 只读并显示 run 链接。

### 6.6 通用性与模板

- 页面与 API 不依赖具体产品；
- 角色枚举从 `AgentTemplate` 读取，允许项目自定义；
- 任务数量上下限、effort 映射放在配置/常量，不散落；
- 提供两份 fixture 设计规格：
  - `tests/fixtures/designs/chat-product.md`（即时通讯类）
  - `tests/fixtures/designs/content-product.md`（内容社区类）
- 断言两份输入的产出在任务数/角色分布/波次结构上明显不同；
- 代码检索测试：`backend/app`、`frontend/src`、`templates/` 中不得出现 fixture 产品名。

---

## 7. UI/UX 规范

### 7.1 风格基线

沿用现有 endfield-ui 控制台：

- 纸白 `#F4F4F2`、面板 `#FFFFFF`、墨 `#161616`、次级墨 `#4A4A46`；
- 1px 细线分区，无阴影、无发光、无渐变；
- Saira 英文 + 中文黑体；英文大写、字距 0.14em；
- 真实数字，不用假进度条。

### 7.2 酸性黄

本页在原风格上**增加**酸性黄，但保持“唯一强调色只做填充”：

允许出现的位置（最多 5 类锚点）：

1. 当前选中任务节点/行的左侧 4px 填充条；
2. 当前 wave 的表头底边或左侧标记；
3. 关键路径边/关键任务角标；
4. 主操作按钮（生成/提交）的填充；
5. 生成中状态的脉冲点。

禁止：

- 黄色正文、黄色细线、黄色大面积背景；
- 浅色底上黑底黄大字；
- 同时用黄+红+绿表达三种状态；错误只用红色，成功用墨色/勾。

### 7.3 硬角与圆角

采用“结构硬、交互软”的两档半径：

| 元素 | 半径 |
|---|---|
| 页面分区、面板、表格、图容器、节点卡片 | `0` 或 `2px` |
| 输入框、按钮、任务 chip、状态标签 | `6px` 或 `8px` |
| 头像/圆点等特殊元素 | 可 `999px`，但全页不超过 2 处 |

要求：

- 不出现 `12px+` 的大圆角卡片；
- 同一元素不同时使用切角与圆角；
- 可点击元素用圆角/黄色 hover 提示，展示容器保持直角；
- 不因为圆角引入阴影。

### 7.4 排版与密度

- 页面标题 28–32px；区块标题 16–20px；正文 12–13px；微标签不小于 10px；
- 任务表行高 ≥34px；图节点最小 120×44；
- 1440 下三栏为 `300px / 1fr / 360px`；1080 下左栏抽屉、右栏覆盖；
- 长标题两行截断 + Tooltip；
- 所有交互有 focus 态，键盘可操作。

### 7.5 文案

- 状态词统一：`草稿 / 生成中 / 待校验 / 可提交 / 有错误 / 已提交 / 生成失败`；
- 错误文案说“哪里错、怎么修”，不写“系统错误”；
- 空态给下一步动作，不只写“暂无数据”。

---

## 8. 数据模型（推荐方案）

> 实施前先确认：优先复用现有表；以下为推荐最小模型。

### 8.1 `plans`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | str PK | uuid hex 12 |
| `product_name` | str | 用户输入 |
| `source_type` | str | `artifact / upload / text` |
| `source_ref` | str? | run_id/工件类型/文件名 |
| `design_text` | text | 原始设计规格 |
| `sections_json` | text | 章节索引 |
| `status` | str | `draft/generating/generated/valid/invalid/committed/failed/archived` |
| `model_id` | int? | 生成所用模型 |
| `raw_output` | text? | PM Agent 原始输出 |
| `error` | text? | 失败原因 |
| `created_at/updated_at` | datetime | |

### 8.2 `plan_tasks`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | int PK | |
| `plan_id` | str FK | |
| `task_uid` | str | 唯一（plan 内） |
| `title` | str | |
| `description` | text | |
| `deps_json` | text | uid 数组 |
| `design_section` | str | |
| `acceptance_json` | text | 字符串数组 |
| `role_key` | str | |
| `effort` | str | S/M/L/XL |
| `priority` | str | P0/P1/P2 |
| `order_index` | int | |

### 8.3 `plan_snapshots`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | int PK | |
| `plan_id` | str FK | |
| `version` | int | 自增 |
| `tasks_json` | text | 校验后的任务 |
| `dag_json` | text | 边 |
| `waves_json` | text | 波次分组 |
| `critical_path_json` | text | 序列与点数 |
| `metrics_json` | text | 摘要 |
| `validation_json` | text | 错误/警告 |
| `created_at` | datetime | |

约束：

- 同一 plan 只允许一个 `valid` 快照；旧快照保留；
- SQLite 写入必须在 `session_scope()` 内完成，SSE 生成器不持 session；
- 大文本（design_text/raw_output）可存文件并在表中存路径，二选一需统一。

---

## 9. API 设计

统一前缀 `/api`。错误统一 `{code,message,details,hint}`，HTTP 400/404/409/422。

### 9.1 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/plans` | 创建草稿：`{product_name, source_type, source_ref?, design_text?}` |
| `GET` | `/plans?status=` | 列表，含任务数/状态/更新时间 |
| `GET` | `/plans/{id}` | 详情：plan + tasks + 最新 snapshot + 章节索引 |
| `POST` | `/plans/{id}/generate` | 触发 PM Agent；返回 202 + 事件流 |
| `GET` | `/plans/{id}/events` | SSE：`plan_token/plan_generated/plan_failed` |
| `POST` | `/plans/{id}/validate` | 重新校验并生成快照；返回 valid/invalid |
| `PATCH` | `/plans/{id}/tasks/{uid}` | 编辑任务字段/依赖 |
| `POST` | `/plans/{id}/tasks` | 新增任务 |
| `DELETE` | `/plans/{id}/tasks/{uid}` | 删除任务（校验下游） |
| `POST` | `/plans/{id}/commit` | `{mode:"save"\|"run"}` 提交 |
| `POST` | `/plans/{id}/archive` | 归档 |
| `GET` | `/plans/{id}/snapshots` | 快照历史 |
| `POST` | `/plans/{id}/restore/{version}` | 恢复上一版 |

### 9.2 事件

新增并写入 README：

- `plan_created`
- `plan_generation_started`
- `plan_token`
- `plan_generated`
- `plan_validation_failed`
- `plan_validated`
- `plan_task_updated`
- `plan_committed`

事件 payload 至少含 `plan_id`、`status`、`version`；token 事件不落库。

### 9.3 并发与幂等

- 同一 plan 同时只允许一个生成任务；重复触发返回 409；
- 生成中允许中止（`POST /plans/{id}/cancel`），状态回 `generated` 或 `failed`；
- 提交幂等：已 committed 再提交返回当前结果，不重复建 Run。

---

## 10. 后端实现建议

### 10.1 文件

```text
backend/app/
  api/plans.py                 # 路由
  engine/planner.py            # 校验/拓扑/wave/关键路径/分配
  engine/plan_generation.py    # PM Agent 调用、解析、修复、事件
  agents/templates/roles.py    # 新增 pm 角色（或复用 task_breaker）
  db/models.py                 # Plan/PlanTask/PlanSnapshot
  api/schemas.py               # 请求/响应模型
```

### 10.2 复用

- `engine/graph.py::compute_waves`：优先复用；若输入结构不同，抽出通用函数并补测试；
- `engine/tasks.py::parse_tasks/validate_tasks`：参考其校验风格与错误结构；
- `engine/runners.py` 的 `TASKGRAPH expand`：提交时复用建节点逻辑；
- `models/pool.py`：取模型与能力；
- `engine/events.py`：事件写入与 SSE；
- `artifacts/store.py`：design 工件读取与 plan 快照落盘（可选）。

### 10.3 Mock

- `scripts/mock_provider.py` 增加 PM 角色分支：按设计规格章节生成 12–18 个任务；
- 两份 fixture 产出不同任务集合；
- 保证离线演示不依赖真实模型。

---

## 11. 前端实现建议

### 11.1 文件

```text
frontend/src/
  pages/PlansPage.tsx          # 列表 + 新建
  pages/PlanPage.tsx           # 工作台
  canvas/PlanGraph.tsx         # wave 列 + 依赖边 + 关键路径
  panels/PlanInspector.tsx     # 任务详情/编辑
  panels/PlanSourcePanel.tsx   # 输入与章节索引
  api/plans.ts                 # 请求与 SSE
  styles/plan.css              # 页面样式
```

### 11.2 交互细节

- 生成按钮在生成中变“中止”，并显示已收到 token 数；
- 生成完成后自动选中第一个关键路径任务；
- 任务表支持按 wave/角色/优先级排序；
- 图与表共享选中态；切换视图不丢失选中；
- 编辑依赖时，候选列表排除自己与会造成环的任务（前端预判，后端最终校验）；
- 错误态点击错误项，定位到对应任务/边；
- 提交成功后显示 run 链接，不自动跳转（避免丢失对比上下文）。

### 11.3 与现有页面

- `App.tsx` 左侧栏新增“编排 / PLANS”入口（图标可用 `ApartmentOutlined` 或 `PartitionOutlined`）；
- 运行详情页保持现状；可在 Run 详情增加“查看来源编排”链接（可选）；
- 不删除旧 `FlowCanvas`，但新页面不复用它；
- 样式只新增 `plan.css`，不覆盖运行详情页既有类。

---

## 12. 测试计划

### 12.1 单元测试

- `validate`：重复 uid、缺依赖、自依赖、环、非法章节、空验收、任务数越界；
- `build_plan`：wave 计算、并行组、关键路径、指标、角色回退；
- PM 输出解析：纯 JSON、带围栏、缺字段、多余字段、非法 JSON；
- 章节匹配：编号 vs 标题、大小写、空白。

### 12.2 API 测试

- 创建/读取/列表；
- 生成（Mock）→ valid；
- 编辑依赖 → 重算；
- 成环 → 400 + `DEP_CYCLE`；
- 提交 save / run 两种模式；
- 重复提交幂等；
- 归档。

### 12.3 前端测试/验收

- typecheck、build；
- 1440/1080 截图；
- 空态、生成中、无效态、有效态、已提交五态截图；
- 键盘 Tab 可走完主要操作；
- console 无报错。

### 12.4 通用性测试

- `chat-product.md` 与 `content-product.md` 分别生成；
- 断言任务数/角色分布/波次结构不同；
- 检索代码与模板，确保不含 fixture 产品名。

---

## 13. 验收标准（Definition of Done）

1. 可从 design 工件、上传、粘贴三种来源创建 plan。
2. Mock 模式下，两份不同设计规格生成不同任务清单，且均通过校验。
3. 任务表、wave 泳道图、依赖边、关键路径、摘要指标正确显示。
4. 修改依赖后 1 秒内完成重算并更新图/表/摘要。
5. 人为制造环，页面高亮环并禁止提交；错误信息包含环路径。
6. 缺失依赖、非法章节、空验收均有明确错误。
7. 生成失败/超时保留输入与已生成部分，可重试。
8. 提交“仅保存”不创建 Run；提交“创建运行”生成正确 Task/Node 并可启动。
9. 代码、模板、断言中不含具体产品名（grep 通过）。
10. `pytest` 全绿；`npm run typecheck`、`npm run build` 通过；浏览器 console 无错误。
11. 1440 与 1080 截图齐全；包含五态。
12. README 增加“编排工作台”章节：输入、输出、API、边界、失败处理、演示步骤。
13. PR 通过 review，未直接推 main。
14. 原有四页、门审批、SSE 补发、左栏折叠全部回归通过；`git diff` 无未经说明的删除/重命名/整体重写；B 级优化均有前后截图与说明。
15. PR 描述附基线/回归证据：基线 SHA、pytest 14+、typecheck/build、四页截图、Mock 完整 run 的 7 工件/5 门/退出码 0。

---

## 14. 执行计划（给编码 Agent）

### Phase 0：勘察与方案（0.5 天）

- 记录基线：main SHA、pytest/typecheck/build 结果、四页截图、Mock 完整 run 证据；
- 阅读：README、`docs/设计规格说明书.md`、`backend/app/engine/*`、`frontend/src/pages/RunPage.tsx`、`canvas/ExecutionMap.tsx`、`panels/GateConsole.tsx`、`styles/console.css`；
- 输出 10 行以内方案：数据模型、API、页面结构、与现有代码的边界、风险；
- 等用户确认后再写代码。

### Phase 1：后端骨架（1 天）

- 表 + schema + `POST/GET/PATCH`；
- 校验器 + 单测。

### Phase 2：PM Agent + 编排器（1 天）

- 角色模板 + 提示词 + JSON 解析/修复；
- wave/关键路径/指标；
- Mock 分支；
- 单测覆盖错误路径。

### Phase 3：前端工作台（1.5 天）

- 列表/新建/工作台；
- 图 + 表 + 详情 + 编辑；
- 五态 + 响应式；
- 接入 SSE。

### Phase 4：提交与联调（0.5 天）

- commit save/run；
- 两份 fixture 端到端；
- 截图 + README。

### Phase 5：三轮自动迭代（1 天）

每轮：

1. 用 3 个子智能体并行审查：
   - 架构/数据一致性；
   - UX/视觉与响应式；
   - 测试覆盖与错误路径。
2. 汇总问题，按 P0/P1/P2 排序；
3. 修复 P0/P1；
4. 重新跑 typecheck/build/pytest；
5. 写 3 条自我批评到 `docs/tasks/orchestration-workbench-log.md`。

三轮后仍存在的问题写入“已知限制”，不得假装解决。

---

## 15. Git 与交付

- 分支：`feat/orchestration-workbench`；
- 提交拆分：
  1. `feat(plan): 数据模型与校验器`
  2. `feat(plan): PM Agent 生成与 Mock`
  3. `feat(plan): 编排 API 与快照`
  4. `feat(ui): 编排工作台与可视化`
  5. `test(plan): 单元/API/通用性`
  6. `docs: 编排工作台说明与演示`
- 不直接推 main；开 PR，附截图、演示步骤、已知限制；
- 若仓库规则要求审批，按现有流程走；
- 交付时给出：分支、PR、commit 列表、测试结果、截图路径、演示命令。

---

## 16. 演示脚本（课程用）

1. 启动 Mock + API + 前端（8376/8375/5173 或当前可用端口）。
2. 打开“编排”，选择 `chat-product.md`，生成。
3. 展示流式生成、任务表、wave 图、关键路径、摘要。
4. 点开一个任务，展示来源章节与验收标准。
5. 删除一条依赖，展示 wave 变化；再加回，展示恢复。
6. 人为加一条成环依赖，展示错误与修复。
7. “仅保存编排”，返回列表看到已提交。
8. 换 `content-product.md`，重复生成，强调任务结构不同。
9. 选一份“创建运行”，跳转 Run 详情展示任务/节点。
10. 打开 1080 宽度，展示左栏折叠与图横向滚动。

---

## 17. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 模型输出不稳定 | 解析失败/任务质量差 | 严格 schema、修复一次、Mock 兜底、错误可见 |
| 过度拆分/拆分不足 | 任务 40+ 或 <8 | 上下限 + 明确错误 + 提示词约束 |
| 章节匹配不准 | 大量 `SECTION_MISSING` | 编号/标题双向匹配、模糊匹配、人工可改 |
| 图节点过多 | 不可读/性能差 | wave 列 + 缩放 + 折叠 + 表视图 |
| 编辑后频繁重算 | 请求抖动 | 400ms 防抖 + 后端幂等 |
| SQLite 写锁 | 生成中卡顿 | 短事务、SSE 不持 session、避免长事务 |
| AntD 圆角冲突 | 风格不统一 | 组件 token 覆盖 + `plan.css` 限定作用域 |
| 误把案例写死 | 违背通用目标 | fixture 隔离 + grep 测试 |
| 提交建 Run 破坏现有模板 | 运行详情异常 | 复用 `instantiate_nodes`，补集成测试 |

---

## 18. 待确认问题

1. **PM Agent 角色**：复用 `task_breaker` 并改显示名，还是新增 `pm`？（建议新增 `pm`，保留 `task_breaker` 兼容旧模板）
2. **提交行为**：默认“仅保存”还是“创建运行”？两者都做时，哪个是主按钮？
3. **输入来源**：三种都做，还是先做“现有 design 工件 + 粘贴”，上传放二期？
4. **Plan 与 Run 边界**：Plan 是否允许一个 plan 对应多个 run（对比不同模型）？建议 v1 一对一。
5. **任务上限**：40 是否合适？大型设计规格可能需要 60+，是否改为按章节自适应？
6. **是否允许手工改 uid**：改 uid 会影响依赖引用；建议 v1 不允许，只允许改标题/描述/依赖。

---

## 19. 已知不做（v1 边界）

- 不做拖拽连边；
- 不做甘特图/工时日历；
- 不做多人协作与评论；
- 不做 plan 版本 diff 可视化（只保留快照与恢复）；
- 不做自动修复环（只提示，人工改）；
- 不做真实模型的多模型对比（仅保留字段）。

---

## 20. 附录

### A. 示例设计规格片段

```markdown
## 3. 功能需求

### 3.1 账号与登录
支持手机号验证码登录、第三方登录；登录态 7 天有效。

### 3.2 内容发布
支持图文、短视频；发布前可存草稿；内容需经过审核后可见。

### 3.3 信息流
按关注/推荐两个 tab 展示；支持下拉刷新与分页。
```

对应任务示例：

```json
{
  "uid": "T2",
  "title": "实现内容发布草稿与审核状态",
  "description": "覆盖图文/短视频发布、草稿保存、提交审核、审核中/通过/拒绝状态展示；不包含推荐算法。",
  "deps": ["T1"],
  "design_section": "3.2 内容发布",
  "acceptance": [
    "草稿保存后刷新页面仍可恢复",
    "提交审核后状态变为审核中",
    "审核拒绝时展示原因并可重新编辑"
  ],
  "role": "dev",
  "effort": "L",
  "priority": "P0"
}
```

### B. 错误示例

```json
{
  "code": "SECTION_MISSING",
  "message": "任务 T4 引用了不存在的章节",
  "details": {"task": "T4", "section": "5.1 推荐算法"},
  "hint": "改为设计规格中存在的章节，或从任务中删除该引用"
}
```

### C. UI 文案

- 空态标题：`还没有编排`
- 空态说明：`选择一份设计规格，生成任务分解与执行编排。`
- 生成中：`PM Agent 正在拆分任务…`
- 有效态：`编排可提交`
- 无效态：`编排存在 3 个问题`
- 提交确认：`将创建 14 个任务、9 个波次，是否继续？`

---

## 21. 完成定义（给评审）

当且仅当以下全部满足，本任务才算完成：

- 两份不同设计规格可在不改代码的情况下生成有效编排；
- 图/表/详情/摘要四者数据一致；
- 错误路径可见、可修、不会静默失败；
- 提交后 Run 详情页能正确展示任务与节点；
- 测试、构建、截图、README、PR 齐全；
- 三轮迭代记录与已知限制已写入日志。
