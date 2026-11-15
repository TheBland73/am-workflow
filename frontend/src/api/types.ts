export type RunStatus =
  | "created" | "running" | "waiting_human" | "finished" | "failed" | "stopped";

export interface Run {
  id: string;
  workflow_id: number;
  status: RunStatus;
  current_phase: string | null;
  idea: string;
  created_at: string | null;
  finished_at: string | null;
  budget_tokens: number;
  error: string | null;
  nodes_total?: number;
  nodes_done?: number;
}

export interface NodeAttemptInfo {
  attempt_no: number;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  tokens_in: number;
  tokens_out: number;
  error: string | null;
}

export interface Node {
  id: number;
  node_key: string;
  type: string;
  name: string;
  phase: string;
  status: string;
  assignee: Record<string, unknown>;
  deps: string[];
  wave: number | null;
  max_attempts: number;
  attempts: number;
  config: Record<string, unknown>;
  attempt?: NodeAttemptInfo | null;
}

export interface NodeAttemptDetail {
  id: number;
  attempt_no: number;
  model_id: number | null;
  status: string;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  tokens_in: number;
  tokens_out: number;
}

export interface NodeMessage {
  role: string;
  content: string;
  tool_call_id?: string | null;
}

export type NodeDetail = Omit<Node, "attempts"> & {
  attempts: NodeAttemptDetail[];
  messages: NodeMessage[];
};

export interface Task {
  task_uid: string;
  title: string;
  deps: string[];
  design_section?: string | null;
  test_cases: string[];
  assignee: Record<string, unknown>;
  wave: number | null;
  status: string;
}

export interface Gate {
  id: number;
  node_id: number;
  node_key: string;
  status: string;
  feedback: string | null;
  decided_by: string | null;
  created_at?: string | null;
  decided_at: string | null;
}

export interface ArtifactRecord {
  kind: string;
  version: number;
  path: string;
  status: string;
  created_by_node: string | null;
}

export interface Usage {
  tokens_in: number;
  tokens_out: number;
  total: number;
  remaining: number;
  budget: number;
}

export interface Snapshot {
  run: Run;
  workflow: { key: string; name: string; phases: string[] };
  phase_names: Record<string, string>;
  nodes: Node[];
  tasks: Task[];
  gates: Gate[];
  artifacts: ArtifactRecord[];
  usage: Usage;
  stats: { events: number; event_types: number; tool_calls: number };
}

export interface StreamEvent {
  seq: number | null;
  type: string;
  node_id: number | null;
  ts: string;
  payload: Record<string, unknown>;
}

export interface Provider {
  id: number;
  name: string;
  protocol: string;
  base_url: string;
  api_key_masked: string;
  has_key: boolean;
  extra_headers: Record<string, string>;
  timeout_s: number;
  max_concurrency: number;
  enabled: boolean;
  models_count: number;
}

export interface ModelEntry {
  id: number;
  provider_id: number;
  model_name: string;
  display_name: string;
  context_window: number;
  max_output_tokens: number;
  cap_tools: boolean;
  cap_vision: boolean;
  cap_json: boolean;
  price_in: number | null;
  price_out: number | null;
  enabled: boolean;
}

export interface AgentTemplate {
  id: number;
  role_key: string;
  name: string;
  system_prompt: string;
  tools_json: string[];
  memory_scope: string;
  default_model_id: number | null;
  max_steps: number;
}

export interface Preset {
  key: string;
  name: string;
  protocol: string;
  base_url: string;
  models: Array<{
    model_name: string;
    display_name: string;
    context_window: number;
    max_output_tokens: number;
    cap_tools?: boolean;
    cap_json?: boolean;
  }>;
}
