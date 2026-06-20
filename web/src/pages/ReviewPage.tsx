import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import {
  clearPendingReviewResults,
  downloadReviewExport,
  getResultDetail,
  listResults,
  listTasks,
  saveReview
} from "../api";
import {
  formatConfidenceLabel,
  formatMetricLabel,
  formatReviewLabel,
  formatSemanticStatusLabel,
  formatTaskStatusLabel
} from "../displayText";
import { buildEvidenceHighlightRanges, renderHighlightedEvidence } from "../evidenceHighlight";
import { Icon } from "../icons";
import { PaginationBar } from "../PaginationBar";
import { StatusState } from "../StatusState";
import { buildReviewSearchParams, parseReviewQueryState } from "../workflowLinks";

const DEFAULT_REVIEW_STATUS = "pending";
const REVIEW_PAGE_SIZE = 8;
const REVIEW_RESULT_SORT_BY = "score_desc";
const DEFAULT_CANDIDATE_DISPLAY_SCORE_THRESHOLD = 0.01;
const REVIEW_THRESHOLD_STORAGE_KEY = "novel-compare-review-threshold";

const REVIEW_STATUS_OPTIONS = [
  { value: "pending", label: "待复核" },
  { value: "confirmed_high_risk", label: "确认高风险" },
  { value: "needs_followup", label: "继续跟进" },
  { value: "false_positive", label: "误报" }
] as const;

function clipText(value: unknown, limit = 24): string {
  const text = String(value ?? "").trim();
  if (!text) return "-";
  return text.length <= limit ? text : `${text.slice(0, limit).trim()}...`;
}

function formatEpisodeLabel(value: unknown): string {
  const text = String(value ?? "").trim();
  if (!text) return "-";
  if (text.startsWith("第") && text.endsWith("集")) return text;
  return `第${text}集`;
}

function formatDuration(value: unknown): string {
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric < 0) return "-";
  if (numeric < 1) return `${numeric.toFixed(2)}s`;
  if (numeric < 10) return `${numeric.toFixed(1)}s`;
  return `${numeric.toFixed(0)}s`;
}

function metricPercent(value: unknown): number {
  const numeric = Number.parseFloat(String(value ?? ""));
  if (Number.isNaN(numeric)) return 0;
  if (numeric <= 1) return Math.round(numeric * 100);
  return Math.min(Math.round(numeric), 100);
}

function formatScore(value: unknown): string {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toFixed(2) : "-";
}

function formatReviewStatus(value: string): string {
  const normalized = value.trim();
  if (!normalized) return "待复核";

  const known = REVIEW_STATUS_OPTIONS.find((item) => item.value === normalized);
  if (known) return known.label;

  return normalized
    .split("_")
    .join(" ")
    .replace(/\b\w/g, (char: string) => char.toUpperCase());
}

function clampThreshold(value: number): number {
  if (!Number.isFinite(value)) return DEFAULT_CANDIDATE_DISPLAY_SCORE_THRESHOLD;
  return Math.max(0, Math.min(1, value));
}

function readStoredThreshold(): number {
  if (typeof window === "undefined") return DEFAULT_CANDIDATE_DISPLAY_SCORE_THRESHOLD;
  const raw = window.localStorage.getItem(REVIEW_THRESHOLD_STORAGE_KEY);
  const numeric = Number(raw);
  return clampThreshold(numeric);
}

function parseThresholdText(value: string): number {
  const text = String(value ?? "").trim();
  if (!text) return readStoredThreshold();
  return clampThreshold(Number(text));
}

function buildFilterKey(filters: {
  taskFilter?: string;
  resultStatusFilter?: string;
  reviewStatusFilter?: string;
  textFilter?: string;
  dedupeLatest?: boolean;
  displayThreshold?: number;
}) {
  return [
    filters.taskFilter ?? "",
    filters.resultStatusFilter ?? "",
    filters.reviewStatusFilter ?? "",
    filters.textFilter ?? "",
    filters.dedupeLatest ? "1" : "0",
    String(filters.displayThreshold ?? DEFAULT_CANDIDATE_DISPLAY_SCORE_THRESHOLD)
  ].join("\u0001");
}

export function ReviewPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [tasks, setTasks] = useState<Record<string, any>[]>([]);
  const [tasksLoading, setTasksLoading] = useState(true);
  const [results, setResults] = useState<Record<string, any>[]>([]);
  const [resultStats, setResultStats] = useState<Record<string, number>>({});
  const [resultTotal, setResultTotal] = useState(0);
  const [selectedResultId, setSelectedResultId] = useState<number | null>(null);
  const [detail, setDetail] = useState<Record<string, any> | null>(null);
  const [isEvidenceModalOpen, setIsEvidenceModalOpen] = useState(false);
  const [reviewNote, setReviewNote] = useState("");
  const [reviewStatus, setReviewStatus] = useState(DEFAULT_REVIEW_STATUS);
  const [taskFilter, setTaskFilter] = useState("");
  const [resultStatusFilter, setResultStatusFilter] = useState("");
  const [reviewStatusFilter, setReviewStatusFilter] = useState("");
  const [textFilter, setTextFilter] = useState("");
  const [dedupeLatest, setDedupeLatest] = useState(true);
  const [displayThreshold, setDisplayThreshold] = useState<number>(() => readStoredThreshold());
  const [resultPage, setResultPage] = useState(1);
  const [resultsLoading, setResultsLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [isClearingPending, setIsClearingPending] = useState(false);
  const [isExporting, setIsExporting] = useState(false);
  const [syncedQueueHeight, setSyncedQueueHeight] = useState<number | null>(null);
  const [error, setError] = useState("");
  const hasSelection = selectedResultId !== null;
  const detailRequestRef = useRef(0);
  const appliedFilterKeyRef = useRef<string | null>(null);
  const detailInFlightKeyRef = useRef<string | null>(null);
  const detailInFlightPromiseRef = useRef<Promise<void> | null>(null);
  const resultsInFlightKeyRef = useRef<string | null>(null);
  const resultsInFlightPromiseRef = useRef<Promise<void> | null>(null);
  const skipInitialPageLoadRef = useRef(true);
  const reviewDetailRef = useRef<HTMLElement | null>(null);

  async function loadTaskOptions() {
    setTasksLoading(true);
    try {
      const response = await listTasks(100, 0);
      setTasks(response.items);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "加载任务列表失败");
    } finally {
      setTasksLoading(false);
    }
  }

  function getAppliedFilters(
    overrides?: {
      taskFilter?: string;
      resultStatusFilter?: string;
      reviewStatusFilter?: string;
      textFilter?: string;
      dedupeLatest?: boolean;
      displayThreshold?: number;
    }
  ) {
    return {
      taskFilter: overrides?.taskFilter ?? taskFilter,
      resultStatusFilter: overrides?.resultStatusFilter ?? resultStatusFilter,
      reviewStatusFilter: overrides?.reviewStatusFilter ?? reviewStatusFilter,
      textFilter: overrides?.textFilter ?? textFilter,
      dedupeLatest: overrides?.dedupeLatest ?? dedupeLatest,
      displayThreshold: overrides?.displayThreshold ?? displayThreshold
    };
  }

  function buildSearchParams(
    overrides?: {
      taskFilter?: string;
      resultStatusFilter?: string;
      reviewStatusFilter?: string;
      textFilter?: string;
      selectedResultId?: number | null;
      dedupeLatest?: boolean;
      displayThreshold?: number;
    }
  ) {
    const next = getAppliedFilters(overrides);
    const nextResultId = overrides?.selectedResultId ?? selectedResultId;
    return buildReviewSearchParams({
      taskId: next.taskFilter,
      status: next.resultStatusFilter,
      reviewStatus: next.reviewStatusFilter,
      q: next.textFilter,
      resultId: nextResultId,
      threshold: next.displayThreshold.toFixed(2)
    });
  }

  async function loadDetail(resultId: number) {
    const requestId = ++detailRequestRef.current;
    setDetailLoading(true);
    setSelectedResultId(resultId);
    setDetail(null);
    setReviewStatus(DEFAULT_REVIEW_STATUS);
    setReviewNote("");
    try {
      const resultDetail = await getResultDetail(resultId);
      if (detailRequestRef.current !== requestId) return;
      const nextResult = resultDetail.result;
      setDetail(nextResult);
      setReviewStatus(nextResult.review?.review_status || DEFAULT_REVIEW_STATUS);
      setReviewNote(nextResult.review?.review_note || "");
    } catch (requestError) {
      if (detailRequestRef.current !== requestId) return;
      setError(requestError instanceof Error ? requestError.message : "加载复核详情失败");
    } finally {
      if (detailRequestRef.current === requestId) {
        setDetailLoading(false);
      }
    }
  }

  async function loadResults(
    targetResultId?: number,
    overrides?: {
      taskFilter?: string;
      resultStatusFilter?: string;
      reviewStatusFilter?: string;
      textFilter?: string;
      dedupeLatest?: boolean;
      displayThreshold?: number;
    },
    pageOverride?: number
  ) {
    setResultsLoading(true);
    setError("");
    try {
      const {
        taskFilter: nextTaskFilter,
        resultStatusFilter: nextResultStatusFilter,
        reviewStatusFilter: nextReviewStatusFilter,
        textFilter: nextTextFilter,
        dedupeLatest: nextDedupeLatest,
        displayThreshold: nextDisplayThreshold
      } = getAppliedFilters(overrides);
      const requestedPage = Math.max(pageOverride ?? resultPage, 1);
      const response = await listResults({
        limit: REVIEW_PAGE_SIZE,
        offset: (requestedPage - 1) * REVIEW_PAGE_SIZE,
        taskId: nextTaskFilter.trim() || undefined,
        status: nextResultStatusFilter || undefined,
        reviewStatus: nextReviewStatusFilter || undefined,
        sortBy: REVIEW_RESULT_SORT_BY,
        dedupeLatest: nextDedupeLatest,
        q: nextTextFilter.trim() || undefined,
        excludeCleared: true,
        candidateScoreThreshold: nextDisplayThreshold
      });

      const nextItems = Array.isArray(response.items) ? response.items : [];
      const nextTotal = Math.max(Number(response.total ?? nextItems.length ?? 0), 0);
      const nextPageCount = Math.max(Math.ceil(nextTotal / REVIEW_PAGE_SIZE), 1);
      const normalizedPage = Math.min(requestedPage, nextPageCount);

      if (normalizedPage !== requestedPage) {
        await requestResults(undefined, overrides, normalizedPage);
        return;
      }

      const retainedSelectedId =
        selectedResultId && nextItems.some((item) => Number(item.result_id) === selectedResultId)
          ? selectedResultId
          : undefined;
      const nextId = targetResultId && nextItems.some((item) => Number(item.result_id) === targetResultId)
        ? targetResultId
        : retainedSelectedId ?? (nextItems[0]?.result_id as number | undefined);

      setResults(nextItems);
      setResultTotal(nextTotal);
      setResultStats(response.stats ?? {});
      setResultPage(normalizedPage);

      if (!nextId) {
        setSelectedResultId(null);
        setDetail(null);
        setDetailLoading(false);
        setReviewStatus(DEFAULT_REVIEW_STATUS);
        setReviewNote("");
        return;
      }

      void requestDetail(nextId);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "加载复核结果失败");
    } finally {
      setResultsLoading(false);
    }
  }

  async function requestDetail(resultId: number) {
    const requestKey = String(resultId);
    if (detailInFlightKeyRef.current === requestKey && detailInFlightPromiseRef.current) {
      await detailInFlightPromiseRef.current;
      return;
    }
    const task = loadDetail(resultId);
    detailInFlightKeyRef.current = requestKey;
    detailInFlightPromiseRef.current = task;
    try {
      await task;
    } finally {
      if (detailInFlightPromiseRef.current === task) {
        detailInFlightKeyRef.current = null;
        detailInFlightPromiseRef.current = null;
      }
    }
  }

  async function requestResults(
    targetResultId?: number,
    overrides?: {
      taskFilter?: string;
      resultStatusFilter?: string;
      reviewStatusFilter?: string;
      textFilter?: string;
      dedupeLatest?: boolean;
      displayThreshold?: number;
    },
    pageOverride?: number
  ) {
    const {
      taskFilter: nextTaskFilter,
      resultStatusFilter: nextResultStatusFilter,
      reviewStatusFilter: nextReviewStatusFilter,
      textFilter: nextTextFilter,
      dedupeLatest: nextDedupeLatest,
      displayThreshold: nextDisplayThreshold
    } = getAppliedFilters(overrides);
    const requestedPage = Math.max(pageOverride ?? resultPage, 1);
    const requestKey = JSON.stringify({
      taskFilter: nextTaskFilter.trim(),
      resultStatusFilter: nextResultStatusFilter,
      reviewStatusFilter: nextReviewStatusFilter,
      textFilter: nextTextFilter.trim(),
      dedupeLatest: nextDedupeLatest,
      displayThreshold: nextDisplayThreshold,
      requestedPage,
      targetResultId: targetResultId ?? null
    });
    if (resultsInFlightKeyRef.current === requestKey && resultsInFlightPromiseRef.current) {
      await resultsInFlightPromiseRef.current;
      return;
    }
    const task = loadResults(targetResultId, overrides, pageOverride);
    resultsInFlightKeyRef.current = requestKey;
    resultsInFlightPromiseRef.current = task;
    try {
      await task;
    } finally {
      if (resultsInFlightPromiseRef.current === task) {
        resultsInFlightKeyRef.current = null;
        resultsInFlightPromiseRef.current = null;
      }
    }
  }

  useEffect(() => {
    void loadTaskOptions();
  }, []);

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(REVIEW_THRESHOLD_STORAGE_KEY, displayThreshold.toFixed(2));
  }, [displayThreshold]);

  useEffect(() => {
    const nextQueryState = parseReviewQueryState(searchParams);
    const nextDisplayThreshold = parseThresholdText(nextQueryState.threshold);
    const nextTaskFilter = nextQueryState.taskId || nextQueryState.taskIds[0] || "";
    const nextFilters = {
      taskFilter: nextTaskFilter,
      resultStatusFilter: nextQueryState.status,
      reviewStatusFilter: nextQueryState.reviewStatus,
      textFilter: nextQueryState.q,
      dedupeLatest,
      displayThreshold: nextDisplayThreshold
    };
    const nextFilterKey = buildFilterKey(nextFilters);
    const filtersChanged = appliedFilterKeyRef.current !== nextFilterKey;
    appliedFilterKeyRef.current = nextFilterKey;

    setTaskFilter(nextTaskFilter);
    setResultStatusFilter(nextQueryState.status);
    setReviewStatusFilter(nextQueryState.reviewStatus);
    setTextFilter(nextQueryState.q);
    setDisplayThreshold(nextDisplayThreshold);

    if (filtersChanged || results.length === 0) {
      void requestResults(nextQueryState.resultId ?? undefined, nextFilters);
      return;
    }

    if (nextQueryState.resultId) {
      if (nextQueryState.resultId !== selectedResultId) {
        setError("");
        void requestDetail(nextQueryState.resultId);
      }
      return;
    }

    const fallbackId = results[0]?.result_id as number | undefined;
    if (!fallbackId) {
      setSelectedResultId(null);
      setDetail(null);
      setDetailLoading(false);
      setReviewStatus(DEFAULT_REVIEW_STATUS);
      setReviewNote("");
      return;
    }

    if (selectedResultId !== Number(fallbackId)) {
      setError("");
      void requestDetail(Number(fallbackId));
    }
  }, [searchParams, dedupeLatest]);

  useEffect(() => {
    if (skipInitialPageLoadRef.current) {
      skipInitialPageLoadRef.current = false;
      return;
    }
    void requestResults(undefined, undefined, resultPage);
  }, [resultPage]);

  useEffect(() => {
    setIsEvidenceModalOpen(false);
  }, [selectedResultId]);

  useLayoutEffect(() => {
    if (typeof window === "undefined") return;

    const detailElement = reviewDetailRef.current;
    if (!detailElement) return;

    let frameId = 0;

    const syncQueueHeight = () => {
      frameId = 0;

      if (window.innerWidth <= 900 || !hasSelection || detailLoading) {
        setSyncedQueueHeight(null);
        return;
      }

      const nextHeight = Math.ceil(detailElement.getBoundingClientRect().height);
      if (nextHeight <= 0) return;
      setSyncedQueueHeight((currentHeight) => (currentHeight === nextHeight ? currentHeight : nextHeight));
    };

    const requestSync = () => {
      if (frameId) {
        cancelAnimationFrame(frameId);
      }
      frameId = requestAnimationFrame(syncQueueHeight);
    };

    requestSync();

    const resizeObserver = new ResizeObserver(() => {
      requestSync();
    });
    resizeObserver.observe(detailElement);

    window.addEventListener("resize", requestSync);

    return () => {
      if (frameId) {
        cancelAnimationFrame(frameId);
      }
      resizeObserver.disconnect();
      window.removeEventListener("resize", requestSync);
    };
  }, [hasSelection, detailLoading, detail, results.length, resultPage]);

  const reviewQueueStyle = useMemo(() => {
    if (!syncedQueueHeight) return undefined;
    return {
      minHeight: `${syncedQueueHeight}px`,
      maxHeight: `${syncedQueueHeight}px`
    };
  }, [syncedQueueHeight]);

  async function handleSelectResult(resultId: number) {
    if (selectedResultId === resultId) return;
    const nextParams = buildSearchParams({ selectedResultId: resultId });
    setSearchParams(nextParams, { replace: false });
    setError("");
  }

  async function handleSaveReview(nextStatus = reviewStatus) {
    if (!selectedResultId) return;

    setIsSaving(true);
    setError("");
    try {
      const response = await saveReview(selectedResultId, {
        reviewStatus: nextStatus,
        reviewNote
      });
      const nextResult = response.result;
      setDetail(nextResult);
      setReviewStatus(nextResult.review?.review_status || nextStatus);
      setReviewNote(nextResult.review?.review_note || reviewNote);
      setResults((current) =>
        current.map((item) =>
          Number(item.result_id) === Number(nextResult.result_id)
            ? {
                ...item,
                ...nextResult,
                review: nextResult.review ?? item.review
              }
            : item
        )
      );
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "保存复核结果失败");
    } finally {
      setIsSaving(false);
    }
  }

  async function handleApplyFilters() {
    setResultPage(1);
    const nextParams = buildSearchParams({ selectedResultId: null });
    setSearchParams(nextParams, { replace: false });
  }

  async function handleClearFilters() {
    setTaskFilter("");
    setResultStatusFilter("");
    setReviewStatusFilter("");
    setTextFilter("");
    setDisplayThreshold(DEFAULT_CANDIDATE_DISPLAY_SCORE_THRESHOLD);
    setResultPage(1);
    const nextParams = buildReviewSearchParams({
      threshold: DEFAULT_CANDIDATE_DISPLAY_SCORE_THRESHOLD.toFixed(2)
    });
    setSearchParams(nextParams, { replace: false });
  }

  function handleToggleDedupe() {
    setResultPage(1);
    setDedupeLatest((current) => !current);
  }

  function handleTaskFilterChange(taskId: string) {
    const normalizedTaskId = String(taskId || "").trim();
    setResultPage(1);
    const nextParams = buildSearchParams({
      taskFilter: normalizedTaskId,
      selectedResultId: null
    });
    setSearchParams(nextParams, { replace: false });
  }

  async function handleClearPendingQueue() {
    const confirmed = window.confirm("确认清空当前账号下全部待复核结果吗？已确认、继续跟进、误报结果不会被清除。");
    if (!confirmed) return;

    setIsClearingPending(true);
    setError("");
    try {
      await clearPendingReviewResults();
      setSelectedResultId(null);
      setDetail(null);
      setReviewStatus(DEFAULT_REVIEW_STATUS);
      setReviewNote("");
      setResultPage(1);
      const nextParams = buildSearchParams({ selectedResultId: null });
      setSearchParams(nextParams, { replace: false });
      await requestResults(undefined, undefined, 1);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "清空待复核结果失败");
    } finally {
      setIsClearingPending(false);
    }
  }

  async function handleExportReviewedResults() {
    setIsExporting(true);
    setError("");
    try {
      await downloadReviewExport({
        taskId: taskFilter.trim() || undefined,
        status: resultStatusFilter || undefined,
        reviewStatus: reviewStatusFilter || undefined,
        sortBy: REVIEW_RESULT_SORT_BY,
        dedupeLatest,
        q: textFilter.trim() || undefined,
        candidateScoreThreshold: displayThreshold
      });
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "导出复核结果失败");
    } finally {
      setIsExporting(false);
    }
  }

  const fineResults = Array.isArray(detail?.result_payload?.fine?.results)
    ? (detail.result_payload.fine.results as Record<string, any>[])
    : [];
  const topFine = fineResults[0] ?? null;
  const bestMatch = topFine?.best_match as Record<string, any> | undefined;
  const metrics = bestMatch
    ? [
        ["longest_match_len", String(bestMatch.longest_match_len ?? "-")],
        ["longest_match_ratio", String(bestMatch.longest_match_ratio ?? "-")],
        ["ngram_recall", String(bestMatch.ngram_recall ?? "-")],
        ["ngram_precision", String(bestMatch.ngram_precision ?? "-")],
        ["jaccard", String(bestMatch.jaccard ?? "-")],
        ["sequence_ratio", String(bestMatch.sequence_ratio ?? "-")]
      ]
    : [];

  const availableStatuses = useMemo(() => {
    const dynamicValues = new Set<string>();

    for (const item of results) {
      const value = String(item.review?.review_status ?? "").trim();
      if (value) dynamicValues.add(value);
    }

    const currentValue = String(detail?.review?.review_status ?? reviewStatus ?? "").trim();
    if (currentValue) dynamicValues.add(currentValue);

    const options: Array<{ value: string; label: string }> = REVIEW_STATUS_OPTIONS.map((item) => ({
      value: item.value,
      label: item.label
    }));
    for (const value of dynamicValues) {
      if (!options.some((item) => item.value === value)) {
        options.push({ value, label: formatReviewStatus(value) });
      }
    }

    return options;
  }, [detail?.review?.review_status, results, reviewStatus]);

  const stats = useMemo(() => {
    return [
      ["待复核", String(resultStats.pending ?? 0), "doc"],
      ["确认高风险", String(resultStats.confirmed_high_risk ?? 0), "warning"],
      ["继续跟进", String(resultStats.needs_followup ?? 0), "refresh"],
      ["误报", String(resultStats.false_positive ?? 0), "shield"],
      ["当前队列", String(resultStats.total ?? resultTotal), "filter"]
    ] as const;
  }, [resultStats, resultTotal]);

  const selectedStatus = hasSelection ? String(detail?.review?.review_status ?? reviewStatus ?? "").trim() || DEFAULT_REVIEW_STATUS : "";
  const canClearFilters = Boolean(taskFilter || resultStatusFilter || reviewStatusFilter || textFilter || displayThreshold !== DEFAULT_CANDIDATE_DISPLAY_SCORE_THRESHOLD);
  const isDetailLoading = hasSelection && detailLoading && !detail;
  const selectedResultLabel = hasSelection ? `#${selectedResultId}` : "-";
  const detailUpdatedAt = detail?.review?.updated_at || detail?.updated_at || "-";
  const detailReviewer = detail?.review?.reviewer_name || "-";
  const shortDramaName = String(detail?.source_short_drama || "").trim() || "未标注短剧名";
  const sourceNovelName = String(detail?.source_novel_name || "").trim() || "-";
  const sourceExcelRow = String(detail?.source_excel_row || "").trim() || "-";
  const sourceEpisode = formatEpisodeLabel(detail?.source_episode);
  const sourceAuthor = String(detail?.source_author || "").trim() || "-";
  const sourcePlatform = String(detail?.source_platform || "").trim() || "-";
  const sourceDisplayTitle = String(detail?.source_display_title || "").trim() || "-";
  const sourceDescription = String(detail?.source_description || "").trim() || "-";
  const queryPreview = detail?.query_text || bestMatch?.query_text || bestMatch?.query_text_preview || "-";
  const candidatePreview =
    bestMatch?.candidate_review_context_text ||
    bestMatch?.candidate_text_full ||
    bestMatch?.candidate_text ||
    bestMatch?.candidate_text_preview ||
    "-";
  const matchedSubstring = String(bestMatch?.matched_substring || "").trim();
  const evidenceHighlight = useMemo(
    () => buildEvidenceHighlightRanges(queryPreview, candidatePreview, matchedSubstring),
    [candidatePreview, matchedSubstring, queryPreview]
  );
  const activeTask = tasks.find((task) => String(task.task_id) === taskFilter) ?? null;

  return (
    <div className="page-grid review-reference-page">
      <section className="single-page-heading">
        <div>
          <h1>结果复核</h1>
          <p>把短剧名、查询文本和命中小说放在一个视野里，减少跳转，直接完成侵权复核判断。</p>
        </div>
      </section>

      <section className="stats-grid review-stats-grid span-full">
        {stats.map(([label, value, icon]) => (
          <article key={label} className="stat-card compact review-stat-card">
            <div className="stat-icon-wrap">
              <Icon name={icon as "doc" | "warning" | "refresh" | "shield" | "filter"} />
            </div>
            <div className="stat-body">
              <span className="stat-label">{label}</span>
              <strong className="stat-value">{value}</strong>
            </div>
          </article>
        ))}
      </section>

      <section className="card-panel span-full review-filter-panel">
        <div className="review-task-toolbar">
          <div className="review-task-toolbar-head">
            <div>
              <span className="panel-label">任务范围</span>
              <h2>选择任务并绑定复核结果</h2>
            </div>
            <div className="review-task-toolbar-actions">
              <div className="filter-chip">当前绑定：{activeTask ? clipText(activeTask.source_file_name || activeTask.task_id, 24) : "全部任务"}</div>
            </div>
          </div>

          {tasksLoading ? (
            <StatusState title="正在加载任务列表" description="同步当前账号下的批量任务，用于绑定复核结果和批量删除。" tone="info" variant="inline" icon="tasks" />
          ) : tasks.length === 0 ? (
            <StatusState title="当前没有可选择的任务" description="先创建并运行批量任务，这里才会出现对应的任务范围。" variant="inline" icon="tasks" />
          ) : (
            <div className="review-task-selector review-task-selector--single">
              <label className="review-task-select-field">
                <span>复核任务</span>
                <select
                  className="compact-select review-task-select"
                  value={taskFilter}
                  onChange={(event) => handleTaskFilterChange(event.target.value)}
                >
                  <option value="">全部任务</option>
                  {tasks.map((task) => {
                    const taskId = String(task.task_id);
                    const taskName = String(task.source_file_name || taskId).trim() || taskId;
                    return (
                      <option key={taskId} value={taskId}>
                        {`${clipText(taskName, 28)} | ${formatTaskStatusLabel(String(task.status || ""))} | ${task.counts?.completed ?? 0}/${task.counts?.accepted ?? 0}`}
                      </option>
                    );
                  })}
                </select>
              </label>
            </div>
          )}
        </div>

        <div className="filter-bar review-filter-bar">
          <select className="compact-select" value={resultStatusFilter} onChange={(event) => setResultStatusFilter(event.target.value)}>
            <option value="">全部结果状态</option>
            <option value="completed">{formatTaskStatusLabel("completed")}</option>
            <option value="failed">{formatTaskStatusLabel("failed")}</option>
            <option value="queued">{formatTaskStatusLabel("queued")}</option>
          </select>
          <select className="compact-select" value={reviewStatusFilter} onChange={(event) => setReviewStatusFilter(event.target.value)}>
            <option value="">全部复核状态</option>
            {availableStatuses.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <input
            className="compact-select"
            type="text"
            value={textFilter}
            onChange={(event) => setTextFilter(event.target.value)}
            placeholder="按短剧名、书名、章节或文本"
          />
          <label className="batch-results-threshold-field review-threshold-field">
            <span>展示阈值</span>
            <input
              type="number"
              min={0}
              max={1}
              step={0.01}
              value={displayThreshold}
              onChange={(event) => setDisplayThreshold(clampThreshold(Number(event.target.value)))}
            />
          </label>
          <label className={`filter-chip review-toggle-chip${dedupeLatest ? " active" : ""}`}>
            <input type="checkbox" checked={dedupeLatest} onChange={handleToggleDedupe} />
            只看最新去重结果
          </label>
          <div className="filter-chip">当前结果：{resultTotal} 条</div>
          <div className="filter-chip">当前选中：{selectedResultLabel}</div>
          <button className="outline-button slim" type="button" onClick={() => void handleApplyFilters()}>
            <Icon name="refresh" />
            应用
          </button>
          <button
            className="outline-button slim"
            type="button"
            disabled={resultsLoading || isExporting}
            onClick={() => void handleExportReviewedResults()}
          >
            <Icon name="doc" />
            {isExporting ? "导出中..." : "导出已处理结果"}
          </button>
          <button className="ghost-button danger slim" type="button" disabled={resultsLoading || isClearingPending} onClick={() => void handleClearPendingQueue()}>
            {isClearingPending ? "清空中..." : "清空待复核"}
          </button>
          <button className="ghost-button slim" type="button" disabled={!canClearFilters} onClick={() => void handleClearFilters()}>
            清空
          </button>
        </div>
      </section>

      {error && (
        <StatusState
          className="span-full"
          title="复核页面操作失败"
          description={error}
          tone="error"
          variant="inline"
          icon="warning"
        />
      )}

      <div className="review-reference-layout span-full">
        <section className="card-panel review-reference-list" style={reviewQueueStyle}>
          <div className="section-heading compact-bottom">
            <div>
              <h2>结果队列</h2>
              <p>{taskFilter ? `当前只显示任务 ${clipText(taskFilter, 16)} 下的结果。` : "当前显示所有任务的复核结果，可结合上方任务范围切换。"}</p>
            </div>
          </div>

          <div className="review-reference-items">
            {resultsLoading ? (
              <StatusState title="正在加载复核结果" description="同步当前筛选条件下的结果队列。" tone="info" icon="queue" />
            ) : results.length === 0 ? (
              <StatusState title="当前没有可复核结果" description="先完成一次比对任务，或调整任务范围与展示阈值。" icon="review" />
            ) : (
              results.map((item) => {
                const itemStatus = String(item.review?.review_status ?? "").trim() || DEFAULT_REVIEW_STATUS;
                const itemDrama = String(item.source_short_drama || "").trim() || "未标注短剧名";

                return (
                  <article
                    key={String(item.result_id)}
                    className={`review-reference-item${selectedResultId === item.result_id ? " active" : ""}`}
                    onClick={() => void handleSelectResult(Number(item.result_id))}
                  >
                    <div className="review-reference-marker">
                      {selectedResultId === item.result_id ? <Icon name="check" /> : <span />}
                    </div>
                    <div className="review-reference-main">
                      <div className="review-reference-title-row">
                        <strong className="review-reference-drama">{itemDrama}</strong>
                        <span className="soft-tag">{formatReviewStatus(itemStatus)}</span>
                      </div>
                      <div className="review-reference-preview-meta">
                        <span>{formatEpisodeLabel(item.source_episode)}</span>
                        <span>{item.source_author || "-"}</span>
                      </div>
                      <div className="review-reference-preview">{item.query_text_preview || "-"}</div>
                      <div className="review-reference-grid">
                        <div>
                          <span>命中书名</span>
                          <strong>{item.top1_book_name || "-"}</strong>
                        </div>
                        <div>
                          <span>命中章节</span>
                          <strong>{item.top1_chapter_name || "-"}</strong>
                        </div>
                        <div>
                          <span>集数</span>
                          <strong>{formatEpisodeLabel(item.source_episode)}</strong>
                        </div>
                        <div>
                          <span>作者</span>
                          <strong>{item.source_author || "-"}</strong>
                        </div>
                        <div>
                          <span>源小说名</span>
                          <strong>{item.source_novel_name || "-"}</strong>
                        </div>
                        <div>
                          <span>{formatMetricLabel("fine_score")}</span>
                          <strong>{formatScore(item.top1_fine_score)}</strong>
                        </div>
                        <div>
                          <span>{formatMetricLabel("semantic_status")}</span>
                          <strong>{formatSemanticStatusLabel(String(item.semantic_status || ""))}</strong>
                        </div>
                        <div>
                          <span>耗时</span>
                          <strong>{formatDuration(item.duration_seconds)}</strong>
                        </div>
                        <div>
                          <span>Excel 行号</span>
                          <strong>{item.source_excel_row || "-"}</strong>
                        </div>
                        <div>
                          <span>任务 ID</span>
                          <strong>{clipText(item.task_id, 16)}</strong>
                        </div>
                      </div>
                    </div>
                  </article>
                );
              })
            )}
          </div>

          {!resultsLoading && (
            <PaginationBar
              className="review-reference-pagination"
              page={resultPage}
              pageCount={Math.max(Math.ceil(resultTotal / REVIEW_PAGE_SIZE), 1)}
              total={resultTotal}
              pageSize={REVIEW_PAGE_SIZE}
              itemLabel="结果"
              onChange={setResultPage}
            />
          )}
        </section>

        <section className="review-reference-detail" ref={reviewDetailRef}>
          {!hasSelection ? (
            <article className="dark-card-panel review-reference-empty">
              <div className="review-reference-empty-copy">
                <span className="eyebrow">复核详情</span>
                <h2>请选择一条结果</h2>
                <p>从左侧结果队列中选中一条后，这里会展示短剧样本、命中小说和文本证据。</p>
              </div>
            </article>
          ) : isDetailLoading ? (
            <article className="card-panel review-reference-loading">
              <StatusState title="正在加载复核详情" description="同步当前结果的文本证据、指标和复核状态。" tone="info" variant="inline" icon="review" />
            </article>
          ) : (
            <>
              <article className="dark-card-panel review-reference-summary">
                <div className="review-reference-summary-header">
                  <div>
                    <span className="panel-label">短剧样本</span>
                    <h2>{shortDramaName}</h2>
                    <p>{sourceEpisode} · 作者：{sourceAuthor} · 平台：{sourcePlatform}</p>
                  </div>
                  <div className="review-reference-summary-badges">
                    <span className="soft-tag">{formatReviewLabel(String(detail?.top1_review_label || ""))}</span>
                    <span className="soft-tag muted">{formatConfidenceLabel(String(detail?.top1_confidence_label || ""))}</span>
                  </div>
                </div>
                <div className="review-reference-summary-grid">
                  <div>
                    <span>集数</span>
                    <strong>{sourceEpisode}</strong>
                  </div>
                  <div>
                    <span>作者</span>
                    <strong>{sourceAuthor}</strong>
                  </div>
                  <div>
                    <span>命中书名</span>
                    <strong>{detail?.top1_book_name || "-"}</strong>
                  </div>
                  <div>
                    <span>命中章节</span>
                    <strong>{detail?.top1_chapter_name || "-"}</strong>
                  </div>
                  <div>
                    <span>{formatMetricLabel("fine_score")}</span>
                    <strong>{formatScore(detail?.top1_fine_score)}</strong>
                  </div>
                  <div>
                    <span>耗时</span>
                    <strong>{formatDuration(detail?.duration_seconds)}</strong>
                  </div>
                  <div>
                    <span>{formatMetricLabel("semantic_status")}</span>
                    <strong>{formatSemanticStatusLabel(String(detail?.semantic_status || ""))}</strong>
                  </div>
                  <div>
                    <span>复核状态</span>
                    <strong>{formatReviewStatus(selectedStatus)}</strong>
                  </div>
                  <div>
                    <span>任务状态</span>
                    <strong>{formatTaskStatusLabel(String(detail?.task_status || ""))}</strong>
                  </div>
                  <div>
                    <span>任务 ID</span>
                    <strong>{detail?.task_id || "-"}</strong>
                  </div>
                </div>
              </article>

              <div className="review-reference-main-grid">
                <article className="card-panel review-reference-evidence">
                  <div className="review-reference-section-head">
                    <div>
                      <span className="panel-label">核心对比</span>
                      <h3>先看文本，再判断是否侵权</h3>
                    </div>
                    <div className="review-reference-head-actions">
                      <div className="review-reference-head-chips">
                        <span className="soft-tag">{formatSemanticStatusLabel(String(detail?.semantic_status || ""))}</span>
                        <span className="soft-tag muted">结果 {selectedResultLabel}</span>
                      </div>
                      {bestMatch ? (
                        <button className="ghost-button slim single-evidence-expand-button" type="button" onClick={() => setIsEvidenceModalOpen(true)}>
                          <Icon name="expand" />
                          放大查看
                        </button>
                      ) : null}
                    </div>
                  </div>

                  {bestMatch ? (
                    <>
                      <div className="single-evidence-grid review-reference-evidence-grid">
                        <article className="evidence-card review-evidence-card review-evidence-card-query">
                          <span className="single-evidence-label">查询文本</span>
                          <p>{renderHighlightedEvidence(queryPreview, evidenceHighlight.queryRanges, "review-query")}</p>
                        </article>
                        <article className="evidence-card highlighted review-evidence-card review-evidence-card-candidate">
                          <span className="single-evidence-label">候选文本</span>
                          <p>{renderHighlightedEvidence(candidatePreview, evidenceHighlight.candidateRanges, "review-candidate")}</p>
                        </article>
                      </div>

                      <div className="review-reference-inline-support">
                        <div className="review-reference-support-block surface">
                          <div className="review-reference-section-head compact">
                            <div>
                              <span className="panel-label">辅助信息</span>
                              <h3>保留必要背景</h3>
                            </div>
                          </div>
                          <div className="review-reference-meta-grid">
                            <div>
                              <span>展示标题</span>
                              <strong>{sourceDisplayTitle}</strong>
                            </div>
                            <div>
                              <span>平台</span>
                              <strong>{sourcePlatform}</strong>
                            </div>
                            <div>
                              <span>短剧名</span>
                              <strong>{shortDramaName}</strong>
                            </div>
                            <div>
                              <span>源小说名</span>
                              <strong>{sourceNovelName}</strong>
                            </div>
                            <div>
                              <span>来源标识</span>
                              <strong>{detail?.source_ref || "-"}</strong>
                            </div>
                            <div>
                              <span>Excel 行号</span>
                              <strong>{sourceExcelRow}</strong>
                            </div>
                            <div>
                              <span>描述</span>
                              <strong>{sourceDescription}</strong>
                            </div>
                          </div>
                        </div>

                        <div className="review-reference-support-block surface">
                          <div className="review-reference-section-head compact">
                            <div>
                              <span className="panel-label">辅助指标</span>
                              <h3>只展示关键指标</h3>
                            </div>
                          </div>
                          <div className="review-metrics-grid review-metrics-grid-compact">
                            {metrics.length === 0 ? (
                              <StatusState title="当前没有指标详情" description="该结果暂时没有可展示的命中指标。" icon="filter" />
                            ) : (
                              metrics.map(([label, value]) => (
                                <div key={label} className="review-metric-box">
                                  <span>{formatMetricLabel(label)}</span>
                                  <strong>{formatScore(value)}</strong>
                                  <div className="single-inline-score"><span style={{ width: `${metricPercent(value)}%` }} /></div>
                                </div>
                              ))
                            )}
                          </div>
                        </div>
                      </div>
                    </>
                  ) : (
                    <StatusState title="当前没有证据内容" description="该结果暂时没有可展示的最佳命中文本。" icon="search" />
                  )}
                </article>

                <aside className="review-reference-sidebar">
                  <article className="card-panel review-reference-actions review-reference-control-panel">
                    <div className="review-reference-section-head compact">
                      <div>
                        <span className="panel-label">复核操作</span>
                        <h3>在右侧直接下结论</h3>
                      </div>
                    </div>

                    <div className="review-actions">
                      <button className="primary-button" type="button" disabled={!selectedResultId || isSaving} onClick={() => void handleSaveReview("confirmed_high_risk")}>
                        确认高风险
                      </button>
                      <button className="ghost-button warm" type="button" disabled={!selectedResultId || isSaving} onClick={() => void handleSaveReview("needs_followup")}>
                        继续跟进
                      </button>
                      <button className="ghost-button" type="button" disabled={!selectedResultId || isSaving} onClick={() => void handleSaveReview("false_positive")}>
                        标记误报
                      </button>
                    </div>

                    <div className="review-reference-notes">
                      <div className="review-reference-note-box">
                        <span className="panel-label">复核状态</span>
                        <select className="compact-select" value={reviewStatus} onChange={(event) => setReviewStatus(event.target.value)}>
                          {availableStatuses.map((option) => (
                            <option key={option.value} value={option.value}>
                              {option.label}
                            </option>
                          ))}
                        </select>
                        <span className="panel-label">复核备注</span>
                        <textarea
                          className="review-notes compact"
                          value={reviewNote}
                          onChange={(event) => setReviewNote(event.target.value)}
                          placeholder="记录判断原因、关键证据或后续跟进行动。"
                        />
                      </div>

                      <button className="primary-button note-save" type="button" disabled={!selectedResultId || isSaving} onClick={() => void handleSaveReview()}>
                        {isSaving ? "保存中..." : "保存复核"}
                      </button>
                    </div>

                    <div className="review-reference-footer">
                      <span>复核人：{detailReviewer}</span>
                      <span>更新时间：{detailUpdatedAt}</span>
                    </div>

                    {detailLoading && (
                      <StatusState title="正在刷新复核详情" description="保存后会自动同步当前结果的最新状态。" tone="info" variant="inline" icon="refresh" />
                    )}
                  </article>
                </aside>
              </div>
            </>
          )}
        </section>
      </div>

      {bestMatch && isEvidenceModalOpen ? (
        <div className="single-evidence-modal-overlay" role="dialog" aria-modal="true" aria-label="证据详情放大查看" onClick={() => setIsEvidenceModalOpen(false)}>
          <div className="single-evidence-modal" onClick={(event) => event.stopPropagation()}>
            <div className="single-evidence-modal-header">
              <div className="single-evidence-modal-title">
                <div>
                  <span className="single-evidence-modal-eyebrow">证据详情</span>
                  <h2>{shortDramaName}</h2>
                  <p>{detail?.top1_book_name || "当前命中结果"} · {detail?.top1_chapter_name || "查看完整文本对照"}</p>
                </div>
              </div>
              <button className="icon-button single-evidence-modal-close" type="button" aria-label="关闭证据详情弹窗" onClick={() => setIsEvidenceModalOpen(false)}>
                <Icon name="close" />
              </button>
            </div>

            <div className="single-evidence-modal-body">
              <div className="single-evidence-modal-grid">
                <article className="evidence-card single-evidence-modal-card">
                  <span className="single-evidence-label">查询文本</span>
                  <p>{renderHighlightedEvidence(queryPreview, evidenceHighlight.queryRanges, "review-modal-query")}</p>
                </article>
                <article className="evidence-card highlighted single-evidence-modal-card">
                  <span className="single-evidence-label">候选文本</span>
                  <p>{renderHighlightedEvidence(candidatePreview, evidenceHighlight.candidateRanges, "review-modal-candidate")}</p>
                </article>
              </div>

              <article className="metrics-card single-evidence-modal-metrics">
                <div className="single-evidence-modal-metrics-header">
                  <span className="single-evidence-label">命中指标</span>
                  <strong>{formatScore(detail?.top1_fine_score)}</strong>
                </div>
                <div className="single-metrics-list">
                  {metrics.map(([label, value]) => (
                    <div key={label} className="single-metric-row">
                      <div>
                        <strong>{formatMetricLabel(label)}</strong>
                        <span>{formatScore(value)}</span>
                      </div>
                      <div className="single-inline-score"><span style={{ width: `${metricPercent(value)}%` }} /></div>
                    </div>
                  ))}
                </div>
              </article>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
