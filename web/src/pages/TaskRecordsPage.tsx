import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { cancelTask, downloadTaskExport, getTaskDetail, listTasks, retryTask } from "../api";
import { formatDetectionModeLabel, formatTaskMessage, formatTaskStatusLabel, formatTaskTypeLabel } from "../displayText";
import { PaginationBar } from "../PaginationBar";
import { StatusState } from "../StatusState";
import { buildReviewPath } from "../workflowLinks";

const CANCELABLE_STATUSES = ["queued", "running", "cancel_requested"];
const RETRYABLE_STATUSES = ["failed", "partial_failed", "cancelled"];
const TASK_RECORD_PAGE_SIZE = 8;

function countTasksByStatuses(tasks: Record<string, any>[], statuses: string[]) {
  return tasks.filter((item) => statuses.includes(String(item.status))).length;
}

export function TaskRecordsPage() {
  const [tasks, setTasks] = useState<Record<string, any>[]>([]);
  const [taskPage, setTaskPage] = useState(1);
  const [selectedTaskId, setSelectedTaskId] = useState("");
  const [selectedTask, setSelectedTask] = useState<Record<string, any> | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [exportingKind, setExportingKind] = useState<"summary" | "review" | null>(null);
  const [error, setError] = useState("");

  async function loadTasks(preferredTaskId = "") {
    setIsLoading(true);
    const response = await listTasks(50, 0);
    setTasks(response.items);
    setTaskPage((current) => {
      const nextPageCount = Math.max(Math.ceil(response.items.length / TASK_RECORD_PAGE_SIZE), 1);
      return Math.min(current, nextPageCount);
    });

    const nextTaskId = preferredTaskId || selectedTaskId || response.items[0]?.task_id || "";
    if (!nextTaskId) {
      setSelectedTaskId("");
      setSelectedTask(null);
      setIsLoading(false);
      return;
    }

    const detail = await getTaskDetail(String(nextTaskId), 20, 0);
    setSelectedTaskId(String(nextTaskId));
    setSelectedTask(detail.task);
    setIsLoading(false);
  }

  useEffect(() => {
    async function load() {
      try {
        setError("");
        await loadTasks();
    } catch (requestError) {
        setError(requestError instanceof Error ? requestError.message : "加载任务记录失败");
        setIsLoading(false);
      }
    }

    void load();
  }, []);

  async function handleSelect(taskId: string) {
    setSelectedTaskId(taskId);
    setIsLoading(true);
    try {
      const detail = await getTaskDetail(taskId, 20, 0);
      setSelectedTask(detail.task);
      setError("");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "加载任务详情失败");
    } finally {
      setIsLoading(false);
    }
  }

  async function handleRefresh() {
    try {
      setError("");
      await loadTasks(selectedTaskId);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "刷新任务记录失败");
      setIsLoading(false);
    }
  }

  async function handleCancelTask(taskId: string) {
    try {
      setError("");
      await cancelTask(taskId);
      await loadTasks(taskId);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "取消任务失败");
      setIsLoading(false);
    }
  }

  async function handleRetryTask(taskId: string) {
    try {
      setError("");
      const response = await retryTask(taskId);
      await loadTasks(String(response.task_id));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "重试任务失败");
      setIsLoading(false);
    }
  }

  async function handleExport(kind: "summary" | "review") {
    if (!selectedTask) return;
    setError("");
    setExportingKind(kind);
    try {
      await downloadTaskExport(String(selectedTask.task_id), kind);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "导出文件失败");
    } finally {
      setExportingKind(null);
    }
  }

  const stats = useMemo(() => {
    const total = tasks.length;
    const recent = tasks.filter((item) => String(item.created_at).startsWith("2026-05")).length;
    const failed = countTasksByStatuses(tasks, ["failed", "partial_failed", "cancelled"]);
    const exported = tasks.filter((item) => item.summary_export_path).length;

    return [
      ["任务总数", String(total), "blue"],
      ["本月新增", String(recent), "cyan"],
      ["待关注", String(failed), "orange"],
      ["导出就绪", String(exported), "green"]
    ] as const;
  }, [tasks]);

  const selectedTaskStatus = String(selectedTask?.status ?? "");
  const canCancelTask = CANCELABLE_STATUSES.includes(selectedTaskStatus);
  const canRetryTask = RETRYABLE_STATUSES.includes(selectedTaskStatus);
  const hasSummaryExport = Boolean(selectedTask?.summary_export_path);
  const hasReviewExport = Boolean(selectedTask?.review_export_path);
  const taskPageCount = Math.max(Math.ceil(tasks.length / TASK_RECORD_PAGE_SIZE), 1);
  const currentTaskPage = Math.min(taskPage, taskPageCount);
  const visibleTasks = useMemo(
    () => tasks.slice((currentTaskPage - 1) * TASK_RECORD_PAGE_SIZE, currentTaskPage * TASK_RECORD_PAGE_SIZE),
    [currentTaskPage, tasks]
  );

  return (
    <div className="page-grid records-page-grid">
      <section className="page-heading">
        <div>
          <span className="eyebrow">任务记录</span>
          <h1>任务历史</h1>
          <p>查看批量任务历史、生命周期状态，并从当前快照继续处理。</p>
        </div>
        <div className="toolbar-compact">
          <button className="outline-button" type="button" onClick={() => void handleRefresh()}>
            刷新
          </button>
        </div>
      </section>

      <section className="stats-grid">
        {stats.map(([label, value, tone]) => (
          <article key={label} className={`stat-card tone-${tone}`}>
            <div className="stat-body">
              <span className="stat-label">{label}</span>
              <strong className="stat-value">{value}</strong>
            </div>
          </article>
        ))}
      </section>

      {error && (
        <StatusState
          className="span-full"
          title="任务记录操作失败"
          description={error}
          tone="error"
          variant="inline"
          icon="warning"
        />
      )}

      <section className="card-panel span-full records-shell">
        <div className="filter-bar">
          <div className="search-field">比对任务库中的最近 50 个任务</div>
          <div className="filter-chip">当前选中：{selectedTaskId || "-"}</div>
          <div className="filter-chip">运行中：{countTasksByStatuses(tasks, ["queued", "running", "cancel_requested"])}</div>
          <div className="filter-chip">可重试：{countTasksByStatuses(tasks, RETRYABLE_STATUSES)}</div>
        </div>

        <div className="records-layout">
          <div className="records-table-wrap">
            <table className="data-table compact">
              <thead>
                <tr>
                  <th>任务 ID</th>
                  <th>类型</th>
                  <th>源文件</th>
                  <th>模式</th>
                  <th>创建人</th>
                  <th>创建时间</th>
                  <th>完成时间</th>
                  <th>状态</th>
                  <th>接收数</th>
                  <th>失败数</th>
                  <th>导出</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {visibleTasks.map((item) => {
                  const taskId = String(item.task_id);
                  const isSelected = selectedTaskId === taskId;

                  return (
                    <tr key={taskId} className={isSelected ? "selected" : ""} onClick={() => void handleSelect(taskId)}>
                      <td>{taskId}</td>
                      <td>{formatTaskTypeLabel(String(item.task_type ?? ""))}</td>
                      <td>{item.source_file_name}</td>
                      <td>{formatDetectionModeLabel(String(item.detection_mode ?? ""))}</td>
                      <td>{item.created_by || "-"}</td>
                      <td>{item.created_at || "-"}</td>
                      <td>{item.finished_at || "-"}</td>
                      <td><span className={`status-pill ${item.status}`}>{formatTaskStatusLabel(String(item.status ?? ""))}</span></td>
                      <td>{item.counts?.accepted ?? "-"}</td>
                      <td>{item.counts?.failed ?? 0}</td>
                      <td>{item.summary_export_path ? "已就绪" : "-"}</td>
                      <td className="dashboard-actions-cell">
                        <Link className="text-button" to={buildReviewPath({ taskId })}>
                          复核
                        </Link>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>

            {isLoading && (
              <StatusState title="正在加载任务快照" description="正在同步任务列表和当前选中任务的最新状态。" tone="info" variant="inline" icon="refresh" />
            )}
            <PaginationBar
              page={currentTaskPage}
              pageCount={taskPageCount}
              total={tasks.length}
              pageSize={TASK_RECORD_PAGE_SIZE}
              itemLabel="任务"
              onChange={setTaskPage}
            />
          </div>

          <aside className="card-panel detail-drawer">
            <div className="section-heading">
              <div>
                <h2>任务详情</h2>
                <p>查看当前任务的生命周期、执行参数和导出可用性。</p>
              </div>
            </div>

            {selectedTask ? (
              <>
                <div className="detail-list">
                  <div><span>任务 ID</span><strong>{selectedTask.task_id}</strong></div>
                  <div><span>状态</span><strong>{formatTaskStatusLabel(String(selectedTask.status ?? ""))}</strong></div>
                  <div><span>模式</span><strong>{formatDetectionModeLabel(String(selectedTask.detection_mode ?? ""))}</strong></div>
                  <div><span>创建人</span><strong>{selectedTask.created_by || "-"}</strong></div>
                  <div><span>创建时间</span><strong>{selectedTask.created_at || "-"}</strong></div>
                  <div><span>完成时间</span><strong>{selectedTask.finished_at || "-"}</strong></div>
                  <div><span>接收数</span><strong>{selectedTask.counts?.accepted ?? 0}</strong></div>
                  <div><span>完成数</span><strong>{selectedTask.counts?.completed ?? 0}</strong></div>
                  <div><span>失败数</span><strong>{selectedTask.counts?.failed ?? 0}</strong></div>
                  <div><span>top_k</span><strong>{selectedTask.params?.top_k ?? "-"}</strong></div>
                  <div><span>compare_top_k</span><strong>{selectedTask.params?.compare_top_k ?? "-"}</strong></div>
                  <div><span>merged_top_k</span><strong>{selectedTask.params?.merged_top_k ?? "-"}</strong></div>
                  <div><span>候选展示阈值</span><strong>{selectedTask.params?.candidate_display_score_threshold ?? "-"}</strong></div>
                  <div><span>状态说明</span><strong>{formatTaskMessage(String(selectedTask.status_message || ""))}</strong></div>
                </div>

                <div className="drawer-actions">
                  {canCancelTask && (
                    <button className="ghost-button danger" type="button" onClick={() => void handleCancelTask(String(selectedTask.task_id))}>
                      取消任务
                    </button>
                  )}
                  {canRetryTask && (
                    <button className="ghost-button warm" type="button" onClick={() => void handleRetryTask(String(selectedTask.task_id))}>
                      重试任务
                    </button>
                  )}
                  {hasSummaryExport ? (
                    <button className="outline-button" type="button" disabled={exportingKind !== null} onClick={() => void handleExport("summary") }>
                      {exportingKind === "summary" ? "导出中..." : "摘要 CSV"}
                    </button>
                  ) : (
                    <span className="outline-button disabled" aria-disabled="true">
                      摘要 CSV
                    </span>
                  )}
                  {hasReviewExport ? (
                    <button className="primary-button" type="button" disabled={exportingKind !== null} onClick={() => void handleExport("review") }>
                      {exportingKind === "review" ? "导出中..." : "复核 CSV"}
                    </button>
                  ) : (
                    <span className="primary-button disabled" aria-disabled="true">
                      复核 CSV
                    </span>
                  )}
                </div>
              </>
            ) : (
              <StatusState title="尚未选择任务" description="请先从左侧任务列表中选中一条记录，再查看当前生命周期状态。" icon="tasks" />
            )}
          </aside>
        </div>
      </section>
    </div>
  );
}
