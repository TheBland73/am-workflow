import { Empty, Tag } from "antd";
import { Link } from "react-router-dom";
import {
  CheckCircleOutlined, CloseCircleOutlined, FileTextOutlined, HistoryOutlined,
  RocketOutlined, ThunderboltOutlined,
} from "@ant-design/icons";
import type { PlanActivityItem, PlanActivityKind } from "../api/types";

interface Props {
  items: PlanActivityItem[] | undefined;
  loading: boolean;
}

const KIND_META: Record<PlanActivityKind, { label: string; icon: JSX.Element; cls: string }> = {
  created: { label: "创建", icon: <FileTextOutlined />, cls: "is-created" },
  generate: { label: "生成", icon: <ThunderboltOutlined />, cls: "is-generate" },
  validate: { label: "校验", icon: <CheckCircleOutlined />, cls: "is-validate" },
  commit: { label: "提交", icon: <RocketOutlined />, cls: "is-commit" },
  archive: { label: "归档", icon: <HistoryOutlined />, cls: "is-archive" },
};

function stamp(value: string | null): string {
  if (!value) return "—";
  const raw = /[zZ]|[+-]\d{2}:?\d{2}$/.test(value) ? value : `${value}Z`;
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

/** 旧快照的 errors 可能是对象、对象数组或字符串，统一降级成可渲染文本。 */
function lines(value: unknown): string[] {
  if (value == null) return [];
  const items = Array.isArray(value) ? value : [value];
  const out: string[] = [];
  for (const item of items) {
    if (typeof item === "string") {
      if (item.trim()) out.push(item);
    } else if (item && typeof item === "object") {
      const row = item as { message?: string; code?: string; hint?: string };
      const text = (row.message || row.code || "").trim();
      if (text) out.push(row.hint ? `${text}（${row.hint}）` : text);
    } else if (item != null) {
      out.push(String(item));
    }
  }
  return out;
}

export default function PlanActivity({ items, loading }: Props) {
  if (loading) {
    return <div className="plan-activity-empty">读取活动中…</div>;
  }
  if (!items || items.length === 0) {
    return (
      <div className="plan-activity-empty">
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有活动记录" />
      </div>
    );
  }
  return (
    <div className="plan-activity">
      {items.map((item, index) => {
        const meta = KIND_META[item.kind] ?? { label: item.kind, icon: <HistoryOutlined />, cls: "" };
        const errs = lines(item.errors);
        const warns = lines(item.warnings);
        return (
          <div className={`plan-activity-row ${meta.cls}`} key={`${item.kind}-${item.at}-${index}`}>
            <div className="plan-activity-rail">
              <i>{meta.icon}</i>
              {index < items.length - 1 ? <span /> : null}
            </div>
            <div className="plan-activity-body">
              <div className="plan-activity-head">
                <b>{item.title}</b>
                <Tag className="ef-tag">{meta.label}</Tag>
                {item.version ? <em>v{item.version}</em> : null}
                {item.ok === true ? <Tag color="success">通过</Tag> : null}
                {item.ok === false ? (
                  <Tag color="error" icon={<CloseCircleOutlined />}>未通过</Tag>
                ) : null}
                <span className="plan-activity-time">{stamp(item.at)}</span>
              </div>
              <p>{item.detail}</p>
              {errs.length > 0 ? (
                <ul className="plan-activity-errors">
                  {errs.slice(0, 4).map((error) => <li key={error}>{error}</li>)}
                  {errs.length > 4 ? <li>还有 {errs.length - 4} 条…</li> : null}
                </ul>
              ) : null}
              <div className="plan-activity-foot">
                {item.stats ? (
                  <>
                    <span>{item.stats.tasks} 任务</span>
                    <span>{item.stats.sections} 章节</span>
                    <span>{item.stats.covered} 已承接</span>
                    <span>{item.stats.points} 点</span>
                  </>
                ) : null}
                {warns.length > 0 ? <em>{warns.length} 条提示</em> : null}
                {item.run_id ? <Link to={`/runs/${item.run_id}`}>运行 {item.run_id}</Link> : null}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
