export type DetectionMode = "reuse" | "rewrite";

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
};

async function requestJson<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const url = new URL(path, window.location.origin);
  if (options.query) {
    for (const [key, value] of Object.entries(options.query)) {
      if (value === undefined || value === null || value === "") continue;
      url.searchParams.set(key, String(value));
    }
  }

  const response = await fetch(url.toString(), {
    ...options,
    headers: {
      ...(options.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...(options.headers ?? {})
    }
  });

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

export type ComparePayload = Record<string, any>;
export type CompareSingleResponse = {
  request_id: string;
  duration_seconds: number;
  payload: ComparePayload;
};

export type TaskListResponse = {
  items: Record<string, any>[];
  limit: number;
  offset: number;
};

export type TaskDetailResponse = {
  task: Record<string, any>;
  items: Record<string, any>[];
  item_limit: number;
  item_offset: number;
};

export type TaskCreateResponse = {
  task_id: string;
  status: string;
  detection_mode: string;
  source_file_name: string;
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
};

export type SystemStatusResponse = {
  payload: {
    cards: Array<{ label: string; value: string; hint: string; tone: string }>;
    services: Array<{ title: string; status: string; items: Array<[string, string]> }>;
    recent_exceptions: Array<{ time: string; module: string; type: string; summary: string; impact: string }>;
    slow_tasks: Array<{ id: string; type: string; duration: string; status: string; time: string }>;
  };
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

export function createCompareTask(params: {
  file: File;
  detectionMode: DetectionMode;
  topK?: number;
  compareTopK?: number;
  mergedTopK?: number;
  candidateDisplayScoreThreshold?: number;
  createdBy?: string;
}): Promise<TaskCreateResponse> {
  const formData = new FormData();
  formData.append("file", params.file);
  formData.append("detection_mode", params.detectionMode);
  if (params.topK !== undefined) formData.append("top_k", String(params.topK));
  if (params.compareTopK !== undefined) formData.append("compare_top_k", String(params.compareTopK));
  if (params.mergedTopK !== undefined) formData.append("merged_top_k", String(params.mergedTopK));
  if (params.candidateDisplayScoreThreshold !== undefined) {
    formData.append("candidate_display_score_threshold", String(params.candidateDisplayScoreThreshold));
  }
  if (params.createdBy) formData.append("created_by", params.createdBy);

  return requestJson<TaskCreateResponse>("/api/v1/tasks", {
    method: "POST",
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
} = {}): Promise<ResultListResponse> {
  return requestJson<ResultListResponse>("/api/v1/results", {
    query: {
      limit: params.limit ?? 20,
      offset: params.offset ?? 0,
      task_id: params.taskId,
      status: params.status,
      review_status: params.reviewStatus,
      sort_by: params.sortBy
    }
  });
}

export function getResultDetail(resultId: number): Promise<TaskResultResponse> {
  return requestJson<TaskResultResponse>(`/api/v1/results/${resultId}`);
}

export function saveReview(resultId: number, params: {
  reviewStatus: string;
  reviewerName?: string;
  reviewNote?: string;
}): Promise<TaskResultResponse> {
  return requestJson<TaskResultResponse>(`/api/v1/results/${resultId}/review`, {
    method: "POST",
    body: JSON.stringify({
      review_status: params.reviewStatus,
      reviewer_name: params.reviewerName ?? "",
      review_note: params.reviewNote ?? ""
    })
  });
}

export function getSystemStatus(): Promise<SystemStatusResponse> {
  return requestJson<SystemStatusResponse>("/api/v1/system/status");
}

export function exportTaskUrl(taskId: string, kind: "summary" | "review" | "json"): string {
  return `/api/v1/tasks/${encodeURIComponent(taskId)}/exports/${kind}`;
}

export async function downloadTaskExport(taskId: string, kind: "summary" | "review" | "json"): Promise<string> {
  const response = await fetch(exportTaskUrl(taskId, kind));
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
