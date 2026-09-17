export type DetectionMode = "reuse" | "rewrite";

export type CoverImportKind = "channels" | "videos" | "baseline";

export type CoverImportResponse = {
  import_id: string;
  import_kind: CoverImportKind;
  status: string;
  source_file_name: string;
  source_file_sha256: string;
  sheet_name: string | null;
  mapping: Record<string, string>;
  stats: Record<string, number>;
  error_message: string | null;
  conflict_samples: Array<Record<string, unknown>>;
  created_at: string;
  updated_at: string;
  confirmed_at: string | null;
  finished_at: string | null;
};

export type CoverRunSummary = {
  run_id: string;
  status: string;
  trigger_type: string;
  intensity: string;
  total_item_count: number;
  completed_item_count: number;
  failed_item_count: number;
  total_channel_count: number;
  status_message?: string | null;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
};

export type CoverRunListResponse = {
  items: CoverRunSummary[];
  total: number;
  limit: number;
  offset: number;
};

export type CoverChannelSummary = {
  channel_pk: number;
  platform: string;
  channel_id: string;
  name: string;
  source_url: string;
  active: boolean;
  operator_pk?: number | null;
  operator_name?: string | null;
  video_count: number;
  open_case_count: number;
  last_scan_at?: string | null;
  latest_scan_status?: string | null;
  latest_scan_completeness?: string | null;
  updated_at: string;
};

export type CoverChannelListResponse = {
  items: CoverChannelSummary[];
  total: number;
  limit: number;
  offset: number;
};

export type CoverChannelIdListResponse = {
  channel_pks: number[];
  total: number;
  truncated: boolean;
};

export type CoverChannelDeactivateResponse = {
  requested_count: number;
  deactivated_count: number;
  already_inactive_count: number;
  not_found_count: number;
};

export type CoverFilterOptionsResponse = {
  operators: Array<{
    operator_pk: number;
    name: string;
    channel_count: number;
  }>;
  channels: Array<{
    channel_pk: number;
    channel_id: string;
    name: string;
    operator_pk: number;
  }>;
};

export type CoverRunDetailResponse = {
  run: CoverRunSummary;
  channels: Array<{
    run_channel_id: number;
    channel_pk: number;
    channel_name: string;
    source_url: string;
    scan_status: string;
    completeness: string;
    discovered_count: number;
    error_message?: string | null;
  }>;
  items: Array<{
    task_item_id: number;
    video_pk: number;
    video_id: string;
    video_title: string;
    video_url: string;
    thumbnail_url: string;
    reason: string;
    stage: string;
    status: string;
    attempts: number;
    error_type?: string | null;
    error_message?: string | null;
    overall_risk?: string | null;
    confidence?: number | null;
    summary?: string | null;
  }>;
  item_total: number;
  item_limit: number;
  item_offset: number;
};

export type CoverRiskCaseSummary = {
  case_id: string;
  video_pk: number;
  video_id: string;
  video_title: string;
  video_url: string;
  thumbnail_url: string;
  current_status: string;
  opened_detection_id: string;
  opened_risk: string;
  opened_summary: string;
  opened_evidence: string;
  opened_confidence: number;
  opened_asset_id: string;
  latest_event_type?: string | null;
  opened_at: string;
  updated_at: string;
  closed_at?: string | null;
};

export type CoverRiskCaseEvent = {
  case_event_id: number;
  detection_id?: string | null;
  event_type: string;
  actor_type: string;
  actor_user_id?: number | null;
  reason: string;
  created_at: string;
  overall_risk?: string | null;
  risk_tags: string[];
  summary?: string | null;
  evidence?: string | null;
  confidence?: number | null;
  provider?: string | null;
  model?: string | null;
  duration_seconds?: number | null;
  asset_id?: string | null;
  content_sha256?: string | null;
  original_url?: string | null;
  fetched_url?: string | null;
  width?: number | null;
  height?: number | null;
};

export type CoverRiskCaseDetailResponse = {
  case: CoverRiskCaseSummary;
  events: CoverRiskCaseEvent[];
  reviews: Array<{
    case_review_id: number;
    detection_id?: string | null;
    action: string;
    reason: string;
    reviewed_by_user_id: number;
    reviewed_at: string;
  }>;
};

export type CoverRiskCaseListResponse = {
  items: CoverRiskCaseSummary[];
  total: number;
  limit: number;
  offset: number;
};

export type CoverResultSummary = {
  result_id: string;
  source: "current_detection" | "historical_import";
  video_pk: number;
  video_id: string;
  video_title: string;
  video_url: string;
  thumbnail_url: string;
  overall_risk: "risk" | "review" | "unknown";
  risk_tags: string[];
  summary: string;
  evidence: string;
  confidence: number;
  model: string;
  created_at: string;
};

export type CoverResultListResponse = {
  items: CoverResultSummary[];
  total: number;
  limit: number;
  offset: number;
  counts: Record<"all" | "risk" | "review" | "unknown", number>;
};

export type CoverRiskCaseReviewAction =
  | "confirm_rectified"
  | "false_positive"
  | "keep_open"
  | "mark_unavailable"
  | "reopen";

const DEFAULT_REQUEST_TIMEOUT_MS = 15_000;
const LONG_REQUEST_TIMEOUT_MS = 60_000;
const DOWNLOAD_REQUEST_TIMEOUT_MS = 120_000;

function normalizeBasePath(path: string): string {
  const trimmed = (path || "").trim();
  if (!trimmed) return "/api";
  const withLeadingSlash = trimmed.startsWith("/") ? trimmed : `/${trimmed}`;
  return withLeadingSlash.endsWith("/") && withLeadingSlash !== "/"
    ? withLeadingSlash.slice(0, -1)
    : withLeadingSlash;
}

const API_BASE_PATH = normalizeBasePath(import.meta.env.VITE_API_BASE_PATH || "/api");

function resolveApiPath(path: string): string {
  if (!path) return API_BASE_PATH;
  if (path.startsWith("/api/")) {
    return `${API_BASE_PATH}${path.slice(4)}`;
  }
  if (path === "/api") {
    return API_BASE_PATH;
  }
  return path;
}

function parseErrorDetail(errorText: string, fallback: string): string {
  if (!errorText) return fallback;
  try {
    const payload = JSON.parse(errorText) as { detail?: string };
    return payload.detail || errorText;
  } catch {
    return errorText;
  }
}

function parseDownloadFilename(contentDisposition: string | null, fallback: string): string {
  if (!contentDisposition) return fallback;

  const utf8Match = contentDisposition.match(/filename\*\s*=\s*UTF-8''([^;]+)/i);
  if (utf8Match?.[1]) {
    try {
      return decodeURIComponent(utf8Match[1]);
    } catch {
      return utf8Match[1];
    }
  }

  const basicMatch = contentDisposition.match(/filename\s*=\s*"?(.*?)"?(?:;|$)/i);
  if (basicMatch?.[1]) {
    return basicMatch[1];
  }

  return fallback;
}

type RequestOptions = RequestInit & {
  query?: Record<string, string | number | undefined>;
  timeoutMs?: number;
};

async function requestJson<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const url = new URL(resolveApiPath(path), window.location.origin);
  if (options.query) {
    for (const [key, value] of Object.entries(options.query)) {
      if (value === undefined || value === null || value === "") continue;
      url.searchParams.set(key, String(value));
    }
  }

  const controller = new AbortController();
  const timeoutId = window.setTimeout(
    () => controller.abort(),
    Math.max(Number(options.timeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS), 1)
  );

  let response: Response;
  try {
    response = await fetch(url.toString(), {
      ...options,
      credentials: "include",
      signal: controller.signal,
      headers: {
        ...(options.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
        ...(options.headers ?? {})
      }
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error("请求超时，请稍后重试");
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
  }

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const errorText = await response.text();
      detail = parseErrorDetail(errorText, detail);
    } catch {
      detail = response.statusText;
    }
    throw new Error(detail || `Request failed: ${response.status}`);
  }

  return (await response.json()) as T;
}

async function fetchWithTimeout(input: string, init: RequestInit = {}, timeoutMs = DOWNLOAD_REQUEST_TIMEOUT_MS): Promise<Response> {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), Math.max(timeoutMs, 1));
  try {
    return await fetch(input, {
      ...init,
      signal: controller.signal
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error("请求超时，请稍后重试");
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

export type ComparePayload = Record<string, any>;
export type CompareSingleResponse = {
  request_id: string;
  duration_seconds: number;
  payload: ComparePayload;
};

export type DramaSubtitleTaskCreateResponse = {
  task_id: string;
  status: string;
  source_file_name: string;
};

export type DramaSubtitleTaskDetailResponse = {
  task: Record<string, any>;
  items: Record<string, any>[];
};

export type DramaSubtitleEvidenceContextResponse = {
  context: Record<string, any>;
};

export type TaskListResponse = {
  items: Record<string, any>[];
  limit: number;
  offset: number;
};

export type BulkTaskDeleteResponse = {
  status: string;
  affected_count: number;
  message: string;
};

export type TaskDetailResponse = {
  task: Record<string, any>;
  items: Record<string, any>[];
  item_limit: number;
  item_offset: number;
  item_total: number;
  result_stats: Record<string, number>;
};

export type TaskCreateResponse = {
  task_id: string;
  status: string;
  detection_mode: string;
  source_file_name: string;
};

export type UserProfile = {
  user_id: number;
  username: string;
  display_name: string;
  role: string;
  is_active: boolean;
  created_at?: string | null;
  updated_at?: string | null;
  last_login_at?: string | null;
};

export type UserProfileResponse = {
  user: UserProfile;
};

export type BasicTaskResponse = {
  task: Record<string, any>;
};

export type TaskResultResponse = {
  result: Record<string, any>;
};

export type ResultListResponse = {
  items: Record<string, any>[];
  limit: number;
  offset: number;
  total?: number | null;
  stats?: Record<string, number> | null;
};

export type BulkActionResponse = {
  status: string;
  affected_count: number;
  message: string;
};

export type SystemStatusResponse = {
  payload: {
    cards: Array<{ label: string; value: string; hint: string; tone: string }>;
    services: Array<{ title: string; status: string; items: Array<[string, string]> }>;
    recent_exceptions: Array<{ time: string; module: string; type: string; summary: string; impact: string }>;
    slow_tasks: Array<{ id: string; type: string; duration: string; status: string; time: string }>;
  };
};

export type CoverMonitorOverviewResponse = {
  channel_count: number;
  video_count: number;
  risk_count: number;
  pending_review_count: number;
  risk_distribution: Record<"safe" | "review" | "risk" | "unknown", number>;
  latest_run: CoverRunSummary | null;
};

export function compareSingle(params: {
  queryText: string;
  detectionMode: DetectionMode;
  topK?: number;
  compareTopK?: number;
  mergedTopK?: number;
  candidateDisplayScoreThreshold?: number;
}): Promise<CompareSingleResponse> {
  return requestJson<CompareSingleResponse>("/api/v1/compare/single", {
    method: "POST",
    timeoutMs: LONG_REQUEST_TIMEOUT_MS,
    body: JSON.stringify({
      query_text: params.queryText,
      detection_mode: params.detectionMode,
      top_k: params.topK,
      compare_top_k: params.compareTopK,
      merged_top_k: params.mergedTopK,
      candidate_display_score_threshold: params.candidateDisplayScoreThreshold
    })
  });
}

export function compareDramaSubtitles(params: {
  queryText: string;
  topK?: number;
  windowLimit?: number;
  languageCode?: string;
}): Promise<CompareSingleResponse> {
  return requestJson<CompareSingleResponse>("/api/v1/drama-subtitles/compare", {
    method: "POST",
    timeoutMs: LONG_REQUEST_TIMEOUT_MS,
    body: JSON.stringify({
      query_text: params.queryText,
      top_k: params.topK,
      window_limit: params.windowLimit,
      language_code: params.languageCode || undefined
    })
  });
}

export function getDramaSubtitleEvidenceContext(windowUid: string, contextChars = 2400): Promise<DramaSubtitleEvidenceContextResponse> {
  return requestJson<DramaSubtitleEvidenceContextResponse>(
    `/api/v1/drama-subtitles/evidence-context/${encodeURIComponent(windowUid)}`,
    { query: { context_chars: contextChars, before_lines: 6 }, timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS }
  );
}

export function createDramaSubtitleTask(params: {
  file: File;
  topK?: number;
  windowLimit?: number;
}): Promise<DramaSubtitleTaskCreateResponse> {
  const formData = new FormData();
  formData.append("file", params.file);
  if (params.topK !== undefined) formData.append("top_k", String(params.topK));
  if (params.windowLimit !== undefined) formData.append("window_limit", String(params.windowLimit));
  return requestJson<DramaSubtitleTaskCreateResponse>("/api/v1/drama-subtitles/tasks", {
    method: "POST",
    timeoutMs: LONG_REQUEST_TIMEOUT_MS,
    body: formData
  });
}

export function listDramaSubtitleTasks(limit = 20, offset = 0): Promise<TaskListResponse> {
  return requestJson<TaskListResponse>("/api/v1/drama-subtitles/tasks", {
    query: { limit, offset }
  });
}

export function getDramaSubtitleTaskDetail(taskId: string): Promise<DramaSubtitleTaskDetailResponse> {
  return requestJson<DramaSubtitleTaskDetailResponse>(`/api/v1/drama-subtitles/tasks/${encodeURIComponent(taskId)}`);
}

export function controlDramaSubtitleTask(taskId: string, action: "pause" | "resume" | "cancel" | "delete"): Promise<BasicTaskResponse> {
  const path = `/api/v1/drama-subtitles/tasks/${encodeURIComponent(taskId)}`;
  if (action === "delete") {
    return requestJson<BasicTaskResponse>(path, { method: "DELETE" });
  }
  return requestJson<BasicTaskResponse>(`${path}/${action}`, { method: "POST" });
}

export function saveDramaSubtitleReview(taskItemId: number, reviewStatus: string, reviewNote = ""): Promise<TaskResultResponse> {
  return requestJson<TaskResultResponse>(`/api/v1/drama-subtitles/tasks/items/${taskItemId}/review`, {
    method: "POST",
    body: JSON.stringify({ review_status: reviewStatus, review_note: reviewNote })
  });
}

export async function downloadDramaSubtitleReviewExport(taskId: string): Promise<string> {
  const response = await fetchWithTimeout(
    resolveApiPath(`/api/v1/drama-subtitles/tasks/${encodeURIComponent(taskId)}/exports/review-xlsx`),
    { credentials: "include" }
  );
  if (!response.ok) {
    throw new Error(parseErrorDetail(await response.text(), response.statusText || "导出短剧复核结果失败"));
  }
  const fileName = parseDownloadFilename(response.headers.get("content-disposition"), "短剧字幕复核导出.xlsx");
  const blobUrl = window.URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = blobUrl;
  anchor.download = fileName;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => window.URL.revokeObjectURL(blobUrl), 1000);
  return fileName;
}
export function createCompareTask(params: {
  file: File;
  detectionMode: DetectionMode;
  topK?: number;
  compareTopK?: number;
  mergedTopK?: number;
}): Promise<TaskCreateResponse> {
  const formData = new FormData();
  formData.append("file", params.file);
  formData.append("detection_mode", params.detectionMode);
  if (params.topK !== undefined) formData.append("top_k", String(params.topK));
  if (params.compareTopK !== undefined) formData.append("compare_top_k", String(params.compareTopK));
  if (params.mergedTopK !== undefined) formData.append("merged_top_k", String(params.mergedTopK));

  return requestJson<TaskCreateResponse>("/api/v1/tasks", {
    method: "POST",
    timeoutMs: LONG_REQUEST_TIMEOUT_MS,
    body: formData
  });
}

export function listTasks(limit = 20, offset = 0): Promise<TaskListResponse> {
  return requestJson<TaskListResponse>("/api/v1/tasks", {
    query: { limit, offset }
  });
}

export function getTaskDetail(taskId: string, itemLimit = 20, itemOffset = 0): Promise<TaskDetailResponse> {
  return requestJson<TaskDetailResponse>(`/api/v1/tasks/${encodeURIComponent(taskId)}`, {
    query: { item_limit: itemLimit, item_offset: itemOffset }
  });
}

export function cancelTask(taskId: string): Promise<BasicTaskResponse> {
  return requestJson<BasicTaskResponse>(`/api/v1/tasks/${encodeURIComponent(taskId)}/cancel`, {
    method: "POST"
  });
}

export function pauseTask(taskId: string): Promise<BasicTaskResponse> {
  return requestJson<BasicTaskResponse>(`/api/v1/tasks/${encodeURIComponent(taskId)}/pause`, {
    method: "POST"
  });
}

export function resumeTask(taskId: string): Promise<BasicTaskResponse> {
  return requestJson<BasicTaskResponse>(`/api/v1/tasks/${encodeURIComponent(taskId)}/resume`, {
    method: "POST"
  });
}

export function deleteTask(taskId: string): Promise<BasicTaskResponse> {
  return requestJson<BasicTaskResponse>(`/api/v1/tasks/${encodeURIComponent(taskId)}`, {
    method: "DELETE"
  });
}

export function retryTask(taskId: string): Promise<TaskCreateResponse> {
  return requestJson<TaskCreateResponse>(`/api/v1/tasks/${encodeURIComponent(taskId)}/retry`, {
    method: "POST"
  });
}

export function listResults(params: {
  limit?: number;
  offset?: number;
  taskId?: string;
  status?: string;
  reviewStatus?: string;
  sortBy?: string;
  dedupeLatest?: boolean;
  q?: string;
  excludeCleared?: boolean;
  candidateScoreThreshold?: number;
} = {}): Promise<ResultListResponse> {
  return requestJson<ResultListResponse>("/api/v1/results", {
    query: {
      limit: params.limit ?? 20,
      offset: params.offset ?? 0,
      task_id: params.taskId,
      status: params.status,
      review_status: params.reviewStatus,
      sort_by: params.sortBy,
      dedupe_latest: params.dedupeLatest ? "true" : undefined,
      q: params.q,
      exclude_cleared: params.excludeCleared ? "true" : undefined,
      candidate_score_threshold: params.candidateScoreThreshold
    }
  });
}

export function getResultDetail(resultId: number): Promise<TaskResultResponse> {
  return requestJson<TaskResultResponse>(`/api/v1/results/${resultId}`);
}

export function saveReview(resultId: number, params: {
  reviewStatus: string;
  reviewNote?: string;
}): Promise<TaskResultResponse> {
  return requestJson<TaskResultResponse>(`/api/v1/results/${resultId}/review`, {
    method: "POST",
    body: JSON.stringify({
      review_status: params.reviewStatus,
      review_note: params.reviewNote ?? ""
    })
  });
}

export function clearPendingReviewResults(): Promise<BulkActionResponse> {
  return requestJson<BulkActionResponse>("/api/v1/results/clear-pending", {
    method: "POST"
  });
}

export function reviewExportUrl(params: {
  taskId?: string;
  status?: string;
  reviewStatus?: string;
  sortBy?: string;
  dedupeLatest?: boolean;
  q?: string;
  candidateScoreThreshold?: number;
} = {}): string {
  const url = new URL(resolveApiPath("/api/v1/results/exports/review-xlsx"), window.location.origin);
  if (params.taskId) url.searchParams.set("task_id", params.taskId);
  if (params.status) url.searchParams.set("status", params.status);
  if (params.reviewStatus) url.searchParams.set("review_status", params.reviewStatus);
  if (params.sortBy) url.searchParams.set("sort_by", params.sortBy);
  if (params.dedupeLatest) url.searchParams.set("dedupe_latest", "true");
  if (params.q) url.searchParams.set("q", params.q);
  if (params.candidateScoreThreshold !== undefined) {
    url.searchParams.set("candidate_score_threshold", String(params.candidateScoreThreshold));
  }
  return url.toString();
}

export async function downloadReviewExport(params: {
  taskId?: string;
  status?: string;
  reviewStatus?: string;
  sortBy?: string;
  dedupeLatest?: boolean;
  q?: string;
  candidateScoreThreshold?: number;
} = {}): Promise<string> {
  const response = await fetchWithTimeout(reviewExportUrl(params), {
    credentials: "include"
  });
  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(parseErrorDetail(errorText, response.statusText || `Request failed: ${response.status}`));
  }

  const fileName = parseDownloadFilename(
    response.headers.get("content-disposition"),
    "复核结果导出.xlsx"
  );
  const blob = await response.blob();
  const blobUrl = window.URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = blobUrl;
  anchor.download = fileName;
  anchor.rel = "noopener";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => window.URL.revokeObjectURL(blobUrl), 1000);
  return fileName;
}
export async function deleteTasks(taskIds: string[]): Promise<BulkTaskDeleteResponse> {
  const normalizedTaskIds = Array.from(
    new Set(taskIds.map((taskId) => String(taskId || "").trim()).filter(Boolean))
  );
  const settled = await Promise.allSettled(
    normalizedTaskIds.map(async (taskId) => {
      await deleteTask(taskId);
      return taskId;
    })
  );

  const affectedCount = settled.filter((item) => item.status === "fulfilled").length;
  const failedCount = settled.length - affectedCount;

  if (affectedCount <= 0 && failedCount > 0) {
    const firstFailure = settled.find((item) => item.status === "rejected");
    const reason = firstFailure && firstFailure.status === "rejected" ? firstFailure.reason : null;
    throw reason instanceof Error ? reason : new Error("删除任务失败");
  }

  return {
    status: "ok",
    affected_count: affectedCount,
    message:
      failedCount > 0
        ? `deleted ${affectedCount} tasks, ${failedCount} failed`
        : `deleted ${affectedCount} tasks`
  };
}

export function login(username: string, password: string): Promise<UserProfileResponse> {
  return requestJson<UserProfileResponse>("/api/v1/auth/login", {
    method: "POST",
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    body: JSON.stringify({ username, password })
  });
}

export function logout(): Promise<{ status: string }> {
  return requestJson<{ status: string }>("/api/v1/auth/logout", {
    method: "POST",
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS
  });
}

export function getCurrentUser(): Promise<UserProfileResponse> {
  return requestJson<UserProfileResponse>("/api/v1/auth/me", {
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS
  });
}

export function getSystemStatus(): Promise<SystemStatusResponse> {
  return requestJson<SystemStatusResponse>("/api/v1/system/status", {
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS
  });
}

export function getCoverMonitorOverview(): Promise<CoverMonitorOverviewResponse> {
  return requestJson<CoverMonitorOverviewResponse>("/api/v1/cover-monitor/overview", {
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS
  });
}

export function listCoverMonitorChannels(params: {
  keyword?: string;
  active?: boolean;
  operatorPk?: number;
  limit?: number;
  offset?: number;
} = {}): Promise<CoverChannelListResponse> {
  return requestJson<CoverChannelListResponse>("/api/v1/cover-monitor/channels", {
    query: {
      keyword: params.keyword || undefined,
      active: params.active == null ? undefined : String(params.active),
      operator_pk: params.operatorPk,
      limit: params.limit ?? 50,
      offset: params.offset ?? 0
    },
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS
  });
}

export function listCoverMonitorChannelIds(params: {
  keyword?: string;
  active?: boolean;
  operatorPk?: number;
} = {}): Promise<CoverChannelIdListResponse> {
  return requestJson<CoverChannelIdListResponse>("/api/v1/cover-monitor/channels/ids", {
    query: {
      keyword: params.keyword || undefined,
      active: params.active == null ? undefined : String(params.active),
      operator_pk: params.operatorPk
    },
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS
  });
}

export function deactivateCoverMonitorChannels(
  channelPks: number[]
): Promise<CoverChannelDeactivateResponse> {
  return requestJson<CoverChannelDeactivateResponse>("/api/v1/cover-monitor/channels/deactivate", {
    method: "POST",
    body: JSON.stringify({ channel_pks: channelPks }),
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS
  });
}

export function getCoverMonitorFilterOptions(
  operatorPk?: number
): Promise<CoverFilterOptionsResponse> {
  return requestJson<CoverFilterOptionsResponse>("/api/v1/cover-monitor/filter-options", {
    query: { operator_pk: operatorPk },
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS
  });
}

export function createCoverMonitorRun(params: {
  intensity: "conservative" | "standard" | "strict";
  includeShorts: boolean;
  forceRefresh: boolean;
  maxItemsPerScope: number;
  channelPks?: number[];
}): Promise<CoverRunSummary> {
  return requestJson<CoverRunSummary>("/api/v1/cover-monitor/runs", {
    method: "POST",
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    body: JSON.stringify({
      intensity: params.intensity,
      include_shorts: params.includeShorts,
      force_refresh: params.forceRefresh,
      max_items_per_scope: params.maxItemsPerScope,
      channel_pks: params.channelPks ?? []
    })
  });
}

export function controlCoverMonitorRun(
  runId: string,
  action: "pause" | "resume" | "cancel"
): Promise<CoverRunSummary> {
  return requestJson<CoverRunSummary>(
    `/api/v1/cover-monitor/runs/${encodeURIComponent(runId)}/${action}`,
    { method: "POST", timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS }
  );
}

export function listCoverMonitorRuns(limit = 50, offset = 0): Promise<CoverRunListResponse> {
  return requestJson<CoverRunListResponse>("/api/v1/cover-monitor/runs", {
    query: { limit, offset },
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS
  });
}

export function getCoverMonitorRunDetail(
  runId: string,
  itemLimit = 50,
  itemOffset = 0
): Promise<CoverRunDetailResponse> {
  return requestJson<CoverRunDetailResponse>(
    `/api/v1/cover-monitor/runs/${encodeURIComponent(runId)}/detail`,
    {
      query: { item_limit: itemLimit, item_offset: itemOffset },
      timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS
    }
  );
}

export async function downloadCoverMonitorRunExport(
  runId: string,
  kind: "new-findings" | "historical-rectification"
): Promise<string> {
  const response = await fetchWithTimeout(
    resolveApiPath(`/api/v1/cover-monitor/runs/${encodeURIComponent(runId)}/exports/${kind}`),
    { credentials: "include" },
    DOWNLOAD_REQUEST_TIMEOUT_MS
  );
  if (!response.ok) {
    throw new Error(parseErrorDetail(await response.text(), response.statusText || "封面巡检报告导出失败"));
  }
  const fallback = kind === "new-findings" ? "封面新增检测报告.xlsx" : "封面历史整改报告.xlsx";
  const fileName = parseDownloadFilename(response.headers.get("content-disposition"), fallback);
  const blobUrl = window.URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = blobUrl;
  anchor.download = fileName;
  anchor.rel = "noopener";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => window.URL.revokeObjectURL(blobUrl), 1000);
  return fileName;
}

export function listCoverRiskCases(
  status = "needs_review",
  limit = 50,
  offset = 0,
  operatorPk?: number,
  channelPk?: number
): Promise<CoverRiskCaseListResponse> {
  return requestJson<CoverRiskCaseListResponse>("/api/v1/cover-monitor/risk-cases", {
    query: {
      status: status || undefined,
      operator_pk: operatorPk,
      channel_pk: channelPk,
      limit,
      offset
    },
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS
  });
}

export function listCoverMonitorResults(
  overallRisk = "",
  limit = 20,
  offset = 0,
  operatorPk?: number,
  channelPk?: number
): Promise<CoverResultListResponse> {
  return requestJson<CoverResultListResponse>("/api/v1/cover-monitor/results", {
    query: {
      overall_risk: overallRisk || undefined,
      operator_pk: operatorPk,
      channel_pk: channelPk,
      limit,
      offset
    },
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS
  });
}

export function getCoverRiskCaseDetail(caseId: string): Promise<CoverRiskCaseDetailResponse> {
  return requestJson<CoverRiskCaseDetailResponse>(
    `/api/v1/cover-monitor/risk-cases/${encodeURIComponent(caseId)}`,
    { timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS }
  );
}

export function reviewCoverRiskCase(
  caseId: string,
  action: CoverRiskCaseReviewAction,
  reason: string
): Promise<CoverRiskCaseDetailResponse> {
  return requestJson<CoverRiskCaseDetailResponse>(
    `/api/v1/cover-monitor/risk-cases/${encodeURIComponent(caseId)}/review`,
    {
      method: "POST",
      timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
      body: JSON.stringify({ action, reason })
    }
  );
}

export function coverRiskCaseAssetUrl(caseId: string, assetId: string): string {
  return resolveApiPath(
    `/api/v1/cover-monitor/risk-cases/${encodeURIComponent(caseId)}/assets/${encodeURIComponent(assetId)}`
  );
}

export function previewCoverMonitorImport(params: {
  file: File;
  importKind: CoverImportKind;
  sheetName?: string;
}): Promise<CoverImportResponse> {
  const formData = new FormData();
  formData.append("file", params.file);
  formData.append("import_kind", params.importKind);
  if (params.sheetName?.trim()) formData.append("sheet_name", params.sheetName.trim());
  return requestJson<CoverImportResponse>("/api/v1/cover-monitor/imports/preview", {
    method: "POST",
    body: formData,
    timeoutMs: 5 * 60_000
  });
}

export function confirmCoverMonitorImport(importId: string): Promise<CoverImportResponse> {
  return requestJson<CoverImportResponse>(
    `/api/v1/cover-monitor/imports/${encodeURIComponent(importId)}/confirm`,
    { method: "POST", timeoutMs: 5 * 60_000 }
  );
}

export function exportTaskUrl(taskId: string, kind: "summary" | "review" | "json"): string {
  return resolveApiPath(`/api/v1/tasks/${encodeURIComponent(taskId)}/exports/${kind}`);
}

export async function downloadTaskExport(taskId: string, kind: "summary" | "review" | "json"): Promise<string> {
  const response = await fetchWithTimeout(exportTaskUrl(taskId, kind), {
    credentials: "include"
  });
  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(parseErrorDetail(errorText, response.statusText || `Request failed: ${response.status}`));
  }

  const fallbackName = kind === "summary" ? "task_summary.csv" : kind === "review" ? "task_review_rows.csv" : "task_summary.json";
  const fileName = parseDownloadFilename(response.headers.get("content-disposition"), fallbackName);
  const blob = await response.blob();
  const blobUrl = window.URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = blobUrl;
  anchor.download = fileName;
  anchor.rel = "noopener";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => window.URL.revokeObjectURL(blobUrl), 1000);
  return fileName;
}
