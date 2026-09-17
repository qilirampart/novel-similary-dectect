const RUN_STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "执行中",
  pause_requested: "暂停处理中",
  paused: "已暂停",
  cancel_requested: "取消处理中",
  cancelled: "已取消",
  completed: "已完成",
  partial_failed: "部分失败",
  failed: "失败"
};

export function coverRunStatusLabel(status: string, failedItemCount: number): string {
  if (status === "partial_failed" && Math.max(Number(failedItemCount) || 0, 0) === 0) {
    return "部分完成";
  }
  return RUN_STATUS_LABELS[status] || status || "未知";
}

export type CoverRunAction = "pause" | "resume" | "cancel";

const RISK_CASE_STATUS_LABELS: Record<string, string> = {
  open: "待复核",
  needs_review: "待复核",
  confirmed_risk: "已确认风险",
  confirmed_rectified: "已确认整改",
  false_positive: "误报",
  unavailable: "已失联",
  closed: "已关闭"
};

export function coverRiskCaseStatusLabel(status: string): string {
  return RISK_CASE_STATUS_LABELS[status] || status || "未知";
}

export function coverRunActions(status: string): CoverRunAction[] {
  if (status === "paused") return ["resume", "cancel"];
  if (status === "queued" || status === "running") return ["pause", "cancel"];
  if (status === "pause_requested") return ["cancel"];
  return [];
}
