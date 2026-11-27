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
  config: Record<string, unknown>;
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
  order_index: number;
  run_status: string;
  upstream: NodeDownstreamRef[];
  downstream: NodeDownstreamRef[];
  open_gate: NodeGateRef | null;
  can_retry: boolean;
  can_reset: boolean;
  can_skip: boolean;
  can_feedback: boolean;
  can_edit_config: boolean;
  events: StreamEvent[];
  artifact_kinds: string[];
};

/** 上下游节点的轻量引用（GET /nodes/{id}）。 */
export interface NodeDownstreamRef {
  node_key: string;
  name: string;
  status: string;
}

/** 节点当前挂起的人工门（GET /nodes/{id}）；无门时为 null。 */
export interface NodeGateRef {
  id: number;
  node_id: number;
  node_key: string;
  status: string;
}

/** POST /nodes/{id}/retry 请求体。expected_status 用于幂等保护。 */
export interface RetryNodeBody {
  feedback?: string;
  reset_downstream?: boolean;
  expected_status?: string;
}

/** POST /nodes/{id}/reset 请求体。 */
export interface ResetNodeBody {
  reset_downstream?: boolean;
  expected_status?: string;
}

/** POST /nodes/{id}/skip 请求体。 */
export interface SkipNodeBody {
  reason?: string;
  cascade_downstream?: boolean;
  expected_status?: string;
}

/** 节点动作端点的统一响应（feedback 响应没有 run_id）。 */
export interface NodeActionResponse {
  node_id: number;
  status: string;
  run_id?: string;
  reset_nodes?: string[];
  closed_gates?: number;
  feedback?: string;
}

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
  kind: string;
  payload: Record<string, unknown>;
  decision: Record<string, unknown>;
}

/** `/gates?run_id=` 的精简列表项（与快照里的门不是同一份形状）。 */
export interface GateListItem {
  id: number;
  run_id: string;
  node_id: number;
  status: string;
  feedback: string | null;
  decided_by: string | null;
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
  builtin: boolean;
  meta: { description?: string; tags?: string[]; meeting_roles?: string[] };
}

/* ---------- 会议式前半段：会议、确认点、参数表 ---------- */

export interface MeetingSummary {
  id: number;
  run_id: string;
  node_id: number;
  node_key: string;
  protocol: string;
  status: string;
  phase: string;
  round: number;
}

export interface Participant {
  id: number;
  meeting_id: number;
  role_key: string | null;
  display_name: string;
  mode: string;
  system_prompt: string;
  model_id: number | null;
  source: string;
  expertise: string[];
  status: string;
  position: number;
}

export interface ParticipantIn {
  role_key?: string | null;
  display_name: string;
  persona?: string;
  model_id?: number | null;
  mode?: string;
}

export interface MeetingTurn {
  id: number;
  seq: number;
  round: number;
  phase: string;
  speaker_id: number | null;
  speaker_name: string;
  kind: string;
  decision_point_id: number | null;
  content: string;
  structured: Record<string, unknown>;
  reply_to: number | null;
  tokens_in: number;
  tokens_out: number;
  cached_tokens: number;
}

export interface DecisionPoint {
  id: number;
  meeting_id: number;
  index: number;
  title: string;
  question: string;
  options: string[];
  raised_by: string | null;
  status: string;
  conclusion: string | null;
  rationale: string | null;
  exchange_summary: string | null;
  stances: Record<string, unknown>;
  origin_round: number | null;
  closed_round: number | null;
  superseded_by: number | null;
}

export interface MeetingDetail {
  meeting: MeetingSummary;
  participants: Participant[];
  decision_points: DecisionPoint[];
  checkpoints: Gate[];
  turn_count: number;
  node_status: string | null;
}

export interface ParamSpec {
  type: "int" | "float" | "bool" | "str";
  default: number | string | boolean;
  min?: number;
  max?: number;
  choices?: string[];
  title: string;
  recommend?: string;
  hint?: string;
}

export interface TemplateNode {
  key: string;
  type: string;
  name?: string;
  phase?: string;
  deps?: string[];
  config?: Record<string, unknown>;
}

export interface TemplateDefinition {
  key: string;
  name: string;
  phases: Array<{ key: string; name: string }>;
  params: Record<string, Record<string, ParamSpec>>;
  automation_levels: Record<string, Record<string, string>>;
  rubrics: Record<string, string>;
  nodes: TemplateNode[];
}

export interface RunConfig {
  automation?: { level?: string; checkpoints?: Record<string, string> };
  params?: Record<string, Record<string, unknown>>;
  rosters?: Record<string, { role_keys?: string[]; count?: number }>;
}

export interface MaterialIn {
  name?: string;
  content: string;
}

export interface MaterialRecord {
  name: string;
  chars: number;
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

export interface PlanEvent {
  seq: number;
  type: "plan_token" | "plan_generated" | "plan_failed";
  payload: { token?: string; status?: string; plan_id?: string; error?: string; [key: string]: unknown };
}

/** 缁熻鏃堕棿绐楋細鍚庣鎸?runs.created_at銆乁TC 鑷劧鏃ュ垏妗躲€?*/
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
  /** false = 褰撳墠鑼冨洿鍐呮病鏈夈€岄厤浜嗗崟浠蜂笖鎴愪氦杩囥€嶇殑妯″瀷锛氬彧灞曠ず绌虹姸鎬侊紝涓嶅睍绀?0 鍏冪粨璁恒€?*/
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

