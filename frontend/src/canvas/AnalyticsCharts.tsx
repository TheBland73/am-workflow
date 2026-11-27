/** 数据统计页的图元：全部是原生 SVG / CSS，不引入图表库（与其他页面共用 endfield 样式变量）。 */
import type { AnalyticsStatusItem, AnalyticsTrendPoint } from "../api/types";

function int(value: number): string {
  return value.toLocaleString("en-US");
}

/** 每日运行量：一根柱子分三段（成功 / 失败 / 其他），高度按天内的最大值归一。 */
export function TrendBars({ items }: { items: AnalyticsTrendPoint[] }) {
  const max = Math.max(1, ...items.map((item) => item.runs));
  const step = Math.max(1, Math.ceil(items.length / 8));

  return (
    <div className="an-bars" role="img" aria-label="每日运行趋势">
      {items.map((item, index) => {
        const others = Math.max(0, item.runs - item.finished - item.failed);
        const height = (item.runs / max) * 100;
        const label = index === 0 || index === items.length - 1 || index % step === 0
          ? item.date.slice(5)
          : "";
        const tip = [
          item.date,
          `运行 ${item.runs}`,
          `成功 ${item.finished}`,
          `失败 ${item.failed}`,
          item.total_tokens ? `token ${int(item.total_tokens)}` : "",
        ].filter(Boolean).join(" · ");
        return (
          <div key={item.date} className={`an-bar${item.runs ? "" : " is-empty"}`} title={tip}>
            <div className="an-bar-track">
              <div className="an-bar-col" style={{ height: `${height}%` }}>
                {others > 0 && (
                  <span className="an-seg is-other" style={{ flexGrow: others }} />
                )}
                {item.failed > 0 && (
                  <span className="an-seg is-failed" style={{ flexGrow: item.failed }} />
                )}
                {item.finished > 0 && (
                  <span className="an-seg is-finished" style={{ flexGrow: item.finished }} />
                )}
              </div>
            </div>
            <span className="an-bar-label">{label}</span>
          </div>
        );
      })}
    </div>
  );
}

const RADIUS = 62;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

/** 状态分布环形图；点击某一段切换运行记录的状态筛选。 */
export function StatusDonut({
  items,
  total,
  active,
  onSelect,
}: {
  items: AnalyticsStatusItem[];
  total: number;
  active: string | null;
  onSelect: (status: string) => void;
}) {
  const slices = items.filter((item) => item.count > 0);
  let offset = 0;

  return (
    <svg className="an-donut" viewBox="0 0 160 160" role="img" aria-label="运行状态分布">
      <circle className="an-donut-base" cx="80" cy="80" r={RADIUS} fill="none" strokeWidth={16} />
      <g transform="rotate(-90 80 80)">
        {slices.map((item) => {
          const length = total > 0 ? (item.count / total) * CIRCUMFERENCE : 0;
          const dash = `${length} ${CIRCUMFERENCE - length}`;
          const current = offset;
          offset += length;
          const label = `${item.status} ${item.count}`;
          return (
            <circle
              key={item.status}
              className={`an-arc is-${item.status}${active === item.status ? " is-active" : ""}`}
              cx="80"
              cy="80"
              r={RADIUS}
              fill="none"
              strokeWidth={active === item.status ? 22 : 16}
              strokeDasharray={dash}
              strokeDashoffset={-current}
              role="button"
              tabIndex={0}
              aria-label={label}
              onClick={() => onSelect(item.status)}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  onSelect(item.status);
                }
              }}
            >
              <title>{label}</title>
            </circle>
          );
        })}
      </g>
      <text className="an-donut-value" x="80" y="76" textAnchor="middle">{int(total)}</text>
      <text className="an-donut-cap" x="80" y="96" textAnchor="middle">运行</text>
    </svg>
  );
}

export interface RatioRow {
  label: string;
  value: number;
  ratio: number;
  hint?: string;
}

/** 占比条：按模型 / 按状态排好的横向比例。 */
export function RatioRows({ rows, unit }: { rows: RatioRow[]; unit?: string }) {
  return (
    <ul className="an-ratio">
      {rows.map((row) => (
        <li key={row.label} className="an-ratio-row">
          <div className="an-ratio-head">
            <b title={row.label}>{row.label}</b>
            <span>
              {int(row.value)}
              {unit ? ` ${unit}` : ""}
              <em>{(row.ratio * 100).toFixed(1)}%</em>
            </span>
          </div>
          <div className="an-ratio-track">
            <i style={{ width: `${Math.max(row.ratio * 100, row.value ? 1.5 : 0)}%` }} />
          </div>
          {row.hint && <small>{row.hint}</small>}
        </li>
      ))}
    </ul>
  );
}
