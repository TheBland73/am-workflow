import type { RunStatus } from "../api/types";

export const RUN_STATUS: Record<RunStatus, { color: string; label: string }> = {
  created: { color: "default", label: "已创建" },
  running: { color: "processing", label: "运行中" },
  waiting_human: { color: "warning", label: "等待人工" },
  finished: { color: "success", label: "已完成" },
  failed: { color: "error", label: "失败" },
  stopped: { color: "default", label: "已停止" },
};

export function StatusTag({ status }: { status: RunStatus }) {
  const item = RUN_STATUS[status] ?? { color: "default", label: status };
  return <span className={`status-pill is-${status}`}>{item.label}</span>;
}

/** 状态中文名：运行列表与统计页共用，避免两个页面说法不一致。 */
export function runStatusLabel(status: string): string {
  return RUN_STATUS[status as RunStatus]?.label ?? status;
}
