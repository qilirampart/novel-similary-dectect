function parseIsoDate(value: unknown): number | null {
  const text = String(value ?? "").trim();
  if (!text) return null;
  const normalized = text.includes("T") ? text : text.replace(" ", "T");
  const timestamp = Date.parse(normalized);
  return Number.isFinite(timestamp) ? timestamp : null;
}

function formatSeconds(totalSeconds: number): string {
  const safe = Math.max(0, Math.round(totalSeconds));
  if (safe < 60) return `${safe}秒`;
  const hours = Math.floor(safe / 3600);
  const minutes = Math.floor((safe % 3600) / 60);
  const seconds = safe % 60;
  if (hours > 0) {
    if (minutes <= 0) return `${hours}小时`;
    return `${hours}小时${minutes}分`;
  }
  if (seconds <= 0) return `${minutes}分`;
  return `${minutes}分${seconds}秒`;
}

export function getTaskProcessedCount(task: Record<string, any>): number {
  const counts = (task.counts ?? {}) as Record<string, number | null>;
  return Math.max(Number(counts.completed ?? 0) + Number(counts.failed ?? 0), 0);
}

export function getTaskAcceptedCount(task: Record<string, any>): number {
  const counts = (task.counts ?? {}) as Record<string, number | null>;
  return Math.max(Number(counts.accepted ?? 0), 0);
}

export function estimateTaskRemainingSeconds(task: Record<string, any>): number | null {
  const accepted = getTaskAcceptedCount(task);
  const processed = getTaskProcessedCount(task);
  if (accepted <= 0 || processed <= 0 || processed >= accepted) return null;
  const startedAt = parseIsoDate(task.started_at ?? task.created_at);
  if (startedAt === null) return null;
  const elapsedSeconds = (Date.now() - startedAt) / 1000;
  if (!Number.isFinite(elapsedSeconds) || elapsedSeconds <= 1) return null;
  const avgSecondsPerItem = elapsedSeconds / processed;
  if (!Number.isFinite(avgSecondsPerItem) || avgSecondsPerItem <= 0) return null;
  return Math.max((accepted - processed) * avgSecondsPerItem, 0);
}

export function formatTaskEta(task: Record<string, any>): string {
  const remainingSeconds = estimateTaskRemainingSeconds(task);
  if (remainingSeconds === null) return "-";
  return `约 ${formatSeconds(remainingSeconds)}`;
}

export function formatTaskProgressDetail(task: Record<string, any>): string {
  const accepted = getTaskAcceptedCount(task);
  const processed = getTaskProcessedCount(task);
  if (accepted <= 0) return "-";
  return `${processed}/${accepted}`;
}
