import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { getResultDetail, listResults, saveReview } from "../api";
import { formatConfidenceLabel, formatMetricLabel, formatReviewLabel, formatSemanticStatusLabel, formatTaskStatusLabel } from "../displayText";
import { Icon } from "../icons";
import { PaginationBar } from "../PaginationBar";
import { StatusState } from "../StatusState";
import { buildReviewSearchParams, parseReviewQueryState } from "../workflowLinks";

const DEFAULT_REVIEW_STATUS = "pending";
const REVIEW_PAGE_SIZE = 8;
const REVIEW_STATUS_OPTIONS = [
  { value: "pending", label: "待复核" },
  { value: "confirmed_high_risk", label: "确认高风险" },
  { value: "needs_followup", label: "需要继续跟进" },
  { value: "false_positive", label: "误报" }
] as const;

function buildFilterKey(filters: {
  taskFilter?: string;
  resultStatusFilter?: string;
  reviewStatusFilter?: string;
  textFilter?: string;
}) {
  return [
    filters.taskFilter ?? "",
    filters.resultStatusFilter ?? "",
    filters.reviewStatusFilter ?? "",
    filters.textFilter ?? ""
  ].join("\u0001");
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

export function ReviewPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [results, setResults] = useState<Record<string, any>[]>([]);
  const [selectedResultId, setSelectedResultId] = useState<number | null>(null);
  const [detail, setDetail] = useState<Record<string, any> | null>(null);
  const [isEvidenceModalOpen, setIsEvidenceModalOpen] = useState(false);
  const [reviewNote, setReviewNote] = useState("");
  const [reviewStatus, setReviewStatus] = useState(DEFAULT_REVIEW_STATUS);
  const [taskFilter, setTaskFilter] = useState("");
  const [resultStatusFilter, setResultStatusFilter] = useState("");
  const [reviewStatusFilter, setReviewStatusFilter] = useState("");
  const [textFilter, setTextFilter] = useState("");
  const [resultPage, setResultPage] = useState(1);
  const [resultsLoading, setResultsLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState("");
  const hasSelection = selectedResultId !== null;
  const detailRequestRef = useRef(0);
  const appliedFilterKeyRef = useRef<string | null>(null);

  function buildSearchParams(
    overrides?: {
      taskFilter?: string;
      resultStatusFilter?: string;
      reviewStatusFilter?: string;
      textFilter?: string;
      selectedResultId?: number | null;
    }
  ) {
    const next = getAppliedFilters(overrides);
    const nextResultId = overrides?.selectedResultId ?? selectedResultId;
    return buildReviewSearchParams({
      taskId: next.taskFilter,
      status: next.resultStatusFilter,
      reviewStatus: next.reviewStatusFilter,
      q: next.textFilter,
      resultId: nextResultId
    });
  }

  function getAppliedFilters(
    overrides?: {
      taskFilter?: string;
      resultStatusFilter?: string;
      reviewStatusFilter?: string;
      textFilter?: string;
    }
  ) {
    return {
      taskFilter: overrides?.taskFilter ?? taskFilter,
      resultStatusFilter: overrides?.resultStatusFilter ?? resultStatusFilter,
      reviewStatusFilter: overrides?.reviewStatusFilter ?? reviewStatusFilter,
      textFilter: overrides?.textFilter ?? textFilter
    };
  }

  async function loadResults(
    targetResultId?: number,
    overrides?: {
      taskFilter?: string;
      resultStatusFilter?: string;
      reviewStatusFilter?: string;
      textFilter?: string;
    }
  ) {
    setResultsLoading(true);
    setError("");
    try {
      const { taskFilter: nextTaskFilter, resultStatusFilter: nextResultStatusFilter, reviewStatusFilter: nextReviewStatusFilter, textFilter: nextTextFilter } = getAppliedFilters(overrides);
      const response = await listResults({
        limit: 100,
        offset: 0,
        taskId: nextTaskFilter.trim() || undefined,
        status: nextResultStatusFilter || undefined,
        reviewStatus: nextReviewStatusFilter || undefined,
        sortBy: "updated_at_desc"
      });
      const filteredItems = response.items.filter((item) => {
        const keyword = nextTextFilter.trim().toLowerCase();
        if (!keyword) return true;

        return [
          item.query_text_preview,
          item.top1_book_name,
          item.top1_chapter_name,
          item.task_id,
          item.top1_review_label
        ]
          .map((value) => String(value ?? "").toLowerCase())
          .some((value) => value.includes(keyword));
      });
      const retainedSelectedId =
        selectedResultId && filteredItems.some((item) => Number(item.result_id) === selectedResultId)
          ? selectedResultId
          : undefined;
      const nextId = targetResultId ?? retainedSelectedId ?? (filteredItems[0]?.result_id as number | undefined);
      const nextPageCount = Math.max(Math.ceil(filteredItems.length / REVIEW_PAGE_SIZE), 1);
      const nextSelectedIndex =
        nextId === undefined ? -1 : filteredItems.findIndex((item) => Number(item.result_id) === Number(nextId));

      setResults(filteredItems);
      setResultPage((current) => {
        if (nextSelectedIndex >= 0) {
          return Math.floor(nextSelectedIndex / REVIEW_PAGE_SIZE) + 1;
        }
        return Math.min(current, nextPageCount);
      });
      if (!nextId) {
        setSelectedResultId(null);
        setDetail(null);
        setDetailLoading(false);
        setReviewStatus(DEFAULT_REVIEW_STATUS);
        setReviewNote("");
        return;
      }

      await loadDetail(nextId);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "加载复核结果失败");
    } finally {
      setResultsLoading(false);
    }
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

  useEffect(() => {
    const nextQueryState = parseReviewQueryState(searchParams);
    const nextTaskFilter = nextQueryState.taskId;
    const nextResultStatusFilter = nextQueryState.status;
    const nextReviewStatusFilter = nextQueryState.reviewStatus;
    const nextTextFilter = nextQueryState.q;
    const nextResultId = nextQueryState.resultId;
    const nextFilters = {
      taskFilter: nextTaskFilter,
      resultStatusFilter: nextResultStatusFilter,
      reviewStatusFilter: nextReviewStatusFilter,
      textFilter: nextTextFilter
    };
    const nextFilterKey = buildFilterKey(nextFilters);
    const filtersChanged = appliedFilterKeyRef.current !== nextFilterKey;
    appliedFilterKeyRef.current = nextFilterKey;

    setTaskFilter(nextTaskFilter);
    setResultStatusFilter(nextResultStatusFilter);
    setReviewStatusFilter(nextReviewStatusFilter);
    setTextFilter(nextTextFilter);

    if (filtersChanged || results.length === 0) {
      void loadResults(nextResultId ?? undefined, nextFilters);
      return;
    }

    if (nextResultId) {
      if (nextResultId !== selectedResultId) {
        setError("");
        void loadDetail(nextResultId);
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
      void loadDetail(Number(fallbackId));
    }
  }, [searchParams]);

  useEffect(() => {
    setIsEvidenceModalOpen(false);
  }, [selectedResultId]);

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
        reviewerName: "web-ui",
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

  const fineResults = Array.isArray(detail?.result_payload?.fine?.results) ? (detail.result_payload.fine.results as Record<string, any>[]) : [];
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

    const options: Array<{ value: string; label: string }> = REVIEW_STATUS_OPTIONS.map((item) => ({ value: item.value, label: item.label }));
    for (const value of dynamicValues) {
      if (!options.some((item) => item.value === value)) {
        options.push({ value, label: formatReviewStatus(value) });
      }
    }

    return options;
  }, [detail?.review?.review_status, results, reviewStatus]);

  const stats = useMemo(() => {
    const countBy = (status: string) =>
      results.filter((item) => {
        const current = String(item.review?.review_status ?? "").trim() || DEFAULT_REVIEW_STATUS;
        return current === status;
      }).length;

    return [
      ["待复核", String(countBy("pending")), "doc"],
      ["已确认", String(countBy("confirmed_high_risk")), "warning"],
      ["待跟进", String(countBy("needs_followup")), "refresh"],
      ["误报", String(countBy("false_positive")), "shield"],
      ["结果总数", String(results.length), "filter"]
    ] as const;
  }, [results]);

  const selectedStatus = hasSelection ? String(detail?.review?.review_status ?? reviewStatus ?? "").trim() || DEFAULT_REVIEW_STATUS : "";
  const canClearFilters = Boolean(taskFilter || resultStatusFilter || reviewStatusFilter || textFilter);
  const isDetailLoading = hasSelection && detailLoading && !detail;
  const selectedResultLabel = hasSelection ? `#${selectedResultId}` : "-";
  const detailUpdatedAt = detail?.review?.updated_at || detail?.updated_at || "-";
  const detailReviewer = detail?.review?.reviewer_name || "-";
  const resultPageCount = Math.max(Math.ceil(results.length / REVIEW_PAGE_SIZE), 1);
  const currentResultPage = Math.min(resultPage, resultPageCount);
  const visibleResults = useMemo(
    () => results.slice((currentResultPage - 1) * REVIEW_PAGE_SIZE, currentResultPage * REVIEW_PAGE_SIZE),
    [currentResultPage, results]
  );

  async function handleApplyFilters() {
    const nextParams = buildSearchParams();
    setSearchParams(nextParams, { replace: false });
  }

  async function handleClearFilters() {
    setTaskFilter("");
    setResultStatusFilter("");
    setReviewStatusFilter("");
    setTextFilter("");
    const nextParams = buildReviewSearchParams({ resultId: selectedResultId });
    setSearchParams(nextParams, { replace: false });
  }

  return (
    <div className="page-grid review-reference-page">
      <section className="single-page-heading">
        <div>
          <h1>结果复核</h1>
          <p>查看排序结果、核验证据，并保存复核结论供后续导出和处理。</p>
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
        <div className="filter-bar review-filter-bar">
          <input
            className="compact-select"
            type="text"
            value={taskFilter}
            onChange={(event) => setTaskFilter(event.target.value)}
            placeholder="按任务 ID 筛选"
          />
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
            placeholder="按预览、书名、章节或标签筛选"
          />
          <div className="filter-chip">已筛结果：{results.length} 条</div>
          <div className="filter-chip">当前选中：{selectedResultLabel}</div>
          <div className="filter-chip">语义状态：{formatSemanticStatusLabel(String(detail?.semantic_status || ""))}</div>
          <div className="filter-chip">任务：{detail?.task_id || "-"}</div>
          <div className="filter-chip">复核状态：{hasSelection ? formatReviewStatus(selectedStatus) : "-"}</div>
          <button
            className="outline-button slim"
            type="button"
            onClick={() => void handleApplyFilters()}
          >
            <Icon name="refresh" />
            应用
          </button>
          <button
            className="ghost-button slim"
            type="button"
            disabled={!canClearFilters}
            onClick={() => void handleClearFilters()}
          >
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
        <section className="card-panel review-reference-list">
          <div className="section-heading compact-bottom">
            <div>
              <h2>结果队列</h2>
              <p>当前共有 {results.length} 条可复核的比对结果。</p>
            </div>
          </div>

          <div className="review-reference-items">
            {resultsLoading ? (
              <StatusState title="正在加载复核结果" description="正在同步符合筛选条件的结果列表。" tone="info" icon="queue" />
            ) : results.length === 0 ? (
              <StatusState title="当前还没有可复核结果" description="请先完成一次比对任务，再到这里查看命中结果。" icon="review" />
            ) : (
              visibleResults.map((item) => {
                const itemStatus = String(item.review?.review_status ?? "").trim() || DEFAULT_REVIEW_STATUS;

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
                      <div className="review-reference-preview">{item.query_text_preview || "-"}</div>
                      <div className="review-reference-grid">
                        <div>
                          <span>Top1 书名</span>
                          <strong>{item.top1_book_name || "-"}</strong>
                        </div>
                        <div>
                          <span>Top1 章节</span>
                          <strong>{item.top1_chapter_name || "-"}</strong>
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
                          <span>复核状态</span>
                          <strong>{formatReviewStatus(itemStatus)}</strong>
                        </div>
                        <div>
                          <span>任务 ID</span>
                          <strong>{item.task_id || "-"}</strong>
                        </div>
                        <div>
                          <span>{formatMetricLabel("review_label")}</span>
                          <strong>{formatReviewLabel(String(item.top1_review_label || ""))}</strong>
                        </div>
                        <div>
                          <span>更新时间</span>
                          <strong>{item.updated_at || "-"}</strong>
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
              page={currentResultPage}
              pageCount={resultPageCount}
              total={results.length}
              pageSize={REVIEW_PAGE_SIZE}
              itemLabel="结果"
              onChange={setResultPage}
            />
          )}
        </section>

        <section className="review-reference-detail">
          {!hasSelection ? (
            <article className="dark-card-panel review-reference-empty">
              <div className="review-reference-empty-copy">
                <span className="eyebrow">复核详情</span>
                <h2>请选择一条结果</h2>
                <p>从左侧结果队列中选择一条记录后，即可查看命中文本、指标证据和当前复核结论。</p>
              </div>
            </article>
          ) : isDetailLoading ? (
            <article className="card-panel review-reference-loading">
              <StatusState title="正在加载复核详情" description="正在同步当前结果的证据、指标和复核状态。" tone="info" variant="inline" icon="review" />
            </article>
          ) : (
            <>
              <article className="dark-card-panel review-reference-summary">
                <div className="review-reference-summary-grid">
                  <div>
                    <span>Top1 书名</span>
                    <strong>{detail?.top1_book_name || "-"}</strong>
                  </div>
                  <div>
                    <span>Top1 章节</span>
                    <strong>{detail?.top1_chapter_name || "-"}</strong>
                  </div>
                  <div>
                    <span>{formatMetricLabel("fine_score")}</span>
                    <strong>{formatScore(detail?.top1_fine_score)}</strong>
                  </div>
                  <div>
                    <span>{formatMetricLabel("review_label")}</span>
                    <strong className="emphasis warm">{formatReviewLabel(String(detail?.top1_review_label || ""))}</strong>
                  </div>
                  <div>
                    <span>{formatMetricLabel("confidence_label")}</span>
                    <strong className="emphasis warm">{formatConfidenceLabel(String(detail?.top1_confidence_label || ""))}</strong>
                  </div>
                  <div>
                    <span>{formatMetricLabel("semantic_status")}</span>
                    <strong>{formatSemanticStatusLabel(String(detail?.semantic_status || ""))}</strong>
                  </div>
                  <div>
                    <span>复核状态</span>
                    <strong>{formatReviewStatus(selectedStatus)}</strong>
                  </div>
                </div>
              </article>

              <div className="review-reference-main-grid">
                <article className="card-panel review-reference-evidence">
                  <div className="review-reference-section-head">
                    <div>
                      <span className="panel-label">核心对比</span>
                      <h3>先看文本，再做判断</h3>
                    </div>
                    <div className="review-reference-head-actions">
                      <div className="review-reference-head-chips">
                        <span className="soft-tag">{formatReviewLabel(String(detail?.top1_review_label || ""))}</span>
                        <span className="soft-tag muted">{formatConfidenceLabel(String(detail?.top1_confidence_label || ""))}</span>
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
                    <div className="single-evidence-grid review-reference-evidence-grid">
                      <article className="evidence-card review-evidence-card review-evidence-card-query">
                        <span className="single-evidence-label">查询文本</span>
                        <p>{bestMatch.query_text || detail?.query_text || bestMatch.query_text_preview || "-"}</p>
                      </article>
                      <article className="evidence-card highlighted review-evidence-card review-evidence-card-candidate">
                        <span className="single-evidence-label">候选文本</span>
                        <p>{bestMatch.candidate_text || bestMatch.candidate_text_preview || "-"}</p>
                      </article>
                    </div>
                  ) : (
                    <StatusState title="当前没有证据内容" description="该结果暂时没有可展示的最佳命中文本预览。" icon="search" />
                  )}

                  {detail ? (
                    <div className="review-reference-inline-support">
                      <div className="review-reference-support-block surface">
                        <div className="review-reference-section-head compact">
                          <div>
                            <span className="panel-label">辅助信息</span>
                            <h3>只保留必要背景</h3>
                          </div>
                        </div>
                        <div className="review-reference-meta-grid">
                          <div>
                            <span>任务 ID</span>
                            <strong>{detail?.task_id || "-"}</strong>
                          </div>
                          <div>
                            <span>结果编号</span>
                            <strong>{selectedResultLabel}</strong>
                          </div>
                          <div>
                            <span>{formatMetricLabel("semantic_status")}</span>
                            <strong>{formatSemanticStatusLabel(String(detail?.semantic_status || ""))}</strong>
                          </div>
                          <div>
                            <span>复核状态</span>
                            <strong>{formatReviewStatus(selectedStatus)}</strong>
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
                  ) : null}
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
                        需要继续跟进
                      </button>
                      <button className="ghost-button" type="button" disabled={!selectedResultId || isSaving} onClick={() => void handleSaveReview("false_positive")}>
                        标记为误报
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
                    <div className="review-reference-support">
                      <div className="review-reference-support-block">
                      <div className="review-reference-section-head compact">
                        <div>
                          <span className="panel-label">辅助信息</span>
                          <h3>只保留必要背景</h3>
                        </div>
                      </div>
                      <div className="review-reference-meta-grid">
                        <div>
                          <span>任务 ID</span>
                          <strong>{detail?.task_id || "-"}</strong>
                        </div>
                        <div>
                          <span>结果编号</span>
                          <strong>{selectedResultLabel}</strong>
                        </div>
                        <div>
                          <span>{formatMetricLabel("semantic_status")}</span>
                          <strong>{formatSemanticStatusLabel(String(detail?.semantic_status || ""))}</strong>
                        </div>
                        <div>
                          <span>复核状态</span>
                          <strong>{formatReviewStatus(selectedStatus)}</strong>
                        </div>
                      </div>
                    </div>

                    <div className="review-reference-support-block">
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
                  <h2>{detail?.top1_book_name || "当前命中结果"}</h2>
                  <p>{detail?.top1_chapter_name || "查看查询文本与候选文本的完整对照"}</p>
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
                  <p>{bestMatch.query_text || detail?.query_text || bestMatch.query_text_preview || "-"}</p>
                </article>
                <article className="evidence-card highlighted single-evidence-modal-card">
                  <span className="single-evidence-label">候选文本</span>
                  <p>{bestMatch.candidate_text || bestMatch.candidate_text_preview || "-"}</p>
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
