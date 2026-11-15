import { create } from "zustand";
import type { StreamEvent } from "../api/types";

export interface LogEntry {
  id: string;
  seq: number | null;
  ts: string;
  type: string;
  nodeKey?: string;
  level: "info" | "warn" | "error";
  text: string;
}

interface RunStore {
  runId: string | null;
  logs: LogEntry[];
  streams: Record<string, string>;
  lastSeq: number;
  reset: (runId: string) => void;
  push: (event: StreamEvent, nodeKey?: string) => void;
  pushHistory: (events: StreamEvent[], nodeKeyOf: (id: number | null) => string | undefined) => void;
}

let counter = 0;
const nextId = () => `l${++counter}`;

export const useRunStore = create<RunStore>((set, get) => ({
  runId: null,
  logs: [],
  streams: {},
  lastSeq: 0,
  reset: (runId) => set({ runId, logs: [], streams: {}, lastSeq: 0 }),
  push: (event, nodeKey) => {
    const payload = event.payload || {};
    if (event.type === "node_token") {
      const text = String(payload.text ?? "");
      if (!text) return;
      const streams = { ...get().streams };
      const key = nodeKey || String(event.node_id);
      streams[key] = (streams[key] || "") + text;
      set({ streams });
      return;
    }
    let text = "";
    let level: LogEntry["level"] = "info";
    switch (event.type) {
      case "node_log":
        text = String(payload.message ?? "");
        level = (payload.level as LogEntry["level"]) || "info";
        break;
      case "node_started":
        text = `开始执行（第 ${payload.attempt ?? 1} 次尝试）`;
        break;
      case "node_finished":
        text = `完成${payload.duration_s ? `，耗时 ${payload.duration_s}s` : ""}`;
        break;
      case "node_failed":
        text = `失败：${String(payload.error ?? "")}`;
        level = "error";
        break;
      case "node_tool_call":
        text = `调用工具 ${String(payload.name ?? "")} ${JSON.stringify(payload.args ?? {})}`;
        break;
      case "node_tool_result":
        text = `工具返回${payload.is_error ? "（错误）" : ""}：${String(payload.output ?? "").slice(0, 1200)}`;
        level = payload.is_error ? "warn" : "info";
        break;
      case "gate_requested":
        text = `请求人工审批：${String(payload.title ?? payload.reason ?? "")}`;
        level = "warn";
        break;
      case "gate_decided":
        text = `人工审批${payload.status === "approved" ? "通过" : "驳回"}${
          payload.feedback ? `：${String(payload.feedback)}` : ""
        }`;
        break;
      case "nodes_reset":
        text = `重置节点：${(payload.nodes as string[] | undefined)?.join(", ") ?? ""}`;
        level = "warn";
        break;
      case "phase_entered":
        text = `进入阶段 ${String(payload.phase ?? "")}`;
        break;
      case "artifact_written":
        text = `写入工件 ${String(payload.kind ?? "")} v${String(payload.version ?? "")}`;
        break;
      case "task_created":
        text = `创建任务 ${String(payload.task_uid ?? "")} ${String(payload.title ?? "")}`;
        break;
      case "task_updated":
        text = `更新任务 ${String(payload.task_uid ?? "")} → ${String(payload.status ?? "")}`;
        break;
      case "run_recovered":
        text = `进程恢复，重置 ${String(payload.reset_nodes ?? 0)} 个未完成节点`;
        level = "warn";
        break;
      case "run_finished":
        text = "运行完成";
        break;
      case "run_error":
        text = `运行失败：${String(payload.error ?? "")}`;
        level = "error";
        break;
      case "run_stopped":
        text = "运行已停止";
        level = "warn";
        break;
      default:
        text = event.type;
    }
    if (!text) return;
    const entry: LogEntry = {
      id: nextId(),
      seq: event.seq,
      ts: event.ts,
      type: event.type,
      nodeKey,
      level,
      text,
    };
    const logs = [...get().logs, entry];
    set({
      logs: logs.length > 4000 ? logs.slice(logs.length - 4000) : logs,
      lastSeq: event.seq ?? get().lastSeq,
    });
  },
  pushHistory: (events, nodeKeyOf) => {
    const state = get();
    state.logs = [];
    state.streams = {};
    events.forEach((event) => state.push(event, nodeKeyOf(event.node_id)));
  },
}));
