const TASK_STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "执行中",
  paused: "已暂停",
  pause_requested: "暂停中",
  completed: "已完成",
  failed: "失败",
  partial_failed: "部分失败",
  cancel_requested: "取消中",
  cancelled: "已取消"
};

const DETECTION_MODE_LABELS: Record<string, string> = {
  reuse: "普通检测",
  rewrite: "普通检测 + 改写检测"
};

const TASK_TYPE_LABELS: Record<string, string> = {
  batch_compare: "批量比对"
};

const SEMANTIC_STATUS_LABELS: Record<string, string> = {
  waiting: "待执行",
  disabled: "未启用",
  semantic_ready: "语义可用",
  fallback_lexical_only: "仅词法回退",
  fallback_semantic_timeout: "语义超时回退"
};

const REVIEW_LABELS: Record<string, string> = {
  high_risk: "高风险",
  medium_risk: "中风险",
  low_risk: "低风险",
  unknown: "未知"
};

const CONFIDENCE_LABELS: Record<string, string> = {
  strong: "高置信",
  medium: "中置信",
  weak: "低置信",
  unknown: "未知"
};

const SYSTEM_CARD_LABELS: Record<string, string> = {
  "Platform Health": "平台健康",
  "Stored Contents": "已存内容",
  "Semantic Chunks": "语义分片",
  "Running Tasks": "运行中任务",
  "Stored Results": "已存结果",
  "Fallback Results": "回退结果"
};

const SYSTEM_SERVICE_TITLES: Record<string, string> = {
  "Retrieval Database": "检索数据库",
  "Business Store": "业务库",
  "Semantic Backend": "语义后端",
  "Runtime Paths": "运行目录"
};

const SYSTEM_STATE_LABELS: Record<string, string> = {
  healthy: "健康",
  degraded: "降级",
  ready: "正常",
  missing: "缺失",
  configured: "已配置",
  disabled: "未启用",
  available: "可用",
  normal: "正常"
};

const SYSTEM_ITEM_LABELS: Record<string, string> = {
  database: "数据库文件",
  chapters: "章节数",
  contents: "内容数",
  "semantic chunks": "语义分片",
  tasks: "任务数",
  results: "结果数",
  reviews: "复核数",
  backend: "后端",
  "embedding provider": "向量服务",
  model: "模型",
  Qdrant: "Qdrant 地址",
  "chunk collection": "分片集合",
  "upload root": "上传目录",
  "export root": "导出目录",
  "default creator": "默认创建人",
  "allowed origins": "允许来源"
};

const METRIC_LABELS: Record<string, string> = {
  fine_score: "精排分数",
  review_label: "风险标签",
  confidence_label: "置信标签",
  semantic_status: "语义状态",
  longest_match_len: "最长匹配长度",
  longest_match_ratio: "最长匹配比例",
  ngram_recall: "N-Gram 召回",
  ngram_precision: "N-Gram 精度",
  jaccard: "Jaccard 相似度",
  sequence_ratio: "序列相似度",
  coarse_rank: "粗排名次"
};

const MODULE_LABELS: Record<string, string> = {
  "task executor": "任务执行器",
  "system monitor": "系统监控"
};

function mapKnown(value: string, mapping: Record<string, string>): string {
  const normalized = value.trim();
  if (!normalized) return "-";
  return mapping[normalized] ?? normalized;
}

export function formatTaskStatusLabel(value: string): string {
  return mapKnown(value, TASK_STATUS_LABELS);
}

export function formatDetectionModeLabel(value: string): string {
  return mapKnown(value, DETECTION_MODE_LABELS);
}

export function formatTaskTypeLabel(value: string): string {
  return mapKnown(value, TASK_TYPE_LABELS);
}

export function formatSemanticStatusLabel(value: string): string {
  return mapKnown(value, SEMANTIC_STATUS_LABELS);
}

export function formatReviewLabel(value: string): string {
  return mapKnown(value, REVIEW_LABELS);
}

export function formatConfidenceLabel(value: string): string {
  return mapKnown(value, CONFIDENCE_LABELS);
}

export function formatSystemCardLabel(value: string): string {
  return mapKnown(value, SYSTEM_CARD_LABELS);
}

export function formatSystemServiceTitle(value: string): string {
  return mapKnown(value, SYSTEM_SERVICE_TITLES);
}

export function formatRuntimeStateLabel(value: string): string {
  return mapKnown(value, SYSTEM_STATE_LABELS);
}

export function formatSystemItemLabel(value: string): string {
  return mapKnown(value, SYSTEM_ITEM_LABELS);
}

export function formatMetricLabel(value: string): string {
  return mapKnown(value, METRIC_LABELS);
}

export function formatModuleLabel(value: string): string {
  return mapKnown(value, MODULE_LABELS);
}

export function formatSystemHint(value: string): string {
  const normalized = value.trim();
  if (!normalized) return "-";
  if (normalized === "Retrieval and business databases are ready") return "检索库和业务库均已就绪";
  if (normalized === "Check runtime storage and worker dependencies") return "请检查运行目录和 Worker 依赖";
  if (normalized === "Live workers") return "当前 Worker";
  if (normalized === "Top1 review labels") return "Top1 风险标签";
  if (normalized === "rewrite fallback") return "改写检测回退";
  if (normalized === "No recent task failures. Runtime indicators are stable.") return "最近没有任务失败，运行指标稳定。";
  if (normalized === "latest task update") return "最近一次任务更新";

  const chapterMatch = normalized.match(/^Chapters\s+(.+)$/);
  if (chapterMatch) return `章节 ${chapterMatch[1]}`;

  const evidenceMatch = normalized.match(/^Evidence windows\s+(.+)$/);
  if (evidenceMatch) return `证据窗 ${evidenceMatch[1]}`;

  const queuePausedMatch = normalized.match(/^Queued\s+(.+?)\s*\/\s*Paused\s+(.+?)\s*\/\s*Total\s+(.+)$/);
  if (queuePausedMatch) return `排队 ${queuePausedMatch[1]} / 暂停 ${queuePausedMatch[2]} / 总数 ${queuePausedMatch[3]}`;

  const queueMatch = normalized.match(/^Queued\s+(.+?)\s*\/\s*Total\s+(.+)$/);
  if (queueMatch) return `排队 ${queueMatch[1]} / 总数 ${queueMatch[2]}`;

  const reviewMatch = normalized.match(/^Reviews\s+(.+)$/);
  if (reviewMatch) return `复核 ${reviewMatch[1]}`;

  const failureMatch = normalized.match(/^Failed or partial tasks\s+(.+)$/);
  if (failureMatch) return `失败或部分失败 ${failureMatch[1]}`;

  return normalized;
}

export function formatTaskMessage(value: string): string {
  const normalized = value.trim();
  if (!normalized) return "-";
  if (/[\u4e00-\u9fff]/.test(normalized)) return normalized;
  if (normalized === "Task claimed by worker. Parsing input file.") return "任务已被 Worker 接手，正在解析输入文件。";
  if (normalized === "Task execution failed with an unhandled exception.") return "任务执行时发生未处理异常。";
  if (normalized === "Task cancelled during execution.") return "任务在执行过程中已取消。";
  if (normalized === "task paused from api") return "任务已收到暂停请求。";
  if (normalized === "task resumed from api") return "任务已继续，重新进入队列。";
  if (normalized === "task deleted from api") return "任务已删除。";
  if (normalized === "task paused before execution") return "任务已在执行前暂停。";
  if (normalized === "task pause requested") return "任务正在暂停，等待当前执行项结束。";
  if (normalized === "task resumed and returned to queue") return "任务已继续，等待 Worker 重新领取。";
  if (normalized === "task pause request revoked and execution resumed") return "任务已继续，本轮暂停请求已取消。";
  if (normalized === "Input file contains no executable text.") return "输入文件中没有可执行文本。";
  if (normalized === "Input file is missing and the task cannot be executed.") return "输入文件缺失，任务无法执行。";

  const parsedMatch = normalized.match(/^Parsed input file with (\d+) pending text items\.$/);
  if (parsedMatch) return `输入文件解析完成，共有 ${parsedMatch[1]} 条待处理文本。`;

  const resumedMatch = normalized.match(/^Resumed task with (\d+) remaining text items\.$/);
  if (resumedMatch) return `任务已继续，剩余 ${resumedMatch[1]} 条待处理文本。`;

  const processingMatch = normalized.match(/^Processing item (\d+)\/(\d+)\.$/);
  if (processingMatch) return `正在处理第 ${processingMatch[1]}/${processingMatch[2]} 条。`;

  const processingInFlightMatch = normalized.match(/^Processing item (\d+)\/(\d+)\. In flight (\d+)\.$/);
  if (processingInFlightMatch) {
    return `正在处理第 ${processingInFlightMatch[1]}/${processingInFlightMatch[2]} 条，并发中 ${processingInFlightMatch[3]} 条。`;
  }

  const completedMatch = normalized.match(/^Task completed successfully\. Processed (\d+) items\.$/);
  if (completedMatch) return `任务已完成，共处理 ${completedMatch[1]} 条。`;

  const pausedMatch = normalized.match(/^Task paused\. Processed (\d+)\/(\d+) items\.$/);
  if (pausedMatch) return `任务已暂停，已处理 ${pausedMatch[1]}/${pausedMatch[2]} 条。`;

  const partialFailedMatch = normalized.match(/^Task completed with partial failure\. (\d+)\/(\d+) items failed\.$/);
  if (partialFailedMatch) return `任务已完成，但有部分失败，${partialFailedMatch[1]}/${partialFailedMatch[2]} 条处理失败。`;

  const failedMatch = normalized.match(/^Task failed\. (\d+)\/(\d+) items ended in error\.$/);
  if (failedMatch) return `任务失败，${failedMatch[1]}/${failedMatch[2]} 条处理出错。`;

  return normalized;
}

export function formatOperationalImpact(value: string): string {
  const normalized = value.trim();
  if (!normalized) return "-";

  const runningQueuedMatch = normalized.match(/^running\s+(\d+)\s*\/\s*queued\s+(\d+)$/i);
  if (runningQueuedMatch) {
    return `运行中 ${runningQueuedMatch[1]} / 排队中 ${runningQueuedMatch[2]}`;
  }

  return normalized;
}
