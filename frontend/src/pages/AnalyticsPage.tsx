import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Button, Empty, Segmented, Spin, Table, Tooltip } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";

import { api } from "../api/client";
import type {
  AnalyticsFailedItem, AnalyticsRange, AnalyticsRunItem, AnalyticsStatusItem,
  AnalyticsTrendPoint,
} from "../api/types";
import { RatioRows, StatusDonut, TrendBars } from "../canvas/AnalyticsCharts";
import { StatusTag, runStatusLabel } from "./RunsPage";
import "../styles/analytics.css";

const RANGE_OPTIONS: Array<{ label: string; value: AnalyticsRange }> = [
  { label: "最近 7 天", value: 7 },
  { label: "最近 30 天", value: 30 },
  { label: "最近 90 天", value: 90 },
];

function fmtInt(value: number | null | undefined): string {
  return typeof value === "number" ? value.toLocaleString("en-US") : "—";
}

function fmtPct(ratio: number, digits = 1): string {
  return `${(ratio * 100).toFixed(digits)}%`;
}

function fmtDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  const totalSeconds = Math.round(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  if (minutes < 60) return `${minutes}m ${String(totalSeconds % 60).padStart(2, "0")}s`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
}

function fmtCost(cost: number, available: boolean): string {
  if (!available) return "—";
  if (!cost) return "0";
  return cost < 0.0001 ? cost.toExponential(2) : cost.toFixed(cost < 1 ? 4 : 2);
}

/** 与运行列表一致：后端返回的是未带时区的 UTC 字符串，这里按同一写法展示。 */
function fmtStamp(value: string | null | undefined): string {
  return value ? value.replace("T", " ").slice(0, 19) : "—";
}

function fmtDay(value: string | null | undefined): string {
  return value ? value.slice(5) : "—";
}

function PanelError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="an-error">
      读取失败：{message}
      <Button size="small" style={{ marginLeft: 10 }} onClick={onRetry}>重试</Button>
    </div>
  );
}

function Panel({ title, note, children }: {
  title: string;
  note?: string;
  children: ReactNode;
}) {
  return (
    <section className="ef-panel">
      <div className="an-panel-head">
        <h2>{title}</h2>
        {note && <span className="an-note">{note}</span>}
      </div>
      <div className="an-panel-body">{children}</div>
    </section>
  );
}

export default function AnalyticsPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [range, setRange] = useState<AnalyticsRange>(7);
  const [statusFilter, setStatusFilter] = useState<string | null>(null);

  const overview = useQuery({
    queryKey: ["analytics", "overview", range],
    queryFn: () => api.analyticsOverview(range),
  });
  const trends = useQuery({
    queryKey: ["analytics", "trends", range],
    queryFn: () => api.analyticsTrends(range),
  });
  const distribution = useQuery({
    queryKey: ["analytics", "distribution", range],
    queryFn: () => api.analyticsDistribution(range),
  });
  const usage = useQuery({
    queryKey: ["analytics", "usage", range],
    queryFn: () => api.analyticsUsage(range),
  });
  const recent = useQuery({
    queryKey: ["analytics", "recent", range, statusFilter],
    queryFn: () => api.analyticsRecentRuns(range, statusFilter),
  });
  const failed = useQuery({
    queryKey: ["analytics", "failed", range],
    queryFn: () => api.analyticsFailedRuns(range),
  });

  const refreshing = [overview, trends, distribution, usage, recent, failed]
    .some((query) => query.isFetching);
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["analytics"] });

  const summary = overview.data;
  const window = summary?.window;
  const costAvailable = summary?.cost.available ?? false;

  const toggleStatus = (status: string) => {
    setStatusFilter((current) => (current === status ? null : status));
  };

  const recentColumns = useMemo(() => [
    {
      title: "RUN",
      dataIndex: "id",
      width: 122,
      render: (id: string) => <span className="am-mono an-nowrap">{id}</span>,
    },
    {
      title: "想法",
      dataIndex: "idea",
      ellipsis: true,
      render: (idea: string) => (
        <Tooltip title={idea}><span>{idea || "（未填写）"}</span></Tooltip>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 104,
      render: (status: AnalyticsRunItem["status"]) => <StatusTag status={status} />,
    },
    {
      title: "创建时间",
      dataIndex: "created_at",
      width: 152,
      render: (value: string | null) => (
        <span className="am-mono an-nowrap">{fmtStamp(value)}</span>
      ),
    },
    {
      title: "耗时",
      dataIndex: "duration_ms",
      width: 92,
      align: "right" as const,
      render: (value: number | null) => fmtDuration(value),
    },
    {
      title: "TOKEN",
      dataIndex: "total_tokens",
      width: 92,
      align: "right" as const,
      render: (value: number) => fmtInt(value),
    },
    {
      title: "费用",
      dataIndex: "cost",
      width: 88,
      align: "right" as const,
      render: (value: number, row: AnalyticsRunItem) => fmtCost(value, row.cost_available),
    },
  ], []);

  const failedColumns = useMemo(() => [
    {
      title: "RUN",
      dataIndex: "id",
      width: 122,
      render: (id: string) => <span className="am-mono an-nowrap">{id}</span>,
    },
    {
      title: "失败节点",
      dataIndex: "node_name",
      width: 120,
      render: (value: string | null, row: AnalyticsFailedItem) => (
        <span className="am-mono">{value ?? row.node_key ?? "—"}</span>
      ),
    },
    {
      title: "错误摘要",
      dataIndex: "error",
      ellipsis: true,
      render: (value: string, row: AnalyticsFailedItem) => (
        <Tooltip title={row.node_error || value}>
          <span>{value}</span>
        </Tooltip>
      ),
    },
    {
      title: "失败时间",
      dataIndex: "failed_at",
      width: 152,
      render: (value: string | null) => (
        <span className="am-mono an-nowrap">{fmtStamp(value)}</span>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 104,
      render: (status: AnalyticsFailedItem["status"]) => <StatusTag status={status} />,
    },
  ], []);

  const statusRows: AnalyticsStatusItem[] = distribution.data?.items ?? [];

  const modelRows = useMemo(() => {
    const rows = usage.data?.by_model ?? [];
    return rows.map((item) => ({
      label: item.provider ? `${item.model_name} · ${item.provider}` : item.model_name,
      value: item.total,
      ratio: item.ratio,
      hint: [
        `IN ${fmtInt(item.tokens_in)} / OUT ${fmtInt(item.tokens_out)}`,
        `${fmtInt(item.attempts)} 次 attempt`,
        item.cost > 0 ? `费用 ${fmtCost(item.cost, true)}` : "",
      ].filter(Boolean).join(" · "),
    }));
  }, [usage.data]);

  const trendItems: AnalyticsTrendPoint[] = trends.data?.items ?? [];
  const trendTotal = trends.data?.totals.runs ?? 0;

  return (
    <div className="an-page">
      <header className="ef-head">
        <div className="ef-eyebrow">
          ANALYTICS / {window ? `${window.start_date} — ${window.end_date}` : "全局统计"}
        </div>
        <div className="head-row">
          <h1>数据统计</h1>
          <div className="head-actions">
            <Segmented
              value={range}
              options={RANGE_OPTIONS}
              onChange={(value) => setRange(value as AnalyticsRange)}
            />
            <Button icon={<ReloadOutlined />} loading={refreshing} onClick={refresh}>
              刷新
            </Button>
          </div>
        </div>
        <p>
          全部工作流的运行量、成功率、耗时、Token 与费用。口径：按运行创建时间落在窗口内的记录统计，
          日期按 UTC 自然日切分；成功率只算 <b>finished</b>，失败不计入平均耗时。
        </p>
        <div className="ef-bar-title"><span>GLOBAL OVERVIEW</span></div>
      </header>

      {/* 1. 全局统计卡片 */}
      {overview.error ? (
        <Panel title="全局统计" note="OVERVIEW">
          <PanelError
            message={(overview.error as Error).message}
            onRetry={() => overview.refetch()}
          />
        </Panel>
      ) : overview.isLoading ? (
        <div className="ef-panel an-loading"><Spin /></div>
      ) : summary ? (
        <div className="an-kpis">
          <div className="an-kpi">
            <div className="an-kpi-label">总运行次数 <small>RUNS</small></div>
            <div className="an-kpi-value">{fmtInt(summary.runs.total)}</div>
            <div className="an-kpi-sub">窗口内全部运行</div>
          </div>
          <div className="an-kpi">
            <div className="an-kpi-label">成功运行 <small>SUCCESS</small></div>
            <div className="an-kpi-value">{fmtInt(summary.runs.finished)}</div>
            <div className="an-kpi-sub">
              失败 {fmtInt(summary.runs.failed)} · 停止 {fmtInt(summary.runs.stopped)}
            </div>
          </div>
          <div className="an-kpi is-accent">
            <div className="an-kpi-label">成功率 <small>SUCCESS RATE</small></div>
            <div className="an-kpi-value">{fmtPct(summary.success_rate)}</div>
            <div className="an-kpi-sub">
              {fmtInt(summary.runs.finished)} / {fmtInt(summary.runs.total)} 次运行
            </div>
          </div>
          <div className="an-kpi">
            <div className="an-kpi-label">平均耗时 <small>AVG DURATION</small></div>
            <div className="an-kpi-value">{fmtDuration(summary.duration.avg_ms)}</div>
            <div className="an-kpi-sub">
              {summary.duration.samples
                ? `样本 ${fmtInt(summary.duration.samples)} 次已完成运行`
                : "暂无已完成运行"}
            </div>
          </div>
          <div className="an-kpi">
            <div className="an-kpi-label">Token 总消耗 <small>TOKENS</small></div>
            <div className="an-kpi-value">{fmtInt(summary.tokens.total)}</div>
            <div className="an-kpi-sub">
              IN {fmtInt(summary.tokens.input)} · OUT {fmtInt(summary.tokens.output)}
            </div>
          </div>
          <div className="an-kpi">
            <div className="an-kpi-label">累计费用 <small>COST</small></div>
            <div className="an-kpi-value">
              {summary.cost.available ? fmtCost(summary.cost.estimated_cost, true) : "暂无"}
            </div>
            <div className="an-kpi-sub">
              {summary.cost.available
                ? `${fmtInt(summary.cost.priced_models)} 个计价模型`
                : "未配置模型单价"}
            </div>
          </div>
        </div>
      ) : null}

      {/* 2. 趋势 + 状态分布 */}
      <div className="an-row is-trend">
        <Panel title="运行趋势" note={`TOTAL ${fmtInt(trendTotal)}`}>
          {trends.error ? (
            <PanelError message={(trends.error as Error).message} onRetry={() => trends.refetch()} />
          ) : trends.isLoading ? (
            <div className="an-loading"><Spin /></div>
          ) : trendTotal === 0 ? (
            <div className="an-empty">
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前时间范围内没有运行记录" />
            </div>
          ) : (
            <>
              <ul className="an-legend">
                <li><i className="is-finished" />成功 {fmtInt(trends.data?.totals.finished)}</li>
                <li><i className="is-failed" />失败 {fmtInt(trends.data?.totals.failed)}</li>
                <li>
                  <i className="is-other" />
                  其他 {fmtInt(trendTotal - (trends.data?.totals.finished ?? 0) - (trends.data?.totals.failed ?? 0))}
                </li>
              </ul>
              <TrendBars items={trendItems} />
              <p className="an-hint">
                每日运行数按 UTC 自然日切分；Token 合计 {fmtInt(trends.data?.totals.total_tokens)}。
              </p>
            </>
          )}
        </Panel>

        <Panel title="运行状态分布" note={`TOTAL ${fmtInt(distribution.data?.total ?? 0)}`}>
          {distribution.error ? (
            <PanelError
              message={(distribution.error as Error).message}
              onRetry={() => distribution.refetch()}
            />
          ) : distribution.isLoading ? (
            <div className="an-loading"><Spin /></div>
          ) : (distribution.data?.total ?? 0) === 0 ? (
            <div className="an-empty">
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前时间范围内没有运行记录" />
            </div>
          ) : (
            <div className="an-donut-wrap">
              <StatusDonut
                items={statusRows}
                total={distribution.data?.total ?? 0}
                active={statusFilter}
                onSelect={toggleStatus}
              />
              <ul className="an-leg">
                {statusRows.map((item) => (
                  <li key={item.status}>
                    <button
                      type="button"
                      className="an-leg-item"
                      aria-pressed={statusFilter === item.status}
                      disabled={item.count === 0}
                      onClick={() => toggleStatus(item.status)}
                      title="按此状态筛选下方运行记录"
                    >
                      <span className="k">{runStatusLabel(item.status)}</span>
                      <span className="n">{fmtInt(item.count)}</span>
                      <span className="r">{fmtPct(item.ratio)}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </Panel>
      </div>

      {/* 3. Token / 成本 */}
      <div className="an-row is-usage">
        <Panel title="Token 使用" note={`TOTAL ${fmtInt(usage.data?.tokens.total ?? 0)}`}>
          {usage.error ? (
            <PanelError message={(usage.error as Error).message} onRetry={() => usage.refetch()} />
          ) : usage.isLoading ? (
            <div className="an-loading"><Spin /></div>
          ) : modelRows.length === 0 ? (
            <div className="an-empty">
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前时间范围内没有模型调用记录" />
            </div>
          ) : (
            <>
              <RatioRows rows={modelRows} unit="tokens" />
              <p className="an-hint">
                IN {fmtInt(usage.data?.tokens.input)} / OUT {fmtInt(usage.data?.tokens.output)}
                ，按 node_attempts.tokens_in / tokens_out 归集；未归因模型的调用单列「未记录模型」。
              </p>
            </>
          )}
        </Panel>

        <Panel title="成本统计" note={costAvailable ? "ESTIMATED" : "NO DATA"}>
          {usage.error ? (
            <PanelError message={(usage.error as Error).message} onRetry={() => usage.refetch()} />
          ) : usage.isLoading ? (
            <div className="an-loading"><Spin /></div>
          ) : !(usage.data?.cost.available ?? false) ? (
            <div className="an-empty">
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无成本数据" />
              <p className="an-hint">
                {usage.data?.cost.note ?? "尚未配置模型单价，暂无成本数据"}
                。费用只按「模型单价 × 实际 token」累计，不会用 token 数量估算。
              </p>
            </div>
          ) : (
            <>
              <div className="an-kpi-value">{fmtCost(usage.data?.cost.estimated_cost ?? 0, true)}</div>
              <p className="an-hint">{usage.data?.cost.note}</p>
              <RatioRows
                rows={(usage.data?.cost.by_model ?? []).map((item) => ({
                  label: item.model_name,
                  value: item.total_tokens,
                  ratio: (usage.data?.tokens.total ?? 0) > 0
                    ? item.total_tokens / (usage.data?.tokens.total ?? 1)
                    : 0,
                  hint: `费用 ${fmtCost(item.cost, true)}`,
                }))}
                unit="tokens"
              />
              <p className="an-hint">
                未计价 token：{fmtInt(usage.data?.cost.unpriced_tokens)}（没有单价或未归因模型的调用）。
              </p>
            </>
          )}
        </Panel>
      </div>

      {/* 4. 最近运行 + 失败记录 */}
      <div className="an-row">
        <Panel title="最近运行" note={`最近 ${recent.data?.limit ?? 10} 条`}>
          {statusFilter && (
            <div className="an-filter">
              <span>已按状态筛选：<b>{runStatusLabel(statusFilter)}</b></span>
              <Button size="small" type="link" onClick={() => setStatusFilter(null)}>
                清除
              </Button>
            </div>
          )}
          {recent.error ? (
            <PanelError message={(recent.error as Error).message} onRetry={() => recent.refetch()} />
          ) : (
            <Table
              rowKey="id"
              size="small"
              loading={recent.isLoading}
              dataSource={recent.data?.items ?? []}
              columns={recentColumns}
              pagination={false}
              scroll={{ x: 880 }}
              locale={{
                emptyText: (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={statusFilter
                    ? `当前范围内没有「${runStatusLabel(statusFilter)}」的运行`
                    : "当前时间范围内没有运行记录"}
                  />
                ),
              }}
              onRow={(row) => ({
                onClick: () => navigate(`/runs/${row.id}`),
                style: { cursor: "pointer" },
              })}
            />
          )}
        </Panel>

        <Panel title="失败运行" note={`最近 ${failed.data?.items.length ?? 0} 条`}>
          {failed.error ? (
            <PanelError message={(failed.error as Error).message} onRetry={() => failed.refetch()} />
          ) : (
            <Table
              rowKey="id"
              size="small"
              loading={failed.isLoading}
              dataSource={failed.data?.items ?? []}
              columns={failedColumns}
              pagination={false}
              scroll={{ x: 760 }}
              locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前时间范围内没有失败运行" /> }}
              onRow={(row) => ({
                onClick: () => navigate(`/runs/${row.id}`),
                style: { cursor: "pointer" },
              })}
            />
          )}
        </Panel>
      </div>

      {window && (
        <p className="an-hint">
          统计窗口：{window.start_date} → {window.end_date}（{window.days} 天，{window.timezone}）；
          数据来源 runs / nodes / node_attempts / tasks / model_entries，只读聚合。
        </p>
      )}
    </div>
  );
}
