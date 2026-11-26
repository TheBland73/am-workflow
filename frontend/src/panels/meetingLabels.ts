/** 会议式前半段的中文名：步骤、确认点类型、决策点状态、结论与判定者。 */

import type { AgentTemplate } from "../api/types";

export const MEETING_PHASE_LABEL: Record<string, string> = {
  // 三种协议共用
  prepare: "会前准备", intake: "接收上游", report: "写出报告",
  // 讨论
  clarify: "澄清", roster: "参会名单", positions: "立场", agenda: "议题",
  stances: "表态", exchange: "交锋", summary: "交锋摘要", restance: "重新表态",
  consensus: "轮次共识", reopen: "复议", draft: "成稿", check: "成稿复核",
  // 评审
  points: "评审要点", review: "逐条评审", merge: "汇总意见", respond: "作者回应",
  discuss: "会议讨论", verdict: "定夺", revise: "修订", verify: "验证修订",
  // 确认
  expect: "先写预期", fidelity: "忠实性核对", quality: "质量评审",
};

export const CHECKPOINT_LABEL: Record<string, string> = {
  clarify: "澄清",
  roster: "参会名单",
  consensus: "轮次共识",
  escalation: "轮次用尽",
  // 运行级配置里的工件确认点
  proposal: "方案确认",
  review: "评审裁决",
  requirement: "需求确认",
};

export const POINT_STATUS_LABEL: Record<string, string> = {
  open: "未了结",
  reopened: "复议中",
  agreed: "已一致",
  ruled: "已裁决",
  deferred: "待定",
  superseded: "已被取代",
};

export const VERDICT_LABEL: Record<string, string> = {
  accept: "接受",
  conditional: "有条件接受",
  return: "退回",
  pass: "通过",
  fail: "不通过",
};

export function phaseLabel(phase: string | null | undefined): string {
  if (!phase) return "—";
  return MEETING_PHASE_LABEL[phase] ?? phase;
}

export function statusLabel(status: string | null | undefined): string {
  if (!status) return "—";
  return POINT_STATUS_LABEL[status] ?? status;
}

export function verdictLabel(verdict: string | null | undefined): string {
  if (!verdict) return "—";
  return VERDICT_LABEL[verdict] ?? verdict;
}

/** 判定者：人工、模型名、程序、人工委托。 */
export function decidedByLabel(value: string | null | undefined): string {
  if (!value) return "—";
  if (value.startsWith("model:")) return value.slice("model:".length) || "模型";
  if (value === "human" || value === "user") return "人工";
  if (value === "user-delegated") return "人工委托";
  if (value === "program" || value === "proxy") return "程序";
  return value;
}

/** 确认点的判定者（运行级配置里的 automation.checkpoints）。 */
export const DECIDER_LABEL: Record<string, string> = {
  human: "人工",
  program: "程序",
  program_fallback: "程序（兜底让步）",
  proxy: "需求方代理",
  auto: "自动",
  model: "模型",
};

/** 每个确认点允许的判定者，与后端参数校验的规则一致。 */
export const CHECKPOINT_DECIDERS: Record<string, string[]> = {
  clarify: ["human", "proxy"],
  roster: ["human", "auto"],
  consensus: ["human", "program", "program_fallback"],
  proposal: ["human", "auto"],
  review: ["human", "model"],
  requirement: ["human", "model"],
};

export function deciderLabel(value: string | null | undefined): string {
  if (!value) return "—";
  return DECIDER_LABEL[value] ?? value;
}

/** 能当讨论参会者的角色：元数据标了适合讨论，或者没标会议角色（用户自建、没填）。 */
export function canJoinDiscussion(template: AgentTemplate): boolean {
  const roles = template.meta?.meeting_roles;
  return roles ? roles.includes("discussion") : true;
}
