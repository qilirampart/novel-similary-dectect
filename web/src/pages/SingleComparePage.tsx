import { useMemo, useState } from "react";

import { compareSingle, type DetectionMode } from "../api";
import { formatConfidenceLabel, formatMetricLabel, formatReviewLabel, formatSemanticStatusLabel } from "../displayText";
import { Icon } from "../icons";
import { StatusState } from "../StatusState";

const sampleText = `女主在婚礼前夜突然失踪，男主顺着她留下的线索一路追查，发现整场联姻只是更大阴谋的入口。
三年前的一场旧案、一个被替换的身份、一次精心设计的重逢，把所有人重新拖回真相中央。
当表面的甜宠关系开始露出裂缝，真正危险的不是爱情本身，而是谁在操控这段爱情叙事。`;

const CANDIDATE_PAGE_SIZE = 5;
const DEFAULT_CANDIDATE_DISPLAY_SCORE_THRESHOLD = 0.1;

type MetricRow = {
  label: string;
  value: string;
};

function formatDurationSeconds(value: unknown): string {
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric < 0) return "-";
  if (numeric < 1) return `${numeric.toFixed(2)} 秒`;
  if (numeric < 10) return `${numeric.toFixed(1)} 秒`;
  return `${numeric.toFixed(0)} 秒`;
}

function metricValueToPercent(value: string): number {
  const numeric = Number.parseFloat(value);
  if (Number.isNaN(numeric)) return 0;
  if (numeric <= 1) return Math.round(numeric * 100);
  return Math.min(Math.round(numeric), 100);
}

function formatScore(value: unknown): string {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toFixed(2) : "-";
}

function isDisplayableCandidate(item: Record<string, any>, threshold: number): boolean {
  const numeric = Number(item?.fine_score);
  return Number.isFinite(numeric) && numeric >= threshold;
}

function clampThreshold(value: number): number {
  if (!Number.isFinite(value)) return DEFAULT_CANDIDATE_DISPLAY_SCORE_THRESHOLD;
  return Math.max(0, Math.min(1, value));
}

export function SingleComparePage() {
  const [rewriteEnabled, setRewriteEnabled] = useState(false);
  const [text, setText] = useState(sampleText);
  const [topK, setTopK] = useState(10);
  const [compareTopK, setCompareTopK] = useState(50);
  const [mergedTopK, setMergedTopK] = useState(20);
  const [candidateDisplayScoreThreshold, setCandidateDisplayScoreThreshold] = useState(DEFAULT_CANDIDATE_DISPLAY_SCORE_THRESHOLD);
  const [isRunning, setIsRunning] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<Record<string, any> | null>(null);
  const [durationSeconds, setDurationSeconds] = useState<number | null>(null);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [candidatePage, setCandidatePage] = useState(1);
  const [isEvidenceModalOpen, setIsEvidenceModalOpen] = useState(false);
  const mode: DetectionMode = rewriteEnabled ? "rewrite" : "reuse";

  const semanticStatus = result?.rewrite_detection?.status ?? "waiting";
  const effectiveCandidateDisplayScoreThreshold = clampThreshold(
    Number(result?.params?.candidate_display_score_threshold ?? candidateDisplayScoreThreshold)
  );
  const fineResults = Array.isArray(result?.fine?.results) ? (result.fine.results as Record<string, any>[]) : [];
  const displayCandidates = useMemo(
    () => fineResults.filter((item) => isDisplayableCandidate(item, effectiveCandidateDisplayScoreThreshold)),
    [effectiveCandidateDisplayScoreThreshold, fineResults]
  );
  const filteredCandidateCount = Math.max(fineResults.length - displayCandidates.length, 0);
  const candidatePageCount = Math.max(Math.ceil(displayCandidates.length / CANDIDATE_PAGE_SIZE), 1);
  const currentCandidatePage = Math.min(candidatePage, candidatePageCount);
  const candidatePageStart = (currentCandidatePage - 1) * CANDIDATE_PAGE_SIZE;
  const visibleCandidates = displayCandidates.slice(candidatePageStart, candidatePageStart + CANDIDATE_PAGE_SIZE);
  const selectedResult = displayCandidates[selectedIndex] ?? displayCandidates[0] ?? null;
  const selectedMatch = selectedResult?.best_match as Record<string, any> | undefined;

  const metrics = useMemo(
    () => [
      ["语义状态", mode === "rewrite" ? formatSemanticStatusLabel(semanticStatus) : formatSemanticStatusLabel("disabled")],
      ["候选池", result ? String(displayCandidates.length) : "待执行"],
      ["检测范围", rewriteEnabled ? "普通检测 + 改写检测" : "普通检测"]
    ],
    [displayCandidates.length, mode, result, rewriteEnabled, semanticStatus]
  );

  const evidenceMetrics: MetricRow[] = selectedMatch
    ? [
        { label: "longest_match_len", value: String(selectedMatch.longest_match_len ?? "-") },
        { label: "longest_match_ratio", value: String(selectedMatch.longest_match_ratio ?? "-") },
        { label: "ngram_recall", value: String(selectedMatch.ngram_recall ?? "-") },
        { label: "sequence_ratio", value: String(selectedMatch.sequence_ratio ?? "-") }
      ]
    : [];

  function goToCandidatePage(page: number) {
    const nextPage = Math.max(1, Math.min(page, candidatePageCount));
    const nextStart = (nextPage - 1) * CANDIDATE_PAGE_SIZE;
    const nextEnd = nextStart + CANDIDATE_PAGE_SIZE;
    setCandidatePage(nextPage);
    if (selectedIndex < nextStart || selectedIndex >= nextEnd) {
      setSelectedIndex(nextStart);
    }
  }

  async function handleRun() {
    const queryText = text.trim();
    if (!queryText) {
      setError("开始比对前请先输入待检测文本。");
      return;
    }

    setIsRunning(true);
    setError("");
    try {
      const response = await compareSingle({
        queryText,
        detectionMode: mode,
        topK,
        compareTopK,
        mergedTopK,
        candidateDisplayScoreThreshold
      });
      setResult(response.payload);
      setDurationSeconds(Number.isFinite(Number(response.duration_seconds)) ? Number(response.duration_seconds) : null);
      setSelectedIndex(0);
      setCandidatePage(1);
      setCandidateDisplayScoreThreshold(
        clampThreshold(Number(response.payload?.params?.candidate_display_score_threshold ?? candidateDisplayScoreThreshold))
      );
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "单条比对执行失败");
    } finally {
      setIsRunning(false);
    }
  }

  const summaryText = selectedResult
    ? `当前最高分结果命中 ${selectedResult.book_name || "-"} / ${selectedResult.chapter_name || "-"}，风险标签为 ${formatReviewLabel(String(selectedResult.review_label || "unknown"))}。`
    : fineResults.length > 0
      ? `当前精排结果均低于 ${effectiveCandidateDisplayScoreThreshold.toFixed(2)} 分展示阈值，已自动从候选池中隐藏。`
      : "执行一次比对后，可在这里查看最佳候选和证据详情。";

  const durationLabel = durationSeconds === null ? "待执行" : formatDurationSeconds(durationSeconds);

  return (
    <div className="page-grid single-compare-page">
      <section className="single-page-heading">
        <div>
          <h1>单条比对</h1>
          <p>先对单条文本执行比对，确认候选结果和证据，再决定是否进入批量处理。</p>
        </div>
        <div className="single-header-metrics">
          {metrics.map(([label, value], index) => (
            <div key={label} className="single-header-chip">
              <span className={`single-chip-icon tone-${index}`}>
                <Icon name={index === 0 ? "shield" : index === 1 ? "database" : "clock"} />
              </span>
              <span className="single-chip-label">{label}</span>
              <strong>{value}</strong>
            </div>
          ))}
          <div className="single-header-chip">
            <span className="single-chip-icon tone-3">
              <Icon name="clock" />
            </span>
            <span className="single-chip-label">耗时</span>
            <strong>{durationLabel}</strong>
          </div>
        </div>
      </section>

      <div className="single-compare-layout span-full">
        <div className="single-left-stack">
          <section className="card-panel single-input-panel">
            <div className="single-panel-heading">
              <div>
                <h2>输入文本</h2>
                <p>粘贴剧情简介、推广文案或可疑文本片段。默认执行普通检测，可按需追加改写检测。</p>
              </div>
              <div className="single-input-meta">{text.length} 字</div>
            </div>

            <textarea
              className="single-analysis-textarea"
              value={text}
              onChange={(event) => setText(event.target.value)}
              placeholder="粘贴待比对文本"
            />

            <div className="single-inline-actions">
              <button className="ghost-button slim" type="button" onClick={() => setText("")}>清空</button>
              <button className="ghost-button slim" type="button" onClick={() => setText(sampleText)}>使用示例</button>
            </div>

            <div className="single-mode-switch">
              <button className="active" type="button" disabled>
                <Icon name="refresh" />
                普通检测
              </button>
              <button className={rewriteEnabled ? "active" : ""} type="button" onClick={() => setRewriteEnabled((value) => !value)}>
                <Icon name="review" />
                {rewriteEnabled ? "已开启改写检测" : "开启改写检测"}
              </button>
            </div>

            <button className="primary-button wide single-run-button" type="button" onClick={handleRun} disabled={isRunning}>
              <Icon name="spark" />
              {isRunning ? "比对中..." : "开始比对"}
            </button>

            {error && (
              <StatusState
                title="单条比对执行失败"
                description={error}
                tone="error"
                variant="inline"
                icon="warning"
              />
            )}

            <details className="single-advanced-panel">
              <summary>高级参数</summary>
              <div className="parameter-panel single-parameter-panel">
                <label className="parameter-item editable">
                  <span>top_k</span>
                  <input type="number" min={1} max={20} value={topK} onChange={(event) => setTopK(Number(event.target.value) || 10)} />
                </label>
                <label className="parameter-item editable">
                  <span>compare_top_k</span>
                  <input type="number" min={1} max={100} value={compareTopK} onChange={(event) => setCompareTopK(Number(event.target.value) || 50)} />
                </label>
                <label className="parameter-item editable">
                  <span>merged_top_k</span>
                  <input type="number" min={1} max={200} value={mergedTopK} onChange={(event) => setMergedTopK(Number(event.target.value) || 20)} />
                </label>
                <label className="parameter-item editable">
                  <span>candidate_display_score_threshold</span>
                  <input
                    type="number"
                    min={0}
                    max={1}
                    step={0.01}
                    value={candidateDisplayScoreThreshold}
                    onChange={(event) => setCandidateDisplayScoreThreshold(clampThreshold(Number(event.target.value) || 0))}
                  />
                </label>
              </div>
            </details>
          </section>

          <article className="card-panel single-evidence-panel">
            <div className="single-evidence-header">
              <div className="single-evidence-title">
                <Icon name="review" />
                <div>
                  <h2>证据详情</h2>
                  <p>查看当前候选命中的文本证据，以及支撑其排名的指标信号。</p>
                </div>
              </div>
              {selectedMatch ? (
                <button className="ghost-button slim single-evidence-expand-button" type="button" onClick={() => setIsEvidenceModalOpen(true)}>
                  <Icon name="expand" />
                  放大查看
                </button>
              ) : null}
            </div>

            {selectedMatch ? (
              <div className="single-evidence-grid">
                <div className="single-evidence-preview-stack">
                  <article className="evidence-card single-evidence-card">
                    <span className="single-evidence-label">查询文本</span>
                    <p>{selectedMatch.query_text || selectedMatch.query_text_preview || text}</p>
                  </article>
                  <article className="evidence-card highlighted single-evidence-card">
                    <span className="single-evidence-label">候选文本</span>
                    <p>{selectedMatch.candidate_text || selectedMatch.candidate_text_preview || "-"}</p>
                  </article>
                </div>

                <article className="metrics-card single-metrics-card">
                  <span className="single-evidence-label">命中指标</span>
                  <div className="single-metrics-list">
                    {evidenceMetrics.map((item) => (
                      <div key={item.label} className="single-metric-row">
                        <div>
                          <strong>{formatMetricLabel(item.label)}</strong>
                          <span>{item.value}</span>
                        </div>
                        <div className="single-inline-score"><span style={{ width: `${metricValueToPercent(item.value)}%` }} /></div>
                      </div>
                    ))}
                  </div>
                </article>
              </div>
            ) : (
              <StatusState title="当前还没有证据详情" description="先执行一次比对，并选中候选结果后，这里才会显示命中证据。" icon="review" />
            )}
          </article>
        </div>

        <div className="single-right-stack">
          <article className="dark-card-panel single-summary-panel">
            <div className="single-summary-head">
              <div>
                <h2>最佳命中摘要</h2>
                <div className="single-summary-badges">
                  <span className="single-summary-mode">检测范围：{rewriteEnabled ? "普通检测 + 改写检测" : "普通检测"}</span>
                  <span className="single-summary-mode">耗时：{durationLabel}</span>
                </div>
              </div>
              <span className="single-semantic-state">{formatSemanticStatusLabel(semanticStatus)}</span>
            </div>

            <div className="single-summary-grid">
              <div>
                <span>当前书名</span>
                <strong>{selectedResult?.book_name ?? "-"}</strong>
              </div>
              <div>
                <span>当前章节</span>
                <strong>{selectedResult?.chapter_name ?? "-"}</strong>
              </div>
              <div>
                <span>风险标签</span>
                <strong className="emphasis warm">{formatReviewLabel(String(selectedResult?.review_label ?? ""))}</strong>
              </div>
              <div>
                <span>置信标签</span>
                <strong className="emphasis warm">{formatConfidenceLabel(String(selectedResult?.confidence_label ?? ""))}</strong>
              </div>
              <div>
                <span>精排分数</span>
                <strong>{formatScore(selectedResult?.fine_score)}</strong>
                <div className="single-inline-score">
                  <span style={{ width: `${Math.max(Math.min((Number(selectedResult?.fine_score) || 0) * 100, 100), 0)}%` }} />
                </div>
              </div>
            </div>

            <div className="single-summary-alert">
              <Icon name="warning" />
              <span>{summaryText}</span>
            </div>
          </article>

          <article className="card-panel single-candidate-panel">
            <div className="single-panel-heading">
              <div>
                <h2>精排候选</h2>
                <p>查看当前比对链路返回的候选排序结果。仅展示 fine_score ≥ {effectiveCandidateDisplayScoreThreshold.toFixed(2)} 的候选。</p>
              </div>
              <div className="single-candidate-toolbar">
                <button className="chip-button slim" type="button">共 {displayCandidates.length} 条</button>
                {filteredCandidateCount > 0 ? (
                  <span className="single-page-indicator">已过滤 {filteredCandidateCount} 条低分候选</span>
                ) : null}
                {displayCandidates.length > 0 ? (
                  <span className="single-page-indicator">第 {currentCandidatePage} / {candidatePageCount} 页</span>
                ) : null}
              </div>
            </div>

            <div className="single-candidate-list">
              {displayCandidates.length === 0 ? (
                <StatusState
                  title={fineResults.length > 0 ? "当前没有可展示的候选" : "当前还没有候选结果"}
                  description={
                    fineResults.length > 0
                      ? `当前候选的精排分数均低于 ${effectiveCandidateDisplayScoreThreshold.toFixed(2)}，已自动从候选池中隐藏。`
                      : "执行一次比对后，这里会展示满足阈值条件的精排候选。"
                  }
                  icon="search"
                />
              ) : (
                visibleCandidates.map((item, pageIndex) => {
                  const absoluteIndex = candidatePageStart + pageIndex;
                  return (
                    <article
                      key={`${item.book_name}-${item.chapter_name}-${absoluteIndex}`}
                      className={`single-candidate-row${selectedIndex === absoluteIndex ? " is-selected" : ""}`}
                      onClick={() => setSelectedIndex(absoluteIndex)}
                    >
                      <div className={`single-rank-badge rank-${Math.min(absoluteIndex + 1, 5)}`}>{absoluteIndex + 1}</div>
                      <div className="single-candidate-main">
                        <div className="single-candidate-columns">
                          <div>
                            <span>书名</span>
                            <strong>{item.book_name || "-"}</strong>
                          </div>
                          <div>
                            <span>章节</span>
                            <strong>{item.chapter_name || "-"}</strong>
                          </div>
                          <div className="single-score-block">
                            <span>精排分数</span>
                            <strong>{formatScore(item.fine_score)}</strong>
                            <div className="single-inline-score"><span style={{ width: `${Math.min((Number(item.fine_score) || 0) * 100, 100)}%` }} /></div>
                          </div>
                          <div>
                            <span>风险标签</span>
                            <strong className="soft-emphasis">{formatReviewLabel(String(item.review_label || ""))}</strong>
                          </div>
                          <div>
                            <span>置信标签</span>
                            <strong className="soft-emphasis">{formatConfidenceLabel(String(item.confidence_label || ""))}</strong>
                          </div>
                          <div>
                            <span>{formatMetricLabel("coarse_rank")}</span>
                            <strong>{item.coarse_rank ?? "-"}</strong>
                          </div>
                        </div>
                      </div>
                    </article>
                  );
                })
              )}
            </div>

            {displayCandidates.length > CANDIDATE_PAGE_SIZE ? (
              <div className="single-candidate-pagination">
                <button
                  className="outline-button slim"
                  type="button"
                  onClick={() => goToCandidatePage(currentCandidatePage - 1)}
                  disabled={currentCandidatePage <= 1}
                >
                  上一页
                </button>
                <button
                  className="outline-button slim"
                  type="button"
                  onClick={() => goToCandidatePage(currentCandidatePage + 1)}
                  disabled={currentCandidatePage >= candidatePageCount}
                >
                  下一页
                </button>
              </div>
            ) : null}
          </article>
        </div>
      </div>

      {selectedMatch && isEvidenceModalOpen ? (
        <div className="single-evidence-modal-overlay" role="dialog" aria-modal="true" aria-label="证据详情放大查看" onClick={() => setIsEvidenceModalOpen(false)}>
          <div className="single-evidence-modal" onClick={(event) => event.stopPropagation()}>
            <div className="single-evidence-modal-header">
              <div className="single-evidence-modal-title">
                <div>
                  <span className="single-evidence-modal-eyebrow">证据详情</span>
                  <h2>{selectedResult?.book_name || "当前命中结果"}</h2>
                  <p>{selectedResult?.chapter_name || "查看查询文本与候选文本的完整对照"}</p>
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
                  <p>{selectedMatch.query_text || selectedMatch.query_text_preview || text}</p>
                </article>
                <article className="evidence-card highlighted single-evidence-modal-card">
                  <span className="single-evidence-label">候选文本</span>
                  <p>{selectedMatch.candidate_text || selectedMatch.candidate_text_preview || "-"}</p>
                </article>
              </div>

              <article className="metrics-card single-evidence-modal-metrics">
                <div className="single-evidence-modal-metrics-header">
                  <span className="single-evidence-label">命中指标</span>
                  <strong>{formatScore(selectedResult?.fine_score)}</strong>
                </div>
                <div className="single-metrics-list">
                  {evidenceMetrics.map((item) => (
                    <div key={item.label} className="single-metric-row">
                      <div>
                        <strong>{formatMetricLabel(item.label)}</strong>
                        <span>{item.value}</span>
                      </div>
                      <div className="single-inline-score"><span style={{ width: `${metricValueToPercent(item.value)}%` }} /></div>
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
