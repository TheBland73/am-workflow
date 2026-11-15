import { Collapse, Empty } from "antd";
import type { PlanHeader } from "../api/types";

interface Props {
  plan: PlanHeader;
}

export default function PlanSourcePanel({ plan }: Props) {
  const sourceLabel = plan.source_type === "artifact" ? "工件导入" : plan.source_type === "upload" ? "上传文件" : "粘贴文本";
  return (
    <aside className="plan-source">
      <div className="plan-panel-head">
        <b>设计来源 / SOURCE</b>
        <span>{sourceLabel}</span>
      </div>

      <div className="plan-source-meta">
        <div><span>来源</span><b>{plan.source_ref || "手动粘贴"}</b></div>
        <div><span>模型</span><b>{plan.model_name || "未分配"}</b></div>
        <div><span>章节</span><b>{plan.sections.length} 节</b></div>
      </div>

      <div className="plan-section-index">
        <div className="plan-kicker">章节索引</div>
        {plan.sections.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="未识别到章节" />
        ) : (
          plan.sections.map((section) => (
            <div className={`plan-section-row is-l${section.level}`} key={`${section.line}-${section.key}`}>
              <span>{String(section.line).padStart(3, "0")}</span>
              <b>{section.title}</b>
            </div>
          ))
        )}
      </div>

      <div className="plan-design-text">
        <div className="plan-kicker">设计原文</div>
        <pre>{plan.design_text || "—"}</pre>
      </div>

      {plan.raw_output ? (
        <Collapse
          ghost
          size="small"
          items={[{
            key: "raw",
            label: <span className="plan-kicker">PM 原始输出</span>,
            children: <pre className="plan-raw-output">{plan.raw_output}</pre>,
          }]}
        />
      ) : null}
    </aside>
  );
}
