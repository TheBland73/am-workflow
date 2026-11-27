import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Button, Checkbox, Drawer, Empty, Input, Modal, Spin, Table, Tag, message,
} from "antd";
import { Link, useParams } from "react-router-dom";
import { api, subscribeRun } from "../api/client";
import type {
  ArtifactRecord, Gate, Node, Snapshot, StreamEvent,
} from "../api/types";
import Dial from "../canvas/Dial";
import ExecutionMap from "../canvas/ExecutionMap";
import { heldKeys } from "../canvas/graphLayout";
import GateConsole from "../panels/GateConsole";
import NodeInspector from "../panels/NodeInspector";
import type { NodeActionRequest } from "../panels/NodeInspector";
import {
  decidedByLabel, isSameFamilyDecision, phaseLabel, SAME_FAMILY_HINT,
  statusLabel as pointStatusLabel, verdictLabel,
} from "../panels/meetingLabels";

const ARTIFACT_LABELS: Record<string, string> = {
  idea: "想法", discussion: "会议纪要", proposal: "方案", review: "评审与确认报告",
  requirement: "需求", design: "设计", task: "任务", test: "测试", changelog: "交付日志",
};
const ARTIFACT_KINDS = Object.keys(ARTIFACT_LABELS);
const SEV_LABEL: Record<string, string> = { blocker: "阻塞", major: "重要", minor: "建议" };

const MEETING_STATUS_LABEL: Record<string, string> = {
  active: "进行中", suspended: "已挂起", waiting_rework: "等待返工",
  completed: "已完成", abandoned: "已放弃",
};

/** 这些事件会改变会议详情（步骤、发言、确认点状态），收到后失效会议查询。 */
const MEETING_EVENTS = new Set([
  "meeting_started", "meeting_phase", "meeting_turn_finished", "meeting_ledger_updated",
  "meeting_suspended", "meeting_resumed", "meeting_finished",
  "meeting_intervention_injected", "checkpoint_decided", "gate_decided",
]);
// 事件抽屉里 node_id 改为显示 node_key，比数字列宽：仅本抽屉内联放宽，不改全局 .log-line。
const LOG_LINE_STYLE = { gridTemplateColumns: "58px 74px minmax(0, 1fr)" };

const STATUS_LABELS: Record<string, string> = {
  created: "已创建", running: "运行中", waiting_human: "等待人工",
  finished: "已完成", failed: "失败", stopped: "已停止",
  pending: "等待", ready: "就绪", done: "完成", skipped: "跳过",
  failed_node: "失败",
};

function statusLabel(status: string) {
  return STATUS_LABELS[status] ?? status;
}

function fmtTime(value: string | null | undefined) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return `${date.getFullYear()}.${String(date.getMonth() + 1).padStart(2, "0")}.${String(date.getDate()).padStart(2, "0")} ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

function durationText(start: string | null, end: string | null) {
  if (!start) return "—";
  const from = new Date(start).getTime();
  const to = end ? new Date(end).getTime() : Date.now();
  if (Number.isNaN(from) || Number.isNaN(to)) return "—";
  const total = Math.max(0, Math.floor((to - from) / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  if (m >= 60) {
    const h = Math.floor(m / 60);
    return `${String(h).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function eventText(event: StreamEvent) {
  const payload = event.payload ?? {};
  const key = String(payload.node_key ?? "");
  switch (event.type) {
    case "run_created": return "运行已创建";
    case "run_finished": return "运行完成";
    case "run_error": return `运行失败：${String(payload.message ?? "")}`;
    case "run_recovered": return "进程恢复，重新排队";
    case "phase_entered": return `进入阶段 ${String(payload.phase ?? "")}`;
    case "node_started": return `${key} 开始`;
    case "node_finished": return `${key} 完成`;
    case "node_failed": return `${key} 失败：${String(payload.message ?? "")}`;
    case "node_retried": return `${key} 已重试`;
    case "node_retry_scheduled": return `${key} 将在 ${String(payload.delay_s ?? "")}s 后重试（第 ${String(payload.next_attempt ?? "")} 次）`;
    case "node_skipped": return `${key} 已跳过${payload.reason ? `：${String(payload.reason).slice(0, 60)}` : ""}`;
    case "node_reset": {
      const reset = Array.isArray(payload.reset_nodes) ? payload.reset_nodes.length : 0;
      return `${key} 已重置${reset ? `（下游 ${reset} 个）` : ""}`;
    }
    case "node_feedback_injected": return `${key} 已注入反馈${payload.feedback ? `：${String(payload.feedback).slice(0, 60)}` : ""}`;
    case "nodes_reset": return `重置 ${Array.isArray(payload.nodes) ? payload.nodes.length : 0} 个节点`;
    case "gate_requested": return `等待审批：${key}`;
    case "gate_decided": return `审批 ${String(payload.action ?? "")}：${key}`;
    case "artifact_written": return `写入工件 ${String(payload.kind ?? "")} v${String(payload.version ?? "")}`;
    case "task_created": return `新增任务 ${String(payload.task_uid ?? "")}`;
    case "task_updated": return `更新任务 ${String(payload.task_uid ?? "")}`;
    case "node_tool_call": return `${key} 调用 ${String(payload.name ?? "")}`;
    case "node_tool_result": return `${key} 工具返回${payload.is_error ? "（错误）" : ""}`;
    case "memory_written": return `写入记忆 ${String(payload.subject ?? "")}`;
    // 会议式前半段（详设 §12.5）
    case "meeting_started": return "会议开始";
    case "meeting_phase":
      return `进入：${phaseLabel(String(payload.phase ?? ""))}（第 ${String(payload.round ?? "")} 轮）`;
    case "meeting_turn_started": return `${String(payload.speaker ?? "")} 开始发言`;
    case "meeting_turn_finished":
      return `${String(payload.speaker ?? "")}：${String(payload.summary ?? "")}`;
    case "meeting_ledger_updated":
      return payload.roster
        ? "参会名单已更新"
        : `账本更新（第 ${String(payload.version ?? "")} 版）`;
    case "meeting_suspended": return "会议在确认点挂起";
    case "meeting_resumed": return "会议续开";
    case "meeting_intervention_queued": return "插话已排队";
    case "meeting_intervention_injected": return "插话已注入";
    case "meeting_finished": return "会议完成";
    case "checkpoint_decided": {
      const text = `确认点已由${decidedByLabel(String(payload.decided_by ?? ""))}决定`;
      return isSameFamilyDecision(payload.decision)
        ? `${text}（${SAME_FAMILY_HINT}）`
        : text;
    }
    case "review_round_started": return `评审第 ${String(payload.round ?? "")} 轮开始`;
    case "review_verdict": return `结论：${verdictLabel(String(payload.verdict ?? ""))}`;
    case "context_compacted":
      return `上下文已压缩（${String(payload.before_tokens ?? "")} → ${String(payload.after_tokens ?? "")} token）`;
    case "context_prefix_changed": {
      const segments = Array.isArray(payload.segments) ? payload.segments.map(String) : [];
      return `冻结的内容被改动：${segments.join("、") || "—"}`;
    }
    case "node_rerouted": {
      const targets = Array.isArray(payload.targets) ? payload.targets.map(String) : [];
      return `定点回流到 ${targets.join("、") || "—"}（第 ${String(payload.round ?? "")} 轮）`;
    }
    default: return event.type;
  }
}

export default function RunPage() {
  const { runId = "" } = useParams();
  const client = useQueryClient();
  const [messageApi, holder] = message.useMessage();
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const seen = useRef(new Set<number>());
  const selectedNodeIdRef = useRef<number | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<number | null>(null);
  const [logOpen, setLogOpen] = useState(false);
  const [filterNodeId, setFilterNodeId] = useState<number | null>(null);
  const [artifact, setArtifact] = useState<{ kind: string; body: string; original: string; version: number } | null>(null);
  const [savingArtifact, setSavingArtifact] = useState(false);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [reworkUids, setReworkUids] = useState<string[]>([]);
  const [reworkNote, setReworkNote] = useState("");
  const [meetingRef, setMeetingRef] = useState<{ nodeId: number; meetingId: number } | null>(null);
  const [meetingLoading, setMeetingLoading] = useState(false);
  const [intervention, setIntervention] = useState("");

  useEffect(() => {
    selectedNodeIdRef.current = selectedNodeId;
  }, [selectedNodeId]);

  const snapshotQuery = useQuery({
    queryKey: ["snapshot", runId],
    queryFn: () => api.getRun(runId),
    enabled: Boolean(runId),
    refetchInterval: (query) => {
      const status = query.state.data?.run.status;
      return status === "finished" || status === "failed" || status === "stopped" ? false : 8000;
    },
  });

  const metricsQuery = useQuery({
    queryKey: ["metrics", runId],
    queryFn: () => api.metrics(runId),
    enabled: Boolean(runId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "finished" || status === "failed" || status === "stopped" ? false : 8000;
    },
  });

  const reviewQuery = useQuery({
    queryKey: ["review", runId],
    queryFn: () => api.runReview(runId),
    enabled: Boolean(runId),
    refetchInterval: (query) => {
      const status = query.state.data?.run_status;
      return status === "finished" || status === "failed" || status === "stopped" ? false : 8000;
    },
  });

  // 节点详情查询：queryKey 精确到 nodeId，SSE 只按需 invalidate 该 key（不再手工 fetch）。
  const nodeQuery = useQuery({
    queryKey: ["node", runId, selectedNodeId],
    queryFn: () => api.getNode(selectedNodeId as number, { messages: 40, events: 20 }),
    enabled: selectedNodeId != null && Boolean(runId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "pending" || status === "running" || status === "waiting_human"
        ? 8000
        : false;
    },
  });

  const review = reviewQuery.data;

  const meetingDetailQuery = useQuery({
    queryKey: ["meeting", runId, meetingRef?.meetingId],
    queryFn: () => api.meetingDetail(runId, meetingRef!.meetingId),
    enabled: Boolean(meetingRef),
    refetchInterval: (query) => {
      const status = query.state.data?.meeting.status;
      return status === "active" || status === "waiting_rework" ? 5000 : false;
    },
  });
  const meetingTurnsQuery = useQuery({
    queryKey: ["meeting-turns", runId, meetingRef?.meetingId],
    // 后端按序号升序取"前 N 条"：要最近的 50 条，从总条数倒推起点
    queryFn: () => api.meetingTurns(
      runId, meetingRef!.meetingId,
      Math.max(0, (meetingDetailQuery.data?.turn_count ?? 0) - 50), 50,
    ),
    enabled: Boolean(meetingRef) && meetingDetailQuery.isSuccess,
    refetchInterval: () => {
      const status = meetingDetailQuery.data?.meeting.status;
      return status === "active" || status === "waiting_rework" ? 5000 : false;
    },
  });

  const intervene = useMutation({
    mutationFn: () => api.intervene(runId, meetingRef!.meetingId, intervention.trim()),
    onSuccess: () => {
      messageApi.success("插话已排队，会议会在下一步处理");
      setIntervention("");
      client.invalidateQueries({ queryKey: ["meeting", runId, meetingRef?.meetingId] });
      client.invalidateQueries({ queryKey: ["meeting-turns", runId, meetingRef?.meetingId] });
    },
    onError: (error: Error) => messageApi.error(error.message),
  });

  useEffect(() => {
    // 只在评审阻塞时预选要返工的任务；通过后清空，避免把正常评审误显示成待返工。
    setReworkUids(review?.verdict === "block" ? review?.targets ?? [] : []);
  }, [review?.reviewed_at, review?.targets, review?.verdict]);

  const rework = useMutation({
    mutationFn: () => api.reworkReview(runId, reworkUids, reworkNote.trim() || undefined),
    onSuccess: () => {
      messageApi.success("已退回选中任务，评审将在验证通过后重跑");
      setReworkNote("");
      client.invalidateQueries({ queryKey: ["snapshot", runId] });
      client.invalidateQueries({ queryKey: ["metrics", runId] });
      client.invalidateQueries({ queryKey: ["review", runId] });
    },
    onError: (error: Error) => messageApi.error(error.message),
  });

  useEffect(() => {
    if (!runId) return;
    setEvents([]);
    seen.current = new Set();
    const isTerminal = (type: string) =>
      type === "run_finished" || type === "run_error" || type === "run_stopped";
    const unsubscribe = subscribeRun(runId, (event) => {
      if (event.seq != null) {
        if (seen.current.has(event.seq)) return;
        seen.current.add(event.seq);
      }
      setEvents((previous) => [...previous.slice(-399), event]);
      const selected = selectedNodeIdRef.current;
      if (isTerminal(event.type)) {
        // 终态事件：整页全量刷新（含已打开的节点详情）。
        if (selected != null) {
          client.invalidateQueries({ queryKey: ["node", runId, selected] });
        }
        client.invalidateQueries({ queryKey: ["snapshot", runId] });
        client.invalidateQueries({ queryKey: ["metrics", runId] });
        client.invalidateQueries({ queryKey: ["review", runId] });
        if (MEETING_EVENTS.has(event.type)) {
          client.invalidateQueries({ queryKey: ["meeting", runId] });
          client.invalidateQueries({ queryKey: ["meeting-turns", runId] });
        }
        return;
      }
      if (event.type === "node_token") return;
      if (selected != null && event.node_id === selected) {
        // 选中节点的事件精准刷新详情，但快照/指标/评审仍要刷新：
        // 否则等待人工门的横幅、阶段进度会停留在旧状态。
        client.invalidateQueries({ queryKey: ["node", runId, selected] });
      }
      client.invalidateQueries({ queryKey: ["snapshot", runId] });
      client.invalidateQueries({ queryKey: ["metrics", runId] });
      client.invalidateQueries({ queryKey: ["review", runId] });
      if (MEETING_EVENTS.has(event.type)) {
        client.invalidateQueries({ queryKey: ["meeting", runId] });
        client.invalidateQueries({ queryKey: ["meeting-turns", runId] });
      }
    }, 0);
    return unsubscribe;
  }, [client, runId]);

  // Esc 关闭节点检查器；输入框/文本域内不拦截，Modal 内的 Esc 交给 antd 处理。
  useEffect(() => {
    if (selectedNodeId == null) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      // 事件抽屉盖在检查器之上：Esc 先交给抽屉自己关闭，不连带关闭检查器。
      if (logOpen) return;
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName?.toLowerCase();
      if (tag === "input" || tag === "textarea" || target?.isContentEditable) return;
      if (target?.closest(".ant-modal")) return;
      setSelectedNodeId(null);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [logOpen, selectedNodeId]);

  const snapshot: Snapshot | undefined = snapshotQuery.data;
  const metrics = metricsQuery.data;
  const phases = snapshot?.workflow.phases ?? [];
  const nodes = snapshot?.nodes ?? [];
  const openGate = snapshot?.gates.find((gate) => gate.status === "open") ?? null;
  const held = useMemo(
    () => heldKeys(nodes, openGate ?? null),
    [nodes, openGate],
  );
  const doneCount = nodes.filter((node) => node.status === "done" || node.status === "skipped").length;

  const dialSections = useMemo(() => phases.map((phase) => {
    const phaseNodes = nodes.filter((node) => node.phase === phase);
    const done = phaseNodes.filter((node) => node.status === "done" || node.status === "skipped").length;
    const waiting = phaseNodes.some((node) => node.status === "waiting_human");
    return {
      zh: snapshot?.phase_names[phase] ?? phase,
      en: phase.toUpperCase(),
      big: `${done}/${phaseNodes.length}`,
      unit: "NODES",
      sub: waiting ? "WAITING / 人工裁决" : `${phaseNodes.length - done} 待执行`,
      here: snapshot?.run.current_phase === phase,
      alert: waiting,
    };
  }), [nodes, phases, snapshot?.phase_names, snapshot?.run.current_phase]);

  const usage = snapshot?.usage;
  const budgetShare = usage && usage.budget > 0 ? usage.total / usage.budget : 0;

  const ledgerRows = useMemo(() => {
    const active = nodes.filter((node) =>
      node.attempt || ["done", "waiting_human", "failed", "running"].includes(node.status));
    const numbered = nodes.filter((node) => node.type !== "HUMAN_GATE");
    const gates = nodes.filter((node) => node.type === "HUMAN_GATE");
    return active.slice(-5).map((node) => ({
      node,
      code: node.type === "HUMAN_GATE"
        ? `G${gates.findIndex((item) => item.id === node.id) + 1}`
        : String(numbered.findIndex((item) => item.id === node.id) + 1).padStart(2, "0"),
    }));
  }, [nodes]);

  const openArtifact = async (record: ArtifactRecord) => {
    try {
      const detail = await api.getArtifact(runId, record.kind);
      setArtifact({ kind: record.kind, body: detail.body, original: detail.body, version: record.version });
    } catch (error) {
      messageApi.error((error as Error).message);
    }
  };

  const saveArtifact = async () => {
    if (!artifact) return;
    setSavingArtifact(true);
    try {
      await api.patchArtifact(runId, artifact.kind, artifact.body, "reviewed");
      messageApi.success("已保存为新版本（reviewed）");
      setArtifact(null);
      client.invalidateQueries({ queryKey: ["snapshot", runId] });
    } catch (error) {
      messageApi.error((error as Error).message);
    } finally {
      setSavingArtifact(false);
    }
  };

  // 选中节点：再次点击同一节点取消；详情由 nodeQuery 按 selectedNodeId 拉取。
  // 会议节点同时打开会议详情抽屉（账本、参会者、发言、插话）。
  const openNode = async (node: Node) => {
    setSelectedNodeId((current) => (current === node.id ? null : node.id));
    if (node.type !== "MEETING") return;
    setMeetingLoading(true);
    try {
      const { items } = await api.listMeetings(runId);
      const mine = [...items].reverse().find((item) => item.node_id === node.id);
      if (!mine) {
        messageApi.error("这个会议节点还没有会议记录");
        return;
      }
      setIntervention("");
      setMeetingRef({ nodeId: node.id, meetingId: mine.id });
    } catch (error) {
      messageApi.error((error as Error).message);
    } finally {
      setMeetingLoading(false);
    }
  };

  // 节点动作：短事务端点由后端实现，成功后刷新快照/指标/该节点详情（契约 §8）。
  const runNodeAction = async (request: NodeActionRequest) => {
    if (selectedNodeId == null) return;
    const nodeId = selectedNodeId;
    try {
      switch (request.kind) {
        case "retry":
          await api.retryNode(nodeId, {
            feedback: request.feedback,
            reset_downstream: request.resetDownstream,
            expected_status: request.expectedStatus,
          });
          break;
        case "reset":
          await api.resetNode(nodeId, {
            reset_downstream: request.resetDownstream,
            expected_status: request.expectedStatus,
          });
          break;
        case "skip":
          await api.skipNode(nodeId, {
            reason: request.reason,
            cascade_downstream: request.cascadeDownstream,
            expected_status: request.expectedStatus,
          });
          break;
        case "feedback":
          if (!request.feedback) throw new Error("反馈内容不能为空");
          await api.injectFeedback(nodeId, request.feedback);
          break;
      }
    } catch (error) {
      // 失败（如 409 status_conflict）也刷新一次，让面板回到服务端真实状态再重试。
      client.invalidateQueries({ queryKey: ["snapshot", runId] });
      client.invalidateQueries({ queryKey: ["node", runId, nodeId] });
      throw error;
    }
    client.invalidateQueries({ queryKey: ["snapshot", runId] });
    client.invalidateQueries({ queryKey: ["metrics", runId] });
    client.invalidateQueries({ queryKey: ["node", runId, nodeId] });
  };

  const stopRun = async () => {
    try {
      await api.stopRun(runId);
      client.invalidateQueries({ queryKey: ["snapshot", runId] });
    } catch (error) {
      messageApi.error((error as Error).message);
    }
  };

  if (snapshotQuery.isLoading) {
    return <div className="am-loading"><Spin /></div>;
  }
  if (snapshotQuery.error || !snapshot) {
    return (
      <div className="ef-panel" style={{ marginTop: 24 }}>
        <h2>运行读取失败</h2>
        <p>{(snapshotQuery.error as Error)?.message}</p>
        <Link to="/runs">返回运行列表</Link>
      </div>
    );
  }

  const { run, stats } = snapshot;
  const selectedNode = nodes.find((node) => node.id === selectedNodeId) ?? null;
  const nodeKeyById = new Map(nodes.map((node) => [node.id, node.node_key]));
  // 事件抽屉的节点过滤：数值 node_id 渲染成 node_key（契约 F7）。
  const visibleLogEvents = filterNodeId == null
    ? events
    : events.filter((event) => event.node_id === filterNodeId);
  const logFilterKey = filterNodeId == null
    ? null
    : nodeKeyById.get(filterNodeId) ?? `#${filterNodeId}`;
  const activeArtifacts = snapshot.artifacts.filter((item) => item.status === "active");
  const pendingKinds = ARTIFACT_KINDS.filter(
    (kind) => !activeArtifacts.some((item) => item.kind === kind),
  );
  const gateNode = nodes.find((node) => node.id === openGate?.node_id);
  const gateArtifactKind = gateNode ? String(gateNode.config.artifact ?? "") : "";

  return (
    <div className="run-page">
      {holder}

      <header className="ef-head">
        <div className="ef-eyebrow">
          <Link to="/runs">RUNS</Link> / {run.id} / {statusLabel(run.status)}
        </div>
        <div className="head-row">
          <h1>{run.idea}</h1>
          <div className="head-actions">
            <Button size="small" onClick={() => snapshotQuery.refetch()}>刷新</Button>
            {(run.status === "running" || run.status === "waiting_human") && (
              <Button size="small" danger onClick={stopRun}>停止</Button>
            )}
            <Link className="ef-btn" to="/runs">新建运行</Link>
          </div>
        </div>
        <div className="ef-bar-title"><span>RUN CONTROL</span></div>
        <div className="runfacts">
          <span className={`status-pill is-${run.status === "waiting_human" ? "waiting" : run.status}`}>
            {statusLabel(run.status)}
          </span>
          <div className="ef-facts">
            <span>创建 <b>{fmtTime(run.created_at)}</b></span>
            <span>预算 <b>{run.budget_tokens.toLocaleString()}</b></span>
            <span>进度 <b>{doneCount}/{nodes.length}</b></span>
            <span>阶段 <b>{snapshot.phase_names[run.current_phase ?? ""] ?? "—"}</b></span>
          </div>
        </div>
      </header>

      <section className="board">
        <div className="board-col board-left">
        <div className="l1 ef-panel">
          <h2><span className="ef-eyebrow">STATE</span>运行状态</h2>
          <div className="ef-figs">
            <b>{String(nodes.length).padStart(2, "0")}</b><span>总节点</span>
            <b>{String(doneCount).padStart(2, "0")}</b><span>已完成</span>
            <b>{String(held.size).padStart(2, "0")}</b><span>被暂停</span>
          </div>
          <ul className="ef-ledger">
            <li><span>当前阶段</span><b>{snapshot.phase_names[run.current_phase ?? ""] ?? "—"}</b></li>
            <li><span>已用 Token</span><b>{usage?.total.toLocaleString() ?? "—"}</b></li>
            {metrics && (
              <>
                <li><span>重试 / 尝试</span><b>{metrics.execution.retries} / {metrics.execution.attempts}</b></li>
                <li><span>单节点 P95</span><b>{(metrics.execution.p95_attempt_ms / 1000).toFixed(1)}s</b></li>
                <li><span>人工门等待</span><b>{(metrics.gates.open_wait_ms / 1000).toFixed(0)}s</b></li>
              </>
            )}
          </ul>
        </div>

        <div className="l2 ef-panel">
          <h2><span className="ef-eyebrow">GATE</span>当前门</h2>
          {openGate && gateNode ? (
            <>
              <p className="gate-name">{String(gateNode.config.title ?? gateNode.name)}</p>
              <ul className="ef-ledger">
                <li><span>等待自</span><b>{fmtTime(openGate.created_at)}</b></li>
                <li><span>下游暂停</span><b>{held.size} 节点</b></li>
                <li><span>工件版本</span><b>v{activeArtifacts.find((item) => item.kind === gateArtifactKind)?.version ?? "—"}</b></li>
              </ul>
              <p className="gate-hint">在右栏「审批」卡中裁决。</p>
            </>
          ) : (
            <p className="gate-hint">
              {run.status === "finished" ? "所有人工门已裁决，运行完成。" : "当前没有待审批的人工门。"}
            </p>
          )}
        </div>

        <div className="l3 ef-panel">
          <h2><span className="ef-eyebrow">BUDGET</span>预算</h2>
          <div className="ef-figs">
            <b>{usage ? `${Math.round(budgetShare * 100)}%` : "—"}</b><span>已用</span>
            <b>{usage ? `${(usage.remaining / 1000).toFixed(0)}K` : "—"}</b><span>剩余</span>
          </div>
          <div className="budget-bar"><i style={{ width: `${Math.min(100, budgetShare * 100)}%` }} /></div>
          <ul className="ef-ledger">
            <li><span>输入</span><b>{usage?.tokens_in.toLocaleString() ?? "—"}</b></li>
            <li><span>输出</span><b>{usage?.tokens_out.toLocaleString() ?? "—"}</b></li>
          </ul>
        </div>
        </div>

        <div className="hub">
          <span className="ef-eyebrow">PHASE DIAL</span>
          <Dial sections={dialSections} gauges={[
            { label: "NODES", share: nodes.length ? doneCount / nodes.length : 0 },
            { label: "BUDGET", share: budgetShare },
          ]} idle={{
            zh: "NODES",
            en: "DONE",
            big: String(doneCount).padStart(2, "0"),
            unit: `/${String(nodes.length).padStart(2, "0")}`,
            sub: "COMPLETE",
          }} />
          <div className="hub-foot">
            <span>{phases.length} 阶段</span>
            <span>{doneCount}/{nodes.length} 完成</span>
          </div>
        </div>

        <div className="board-col board-right">
        <div className="r1 ef-panel">
          <h2><span className="ef-eyebrow">NODES</span>节点</h2>
          <ul className="ef-ledger">
            {["done", "running", "waiting_human", "pending", "failed"].map((status) => {
              const count = nodes.filter((node) => node.status === status).length;
              return <li key={status}><span>{statusLabel(status)}</span><b>{String(count).padStart(2, "0")}</b></li>;
            })}
          </ul>
        </div>

        <div className="r2 ef-panel">
          <h2><span className="ef-eyebrow">ARTIFACTS</span>工件</h2>
          <div className="ef-figs">
            <b>{String(activeArtifacts.length).padStart(2, "0")}</b><span>活跃</span>
            <b>{String(ARTIFACT_KINDS.length).padStart(2, "0")}</b><span>种类</span>
          </div>
          <ul className="ef-ledger">
            {activeArtifacts.slice(0, 3).map((item) => (
              <li key={item.kind}>
                <span>{ARTIFACT_LABELS[item.kind] ?? item.kind}</span>
                <b>v{item.version}</b>
              </li>
            ))}
          </ul>
        </div>

        <div className="r3 ef-panel">
          <h2><span className="ef-eyebrow">EVENTS</span>事件</h2>
          <div className="ef-figs">
            <b>{stats.events}</b><span>事件</span>
            <b>{stats.tool_calls}</b><span>工具调用</span>
          </div>
          <button
            type="button"
            className="linkish"
            onClick={() => {
              setFilterNodeId(null);
              setLogOpen(true);
            }}
          >
            查看全部事件 / LOG
          </button>
        </div>
        </div>
      </section>

      <ExecutionMap
        snapshot={snapshot}
        onNodeClick={openNode}
        selectedNodeId={selectedNodeId}
        onNodeSelect={(node) => {
          if (!node) setSelectedNodeId(null);
        }}
      />

      {review?.available ? (
        <section className="ef-panel review-panel">
          <div className="review-title">
            <h2><span className="ef-eyebrow">CODE REVIEW</span>代码评审</h2>
            <div className="review-figs">
              <b className={review.verdict === "pass" ? "is-pass" : "is-block"}>
                {review.verdict === "pass" ? "PASS" : "BLOCK"}
              </b>
              <span>第 {review.round}/{review.max_rounds} 轮 · 阻塞 {review.blocking}</span>
              {metrics?.review ? (
                <span>
                  一次通过 {metrics.review.first_pass ? "是" : "否"} · 返工 {metrics.review.rework_count} 次
                </span>
              ) : null}
            </div>
          </div>
          <p className="review-summary">{review.summary || "评审未给出摘要。"}</p>
          {review.issues.length ? (
            <ul className="review-issues">
              {review.issues.map((issue, index) => {
                const uid = issue.task_uid || "";
                const checked = Boolean(uid) && reworkUids.includes(uid);
                return (
                  <li key={`${uid}-${index}`} className={`is-${issue.severity}`}>
                    <Checkbox
                      checked={checked}
                      disabled={!review.can_rework || !uid}
                      onChange={(event) => {
                        if (!uid) return;
                        setReworkUids((previous) => {
                          if (!event.target.checked) return previous.filter((item) => item !== uid);
                          return previous.includes(uid) ? previous : [...previous, uid];
                        });
                      }}
                    />
                    <Tag className={`review-sev is-${issue.severity}`}>
                      {SEV_LABEL[issue.severity] ?? issue.severity}
                    </Tag>
                    <div className="review-issue-body">
                      <b>
                        {uid || "全局"}
                        {issue.file ? <span className="mono"> · {issue.file}</span> : null}
                      </b>
                      <p>{issue.message}</p>
                      {issue.suggestion ? <em>建议：{issue.suggestion}</em> : null}
                    </div>
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="review-empty">本轮没有发现问题。</p>
          )}
          <div className="review-actions">
            {review.can_rework ? (
              <Input
                size="small"
                placeholder="给返工任务的补充说明（可选）"
                value={reworkNote}
                onChange={(event) => setReworkNote(event.target.value)}
              />
            ) : (
              <em className="review-locked">评审已归档，不可再回退</em>
            )}
            <Button size="small" disabled={!review.body} onClick={() => setReviewOpen(true)}>
              查看报告
            </Button>
            <Button
              size="small"
              type="primary"
              danger
              disabled={!review.can_rework || reworkUids.length === 0 || rework.isPending}
              loading={rework.isPending}
              onClick={() => rework.mutate()}
            >
              回退 {reworkUids.length} 个任务
            </Button>
          </div>
          <div className="review-metricline">
            <span>问题 {metrics?.review?.issues ?? review.issues.length}</span>
            <span>阻塞 {metrics?.review?.severities?.blocker ?? 0}</span>
            <span>重要 {metrics?.review?.severities?.major ?? 0}</span>
            <span>建议 {metrics?.review?.severities?.minor ?? 0}</span>
            <span>
              评审均值 {metrics?.review ? Math.round(metrics.review.avg_review_ms / 1000) : 0}s
            </span>
          </div>
        </section>
      ) : null}

      <section className="lower">
        <div className="ef-panel">
          <h2><span className="ef-eyebrow">RUN LEDGER</span>运行账本</h2>
          <table className="ef-table">
            <thead>
              <tr><th>#</th><th>节点</th><th>相位</th><th>用时</th><th>TOKEN</th><th>状态</th></tr>
            </thead>
            <tbody>
              {ledgerRows.map(({ node, code }) => (
                <tr key={node.id} onClick={() => openNode(node)} className="ledger-node">
                  <td className="mono">{code}</td>
                  <td>{node.name}</td>
                  <td>{snapshot.phase_names[node.phase] ?? node.phase}</td>
                  <td className="mono">{node.attempt ? durationText(node.attempt.started_at, node.attempt.finished_at) : "—"}</td>
                  <td className="mono">{node.attempt ? (node.attempt.tokens_in + node.attempt.tokens_out).toLocaleString() : "—"}</td>
                  <td>{statusLabel(node.status)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="ef-tape">
            {events.slice(-4).map((event) => (
              <span key={`${event.seq ?? event.ts}-${event.type}`}>
                <b>{event.type}</b> {eventText(event)}
              </span>
            ))}
          </div>
        </div>

        <GateConsole
          snapshot={snapshot}
          gate={openGate}
          heldCount={held.size}
          onDecided={() => client.invalidateQueries({ queryKey: ["snapshot", runId] })}
        />
      </section>

      <section className="chain">
        <span className="ef-eyebrow">ARTIFACT CHAIN</span>
        <div className="items">
          {activeArtifacts.map((item) => (
            <button type="button" className="ef-chip" key={item.kind} onClick={() => openArtifact(item)}>
              {ARTIFACT_LABELS[item.kind] ?? item.kind} v{item.version}
            </button>
          ))}
          {pendingKinds.length > 0 && (
            <span className="is-pending">+{pendingKinds.length} PENDING</span>
          )}
        </div>
      </section>

      <footer className="runfoot">
        <span>AM-WORKFLOW · W1</span>
        <span>RUN {run.id} · {snapshot.workflow.key}</span>
        <span>{doneCount}/{nodes.length} NODES · {usage?.total.toLocaleString() ?? 0} TOKENS</span>
      </footer>

      <Drawer
        open={logOpen}
        onClose={() => setLogOpen(false)}
        title={logFilterKey ? `运行事件流 · 只看 ${logFilterKey}` : "运行事件流"}
        width={620}
        extra={filterNodeId != null ? (
          <Button size="small" onClick={() => setFilterNodeId(null)}>查看全部</Button>
        ) : null}
        styles={{ body: { background: "#161616", color: "#f4f4f2", padding: 12 } }}
      >
        <div className="am-log">
          {visibleLogEvents.length === 0 && <Empty description="暂无事件" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
          {visibleLogEvents.map((event, index) => (
            <div
              key={`${event.seq ?? index}-${event.type}`}
              className={`log-line ${event.type.includes("fail") || event.type.includes("error") || event.type === "context_prefix_changed" ? "is-error" : ""}`}
              style={LOG_LINE_STYLE}
            >
              <time>{event.ts?.slice(11, 19) ?? "--:--:--"}</time>
              <span className="node">
                {event.node_id != null ? (nodeKeyById.get(event.node_id) ?? `#${event.node_id}`) : "run"}
              </span>
              <span className="msg">{eventText(event)}</span>
            </div>
          ))}
        </div>
      </Drawer>

      <NodeInspector
        runId={runId}
        runStatus={run.status}
        node={selectedNode}
        detail={nodeQuery.data}
        detailError={nodeQuery.error?.message}
        events={events}
        open={selectedNodeId != null}
        onClose={() => setSelectedNodeId(null)}
        onAction={runNodeAction}
        eventText={eventText}
        artifacts={snapshot.artifacts}
        onOpenArtifact={openArtifact}
        onSelectNode={(nodeKey) => {
          const target = nodes.find((node) => node.node_key === nodeKey);
          if (target) setSelectedNodeId(target.id);
          else messageApi.warning(`节点 ${nodeKey} 不在当前快照中`);
        }}
        onFilterEvents={(target) => {
          setFilterNodeId(target.id);
          setLogOpen(true);
        }}
      />

      <Drawer
        open={Boolean(meetingRef) || meetingLoading}
        onClose={() => {
          setMeetingRef(null);
          setMeetingLoading(false);
          setIntervention("");
        }}
        title={meetingDetailQuery.data
          ? `${nodes.find((node) => node.id === meetingRef?.nodeId)?.name ?? "会议"} · 会议详情`
          : "会议详情"}
        width={880}
      >
        {meetingLoading && <Spin />}
        {!meetingLoading && meetingDetailQuery.isLoading && <Spin />}
        {meetingDetailQuery.data && (() => {
          const detail = meetingDetailQuery.data;
          const turns = (meetingTurnsQuery.data?.items ?? []).slice(-50);
          const status = detail.meeting.status;
          const canIntervene = status === "active" || status === "waiting_rework";
          const disabledReason = status === "suspended"
            ? "会议在确认点挂起，处理完确认点后才能插话。"
            : status === "completed"
              ? "会议已结束，不能再插话。"
              : status === "abandoned"
                ? "会议已放弃，不能再插话。"
                : "";
          return (
            <div className="meeting-detail">
              <p className="ef-facts">
                <span>步骤 <b>{phaseLabel(detail.meeting.phase)}</b></span>
                <span>轮次 <b>{detail.meeting.round}</b></span>
                <span>状态 <b>{MEETING_STATUS_LABEL[status] ?? status}</b></span>
                <span>发言 <b>{detail.turn_count}</b></span>
              </p>

              <h4>参会者</h4>
              <ul className="ef-facts" style={{ display: "block" }}>
                {detail.participants.filter((item) => item.status === "active").map((item) => (
                  <li key={item.id}>
                    {item.display_name}
                    <span style={{ color: "var(--ef-ink-2)" }}>
                      {" "}· {item.mode}
                      {item.role_key ? ` · ${item.role_key}` : ""}
                      {item.model_id ? ` · 模型 #${item.model_id}` : ""}
                    </span>
                  </li>
                ))}
                {detail.participants.filter((item) => item.status === "active").length === 0 && (
                  <li>（还没有参会者）</li>
                )}
              </ul>

              <h4>决策点账本</h4>
              <Table
                size="small"
                rowKey="id"
                pagination={false}
                dataSource={detail.decision_points}
                locale={{ emptyText: "账本还是空的" }}
                columns={[
                  { title: "编号", dataIndex: "index", width: 70 },
                  {
                    title: "状态", dataIndex: "status", width: 100,
                    render: (value: string) => pointStatusLabel(value),
                  },
                  { title: "标题", dataIndex: "title" },
                  {
                    title: "结论", dataIndex: "conclusion",
                    render: (value: string | null) => value || "—",
                  },
                ]}
              />

              <h4>最近五十条发言</h4>
              <div className="node-messages" style={{ maxHeight: 340, overflow: "auto" }}>
                {turns.map((turn) => (
                  <div key={turn.id} className="node-msg">
                    <b>#{turn.seq} {turn.speaker_name}</b>
                    <pre>{turn.content.slice(0, 1200)}</pre>
                  </div>
                ))}
                {turns.length === 0 && <Empty description="还没有发言" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
              </div>

              <div style={{ display: "flex", gap: 8, marginTop: 12, alignItems: "flex-start" }}>
                <Input.TextArea
                  value={intervention}
                  onChange={(event) => setIntervention(event.target.value)}
                  placeholder={canIntervene ? "给会议插一句话；主持人会在下一步回应。" : disabledReason}
                  disabled={!canIntervene}
                  autoSize={{ minRows: 2, maxRows: 4 }}
                />
                <Button
                  type="primary"
                  loading={intervene.isPending}
                  disabled={!canIntervene || !intervention.trim()}
                  onClick={() => intervene.mutate()}
                >
                  插话
                </Button>
              </div>
              {!canIntervene && disabledReason && (
                <p style={{ color: "var(--ef-ink-2)", fontSize: 12, marginTop: 6 }}>
                  {disabledReason}
                </p>
              )}
            </div>
          );
        })()}
      </Drawer>

      <Modal
        open={Boolean(artifact)}
        title={artifact ? `${ARTIFACT_LABELS[artifact.kind] ?? artifact.kind} · ${artifact.kind}.md · v${artifact.version}` : ""}
        width={900}
        onCancel={() => setArtifact(null)}
        onOk={saveArtifact}
        confirmLoading={savingArtifact}
        okText="保存为新版本"
        cancelText="关闭"
      >
        {artifact && (
          <div>
            <p style={{ color: "var(--ef-ink-2)", fontSize: 12 }}>
              人工编辑后保存为 reviewed 版本；不会覆盖历史版本。
            </p>
            <textarea
              className="am-artifact-edit"
              value={artifact.body}
              onChange={(event) => setArtifact({ ...artifact, body: event.target.value })}
            />
          </div>
        )}
      </Modal>

      <Modal
        title="评审报告"
        open={reviewOpen}
        onCancel={() => setReviewOpen(false)}
        footer={<Button onClick={() => setReviewOpen(false)}>关闭</Button>}
        width={760}
      >
        <pre className="review-body">{review?.body || "报告内容为空。"}</pre>
      </Modal>
    </div>
  );
}
