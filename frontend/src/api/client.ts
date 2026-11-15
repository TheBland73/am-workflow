import type {
  AgentTemplate, ArtifactRecord, Gate, ModelEntry, PlanDetail, PlanEvent, PlanListItem,
  PlanTask, Preset, Provider, Run, Snapshot, StreamEvent, Task,
} from "./types";

const BASE = import.meta.env.VITE_API_BASE || "/api";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { ...(init.headers as Record<string, string>) };
  if (init.body) headers["Content-Type"] = "application/json";
  const resp = await fetch(`${BASE}${path}`, { ...init, headers });
  if (resp.status === 204) return undefined as T;
  const text = await resp.text();
  let data: unknown = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!resp.ok) {
    const detail = (data as { detail?: unknown })?.detail;
    throw new ApiError(resp.status, typeof detail === "string" ? detail : text || resp.statusText);
  }
  return data as T;
}

export const api = {
  health: () => request<{ ok: boolean; app: string }>("/health"),

  listRuns: () => request<{ items: Run[] }>("/runs"),
  createRun: (idea: string, templateKey = "sdlc_v1", budgetTokens?: number) =>
    request<Snapshot>("/runs", {
      method: "POST",
      body: JSON.stringify({ idea, template_key: templateKey, budget_tokens: budgetTokens }),
    }),
  getRun: (runId: string) => request<Snapshot>(`/runs/${runId}`),
  stopRun: (runId: string) => request<{ id: string; status: string }>(`/runs/${runId}/stop`),

  history: (runId: string, since = 0) =>
    request<{ items: StreamEvent[] }>(`/runs/${runId}/events/history?since=${since}`),

  decideGate: (
    gateId: number,
    action: "approve" | "reject",
    body: {
      feedback?: string; target_node?: string; decided_by?: string;
      artifact_kind?: string; artifact_body?: string;
    },
  ) => request<Record<string, unknown>>(`/gates/${gateId}/${action}`, {
    method: "POST", body: JSON.stringify(body),
  }),
  gates: (runId: string) => request<{ items: Gate[] }>(`/gates?run_id=${runId}`),

  artifacts: (runId: string) =>
    request<{ items: ArtifactRecord[] }>(`/runs/${runId}/artifacts`),
  getArtifact: (runId: string, kind: string) =>
    request<{ kind: string; meta: Record<string, unknown>; body: string }>(
      `/runs/${runId}/artifacts/${kind}`,
    ),
  patchArtifact: (runId: string, kind: string, body: string, status = "reviewed") =>
    request<ArtifactRecord>(`/runs/${runId}/artifacts/${kind}`, {
      method: "PATCH", body: JSON.stringify({ body, status }),
    }),

  tasks: (runId: string) => request<{ items: Task[] }>(`/runs/${runId}/tasks`),
  createTask: (runId: string, body: Partial<Task> & { task_uid: string; title: string }) =>
    request<Task>(`/runs/${runId}/tasks`, { method: "POST", body: JSON.stringify(body) }),
  patchTask: (runId: string, uid: string, body: Partial<Task>) =>
    request<Task>(`/runs/${runId}/tasks/${uid}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteTask: (runId: string, uid: string) =>
    request<void>(`/runs/${runId}/tasks/${uid}`, { method: "DELETE" }),

  getNode: (nodeId: number) => request<import("./types").NodeDetail>(`/nodes/${nodeId}`),
  retryNode: (nodeId: number) =>
    request<Record<string, unknown>>(`/nodes/${nodeId}/retry`, { method: "POST" }),

  presets: () => request<{ presets: Preset[] }>("/presets"),
  providers: () => request<{ items: Provider[] }>("/providers"),
  createProvider: (body: Record<string, unknown>) =>
    request<Provider>("/providers", { method: "POST", body: JSON.stringify(body) }),
  patchProvider: (id: number, body: Record<string, unknown>) =>
    request<Provider>(`/providers/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteProvider: (id: number) =>
    request<void>(`/providers/${id}`, { method: "DELETE" }),
  testProvider: (id: number, modelId?: number) =>
    request<{ ok: boolean; message: string }>(
      `/providers/${id}/test${modelId ? `?model_id=${modelId}` : ""}`,
      { method: "POST" },
    ),

  models: (providerId?: number) =>
    request<{ items: ModelEntry[] }>(`/models${providerId ? `?provider_id=${providerId}` : ""}`),
  createModel: (body: Record<string, unknown>) =>
    request<ModelEntry>("/models", { method: "POST", body: JSON.stringify(body) }),
  patchModel: (id: number, body: Record<string, unknown>) =>
    request<ModelEntry>(`/models/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteModel: (id: number) => request<void>(`/models/${id}`, { method: "DELETE" }),

  templates: () => request<{ items: AgentTemplate[] }>("/agent-templates"),
  patchTemplate: (id: number, body: Record<string, unknown>) =>
    request<AgentTemplate>(`/agent-templates/${id}`, { method: "PATCH", body: JSON.stringify(body) }),

  /* ---------- 编排工作台（ORCH-01） ---------- */
  plans: (status?: string) =>
    request<{ items: PlanListItem[] }>(`/plans${status ? `?status=${status}` : ""}`),
  createPlan: (body: {
    product_name: string; source_type: "text" | "upload" | "artifact";
    source_ref?: string; design_text?: string;
  }) => request<PlanDetail>("/plans", { method: "POST", body: JSON.stringify(body) }),
  getPlan: (planId: string) => request<PlanDetail>(`/plans/${planId}`),
  generatePlan: (planId: string) =>
    request<{ id: string; status: string }>(`/plans/${planId}/generate`, { method: "POST" }),
  cancelPlan: (planId: string) =>
    request<PlanDetail>(`/plans/${planId}/cancel`, { method: "POST" }),
  validatePlan: (planId: string) =>
    request<PlanDetail>(`/plans/${planId}/validate`, { method: "POST" }),
  patchPlanTask: (planId: string, uid: string, body: Record<string, unknown>) =>
    request<PlanTask>(`/plans/${planId}/tasks/${uid}`, {
      method: "PATCH", body: JSON.stringify(body),
    }),
  createPlanTask: (planId: string, body: Record<string, unknown>) =>
    request<PlanTask>(`/plans/${planId}/tasks`, { method: "POST", body: JSON.stringify(body) }),
  deletePlanTask: (planId: string, uid: string) =>
    request<{ removed_from: string[] }>(`/plans/${planId}/tasks/${uid}`, { method: "DELETE" }),
  commitPlan: (planId: string, mode: "save" | "run") =>
    request<{ plan: PlanDetail["plan"]; run_id?: string | null; run?: Run | null }>(
      `/plans/${planId}/commit`, { method: "POST", body: JSON.stringify({ mode }) },
    ),
  archivePlan: (planId: string) =>
    request<PlanDetail>(`/plans/${planId}/archive`, { method: "POST" }),
  planSnapshots: (planId: string) =>
    request<{ items: Array<{ version: number; metrics: Record<string, number>; is_valid: boolean; validation: unknown; created_at: string | null }> }>(
      `/plans/${planId}/snapshots`,
    ),
};

/** 订阅 run 的 SSE；返回取消订阅函数。事件类型逐个注册（后端用的是命名事件）。 */
export function subscribeRun(
  runId: string,
  onEvent: (event: StreamEvent) => void,
  since = 0,
): () => void {
  const source = new EventSource(`${BASE}/runs/${runId}/events?since=${since}`);
  const types = [
    "run_created", "run_finished", "run_error", "run_stopped", "run_recovered",
    "phase_entered", "node_started", "node_finished", "node_failed", "node_log",
    "node_token", "node_tool_call", "node_tool_result", "node_retried", "nodes_reset",
    "artifact_written", "task_created", "task_updated", "gate_requested", "gate_decided",
    "memory_written",
  ];
  const handler = (raw: MessageEvent) => {
    try {
      onEvent(JSON.parse(raw.data) as StreamEvent);
    } catch {
      // 忽略非法帧
    }
  };
  types.forEach((type) => source.addEventListener(type, handler as EventListener));
  source.onerror = () => {
    // EventSource 自带重连；后端关闭终态连接后浏览器会重试，这里不主动报错。
  };
  return () => source.close();
}

/** 订阅 plan 生成事件（plan_token / plan_generated / plan_failed）。 */
export function subscribePlan(
  planId: string,
  onEvent: (event: PlanEvent) => void,
): () => void {
  const source = new EventSource(`${BASE}/plans/${planId}/events`);
  const handler = (raw: MessageEvent) => {
    try {
      onEvent(JSON.parse(raw.data) as PlanEvent);
    } catch {
      // 忽略非法帧
    }
  };
  ["plan_token", "plan_generated", "plan_failed"].forEach((type) =>
    source.addEventListener(type, handler as EventListener),
  );
  return () => source.close();
}
