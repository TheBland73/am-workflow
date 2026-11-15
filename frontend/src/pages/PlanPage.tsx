import { useEffect, useMemo, useRef, useState } from "react";
import {
  Button, Empty, Modal, Progress, Radio, Segmented, Table, Tag, Tooltip, message,
} from "antd";
import {
  AimOutlined, CheckOutlined, DeleteOutlined, HistoryOutlined, ReloadOutlined,
  RocketOutlined, SaveOutlined, ThunderboltOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api, subscribePlan } from "../api/client";
import type {
  PlanStatus, PlanTask, PlanValidationError, TaskEffort, TaskPriority,
} from "../api/types";
import PlanGraph from "../canvas/PlanGraph";
import PlanInspector from "../panels/PlanInspector";
import PlanSourcePanel from "../panels/PlanSourcePanel";

const STATUS_LABEL: Record<PlanStatus, string> = {
  draft: "草稿",
  generating: "生成中",
  generated: "已生成",
  validating: "校验中",
  valid: "有效",
  invalid: "无效",
  editing: "编辑中",
  committed: "已提交",
  archived: "已归档",
  failed: "生成失败",
};

const EFFORT_LABEL: Record<TaskEffort, string> = { S: "S", M: "M", L: "L", XL: "XL" };
const PRIORITY_LABEL: Record<TaskPriority, string> = { P0: "P0", P1: "P1", P2: "P2" };

const COLUMNS = [
  {
    title: "UID",
    dataIndex: "task_uid",
    width: 64,
    render: (value: string) => <span className="am-mono">{value}</span>,
  },
  {
    title: "任务",
    dataIndex: "title",
    render: (value: string, task: PlanTask) => (
      <div className="plan-title-cell">
        <b>{value}</b>
        <span>{task.design_section}</span>
      </div>
    ),
  },
  {
    title: "依赖",
    dataIndex: "deps",
    width: 130,
    render: (deps: string[]) => (deps.length
      ? deps.map((dep) => <Tag key={dep} className="ef-tag">{dep}</Tag>)
      : <span className="plan-muted">—</span>),
  },
  {
    title: "角色",
    dataIndex: "role_key",
    width: 88,
    render: (value: string) => <span className="plan-role">{value.toUpperCase()}</span>,
  },
  { title: "工作量", dataIndex: "effort", width: 72, render: (value: TaskEffort) => EFFORT_LABEL[value] },
  { title: "优先级", dataIndex: "priority", width: 72, render: (value: TaskPriority) => PRIORITY_LABEL[value] },
  {
    title: "验收",
    dataIndex: "acceptance",
    width: 70,
    render: (items: string[]) => `${items.length} 条`,
  },
];

export default function PlanPage() {
  const { planId = "" } = useParams();
  const client = useQueryClient();
  const [messageApi, holder] = message.useMessage();
  const [selectedUid, setSelectedUid] = useState<string | null>(null);
  const [view, setView] = useState<"graph" | "table">("graph");
  const [genText, setGenText] = useState("");
  const [commitOpen, setCommitOpen] = useState(false);
  const [commitMode, setCommitMode] = useState<"save" | "run">("run");
  const [sourceOpen, setSourceOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const validateTimer = useRef<number | null>(null);

  const planQuery = useQuery({
    queryKey: ["plan", planId],
    queryFn: () => api.getPlan(planId),
    enabled: Boolean(planId),
    refetchInterval: (query) => {
      const status = query.state.data?.plan.status;
      return status === "generating" || status === "validating" ? 2000 : false;
    },
  });

  useEffect(() => subscribePlan(planId, (event) => {
    if (event.type === "plan_token") {
      setGenText((text) => (text + (event.payload.token ?? "")).slice(-4000));
      return;
    }
    if (event.type === "plan_generated") {
      setGenText("");
      client.invalidateQueries({ queryKey: ["plan", planId] });
      client.invalidateQueries({ queryKey: ["plans"] });
    }
    if (event.type === "plan_failed") {
      setGenText("");
      messageApi.error("生成失败，查看右侧错误信息");
      client.invalidateQueries({ queryKey: ["plan", planId] });
    }
  }), [planId, client, messageApi]);

  useEffect(() => () => {
    if (validateTimer.current) window.clearTimeout(validateTimer.current);
  }, []);

  const detail = planQuery.data;
  const plan = detail?.plan;
  const tasks = detail?.tasks ?? [];
  const snapshot = detail?.snapshot ?? null;
  const errors: PlanValidationError[] = snapshot?.validation?.errors ?? [];
  const selected = tasks.find((task) => task.task_uid === selectedUid) ?? null;
  const metrics = snapshot?.metrics ?? {};

  const waveMap = useMemo(() => snapshot?.waves ?? {}, [snapshot]);

  const generate = useMutation({
    mutationFn: () => api.generatePlan(planId),
    onSuccess: () => {
      setSelectedUid(null);
      setGenText("");
      client.invalidateQueries({ queryKey: ["plan", planId] });
    },
    onError: (error: Error) => messageApi.error(error.message),
  });

  const cancel = useMutation({
    mutationFn: () => api.cancelPlan(planId),
    onSuccess: () => client.invalidateQueries({ queryKey: ["plan", planId] }),
    onError: (error: Error) => messageApi.error(error.message),
  });

  const archive = useMutation({
    mutationFn: () => api.archivePlan(planId),
    onSuccess: () => {
      messageApi.success("计划已归档");
      client.invalidateQueries({ queryKey: ["plan", planId] });
      client.invalidateQueries({ queryKey: ["plans"] });
    },
    onError: (error: Error) => messageApi.error(error.message),
  });

  function scheduleValidate() {
    if (validateTimer.current) window.clearTimeout(validateTimer.current);
    validateTimer.current = window.setTimeout(async () => {
      try {
        await api.validatePlan(planId);
        client.invalidateQueries({ queryKey: ["plan", planId] });
      } catch (error) {
        messageApi.error((error as Error).message);
      }
    }, 400);
  }

  const commit = useMutation({
    mutationFn: (mode: "save" | "run") => api.commitPlan(planId, mode),
    onSuccess: (_result, mode) => {
      setCommitOpen(false);
      messageApi.success(
        mode === "run" ? "已提交并创建运行，可在页头打开运行详情" : "已保存为已提交计划",
      );
      client.invalidateQueries({ queryKey: ["plan", planId] });
      client.invalidateQueries({ queryKey: ["plans"] });
    },
    onError: (error: Error) => messageApi.error(error.message),
  });

  if (!plan || !detail) {
    return (
      <div className="plan-page">
        {holder}
        <div className="plan-loading">
          <Empty description={planQuery.isError ? "计划不存在或加载失败" : "加载中…"} />
        </div>
      </div>
    );
  }

  const status = plan.status;
  const valid = snapshot?.is_valid === true && status !== "committed" && status !== "archived";
  const canCommit = status === "valid" || status === "editing";
  const generating = status === "generating";
  const empty = tasks.length === 0;

  return (
    <div className="plan-page">
      {holder}

      <div className="plan-head">
        <div>
          <div className="plan-crumb">
            <Link to="/plans">编排 / PLANS</Link>
            <span>·</span>
            <span className="am-mono">{plan.id}</span>
          </div>
          <h1 className="plan-title">{plan.product_name}</h1>
          <div className="plan-sub">
            设计规格 → PM 只提议 → 程序校验与编排 → 人工确认后提交
          </div>
        </div>
        <div className="plan-head-actions">
          <Button icon={<ReloadOutlined />} onClick={() => planQuery.refetch()}>刷新</Button>
          {generating ? (
            <Button danger onClick={() => cancel.mutate()} loading={cancel.isPending}>取消生成</Button>
          ) : status !== "committed" && status !== "archived" ? (
            <Button
              icon={<ThunderboltOutlined />}
              onClick={() => generate.mutate()}
              loading={generate.isPending}
            >
              {empty ? "生成任务" : "重新生成"}
            </Button>
          ) : null}
          {canCommit ? (
            <Button
              type="primary"
              icon={<RocketOutlined />}
              onClick={() => setCommitOpen(true)}
            >
              提交计划
            </Button>
          ) : null}
          {status !== "committed" && status !== "archived" ? (
            <Button icon={<DeleteOutlined />} onClick={() => archive.mutate()} loading={archive.isPending}>
              归档
            </Button>
          ) : null}
        </div>
      </div>

      <div className="plan-statusbar">
        <span className={`status-pill is-${status}`}>{STATUS_LABEL[status] ?? status}</span>
        <span className="plan-status-note">
          {generating
            ? "PM 正在阅读设计，只输出候选任务；依赖与波次由程序计算。"
            : status === "invalid"
              ? "校验未通过，修正后会自动重新校验，无效计划不能提交。"
              : status === "committed"
                ? plan.run_id
                  ? <>已提交，<Link to={`/runs/${plan.run_id}`}>运行 {plan.run_id}</Link>。</>
                  : "已提交（仅保存）。"
                : status === "archived"
                  ? "已归档，只读。"
                  : valid
                    ? "校验通过：依赖无环，章节可追溯，可以提交。"
                    : empty
                      ? "还没有任务。点击「生成任务」，由 PM Agent 提议候选任务。"
                      : "编辑中：修改任务后 400ms 自动重新校验。"}
        </span>
        {plan.error ? <span className="plan-status-error">ERROR · {plan.error}</span> : null}
      </div>

      <div className="plan-metrics">
        <div><span>任务 / TASKS</span><b>{metrics.task_count ?? tasks.length}</b></div>
        <div><span>波次 / WAVES</span><b>{metrics.wave_count ?? "—"}</b></div>
        <div><span>最大并行 / PARALLEL</span><b>{metrics.max_parallel ?? "—"}</b></div>
        <div><span>关键路径 / CRITICAL</span><b>{metrics.critical_path_tasks ?? "—"}</b></div>
        <div><span>关键点数 / POINTS</span><b>{metrics.critical_path_points ?? "—"}</b></div>
        <div><span>总点数 / TOTAL</span><b>{metrics.total_points ?? "—"}</b></div>
      </div>

      {generating || genText ? (
        <div className="plan-stream">
          <div className="plan-stream-head">
            <b>PM STREAM</b>
            <span>{generating ? <Progress percent={100} showInfo={false} status="active" /> : "DONE"}</span>
          </div>
          <pre>{genText || "等待第一个 token…"}</pre>
        </div>
      ) : null}

      {snapshot && !snapshot.is_valid && errors.length > 0 ? (
        <div className="plan-error-list">
          <div className="plan-error-head">
            <b>校验错误 {errors.length}</b>
            <span>计划无效，禁止提交</span>
          </div>
          <div className="plan-error-grid">
            {errors.slice(0, 8).map((error) => (
              <button
                key={`${error.code}-${error.task_uid}-${error.message}`}
                type="button"
                onClick={() => error.task_uid && setSelectedUid(error.task_uid)}
              >
                <b>{error.task_uid || "PLAN"}</b>
                <span>{error.message}</span>
                {error.hint ? <small>{error.hint}</small> : null}
              </button>
            ))}
          </div>
        </div>
      ) : null}

      <div className={`plan-workbench${empty ? " is-empty" : ""}`}>
        <div className={`plan-source-wrap${sourceOpen ? " is-open" : ""}`}>
          <PlanSourcePanel plan={plan} />
        </div>

        <section className="plan-stage">
          <div className="plan-stage-head">
            <div>
              <b>编排 / ORCHESTRATION</b>
              <span>
                {snapshot ? `快照 v${snapshot.version}` : "尚未生成"}
                {snapshot ? ` · ${Object.keys(waveMap).length} 波次` : ""}
              </span>
            </div>
            <div className="plan-stage-tools">
              <Button size="small" className="plan-mobile-only" onClick={() => setSourceOpen(true)}>
                来源
              </Button>
              <Button size="small" className="plan-mobile-only" onClick={() => setInspectorOpen(true)}>
                详情
              </Button>
              <Segmented
                size="small"
                value={view}
                onChange={(value) => setView(value as "graph" | "table")}
                options={[
                  { label: "图 / GRAPH", value: "graph" },
                  { label: "表 / TABLE", value: "table" },
                ]}
              />
            </div>
          </div>

          {empty ? (
            <div className="plan-empty">
              <b>空 · 等待生成</b>
              <span>PM 会依据章节索引提出 8–40 个任务；程序负责校验、分波与关键路径。</span>
            </div>
          ) : view === "graph" ? (
            <PlanGraph
              tasks={tasks}
              snapshot={snapshot}
              selected={selectedUid}
              onSelect={setSelectedUid}
            />
          ) : (
            <div className="plan-table">
              <Table
                rowKey="task_uid"
                size="small"
                columns={COLUMNS}
                dataSource={tasks}
                pagination={false}
                rowClassName={(task) => [
                  selectedUid === task.task_uid ? "is-selected" : "",
                  errors.some((error) => error.task_uid === task.task_uid) ? "is-error" : "",
                ].join(" ").trim()}
                onRow={(task) => ({ onClick: () => setSelectedUid(task.task_uid) })}
              />
            </div>
          )}
          {view === "graph" && !empty ? (
            <span className="plan-scroll-hint">
              {Object.keys(waveMap).length} WAVES · 横向滚动 →
            </span>
          ) : null}
        </section>

        <div className={`plan-inspector-wrap${inspectorOpen ? " is-open" : ""}`}>
          <PlanInspector
            plan={plan}
            tasks={tasks}
            selected={selected}
            errors={errors}
            onSaved={(task) => {
              setSelectedUid(task.task_uid);
              client.invalidateQueries({ queryKey: ["plan", planId] });
              scheduleValidate();
            }}
            onCreated={(task) => {
              setSelectedUid(task.task_uid);
              client.invalidateQueries({ queryKey: ["plan", planId] });
              scheduleValidate();
            }}
            onDeleted={(uid) => {
              if (selectedUid === uid) setSelectedUid(null);
              client.invalidateQueries({ queryKey: ["plan", planId] });
              scheduleValidate();
            }}
          />
        </div>
      </div>

      <div
        className={`plan-drawer-backdrop${sourceOpen || inspectorOpen ? " is-open" : ""}`}
        onClick={() => { setSourceOpen(false); setInspectorOpen(false); }}
      />

      <Modal
        title="提交计划"
        open={commitOpen}
        onCancel={() => setCommitOpen(false)}
        confirmLoading={commit.isPending}
        okText={commitMode === "run" ? "提交并创建运行" : "仅保存"}
        onOk={() => commit.mutate(commitMode)}
        okButtonProps={{ icon: commitMode === "run" ? <RocketOutlined /> : <SaveOutlined /> }}
      >
        <p className="plan-commit-note">
          提交后计划将锁定为已提交。仅保存不创建运行；创建运行会写入任务、生成
          dev_* 节点与验证节点，并立即开始执行。
        </p>
        <Radio.Group
          value={commitMode}
          onChange={(event) => setCommitMode(event.target.value)}
          className="plan-commit-options"
        >
          <Radio.Button value="run">
            <RocketOutlined /> 创建运行
          </Radio.Button>
          <Radio.Button value="save">
            <SaveOutlined /> 仅保存
          </Radio.Button>
        </Radio.Group>
        <div className="plan-commit-facts">
          <span><AimOutlined /> {tasks.length} 任务 · {tasks.length + 1} 节点 · {metrics.wave_count ?? "—"} 波次</span>
          <span><HistoryOutlined /> {snapshot ? `快照 v${snapshot.version}` : "无快照"}</span>
          <span><CheckOutlined /> 预算 {(plan.budget_tokens ?? 0).toLocaleString()} tokens</span>
        </div>
        {plan.run_id ? (
          <p className="plan-commit-run">
            已有运行：<Link to={`/runs/${plan.run_id}`}>{plan.run_id}</Link>
          </p>
        ) : null}
      </Modal>

      <Tooltip title="快照与历史版本在后续迭代开放">
        <span className="plan-footnote">ORCH-01 · 编排工作台 · {tasks.length} TASKS</span>
      </Tooltip>
    </div>
  );
}
