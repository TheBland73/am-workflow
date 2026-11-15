import { useEffect, useMemo, useRef, useState } from "react";
import {
  Alert, Button, Card, Descriptions, Drawer, Progress, Space, Table, Tabs, Tag, Tooltip,
} from "antd";
import {
  ArrowLeftOutlined, ReloadOutlined, StopOutlined,
} from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { api, subscribeRun } from "../api/client";
import type { Node, Snapshot, StreamEvent } from "../api/types";
import { useRunStore } from "../store/runStore";
import FlowCanvas from "../canvas/FlowCanvas";
import LogPanel from "../panels/LogPanel";
import ArtifactsPanel from "../panels/ArtifactsPanel";
import TasksPanel from "../panels/TasksPanel";
import GateCard from "../panels/GateCard";
import { StatusTag } from "./RunsPage";

const NODE_COLOR: Record<string, string> = {
  done: "success", running: "processing", waiting_human: "warning",
  failed: "error", pending: "default", ready: "default", skipped: "default",
};

const STRUCTURAL = new Set([
  "run_created", "run_finished", "run_error", "run_stopped", "run_recovered",
  "phase_entered", "node_started", "node_finished", "node_failed", "node_retried",
  "nodes_reset", "artifact_written", "task_created", "task_updated",
  "gate_requested", "gate_decided",
]);

export default function RunPage() {
  const { runId = "" } = useParams();
  const navigate = useNavigate();
  const client = useQueryClient();
  const reset = useRunStore((state) => state.reset);
  const push = useRunStore((state) => state.push);
  const [gateClosed, setGateClosed] = useState(false);
  const [selected, setSelected] = useState<Node | null>(null);

  const snapshot = useQuery({
    queryKey: ["snapshot", runId],
    queryFn: () => api.getRun(runId),
    enabled: Boolean(runId),
    refetchInterval: (query) => {
      const data = query.state.data as Snapshot | undefined;
      return data && ["finished", "failed", "stopped"].includes(data.run.status) ? false : 8000;
    },
  });

  const nodesRef = useRef<Node[]>([]);
  useEffect(() => {
    nodesRef.current = snapshot.data?.nodes ?? [];
  }, [snapshot.data]);

  const ready = Boolean(snapshot.data);
  useEffect(() => {
    if (!runId || !ready) return;
    reset(runId);
    let cancelled = false;
    let unsubscribe: () => void = () => undefined;
    const keyOf = (id: number | null) =>
      nodesRef.current.find((node) => node.id === id)?.node_key;
    const onEvent = (event: StreamEvent) => {
      push(event, keyOf(event.node_id));
      if (STRUCTURAL.has(event.type)) {
        client.invalidateQueries({ queryKey: ["snapshot", runId] });
      }
    };
    // 先补历史，再从最后一条 seq 之后订阅，避免回放重复。
    api.history(runId).then(({ items }) => {
      if (cancelled) return;
      items.forEach((event) => push(event, keyOf(event.node_id)));
      unsubscribe = subscribeRun(runId, onEvent, useRunStore.getState().lastSeq);
    }).catch(() => {
      if (!cancelled) unsubscribe = subscribeRun(runId, onEvent, 0);
    });
    return () => {
      cancelled = true;
      unsubscribe();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, ready]);

  const openGate = useMemo(() => {
    if (!snapshot.data) return null;
    if (snapshot.data.run.status !== "waiting_human") return null;
    return snapshot.data.gates.find((gate) => gate.status === "open") ?? null;
  }, [snapshot.data]);

  useEffect(() => setGateClosed(false), [openGate?.id]);

  const data = snapshot.data;

  if (snapshot.isLoading) return <Card loading />;
  if (snapshot.isError || !data) {
    return (
      <Alert
        type="error" showIcon
        message="加载运行失败"
        description={(snapshot.error as Error)?.message}
        action={<Button size="small" onClick={() => snapshot.refetch()}>重试</Button>}
      />
    );
  }

  const phases = data.workflow.phases;
  const currentIndex = data.run.current_phase ? phases.indexOf(data.run.current_phase) : -1;
  const usagePercent = data.usage.budget
    ? Math.min(100, Math.round((data.usage.total / data.usage.budget) * 100))
    : 0;

  return (
    <div>
      <div className="am-page-head">
        <div>
          <Space size={10} align="center">
            <Button size="small" type="text" icon={<ArrowLeftOutlined />} onClick={() => navigate("/runs")} />
            <h1 className="am-page-title">运行 <span className="am-mono">{data.run.id}</span></h1>
            <StatusTag status={data.run.status} />
          </Space>
          <div className="am-page-sub" style={{ maxWidth: 900 }}>{data.run.idea}</div>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => snapshot.refetch()}>刷新</Button>
          {["created", "running", "waiting_human"].includes(data.run.status) && (
            <Button danger icon={<StopOutlined />} onClick={() => api.stopRun(runId)}>停止</Button>
          )}
          {openGate && gateClosed && (
            <Button type="primary" onClick={() => setGateClosed(false)}>处理审批</Button>
          )}
        </Space>
      </div>

      <Card size="small" style={{ marginBottom: 14 }}>
        <div className="am-phase-strip">
          {phases.map((phase, index) => (
            <span
              key={phase}
              className={`am-phase ${index === currentIndex ? "active" : ""} ${
                currentIndex > index || data.run.status === "finished" ? "done" : ""
              }`}
            >
              {index + 1}. {data.phase_names[phase] ?? phase}
            </span>
          ))}
        </div>
        <div style={{ display: "flex", gap: 28, marginTop: 14, flexWrap: "wrap" }}>
          <div style={{ minWidth: 220, flex: 1 }}>
            <div style={{ fontSize: 12, color: "#6b7280", marginBottom: 4 }}>
              Token 用量 {data.usage.total.toLocaleString()} / {data.usage.budget.toLocaleString()}
            </div>
            <Progress percent={usagePercent} size="small" showInfo={false} />
          </div>
          <Descriptions
            size="small" column={3}
            items={[
              { key: "nodes", label: "节点", children: `${data.nodes.filter((n) => n.status === "done").length}/${data.nodes.length}` },
              { key: "tasks", label: "任务", children: data.tasks.length },
              { key: "artifacts", label: "工件", children: data.artifacts.filter((a) => a.status === "active").length },
            ]}
          />
        </div>
      </Card>

      <Card size="small" title="执行图（只读）" style={{ marginBottom: 14 }}>
        <FlowCanvas snapshot={data} />
      </Card>

      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1.55fr) minmax(360px, 1fr)", gap: 14 }}>
        <div>
          <Card size="small" title="节点">
            <Table
              rowKey="id"
              size="small"
              pagination={false}
              dataSource={data.nodes}
              onRow={(node) => ({ onClick: () => setSelected(node), style: { cursor: "pointer" } })}
              columns={[
                { title: "节点", dataIndex: "name", render: (name: string, node: Node) => (
                  <span><span className="am-mono" style={{ color: "#9ca3af" }}>{node.node_key}</span> {name}</span>
                ) },
                { title: "类型", dataIndex: "type", width: 110 },
                { title: "状态", dataIndex: "status", width: 110,
                  render: (status: string) => <Tag color={NODE_COLOR[status]}>{status}</Tag> },
                { title: "尝试", dataIndex: "attempts", width: 60 },
              ]}
            />
          </Card>
        </div>

        <Card size="small" styles={{ body: { paddingTop: 8 } }}>
          <Tabs
            size="small"
            items={[
              { key: "logs", label: "事件日志", children: <LogPanel /> },
              { key: "artifacts", label: "工件", children: (
                <ArtifactsPanel runId={runId} artifacts={data.artifacts} />
              ) },
              { key: "tasks", label: "任务", children: (
                <TasksPanel runId={runId} tasks={data.tasks} />
              ) },
              { key: "gates", label: "审批记录", children: (
                <Table
                  rowKey="id" size="small" pagination={false}
                  dataSource={data.gates}
                  columns={[
                    { title: "门", dataIndex: "node_key" },
                    { title: "状态", dataIndex: "status", width: 90,
                      render: (status: string) => (
                        <Tag color={status === "approved" ? "success" : status === "rejected" ? "error" : "warning"}>
                          {status}
                        </Tag>
                      ) },
                    { title: "决策人", dataIndex: "decided_by", width: 80 },
                  ]}
                />
              ) },
            ]}
          />
        </Card>
      </div>

      <GateCard
        snapshot={data}
        gate={gateClosed ? null : openGate}
        onClose={() => setGateClosed(true)}
        onDecided={() => client.invalidateQueries({ queryKey: ["snapshot", runId] })}
      />

      <NodeDrawer node={selected} onClose={() => setSelected(null)} />
    </div>
  );
}

function NodeDrawer({ node, onClose }: { node: Node | null; onClose: () => void }) {
  const detail = useQuery({
    queryKey: ["node", node?.id],
    queryFn: () => fetch(`/api/nodes/${node!.id}`).then((resp) => resp.json()),
    enabled: Boolean(node),
  });
  const attempts = (detail.data?.attempts ?? []) as Array<Record<string, unknown>>;
  const messages = (detail.data?.messages ?? []) as Array<Record<string, unknown>>;

  return (
    <Drawer
      open={Boolean(node)} onClose={onClose} width={560}
      title={node ? `${node.name} · ${node.node_key}` : ""}
    >
      {node && (
        <Space direction="vertical" size={16} style={{ width: "100%" }}>
          <Descriptions
            size="small" column={2} bordered
            items={[
              { key: "type", label: "类型", children: node.type },
              { key: "status", label: "状态", children: node.status },
              { key: "phase", label: "阶段", children: node.phase },
              { key: "wave", label: "波次", children: node.wave ?? "—" },
              { key: "deps", label: "依赖", children: node.deps.join(", ") || "—" },
              { key: "attempts", label: "尝试", children: `${node.attempts}/${node.max_attempts}` },
            ]}
          />
          <div>
            <div style={{ fontWeight: 600, marginBottom: 8 }}>节点配置</div>
            <pre className="am-artifact am-mono" style={{ maxHeight: 180 }}>
              {JSON.stringify(node.config, null, 2)}
            </pre>
          </div>
          <div>
            <div style={{ fontWeight: 600, marginBottom: 8 }}>尝试记录</div>
            <Table
              rowKey="id" size="small" pagination={false} dataSource={attempts}
              columns={[
                { title: "#", dataIndex: "attempt_no", width: 48 },
                { title: "状态", dataIndex: "status", width: 90 },
                { title: "tokens", width: 110,
                  render: (_: unknown, row: Record<string, unknown>) =>
                    `${row.tokens_in ?? 0}/${row.tokens_out ?? 0}` },
                { title: "错误", dataIndex: "error", ellipsis: true },
              ]}
            />
          </div>
          <div>
            <div style={{ fontWeight: 600, marginBottom: 8 }}>消息</div>
            {messages.length === 0 && <span style={{ color: "#6b7280" }}>暂无</span>}
            {messages.map((message, index) => (
              <div key={index} style={{ marginBottom: 8 }}>
                <Tag>{String(message.role)}</Tag>
                <Tooltip title="">
                  <pre className="am-artifact am-mono" style={{ maxHeight: 160, margin: 0 }}>
                    {String(message.content ?? "").slice(0, 2400)}
                  </pre>
                </Tooltip>
              </div>
            ))}
          </div>
        </Space>
      )}
    </Drawer>
  );
}
