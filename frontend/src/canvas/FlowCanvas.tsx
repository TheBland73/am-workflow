import { useMemo } from "react";
import {
  Background, Controls, MarkerType, ReactFlow,
  type Edge, type Node as FlowNode,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { Snapshot } from "../api/types";

const COLORS: Record<string, { bg: string; border: string; text: string }> = {
  done: { bg: "#ecfdf5", border: "#34d399", text: "#065f46" },
  running: { bg: "#eef2ff", border: "#6366f1", text: "#3730a3" },
  ready: { bg: "#f8fafc", border: "#94a3b8", text: "#334155" },
  pending: { bg: "#ffffff", border: "#d1d5db", text: "#6b7280" },
  waiting_human: { bg: "#fffbeb", border: "#f59e0b", text: "#92400e" },
  failed: { bg: "#fef2f2", border: "#f87171", text: "#991b1b" },
  skipped: { bg: "#f3f4f6", border: "#cbd5e1", text: "#64748b" },
};

const TYPE_LABEL: Record<string, string> = {
  PASS: "入口", AGENT: "智能体", MEETING: "会议", VERIFY: "验证", HUMAN_GATE: "人工门",
  TASKGRAPH: "任务图", DECISION: "决策", RESTORE: "恢复", CHECKPOINT: "检查点",
};

const COLS = 6;
const COL_GAP = 205;
const ROW_GAP = 150;
const STACK_GAP = 66;

export default function FlowCanvas({ snapshot }: { snapshot: Snapshot }) {
  const { nodes, edges } = useMemo(() => {
    const byWave = new Map<number, string[]>();
    for (const node of snapshot.nodes) {
      const wave = node.wave ?? 0;
      const list = byWave.get(wave) ?? [];
      list.push(node.node_key);
      byWave.set(wave, list);
    }
    const waves = [...byWave.keys()].sort((a, b) => a - b);
    const flowNodes: FlowNode[] = [];
    waves.forEach((wave, waveIndex) => {
      const keys = byWave.get(wave) ?? [];
      const col = waveIndex % COLS;
      const row = Math.floor(waveIndex / COLS);
      keys.forEach((key, index) => {
        const node = snapshot.nodes.find((item) => item.node_key === key)!;
        const color = COLORS[node.status] ?? COLORS.pending;
        flowNodes.push({
          id: node.node_key,
          position: {
            x: col * COL_GAP,
            y: row * ROW_GAP + index * STACK_GAP,
          },
          data: {
            label: (
              <div style={{ textAlign: "left", lineHeight: 1.35 }}>
                <div style={{ fontWeight: 600 }}>{node.name}</div>
                <div style={{ fontSize: 10, opacity: 0.75 }}>
                  {TYPE_LABEL[node.type] ?? node.type} · {node.status}
                </div>
              </div>
            ),
          },
          style: {
            width: 180,
            padding: "8px 10px",
            borderRadius: 10,
            background: color.bg,
            border: `1px solid ${color.border}`,
            color: color.text,
            fontSize: 12,
          },
          draggable: false,
          selectable: false,
        });
      });
    });
    const flowEdges: Edge[] = [];
    for (const node of snapshot.nodes) {
      for (const dep of node.deps) {
        flowEdges.push({
          id: `${dep}->${node.node_key}`,
          source: dep,
          target: node.node_key,
          type: "smoothstep",
          animated: node.status === "running",
          markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14 },
          style: { stroke: "#c7cbd8", strokeWidth: 1.4 },
        });
      }
    }
    return { nodes: flowNodes, edges: flowEdges };
  }, [snapshot]);

  const waveCounts = new Map<number, number>();
  for (const node of snapshot.nodes) {
    const wave = node.wave ?? 0;
    waveCounts.set(wave, (waveCounts.get(wave) ?? 0) + 1);
  }
  const rows = Math.max(1, Math.ceil(waveCounts.size / COLS));
  const maxStack = Math.max(1, ...waveCounts.values());
  const height = Math.min(760, Math.max(300, rows * ROW_GAP + (maxStack - 1) * STACK_GAP + 40));

  return (
    <div style={{ height, border: "1px solid #e5e7eb", borderRadius: 12, overflow: "hidden", background: "#fbfcfe" }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        fitView
        fitViewOptions={{ padding: 0.12 }}
        minZoom={0.3}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        panOnScroll
      >
        <Background gap={18} color="#eceef4" />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
