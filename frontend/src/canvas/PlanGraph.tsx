import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  applyNodeChanges,
  useReactFlow,
  type Connection,
  type Edge,
  type Node,
  type NodeChange,
  type NodeProps,
  type NodeTypes,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Modal, Input } from "antd";
import type { PlanSnapshot, PlanTask } from "../api/types";
import { collectLineageByKey } from "./graphLayout";

export interface PlanPoint {
  x: number;
  y: number;
}

interface Props {
  tasks: PlanTask[];
  snapshot: PlanSnapshot | null;
  layout: Record<string, PlanPoint>;
  selected: string | null;
  focusUids?: string[] | null;
  onSelect: (uid: string) => void;
  onMoveNodes: (positions: Record<string, PlanPoint>) => void;
  onAddDep: (source: string, target: string) => void;
  onRemoveDep: (source: string, target: string) => void;
  onRename: (uid: string, title: string) => Promise<void> | void;
  fitToken?: number;
  resetToken?: number;
}

type TaskData = {
  task: PlanTask;
  role: RoleKey;
  wave: number;
  critical: boolean;
  error: boolean;
  dim: boolean;
  points: number;
  deps: number;
};

type TaskNode = Node<TaskData, "task">;
type LaneData = { wave: number; count: number };
type LaneNode = Node<LaneData, "lane">;

type RoleKey = "dev" | "test" | "design" | "pm" | "review" | "ops";

const ROLES: Record<RoleKey, { label: string; color: string }> = {
  dev: { label: "开发", color: "#1D4ED8" },
  test: { label: "测试", color: "#0F766E" },
  design: { label: "设计", color: "#7C3AED" },
  pm: { label: "产品", color: "#B45309" },
  review: { label: "评审", color: "#BE123C" },
  ops: { label: "运维", color: "#15803D" },
};

export const ROLE_META = ROLES;

const EFFORT_POINTS: Record<string, number> = { S: 1, M: 2, L: 3, XL: 5 };
const NODE_W = 260;
const NODE_H = 156;
const COL_W = 292;
const ROW_H = 168;
const PAD_X = 30;
const PAD_Y = 56;

function roleOf(task: PlanTask): RoleKey {
  return (task.role_key in ROLES ? task.role_key : "dev") as RoleKey;
}

function TaskNodeCard({ data, selected }: NodeProps<TaskNode>) {
  const { task, role, wave, critical, error, dim, points, deps } = data;
  const meta = ROLES[role];
  return (
    <div
      className={[
        "flow-task",
        selected ? "is-selected" : "",
        critical ? "is-critical" : "",
        error ? "is-error" : "",
        dim ? "is-dim" : "",
      ].join(" ").trim()}
      style={{ ["--role" as string]: meta.color }}
    >
      <Handle type="target" position={Position.Left} className="flow-handle" />
      <i className="flow-task-rail" />
      {critical && <span className="flow-flag">CRIT</span>}
      {error && <span className="flow-flag is-error">ERR</span>}
      <div className="flow-task-head">
        <b>{task.task_uid}</b>
        <span>W{wave} · {task.effort} · {points}PT</span>
      </div>
      <div className="flow-task-title" title={task.title}>{task.title}</div>
      <div className="flow-task-desc" title={task.description}>
        {task.description || task.acceptance[0] || "未填写任务描述"}
      </div>
      <div className="flow-task-foot">
        <span className="flow-role">{meta.label}</span>
        <span className="flow-sec" title={task.design_section}>§ {task.design_section}</span>
        <span className="flow-dep">{deps} 依赖</span>
        <span className="flow-pri">{task.priority}</span>
      </div>
      <Handle type="source" position={Position.Right} className="flow-handle" />
    </div>
  );
}

function LaneNodeCard({ data }: NodeProps<LaneNode>) {
  return (
    <div className="flow-lane">
      <div className="flow-lane-head">
        <b>WAVE {String(data.wave).padStart(2, "0")}</b>
        <span>{data.count} TASKS</span>
      </div>
    </div>
  );
}

const nodeTypes = { task: TaskNodeCard, lane: LaneNodeCard } as unknown as NodeTypes;

const EDGE_STYLES = {
  critical: { stroke: "#161616", width: 2.4, dash: undefined, marker: "#161616" },
  handoff: { stroke: "#8A887E", width: 1.4, dash: "6 4", marker: "#8A887E" },
  error: { stroke: "#B3261E", width: 1.6, dash: "3 3", marker: "#B3261E" },
  normal: { stroke: "#C6C4BB", width: 1.3, dash: undefined, marker: "#A9A69B" },
};

function buildEdges(
  tasks: PlanTask[],
  critical: Set<string>,
  errors: Set<string>,
  active: Set<string> | null,
): Edge[] {
  const byUid = new Map(tasks.map((task) => [task.task_uid, task]));
  const result: Edge[] = [];
  tasks.forEach((task) => {
    task.deps.forEach((dep) => {
      if (!byUid.has(dep)) return;
      const source = byUid.get(dep)!;
      const isCritical = critical.has(dep) && critical.has(task.task_uid);
      const isError = errors.has(task.task_uid) || errors.has(dep);
      const isHandoff = source.role_key !== task.role_key;
      const isDim = Boolean(active && (!active.has(dep) || !active.has(task.task_uid)));
      const kind = isError ? "error" : isCritical ? "critical" : isHandoff ? "handoff" : "normal";
      const style = EDGE_STYLES[kind];
      result.push({
        id: `e:${dep}->${task.task_uid}`,
        source: dep,
        target: task.task_uid,
        type: "smoothstep",
        style: {
          stroke: style.stroke,
          strokeWidth: style.width,
          strokeDasharray: style.dash,
        },
        markerEnd: {
          type: MarkerType.ArrowClosed,
          color: style.marker,
          width: 13,
          height: 13,
        },
        data: { dep, uid: task.task_uid, kind },
        className: `flow-edge is-${kind}${isDim ? " is-dim" : ""}`,
      });
    });
  });
  return result;
}

function waveColumn(wave: number) {
  return PAD_X + (wave - 1) * COL_W;
}

function autoY(index: number) {
  return PAD_Y + index * ROW_H;
}

function Canvas({
  tasks,
  snapshot,
  layout,
  selected,
  focusUids,
  onSelect,
  onMoveNodes,
  onAddDep,
  onRemoveDep,
  onRename,
  fitToken,
  resetToken,
}: Props) {
  const flow = useReactFlow();
  const posRef = useRef<Record<string, PlanPoint>>({});
  const [epoch, setEpoch] = useState(0);
  const [nodes, setNodes] = useState<TaskNode[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [renameTarget, setRenameTarget] = useState<PlanTask | null>(null);
  const [renameText, setRenameText] = useState("");
  const [renaming, setRenaming] = useState(false);
  const uidKey = useMemo(() => tasks.map((task) => task.task_uid).join(","), [tasks]);

  const critical = useMemo(() => new Set(snapshot?.critical_path ?? []), [snapshot]);
  const errorUids = useMemo(() => {
    const set = new Set<string>();
    snapshot?.validation?.errors?.forEach((error) => {
      if (error.task_uid) set.add(error.task_uid);
    });
    return set;
  }, [snapshot]);

  const waveInfo = useMemo(() => {
    const waveOf = new Map<string, number>();
    Object.entries(snapshot?.waves ?? {}).forEach(([wave, members]) => {
      (members ?? []).forEach((uid) => waveOf.set(uid, Number(wave) || 1));
    });
    tasks.forEach((task) => {
      if (!waveOf.has(task.task_uid)) waveOf.set(task.task_uid, 1);
    });
    const waves = Array.from(new Set(waveOf.values())).sort((a, b) => a - b);
    return { waveOf, waves };
  }, [tasks, snapshot]);

  const active = useMemo(() => {
    if (focusUids && focusUids.length) return new Set(focusUids);
    if (!selected) return null;
    // 血缘算法与运行图共用 graphLayout；任务键是 task_uid（不是 node_key）。
    const lineage = collectLineageByKey(selected, tasks, (task) => task.task_uid);
    return lineage.size > 0 ? lineage : null;
  }, [tasks, selected, focusUids]);

  useEffect(() => {
    if (!tasks.length) {
      setNodes([]);
      setEdges([]);
      return;
    }
    const order = new Map<string, number>();
    tasks.forEach((task) => {
      const value = Number(task.task_uid.replace(/^T/, ""));
      order.set(task.task_uid, Number.isFinite(value) ? value : 9999);
    });
    const grouped = new Map<number, PlanTask[]>();
    tasks.forEach((task) => {
      const wave = waveInfo.waveOf.get(task.task_uid) ?? 1;
      const bucket = grouped.get(wave) ?? [];
      bucket.push(task);
      grouped.set(wave, bucket);
    });

    const next: Node[] = [];
    waveInfo.waves.forEach((wave) => {
      const bucket = (grouped.get(wave) ?? []).slice().sort(
        (a, b) => (order.get(a.task_uid) ?? 0) - (order.get(b.task_uid) ?? 0),
      );
      bucket.forEach((task, row) => {
        const stored = layout[task.task_uid] ?? posRef.current[task.task_uid];
        const position = stored
          ? { x: stored.x, y: stored.y }
          : { x: waveColumn(wave), y: autoY(row) };
        posRef.current[task.task_uid] = position;
        next.push({
          id: task.task_uid,
          type: "task",
          position,
          data: {
            task,
            role: roleOf(task),
            wave,
            critical: critical.has(task.task_uid),
            error: errorUids.has(task.task_uid),
            dim: Boolean(active && !active.has(task.task_uid)),
            points: EFFORT_POINTS[task.effort] ?? 1,
            deps: task.deps.length,
          },
          selected: selected === task.task_uid,
          draggable: true,
        } as TaskNode);
      });
    });

    waveInfo.waves.forEach((wave) => {
      const count = (grouped.get(wave) ?? []).length;
      next.unshift({
        id: `lane-${wave}`,
        type: "lane",
        position: { x: waveColumn(wave), y: 10 },
        data: { wave, count },
        draggable: false,
        selectable: false,
        connectable: false,
        focusable: false,
        zIndex: -1,
        style: {
          width: NODE_W,
          height: Math.max(168, count * ROW_H + 48 - (ROW_H - NODE_H) + 16),
          pointerEvents: "none",
        },
      } as LaneNode);
    });

    setNodes(next as TaskNode[]);
    setEdges(buildEdges(tasks, critical, errorUids, active));
  }, [tasks, snapshot, selected, layout, epoch, critical, errorUids, waveInfo, active]);

  const fitAll = useCallback(() => {
    flow.fitView({ padding: 0.1, duration: 360, minZoom: 0.32, maxZoom: 1 });
  }, [flow]);

  const onNodesChange = useCallback((changes: NodeChange<TaskNode>[]) => {
    setNodes((current) => applyNodeChanges(changes, current) as TaskNode[]);
  }, []);

  const onNodeDragStop = useCallback(
    (_event: unknown, node: Node) => {
      if (node.type !== "task") return;
      const positions: Record<string, PlanPoint> = {};
      nodes.forEach((item) => {
        if (item.type !== "task") return;
        const position = item.id === node.id ? node.position : item.position;
        posRef.current[item.id] = { x: position.x, y: position.y };
        positions[item.id] = posRef.current[item.id];
      });
      onMoveNodes(positions);
    },
    [nodes, onMoveNodes],
  );

  const onConnect = useCallback(
    (connection: Connection) => {
      const { source, target } = connection;
      if (!source || !target || source === target) return;
      const existing = tasks.find((task) => task.task_uid === target);
      if (existing?.deps.includes(source)) return;
      onAddDep(source, target);
    },
    [tasks, onAddDep],
  );

  const onEdgeClick = useCallback(
    (_event: unknown, edge: Edge) => {
      const data = (edge.data ?? {}) as { dep?: string; uid?: string };
      if (!data.dep || !data.uid) return;
      Modal.confirm({
        title: "删除依赖",
        content: `确认移除 ${data.dep} → ${data.uid} 的依赖？`,
        okText: "删除",
        cancelText: "取消",
        okButtonProps: { danger: true },
        onOk: () => onRemoveDep(data.dep!, data.uid!),
      });
    },
    [onRemoveDep],
  );

  const resetLayout = useCallback(() => {
    posRef.current = {};
    setEpoch((value) => value + 1);
    onMoveNodes({});
  }, [onMoveNodes]);

  useEffect(() => {
    if (fitToken) fitAll();
  }, [fitToken, fitAll]);

  useEffect(() => {
    if (resetToken) resetLayout();
  }, [resetToken, resetLayout]);

  const openRename = useCallback((task: PlanTask) => {
    setRenameTarget(task);
    setRenameText(task.title);
  }, []);

  const commitRename = useCallback(async () => {
    if (!renameTarget) return;
    const title = renameText.trim();
    if (title.length < 6 || title.length > 40) return;
    if (title === renameTarget.title) {
      setRenameTarget(null);
      return;
    }
    setRenaming(true);
    try {
      await onRename(renameTarget.task_uid, title);
      setRenameTarget(null);
    } catch {
      /* 失败时保留弹窗，错误由上层 toast 呈现 */
    } finally {
      setRenaming(false);
    }
  }, [renameTarget, renameText, onRename]);

  return (
    <div className="plan-flow-wrap">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onNodeDragStop={onNodeDragStop}
        onConnect={onConnect}
        onEdgeClick={onEdgeClick}
        onNodeClick={(_event, node) => {
          if (node.type === "task") onSelect(node.id);
        }}
        onNodeDoubleClick={(_event, node) => {
          if (node.type !== "task") return;
          const data = node.data as TaskData;
          openRename(data.task);
        }}
        nodesConnectable
        elementsSelectable
        panOnScroll
        selectionOnDrag={false}
        minZoom={0.25}
        maxZoom={1.6}
        defaultViewport={{ x: 14, y: 6, zoom: 0.62 }}
        proOptions={{ hideAttribution: true }}
        defaultEdgeOptions={{ type: "smoothstep" }}
      >
        <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="#D3D1C8" />
        <Controls showInteractive={false} position="bottom-left" />
        <MiniMap
          pannable
          zoomable
          position="bottom-right"
          style={{ width: 138, height: 92 }}
          maskColor="rgba(244, 244, 242, 0.72)"
          nodeColor={(node) => {
            const data = node.data as TaskData | undefined;
            return data?.role ? ROLES[data.role].color : "#C6C4BB";
          }}
          nodeStrokeColor="#161616"
          nodeStrokeWidth={1}
        />
      </ReactFlow>

      <Modal
        open={Boolean(renameTarget)}
        title="重命名任务"
        okText="保存并校验"
        cancelText="取消"
        confirmLoading={renaming}
        okButtonProps={{ disabled: renameText.trim().length < 6 }}
        onOk={commitRename}
        onCancel={() => setRenameTarget(null)}
      >
        <div className="flow-rename">
          <Input
            autoFocus
            value={renameText}
            maxLength={40}
            onChange={(event) => setRenameText(event.target.value)}
            onPressEnter={commitRename}
            placeholder="6–40 字，说清楚交付物"
          />
          <span>双击标题是快捷入口；描述、依赖、验收标准在「任务详情」里改。</span>
        </div>
      </Modal>
    </div>
  );
}

export default function PlanGraph(props: Props) {
  if (!props.tasks.length) {
    return (
      <div className="plan-graph is-empty">
        <div className="plan-empty">
          <b>还没有任务</b>
          <span>生成任务后，这里会按波次排成一张可拖拽的依赖图。</span>
        </div>
      </div>
    );
  }
  return (
    <ReactFlowProvider>
      <Canvas {...props} />
    </ReactFlowProvider>
  );
}
