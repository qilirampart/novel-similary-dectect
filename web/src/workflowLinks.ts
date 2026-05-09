type ReviewPathParams = {
  taskId?: string | number | null;
  resultId?: string | number | null;
  q?: string | null;
  status?: string | null;
  reviewStatus?: string | null;
};

export type ReviewQueryState = {
  taskId: string;
  resultId: number | null;
  q: string;
  status: string;
  reviewStatus: string;
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
  addSearchParam(searchParams, "resultId", params.resultId);
  addSearchParam(searchParams, "status", params.status);
  addSearchParam(searchParams, "reviewStatus", params.reviewStatus);
  addSearchParam(searchParams, "q", params.q);
  return searchParams;
}

export function parseReviewQueryState(searchParams: URLSearchParams): ReviewQueryState {
  return {
    taskId: searchParams.get("taskId") || "",
    resultId: parsePositiveId(searchParams.get("resultId")),
    status: searchParams.get("status") || "",
    reviewStatus: searchParams.get("reviewStatus") || "",
    q: searchParams.get("q") || ""
  };
}

export function buildReviewPath(params: ReviewPathParams = {}): string {
  const searchParams = buildReviewSearchParams(params);
  const search = searchParams.toString();
  return search ? `/review?${search}` : "/review";
}
