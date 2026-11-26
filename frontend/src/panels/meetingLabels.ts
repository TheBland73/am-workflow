/** 会议式前半段的中文名：步骤、确认点类型、决策点状态、结论与判定者。 */

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
