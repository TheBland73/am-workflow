import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  Alert, Button, Checkbox, Drawer, Empty, Input, Modal, Spin, Table, Tag, message,
} from "antd";
import type {
  ArtifactRecord, Node, NodeAttemptDetail, NodeDetail, StreamEvent,
} from "../api/types";

/** 节点动作种类（契约 §4）。 */
export type NodeActionKind = "retry" | "reset" | "skip" | "feedback";

/**
 * NodeInspector → RunPage 的动作请求：面板只负责收集参数与二次确认，
 * 真正调用 API、刷新快照的动作由 RunPage 执行（契约 §8）。
 */
export interface NodeActionRequest {
  kind: NodeActionKind;
  feedback?: string;
  resetDownstream?: boolean;
  reason?: string;
  cascadeDownstream?: boolean;
  /** 幂等保护：以当前详情状态作为 expected_status 传给后端（契约 §4）。 */
  expectedStatus?: string;
}

export interface NodeInspectorProps {
  runId: string;
  runStatus: string;
  node: Node | null;
  detail?: NodeDetail;
  /** 详情查询失败时的错误文案（GET /nodes/{id} 失败）。 */
  detailError?: string;
  events: StreamEvent[];
  open: boolean;
  onClose: () => void;
  onAction: (request: NodeActionRequest) => Promise<void>;
  /** 上下游/血缘跳转：把 node_key 交回 RunPage 重新选中。 */
  onSelectNode?: (nodeKey: string) => void;
  /** 「只看此节点事件」：RunPage 打开事件抽屉并加上节点过滤。 */
  onFilterEvents?: (node: Node) => void;
  /** 产出工件点击：复用 RunPage 已有的工件编辑 Modal。 */
  onOpenArtifact?: (record: ArtifactRecord) => void;
  /** snapshot.artifacts 中 created_by_node === node.node_key 的项。 */
  artifacts?: ArtifactRecord[];
  /** 复用 RunPage 的 eventText 文案（避免两套事件文案漂移）。 */
  eventText?: (event: StreamEvent) => string;
}

const STATUS_LABELS: Record<string, string> = {
  created: "已创建", running: "运行中", waiting_human: "等待人工",
  finished: "已完成", failed: "失败", stopped: "已停止",
  pending: "等待", ready: "就绪", done: "完成", skipped: "跳过",
};

const ARTIFACT_LABELS: Record<string, string> = {
  idea: "想法", discussion: "讨论", proposal: "方案", review: "评审",
  requirement: "需求", design: "设计", task: "任务", test: "测试", changelog: "交付日志",
};

/** 契约 §1：config_json 里属于「调度状态」的保护键，面板里灰显只读。 */
const PROTECTED_CONFIG_KEYS = new Set([
  "feedback", "retry_at", "retry_count", "escalation", "escalation_reason",
  "round", "review", "review_history",
]);

const TERMINAL_RUN_STATUS = new Set(["finished", "failed", "stopped"]);

const COLLAPSED_MESSAGES = 3;
const COLLAPSED_EVENTS = 8;
const EXPANDED_EVENTS = 30;

const ACTION_TITLE: Record<NodeActionKind, string> = {
  retry: "重试节点", reset: "重置节点", skip: "跳过节点", feedback: "注入反馈（不重跑）",
};
const ACTION_OK_TEXT: Record<NodeActionKind, string> = {
  retry: "确认重试", reset: "确认重置", skip: "确认跳过", feedback: "注入反馈",
};
const ACTION_SUCCESS: Record<NodeActionKind, string> = {
  retry: "已提交重试，节点将重新执行",
  reset: "节点已重置，调度状态已清理",
  skip: "节点已跳过",
  feedback: "反馈已注入，不会触发重跑",
};

function statusLabel(status: string) {
  return STATUS_LABELS[status] ?? status;
}

function fmtTime(value: string | null | undefined) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return `${date.getFullYear()}.${String(date.getMonth() + 1).padStart(2, "0")}.${String(date.getDate()).padStart(2, "0")} ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

/** 配置值渲染：标量直接可读，对象/数组按 JSON 展示。 */
function valueText(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function Section({
  title, extra, children,
}: {
  title: string;
  extra?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="node-inspector-section">
      <header className="node-inspector-section-head">
        <h4>{title}</h4>
        {extra}
      </header>
      {children}
    </section>
  );
}

export default function NodeInspector({
  runId, runStatus, node, detail, detailError, events, open, onClose, onAction,
  onSelectNode, onFilterEvents, onOpenArtifact, artifacts = [], eventText,
}: NodeInspectorProps) {
  const [messageApi, holder] = message.useMessage();
  const [action, setAction] = useState<NodeActionKind | null>(null);
  const [feedback, setFeedback] = useState("");
  const [skipReason, setSkipReason] = useState("");
  const [resetDownstream, setResetDownstream] = useState(false);
  const [busy, setBusy] = useState(false);
  const [messagesExpanded, setMessagesExpanded] = useState(false);
  const [eventsExpanded, setEventsExpanded] = useState(false);

  // 切换节点时回到折叠态，避免上一个节点的展开状态串到下一个节点。
  useEffect(() => {
    setMessagesExpanded(false);
    setEventsExpanded(false);
  }, [node?.id]);

  // 详情事件（后端最近 N 条）+ 实时 SSE 事件，按 seq 去重后取最近若干条。
  const nodeEvents = useMemo(() => {
    const merged = new Map<string, StreamEvent>();
    const add = (event: StreamEvent, fallbackIndex: number) => {
      if (event.node_id == null) return;
      if (node && event.node_id !== node.id) return;
      const dedupeKey = event.seq != null
        ? `seq:${event.seq}`
        : `${event.type}:${event.ts}:${fallbackIndex}`;
      if (!merged.has(dedupeKey)) merged.set(dedupeKey, event);
    };
    (detail?.events ?? []).forEach(add);
    events.forEach(add);
    const seqOf = (event: StreamEvent) =>
      (typeof event.seq === "number" ? event.seq : Number.MAX_SAFE_INTEGER);
    return [...merged.values()].sort((left, right) => seqOf(left) - seqOf(right));
  }, [detail, events, node]);

  const configEntries = Object.entries(detail?.config ?? {});
  const executionEntries = configEntries.filter(([key]) => !PROTECTED_CONFIG_KEYS.has(key));
  const schedulingEntries = configEntries.filter(([key]) => PROTECTED_CONFIG_KEYS.has(key));
  const upstreamRefs = detail?.upstream ?? [];
  const downstreamRefs = detail?.downstream ?? [];
  const artifactKinds = detail?.artifact_kinds ?? [];
  const produced = detail
    ? artifacts.filter((record) => record.created_by_node === detail.node_key)
    : [];

  const actionHint = (() => {
    if (!detail) return "";
    if (detail.can_retry || detail.can_reset || detail.can_skip || detail.can_feedback) return "";
    if (TERMINAL_RUN_STATUS.has(runStatus)) return "运行已结束，节点动作不可用。";
    if (detail.status === "running") return "节点正在运行，等待本次执行结束后可操作。";
    if (detail.open_gate) return "该节点有人工门待裁决，请先在右栏审批卡处理。";
    return "当前状态没有可用的节点动作。";
  })();

  const openAction = (next: NodeActionKind) => {
    setAction(next);
    setFeedback("");
    setSkipReason("");
    setResetDownstream(next === "reset");
  };

  const submit = async () => {
    if (!action || !detail) return;
    const text = feedback.trim();
    if (action === "feedback" && !text) {
      messageApi.warning("请填写要注入的反馈内容");
      return;
    }
    setBusy(true);
    try {
      await onAction({
        kind: action,
        feedback: action === "retry" || action === "feedback" ? text || undefined : undefined,
        resetDownstream: action === "retry"
          ? resetDownstream
          : action === "reset" ? resetDownstream : undefined,
        reason: action === "skip" ? skipReason.trim() || undefined : undefined,
        expectedStatus: detail.status,
      });
      messageApi.success(ACTION_SUCCESS[action]);
      setAction(null);
    } catch (error) {
      // 失败保留弹窗，方便补填/重试；错误文案来自 ApiError（含后端 code 映射后的 message）。
      messageApi.error((error as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const renderEventText = eventText ?? ((event: StreamEvent) => event.type);
  const visibleEvents = eventsExpanded
    ? nodeEvents.slice(-EXPANDED_EVENTS)
    : nodeEvents.slice(-COLLAPSED_EVENTS);
  const canExpandEvents = nodeEvents.length > COLLAPSED_EVENTS;

  return (
    <>
      {/* Esc 由 RunPage 统一处理（输入框内不拦截）；关掉 Drawer 自带键盘关闭避免和输入冲突。 */}
      <Drawer
        open={open}
        onClose={onClose}
        width={720}
        keyboard={false}
        styles={{ body: { padding: "14px 18px" } }}
        title={node ? `${node.name} · ${node.node_key}` : "节点检查器"}
      >
        {holder}
        {!node ? (
          <Empty description="未选中节点" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ) : (
          <div className="node-inspector">
            <div className="node-inspector-meta">
              <span className="ef-eyebrow">NODE</span>
              <b>{node.name}</b>
              <span className="mono">{node.node_key}</span>
              <Tag>{statusLabel(detail?.status ?? node.status)}</Tag>
              <span>{detail?.phase ?? node.phase}</span>
              <span className="mono">RUN {runId}</span>
              {detail && <span>{statusLabel(detail.run_status)}</span>}
            </div>

            {!detail ? (
              detailError ? (
                <Alert
                  type="error"
                  showIcon
                  message="节点详情读取失败"
                  description={detailError}
                />
              ) : (
                <div className="node-inspector-loading"><Spin /></div>
              )
            ) : (
              <>
                <Section title="基础信息">
                  <dl className="node-inspector-facts">
                    <div><dt>节点键</dt><dd className="mono">{detail.node_key}</dd></div>
                    <div><dt>类型</dt><dd>{detail.type}</dd></div>
                    <div><dt>状态</dt><dd>{statusLabel(detail.status)}</dd></div>
                    <div><dt>相位</dt><dd>{detail.phase}</dd></div>
                    <div><dt>波次</dt><dd>{detail.wave ?? "—"}</dd></div>
                    <div><dt>序号</dt><dd>{detail.order_index}</dd></div>
                    <div><dt>尝试</dt><dd>{detail.attempts.length}/{detail.max_attempts}</dd></div>
                    <div><dt>运行状态</dt><dd>{statusLabel(detail.run_status)}</dd></div>
                    <div><dt>依赖</dt><dd>{detail.deps.length ? detail.deps.join("、") : "无依赖"}</dd></div>
                    <div><dt>人工门</dt><dd>{detail.open_gate ? `${detail.open_gate.node_key} · ${detail.open_gate.status}` : "无"}</dd></div>
                    <div><dt>执行者</dt><dd>{valueText(detail.assignee)}</dd></div>
                  </dl>
                </Section>

                <Section title="上下游">
                  <div className="node-inspector-chain">
                    <div className="node-inspector-chain-group">
                      <span className="node-inspector-chain-label">上游 {upstreamRefs.length}</span>
                      {upstreamRefs.length === 0 ? (
                        <em className="node-inspector-empty">起始节点，无上游</em>
                      ) : (
                        <div className="node-inspector-refs">
                          {upstreamRefs.map((ref) => (
                            <button
                              type="button"
                              key={ref.node_key}
                              className="node-inspector-ref"
                              title={`选中 ${ref.node_key}`}
                              onClick={() => onSelectNode?.(ref.node_key)}
                            >
                              <span className="nm">{ref.name}</span>
                              <span className="key">{ref.node_key}</span>
                              <span className="st">{statusLabel(ref.status)}</span>
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                    <div className="node-inspector-chain-group">
                      <span className="node-inspector-chain-label">下游 {downstreamRefs.length}</span>
                      {downstreamRefs.length === 0 ? (
                        <em className="node-inspector-empty">末端节点，无下游</em>
                      ) : (
                        <div className="node-inspector-refs">
                          {downstreamRefs.map((ref) => (
                            <button
                              type="button"
                              key={ref.node_key}
                              className="node-inspector-ref"
                              title={`选中 ${ref.node_key}`}
                              onClick={() => onSelectNode?.(ref.node_key)}
                            >
                              <span className="nm">{ref.name}</span>
                              <span className="key">{ref.node_key}</span>
                              <span className="st">{statusLabel(ref.status)}</span>
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                </Section>

                <Section title="配置">
                  <div className="node-inspector-config">
                    <div className="node-inspector-config-group">
                      <span className="node-inspector-config-label">执行参数</span>
                      {executionEntries.length === 0 ? (
                        <em className="node-inspector-empty">无执行参数</em>
                      ) : executionEntries.map(([key, value]) => (
                        <div key={key} className="node-inspector-config-row">
                          <span className="k">{key}</span>
                          <span className="v">{valueText(value)}</span>
                        </div>
                      ))}
                    </div>
                    <div className="node-inspector-config-group is-readonly">
                      <span className="node-inspector-config-label">调度状态（只读）</span>
                      {schedulingEntries.length === 0 ? (
                        <em className="node-inspector-empty">无调度状态</em>
                      ) : schedulingEntries.map(([key, value]) => (
                        <div key={key} className="node-inspector-config-row">
                          <span className="k">{key}</span>
                          <span className="v">{valueText(value)}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                </Section>

                <Section
                  title="尝试记录"
                  extra={<span className="node-inspector-count">{detail.attempts.length}/{detail.max_attempts}</span>}
                >
                  {detail.attempts.length === 0 ? (
                    <em className="node-inspector-empty">尚无尝试记录</em>
                  ) : (
                    <Table
                      size="small"
                      rowKey="id"
                      pagination={false}
                      dataSource={detail.attempts}
                      columns={[
                        { title: "#", dataIndex: "attempt_no", width: 40 },
                        { title: "状态", dataIndex: "status", width: 76 },
                        {
                          title: "MODEL",
                          dataIndex: "model_id",
                          width: 64,
                          render: (value: number | null) => (value == null ? "—" : `#${value}`),
                        },
                        {
                          title: "起止",
                          width: 168,
                          render: (_: unknown, row: NodeAttemptDetail) => (
                            <span className="node-inspector-duration">
                              {fmtTime(row.started_at)}
                              <br />
                              {fmtTime(row.finished_at)}
                            </span>
                          ),
                        },
                        {
                          title: "TOKEN",
                          width: 68,
                          render: (_: unknown, row: NodeAttemptDetail) =>
                            (row.tokens_in + row.tokens_out).toLocaleString(),
                        },
                        {
                          title: "ERROR",
                          dataIndex: "error",
                          render: (value: string | null) => (
                            value
                              ? <span title={value}>{`${value.slice(0, 48)}${value.length > 48 ? "…" : ""}`}</span>
                              : "—"
                          ),
                        },
                      ]}
                    />
                  )}
                  {detail.attempts.filter((item) => item.error).length > 0 && (
                    <div className="node-inspector-errors">
                      {detail.attempts.filter((item) => item.error).map((item) => (
                        <pre key={item.id} className="node-inspector-error">
                          <b>#{item.attempt_no}</b> {item.error}
                        </pre>
                      ))}
                    </div>
                  )}
                </Section>

                <Section
                  title="最近消息"
                  extra={detail.messages.length > COLLAPSED_MESSAGES ? (
                    <Button
                      size="small"
                      type="link"
                      onClick={() => setMessagesExpanded((value) => !value)}
                    >
                      {messagesExpanded ? "收起" : `展开全部 ${detail.messages.length} 条`}
                    </Button>
                  ) : null}
                >
                  {detail.messages.length === 0 ? (
                    <em className="node-inspector-empty">暂无消息</em>
                  ) : (
                    <div className="node-inspector-messages">
                      {(messagesExpanded
                        ? detail.messages
                        : detail.messages.slice(-COLLAPSED_MESSAGES)
                      ).map((item, index) => (
                        <div key={`${item.role}-${index}`} className={`node-inspector-msg is-${item.role}`}>
                          <b>{item.role}</b>
                          <pre>{item.content.slice(0, 2000)}</pre>
                        </div>
                      ))}
                    </div>
                  )}
                </Section>

                <Section
                  title="节点事件"
                  extra={(
                    <div className="node-inspector-event-tools">
                      <span className="node-inspector-count">{nodeEvents.length}</span>
                      {canExpandEvents && (
                        <Button
                          size="small"
                          type="link"
                          onClick={() => setEventsExpanded((value) => !value)}
                        >
                          {eventsExpanded ? "收起" : "展开"}
                        </Button>
                      )}
                      {onFilterEvents && (
                        <Button size="small" type="link" onClick={() => onFilterEvents(node)}>
                          只看此节点事件
                        </Button>
                      )}
                    </div>
                  )}
                >
                  {nodeEvents.length === 0 ? (
                    <em className="node-inspector-empty">暂无该节点事件</em>
                  ) : (
                    <div className="node-inspector-events">
                      {visibleEvents.map((event, index) => (
                        <div key={`${event.seq ?? event.ts}-${event.type}-${index}`} className="node-inspector-event">
                          <time>{event.ts?.slice(11, 19) ?? "--:--:--"}</time>
                          <span className="type">{event.type}</span>
                          <span className="text">{renderEventText(event)}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </Section>

                <Section
                  title="产出工件"
                  extra={<span className="node-inspector-count">{produced.length}</span>}
                >
                  {produced.length === 0 ? (
                    <>
                      <em className="node-inspector-empty">暂无该节点产出的工件</em>
                      {artifactKinds.length > 0 && (
                        <p className="node-inspector-hint">
                          计划产出：{artifactKinds
                            .map((kind) => ARTIFACT_LABELS[kind] ?? kind)
                            .join("、")}（尚未写入）
                        </p>
                      )}
                    </>
                  ) : (
                    <div className="node-inspector-refs">
                      {produced.map((record) => (
                        <button
                          type="button"
                          key={`${record.kind}-${record.version}`}
                          className="node-inspector-artifact"
                          title={record.path}
                          onClick={() => onOpenArtifact?.(record)}
                        >
                          <b>{ARTIFACT_LABELS[record.kind] ?? record.kind}</b>
                          <span>v{record.version} · {record.status}</span>
                        </button>
                      ))}
                    </div>
                  )}
                </Section>

                <div className="node-inspector-actions">
                  <div className="node-inspector-action-buttons">
                    {detail.can_retry && (
                      <Button type="primary" size="small" onClick={() => openAction("retry")}>
                        重试
                      </Button>
                    )}
                    {detail.can_reset && (
                      <Button size="small" onClick={() => openAction("reset")}>重置</Button>
                    )}
                    {detail.can_skip && (
                      <Button size="small" danger onClick={() => openAction("skip")}>跳过</Button>
                    )}
                    {detail.can_feedback && (
                      <Button size="small" onClick={() => openAction("feedback")}>注入反馈</Button>
                    )}
                    {onFilterEvents && (
                      <Button size="small" type="link" onClick={() => onFilterEvents(node)}>
                        只看此节点事件
                      </Button>
                    )}
                  </div>
                  {actionHint && <p className="node-inspector-action-hint">{actionHint}</p>}
                </div>
              </>
            )}
          </div>
        )}
      </Drawer>

      <Modal
        open={action !== null && detail != null}
        title={action ? ACTION_TITLE[action] : ""}
        okText={action ? ACTION_OK_TEXT[action] : "确认"}
        cancelText="取消"
        confirmLoading={busy}
        okButtonProps={action === "skip" ? { danger: true } : undefined}
        onOk={submit}
        onCancel={() => {
          if (!busy) setAction(null);
        }}
      >
        {action === "retry" && (
          <div className="node-inspector-modal">
            <p>重试会把节点置回 pending，由调度器重新执行；可附带写给下一次 attempt 的反馈。</p>
            <Input.TextArea
              rows={4}
              maxLength={4000}
              value={feedback}
              onChange={(event) => setFeedback(event.target.value)}
              placeholder="可选：反馈会写入节点 config.feedback，重跑时交给执行器"
            />
            <Checkbox
              checked={resetDownstream}
              onChange={(event) => setResetDownstream(event.target.checked)}
            >
              级联重置下游（下游回到 pending，避免旧工件污染）
            </Checkbox>
          </div>
        )}
        {action === "reset" && (
          <div className="node-inspector-modal">
            <Alert
              type="warning"
              showIcon
              message="重置会清理调度状态，节点回到 pending"
              description="feedback / retry_at / retry_count / escalation 等调度键会被清空，节点由调度器重新排队。"
            />
            <Checkbox
              checked={resetDownstream}
              onChange={(event) => setResetDownstream(event.target.checked)}
            >
              级联重置下游（默认开启，避免旧工件污染）
            </Checkbox>
          </div>
        )}
        {action === "skip" && (
          <div className="node-inspector-modal">
            <Alert
              type="warning"
              showIcon
              message="该节点不产出工件，下游将按缺失输入继续"
              description="跳过后节点标记为 skipped，运行不会因该节点阻塞；请确认这是预期取舍。"
            />
            <Input.TextArea
              rows={3}
              maxLength={4000}
              value={skipReason}
              onChange={(event) => setSkipReason(event.target.value)}
              placeholder="可选：跳过原因（写入 node_skipped 事件，便于审计）"
            />
          </div>
        )}
        {action === "feedback" && (
          <div className="node-inspector-modal">
            <p>反馈写入节点 config.feedback，不会触发重跑；下一次 attempt 生效。</p>
            <Input.TextArea
              rows={4}
              maxLength={4000}
              showCount
              value={feedback}
              onChange={(event) => setFeedback(event.target.value)}
              placeholder="必填：要注入的反馈内容（1–4000 字）"
            />
          </div>
        )}
      </Modal>
    </>
  );
}
