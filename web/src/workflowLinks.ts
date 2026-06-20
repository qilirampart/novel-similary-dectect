type ReviewPathParams = {
  taskId?: string | number | null;
  taskIds?: Array<string | number> | null;
  resultId?: string | number | null;
  q?: string | null;
  status?: string | null;
  reviewStatus?: string | null;
  threshold?: string | number | null;
};

export type ReviewQueryState = {
  taskId: string;
  taskIds: string[];
  resultId: number | null;
  q: string;
  status: string;
  reviewStatus: string;
  threshold: string;
};

const HIGH_RISK_REVIEW_LABEL = "\u5f3a\u8bc1\u636e";

function addSearchParam(params: URLSearchParams, key: string, value: string | number | null | undefined) {
  const nextValue = String(value ?? "").trim();
  if (nextValue) {
    params.set(key, nextValue);
  }
}

export function parsePositiveId(value: string | null): number | null {
  if (!value) return null;
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

export function isHighRiskReviewLabel(value: unknown): boolean {
  return String(value ?? "").trim() === HIGH_RISK_REVIEW_LABEL;
}

export function buildReviewSearchParams(params: ReviewPathParams = {}): URLSearchParams {
  const searchParams = new URLSearchParams();
  addSearchParam(searchParams, "taskId", params.taskId);
  if (Array.isArray(params.taskIds) && params.taskIds.length > 0) {
    const normalized = params.taskIds
      .map((taskId) => String(taskId ?? "").trim())
      .filter(Boolean)
      .join(",");
    addSearchParam(searchParams, "taskIds", normalized);
  }
  addSearchParam(searchParams, "resultId", params.resultId);
  addSearchParam(searchParams, "status", params.status);
  addSearchParam(searchParams, "reviewStatus", params.reviewStatus);
  addSearchParam(searchParams, "q", params.q);
  addSearchParam(searchParams, "threshold", params.threshold);
  return searchParams;
}

export function parseReviewQueryState(searchParams: URLSearchParams): ReviewQueryState {
  const taskIds = (searchParams.get("taskIds") || "")
    .split(",")
    .map((taskId) => taskId.trim())
    .filter(Boolean);
  return {
    taskId: searchParams.get("taskId") || "",
    taskIds,
    resultId: parsePositiveId(searchParams.get("resultId")),
    status: searchParams.get("status") || "",
    reviewStatus: searchParams.get("reviewStatus") || "",
    q: searchParams.get("q") || "",
    threshold: searchParams.get("threshold") || ""
  };
}

export function buildReviewPath(params: ReviewPathParams = {}): string {
  const searchParams = buildReviewSearchParams(params);
  const search = searchParams.toString();
  return search ? `/review?${search}` : "/review";
}
