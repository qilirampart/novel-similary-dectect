import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { getSystemStatus, listResults, listTasks } from "../api";
import { formatConfidenceLabel, formatDetectionModeLabel, formatReviewLabel, formatRuntimeStateLabel, formatSystemCardLabel, formatSystemHint, formatTaskTypeLabel } from "../displayText";
import { Icon } from "../icons";
import { StatusState } from "../StatusState";
import { formatTaskEta, formatTaskProgressDetail } from "../taskProgress";
import { buildReviewPath, isHighRiskReviewLabel } from "../workflowLinks";

function toPercent(task: Record<string, any>): number {
  const counts = task.counts ?? {};
  const accepted = Number(counts.accepted ?? 0);
  const completed = Number(counts.completed ?? 0);
  const failed = Number(counts.failed ?? 0);
  if (accepted <= 0) return 0;
  return Math.min(Math.round(((completed + failed) / accepted) * 100), 100);
}

function scorePercent(value: unknown): number {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 0;
  return Math.min(Math.round(numeric * 100), 100);
}

export function DashboardPage() {
  const [tasks, setTasks] = useState<Record<string, any>[]>([]);
  const [results, setResults] = useState<Record<string, any>[]>([]);
  const [systemCards, setSystemCards] = useState<Array<{ label: string; value: string; hint: string; tone: string }>>([]);
  const [tasksLoading, setTasksLoading] = useState(true);
  const [resultsLoading, setResultsLoading] = useState(true);
  const [systemLoading, setSystemLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;

    async function loadTasks() {
      try {
        const response = await listTasks(10, 0);
        if (!cancelled) {
          setTasks(response.items);
        }
      } catch {
        if (!cancelled) {
          setTasks([]);
        }
      } finally {
        if (!cancelled) {
          setTasksLoading(false);
        }
      }
    }

    async function loadResults() {
      try {
        const response = await listResults({ limit: 8, offset: 0, sortBy: "score_desc" });
        if (!cancelled) {
          setResults(response.items);
        }
      } catch {
        if (!cancelled) {
          setResults([]);
        }
      } finally {
        if (!cancelled) {
          setResultsLoading(false);
        }
      }
    }

    async function loadSystemCards() {
      try {
        const response = await getSystemStatus();
        if (!cancelled) {
          setSystemCards(response.payload.cards);
        }
      } catch {
        if (!cancelled) {
          setSystemCards([]);
        }
      } finally {
        if (!cancelled) {
          setSystemLoading(false);
        }
      }
    }

    void loadTasks();
    void loadResults();
    void loadSystemCards();

    return () => {
      cancelled = true;
    };
  }, []);

  const dashboardStats = useMemo(() => {
    const running = tasks.filter((task) => task.status === "running").length;
    const queued = tasks.filter((task) => task.status === "queued").length;
    const highRisk = results.filter((result) => isHighRiskReviewLabel(result.top1_review_label)).length;
    const semanticFallback = results.filter((result) => result.semantic_status === "fallback_lexical_only").length;

    return [
      {
        label: "任务总数",
        value: tasksLoading ? "加载中" : String(tasks.length),
        delta: tasksLoading ? "正在拉取任务列表" : `排队中 ${queued}`,
        tone: "blue"
      },
      {
        label: "运行中",
        value: tasksLoading ? "加载中" : String(running),
        delta: tasksLoading ? "正在拉取任务列表" : "当前工作进程",
        tone: "cyan"
      },
      {
        label: "高风险",
        value: resultsLoading ? "加载中" : String(highRisk),
        delta: resultsLoading ? "正在拉取结果列表" : "Top1 风险标签",
        tone: "orange"
      },
      {
        label: "回退结果",
        value: resultsLoading ? "加载中" : String(semanticFallback),
        delta: resultsLoading ? "正在拉取结果列表" : "改写检测回退",
        tone: "green"
      }
    ];
  }, [results, resultsLoading, tasks, tasksLoading]);

  const systemCardItems = useMemo(() => {
    if (systemCards.length > 0) {
      return systemCards.slice(0, 4);
    }

    if (systemLoading) {
      return [
        { label: "Platform Health", value: "加载中", hint: "正在拉取系统状态", tone: "green" },
        { label: "Stored Contents", value: "加载中", hint: "正在拉取系统状态", tone: "blue" },
        { label: "Semantic Chunks", value: "加载中", hint: "正在拉取系统状态", tone: "cyan" },
        { label: "Running Tasks", value: "加载中", hint: "正在拉取系统状态", tone: "orange" }
      ];
    }

    return [];
  }, [systemCards, systemLoading]);

  return (
    <div className="page-grid dashboard-grid">
      <section className="hero-panel">
        <div className="hero-copy">
          <span className="eyebrow">控制台</span>
          <h1>小说库相似度比对</h1>
          <p>在一个运营总览中集中监控任务吞吐、复核信号和系统健康状态。</p>
          <div className="hero-actions">
            <Link className="primary-button" to="/single-compare">
              <Icon name="spark" />
              开始比对
            </Link>
            <Link className="ghost-button dark" to="/batch-tasks">
              <Icon name="layers" />
              批量任务
            </Link>
          </div>
        </div>
        <div className="hero-visual">
          <div className="hero-orbit orbit-a" />
          <div className="hero-orbit orbit-b" />
          <div className="hero-card">
            <div className="hero-paper" />
            <div className="hero-lens" />
          </div>
        </div>
      </section>

      <section className="stats-grid">
        {dashboardStats.map((item) => (
          <article key={item.label} className={`stat-card tone-${item.tone}`}>
            <div className="stat-icon-wrap">
              <Icon name={item.tone === "orange" ? "warning" : item.tone === "green" ? "shield" : "doc"} />
            </div>
            <div className="stat-body">
              <span className="stat-label">{item.label}</span>
              <strong className="stat-value">{item.value}</strong>
              <span className="stat-delta">{item.delta}</span>
            </div>
            <div className="sparkline" />
          </article>
        ))}
      </section>

      <div className="dashboard-main-grid span-full">
        <section className="card-panel tasks-panel">
          <div className="section-heading">
            <div>
              <h2>最近任务</h2>
              <p>查看最近任务动态与执行进度快照。</p>
            </div>
          </div>
          <table className="data-table">
            <thead>
              <tr>
                <th>任务 ID</th>
                <th>类型</th>
                <th>模式</th>
                <th>状态</th>
                <th>进度</th>
                <th>创建时间</th>
              </tr>
            </thead>
            <tbody>
              {tasksLoading ? (
                <tr>
                  <td colSpan={6}>
                    <StatusState title="正在加载任务列表" description="正在同步最近任务的执行进度和状态快照。" tone="info" />
                  </td>
                </tr>
              ) : tasks.length > 0 ? (
                tasks.map((task) => (
                  <tr key={String(task.task_id)}>
                    <td>{task.task_id}</td>
                    <td>{formatTaskTypeLabel(String(task.task_type ?? ""))}</td>
                    <td>{formatDetectionModeLabel(String(task.detection_mode ?? ""))}</td>
                    <td><span className={`status-pill ${task.status}`}>{formatRuntimeStateLabel(String(task.status ?? ""))}</span></td>
                    <td>
                      <div className="progress-inline">
                        <div className="progress-bar"><span style={{ width: `${toPercent(task)}%` }} /></div>
                        <span>{task.status === "running" ? `${formatTaskProgressDetail(task)} · ${formatTaskEta(task)}` : `${toPercent(task)}%`}</span>
                      </div>
                    </td>
                    <td>{task.created_at}</td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={6}>
                    <StatusState title="当前还没有任务" description="先创建一次单条或批量比对任务，这里才会出现最新执行记录。" />
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </section>

        <section className="dark-card-panel status-panel dashboard-status-panel">
          <div className="section-heading inverted">
            <div>
              <h2>系统健康</h2>
              <p>快速查看运行态和持久化状态。</p>
            </div>
            <Link className="ghost-button dark subtle" to="/system-status">查看状态</Link>
          </div>
          <div className="status-list">
            {systemCardItems.map((item) => (
              <div key={item.label} className="status-row dashboard-status-row">
                <div className="status-leading">
                  <span className="status-icon"><Icon name={item.label.toLowerCase().includes("semantic") ? "spark" : item.label.toLowerCase().includes("task") ? "pulse" : "database"} /></span>
                  <div>
                    <div className="status-name">{formatSystemCardLabel(item.label)}</div>
                    <div className="status-extra">{formatSystemHint(item.hint)}</div>
                  </div>
                </div>
                <div className="status-meta">
                  <span className="online-dot" />
                  <span>{formatRuntimeStateLabel(String(item.value ?? ""))}</span>
                </div>
              </div>
            ))}
          </div>
        </section>
      </div>

      <section className="card-panel span-full dashboard-risk-panel">
        <div className="section-heading">
          <div>
            <h2>高分命中</h2>
            <p>查看最近结果中置信度最高的候选命中。</p>
          </div>
        </div>
        <div className="risk-table dashboard-risk-table">
          {resultsLoading ? (
            <StatusState title="正在加载高分命中" description="正在拉取最近结果中的高分候选。" tone="info" />
          ) : results.length > 0 ? (
            results.map((item) => (
              <div key={String(item.result_id)} className="risk-row">
                <div className="risk-title">
                  <span className="risk-dot" />
                  <div>
                    <strong>{item.top1_book_name || "-"}</strong>
                    <span>{item.top1_chapter_name || "-"}</span>
                  </div>
                </div>
                <div className="risk-tags">
                  <span className="soft-tag">{formatReviewLabel(String(item.top1_review_label || ""))}</span>
                  <span className="soft-tag">{formatConfidenceLabel(String(item.top1_confidence_label || ""))}</span>
                </div>
                <div className="risk-score">
                  <div className="bar-shell"><span style={{ width: `${Math.min(scorePercent(item.top1_fine_score), 100)}%` }} /></div>
                  <strong>{item.top1_fine_score === null || item.top1_fine_score === undefined ? "-" : `${Math.round(Number(item.top1_fine_score) * 100)}%`}</strong>
                </div>
                <div className="risk-source">{item.task_id}</div>
                <Link
                  className="outline-button"
                  to={buildReviewPath({
                    taskId: item.task_id,
                    resultId: item.result_id,
                    q: item.query_text_preview || item.top1_book_name || item.top1_chapter_name
                  })}
                >
                  去复核
                </Link>
              </div>
            ))
          ) : (
            <StatusState title="当前还没有高分命中" description="待任务产生结果后，这里会展示最近的高分候选命中。" />
          )}
        </div>
      </section>
    </div>
  );
}
