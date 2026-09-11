import { useEffect, useRef, useState, type DragEvent } from "react";
import { Link } from "react-router-dom";

import {
  compareDramaSubtitles,
  controlDramaSubtitleTask,
  createDramaSubtitleTask,
  downloadDramaSubtitleReviewExport,
  getDramaSubtitleEvidenceContext,
  getDramaSubtitleTaskDetail,
  listDramaSubtitleTasks
  ,saveDramaSubtitleReview
} from "../api";
import { Icon } from "../icons";
import { StatusState } from "../StatusState";

const LANGUAGE_LABELS: Record<string, string> = {
  zh: "中文", en: "英文", pt: "葡萄牙语", ko: "韩文", ja: "日文", mixed: "混合语言", unknown: "未识别"
};

function languageLabel(value: unknown): string {
  const code = String(value ?? "unknown").trim().toLowerCase();
  return (LANGUAGE_LABELS[code] ?? code) || "未识别";
}
function formatDuration(value: unknown): string {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return "-";
  return `${seconds < 10 ? seconds.toFixed(2) : seconds.toFixed(1)} 秒`;
}

function formatQueueSeconds(value: unknown): string {
  const seconds = Math.max(Math.round(Number(value) || 0), 0);
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  const remain = seconds % 60;
  if (minutes < 60) return remain > 0 ? `${minutes} 分 ${remain} 秒` : `${minutes} 分`;
  const hours = Math.floor(minutes / 60);
  return `${hours} 小时 ${minutes % 60} 分`;
}

function taskQueueFeedback(task: Record<string, any>): string {
  const queue = task.queue && typeof task.queue === "object" ? task.queue : {};
  const status = String(task.status || "");
  if (status === "completed") return "已完成";
  if (["failed", "partial_failed", "cancelled"].includes(status)) return "任务已结束";
  if (status === "paused") return "已暂停，不占用排队位置";
  if (String(queue.state || "") === "queued") {
    const ahead = Number(queue.tasks_ahead_count || 0);
    const items = Number(queue.items_ahead_count || 0);
    return `前方 ${ahead} 个任务，约 ${items} 条 · 预计等待 ${formatQueueSeconds(queue.estimated_wait_seconds)}`;
  }
  if (String(queue.state || "") === "running") {
    return `已领取 · 预计剩余 ${formatQueueSeconds(queue.estimated_remaining_seconds)}`;
  }
  if (String(queue.state || "") === "control_pending") return "正在处理暂停或取消请求";
  return "等待队列信息更新";
}

function formatPercent(value: unknown): string {
  const rate = Number(value);
  if (!Number.isFinite(rate)) return "-";
  return `${(Math.max(0, Math.min(rate, 1)) * 100).toFixed(1)}%`;
}

function formatSimilarity(value: unknown): string {
  const score = Number(value);
  return Number.isFinite(score) ? score.toFixed(3) : "-";
}

function highlightSharedTrigrams(text: string, trigrams: unknown): Array<string | JSX.Element> {
  const shared = Array.isArray(trigrams) ? trigrams.filter((value): value is string => Boolean(value)) : [];
  if (!text || shared.length === 0) return [text];
  const compactChars: string[] = [];
  const originalIndexes: number[] = [];
  for (let index = 0; index < text.length; index += 1) {
    if (/\s/.test(text[index])) continue;
    compactChars.push(text[index]);
    originalIndexes.push(index);
  }
  const compact = compactChars.join("");
  const ranges: Array<[number, number]> = [];
  for (const gram of shared) {
    let offset = compact.indexOf(gram);
    while (offset >= 0) {
      const end = offset + gram.length - 1;
      if (originalIndexes[end] !== undefined) ranges.push([originalIndexes[offset], originalIndexes[end] + 1]);
      offset = compact.indexOf(gram, offset + 1);
    }
  }
  ranges.sort((left, right) => left[0] - right[0] || left[1] - right[1]);
  const merged: Array<[number, number]> = [];
  for (const range of ranges) {
    const previous = merged[merged.length - 1];
    if (previous && range[0] <= previous[1]) previous[1] = Math.max(previous[1], range[1]);
    else merged.push([...range]);
  }
  if (merged.length === 0) return [text];
  const parts: Array<string | JSX.Element> = [];
  let cursor = 0;
  merged.forEach(([start, end], index) => {
    if (start > cursor) parts.push(text.slice(cursor, start));
    parts.push(<mark key={`shared-${index}`}>{text.slice(start, end)}</mark>);
    cursor = end;
  });
  if (cursor < text.length) parts.push(text.slice(cursor));
  return parts;
}

const ENGLISH_FUNCTION_WORDS = new Set([
  "about", "after", "again", "also", "and", "are", "been", "being", "but", "can", "could", "did", "does", "for",
  "from", "have", "here", "into", "just", "like", "more", "most", "much", "not", "only", "over", "some", "than",
  "that", "the", "their", "there", "these", "they", "this", "those", "through", "very", "was", "were", "what",
  "when", "where", "which", "while", "with", "would", "your",
]);

function englishWordStem(value: string): string {
  let word = value.toLowerCase().replace(/[^a-z0-9]/g, "");
  if (word.length < 4 || ENGLISH_FUNCTION_WORDS.has(word)) return "";
  if (word.endsWith("ies") && word.length > 5) word = word.slice(0, -3) + "y";
  else if (word.endsWith("ing") && word.length > 6) word = word.slice(0, -3);
  else if (word.endsWith("ed") && word.length > 5) word = word.slice(0, -2);
  else if (word.endsWith("s") && word.length > 4) word = word.slice(0, -1);
  return word;
}

function highlightSharedWordPhrases(
  text: string,
  phrases: unknown,
  comparisonText = "",
): Array<string | JSX.Element> {
  const shared = Array.isArray(phrases) ? phrases.filter((value): value is string => Boolean(value)) : [];
  if (!text) return [text];
  const phraseRanges: Array<[number, number]> = [];
  for (const phrase of shared) {
    const words = phrase.split(/\s+/).filter(Boolean);
    if (words.length === 0) continue;
    const matcher = new RegExp("\\b" + words.join("[^A-Za-z0-9]+") + "\\b", "gi");
    let match = matcher.exec(text);
    while (match) {
      phraseRanges.push([match.index, match.index + match[0].length]);
      match = matcher.exec(text);
    }
  }
  phraseRanges.sort((left, right) => left[0] - right[0] || left[1] - right[1]);
  const mergedPhrases: Array<[number, number]> = [];
  for (const range of phraseRanges) {
    const previous = mergedPhrases[mergedPhrases.length - 1];
    if (previous && range[0] <= previous[1]) previous[1] = Math.max(previous[1], range[1]);
    else mergedPhrases.push([...range]);
  }

  const comparisonStems = new Set(
    (comparisonText.match(/[A-Za-z][A-Za-z']*/g) || [])
      .map(englishWordStem)
      .filter(Boolean),
  );
  const softRanges: Array<[number, number]> = [];
  const wordMatcher = /[A-Za-z][A-Za-z']*/g;
  let wordMatch = wordMatcher.exec(text);
  while (wordMatch) {
    const start = wordMatch.index;
    const end = start + wordMatch[0].length;
    const overlapsPhrase = mergedPhrases.some(([phraseStart, phraseEnd]) => start < phraseEnd && end > phraseStart);
    if (!overlapsPhrase && comparisonStems.has(englishWordStem(wordMatch[0]))) {
      softRanges.push([start, end]);
    }
    wordMatch = wordMatcher.exec(text);
  }

  const ranges = [
    ...mergedPhrases.map(([start, end]) => ({ start, end, tone: "phrase" as const })),
    ...softRanges.map(([start, end]) => ({ start, end, tone: "word" as const })),
  ].sort((left, right) => left.start - right.start || right.end - left.end);
  if (ranges.length === 0) return [text];
  const parts: Array<string | JSX.Element> = [];
  let cursor = 0;
  ranges.forEach(({ start, end, tone }, index) => {
    if (start > cursor) parts.push(text.slice(cursor, start));
    parts.push(<mark className={tone === "phrase" ? "phrase-match" : "soft-match"} key={"shared-word-" + index}>{text.slice(start, end)}</mark>);
    cursor = end;
  });
  if (cursor < text.length) parts.push(text.slice(cursor));
  return parts;
}

function highlightMatchedUnits(
  text: string,
  metrics: Record<string, any> | undefined,
  comparisonText = "",
): Array<string | JSX.Element> {
  if (metrics?.match_unit === "word_4gram") {
    return highlightSharedWordPhrases(text, metrics.shared_trigrams, comparisonText);
  }
  return highlightSharedTrigrams(text, metrics?.shared_trigrams);
}

function semanticStatusLabel(value: unknown): string {
  if (!value) return "-";
  switch (String(value || "")) {
    case "ok": return "语义增强正常";
    case "disabled": return "仅词级检索";
    case "fallback_lexical_only": return "语义降级为词级";
    default: return "语义状态待确认";
  }
}

function decisionStatusLabel(value: unknown): string {
  switch (String(value || "")) {
    case "matched": return "确认命中";
    case "review_required": return "待人工复核";
    case "not_matched": return "未命中当前库";
    default: return "尚未判定";
  }
}

function decisionReasonLabel(value: unknown): string {
  switch (String(value || "")) {
    case "strong_lexical_evidence": return "词级复用证据达到确认阈值";
    case "strong_fuzzy_lexical_evidence": return "中文转写容错对齐达到确认阈值";
    case "lexical_evidence_below_acceptance_threshold": return "存在词级证据，但还不足以自动确认";
    case "semantic_candidate_requires_manual_review": return "语义相似度较高，需要人工核验原文";
    case "translation_assisted_candidate_requires_review": return "翻译后发现跨语言候选，需要核验原字幕";
    case "multiple_books_with_strong_lexical_evidence": return "多个候选同时具备强证据，需要人工区分";
    case "semantic_unavailable_no_lexical_evidence": return "语义服务不可用，且没有可确认的词级证据";
    case "no_candidate_above_acceptance_threshold": return "没有候选达到确认或复核门槛";
    default: return String(value || "");
  }
}

function decisionMessage(decision: Record<string, any>): string {
  const message = String(decision.user_message || "").trim();
  return message || decisionReasonLabel(decision.reason);
}

function decisionHeadline(decision: Record<string, any>): string {
  if (decision.hit_status === "review_required" || decision.outcome === "translation_assisted_match") return "发现候选，尚未确认命中";
  if (decision.content_match_status === "matched" && decision.title_resolution === "ambiguous") return "内容已命中，剧名待确认";
  if (decision.content_match_status === "matched") return "内容已确认命中";
  return decisionStatusLabel(decision.status);
}

function decisionReviewFeedback(decision: Record<string, any>): Record<string, any> | null {
  const feedback = decision.review_feedback;
  return feedback && typeof feedback === "object" ? feedback : null;
}

function ReviewFeedback({ decision }: { decision: Record<string, any> }) {
  const feedback = decisionReviewFeedback(decision);
  if (!feedback) return null;
  const signals = Array.isArray(feedback.signals)
    ? feedback.signals.filter((signal): signal is Record<string, any> => Boolean(signal && typeof signal === "object"))
    : [];
  return <div className="drama-review-feedback">
    <strong>{String(feedback.title || "需要人工复核")}</strong>
    {feedback.summary && <p>{String(feedback.summary)}</p>}
    {signals.length > 0 && <div className="drama-review-signals">{signals.map((signal, index) => <span key={`${String(signal.label || "指标")}-${index}`}><b>{String(signal.label || "指标")}</b>{String(signal.value ?? "-")}</span>)}</div>}
    {feedback.recommended_action && <p className="drama-review-action"><b>建议核对：</b>{String(feedback.recommended_action)}</p>}
  </div>;
}

function StrongCandidateList({ candidates, fallbackCandidates = [], title = "强证据候选" }: { candidates: unknown; fallbackCandidates?: unknown; title?: string }) {
  const fallbackItems = Array.isArray(fallbackCandidates) ? fallbackCandidates.filter((candidate): candidate is Record<string, any> => Boolean(candidate && typeof candidate === "object")) : [];
  const items: Record<string, any>[] = (Array.isArray(candidates) ? candidates : []).filter((candidate): candidate is Record<string, any> => Boolean(candidate && typeof candidate === "object")).map((candidate): Record<string, any> => {
    const fallback = fallbackItems.find((item) => String(item.book_id || "") === String(candidate.book_id || "") && Number(item.episode_order || 0) === Number(candidate.matched_episode_order || 0)) || {};
    const metrics = fallback.match_metrics && typeof fallback.match_metrics === "object" ? fallback.match_metrics : {};
    return {
      ...fallback,
      ...candidate,
      text_coverage_rate: candidate.text_coverage_rate ?? metrics.query_coverage_rate,
      aggregate_text_coverage_rate: candidate.aggregate_text_coverage_rate ?? metrics.retrieved_window_query_coverage_rate,
      matched_window_count: candidate.matched_window_count ?? fallback.retrieved_window_count,
      evidence_coverage_rate: candidate.evidence_coverage_rate ?? metrics.evidence_coverage_rate,
      semantic_score: candidate.semantic_score ?? fallback.semantic_score,
    };
  });
  if (items.length === 0) return null;
  return <div className="drama-strong-candidates"><strong className="drama-candidate-list-title">{title}</strong>{items.map((candidate, index) => {
    const priority = Number(candidate.review_priority || index + 1);
    const aggregateCoverage = candidate.aggregate_text_coverage_rate === undefined || candidate.aggregate_text_coverage_rate === null
      ? "历史任务未计算"
      : formatPercent(candidate.aggregate_text_coverage_rate);
    return <div className="drama-strong-candidate" key={`${candidate.book_id || "candidate"}-${candidate.matched_episode_order || index}`}>
      <strong>优先级 {priority}</strong><span>{candidate.book_name || candidate.book_id || "未命名剧集"} · 第 {candidate.matched_episode_order || "-"} 集</span>
      <small>累计文本覆盖 {aggregateCoverage} · 最佳片段覆盖 {formatPercent(candidate.text_coverage_rate)} · 命中窗口 {candidate.matched_window_count || "-"} · 语义相似度 {formatSimilarity(candidate.semantic_score)}{candidate.ordered_alignment_score !== undefined && candidate.ordered_alignment_score !== null ? ` · 同序对齐 ${formatPercent(candidate.ordered_alignment_score)}` : ""}</small>
    </div>;
  })}</div>;
}

function retrievalSourceLabel(value: unknown): string {
  switch (String(value || "")) {
    case "lexical": return "词级证据";
    case "semantic": return "语义候选";
    default: return String(value || "未知来源");
  }
}

function RetrievalSourceBadges({ candidate }: { candidate: Record<string, any> }) {
  const sources = Array.isArray(candidate.retrieval_sources) ? candidate.retrieval_sources : [];
  if (sources.length === 0) return <span className="soft-tag muted">历史词级结果</span>;
  return <div className="drama-result-badges">{sources.map((source: unknown) => <span key={String(source)} className={`soft-tag drama-source-${String(source)}`}>{retrievalSourceLabel(source)}</span>)}</div>;
}

function taskItemPayload(item: Record<string, any> | null | undefined): Record<string, any> {
  try {
    const parsed = JSON.parse(String(item?.result_payload_json || "{}"));
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function taskItemDecision(item: Record<string, any> | null | undefined): Record<string, any> {
  const payload = taskItemPayload(item);
  return payload.decision && typeof payload.decision === "object" ? payload.decision : {};
}

function taskItemDisplayCandidate(item: Record<string, any> | null | undefined): Record<string, any> {
  const payload = taskItemPayload(item);
  const candidates = Array.isArray(payload.candidates) ? payload.candidates : [];
  const decision = taskItemDecision(item);
  const rank = Number(decision.candidate_rank || 0);
  return (candidates.find((candidate: Record<string, any>) => Number(candidate?.rank || 0) === rank) || candidates[0] || {}) as Record<string, any>;
}

function deriveSharedTrigrams(queryText: string, evidenceText: string): string[] {
  const compact = (text: string) => text.replace(/\s+/g, "");
  const query = compact(queryText);
  const evidence = compact(evidenceText);
  if (query.length < 3 || evidence.length < 3) return [];

  const evidenceTrigrams = new Set<string>();
  for (let index = 0; index <= evidence.length - 3; index += 1) {
    evidenceTrigrams.add(evidence.slice(index, index + 3));
  }

  const shared: string[] = [];
  const seen = new Set<string>();
  for (let index = 0; index <= query.length - 3; index += 1) {
    const trigram = query.slice(index, index + 3);
    if (evidenceTrigrams.has(trigram) && !seen.has(trigram)) {
      shared.push(trigram);
      seen.add(trigram);
    }
  }
  return shared;
}

function deriveSharedWordPhrases(queryText: string, evidenceText: string): string[] {
  const words = (text: string) => (text.toLowerCase().match(/[A-Za-z][A-Za-z']*/g) || []);
  const phrases = (text: string) => {
    const tokens = words(text);
    const values = new Set<string>();
    for (let index = 0; index <= tokens.length - 4; index += 1) {
      values.add(tokens.slice(index, index + 4).join(" "));
    }
    return values;
  };
  const evidencePhrases = phrases(evidenceText);
  return [...phrases(queryText)].filter((phrase) => evidencePhrases.has(phrase));
}

function taskItemEvidence(item: Record<string, any>, expandedEvidenceText = ""): {
  queryText: string;
  evidenceText: string;
  sharedTrigrams: string[];
  matchUnit: string;
} {
  const payload = taskItemPayload(item);
  const topCandidate = Array.isArray(payload.candidates) ? payload.candidates[0] || {} : {};
  const evidence = topCandidate.evidence || {};
  const storedMatchUnit = String(topCandidate.match_metrics?.match_unit || "");
  const storedSharedTrigrams = Array.isArray(topCandidate.match_metrics?.shared_trigrams)
    ? topCandidate.match_metrics.shared_trigrams.filter((value: unknown): value is string => typeof value === "string")
    : [];
  const queryText = String(item.query_text || "");
  const evidenceText = expandedEvidenceText || String(evidence.window_text || evidence.window_text_preview || "");
  const wordBasedLanguages = new Set(["en", "pt"]);
  const isEnglish = wordBasedLanguages.has(
    String(item.query_language_code || topCandidate.language_code || "").trim().toLowerCase()
  );
  const matchUnit = storedMatchUnit || (isEnglish ? "word_4gram" : "character_trigram");
  return {
    queryText,
    evidenceText,
    // Historical batch results predate match_metrics. Preserve the correct unit
    // for each language so English never falls back to misleading character marks.
    sharedTrigrams: storedSharedTrigrams.length > 0
      ? storedSharedTrigrams
      : isEnglish
        ? deriveSharedWordPhrases(queryText, evidenceText)
        : deriveSharedTrigrams(queryText, evidenceText),
    matchUnit
  };
}

function taskItemWindowUid(item: Record<string, any> | null | undefined): string {
  const payload = taskItemPayload(item);
  const topCandidate = Array.isArray(payload.candidates) ? payload.candidates[0] || {} : {};
  return String(topCandidate.evidence?.window_uid || "");
}

function reviewContextChars(queryText: string): number {
  return Math.min(Math.max(queryText.length, 1800), 5000);
}

export function DramaSubtitlePage({ mode = "all" }: { mode?: "all" | "single" | "batch" }) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [queryText, setQueryText] = useState("");
  const [languageOverride, setLanguageOverride] = useState("");
  const [singleResult, setSingleResult] = useState<Record<string, any> | null>(null);
  const [durationSeconds, setDurationSeconds] = useState<number | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [tasks, setTasks] = useState<Record<string, any>[]>([]);
  const [selectedTask, setSelectedTask] = useState<Record<string, any> | null>(null);
  const [taskItems, setTaskItems] = useState<Record<string, any>[]>([]);
  const [selectedTaskItemId, setSelectedTaskItemId] = useState<number | null>(null);
  const [isComparing, setIsComparing] = useState(false);
  const [isCreating, setIsCreating] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const [reviewingItemId, setReviewingItemId] = useState<number | null>(null);
  const [isExporting, setIsExporting] = useState(false);
  const [error, setError] = useState("");
  const [evidenceContexts, setEvidenceContexts] = useState<Record<string, Record<string, any>>>({});
  const [loadingEvidenceWindowUid, setLoadingEvidenceWindowUid] = useState("");
  const [collapsedEvidenceWindowUid, setCollapsedEvidenceWindowUid] = useState("");

  async function loadTasks(preferredTaskId = "") {
    const response = await listDramaSubtitleTasks(30, 0);
    setTasks(response.items);
    const taskId = preferredTaskId || selectedTask?.task_id || response.items[0]?.task_id;
    if (!taskId) {
      setSelectedTask(null);
      setTaskItems([]);
      return;
    }
    const detail = await getDramaSubtitleTaskDetail(String(taskId));
    setSelectedTask(detail.task);
    const nextItems = detail.items;
    setTaskItems(nextItems);
    setSelectedTaskItemId((current) => nextItems.some((item) => Number(item.task_item_id) === current)
      ? current
      : (nextItems[0] ? Number(nextItems[0].task_item_id) : null));
  }

  useEffect(() => {
    void loadTasks().catch((reason) => setError(reason instanceof Error ? reason.message : "加载短剧任务失败"));
  }, []);

  useEffect(() => {
    if (!tasks.some((task) => ["queued", "running"].includes(String(task.status)))) return;
    const timer = window.setInterval(() => void loadTasks().catch(() => undefined), 3000);
    return () => window.clearInterval(timer);
  }, [tasks, selectedTask?.task_id]);

  async function handleCompare() {
    if (!queryText.trim()) {
      setError("请输入需要检测的字幕或台词。");
      return;
    }
    setIsComparing(true);
    setError("");
    try {
      const response = await compareDramaSubtitles({ queryText, topK: 10, windowLimit: 200, languageCode: languageOverride });
      setSingleResult(response.payload);
      setDurationSeconds(response.duration_seconds);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "短剧字幕检测失败");
    } finally {
      setIsComparing(false);
    }
  }

  function selectFile(nextFile: File | null) {
    if (nextFile) {
      setFile(nextFile);
      setError("");
    }
  }

  function handleDrop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    setIsDragging(false);
    selectFile(event.dataTransfer.files?.[0] ?? null);
  }

  async function handleCreateTask() {
    if (!file) {
      setError("请先选择包含台词的 Excel、CSV、TSV 或 TXT 文件。");
      return;
    }
    setIsCreating(true);
    setError("");
    try {
      const response = await createDramaSubtitleTask({ file, topK: 10, windowLimit: 200 });
      setFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
      await loadTasks(response.task_id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "创建短剧批量任务失败");
    } finally {
      setIsCreating(false);
    }
  }

  async function handleTaskControl(action: "pause" | "resume" | "cancel" | "delete") {
    if (!selectedTask?.task_id) return;
    setError("");
    try {
      await controlDramaSubtitleTask(String(selectedTask.task_id), action);
      await loadTasks(String(selectedTask.task_id));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "短剧任务操作失败");
    }
  }

  async function handleReview(taskItemId: number, reviewStatus: string) {
    setReviewingItemId(taskItemId);
    setError("");
    try {
      await saveDramaSubtitleReview(taskItemId, reviewStatus);
      if (selectedTask?.task_id) await loadTasks(String(selectedTask.task_id));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存短剧复核状态失败");
    } finally {
      setReviewingItemId(null);
    }
  }

  async function handleExport() {
    if (!selectedTask?.task_id) return;
    setIsExporting(true);
    setError("");
    try {
      await downloadDramaSubtitleReviewExport(String(selectedTask.task_id));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "导出短剧复核结果失败");
    } finally {
      setIsExporting(false);
    }
  }

  async function loadEvidenceContext(windowUid: string, queryTextForLength: string) {
    if (!windowUid || evidenceContexts[windowUid] || loadingEvidenceWindowUid === windowUid) return;
    setLoadingEvidenceWindowUid(windowUid);
    try {
      const response = await getDramaSubtitleEvidenceContext(windowUid, reviewContextChars(queryTextForLength));
      setEvidenceContexts((current) => ({ ...current, [windowUid]: response.context }));
    } catch (reason) {
      setEvidenceContexts((current) => ({
        ...current,
        [windowUid]: { error: reason instanceof Error ? reason.message : "加载扩展证据失败" }
      }));
    } finally {
      setLoadingEvidenceWindowUid((current) => current === windowUid ? "" : current);
    }
  }

  const candidates = Array.isArray(singleResult?.candidates) ? singleResult.candidates : [];
  const selectedTaskItem = taskItems.find((item) => Number(item.task_item_id) === selectedTaskItemId) ?? null;
  const selectedTaskPayload = taskItemPayload(selectedTaskItem);
  const selectedTaskDecision = taskItemDecision(selectedTaskItem);
  const selectedTaskCandidate = Array.isArray(selectedTaskPayload.candidates) ? selectedTaskPayload.candidates[0] || null : null;
  const selectedTaskEvidenceWindowUid = taskItemWindowUid(selectedTaskItem);
  const selectedTaskEvidenceContext = evidenceContexts[selectedTaskEvidenceWindowUid] || null;

  useEffect(() => {
    if (!selectedTaskEvidenceWindowUid || !selectedTaskItem?.query_text) return;
    void loadEvidenceContext(selectedTaskEvidenceWindowUid, String(selectedTaskItem.query_text));
  }, [selectedTaskEvidenceWindowUid, selectedTaskItem?.query_text, evidenceContexts]);

  return (
    <div className={`page-grid drama-subtitle-page is-${mode}`}>
      <div className="drama-page-switch"><Link className="outline-button slim" to={mode === "batch" ? "/drama-subtitles" : "/drama-subtitles/batch"}>{mode === "batch" ? "\u8fd4\u56de\u5355\u6761\u68c0\u6d4b" : "\u8fdb\u5165\u6279\u91cf\u4efb\u52a1"}</Link></div>
      <section className="single-page-heading">
        <div><h1>短剧字幕比对</h1><p>优先按输入原语言检索同语种字幕，返回命中剧集、集数和原始字幕时间证据。</p></div>
        {mode === "batch" && <div className="drama-batch-page-title"><h1>{"\u77ed\u5267\u6279\u91cf\u4efb\u52a1"}</h1><p>{"\u72ec\u7acb\u7ba1\u7406\u77ed\u5267\u5b57\u5e55\u6279\u91cf\u4efb\u52a1\u3001\u8fdb\u5ea6\u3001\u7ed3\u679c\u590d\u6838\u548c\u8bc1\u636e\u5bfc\u51fa\u3002"}</p></div>}
        <div className="single-header-metrics">
          <div className="single-header-chip"><span className="single-chip-label">输入语言</span><strong>{languageLabel(singleResult?.query_language_code)}</strong></div>
          <div className="single-header-chip"><span className="single-chip-label">候选剧集</span><strong>{singleResult?.candidate_count ?? "待检测"}</strong></div>
          <div className="single-header-chip"><span className="single-chip-label">语义增强</span><strong>{semanticStatusLabel(singleResult?.semantic_status)}</strong></div>
          <div className="single-header-chip"><span className="single-chip-label">耗时</span><strong>{durationSeconds === null ? "-" : formatDuration(durationSeconds)}</strong></div>
        </div>
      </section>

      <div className="drama-subtitle-workbench span-full">
        <section className="card-panel drama-subtitle-input">
          <h2>单条字幕检测</h2><p>先同语种匹配。翻译回退仅在原语言无候选或低结果时再启用。</p>
          <textarea className="single-analysis-textarea" value={queryText} onChange={(event) => setQueryText(event.target.value)} placeholder="粘贴待检测的字幕或台词片段" />
          <div className="drama-subtitle-controls">
            <label>语言覆盖<select value={languageOverride} onChange={(event) => setLanguageOverride(event.target.value)}><option value="">自动识别</option><option value="zh">中文</option><option value="en">英文</option><option value="ko">韩文</option><option value="ja">日文</option></select></label>
            <button className="ghost-button" type="button" onClick={() => setQueryText("")}>清空</button>
            <button className="primary-button" type="button" onClick={() => void handleCompare()} disabled={isComparing}><Icon name="search" />{isComparing ? "检索中..." : "开始检测"}</button>
          </div>
        </section>

        <section className="card-panel drama-subtitle-upload">
          <h2>批量字幕任务</h2><p>支持采集软件导出的 Excel、CSV、TSV 或 TXT，短剧名、作者和集数会随任务保留。</p>
          <label className={`batch-dropzone${isDragging ? " is-dragging" : ""}`} onDragEnter={() => setIsDragging(true)} onDragLeave={() => setIsDragging(false)} onDragOver={(event) => event.preventDefault()} onDrop={handleDrop}>
            <input ref={fileInputRef} type="file" accept=".xlsx,.csv,.tsv,.txt" onChange={(event) => selectFile(event.target.files?.[0] ?? null)} />
            <Icon name="layers" /><strong>{file ? file.name : "拖入文件，或点击选择文件"}</strong><span>{file ? `${Math.max(file.size / 1024, 1).toFixed(1)} KB` : "支持 Excel / CSV / TSV / TXT"}</span>
          </label>
          <button className="primary-button wide" type="button" onClick={() => void handleCreateTask()} disabled={isCreating}>{isCreating ? "创建中..." : "创建短剧批量任务"}</button>
        </section>
      </div>

      {error && <StatusState title="短剧字幕检测提示" description={error} tone="error" variant="inline" icon="warning" />}

      <section className="card-panel drama-subtitle-candidates span-full">
        <div className="section-heading"><div><h2>单条候选证据</h2><p>词级结果提供可解释高亮，语义结果用于补充改写候选；证据始终展示原始字幕。</p></div></div>
        {singleResult?.decision && <div className="drama-decision-notice"><strong>最终判定：{decisionHeadline(singleResult.decision)}</strong><span>{decisionMessage(singleResult.decision)}</span><ReviewFeedback decision={singleResult.decision} /><StrongCandidateList candidates={singleResult.decision.strong_match_candidates} fallbackCandidates={singleResult.candidates} /><StrongCandidateList candidates={singleResult.decision.content_candidate_options} fallbackCandidates={singleResult.candidates} title="待核验内容候选（不代表命中）" />{singleResult.decision.related_episode_orders?.length > 0 && <span>同剧关联集数：{singleResult.decision.related_episode_orders.join("、")}</span>}</div>}
        {singleResult?.semantic_status === "fallback_lexical_only" && <div className="drama-semantic-notice"><strong>语义增强暂不可用</strong><span>{singleResult?.semantic_error || "已自动保留词级检索结果。"}</span></div>}
        {candidates.length > 0 ? <div className="drama-candidate-grid">{candidates.map((candidate: Record<string, any>) => { const windowUid = String(candidate.evidence?.window_uid || ""); const expanded = evidenceContexts[windowUid]?.context; const expandedText = String(expanded?.text || ""); return <article key={`${candidate.book_id}-${candidate.episode_order}-${candidate.rank}`} className="drama-candidate-card"><span>候选 #{candidate.rank}</span><RetrievalSourceBadges candidate={candidate} /><h3>{candidate.book_name || "未命名剧集"}</h3><p>{candidate.book_id} · 第 {candidate.episode_order} 集 · {languageLabel(candidate.language_code)}</p><small>{candidate.evidence?.time_start || "-"} 至 {candidate.evidence?.time_end || "-"} · 命中窗口 {candidate.retrieved_window_count}</small><pre>{expandedText || candidate.evidence?.window_text || candidate.evidence?.window_text_preview || "无可用证据文本"}</pre><button className="ghost-button slim" type="button" disabled={!windowUid || loadingEvidenceWindowUid === windowUid} onClick={() => void loadEvidenceContext(windowUid, String(singleResult?.query_text || queryText))}>{loadingEvidenceWindowUid === windowUid ? "加载完整上下文中..." : expandedText ? "已加载扩展复核内容" : "加载完整复核上下文"}</button>{expanded && <small className="drama-evidence-context-meta">复核范围：第 {expanded.line_start} 至 {expanded.line_end} 行，共 {expanded.char_count} 字符；命中窗口已保留。</small>}</article>; })}</div> : <StatusState title="尚无候选" description="提交一段字幕后，这里会展示命中剧集及其原始字幕证据。" icon="search" />}
      </section>

      {candidates.length > 0 && <div className="drama-candidate-insights span-full"><div className="drama-metric-note">词法覆盖率 = 查询片段在候选证据中的覆盖比例；英文按连续四词短语，中文等连续文本按字符三元词。语义分数用于补充排序，不替代词级证据高亮。</div>{candidates.map((candidate: Record<string, any>) => { const windowUid = String(candidate.evidence?.window_uid || ""); const contextText = String(evidenceContexts[windowUid]?.context?.text || candidate.evidence?.window_text || candidate.evidence?.window_text_preview || ""); return <article key={`insight-${candidate.book_id}-${candidate.episode_order}-${candidate.rank}`} className="drama-candidate-insight"><div><strong>候选证据 · #{candidate.rank}</strong><RetrievalSourceBadges candidate={candidate} /><h3>{candidate.book_name || "未命名剧集"}</h3><p>{candidate.book_id} · 第 {candidate.episode_order} 集 · {languageLabel(candidate.language_code)} · {candidate.evidence?.time_start || "-"} 至 {candidate.evidence?.time_end || "-"}</p></div><div className="drama-match-metrics"><strong>词法覆盖率 {formatPercent(candidate.match_metrics?.query_coverage_rate)}</strong><span>{candidate.match_metrics?.match_unit_label || "共享三元词"} {candidate.match_metrics?.shared_trigram_count ?? 0}/{candidate.match_metrics?.query_trigram_count ?? 0}</span><span>语义分数 {candidate.semantic_score === null || candidate.semantic_score === undefined ? "-" : Number(candidate.semantic_score).toFixed(3)}</span><span>命中窗口 {candidate.retrieved_window_count ?? 0}</span></div><div className="drama-evidence-text">{highlightMatchedUnits(contextText, candidate.match_metrics, String(singleResult?.query_text || ""))}</div>{!evidenceContexts[windowUid]?.context && <button className="ghost-button slim" type="button" disabled={!windowUid || loadingEvidenceWindowUid === windowUid} onClick={() => void loadEvidenceContext(windowUid, String(singleResult?.query_text || queryText))}>{loadingEvidenceWindowUid === windowUid ? "加载完整上下文中..." : "加载更多复核内容"}</button>}</article>; })}</div>}

      {selectedTaskItem && <section className="card-panel drama-batch-semantic-summary span-full">
        <div className="section-heading"><div><h2>当前批量结果的检索状态</h2><p>选中第 {selectedTaskItem.item_order} 条后，可在这里确认本条使用的检索路径和降级信息。</p></div><span className={`semantic-chip semantic-state-${String(selectedTaskPayload.semantic_status || "unknown")}`}>{semanticStatusLabel(selectedTaskPayload.semantic_status)}</span></div>
        <div className="drama-decision-notice"><strong>最终判定：{decisionHeadline(selectedTaskDecision)}</strong><span>{decisionMessage(selectedTaskDecision)}</span><ReviewFeedback decision={selectedTaskDecision} /><StrongCandidateList candidates={selectedTaskDecision.strong_match_candidates} fallbackCandidates={selectedTaskPayload.candidates} /><StrongCandidateList candidates={selectedTaskDecision.content_candidate_options} fallbackCandidates={selectedTaskPayload.candidates} title="待核验内容候选（不代表命中）" /></div>
        <div className="drama-batch-semantic-metrics">
          <div><span>本条耗时</span><strong>{formatDuration(selectedTaskItem.duration_seconds)}</strong></div>
          <div><span>词级候选</span><strong>{selectedTaskPayload.lexical_candidate_count ?? "-"}</strong></div>
          <div><span>语义候选</span><strong>{selectedTaskPayload.semantic_candidate_count ?? "-"}</strong></div>
          <div><span>语义耗时</span><strong>{formatDuration(selectedTaskPayload.semantic_duration_seconds)}</strong></div>
          <div><span>Top1 语义分数</span><strong>{selectedTaskCandidate?.semantic_score === null || selectedTaskCandidate?.semantic_score === undefined ? "-" : Number(selectedTaskCandidate.semantic_score).toFixed(3)}</strong></div>
        </div>
        {selectedTaskCandidate && <div className="drama-batch-source-row"><span>候选来源</span><RetrievalSourceBadges candidate={selectedTaskCandidate} /></div>}
        {selectedTaskPayload.semantic_status === "fallback_lexical_only" && <div className="drama-semantic-notice"><strong>本条已降级</strong><span>{selectedTaskPayload.semantic_error || "语义增强未完成，已保留词级结果。"}</span></div>}
      </section>}

      <section className="card-panel drama-subtitle-tasks span-full">
        <div className="section-heading"><div><h2>短剧批量任务</h2><p>任务与小说批量检测独立保存，结果只包含短剧字幕匹配信息。</p></div><button className="ghost-button slim" type="button" onClick={() => void loadTasks().catch(() => undefined)}><Icon name="refresh" />刷新</button></div>
        <div className="drama-task-layout">
          <div className="list-shell list-shell-table">
            <table className="data-table compact">
              <thead><tr><th>任务</th><th>状态</th><th>进度</th><th>排队反馈</th><th>文件</th></tr></thead>
              <tbody>{tasks.length > 0 ? tasks.map((task) => <tr key={String(task.task_id)} className={selectedTask?.task_id === task.task_id ? "selected" : ""} onClick={() => void loadTasks(String(task.task_id)).catch(() => undefined)}><td>{String(task.task_id).slice(0, 8)}</td><td><span className={`status-pill ${task.status}`}>{task.status}</span></td><td>{task.counts?.completed ?? 0}/{task.counts?.accepted ?? 0}</td><td className="drama-task-queue-feedback">{taskQueueFeedback(task)}</td><td>{task.source_file_name}</td></tr>) : <tr><td colSpan={5}>暂未创建短剧字幕批量任务。</td></tr>}</tbody>
            </table>
          </div>
          <div className="drama-task-results">
            <div className="drama-task-result-head">
              <h3>{selectedTask ? `任务结果 · ${String(selectedTask.task_id).slice(0, 8)}` : "选择左侧任务查看结果"}</h3>
              {selectedTask && <div className="button-row">
                {["queued", "running"].includes(String(selectedTask.status)) && <button className="ghost-button slim" type="button" onClick={() => void handleTaskControl("pause")}>暂停</button>}
                {selectedTask.status === "paused" && <button className="ghost-button warm slim" type="button" onClick={() => void handleTaskControl("resume")}>继续</button>}
                {["queued", "running", "pause_requested"].includes(String(selectedTask.status)) && <button className="ghost-button danger slim" type="button" onClick={() => void handleTaskControl("cancel")}>取消</button>}
                {!["running", "pause_requested", "cancel_requested"].includes(String(selectedTask.status)) && <button className="ghost-button danger slim" type="button" onClick={() => void handleTaskControl("delete")}>删除</button>}
                <button className="outline-button slim" type="button" disabled={isExporting} onClick={() => void handleExport()}>{isExporting ? "导出中..." : "导出复核表"}</button>
              </div>}
            </div>
            {taskItems.length > 0 ? <div className="drama-task-item-list">{taskItems.map((item) => {
              const itemId = Number(item.task_item_id);
              const isSelected = selectedTaskItemId === itemId;
              const evidenceWindowUid = taskItemWindowUid(item);
              const expandedContext = evidenceContexts[evidenceWindowUid]?.context;
              const showExpandedContext = Boolean(expandedContext);
              const evidence = isSelected ? taskItemEvidence(item, showExpandedContext ? String(expandedContext?.text || "") : "") : null;
              const itemDecision = taskItemDecision(item);
              const displayCandidate = taskItemDisplayCandidate(item);
              const isConfirmedMatch = itemDecision.is_confirmed_match === true
                || (itemDecision.status === "matched" && itemDecision.matched === true);
              const itemSummary = itemDecision.content_match_status === "matched" && itemDecision.title_resolution === "ambiguous"
                ? "内容已命中，剧名待确认"
                : isConfirmedMatch && item.matched_book_name
                  ? `命中：${item.matched_book_name} 第 ${item.matched_episode_order} 集`
                  : itemDecision.status === "review_required"
                    ? String(decisionReviewFeedback(itemDecision)?.title || "发现可疑证据，待人工复核")
                    : "未命中当前字幕库";
              return <article key={String(item.task_item_id)} className={isSelected ? "is-selected" : ""} onClick={() => { if (!isSelected) setSelectedTaskItemId(itemId); }}>
                <strong>{item.source_short_drama || item.source_display_title || `第 ${item.item_order} 条`}</strong>
                <span>{languageLabel(item.query_language_code)} → {languageLabel(item.matched_language_code || displayCandidate.language_code)} · {formatDuration(item.duration_seconds)}</span>
                <p>{itemSummary}</p>
                <StrongCandidateList candidates={itemDecision.strong_match_candidates} fallbackCandidates={taskItemPayload(item).candidates} /><StrongCandidateList candidates={itemDecision.content_candidate_options} fallbackCandidates={taskItemPayload(item).candidates} title="待核验内容候选（不代表命中）" />
                <small>{item.evidence_time_start || displayCandidate.evidence?.time_start || "-"} 至 {item.evidence_time_end || displayCandidate.evidence?.time_end || "-"}</small>
                {isSelected && <button className="ghost-button slim drama-item-collapse" type="button" onClick={(event) => { event.stopPropagation(); setSelectedTaskItemId(null); }}>收起本条详情</button>}
                {isSelected && <div className="drama-task-evidence">
                  <div><strong>查询台词</strong><pre>{highlightMatchedUnits(evidence?.queryText || "-", { match_unit: evidence?.matchUnit, shared_trigrams: evidence?.sharedTrigrams }, evidence?.evidenceText || "")}</pre></div>
                  <div className="drama-context-pane"><div className="drama-evidence-heading"><strong>{showExpandedContext ? "扩展复核上下文" : "命中证据"}</strong></div><pre>{highlightMatchedUnits(evidence?.evidenceText || "本条结果未命中可展示的候选证据。", { match_unit: evidence?.matchUnit, shared_trigrams: evidence?.sharedTrigrams }, evidence?.queryText || "")}</pre>{showExpandedContext && <small className="drama-evidence-context-meta">已从命中窗口向前补充 {expandedContext.before_line_count} 行，并向后展示 {expandedContext.after_line_count} 行后续字幕，共 {expandedContext.char_count} 字符。</small>}</div>
                </div>}
                <label className="drama-review-select" onClick={(event) => event.stopPropagation()}>复核状态<select value={String(item.review_status || "pending")} disabled={reviewingItemId === Number(item.task_item_id)} onChange={(event) => void handleReview(Number(item.task_item_id), event.target.value)}><option value="pending">待复核</option><option value="confirmed_high_risk">确认高风险</option><option value="needs_followup">继续跟进</option><option value="false_positive">标记误报</option></select></label>
              </article>;
            })}</div> : <StatusState title="任务结果待生成" description="任务执行后会在这里展示每条台词的命中剧集和时间证据。" icon="tasks" />}
          </div>
        </div>
      </section>
    </div>
  );
}
