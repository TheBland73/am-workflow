import { useEffect, useMemo, useRef, useState } from "react";
import {
  Button, Checkbox, Empty, InputNumber, Modal, Progress, Radio, Tag, Tooltip, message,
} from "antd";
import {
  AimOutlined, CheckOutlined, DeleteOutlined, FileTextOutlined, HistoryOutlined,
  ReloadOutlined, RocketOutlined, SaveOutlined, ThunderboltOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api, subscribePlan } from "../api/client";
import type {
  PlanDetail, PlanPoint, PlanStatus, PlanValidationError,
} from "../api/types";
import PlanGraph, { ROLE_META } from "../canvas/PlanGraph";
import PlanActivity from "../panels/PlanActivity";
import PlanInspector from "../panels/PlanInspector";
import PlanPhaseNav from "../panels/PlanPhaseNav";
import PlanSourcePanel from "../panels/PlanSourcePanel";
import PlanTwinGraph from "../panels/PlanTwinGraph";

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

type UndoEntry = { label: string; run: () => Promise<void> };

type ViewKey = "detail" | "monitor" | "activity" | "graph";


export default function PlanPage() {
  const { planId = "" } = useParams();
  const client = useQueryClient();
  const [messageApi, holder] = message.useMessage();
  const [selectedUid, setSelectedUid] = useState<string | null>(null);
  const [view, setView] = useState<ViewKey>("graph");
  const [genText, setGenText] = useState("");
  const [commitOpen, setCommitOpen] = useState(false);
  const [commitMode, setCommitMode] = useState<"save" | "run">("run");
  const [commitBudget, setCommitBudget] = useState<number | null>(null);
  const [commitReview, setCommitReview] = useState(true);
  const [commitAccept, setCommitAccept] = useState(true);
  const [sourceOpen, setSourceOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [fitTick, setFitTick] = useState(0);
  const [resetTick, setResetTick] = useState(0);
  const [focusSection, setFocusSection] = useState<string | null>(null);
  const validateTimer = useRef<number | null>(null);
  const layoutTimer = useRef<number | null>(null);
  const streamRef = useRef<HTMLPreElement | null>(null);
  const undoStack = useRef<UndoEntry[]>([]);
  const undoLastRef = useRef<() => void>(() => {});

  // 切换计划时清空撤销栈，避免把 A 计划的操作撤到 B 计划上。
  useEffect(() => {
    undoStack.current = [];
  }, [planId]);

  useEffect(() => {
    const node = streamRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [genText]);

  const planQuery = useQuery({
    queryKey: ["plan", planId],
    queryFn: () => api.getPlan(planId),
    enabled: Boolean(planId),
    refetchInterval: (query) => {
      const status = query.state.data?.plan.status;
      return status === "generating" || status === "validating" ? 2000 : false;
    },
  });

  const historyQuery = useQuery({
    queryKey: ["plan-snapshots", planId],
    queryFn: () => api.planSnapshots(planId),
    enabled: Boolean(planId) && historyOpen,
  });

  const restore = useMutation({
    mutationFn: (version: number) => api.restorePlanSnapshot(planId, version),
    onSuccess: (_, version) => {
      messageApi.success(`已恢复到 v${version}`);
      setHistoryOpen(false);
      client.invalidateQueries({ queryKey: ["plan-phases"] });
      refreshAfterGraphEdit();
    },
    onError: (error: Error) => messageApi.error(error.message),
  });

  useEffect(() => subscribePlan(planId, (event) => {
    if (event.type === "plan_token") {
      const chunk = event.payload.text ?? event.payload.token ?? "";
      setGenText((text) => (text + chunk).slice(-4000));
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
  const coverage = snapshot?.coverage;
  const warnings = snapshot?.validation?.warnings ?? [];
  // 画布坐标必须是稳定引用：内联 `?? {}` 会让 PlanGraph 的重建 effect 每次渲染都触发。
  const planLayout = useMemo<Record<string, PlanPoint>>(
    () => detail?.plan.layout ?? {},
    [detail?.plan.layout],
  );
  const focusUids = useMemo(() => {
    if (!focusSection || !coverage) return null;
    const section = coverage.sections.find((item) => item.key === focusSection);
    return section && section.tasks.length ? section.tasks : null;
  }, [focusSection, coverage]);

  useEffect(() => {
    if (!selectedUid && tasks.length > 0) setSelectedUid(tasks[0].task_uid);
  }, [selectedUid, tasks]);

  function selectTask(uid: string | null) {
    setSelectedUid(uid);
  }

  /** 左侧阶段导航点任务：选中并切到任务详情。 */
  function openTask(uid: string) {
    setSelectedUid(uid);
    setView("detail");
  }

  const generate = useMutation({
    mutationFn: () => api.generatePlan(planId),
    onSuccess: () => {
      setSelectedUid(null);
      setGenText("");
      client.invalidateQueries({ queryKey: ["plan", planId] });
      client.invalidateQueries({ queryKey: ["plan-phases"] });
      client.invalidateQueries({ queryKey: ["plan-activity"] });
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

  function pushUndo(entry: UndoEntry) {
    undoStack.current.push(entry);
    if (undoStack.current.length > 30) undoStack.current.shift();
  }

  /** Ctrl/Cmd+Z：撤销最近一次图编辑（移动 / 改名 / 依赖增删 / 任务详情保存）。 */
  async function undoLast() {
    if (plan?.status === "committed" || plan?.status === "archived") {
      messageApi.info("计划已提交或归档，不能撤销");
      return;
    }
    const entry = undoStack.current.pop();
    if (!entry) {
      messageApi.info("没有可撤销的操作");
      return;
    }
    try {
      await entry.run();
      messageApi.success(`已撤销：${entry.label}`);
      client.invalidateQueries({ queryKey: ["plan", planId] });
      client.invalidateQueries({ queryKey: ["plan-phases"] });
      client.invalidateQueries({ queryKey: ["plan-activity"] });
      client.invalidateQueries({ queryKey: ["plans"] });
      scheduleValidate();
    } catch (error) {
      undoStack.current.push(entry);
      messageApi.error(`撤销失败：${(error as Error).message}`);
    }
  }
  undoLastRef.current = undoLast;

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (!(event.ctrlKey || event.metaKey) || event.shiftKey || event.altKey) return;
      if (event.key.toLowerCase() !== "z") return;
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target?.isContentEditable) {
        return; // 输入框内保留浏览器自带的文字撤销
      }
      event.preventDefault();
      void undoLastRef.current();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  /** 拖拽结束后先乐观更新本地坐标，再防抖落库。 */
  function moveNodes(positions: Record<string, PlanPoint>) {
    const before = client.getQueryData<PlanDetail>(["plan", planId])?.plan.layout ?? {};
    pushUndo({
      label: "移动节点",
      run: async () => {
        if (layoutTimer.current) window.clearTimeout(layoutTimer.current);
        await api.savePlanLayout(planId, before);
        client.setQueryData<PlanDetail>(["plan", planId], (old) =>
          old ? { ...old, plan: { ...old.plan, layout: before } } : old,
        );
      },
    });
    client.setQueryData<PlanDetail>(["plan", planId], (old) =>
      old ? { ...old, plan: { ...old.plan, layout: positions } } : old,
    );
    if (layoutTimer.current) window.clearTimeout(layoutTimer.current);
    layoutTimer.current = window.setTimeout(() => {
      api
        .savePlanLayout(planId, positions)
        .catch((error: Error) => messageApi.error(`画布保存失败：${error.message}`));
    }, 700);
  }

  function refreshAfterGraphEdit() {
    client.invalidateQueries({ queryKey: ["plan", planId] });
    client.invalidateQueries({ queryKey: ["plan-phases"] });
    client.invalidateQueries({ queryKey: ["plan-activity"] });
    client.invalidateQueries({ queryKey: ["plans"] });
    scheduleValidate();
  }

  function addDependency(source: string, target: string) {
    api
      .addPlanDep(planId, source, target)
      .then(() => {
        pushUndo({
          label: `新增依赖 ${source} → ${target}`,
          run: async () => { await api.removePlanDep(planId, source, target); },
        });
        refreshAfterGraphEdit();
      })
      .catch((error: Error) => messageApi.error(error.message));
  }

  /** 画布双击改名：只提交标题，其余字段仍走任务详情。 */
  function renameTask(uid: string, title: string) {
    const previous = tasks.find((task) => task.task_uid === uid)?.title;
    return api
      .patchPlanTask(planId, uid, { title })
      .then(() => {
        if (previous !== undefined) {
          pushUndo({ label: "改名", run: async () => { await api.patchPlanTask(planId, uid, { title: previous }); } });
        }
        messageApi.success("标题已更新，正在重新校验");
        refreshAfterGraphEdit();
      })
      .catch((error: Error) => {
        messageApi.error(error.message);
        throw error;
      });
  }

  function removeDependency(source: string, target: string) {
    api
      .removePlanDep(planId, source, target)
      .then(() => {
        pushUndo({
          label: `删除依赖 ${source} → ${target}`,
          run: async () => { await api.addPlanDep(planId, source, target); },
        });
        refreshAfterGraphEdit();
      })
      .catch((error: Error) => messageApi.error(error.message));
  }

  const commit = useMutation({
    mutationFn: (mode: "save" | "run") =>
      api.commitPlan(
        planId,
        mode,
        mode === "run" ? commitBudget : null,
        mode === "run" ? commitReview : false,
        mode === "run" ? commitAccept : false,
      ),
    onSuccess: (_result, mode) => {
      setCommitOpen(false);
      messageApi.success(
        mode === "run" ? "已提交并创建运行，可在页头打开运行详情" : "已保存为已提交计划",
      );
      client.invalidateQueries({ queryKey: ["plan", planId] });
      client.invalidateQueries({ queryKey: ["plan-phases"] });
      client.invalidateQueries({ queryKey: ["plan-activity"] });
      client.invalidateQueries({ queryKey: ["plans"] });
    },
    onError: (error: Error) => messageApi.error(error.message),
  });

  const phasesQuery = useQuery({
    queryKey: ["plan-phases", planId, snapshot?.version ?? 0, tasks.length],
    queryFn: () => api.planPhases(planId),
    enabled: Boolean(planId) && tasks.length > 0,
  });

  const activityQuery = useQuery({
    queryKey: ["plan-activity", planId, snapshot?.version ?? 0],
    queryFn: () => api.planActivity(planId, 60),
    enabled: Boolean(planId) && view === "activity",
  });

  const errorUids = useMemo(
    () => new Set(errors.map((error) => error.task_uid).filter(Boolean) as string[]),
    [errors],
  );

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
          <Button icon={<FileTextOutlined />} onClick={() => setSourceOpen(true)}>设计来源</Button>
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
              onClick={() => {
                setCommitBudget(plan.budget_tokens ?? null);
                setCommitReview(true);
                setCommitAccept(true);
                setCommitOpen(true);
              }}
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

      <div className="plan-tabbar" role="tablist" aria-label="编排视图">
        <button
          type="button"
          role="tab"
          aria-selected={view === "detail"}
          className={view === "detail" ? "is-active" : ""}
          onClick={() => setView("detail")}
        >
          任务详情 <span>TASK DETAILS</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={view === "monitor"}
          className={view === "monitor" ? "is-active" : ""}
          onClick={() => setView("monitor")}
        >
          流程监控 <span>FLOW MONITOR</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={view === "activity"}
          className={view === "activity" ? "is-active" : ""}
          onClick={() => setView("activity")}
        >
          活动 <span>ACTIVITY</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={view === "graph"}
          className={view === "graph" ? "is-active" : ""}
          onClick={() => setView("graph")}
        >
          执行图 <span>DEPENDENCY</span>
        </button>
        <span className="plan-tabbar-meta">
          {phasesQuery.data
            ? `${phasesQuery.data.phase_count} 阶段 · ${phasesQuery.data.total_tasks} 任务`
            : snapshot ? `快照 v${snapshot.version}` : "尚未生成"}
        </span>
      </div>

      {generating || genText ? (
        <div className="plan-stream">
          <div className="plan-stream-head">
            <b>PM STREAM</b>
            <span>
              {genText ? <em>{genText.length.toLocaleString()} 字</em> : null}
              {generating ? <Progress percent={100} showInfo={false} status="active" /> : "DONE"}
            </span>
          </div>
          <pre ref={streamRef}>{genText || "等待第一个 token…"}</pre>
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
                onClick={() => error.task_uid && selectTask(error.task_uid)}
              >
                <b>{error.task_uid || "PLAN"}</b>
                <span>{error.message}</span>
                {error.hint ? <small>{error.hint}</small> : null}
              </button>
            ))}
          </div>
        </div>
      ) : null}

      <div className={`plan-station${empty ? " is-empty" : ""}`}>
        <PlanPhaseNav
          phases={phasesQuery.data}
          loading={phasesQuery.isLoading}
          selectedUid={selectedUid}
          errorUids={errorUids}
          onSelect={openTask}
        />

        <section className="plan-content">
          <div className="plan-content-head">
            <div>
              <b>编排工作台 / WORKSTATION</b>
              <span>
                {snapshot
                  ? `快照 v${snapshot.version} · ${phasesQuery.data?.phase_count ?? 0} 阶段 · ${tasks.length} 任务`
                  : "尚未生成"}
              </span>
            </div>
            <div className="plan-stage-tools">
              {view === "graph" ? (
                <>
                  <Button size="small" className="plan-detail-btn" onClick={() => setFitTick((value) => value + 1)}>
                    全览
                  </Button>
                  <Button size="small" className="plan-detail-btn" onClick={() => setResetTick((value) => value + 1)}>
                    重置布局
                  </Button>
                </>
              ) : null}
              <Button
                size="small"
                className="plan-detail-btn"
                icon={<HistoryOutlined />}
                onClick={() => setHistoryOpen(true)}
              >
                历史
              </Button>
              <span className="plan-stage-mode">
                {view === "graph"
                  ? "DEPENDENCY GRAPH"
                  : view === "monitor"
                    ? "FLOW MONITOR"
                    : view === "activity"
                      ? "ACTIVITY"
                      : "TASK DETAILS"}
              </span>
            </div>
          </div>

          <div className={`plan-content-body is-${view}`}>
            {empty ? (
              <div className="plan-empty">
                <b>空 · 等待生成</b>
                <span>PM 会依据章节索引提出 8–40 个任务；程序负责校验、分阶段与关键路径。</span>
              </div>
            ) : view === "detail" ? (
              <div className="plan-detail-pane">
                <PlanInspector
                  plan={plan}
                  tasks={tasks}
                  selected={selected}
                  errors={errors}
                  onSaved={(task) => {
                    const previous = tasks.find((item) => item.task_uid === task.task_uid);
                    if (previous) {
                      pushUndo({
                        label: "保存任务详情",
                        run: async () => {
                          await api.patchPlanTask(planId, previous.task_uid, {
                            title: previous.title,
                            description: previous.description,
                            deps: previous.deps,
                            design_section: previous.design_section,
                            acceptance: previous.acceptance,
                            role_key: previous.role_key,
                            effort: previous.effort,
                            priority: previous.priority,
                          });
                        },
                      });
                    }
                    setSelectedUid(task.task_uid);
                    client.invalidateQueries({ queryKey: ["plan", planId] });
                    client.invalidateQueries({ queryKey: ["plan-phases"] });
                    scheduleValidate();
                  }}
                  onCreated={(task) => {
                    pushUndo({
                      label: "新建任务",
                      run: () => api.deletePlanTask(planId, task.task_uid).then(() => undefined),
                    });
                    setSelectedUid(task.task_uid);
                    client.invalidateQueries({ queryKey: ["plan", planId] });
                    client.invalidateQueries({ queryKey: ["plan-phases"] });
                    scheduleValidate();
                  }}
                  onDeleted={(uid) => {
                    if (selectedUid === uid) setSelectedUid(null);
                    client.invalidateQueries({ queryKey: ["plan", planId] });
                    client.invalidateQueries({ queryKey: ["plan-phases"] });
                    scheduleValidate();
                  }}
                  locked={plan.status === "committed" || plan.status === "archived"}
                />
              </div>
            ) : view === "monitor" ? (
              <PlanTwinGraph
                phases={phasesQuery.data}
                selectedUid={selectedUid}
                onSelect={openTask}
              />
            ) : view === "activity" ? (
              <PlanActivity items={activityQuery.data?.items} loading={activityQuery.isLoading} />
            ) : (
              <div className="plan-graph-pane">
                {coverage && coverage.total > 0 ? (
                  <div className="plan-coverage">
                    <div className="plan-coverage-head">
                      <b>设计覆盖 / COVERAGE</b>
                      <span>{coverage.covered}/{coverage.total} 章节有任务承接</span>
                      {coverage.uncovered.length > 0 ? (
                        <em>{coverage.uncovered.length} 个章节还没有任务{warnings.length ? ` · ${warnings.length} 条提示` : ""}</em>
                      ) : (
                        <em className="is-ok">全部章节已承接</em>
                      )}
                    </div>
                    <div className="plan-coverage-list">
                      {coverage.sections.map((section) => {
                        const focused = focusSection === section.key;
                        const lost = !section.tasks.length;
                        return (
                          <button
                            key={`${section.key}-${section.line}`}
                            type="button"
                            className={[
                              "plan-coverage-chip",
                              lost ? "is-open" : "",
                              focused ? "is-focus" : "",
                            ].join(" ").trim()}
                            title={lost
                              ? `${section.title} · 暂无任务承接`
                              : `${section.title} · ${section.tasks.join(" / ")}`}
                            onClick={() => {
                              if (lost) {
                                messageApi.info("该章节还没有任务：在任务详情里新增任务，并把「设计章节」指向它");
                                return;
                              }
                              setFocusSection(focused ? null : section.key);
                              if (!focused && section.tasks[0]) setSelectedUid(section.tasks[0]);
                            }}
                          >
                            <span>{section.title}</span>
                            <b>{lost ? "—" : section.tasks.length}</b>
                          </button>
                        );
                      })}
                    </div>
                  </div>
                ) : null}

                {snapshot?.validation && !snapshot.validation.ok && !Object.keys(snapshot.waves ?? {}).length ? (
                  <div className="plan-graph-blocked">
                    <b>依赖关系无效 · 阶段无法计算</b>
                    <span>先修复上方校验错误；重新校验通过后会自动绘制依赖图。仍可在「任务详情」里逐条编辑。</span>
                  </div>
                ) : (
                  <>
                    <PlanGraph
                      tasks={tasks}
                      snapshot={snapshot}
                      layout={planLayout}
                      selected={selectedUid}
                      focusUids={focusUids}
                      onSelect={selectTask}
                      onMoveNodes={moveNodes}
                      onAddDep={addDependency}
                      onRemoveDep={removeDependency}
                      onRename={renameTask}
                      fitToken={fitTick}
                      resetToken={resetTick}
                    />
                    <div className="plan-graph-legend">
                      <span className="plan-canvas-hint">拖动节点自动保存 · 右侧手柄拖到目标左侧新增依赖 · 点击连线删除 · 双击改名 · Ctrl+Z 撤销</span>
                      <span><i className="plan-key is-normal" />普通依赖</span>
                      <span><i className="plan-key is-critical" />关键路径</span>
                      <span><i className="plan-key is-handoff" />跨角色交接</span>
                      <span><i className="plan-key is-error" />校验错误</span>
                      <span className="plan-legend-roles">
                        {Object.entries(ROLE_META).map(([key, meta]) => (
                          <em key={key}><i style={{ background: meta.color }} />{meta.label}</em>
                        ))}
                      </span>
                      <span className="plan-graph-foot">
                        {phasesQuery.data?.phase_count ?? 0} PHASES · 选中节点看链路 · 点章节聚焦
                      </span>
                    </div>
                  </>
                )}
              </div>
            )}
          </div>
        </section>
      </div>

      <Modal
        title="设计来源"
        open={sourceOpen}
        footer={null}
        width={760}
        onCancel={() => setSourceOpen(false)}
      >
        <div className="plan-source-modal">
          <PlanSourcePanel plan={plan} />
        </div>
      </Modal>

      <Modal
        title="快照历史"
        open={historyOpen}
        footer={null}
        width={540}
        onCancel={() => setHistoryOpen(false)}
      >
        <p className="plan-commit-note">
          每次校验会留一份快照。恢复会把任务与依赖回滚到该版本，并重新校验。
        </p>
        <div className="plan-history">
          {historyQuery.isLoading ? <p className="plan-muted">读取中…</p> : null}
          {(historyQuery.data?.items ?? []).map((item) => {
            const current = snapshot?.version === item.version;
            const passed = (item.validation as { ok?: boolean } | null)?.ok !== false;
            return (
              <div className="plan-history-row" key={item.version}>
                <b>v{item.version}</b>
                <span>
                  {item.metrics?.task_count ?? "—"} 任务 · {item.metrics?.wave_count ?? "—"} 波
                </span>
                <em
                  className={current ? "is-ok" : passed ? "is-meta" : "is-bad"}
                  title={current ? "当前快照" : passed ? "历史快照（校验通过）" : "该版本校验未通过"}
                >
                  {current ? "当前" : passed ? "历史" : "未通过"}
                </em>
                <em>{item.created_at ? new Date(item.created_at).toLocaleString("zh-CN") : "—"}</em>
                <Button
                  size="small"
                  disabled={current}
                  loading={restore.isPending && restore.variables === item.version}
                  onClick={() => restore.mutate(item.version)}
                >
                  {current ? "当前" : "恢复"}
                </Button>
              </div>
            );
          })}
          {!historyQuery.isLoading && !historyQuery.data?.items.length ? (
            <p className="plan-muted">还没有快照；校验通过后会生成版本。</p>
          ) : null}
        </div>
      </Modal>

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
          提交后计划将锁定为已提交。仅保存不创建运行；创建运行会按依赖写入
          dev_* 节点（同一波并行、跨波串行），末尾追加一个 verify 节点；
          可选代码评审 Agent 与验收人工门，门通过后才结束运行。
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
        <div className="plan-commit-budget">
          <span>运行预算</span>
          <InputNumber
            min={10000}
            max={20000000}
            step={100000}
            value={commitBudget ?? undefined}
            disabled={commitMode !== "run"}
            onChange={(value) => setCommitBudget(typeof value === "number" ? value : null)}
          />
          <em>tokens · 留空用系统默认</em>
        </div>
        <div className="plan-commit-flags">
          <Checkbox
            checked={commitReview}
            disabled={commitMode !== "run"}
            onChange={(event) => setCommitReview(event.target.checked)}
          >
            追加代码评审 Agent
          </Checkbox>
          <Checkbox
            checked={commitAccept}
            disabled={commitMode !== "run"}
            onChange={(event) => setCommitAccept(event.target.checked)}
          >
            追加验收人工门
          </Checkbox>
          <em>评审发现阻塞问题时自动退回对应任务，最多返工 2 轮</em>
        </div>
        <div className="plan-commit-facts">
          <span>
            <AimOutlined /> {tasks.length} 任务 ·{" "}
            {commitMode === "run"
              ? tasks.length + 1 + (commitReview ? 1 : 0) + (commitAccept ? 1 : 0)
              : 0}{" "}
            节点 · {metrics.wave_count ?? "—"} 波次
          </span>
          <span><HistoryOutlined /> {snapshot ? `快照 v${snapshot.version}` : "无快照"}</span>
          <span><CheckOutlined /> 预算 {(commitBudget ?? plan.budget_tokens ?? 0).toLocaleString()} tokens</span>
        </div>
        {plan.run_id ? (
          <p className="plan-commit-run">
            已有运行：<Link to={`/runs/${plan.run_id}`}>{plan.run_id}</Link>
          </p>
        ) : null}
      </Modal>

      <Tooltip title="每次校验生成快照，可回滚到历史版本">
        <span className="plan-footnote">ORCH-01 · 编排工作台 · {tasks.length} TASKS</span>
      </Tooltip>
    </div>
  );
}
