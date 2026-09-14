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
