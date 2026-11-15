import { useEffect, useMemo, useRef, useState } from "react";
import type { PlanSnapshot, PlanTask } from "../api/types";

interface Props {
  tasks: PlanTask[];
  snapshot: PlanSnapshot | null;
  selected: string | null;
  onSelect: (uid: string) => void;
}

interface Edge {
  from: string;
  to: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

const EFFORT_POINTS: Record<string, number> = { S: 1, M: 2, L: 3, XL: 5 };

export default function PlanGraph({ tasks, snapshot, selected, onSelect }: Props) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const graphRef = useRef<HTMLDivElement>(null);
  const nodeRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [edges, setEdges] = useState<Edge[]>([]);

  const waves = useMemo(() => {
    const waveOf: Record<string, number> = {};
    Object.entries(snapshot?.waves ?? {}).forEach(([wave, members]) => {
      const number = Number(wave);
      (members ?? []).forEach((uid) => { waveOf[uid] = number; });
    });
    const result: Record<string, PlanTask[]> = {};
    tasks.forEach((task) => {
      const wave = waveOf[task.task_uid] ?? 1;
      (result[wave] ??= []).push(task);
    });
    return Object.entries(result)
      .map(([wave, items]) => ({ wave: Number(wave), items }))
      .sort((a, b) => a.wave - b.wave);
  }, [tasks, snapshot]);

  const critical = useMemo(() => new Set(snapshot?.critical_path ?? []), [snapshot]);
  const errorUids = useMemo(() => {
    const set = new Set<string>();
    snapshot?.validation?.errors?.forEach((error) => {
      if (error.task_uid) set.add(error.task_uid);
    });
    return set;
  }, [snapshot]);

  useEffect(() => {
    const node = graphRef.current;
    if (!node) return;
    const measure = () => setSize({ width: node.clientWidth, height: node.clientHeight });
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const box = graphRef.current;
    if (!box || !tasks.length) {
      setEdges([]);
      return;
    }
    const rect = box.getBoundingClientRect();
    const next: Edge[] = [];
    tasks.forEach((task) => {
      const target = nodeRefs.current[task.task_uid];
      if (!target) return;
      const t = target.getBoundingClientRect();
      task.deps.forEach((dep) => {
        const source = nodeRefs.current[dep];
        if (!source) return;
        const s = source.getBoundingClientRect();
        next.push({
          from: dep,
          to: task.task_uid,
          x1: s.right - rect.left,
          y1: s.top - rect.top + s.height / 2,
          x2: t.left - rect.left,
          y2: t.top - rect.top + t.height / 2,
        });
      });
    });
    setEdges(next);
  }, [tasks, size.width, size.height, snapshot?.version]);

  if (!tasks.length) {
    return (
      <div className="plan-graph is-empty">
        <div className="plan-empty">
          <b>还没有任务</b>
          <span>生成任务后，这里会按波次排成一张依赖图。</span>
        </div>
      </div>
    );
  }

  return (
    <div className="plan-graph-wrap" ref={wrapRef}>
      <div
        className="plan-graph"
        ref={graphRef}
        style={{
          gridTemplateColumns: `repeat(${waves.length}, minmax(150px, 1fr))`,
          minWidth: waves.length * 158,
        }}
      >
        {waves.map(({ wave, items }) => (
          <div className="plan-wave" key={wave}>
            <div className="plan-wave-head">
              <b>WAVE {String(wave).padStart(2, "0")}</b>
              <span>{items.length} TASKS</span>
            </div>
            <div className="plan-wave-body">
              {items.map((task) => (
                <button
                  key={task.task_uid}
                  type="button"
                  ref={(node) => { nodeRefs.current[task.task_uid] = node; }}
                  className={[
                    "plan-node",
                    selected === task.task_uid ? "is-selected" : "",
                    critical.has(task.task_uid) ? "is-critical" : "",
                    errorUids.has(task.task_uid) ? "is-error" : "",
                  ].join(" ").trim()}
                  onClick={() => onSelect(task.task_uid)}
                  title={task.title}
                >
                  <span className="plan-node-top">
                    <b>{task.task_uid}</b>
                    <span>{task.effort} · {EFFORT_POINTS[task.effort] ?? 1}PT</span>
                  </span>
                  <span className="plan-node-title">{task.title}</span>
                  <span className="plan-node-meta">{task.role_key.toUpperCase()} · {task.priority}</span>
                </button>
              ))}
            </div>
          </div>
        ))}
        <svg className="plan-edges" width={size.width} height={size.height} aria-hidden="true">
          {edges.map((edge) => {
            const mid = edge.x1 + Math.max(18, (edge.x2 - edge.x1) / 2);
            const onPath = critical.has(edge.from) && critical.has(edge.to);
            return (
              <path
                key={`${edge.from}-${edge.to}`}
                d={`M${edge.x1} ${edge.y1} H${mid} V${edge.y2} H${edge.x2}`}
                className={onPath ? "is-critical" : ""}
              />
            );
          })}
        </svg>
      </div>
    </div>
  );
}
