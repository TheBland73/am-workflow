import type { Gate, Node } from "../api/types";

/**
 * 运行图 / 计划图共用的纯函数（公共模块 2，契约 §6）。
 *
 * 只做图结构计算，不依赖 React / DOM / xyflow；DOM 测量与连线仍留在
 * ExecutionMap。调用方：ExecutionMap.tsx、RunPage.tsx、PlanGraph.tsx。
 */

/** 完成态：done | skipped。 */
export function isDone(status: string): boolean {
  return status === "done" || status === "skipped";
}

/** node_key → 直接子女 node_key 列表（按 nodes 顺序）。 */
export function buildChildren(nodes: Node[]): Map<string, string[]> {
  const children = new Map<string, string[]>();
  for (const node of nodes) {
    for (const dep of node.deps) {
      const bucket = children.get(dep);
      if (bucket) bucket.push(node.node_key);
      else children.set(dep, [node.node_key]);
    }
  }
  return children;
}

/**
 * 泛型血缘：选中项的祖先 + 自身 + 后代。
 * 键由 keyOf 决定（运行图用 node_key，计划图用 task_uid）；选中项不存在时返回空集。
 */
export function collectLineageByKey<T extends { deps: string[] }>(
  selected: string | null,
  items: T[],
  keyOf: (item: T) => string,
): Set<string> {
  if (!selected) return new Set<string>();
  const byKey = new Map(items.map((item) => [keyOf(item), item]));
  if (!byKey.has(selected)) return new Set<string>();

  const children = new Map<string, string[]>();
  for (const item of items) {
    const key = keyOf(item);
    for (const dep of item.deps) {
      if (!byKey.has(dep)) continue;
      const bucket = children.get(dep);
      if (bucket) bucket.push(key);
      else children.set(dep, [key]);
    }
  }

  const lineage = new Set<string>([selected]);
  const walk = (neighbours: (key: string) => string[]) => {
    const stack = [selected];
    while (stack.length) {
      const current = stack.pop()!;
      for (const next of neighbours(current)) {
        if (lineage.has(next) || !byKey.has(next)) continue;
        lineage.add(next);
        stack.push(next);
      }
    }
  };
  walk((key) => byKey.get(key)?.deps ?? []);
  walk((key) => children.get(key) ?? []);
  return lineage;
}

/** 运行图血缘（键为 node_key）：祖先 + 自身 + 后代。 */
export function collectLineage(selected: string | null, nodes: Node[]): Set<string> {
  return collectLineageByKey(selected, nodes, (node) => node.node_key);
}

/** 门下游尚未完成的节点（node_key），用于 ExecutionMap 的 held 标记与 RunPage 的暂停计数。 */
export function heldKeys(nodes: Node[], gate: Gate | null): Set<string> {
  const result = new Set<string>();
  const gateNode = nodes.find((node) => node.id === gate?.node_id);
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
  // 被门直接或间接挡住、但本身已完成的节点不算「暂停」。
  for (const key of [...result]) {
    const node = byKey.get(key);
    if (!node || isDone(node.status)) result.delete(key);
  }
  return result;
}

/** 按 workflow.phases 顺序分列：每列是该相位的节点。 */
export function phaseColumns(
  nodes: Node[],
  phases: string[],
): { phase: string; nodes: Node[] }[] {
  return phases.map((phase) => ({
    phase,
    nodes: nodes.filter((node) => node.phase === phase),
  }));
}

/** 节点显示编号：普通节点 01/02…，人工门 G1/G2… */
export function nodeNumbers(nodes: Node[]): Map<number, string> {
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
}

/**
 * 预留：运行中允许编辑配置的节点状态。
 * v1 不开放运行中改配置，最终以后端 can_edit_config 为准；这里固化「未开始 / 等待人工」的保守判断。
 */
export function isEditableWhileRunning(status: string): boolean {
  return status === "pending" || status === "waiting_human";
}
