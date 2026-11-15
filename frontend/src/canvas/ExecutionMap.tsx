import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { Node, Snapshot } from "../api/types";

interface EdgePath {
  d: string;
  held: boolean;
}

function isDone(status: string) {
  return status === "done" || status === "skipped";
}

export default function ExecutionMap({
  snapshot, onNodeClick,
}: {
  snapshot: Snapshot;
  onNodeClick: (node: Node) => void;
}) {
  const { nodes, workflow, phase_names: phaseNames, run, gates } = snapshot;
  const wrapRef = useRef<HTMLDivElement>(null);
  const nodeRefs = useRef(new Map<number, HTMLButtonElement>());
  const [edges, setEdges] = useState<EdgePath[]>([]);

  const openGate = gates.find((gate) => gate.status === "open");
  const gateNode = nodes.find((node) => node.id === openGate?.node_id);
  const gateIndex = useMemo(() => {
    const gatesOnly = nodes.filter((node) => node.type === "HUMAN_GATE");
    return gateNode ? gatesOnly.findIndex((node) => node.id === gateNode.id) + 1 : 0;
  }, [gateNode, nodes]);

  const held = useMemo(() => {
    const result = new Set<string>();
    if (!gateNode) return result;
    const byKey = new Map(nodes.map((node) => [node.node_key, node]));
    const queue = nodes.filter((node) => node.deps.includes(gateNode.node_key));
    while (queue.length) {
      const node = queue.shift()!;
      if (result.has(node.node_key)) continue;
      result.add(node.node_key);
      for (const candidate of nodes) {
        if (candidate.deps.includes(node.node_key) && !result.has(candidate.node_key)) {
          queue.push(candidate);
        }
      }
    }
    // 被门直接或间接挡住、但尚未完成的节点
    for (const key of [...result]) {
      const node = byKey.get(key);
      if (!node || isDone(node.status)) result.delete(key);
    }
    return result;
  }, [gateNode, nodes]);

  const columns = useMemo(
    () => workflow.phases.map((phase) => ({
      phase,
      nodes: nodes.filter((node) => node.phase === phase),
    })),
    [nodes, workflow.phases],
  );

  const numbers = useMemo(() => {
    const map = new Map<number, string>();
    let index = 0;
    let gateNo = 0;
    for (const node of nodes) {
      if (node.type === "HUMAN_GATE") {
        gateNo += 1;
        map.set(node.id, `G${gateNo}`);
      } else {
        index += 1;
        map.set(node.id, String(index).padStart(2, "0"));
      }
    }
    return map;
  }, [nodes]);

  const nodeByKey = useMemo(
    () => new Map(nodes.map((node) => [node.node_key, node])),
    [nodes],
  );

  const firstHeldPhase = useMemo(() => {
    if (!held.size) return -1;
    return workflow.phases.findIndex((phase) =>
      nodes.some((node) => node.phase === phase && held.has(node.node_key)));
  }, [held, nodes, workflow.phases]);

  const maxRows = Math.max(...columns.map((column) => column.nodes.length), 1);
  const mapHeight = Math.max(160, maxRows * 34 + 8);

  const measure = useCallback(() => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    const box = wrap.getBoundingClientRect();
    if (!box.width) return;
    const paths: EdgePath[] = [];
    for (const node of nodes) {
      const target = nodeRefs.current.get(node.id);
      if (!target) continue;
      const targetBox = target.getBoundingClientRect();
      for (const depKey of node.deps) {
        const dep = nodeByKey.get(depKey);
        const source = dep ? nodeRefs.current.get(dep.id) : undefined;
        if (!source) continue;
        const sourceBox = source.getBoundingClientRect();
        const sameColumn = Math.abs(sourceBox.left - targetBox.left) < 4;
        let d: string;
        if (sameColumn) {
          const sx = sourceBox.left + sourceBox.width / 2 - box.left;
          const sy = sourceBox.bottom - box.top;
          const tx = targetBox.left + targetBox.width / 2 - box.left;
          const ty = targetBox.top - box.top;
          d = `M${sx.toFixed(1)} ${sy.toFixed(1)} V${((sy + ty) / 2).toFixed(1)} H${tx.toFixed(1)} V${ty.toFixed(1)}`;
        } else {
          const sx = sourceBox.right - box.left;
          const sy = sourceBox.top + sourceBox.height / 2 - box.top;
          const tx = targetBox.left - box.left;
          const ty = targetBox.top + targetBox.height / 2 - box.top;
          const mid = (sx + tx) / 2;
          d = `M${sx.toFixed(1)} ${sy.toFixed(1)} H${mid.toFixed(1)} V${ty.toFixed(1)} H${tx.toFixed(1)}`;
        }
        paths.push({ d, held: held.has(node.node_key) || held.has(depKey) });
      }
    }
    setEdges(paths);
  }, [held, nodeByKey, nodes]);

  useLayoutEffect(() => {
    measure();
    const wrap = wrapRef.current;
    if (!wrap) return;
    const observer = new ResizeObserver(() => measure());
    observer.observe(wrap);
    return () => observer.disconnect();
  }, [measure, snapshot]);

  const done = nodes.filter((node) => isDone(node.status)).length;
  const statusClass = (node: Node) => {
    const classes = ["map-node"];
    if (isDone(node.status)) classes.push("is-done");
    if (held.has(node.node_key)) classes.push("is-held");
    if (node.type === "HUMAN_GATE") classes.push("is-gate");
    if (node.status === "waiting_human" && run.status === "waiting_human") classes.push("is-here");
    return classes.join(" ");
  };

  return (
    <section className="ef-panel map-panel">
      <h2>
        执行图
        <small>{nodes.length} NODES / {String(done).padStart(2, "0")} DONE</small>
        {openGate && gateNode && (
          <span className="ef-tag gate-tag">
            GATE {String(gateIndex).padStart(2, "0")} · {held.size} HELD
          </span>
        )}
      </h2>

      <div className="map-phases" aria-hidden="true">
        {columns.map(({ phase, nodes: phaseNodes }, index) => {
          const doneCount = phaseNodes.filter((node) => isDone(node.status)).length;
          return (
            <div key={phase} className={`map-phase ${run.current_phase === phase ? "is-here" : ""}`}>
              <span className="k">{String(index + 1).padStart(2, "0")} {phase}</span>
              <span className="zh">{phaseNames[phase] ?? phase}</span>
              <span className="n">{doneCount} / {phaseNodes.length}</span>
            </div>
          );
        })}
      </div>

      <div className="map-body" ref={wrapRef} style={{ height: mapHeight }}>
        {firstHeldPhase >= 0 && (
          <>
            <div className="map-heldzone" style={{ left: `${(firstHeldPhase / 8) * 100}%` }} />
            <div className="map-gatebar" style={{ left: `${(firstHeldPhase / 8) * 100}%` }}>
              {openGate && (
                <b>GATE {String(gateIndex).padStart(2, "0")} · {held.size} HELD</b>
              )}
            </div>
          </>
        )}

        <svg
          className="map-edges"
          width="100%"
          height={mapHeight}
          style={{ height: mapHeight }}
          aria-hidden="true"
        >
          {edges.map((edge, index) => (
            <path key={index} className={edge.held ? "held" : ""} d={edge.d} />
          ))}
        </svg>

        <div className="map-grid">
          {columns.map(({ phase, nodes: phaseNodes }) => (
            <div className="map-col" key={phase}>
              {phaseNodes.map((node) => (
                <button
                  type="button"
                  key={node.id}
                  ref={(element) => {
                    if (element) nodeRefs.current.set(node.id, element);
                    else nodeRefs.current.delete(node.id);
                  }}
                  className={statusClass(node)}
                  title={`${node.node_key} · ${node.status} · ${node.name}`}
                  onClick={() => onNodeClick(node)}
                >
                  <span className="no">{numbers.get(node.id)}</span>
                  <span className="nm">{node.name}</span>
                </button>
              ))}
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
