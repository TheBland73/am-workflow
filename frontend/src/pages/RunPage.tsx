import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Button, Drawer, Empty, Modal, Spin, Table, message,
} from "antd";
import { Link, useParams } from "react-router-dom";
import { api, subscribeRun } from "../api/client";
import type {
  ArtifactRecord, Gate, Node, NodeDetail, Snapshot, StreamEvent,
} from "../api/types";
import Dial from "../canvas/Dial";
import ExecutionMap from "../canvas/ExecutionMap";
import GateConsole from "../panels/GateConsole";

const ARTIFACT_LABELS: Record<string, string> = {
  idea: "想法", discussion: "讨论", proposal: "方案", review: "评审",
  requirement: "需求", design: "设计", task: "任务", test: "测试", changelog: "交付日志",
};
const ARTIFACT_KINDS = Object.keys(ARTIFACT_LABELS);

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
    case "nodes_reset": return `重置 ${Array.isArray(payload.nodes) ? payload.nodes.length : 0} 个节点`;
    case "gate_requested": return `等待审批：${key}`;
    case "gate_decided": return `审批 ${String(payload.action ?? "")}：${key}`;
    case "artifact_written": return `写入工件 ${String(payload.kind ?? "")} v${String(payload.version ?? "")}`;
    case "task_created": return `新增任务 ${String(payload.task_uid ?? "")}`;
    case "task_updated": return `更新任务 ${String(payload.task_uid ?? "")}`;
    case "node_tool_call": return `${key} 调用 ${String(payload.name ?? "")}`;
    case "node_tool_result": return `${key} 工具返回${payload.is_error ? "（错误）" : ""}`;
    case "memory_written": return `写入记忆 ${String(payload.subject ?? "")}`;
    default: return event.type;
  }
}

function heldKeys(nodes: Node[], gate: Gate | null) {
  const result = new Set<string>();
  const gateNode = nodes.find((node) => node.id === gate?.node_id);
  if (!gateNode) return result;
  const queue = nodes.filter((node) => node.deps.includes(gateNode.node_key));
  while (queue.length) {
    const node = queue.shift()!;
    if (result.has(node.node_key)) continue;
    if (node.status === "done" || node.status === "skipped") continue;
    result.add(node.node_key);
    for (const candidate of nodes) {
      if (candidate.deps.includes(node.node_key) && !result.has(candidate.node_key)) {
        queue.push(candidate);
      }
    }
  }
  return result;
}

export default function RunPage() {
  const { runId = "" } = useParams();
  const client = useQueryClient();
  const [messageApi, holder] = message.useMessage();
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const seen = useRef(new Set<number>());
  const [logOpen, setLogOpen] = useState(false);
  const [nodeDetail, setNodeDetail] = useState<NodeDetail | null>(null);
  const [nodeLoading, setNodeLoading] = useState(false);
  const [artifact, setArtifact] = useState<{ kind: string; body: string; original: string; version: number } | null>(null);
  const [savingArtifact, setSavingArtifact] = useState(false);

  const snapshotQuery = useQuery({
    queryKey: ["snapshot", runId],
    queryFn: () => api.getRun(runId),
    enabled: Boolean(runId),
    refetchInterval: (query) => {
      const status = query.state.data?.run.status;
      return status === "finished" || status === "failed" || status === "stopped" ? false : 8000;
    },
  });

  useEffect(() => {
    if (!runId) return;
    setEvents([]);
    seen.current = new Set();
    const unsubscribe = subscribeRun(runId, (event) => {
      if (event.seq != null) {
        if (seen.current.has(event.seq)) return;
        seen.current.add(event.seq);
      }
      setEvents((previous) => [...previous.slice(-399), event]);
      if (event.type !== "node_token") {
        client.invalidateQueries({ queryKey: ["snapshot", runId] });
      }
    }, 0);
    return unsubscribe;
  }, [client, runId]);

  const snapshot: Snapshot | undefined = snapshotQuery.data;
  const phases = snapshot?.workflow.phases ?? [];
  const nodes = snapshot?.nodes ?? [];
  const openGate = snapshot?.gates.find((gate) => gate.status === "open") ?? null;
  const held = useMemo(
    () => heldKeys(nodes, openGate),
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

  const openNode = async (node: Node) => {
    setNodeLoading(true);
    try {
      setNodeDetail(await api.getNode(node.id));
    } catch (error) {
      messageApi.error((error as Error).message);
    } finally {
      setNodeLoading(false);
    }
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
          <button type="button" className="linkish" onClick={() => setLogOpen(true)}>
            查看全部事件 / LOG
          </button>
        </div>
      </section>

      <ExecutionMap snapshot={snapshot} onNodeClick={openNode} />

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
        title="运行事件流"
        width={620}
        styles={{ body: { background: "#161616", color: "#f4f4f2", padding: 12 } }}
      >
        <div className="am-log">
          {events.length === 0 && <Empty description="暂无事件" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
          {events.map((event, index) => (
            <div key={`${event.seq ?? index}-${event.type}`} className={`log-line ${event.type.includes("fail") || event.type.includes("error") ? "is-error" : ""}`}>
              <time>{event.ts?.slice(11, 19) ?? "--:--:--"}</time>
              <span className="node">{event.node_id ?? "run"}</span>
              <span className="msg">{eventText(event)}</span>
            </div>
          ))}
        </div>
      </Drawer>

      <Drawer
        open={Boolean(nodeDetail) || nodeLoading}
        onClose={() => { setNodeDetail(null); setNodeLoading(false); }}
        title={nodeDetail ? `${nodeDetail.name} · ${nodeDetail.node_key}` : "节点详情"}
        width={680}
      >
        {nodeLoading && <Spin />}
        {nodeDetail && !nodeLoading && (
          <div className="node-detail">
            <p className="ef-facts">
              <span>类型 <b>{nodeDetail.type}</b></span>
              <span>状态 <b>{statusLabel(nodeDetail.status)}</b></span>
              <span>相位 <b>{nodeDetail.phase}</b></span>
              <span>尝试 <b>{nodeDetail.attempts.length}/{nodeDetail.max_attempts}</b></span>
            </p>
            <h4>尝试记录</h4>
            <Table
              size="small"
              rowKey="id"
              pagination={false}
              dataSource={nodeDetail.attempts}
              columns={[
                { title: "#", dataIndex: "attempt_no", width: 48 },
                { title: "状态", dataIndex: "status", width: 100 },
                { title: "开始", dataIndex: "started_at", render: (value: string | null) => fmtTime(value) },
                { title: "结束", dataIndex: "finished_at", render: (value: string | null) => fmtTime(value) },
                { title: "TOKEN", render: (_: unknown, row: { tokens_in: number; tokens_out: number }) => (row.tokens_in + row.tokens_out).toLocaleString() },
              ]}
            />
            {nodeDetail.attempts.some((item) => item.error) && (
              <pre className="am-artifact">{nodeDetail.attempts.map((item) => item.error).filter(Boolean).join("\n\n")}</pre>
            )}
            <h4>最近消息</h4>
            <div className="node-messages">
              {nodeDetail.messages.slice(-8).map((item, index) => (
                <div key={index} className={`node-msg is-${item.role}`}>
                  <b>{item.role}</b>
                  <pre>{item.content.slice(0, 1600)}</pre>
                </div>
              ))}
            </div>
          </div>
        )}
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
    </div>
  );
}
