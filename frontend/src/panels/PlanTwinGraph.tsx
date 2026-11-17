import { useMemo } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { PlanPhase, PlanPhases } from "../api/types";

interface Props {
  phases: PlanPhases | undefined;
  selectedUid: string | null;
  onSelect: (uid: string) => void;
}

type PhaseData = { phase: PlanPhase; total: number; critical: boolean; active: boolean };
type PhaseNode = Node<PhaseData, "phase">;
type TerminalData = { label: string; kind: "start" | "end" | "verify"; hint: string };
type TerminalNode = Node<TerminalData, "terminal">;

const NODE_W = 380;
const STEP = 78;
const FIRST_Y = 62;

function PhaseNodeView({ data }: NodeProps<PhaseNode>) {
  const { phase, total, critical, active } = data;
  const roles = Object.entries(phase.roles ?? {});
  return (
    <div className={`plan-twin-phase${active ? " is-active" : ""}${critical ? " is-critical" : ""}`}>
      <Handle type="target" position={Position.Top} className="plan-twin-handle" />
      <div className="plan-twin-phase-top">
        <span>PHASE {String(phase.index).padStart(2, "0")} / {String(total).padStart(2, "0")}</span>
        <em>{phase.task_count} 任务 · {phase.points} 点</em>
        {critical ? <i>CRITICAL</i> : null}
      </div>
      <b title={phase.title}>{phase.title}</b>
      <div className="plan-twin-phase-meta">
        {roles.map(([role, count]) => (
          <span key={role}>{role.toUpperCase()} {count}</span>
        ))}
        {phase.cross_links > 0 ? <span>{phase.cross_links} 交接</span> : null}
      </div>
      <Handle type="source" position={Position.Bottom} className="plan-twin-handle" />
    </div>
  );
}

function TerminalNodeView({ data }: NodeProps<TerminalNode>) {
  return (
    <div className={`plan-twin-terminal is-${data.kind}`}>
      <Handle type="target" position={Position.Top} className="plan-twin-handle" />
      <small>{data.label}</small>
      <b>{data.hint}</b>
      <Handle type="source" position={Position.Bottom} className="plan-twin-handle" />
    </div>
  );
}

const nodeTypes = { phase: PhaseNodeView, terminal: TerminalNodeView };

export default function PlanTwinGraph({ phases, selectedUid, onSelect }: Props) {
  const { nodes, edges } = useMemo(() => {
    const list: Array<PhaseNode | TerminalNode> = [];
    const links: Edge[] = [];
    const groups = phases?.phases ?? [];
    const criticalUids = new Set(phases?.critical_path ?? []);

    list.push({
      id: "start",
      type: "terminal",
      position: { x: 90, y: 0 },
      data: { label: "PASSNODE", kind: "start", hint: "Start" },
      draggable: false,
      selectable: false,
    });

    groups.forEach((phase, index) => {
      const critical = phase.tasks.some((task) => criticalUids.has(task.uid));
      const active = phase.tasks.some((task) => task.uid === selectedUid);
      list.push({
        id: `phase-${phase.index}`,
        type: "phase",
        position: { x: 0, y: FIRST_Y + index * STEP },
        data: { phase, total: groups.length, critical, active },
        draggable: false,
        selectable: true,
      });
      const previous = index === 0 ? "start" : `phase-${groups[index - 1].index}`;
      const previousPhase = groups[index - 1];
      const edgeCritical = previousPhase
        ? previousPhase.tasks.some((task) => criticalUids.has(task.uid)) && critical
        : false;
      links.push({
        id: `${previous}->phase-${phase.index}`,
        source: previous,
        target: `phase-${phase.index}`,
        type: "smoothstep",
        animated: edgeCritical,
        style: {
          stroke: edgeCritical ? "#5a51d6" : "#c9c6bb",
          strokeWidth: edgeCritical ? 1.8 : 1.1,
          strokeDasharray: edgeCritical ? undefined : "5 4",
        },
      });
    });

    const last = groups.length ? `phase-${groups[groups.length - 1].index}` : "start";
    const verifyY = FIRST_Y + groups.length * STEP;
    list.push({
      id: "verify",
      type: "terminal",
      position: { x: 90, y: verifyY },
      data: { label: "VERIFYNODE", kind: "verify", hint: "Spec Integrity Guard" },
      draggable: false,
      selectable: false,
    });
    list.push({
      id: "end",
      type: "terminal",
      position: { x: 90, y: verifyY + 58 },
      data: { label: "PASSNODE", kind: "end", hint: "End" },
      draggable: false,
      selectable: false,
    });
    links.push(
      {
        id: `${last}->verify`,
        source: last,
        target: "verify",
        type: "smoothstep",
        style: { stroke: "#c9c6bb", strokeWidth: 1.1, strokeDasharray: "5 4" },
      },
      {
        id: "verify->end",
        source: "verify",
        target: "end",
        type: "smoothstep",
        style: { stroke: "#c9c6bb", strokeWidth: 1.1, strokeDasharray: "5 4" },
      },
    );

    links.forEach((edge) => {
      edge.markerEnd = { type: MarkerType.ArrowClosed, width: 14, height: 14, color: "#8d8a7e" };
    });

    return { nodes: list, edges: links };
  }, [phases, selectedUid]);

  return (
    <div className="plan-twin-wrap">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodeClick={(_, node) => {
          if (node.type !== "phase") return;
          const uid = (node.data as PhaseData).phase.tasks[0]?.uid;
          if (uid) onSelect(uid);
        }}
        defaultViewport={{ x: 300, y: 4, zoom: 0.9 }}
        proOptions={{ hideAttribution: true }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable
      >
        <Background variant={BackgroundVariant.Dots} gap={18} size={1} color="#e2dfd4" />
        <Controls showInteractive={false} position="bottom-left" />
      </ReactFlow>
      <div className="plan-twin-hint">
        <span>Start → 阶段 → Verify → End · 点击阶段定位首个任务</span>
        <span><i className="plan-key is-critical" />关键阶段</span>
      </div>
    </div>
  );
}
