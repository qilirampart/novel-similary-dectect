import { useEffect, useId, useMemo, useRef, useState, type DragEvent, type KeyboardEvent } from "react";
import { Link } from "react-router-dom";

import { cancelTask, createCompareTask, deleteTask, deleteTasks, downloadTaskExport, getTaskDetail, listTasks, pauseTask, resumeTask, retryTask, type DetectionMode } from "../api";
import { formatConfidenceLabel, formatDetectionModeLabel, formatReviewLabel, formatSemanticStatusLabel, formatTaskMessage, formatTaskStatusLabel } from "../displayText";
import { Icon } from "../icons";
import { PaginationBar } from "../PaginationBar";
import { StatusState } from "../StatusState";
import { formatTaskEta, formatTaskProgressDetail } from "../taskProgress";
import { buildReviewPath } from "../workflowLinks";

const DEFAULT_CANDIDATE_DISPLAY_SCORE_THRESHOLD = 0.01;
const BATCH_THRESHOLD_STORAGE_KEY = "novel-compare-batch-threshold";
const TASK_PAGE_SIZE = 8;
const TASK_RESULT_PAGE_SIZE = 8;
const TASK_DETAIL_FETCH_LIMIT = 200;
const BULK_DELETABLE_TASK_STATUSES = ["queued", "paused", "failed", "partial_failed", "cancelled", "completed"];

function toPercent(task: Record<string, any>): number {
  const counts = (task.counts ?? {}) as Record<string, number | null>;
  const accepted = Number(counts.accepted ?? 0);
  const completed = Number(counts.completed ?? 0);
  const failed = Number(counts.failed ?? 0);
  if (accepted <= 0) return 0;
  return Math.min(Math.round(((completed + failed) / accepted) * 100), 100);
}

function getProgressLabel(task: Record<string, any>): string {
  const status = String(task.status ?? "");
  const counts = (task.counts ?? {}) as Record<string, number | null>;
  const accepted = Number(counts.accepted ?? 0);
  const completed = Number(counts.completed ?? 0);
  const failed = Number(counts.failed ?? 0);
  const total = Math.max(accepted, completed + failed);
  const progress = total > 0 ? Math.min(Math.round(((completed + failed) / total) * 100), 100) : 0;

  const detail = formatTaskProgressDetail(task);
  const eta = formatTaskEta(task);

  if (status === "queued") return accepted > 0 ? `排队中 · ${detail}` : "排队中";
  if (status === "running") return eta !== "-" ? `执行中 ${progress}% · ${detail} · 剩余 ${eta}` : `执行中 ${progress}% · ${detail}`;
  if (status === "pause_requested") return `暂停中 ${progress}% · ${detail}`;
  if (status === "paused") return accepted > 0 ? `已暂停 · ${detail}` : "已暂停";
  if (status === "cancel_requested") return `取消中 ${progress}% · ${detail}`;
  if (status === "completed") return "已完成 100%";
  if (status === "partial_failed") return `部分失败 ${progress}%`;
  if (status === "failed") return "失败";
  if (status === "cancelled") return "已取消";
  return `${progress}%`;
}

function formatItemDuration(value: unknown): string {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return "-";

  if (seconds < 60) {
    return `${seconds.toFixed(seconds < 10 ? 1 : 0)} 秒`;
  }

  const minutes = Math.floor(seconds / 60);
  const remainSeconds = seconds - minutes * 60;
  if (remainSeconds < 0.1) {
    return `${minutes} 分`;
  }

  return `${minutes} 分 ${remainSeconds.toFixed(remainSeconds < 10 ? 1 : 0)} 秒`;
}

function formatEpisodeLabel(value: unknown): string {
  const text = String(value ?? "").trim();
  if (!text) return "-";
  if (text.startsWith("第") && text.endsWith("集")) return text;
  return `第${text}集`;
}

function clampThreshold(value: number): number {
  if (!Number.isFinite(value)) return DEFAULT_CANDIDATE_DISPLAY_SCORE_THRESHOLD;
  return Math.max(0, Math.min(1, value));
}

function readStoredThreshold(): number {
  if (typeof window === "undefined") return DEFAULT_CANDIDATE_DISPLAY_SCORE_THRESHOLD;
  const raw = window.localStorage.getItem(BATCH_THRESHOLD_STORAGE_KEY);
  return clampThreshold(Number(raw));
}

function isLiveTask(task: Record<string, any> | null | undefined): boolean {
  const status = String(task?.status ?? "");
  return ["queued", "running", "pause_requested", "cancel_requested"].includes(status);
}

export function BatchTasksPage() {
  const fileInputId = useId();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const taskDetailRequestRef = useRef(0);
  const taskListRequestRef = useRef(0);
  const silentTaskDetailInFlightRef = useRef(false);
  const silentTaskListInFlightRef = useRef(false);
  const [rewriteEnabled, setRewriteEnabled] = useState(true);
  const [file, setFile] = useState<File | null>(null);
  const [topK, setTopK] = useState(10);
  const [compareTopK, setCompareTopK] = useState(50);
  const [mergedTopK, setMergedTopK] = useState(20);
  const [taskResultDisplayThreshold, setTaskResultDisplayThreshold] = useState(() => readStoredThreshold());
  const [tasks, setTasks] = useState<Record<string, any>[]>([]);
  const [selectedTaskIds, setSelectedTaskIds] = useState<string[]>([]);
  const [taskPage, setTaskPage] = useState(1);
  const [selectedTaskId, setSelectedTaskId] = useState("");
  const [selectedTask, setSelectedTask] = useState<Record<string, any> | null>(null);
  const [selectedItems, setSelectedItems] = useState<Record<string, any>[]>([]);
  const [selectedItemTotal, setSelectedItemTotal] = useState(0);
  const [selectedItemStats, setSelectedItemStats] = useState({ item_total: 0, high_risk_count: 0, semantic_fallback_count: 0 });
  const [resultPage, setResultPage] = useState(1);
  const [tasksLoading, setTasksLoading] = useState(true);
  const [tasksRefreshing, setTasksRefreshing] = useState(false);
  const [taskDetailLoading, setTaskDetailLoading] = useState(true);
  const [taskDetailRefreshing, setTaskDetailRefreshing] = useState(false);
  const [isCreating, setIsCreating] = useState(false);
  const [isDraggingFile, setIsDraggingFile] = useState(false);
  const [exportingKind, setExportingKind] = useState<"summary" | "review" | null>(null);
  const [isDeletingSelectedTasks, setIsDeletingSelectedTasks] = useState(false);
  const [error, setError] = useState("");
  const mode: DetectionMode = rewriteEnabled ? "rewrite" : "reuse";

  async function getFullTaskDetail(taskId: string) {
    const firstPage = await getTaskDetail(String(taskId), TASK_DETAIL_FETCH_LIMIT, 0);
    const total = Math.max(Number(firstPage.item_total ?? firstPage.items.length ?? 0), 0);
    if (total <= firstPage.items.length || total <= TASK_DETAIL_FETCH_LIMIT) {
      return firstPage;
    }

    const allItems = [...firstPage.items];
    let offset = allItems.length;

    while (offset < total) {
      const nextPage = await getTaskDetail(String(taskId), TASK_DETAIL_FETCH_LIMIT, offset);
      if (!Array.isArray(nextPage.items) || nextPage.items.length === 0) {
        break;
      }
      allItems.push(...nextPage.items);
      offset += nextPage.items.length;
    }

    return {
      ...firstPage,
      items: allItems
    };
  }

  async function loadTaskDetail(taskId: string, options: { resetView?: boolean; silent?: boolean } = {}) {
    const { resetView = true, silent = false } = options;
    if (silent && silentTaskDetailInFlightRef.current) {
      return;
    }
    const requestId = ++taskDetailRequestRef.current;
    if (silent) {
      silentTaskDetailInFlightRef.current = true;
    }
    if (silent) {
      setTaskDetailRefreshing(true);
    } else {
      setTaskDetailLoading(true);
    }
    if (resetView) {
      setSelectedTask(null);
      setSelectedItems([]);
      setSelectedItemTotal(0);
      setSelectedItemStats({ item_total: 0, high_risk_count: 0, semantic_fallback_count: 0 });
      setResultPage(1);
    }
    try {
      const detail = await getFullTaskDetail(String(taskId));
      if (taskDetailRequestRef.current !== requestId) return;
      setSelectedTaskId(String(taskId));
      setSelectedTask(detail.task);
      setSelectedItems(detail.items);
      setSelectedItemTotal(Number(detail.item_total ?? 0));
      setSelectedItemStats({
        item_total: Number(detail.result_stats?.item_total ?? detail.item_total ?? 0),
        high_risk_count: Number(detail.result_stats?.high_risk_count ?? 0),
        semantic_fallback_count: Number(detail.result_stats?.semantic_fallback_count ?? 0)
      });
    } catch (requestError) {
      if (taskDetailRequestRef.current !== requestId) return;
      if (!silent) {
        setError(requestError instanceof Error ? requestError.message : "加载任务详情失败");
      }
    } finally {
      if (silent) {
        silentTaskDetailInFlightRef.current = false;
      }
      if (taskDetailRequestRef.current === requestId) {
        if (silent) {
          setTaskDetailRefreshing(false);
        } else {
          setTaskDetailLoading(false);
        }
      }
    }
  }

  async function loadTasks(preferredTaskId = "", options: { silent?: boolean } = {}) {
    const { silent = false } = options;
    if (silent && silentTaskListInFlightRef.current) {
      return;
    }
    const requestId = ++taskListRequestRef.current;
    if (silent) {
      silentTaskListInFlightRef.current = true;
    }
    if (silent) {
      setTasksRefreshing(true);
    } else {
      setTasksLoading(true);
      setError("");
    }
    try {
      const response = await listTasks(20, 0);
      if (taskListRequestRef.current !== requestId) return;
      setTasks(response.items);
      setTaskPage((current) => {
        const nextPageCount = Math.max(Math.ceil(response.items.length / TASK_PAGE_SIZE), 1);
        return Math.min(current, nextPageCount);
      });

      const preferredId = String(preferredTaskId || selectedTaskId || "");
      const preferredTask = response.items.find((task) => String(task.task_id) === preferredId);
      const nextTaskId = String(preferredTask?.task_id || response.items[0]?.task_id || "");
      if (!nextTaskId) {
        setSelectedTaskId("");
        setSelectedTask(null);
        setSelectedItems([]);
        setSelectedItemTotal(0);
        setSelectedItemStats({ item_total: 0, high_risk_count: 0, semantic_fallback_count: 0 });
        setTaskDetailLoading(false);
        return;
      }

      const shouldResetView = String(selectedTaskId) !== nextTaskId || !selectedTask;
      const shouldSkipSilentDetailRefresh =
        silent &&
        !shouldResetView &&
        isLiveTask(preferredTask);
      if (shouldSkipSilentDetailRefresh) {
        setSelectedTask((current) =>
          current && String(current.task_id) === nextTaskId
            ? { ...current, ...preferredTask }
            : preferredTask ?? current
        );
        setSelectedTaskId(nextTaskId);
        return;
      }
      void loadTaskDetail(nextTaskId, {
        resetView: shouldResetView,
        silent: silent && !shouldResetView
      });
    } catch (requestError) {
      if (taskListRequestRef.current !== requestId) return;
      if (!silent) {
        setError(requestError instanceof Error ? requestError.message : "加载批量任务失败");
      }
    } finally {
      if (silent) {
        silentTaskListInFlightRef.current = false;
      }
      if (taskListRequestRef.current === requestId) {
        if (silent) {
          setTasksRefreshing(false);
        } else {
          setTasksLoading(false);
        }
      }
    }
  }

  useEffect(() => {
    void loadTasks();
  }, []);

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(BATCH_THRESHOLD_STORAGE_KEY, taskResultDisplayThreshold.toFixed(2));
  }, [taskResultDisplayThreshold]);

  useEffect(() => {
    setSelectedTaskIds((current) =>
      current.filter((taskId) => tasks.some((task) => String(task.task_id) === taskId))
    );
  }, [tasks]);

  useEffect(() => {
    const hasLiveTask = tasks.some((task) => isLiveTask(task)) || isLiveTask(selectedTask);
    if (!hasLiveTask) return;

    const timer = window.setInterval(() => {
      void loadTasks(selectedTaskId, { silent: true });
    }, 4000);

    return () => window.clearInterval(timer);
  }, [tasks, selectedTask, selectedTaskId]);

  useEffect(() => {
    setResultPage(1);
  }, [selectedTaskId, taskResultDisplayThreshold]);

  function clearSelectedFile() {
    setFile(null);
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  }

  function openFilePicker() {
    const input = fileInputRef.current as (HTMLInputElement & { showPicker?: () => void }) | null;
    if (!input) return;
    if (typeof input.showPicker === "function") {
      input.showPicker();
      return;
    }
    input.click();
  }

  function handleDropzoneKeyDown(event: KeyboardEvent<HTMLLabelElement>) {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    openFilePicker();
  }

  function handleFileDrop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    event.stopPropagation();
    setIsDraggingFile(false);
    const droppedFile = event.dataTransfer.files?.[0] ?? null;
    if (droppedFile) {
      setFile(droppedFile);
    }
  }

  async function handleCreateTask() {
    if (!file) {
      setError("请先选择输入文件。");
      return;
    }

    setIsCreating(true);
    setError("");
    try {
      const response = await createCompareTask({
        file,
        detectionMode: mode,
        topK,
        compareTopK,
        mergedTopK
      });
      clearSelectedFile();
      await loadTasks(String(response.task_id));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "创建任务失败");
    } finally {
      setIsCreating(false);
    }
  }

  async function handleSelectTask(taskId: string) {
    setSelectedTaskId(taskId);
    void loadTaskDetail(taskId);
  }

  function handleResultPageChange(page: number) {
    setResultPage(page);
  }

  async function handleCancelTask(taskId: string) {
    setError("");
    try {
      await cancelTask(taskId);
      await loadTasks(taskId);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "取消任务失败");
    }
  }

  async function handleRetryTask(taskId: string) {
    setError("");
    try {
      const response = await retryTask(taskId);
      await loadTasks(String(response.task_id));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "重试任务失败");
    }
  }

  async function handlePauseTask(taskId: string) {
    setError("");
    try {
      await pauseTask(taskId);
      await loadTasks(taskId);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "暂停任务失败");
    }
  }

  async function handleResumeTask(taskId: string) {
    setError("");
    try {
      await resumeTask(taskId);
      await loadTasks(taskId);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "继续任务失败");
    }
  }

  async function handleDeleteTask(taskId: string) {
    setError("");
    try {
      await deleteTask(taskId);
      await loadTasks();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "删除任务失败");
    }
  }

  function handleToggleTaskSelection(taskId: string) {
    setSelectedTaskIds((current) =>
      current.includes(taskId) ? current.filter((currentTaskId) => currentTaskId !== taskId) : [...current, taskId]
    );
  }

  function handleToggleVisibleTaskSelection() {
    const visibleIds = visibleTasks.map((task) => String(task.task_id));
    const allVisibleSelected = visibleIds.length > 0 && visibleIds.every((taskId) => selectedTaskIds.includes(taskId));
    setSelectedTaskIds((current) =>
      allVisibleSelected
        ? current.filter((taskId) => !visibleIds.includes(taskId))
        : Array.from(new Set([...current, ...visibleIds]))
    );
  }

  async function handleDeleteSelectedTasks() {
    const deletableIds = selectedTaskIds.filter((taskId) => {
      const task = tasks.find((item) => String(item.task_id) === taskId);
      return task && BULK_DELETABLE_TASK_STATUSES.includes(String(task.status || ""));
    });
    if (deletableIds.length <= 0) {
      setError("当前选中的任务没有可删除项。运行中、取消中和暂停中的任务暂不支持批量删除。");
      return;
    }

    const skippedCount = selectedTaskIds.length - deletableIds.length;
    const confirmed = window.confirm(
      skippedCount > 0
        ? `将删除 ${deletableIds.length} 个可删除任务，另有 ${skippedCount} 个任务状态不支持删除。是否继续？`
        : `确认删除选中的 ${deletableIds.length} 个任务吗？`
    );
    if (!confirmed) return;

    setIsDeletingSelectedTasks(true);
    setError("");
    try {
      await deleteTasks(deletableIds);
      setSelectedTaskIds((current) => current.filter((taskId) => !deletableIds.includes(taskId)));
      await loadTasks(selectedTaskId);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "批量删除任务失败");
    } finally {
      setIsDeletingSelectedTasks(false);
    }
  }

  async function handleExport(kind: "summary" | "review") {
    if (!selectedTask) return;
    setError("");
    setExportingKind(kind);
    try {
      await downloadTaskExport(String(selectedTask.task_id), kind);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "瀵煎嚭鏂囦欢澶辫触");
    } finally {
      setExportingKind(null);
    }
  }

  const selectedHighRisk = Number(selectedItemStats.high_risk_count ?? 0);
  const semanticFallbackCount = Number(selectedItemStats.semantic_fallback_count ?? 0);
  const taskUsesRewriteDetection = String(selectedTask?.detection_mode ?? "") === "rewrite";
  const hasSemanticFallback = taskUsesRewriteDetection && semanticFallbackCount > 0;
  const selectedTaskReviewLink = selectedTask ? buildReviewPath({ taskId: selectedTask.task_id }) : "";
  const hasSummaryExport = Boolean(selectedTask?.summary_export_path);
  const hasReviewExport = Boolean(selectedTask?.review_export_path);
  const isSilentRefreshing = tasksRefreshing || taskDetailRefreshing;
  const activeResultDisplayThreshold = clampThreshold(taskResultDisplayThreshold);
  const taskPageCount = Math.max(Math.ceil(tasks.length / TASK_PAGE_SIZE), 1);
  const currentTaskPage = Math.min(taskPage, taskPageCount);
  const visibleTasks = useMemo(
    () => tasks.slice((currentTaskPage - 1) * TASK_PAGE_SIZE, currentTaskPage * TASK_PAGE_SIZE),
    [currentTaskPage, tasks]
  );
  const allVisibleSelected =
    visibleTasks.length > 0 && visibleTasks.every((task) => selectedTaskIds.includes(String(task.task_id)));
  const filteredSelectedItems = useMemo(
    () =>
      selectedItems.filter((item) => {
        const score = Number(item.top1_fine_score);
        return !Number.isFinite(score) || score >= activeResultDisplayThreshold;
      }),
    [activeResultDisplayThreshold, selectedItems]
  );
  const filteredResultTotal = filteredSelectedItems.length;
  const loadedResultTotal = selectedItems.length;
  const resultPageCount = Math.max(Math.ceil(filteredResultTotal / TASK_RESULT_PAGE_SIZE), 1);
  const currentResultPage = Math.min(resultPage, resultPageCount);
  const visibleSelectedItems = useMemo(
    () =>
      filteredSelectedItems.slice(
        (currentResultPage - 1) * TASK_RESULT_PAGE_SIZE,
        currentResultPage * TASK_RESULT_PAGE_SIZE
      ),
    [currentResultPage, filteredSelectedItems]
  );

  return (
    <div className="page-grid batch-page-grid">
      <section className="single-page-heading">
        <div>
          <h1>批量任务</h1>
          <p>集中创建、查看、取消和重试批量比对任务。</p>
        </div>
      </section>

      <div className="batch-reference-layout span-full">
        <section className="batch-reference-left">
          <article className="card-panel batch-create-panel">
            <div className="section-heading compact-bottom">
              <div>
                <h2>创建任务</h2>
              </div>
            </div>

            <input
              id={fileInputId}
              ref={fileInputRef}
              className="native-file-input"
              type="file"
              accept=".txt,.csv,.tsv,.xlsx"
              onChange={(event) => setFile(event.target.files?.[0] ?? null)}
            />
            <label
              htmlFor={fileInputId}
              className={`upload-dropzone batch-dropzone${isDraggingFile ? " is-dragging" : ""}${file ? " has-file" : ""}`}
              role="button"
              tabIndex={0}
              aria-label="选择批量任务输入文件"
              onKeyDown={handleDropzoneKeyDown}
              onDragEnter={(event) => {
                event.preventDefault();
                setIsDraggingFile(true);
              }}
              onDragOver={(event) => {
                event.preventDefault();
                setIsDraggingFile(true);
              }}
              onDragLeave={(event) => {
                event.preventDefault();
                setIsDraggingFile(false);
              }}
              onDrop={handleFileDrop}
            >
              <div className="upload-graphic">
                <div className="upload-backdrop" />
                <div className="upload-fg"><Icon name="upload" /></div>
              </div>
              <strong>{file ? file.name : "拖拽文件到这里，或点击选择文件"}</strong>
              <p>支持：txt、csv、tsv、xlsx</p>
            </label>

            <div className="file-card">
              <span className="panel-label">已选文件</span>
              <div className="file-leading">
                <span className="file-badge green">{file ? file.name.slice(0, 1).toUpperCase() : "-"}</span>
                <div>
                  <strong>{file?.name ?? "尚未选择文件"}</strong>
                  <p>{file ? `${(file.size / 1024).toFixed(1)} KB` : "待选择"}</p>
                </div>
                <span className={`status-pill ${file ? "completed" : "queued"}`}>{file ? "已就绪" : "空闲"}</span>
              </div>
            </div>

            <div className="toggle-panel">
              <span className="panel-label">检测范围</span>
              <div className="batch-mode-row">
                <button className="batch-radio active" type="button" disabled>
                  <span className="batch-radio-dot" />
                  普通检测
                </button>
                <button className={`batch-radio${rewriteEnabled ? " active" : ""}`} type="button" onClick={() => setRewriteEnabled((value) => !value)}>
                  <span className="batch-radio-dot" />
                  {rewriteEnabled ? "已开启改写检测" : "开启改写检测"}
                </button>
              </div>
            </div>

            <div className="toggle-panel">
              <span className="panel-label">参数配置</span>
              <div className="batch-parameter-grid batch-parameter-grid--form">
                <label className="batch-parameter-box">
                  <span>top_k</span>
                  <input type="number" min={1} max={20} value={topK} onChange={(event) => setTopK(Number(event.target.value) || 10)} />
                </label>
                <label className="batch-parameter-box">
                  <span>compare_top_k</span>
                  <input type="number" min={1} max={100} value={compareTopK} onChange={(event) => setCompareTopK(Number(event.target.value) || 50)} />
                </label>
                <label className="batch-parameter-box">
                  <span>merged_top_k</span>
                  <input type="number" min={1} max={200} value={mergedTopK} onChange={(event) => setMergedTopK(Number(event.target.value) || 20)} />
                </label>
              </div>
            </div>

            <div className="button-row batch-bottom-actions">
              <button className="primary-button wide" type="button" onClick={handleCreateTask} disabled={isCreating}>
                <Icon name="layers" />
                {isCreating ? "创建中..." : "创建任务"}
              </button>
              <button className="ghost-button wide" type="button" onClick={clearSelectedFile}>清空</button>
            </div>

            {error && (
              <StatusState
                title="批量任务操作失败"
                description={error}
                tone="error"
                variant="inline"
                icon="warning"
              />
            )}

            {rewriteEnabled && (
              <StatusState
                title="批量任务默认开启改写检测"
                description="如果语义召回不可用，系统会自动降级为仅词法召回，并在任务结果中提示回退情况。"
                tone="info"
                variant="inline"
                icon="review"
              />
            )}
          </article>
        </section>

        <section className="batch-reference-right">
          <article className="card-panel batch-queue-table-panel">
            <div className="section-heading compact-bottom">
              <div>
                <h2>任务队列</h2>
              </div>
              <button
                className="chip-button slim"
                type="button"
                onClick={() => void loadTasks(selectedTaskId, { silent: true })}
                disabled={isSilentRefreshing}
              >
                <Icon name="refresh" />
                {isSilentRefreshing ? "同步中" : "刷新"}
              </button>
            </div>

            <div className="batch-queue-toolbar">
              <div className="batch-queue-toolbar-meta">
                <div className="filter-chip">已选任务：{selectedTaskIds.length} 个</div>
                <div className="filter-chip">当前页：{visibleTasks.length} 个</div>
              </div>
              <div className="batch-queue-toolbar-actions">
                <button
                  className="ghost-button danger slim"
                  type="button"
                  disabled={selectedTaskIds.length === 0 || isDeletingSelectedTasks}
                  onClick={() => void handleDeleteSelectedTasks()}
                >
                  {isDeletingSelectedTasks ? "删除中..." : "删除选中任务"}
                </button>
              </div>
            </div>

            <div className="list-shell list-shell-table batch-queue-table-wrap">
              <table className="data-table compact batch-queue-table">
                <thead>
                  <tr>
                    <th className="batch-queue-select-cell">
                      <input
                        type="checkbox"
                        checked={allVisibleSelected}
                        onChange={handleToggleVisibleTaskSelection}
                        aria-label="选择当前页全部任务"
                      />
                    </th>
                    <th>任务 ID</th>
                    <th>状态</th>
                    <th>进度</th>
                    <th>失败数</th>
                    <th>风险数</th>
                    <th>创建时间</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {tasksLoading ? (
                    <tr>
                      <td colSpan={8}>
                        <StatusState title="正在加载任务列表" description="正在同步最近批量任务的执行状态和进度。" tone="info" icon="queue" />
                      </td>
                    </tr>
                  ) : tasks.length > 0 ? (
                    visibleTasks.map((task) => {
                      const progress = toPercent(task);
                      const taskId = String(task.task_id);
                      const selected = selectedTaskId === taskId;
                      const checked = selectedTaskIds.includes(taskId);

                      return (
                        <tr
                          key={taskId}
                          className={selected ? "selected" : ""}
                          onClick={() => void handleSelectTask(taskId)}
                        >
                          <td
                            className="batch-queue-select-cell"
                            onClick={(event) => event.stopPropagation()}
                          >
                            <input
                              type="checkbox"
                              checked={checked}
                              onChange={() => handleToggleTaskSelection(taskId)}
                              aria-label={`选择任务 ${taskId}`}
                            />
                          </td>
                          <td className="batch-queue-id-cell">
                            {selected && <span className="batch-selected-dot" />}
                            {taskId}
                          </td>
                          <td><span className={`status-pill ${task.status}`}>{formatTaskStatusLabel(String(task.status ?? ""))}</span></td>
                          <td>
                            <div className="progress-inline">
                              <div className="progress-bar"><span style={{ width: `${progress}%` }} /></div>
                              <span>{getProgressLabel(task)}</span>
                            </div>
                          </td>
                          <td>{task.counts?.failed ?? 0}</td>
                          <td>{selected ? selectedHighRisk : "-"}</td>
                          <td>{task.created_at || "-"}</td>
                          <td className="dashboard-actions-cell">
                            <Link
                              className="text-button"
                              to={buildReviewPath({ taskId })}
                              onClick={(event) => event.stopPropagation()}
                            >
                              复核
                            </Link>
                          </td>
                        </tr>
                      );
                    })
                  ) : (
                    <tr>
                      <td colSpan={8}>
                        <StatusState title="当前还没有批量任务" description="先上传一个批量文件并创建任务，这里才会展示任务队列。" icon="layers" />
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>

            <div className="batch-queue-footer">
              <span>共 {tasks.length} 个任务</span>
              <PaginationBar
                page={currentTaskPage}
                pageCount={taskPageCount}
                total={tasks.length}
                pageSize={TASK_PAGE_SIZE}
                itemLabel="任务"
                onChange={setTaskPage}
              />
            </div>
          </article>

          <article className="dark-card-panel batch-current-summary">
            <div className="batch-current-head">
              <div>
                <span className="batch-current-kicker">当前任务</span>
                <h2>任务概览</h2>
              </div>
              {selectedTaskReviewLink && (
                <Link className="outline-button slim batch-summary-review-button" to={selectedTaskReviewLink}>
                  打开复核
                </Link>
              )}
            </div>
            {taskDetailLoading && !selectedTask ? (
              <StatusState title="正在加载任务详情" description="正在同步当前任务的参数、统计和导出状态。" tone="info" variant="dark" icon="tasks" />
            ) : selectedTask ? (
              <>
                <div className="batch-current-grid">
                  <div className="batch-current-major">
                    <span>任务 ID</span>
                    <strong>{selectedTask.task_id}</strong>
                  </div>
                  <div>
                    <span>状态</span>
                    <strong>{formatTaskStatusLabel(String(selectedTask.status ?? ""))}</strong>
                  </div>
                  <div>
                    <span>模式</span>
                    <strong>{formatDetectionModeLabel(String(selectedTask.detection_mode ?? ""))}</strong>
                  </div>
                  <div>
                    <span>接收数</span>
                    <strong>{selectedTask.counts?.accepted ?? 0}</strong>
                  </div>
                  <div>
                    <span>完成数</span>
                    <strong>{selectedTask.counts?.completed ?? 0}</strong>
                  </div>
                  <div>
                    <span>失败数</span>
                    <strong>{selectedTask.counts?.failed ?? 0}</strong>
                  </div>
                  <div>
                    <span>当前进度</span>
                    <strong>{formatTaskProgressDetail(selectedTask)}</strong>
                  </div>
                  <div>
                    <span>预计剩余</span>
                    <strong>{isLiveTask(selectedTask) ? formatTaskEta(selectedTask) : "-"}</strong>
                  </div>
                  <div>
                    <span>高风险</span>
                    <strong>{selectedHighRisk}</strong>
                  </div>
                  <div>
                    <span>回退结果</span>
                    <strong>{semanticFallbackCount}</strong>
                  </div>
                  <div>
                    <span>导出</span>
                    <strong>{selectedTask.summary_export_path ? "已就绪" : "待生成"}</strong>
                  </div>
                  <div>
                    <span>当前展示</span>
                    <strong>{filteredResultTotal}</strong>
                  </div>
                  <div>
                    <span>状态说明</span>
                    <strong>{formatTaskMessage(String(selectedTask.status_message || ""))}</strong>
                  </div>
                </div>
                {hasSemanticFallback && (
                  <StatusState
                    title="当前任务出现语义回退"
                    description={`本任务有 ${semanticFallbackCount} 条结果已自动降级为仅词法召回，结果仍可复核，但未使用语义召回能力。`}
                    tone="warning"
                    variant="inline"
                    icon="warning"
                  />
                )}
                {(["queued", "running", "pause_requested", "cancel_requested", "paused"].includes(String(selectedTask.status)) ||
                  ["failed", "partial_failed", "cancelled", "completed"].includes(String(selectedTask.status))) && (
                  <div className="button-row batch-summary-actions">
                    {["queued", "running"].includes(String(selectedTask.status)) && (
                      <button className="ghost-button" type="button" onClick={() => void handlePauseTask(String(selectedTask.task_id))}>暂停任务</button>
                    )}
                    {["paused", "pause_requested"].includes(String(selectedTask.status)) && (
                      <button className="ghost-button warm" type="button" onClick={() => void handleResumeTask(String(selectedTask.task_id))}>继续任务</button>
                    )}
                    {["queued", "running", "pause_requested", "cancel_requested"].includes(String(selectedTask.status)) && (
                      <button className="ghost-button danger" type="button" onClick={() => void handleCancelTask(String(selectedTask.task_id))}>取消任务</button>
                    )}
                    {["failed", "partial_failed", "cancelled"].includes(String(selectedTask.status)) && (
                      <button className="ghost-button warm" type="button" onClick={() => void handleRetryTask(String(selectedTask.task_id))}>重试任务</button>
                    )}
                    {["queued", "paused", "failed", "partial_failed", "cancelled", "completed"].includes(String(selectedTask.status)) && (
                      <button className="ghost-button danger" type="button" onClick={() => void handleDeleteTask(String(selectedTask.task_id))}>删除任务</button>
                    )}
                  </div>
                )}
              </>
            ) : (
              <StatusState title="尚未选择任务" description="先从上方任务队列中选中一条记录，再查看当前任务概览。" variant="dark" icon="tasks" />
            )}
          </article>

          <article className="card-panel batch-results-panel">
            <div className="filter-bar batch-results-filters">
              <div className="batch-results-title-block">
                <h2>当前任务结果</h2>
                <p>查看当前任务输出的结果明细，并直接进入复核。</p>
              </div>
              <div className="filter-chip">结果数 {selectedItemTotal}</div>
              <div className="filter-chip">当前展示 {filteredResultTotal}</div>
              <div className="filter-chip">高风险 {selectedHighRisk}</div>
              <div className="filter-chip">回退 {semanticFallbackCount}</div>
              <div className="filter-chip">已加载 {loadedResultTotal}</div>
              <label className="batch-results-threshold-field">
                <span>展示阈值</span>
                <input
                  type="number"
                  min={0}
                  max={1}
                  step={0.01}
                  value={taskResultDisplayThreshold}
                  onChange={(event) => setTaskResultDisplayThreshold(clampThreshold(Number(event.target.value)))}
                />
              </label>
              {hasSummaryExport ? (
                <button className="outline-button slim" type="button" disabled={exportingKind !== null} onClick={() => void handleExport("summary")}>{exportingKind === "summary" ? "导出中..." : "摘要 CSV"}</button>
              ) : (
                <span className="outline-button slim disabled" aria-disabled="true">摘要 CSV</span>
              )}
              {hasReviewExport ? (
                <button className="outline-button slim" type="button" disabled={exportingKind !== null} onClick={() => void handleExport("review")}>{exportingKind === "review" ? "导出中..." : "复核 CSV"}</button>
              ) : (
                <span className="outline-button slim disabled" aria-disabled="true">复核 CSV</span>
              )}
            </div>

            {taskDetailLoading && !selectedTask ? (
              <StatusState title="正在加载任务结果" description="正在同步当前任务输出的结果明细和复核入口。" tone="info" icon="doc" />
            ) : (
              <div className="batch-results-table-wrap">
              <table className="data-table compact batch-results-table">
                <thead>
                  <tr>
                    <th>序号</th>
                    <th>短剧</th>
                    <th>集数</th>
                    <th>作者</th>
                    <th>查询文本</th>
                    <th>Top1 书名</th>
                    <th>章节</th>
                    <th>风险标签</th>
                    <th>置信标签</th>
                    <th>精排分数</th>
                    <th>语义状态</th>
                    <th>结果状态</th>
                    <th>复核</th>
                  </tr>
                </thead>
                <tbody>
                  {visibleSelectedItems.length > 0 ? (
                    visibleSelectedItems.map((item) => (
                      <tr key={String(item.result_id)}>
                        <td>{item.item_order}</td>
                        <td>{item.source_short_drama || "-"}</td>
                        <td>{formatEpisodeLabel(item.source_episode)}</td>
                        <td>{item.source_author || "-"}</td>
                        <td>
                          <div className="batch-result-query-cell">
                            <span>{item.query_text_preview}</span>
                            <small>耗时：{formatItemDuration(item.duration_seconds)}</small>
                          </div>
                        </td>
                        <td>{item.top1_book_name || "-"}</td>
                        <td>{item.top1_chapter_name || "-"}</td>
                        <td><span className="soft-tag">{formatReviewLabel(String(item.top1_review_label || ""))}</span></td>
                        <td><span className="soft-tag muted">{formatConfidenceLabel(String(item.top1_confidence_label || ""))}</span></td>
                        <td>
                          <div className="score-with-bar">
                            <span>{item.top1_fine_score === null ? "-" : Number(item.top1_fine_score).toFixed(2)}</span>
                            <div className="mini-bar"><span style={{ width: `${Math.min((Number(item.top1_fine_score) || 0) * 100, 100)}%` }} /></div>
                          </div>
                        </td>
                        <td><span className="semantic-chip">{formatSemanticStatusLabel(String(item.semantic_status || ""))}</span></td>
                        <td><span className={`status-pill ${item.status}`}>{formatTaskStatusLabel(String(item.status || ""))}</span></td>
                        <td>
                          <Link
                            className="text-button"
                            to={buildReviewPath({
                              taskId: item.task_id,
                              resultId: item.result_id,
                              q: item.query_text_preview || item.top1_book_name || item.top1_chapter_name
                            })}
                          >
                            去复核
                          </Link>
                        </td>
                      </tr>
                    ))
                  ) : (
                    <tr>
                      <td colSpan={13}>
                        <StatusState
                          title="当前阈值下没有可显示结果"
                          description="可以调低展示阈值，查看更多当前任务结果。"
                          tone="info"
                          icon="filter"
                        />
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
              </div>
            )}

            {!selectedTask && !taskDetailLoading && (
              <StatusState title="尚未选择任务结果" description="先从任务队列中选中一条任务，这里才会显示对应结果。" icon="search" />
            )}
            {selectedTask && !taskDetailLoading && (
              <PaginationBar
                page={currentResultPage}
                pageCount={resultPageCount}
                total={filteredResultTotal}
                pageSize={TASK_RESULT_PAGE_SIZE}
                itemLabel="结果"
                onChange={handleResultPageChange}
              />
            )}
          </article>
        </section>
      </div>
    </div>
  );
}
