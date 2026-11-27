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
  title?: string | null;
  description?: string | null;
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

export interface NodeMetric {
  node_key: string;
  type: string;
  name: string;
  phase: string;
  status: string;
  attempts: number;
  retries: number;
  exec_ms: number;
  tokens_in: number;
  tokens_out: number;
  last_error: string | null;
}

export type ReviewSeverity = "blocker" | "major" | "minor";

export interface ReviewIssue {
  severity: ReviewSeverity;
  task_uid: string;
  file: string;
  message: string;
  suggestion: string;
}

export interface ReviewHistoryItem {
  round: number;
  verdict: string;
  issues?: number;
  blocking?: number;
  at?: string;
  summary?: string;
}

export interface ReviewNodeMetric {
  node_key: string;
  name: string;
  verdict: string | null;
  round: number;
  max_rounds: number;
  issues: number;
  blocking: number;
  summary: string;
  reviewed_at: string | null;
  attempts: number;
  exec_ms: number;
  blocks: number;
  history: ReviewHistoryItem[];
}

export interface ReviewMetrics {
  total: number;
  verdict: string | null;
  round: number;
  max_rounds: number;
  first_pass: boolean;
  rework_count: number;
  blocks: number;
  issues: number;
  blocking: number;
  severities: Record<string, number>;
  avg_review_ms: number;
  nodes: ReviewNodeMetric[];
}

export interface RunReview {
  available: boolean;
  run_id: string;
  run_status: string;
  node_id?: number;
  node_key?: string;
  node_status?: string;
  verdict?: string | null;
  summary?: string | null;
  issues: ReviewIssue[];
  blocking: number;
  targets: string[];
  round: number;
  max_rounds: number;
  reviewed_at?: string | null;
  files: string[];
  history: ReviewHistoryItem[];
  escalated: boolean;
  can_rework: boolean;
  artifact?: ArtifactRecord | null;
  body?: string;
}

export interface RunMetrics {
  run_id: string;
  status: string;
  wall_ms: number;
  nodes: Record<string, number>;
  execution: {
    attempts: number;
    retries: number;
    busy_ms: number;
    avg_attempt_ms: number;
    p95_attempt_ms: number;
    max_attempt_ms: number;
    stale_attempts: number;
  };
  usage: {
    tokens_in: number;
    tokens_out: number;
    total: number;
    estimated_cost: number;
  };
  gates: {
    total: number;
    open: number;
    approved: number;
    rejected: number;
    avg_decision_ms: number;
    open_wait_ms: number;
  };
  events: number;
  per_node: NodeMetric[];
  review?: ReviewMetrics;
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
  models_created?: number;
}

export interface RemoteModel {
  id: string;
  owned_by: string;
  created: number;
}

export interface TestResult {
  ok: boolean;
  message: string;
  latency_ms?: number;
  reply?: string;
  models?: RemoteModel[];
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

/* ---------- 编排工作台（ORCH-01） ---------- */

export type PlanStatus =
  | "draft" | "generating" | "generated" | "validating"
  | "valid" | "invalid" | "editing" | "committed" | "archived" | "failed";

export type PlanSourceType = "artifact" | "upload" | "text";
export type TaskRole = "dev" | "test" | "design" | "pm" | "review" | "ops";
export type TaskEffort = "S" | "M" | "L" | "XL";
export type TaskPriority = "P0" | "P1" | "P2";

export interface PlanSection {
  level: number;
  title: string;
  line: number;
  key: string;
}

export interface PlanTask {
  id: number;
  task_uid: string;
  title: string;
  description: string;
  deps: string[];
  design_section: string;
  acceptance: string[];
  role_key: TaskRole;
  effort: TaskEffort;
  priority: TaskPriority;
  order_index: number;
}

export interface PlanMetrics {
  task_count?: number;
  wave_count?: number;
  max_parallel?: number;
  critical_path_tasks?: number;
  critical_path_points?: number;
  total_points?: number;
  role_distribution?: Record<string, number>;
}

export interface PlanValidationError {
  code: string;
  message: string;
  task_uid?: string | null;
  hint?: string;
  details?: Record<string, unknown>;
}

export interface PlanValidation {
  ok: boolean;
  errors: PlanValidationError[];
  warnings?: PlanValidationError[];
}

export interface PlanCoverageSection {
  title: string;
  key: string;
  level: number;
  line: number;
  tasks: string[];
}

export interface PlanCoverage {
  total: number;
  covered: number;
  ratio: number;
  sections: PlanCoverageSection[];
  uncovered: string[];
  orphan_tasks: string[];
}

export interface PlanDagEdge {
  from: string;
  to: string;
}

export interface PlanSnapshot {
  version: number;
  metrics: PlanMetrics;
  waves: Record<string, string[]>;
  critical_path: string[];
  dag: { nodes?: string[]; edges?: PlanDagEdge[] };
  coverage?: PlanCoverage;
  validation: PlanValidation;
  is_valid: boolean;
  created_at: string | null;
}

export interface PlanPoint {
  x: number;
  y: number;
}

export interface PlanHeader {
  id: string;
  product_name: string;
  source_type: PlanSourceType;
  source_ref: string | null;
  status: PlanStatus;
  model_id: number | null;
  model_name?: string | null;
  error: string | null;
  run_id: string | null;
  created_at: string | null;
  updated_at: string | null;
  design_text?: string;
  raw_output?: string | null;
  sections: PlanSection[];
  budget_tokens?: number;
  layout?: Record<string, PlanPoint>;
}

export interface PlanDetail {
  plan: PlanHeader;
  tasks: PlanTask[];
  snapshot: PlanSnapshot | null;
}

export type PlanPhaseEdgeKind = "critical" | "handoff" | "normal";

export interface PlanPhaseTask {
  uid: string;
  title: string;
  role: TaskRole;
  effort: TaskEffort;
  priority: TaskPriority;
  critical: boolean;
  deps: string[];
}

export interface PlanPhase {
  index: number;
  title: string;
  key: string;
  line: number;
  task_count: number;
  roles: Partial<Record<TaskRole, number>>;
  points: number;
  cross_links: number;
  tasks: PlanPhaseTask[];
}

export interface PlanPhaseEdge {
  from: string;
  to: string;
  kind: PlanPhaseEdgeKind;
  cross: boolean;
}

export interface PlanPhases {
  plan_id: string;
  product_name: string;
  status: PlanStatus;
  total_tasks: number;
  phase_count: number;
  total_sections: number;
  covered_sections: number;
  coverage_ratio: number;
  uncovered: string[];
  orphan_tasks: string[];
  critical_path: string[];
  edges: PlanPhaseEdge[];
  phases: PlanPhase[];
}

export type PlanActivityKind = "created" | "generate" | "validate" | "commit" | "archive";

export interface PlanActivityItem {
  at: string | null;
  kind: PlanActivityKind;
  title: string;
  detail: string;
  version?: number | null;
  ok?: boolean | null;
  errors?: string[];
  warnings?: string[];
  stats?: { tasks: number; sections: number; covered: number; points: number };
  run_id?: string | null;
}

export interface PlanActivity {
  plan_id: string;
  items: PlanActivityItem[];
}

export interface PlanListItem {
  id: string;
  product_name: string;
  source_type: PlanSourceType;
  status: PlanStatus;
  error: string | null;
  run_id: string | null;
  task_count: number;
  metrics: PlanMetrics;
  updated_at: string | null;
}

/* ---------- 数据统计（ANALYTICS） ---------- */

/** 统计时间窗：后端按 runs.created_at、UTC 自然日切桶。 */
export interface AnalyticsWindow {
  days: number;
  start: string;
  end: string;
  start_date: string;
  end_date: string;
  basis: string;
  timezone: string;
}

export type AnalyticsRange = 7 | 30 | 90;

export interface AnalyticsCostModel {
  model_id: number | null;
  model_name: string;
  provider: string;
  cost: number;
  total_tokens: number;
}

export interface AnalyticsCost {
  /** false = 当前范围内没有「配了单价且成交过」的模型：只展示空状态，不展示 0 元结论。 */
  available: boolean;
  currency: string | null;
  priced_models: number;
  note: string;
  estimated_cost: number;
  unpriced_tokens: number;
  by_model?: AnalyticsCostModel[];
}

export interface AnalyticsOverview {
  window: AnalyticsWindow;
  runs: {
    total: number;
    finished: number;
    failed: number;
    running: number;
    waiting_human: number;
    stopped: number;
    created: number;
  };
  success_rate: number;
  duration: {
    avg_ms: number | null;
    min_ms: number | null;
    max_ms: number | null;
    total_ms: number;
    samples: number;
    basis: string;
  };
  tokens: { input: number; output: number; total: number };
  cost: AnalyticsCost;
}

export interface AnalyticsTrendPoint {
  date: string;
  runs: number;
  finished: number;
  failed: number;
  tokens_in: number;
  tokens_out: number;
  total_tokens: number;
}

export interface AnalyticsTrends {
  window: AnalyticsWindow;
  items: AnalyticsTrendPoint[];
  totals: {
    runs: number;
    finished: number;
    failed: number;
    tokens_in: number;
    tokens_out: number;
    total_tokens: number;
  };
}

export interface AnalyticsStatusItem {
  status: string;
  count: number;
  ratio: number;
}

export interface AnalyticsDistribution {
  window: AnalyticsWindow;
  total: number;
  items: AnalyticsStatusItem[];
}

export interface AnalyticsModelUsage {
  model_id: number | null;
  model_name: string;
  provider: string;
  tokens_in: number;
  tokens_out: number;
  total: number;
  attempts: number;
  cost: number;
  ratio: number;
}

export interface AnalyticsUsage {
  window: AnalyticsWindow;
  tokens: { input: number; output: number; total: number };
  by_model: AnalyticsModelUsage[];
  cost: AnalyticsCost;
}

export interface AnalyticsRunItem {
  id: string;
  idea: string;
  status: RunStatus;
  current_phase: string | null;
  created_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  tokens_in: number;
  tokens_out: number;
  total_tokens: number;
  cost: number;
  cost_available: boolean;
  nodes_total: number;
  nodes_done: number;
  tasks_total: number;
  tasks_done: number;
  error: string | null;
}

export interface AnalyticsRecentRuns {
  window: AnalyticsWindow;
  limit: number;
  status: string | null;
  items: AnalyticsRunItem[];
}

export interface AnalyticsFailedItem {
  id: string;
  idea: string;
  status: RunStatus;
  current_phase: string | null;
  created_at: string | null;
  finished_at: string | null;
  failed_at: string | null;
  error: string;
  error_truncated: boolean;
  node_key: string | null;
  node_name: string | null;
  node_phase: string | null;
  node_error: string | null;
}

export interface AnalyticsFailedRuns {
  window: AnalyticsWindow;
  limit: number;
  items: AnalyticsFailedItem[];
}

export interface PlanEvent {
  seq: number;
  type: "plan_token" | "plan_generated" | "plan_failed";
  payload: { token?: string; status?: string; plan_id?: string; error?: string; [key: string]: unknown };
}
