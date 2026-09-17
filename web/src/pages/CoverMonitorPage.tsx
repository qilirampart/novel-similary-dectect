import { useEffect, useRef, useState, type DragEvent } from "react";

import {
  confirmCoverMonitorImport,
  controlCoverMonitorRun,
  createCoverMonitorRun,
  downloadCoverMonitorRunExport,
  getCoverMonitorOverview,
  getCoverMonitorRunDetail,
  listCoverMonitorResults,
  listCoverMonitorRuns,
  previewCoverMonitorImport,
  type CoverImportKind,
  type CoverImportResponse,
  type CoverMonitorOverviewResponse,
  type CoverResultListResponse,
  type CoverRunDetailResponse,
  type CoverRunSummary
} from "../api";
import { Icon } from "../icons";
import { coverRunActions, coverRunStatusLabel, type CoverRunAction } from "../coverDisplay";
import { CoverChannelPanel } from "./CoverChannelPanel";
import { CoverRiskReviewPanel } from "./CoverRiskReviewPanel";
import { CoverScopeFilters } from "./CoverScopeFilters";


const EMPTY_OVERVIEW: CoverMonitorOverviewResponse = {
  channel_count: 0,
  video_count: 0,
  risk_count: 0,
  pending_review_count: 0,
  risk_distribution: { safe: 0, review: 0, risk: 0, unknown: 0 },
  latest_run: null
};

const EMPTY_RESULTS: CoverResultListResponse = {
  items: [],
  total: 0,
  limit: 12,
  offset: 0,
  counts: { all: 0, risk: 0, review: 0, unknown: 0 }
};

const resultRiskLabels: Record<string, string> = {
  risk: "风险",
  review: "待复核",
  unknown: "检测异常"
};

const tabs = ["工作台", "频道清单", "巡检批次", "风险复核", "历史整改"];
const enabledTabs = new Set(["工作台", "频道清单", "巡检批次", "风险复核"]);

function formatNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN").format(Math.max(Number(value) || 0, 0));
}

function runProgress(overview: CoverMonitorOverviewResponse): number {
  const run = overview.latest_run;
  if (!run || run.total_item_count <= 0) return 0;
  const settled = run.completed_item_count + run.failed_item_count;
  return Math.min(Math.round((settled / run.total_item_count) * 100), 100);
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    queued: "排队中",
    running: "执行中",
    pause_requested: "暂停处理中",
    paused: "已暂停",
    cancel_requested: "取消处理中",
    cancelled: "已取消",
    completed: "已完成",
    partial_failed: "部分失败",
    failed: "失败"
  };
  return labels[status] || status || "未知";
}

function reasonLabel(reason: string): string {
  return ({
    new_video: "新增/首次检测",
    historical_risk: "历史风险复测",
    retry_unknown: "异常结果重试",
    manual: "手动强制复检"
  } as Record<string, string>)[reason] || reason;
}

function stageLabel(stage: string): string {
  return ({ download: "下载封面", review: "模型检测", persist: "保存结果" } as Record<string, string>)[stage] || stage;
}

export function CoverMonitorPage() {
  const [activeTab, setActiveTab] = useState("工作台");
  const [overview, setOverview] = useState<CoverMonitorOverviewResponse>(EMPTY_OVERVIEW);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [isImportOpen, setIsImportOpen] = useState(false);
  const [isRunOpen, setIsRunOpen] = useState(false);
  const [runIntensity, setRunIntensity] = useState<"conservative" | "standard" | "strict">("standard");
  const [runIncludeShorts, setRunIncludeShorts] = useState(true);
  const [runForceRefresh, setRunForceRefresh] = useState(false);
  const [runMaxItems, setRunMaxItems] = useState(0);
  const [runBusy, setRunBusy] = useState(false);
  const [runError, setRunError] = useState("");
  const [exportBusy, setExportBusy] = useState<"new-findings" | "historical-rectification" | "">("");
  const [exportMessage, setExportMessage] = useState("");
  const [runs, setRuns] = useState<CoverRunSummary[]>([]);
  const [runTotal, setRunTotal] = useState(0);
  const [selectedRunId, setSelectedRunId] = useState("");
  const [runDetail, setRunDetail] = useState<CoverRunDetailResponse | null>(null);
  const [runListLoading, setRunListLoading] = useState(false);
  const [results, setResults] = useState<CoverResultListResponse>(EMPTY_RESULTS);
  const [resultFilter, setResultFilter] = useState("");
  const [resultOperatorPk, setResultOperatorPk] = useState<number | undefined>();
  const [resultChannelPk, setResultChannelPk] = useState<number | undefined>();
  const [resultLoading, setResultLoading] = useState(true);
  const [previewImage, setPreviewImage] = useState<{ url: string; title: string } | null>(null);
  const [itemOffset, setItemOffset] = useState(0);
  const [importKind, setImportKind] = useState<CoverImportKind>("channels");
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importPreview, setImportPreview] = useState<CoverImportResponse | null>(null);
  const [importError, setImportError] = useState("");
  const [importBusy, setImportBusy] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const importInputRef = useRef<HTMLInputElement | null>(null);
  const runDetailRequestRef = useRef(0);
  const previewCloseRef = useRef<HTMLButtonElement | null>(null);
  const previewTriggerRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!previewImage) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    previewCloseRef.current?.focus();
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setPreviewImage(null);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", closeOnEscape);
      window.requestAnimationFrame(() => previewTriggerRef.current?.focus());
    };
  }, [previewImage]);

  async function exportRun(kind: "new-findings" | "historical-rectification") {
    if (!runDetail || exportBusy) return;
    setExportBusy(kind);
    setExportMessage("");
    setError("");
    try {
      const fileName = await downloadCoverMonitorRunExport(runDetail.run.run_id, kind);
      setExportMessage(`${fileName} 已开始下载`);
    } catch (exportError) {
      setError(exportError instanceof Error ? exportError.message : "封面巡检报告导出失败");
    } finally {
      setExportBusy("");
    }
  }

  async function loadOverview(silent = false) {
    if (!silent) setLoading(true);
    if (!silent) setError("");
    try {
      setOverview(await getCoverMonitorOverview());
      setError("");
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "封面巡检概览加载失败");
    } finally {
      if (!silent) setLoading(false);
    }
  }

  useEffect(() => {
    void loadOverview();
  }, []);

  async function loadResults(filter = resultFilter, offset = 0) {
    setResultLoading(true);
    try {
      setResults(await listCoverMonitorResults(
        filter,
        12,
        offset,
        resultOperatorPk,
        resultChannelPk
      ));
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "封面检测结果加载失败");
    } finally {
      setResultLoading(false);
    }
  }

  useEffect(() => {
    if (activeTab === "工作台") void loadResults(resultFilter, 0);
  }, [activeTab, resultFilter, resultOperatorPk, resultChannelPk]);

  useEffect(() => {
    const status = overview.latest_run?.status;
    if (!status || !["queued", "running", "pause_requested", "cancel_requested"].includes(status)) return;
    const timer = window.setInterval(() => void loadOverview(true), 2000);
    return () => window.clearInterval(timer);
  }, [overview.latest_run?.status]);

  async function loadRunDetail(runId: string, offset = 0, silent = false) {
    const requestId = ++runDetailRequestRef.current;
    if (!silent) {
      setSelectedRunId(runId);
      setRunListLoading(true);
      setError("");
    }
    try {
      const detail = await getCoverMonitorRunDetail(runId, 50, offset);
      if (requestId !== runDetailRequestRef.current) return;
      setRunDetail(detail);
      setItemOffset(offset);
      setError("");
    } catch (detailError) {
      if (requestId !== runDetailRequestRef.current || silent) return;
      setError(detailError instanceof Error ? detailError.message : "巡检批次详情加载失败");
    } finally {
      if (!silent && requestId === runDetailRequestRef.current) setRunListLoading(false);
    }
  }

  async function loadRuns() {
    setRunListLoading(true);
    setError("");
    try {
      const response = await listCoverMonitorRuns(50, 0);
      setRuns(response.items);
      setRunTotal(response.total);
      const selectedStillVisible = response.items.some((item) => item.run_id === selectedRunId);
      const targetRunId = selectedStillVisible ? selectedRunId : response.items[0]?.run_id || "";
      if (targetRunId) await loadRunDetail(targetRunId, targetRunId === selectedRunId ? itemOffset : 0);
      else {
        setSelectedRunId("");
        setRunDetail(null);
      }
    } catch (listError) {
      setError(listError instanceof Error ? listError.message : "巡检批次加载失败");
    } finally {
      setRunListLoading(false);
    }
  }

  useEffect(() => {
    if (activeTab === "巡检批次") void loadRuns();
  }, [activeTab]);

  useEffect(() => {
    if (activeTab !== "巡检批次" || !selectedRunId || !runDetail) return;
    if (!["queued", "running", "pause_requested", "cancel_requested"].includes(runDetail.run.status)) return;
    const timer = window.setInterval(() => void loadRunDetail(selectedRunId, itemOffset, true), 2000);
    return () => window.clearInterval(timer);
  }, [activeTab, selectedRunId, itemOffset, runDetail?.run.status]);

  async function createRun() {
    setRunBusy(true);
    setRunError("");
    try {
      await createCoverMonitorRun({
        intensity: runIntensity,
        includeShorts: runIncludeShorts,
        forceRefresh: runForceRefresh,
        maxItemsPerScope: runMaxItems
      });
      setIsRunOpen(false);
      await loadOverview(true);
    } catch (createError) {
      setRunError(createError instanceof Error ? createError.message : "创建巡检失败");
    } finally {
      setRunBusy(false);
    }
  }

  async function controlRun(action: CoverRunAction, requestedRunId?: string) {
    const runId = requestedRunId || overview.latest_run?.run_id;
    if (!runId) return;
    setRunBusy(true);
    setError("");
    try {
      await controlCoverMonitorRun(runId, action);
      await loadOverview(true);
      if (activeTab === "巡检批次" && runId === selectedRunId) {
        await loadRunDetail(runId, itemOffset, true);
        const response = await listCoverMonitorRuns(50, 0);
        setRuns(response.items);
        setRunTotal(response.total);
      }
    } catch (controlError) {
      setError(controlError instanceof Error ? controlError.message : "巡检状态更新失败");
    } finally {
      setRunBusy(false);
    }
  }

  function closeImport() {
    if (importBusy) return;
    setIsImportOpen(false);
    setImportFile(null);
    setImportPreview(null);
    setImportError("");
  }

  function selectImportFile(file: File | null) {
    setImportFile(file);
    setImportPreview(null);
    setImportError("");
  }

  function handleImportDrop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    setIsDragging(false);
    selectImportFile(event.dataTransfer.files?.[0] ?? null);
  }

  async function runImportPreview() {
    if (!importFile) {
      setImportError("请先选择 Excel 文件");
      return;
    }
    setImportBusy(true);
    setImportError("");
    try {
      setImportPreview(await previewCoverMonitorImport({ file: importFile, importKind }));
    } catch (previewError) {
      setImportError(previewError instanceof Error ? previewError.message : "导入预检失败");
    } finally {
      setImportBusy(false);
    }
  }

  async function confirmImport() {
    if (!importPreview) return;
    setImportBusy(true);
    setImportError("");
    try {
      const completed = await confirmCoverMonitorImport(importPreview.import_id);
      setImportPreview(completed);
      await loadOverview();
    } catch (confirmError) {
      setImportError(confirmError instanceof Error ? confirmError.message : "确认导入失败");
    } finally {
      setImportBusy(false);
    }
  }

  const run = overview.latest_run;
  const progress = runProgress(overview);
  const currentRunActions = coverRunActions(run?.status || "");
  const selectedRunActions = coverRunActions(runDetail?.run.status || "");
  const distributionTotal = Object.values(overview.risk_distribution).reduce(
    (total, value) => total + value,
    0
  );

  return (
    <div className="page-grid cover-monitor-page">
      <header className="cover-monitor-heading">
        <div>
          <span className="eyebrow">COVER MONITOR</span>
          <h1>封面巡检工作台</h1>
          <p>持续追踪频道新增视频与历史风险整改，检测任务和数据与现有匹配业务独立运行。</p>
        </div>
        <div className="cover-monitor-heading-actions">
          <button className="outline-button" type="button" onClick={() => setIsImportOpen(true)}>
            <Icon name="upload" />导入频道表
          </button>
          <button
            className="primary-button"
            type="button"
            disabled={loading || overview.channel_count <= 0 || runBusy}
            title={overview.channel_count <= 0 ? "请先导入频道" : "创建新的封面巡检批次"}
            onClick={() => { setRunError(""); setIsRunOpen(true); }}
          >
            <span className="cover-button-plus">+</span>新建巡检
          </button>
        </div>
      </header>

      <nav className="cover-monitor-tabs" aria-label="封面巡检模块">
        {tabs.map((tab) => (
          <button key={tab} type="button" className={activeTab === tab ? "active" : ""} disabled={!enabledTabs.has(tab)} onClick={() => { setError(""); setActiveTab(tab); }}>
            {tab}{!enabledTabs.has(tab) && <small>待接入</small>}
          </button>
        ))}
      </nav>

      {error && activeTab !== "风险复核" && (
        <div className="cover-monitor-error" role="alert">
          <div><strong>页面数据加载失败</strong><span>{error}</span></div>
          <button className="ghost-button slim" type="button" onClick={() => void (activeTab === "巡检批次" ? loadRuns() : loadOverview())}>重新加载</button>
        </div>
      )}

      {activeTab === "工作台" ? <>
      <section className="cover-monitor-kpis" aria-label="封面巡检概览">
        <article>
          <div className="cover-kpi-icon green"><Icon name="queue" /></div>
          <div><span>在管频道</span><strong>{loading ? "--" : formatNumber(overview.channel_count)}</strong><small>已启用监测的频道</small></div>
        </article>
        <article>
          <div className="cover-kpi-icon blue"><Icon name="file" /></div>
          <div><span>已归档视频</span><strong>{loading ? "--" : formatNumber(overview.video_count)}</strong><small>按视频 ID 去重</small></div>
        </article>
        <article>
          <div className="cover-kpi-icon red"><Icon name="warning" /></div>
          <div><span>风险结果</span><strong>{loading ? "--" : formatNumber(overview.risk_count)}</strong><small>模型初筛，不代替人工结论</small></div>
        </article>
        <article>
          <div className="cover-kpi-icon orange"><Icon name="review" /></div>
          <div><span>待人工处理</span><strong>{loading ? "--" : formatNumber(overview.pending_review_count)}</strong><small>风险与不确定项</small></div>
        </article>
      </section>

      <section className="cover-monitor-overview-grid">
        <article className="card-panel cover-current-run">
          <div className="section-heading">
            <div><h2>当前巡检批次</h2><p>采集、封面下载和模型检测将分别记录进度。</p></div>
            <div className="cover-run-heading-actions">
              {currentRunActions.includes("resume") && <button className="outline-button slim" type="button" disabled={runBusy} onClick={() => void controlRun("resume")}>继续</button>}
              {currentRunActions.includes("pause") && <button className="outline-button slim" type="button" disabled={runBusy} onClick={() => void controlRun("pause")}>暂停</button>}
              {currentRunActions.includes("cancel") && <button className="ghost-button slim danger" type="button" disabled={runBusy} onClick={() => void controlRun("cancel")}>取消</button>}
              {run && <span className={`cover-run-status ${run.status}`}>{coverRunStatusLabel(run.status, run.failed_item_count)}</span>}
            </div>
          </div>
          {run ? (
            <div className="cover-run-body">
              <div className="cover-run-title-row">
                <strong>{run.run_id.slice(0, 12)}</strong><span>{progress}%</span>
              </div>
              <div className="cover-progress-track"><i style={{ width: `${progress}%` }} /></div>
              <div className="cover-run-metrics">
                <div><span>任务总量</span><strong>{formatNumber(run.total_item_count)}</strong></div>
                <div><span>已完成</span><strong>{formatNumber(run.completed_item_count)}</strong></div>
                <div><span>失败项</span><strong>{formatNumber(run.failed_item_count)}</strong></div>
                <div><span>检测档位</span><strong>{{ conservative: "保守", standard: "标准", strict: "严格" }[run.intensity] || run.intensity}</strong></div>
              </div>
              {run.status_message && <p className="cover-run-message">{run.status_message}</p>}
            </div>
          ) : (
            <div className="cover-empty-run">
              <div className="cover-empty-symbol"><Icon name="pulse" /></div>
              <div><strong>尚未创建巡检批次</strong><p>先导入频道总表并完成预检，之后即可创建首次手动巡检。</p></div>
              <span>等待频道导入</span>
            </div>
          )}
        </article>

        <article className="card-panel cover-risk-summary">
          <div className="section-heading">
            <div><h2>风险概览</h2><p>仅统计已经产生有效模型响应的检测。</p></div>
          </div>
          <div className="cover-risk-body">
            <div className={`cover-risk-ring${distributionTotal === 0 ? " empty" : ""}`}>
              <div><strong>{formatNumber(distributionTotal)}</strong><span>检测结果</span></div>
            </div>
            <div className="cover-risk-legend">
              <div><i className="safe" /><span>安全</span><strong>{formatNumber(overview.risk_distribution.safe)}</strong></div>
              <div><i className="risk" /><span>风险</span><strong>{formatNumber(overview.risk_distribution.risk)}</strong></div>
              <div><i className="review" /><span>待复核</span><strong>{formatNumber(overview.risk_distribution.review)}</strong></div>
              <div><i className="unknown" /><span>未知/异常</span><strong>{formatNumber(overview.risk_distribution.unknown)}</strong></div>
            </div>
          </div>
        </article>
      </section>

      <section className="card-panel cover-monitor-results">
        <div className="section-heading">
          <div><h2>风险与待复核</h2><p>后续将在这里集中展示封面证据、模型理由和人工处置记录。</p></div>
          <button className="outline-button slim" type="button" disabled><Icon name="file" />导出结果</button>
        </div>
        <div className="cover-results-toolbar">
          <div className="cover-filter-chips">
            {[
              ["", "全部", results.counts.all],
              ["risk", "风险", results.counts.risk],
              ["review", "待复核", results.counts.review],
              ["unknown", "检测异常", results.counts.unknown]
            ].map(([value, label, count]) => (
              <button
                className={resultFilter === value ? "active" : ""}
                type="button"
                key={String(value)}
                disabled={resultLoading}
                onClick={() => setResultFilter(String(value))}
              >{label} {formatNumber(Number(count))}</button>
            ))}
          </div>
          <div className="cover-toolbar-note"><Icon name="shield" />当前为独立数据空间，不读取字幕或小说任务结果</div>
        </div>
        <CoverScopeFilters
          operatorPk={resultOperatorPk}
          channelPk={resultChannelPk}
          disabled={resultLoading}
          onOperatorChange={setResultOperatorPk}
          onChannelChange={setResultChannelPk}
        />
        {resultLoading ? (
          <div className="cover-results-empty"><strong>正在加载检测结果...</strong></div>
        ) : results.items.length > 0 ? (
          <>
            <div className="cover-result-grid">
              {results.items.map((item) => (
                <article className={`cover-result-card ${item.overall_risk}`} key={item.result_id}>
                  <button
                    className="cover-result-image-button"
                    type="button"
                    aria-label={`放大查看封面：${item.video_title || item.video_id}`}
                    onClick={(event) => {
                      previewTriggerRef.current = event.currentTarget;
                      setPreviewImage({ url: item.thumbnail_url, title: item.video_title || item.video_id });
                    }}
                  >
                    <img src={item.thumbnail_url} alt="" loading="lazy" referrerPolicy="no-referrer" />
                    <span aria-hidden="true">放大查看</span>
                  </button>
                  <div>
                    <div className="cover-result-card-heading">
                      <span className={`cover-result-risk ${item.overall_risk}`}>{resultRiskLabels[item.overall_risk]}</span>
                      <small>{item.source === "historical_import" ? "历史检测表" : "当前模型检测"}</small>
                    </div>
                    <strong title={item.video_title}>{item.video_title || item.video_id}</strong>
                    <p>{item.summary || item.evidence || "原检测表未提供结果说明"}</p>
                    <footer>
                      <span>{item.video_id}</span>
                      <time>{new Date(item.created_at).toLocaleString("zh-CN")}</time>
                    </footer>
                  </div>
                </article>
              ))}
            </div>
            <div className="cover-result-pagination">
              <span>共 {formatNumber(results.total)} 条，第 {Math.floor(results.offset / results.limit) + 1} 页</span>
              <div>
                <button className="ghost-button slim" type="button" disabled={results.offset <= 0 || resultLoading} onClick={() => void loadResults(resultFilter, Math.max(0, results.offset - results.limit))}>上一页</button>
                <button className="ghost-button slim" type="button" disabled={results.offset + results.limit >= results.total || resultLoading} onClick={() => void loadResults(resultFilter, results.offset + results.limit)}>下一页</button>
              </div>
            </div>
          </>
        ) : <div className="cover-results-empty">
          <div className="cover-results-empty-art"><Icon name="review" /></div>
          <strong>还没有封面检测结果</strong>
          <p>完成频道导入和首次巡检后，结果会按风险优先级进入这里。</p>
          <div className="cover-setup-steps">
            <span className="done"><b>1</b>独立数据层</span>
            <i />
            <span><b>2</b>导入频道</span>
            <i />
            <span><b>3</b>创建巡检</span>
            <i />
            <span><b>4</b>人工复核</span>
          </div>
        </div>}
      </section>
      </> : activeTab === "频道清单" ? (
        <CoverChannelPanel />
      ) : activeTab === "巡检批次" ? (
        <section className="cover-run-browser">
          <aside className="card-panel cover-run-list">
            <div className="section-heading">
              <div><h2>巡检批次</h2><p>共 {formatNumber(runTotal)} 个批次</p></div>
              <button className="ghost-button slim" type="button" disabled={runListLoading} onClick={() => void loadRuns()}>刷新</button>
            </div>
            <div className="cover-run-list-items">
              {runs.map((item) => (
                <button key={item.run_id} type="button" className={selectedRunId === item.run_id ? "active" : ""} onClick={() => void loadRunDetail(item.run_id, 0)}>
                  <span><strong>{item.run_id.slice(0, 8)}</strong><i className={item.status}>{coverRunStatusLabel(item.status, item.failed_item_count)}</i></span>
                  <small>{item.total_channel_count} 个频道 · {item.completed_item_count + item.failed_item_count}/{item.total_item_count} 条</small>
                  <time>{new Date(item.created_at).toLocaleString("zh-CN")}</time>
                </button>
              ))}
              {!runListLoading && runs.length === 0 && <div className="cover-run-list-empty">尚无巡检批次</div>}
            </div>
          </aside>

          <article className="card-panel cover-run-detail">
            {runDetail ? <>
              <div className="section-heading">
                <div><h2>批次 {runDetail.run.run_id.slice(0, 8)}</h2><p>{runDetail.run.status_message || "等待状态更新"}</p></div>
                <div className="cover-run-heading-actions">
                  {selectedRunActions.includes("resume") && <button className="outline-button slim" type="button" disabled={runBusy} onClick={() => void controlRun("resume", runDetail.run.run_id)}>继续</button>}
                  {selectedRunActions.includes("pause") && <button className="outline-button slim" type="button" disabled={runBusy} onClick={() => void controlRun("pause", runDetail.run.run_id)}>暂停</button>}
                  {selectedRunActions.includes("cancel") && <button className="ghost-button slim danger" type="button" disabled={runBusy} onClick={() => void controlRun("cancel", runDetail.run.run_id)}>取消</button>}
                  <button className="outline-button slim" type="button" disabled={Boolean(exportBusy)} onClick={() => void exportRun("new-findings")}>{exportBusy === "new-findings" ? "生成中..." : "导出新增报告"}</button>
                  <button className="outline-button slim" type="button" disabled={Boolean(exportBusy)} onClick={() => void exportRun("historical-rectification")}>{exportBusy === "historical-rectification" ? "生成中..." : "导出整改报告"}</button>
                  <span className={`cover-run-status ${runDetail.run.status}`}>{coverRunStatusLabel(runDetail.run.status, runDetail.run.failed_item_count)}</span>
                </div>
              </div>
              {exportMessage && <div className="cover-channel-message" role="status">{exportMessage}</div>}
              <div className="cover-run-detail-summary">
                <div><span>频道</span><strong>{runDetail.channels.length}</strong></div>
                <div><span>任务项</span><strong>{formatNumber(runDetail.item_total)}</strong></div>
                <div><span>已完成</span><strong>{formatNumber(runDetail.run.completed_item_count)}</strong></div>
                <div><span>失败</span><strong>{formatNumber(runDetail.run.failed_item_count)}</strong></div>
              </div>
              <div className="cover-run-channel-strip">
                {runDetail.channels.map((channel) => (
                  <div key={channel.run_channel_id} className={channel.completeness}>
                    <span><strong>{channel.channel_name}</strong><i>{channel.completeness === "complete" ? "完整" : channel.completeness === "partial" ? "部分完成" : channel.completeness === "failed" ? "失败" : "待扫描"}</i></span>
                    <small>发现 {formatNumber(channel.discovered_count)} 条{channel.error_message ? ` · ${channel.error_message}` : ""}</small>
                  </div>
                ))}
              </div>
              <div className="cover-run-item-table">
                <div className="cover-run-item-head"><span>视频</span><span>入队原因</span><span>阶段/状态</span><span>检测结果</span></div>
                {runDetail.items.map((item) => (
                  <div className="cover-run-item-row" key={item.task_item_id}>
                    <div><strong title={item.video_title}>{item.video_title}</strong><small>{item.video_id}</small></div>
                    <span>{reasonLabel(item.reason)}</span>
                    <div><strong>{stageLabel(item.stage)}</strong><small>{statusLabel(item.status)} · 尝试 {item.attempts}</small></div>
                    <div>
                      <strong>{item.overall_risk ? ({ safe: "安全", review: "待复核", risk: "风险", unknown: "异常" } as Record<string, string>)[item.overall_risk] : "待检测"}</strong>
                      <small title={item.error_message || item.summary || ""}>{item.confidence == null ? item.error_message || "暂无结果" : `置信度 ${(item.confidence * 100).toFixed(0)}%${item.summary ? ` · ${item.summary}` : ""}`}</small>
                    </div>
                  </div>
                ))}
                {runDetail.items.length === 0 && <div className="cover-run-detail-empty">{runListLoading ? "正在加载..." : "该批次尚未生成视频任务项"}</div>}
              </div>
              <div className="cover-run-pagination">
                <span>第 {runDetail.item_total === 0 ? 0 : itemOffset + 1}-{Math.min(itemOffset + runDetail.item_limit, runDetail.item_total)} 条，共 {formatNumber(runDetail.item_total)} 条</span>
                <div><button className="outline-button slim" type="button" disabled={itemOffset <= 0 || runListLoading} onClick={() => void loadRunDetail(runDetail.run.run_id, Math.max(itemOffset - 50, 0))}>上一页</button><button className="outline-button slim" type="button" disabled={itemOffset + runDetail.item_limit >= runDetail.item_total || runListLoading} onClick={() => void loadRunDetail(runDetail.run.run_id, itemOffset + 50)}>下一页</button></div>
              </div>
            </> : <div className="cover-run-detail-empty">{runListLoading ? "正在加载批次..." : "从左侧选择一个巡检批次"}</div>}
          </article>
        </section>
      ) : (
        <CoverRiskReviewPanel />
      )}

      {isRunOpen && (
        <div className="cover-import-overlay" role="dialog" aria-modal="true" aria-label="新建封面巡检" onClick={() => !runBusy && setIsRunOpen(false)}>
          <section className="cover-import-dialog cover-run-dialog" onClick={(event) => event.stopPropagation()}>
            <header>
              <div><span className="eyebrow">NEW INSPECTION</span><h2>新建封面巡检</h2><p>默认处理新增、尚未完成当前模型检测及上次结果未知的视频；强制复检会重新检测全部已有视频。</p></div>
              <button className="icon-button" type="button" aria-label="关闭新建巡检窗口" onClick={() => setIsRunOpen(false)} disabled={runBusy}>×</button>
            </header>
            <div className="cover-run-form">
              <label><span>检测档位</span><select value={runIntensity} onChange={(event) => setRunIntensity(event.target.value as typeof runIntensity)} disabled={runBusy}><option value="conservative">保守</option><option value="standard">标准</option><option value="strict">严格</option></select><small>标准档兼顾风险识别与误报控制。</small></label>
              <label><span>单范围采集上限</span><input type="number" min="0" max="10000" value={runMaxItems} onChange={(event) => setRunMaxItems(Math.min(Math.max(Number(event.target.value) || 0, 0), 10000))} disabled={runBusy} /><small>0 表示不限制；Videos 和 Shorts 分别计算。</small></label>
              <label className="cover-run-switch"><input type="checkbox" checked={runIncludeShorts} onChange={(event) => setRunIncludeShorts(event.target.checked)} disabled={runBusy} /><span><strong>同时扫描 Shorts</strong><small>关闭后只检查频道 Videos 页面。</small></span></label>
              <label className="cover-run-switch warning"><input type="checkbox" checked={runForceRefresh} onChange={(event) => setRunForceRefresh(event.target.checked)} disabled={runBusy} /><span><strong>强制复检已有视频</strong><small>会增加封面下载和视觉模型调用量，通常保持关闭。</small></span></label>
            </div>
            {runError && <div className="cover-import-message error" role="alert">{runError}</div>}
            <footer><button className="ghost-button" type="button" onClick={() => setIsRunOpen(false)} disabled={runBusy}>取消</button><button className="primary-button" type="button" onClick={() => void createRun()} disabled={runBusy}>{runBusy ? "正在创建..." : `开始巡检 ${formatNumber(overview.channel_count)} 个频道`}</button></footer>
          </section>
        </div>
      )}

      {previewImage && (
        <div className="cover-image-preview-overlay" role="dialog" aria-modal="true" aria-label={`封面大图：${previewImage.title}`} onClick={() => setPreviewImage(null)}>
          <section className="cover-image-preview-dialog" onClick={(event) => event.stopPropagation()}>
            <header>
              <strong title={previewImage.title}>{previewImage.title}</strong>
              <button ref={previewCloseRef} className="icon-button" type="button" aria-label="关闭封面大图" onClick={() => setPreviewImage(null)}>×</button>
            </header>
            <div><img src={previewImage.url} alt={previewImage.title} referrerPolicy="no-referrer" /></div>
            <p>点击空白区域或按 Esc 关闭</p>
          </section>
        </div>
      )}

      {isImportOpen && (
        <div className="cover-import-overlay" role="dialog" aria-modal="true" aria-label="导入封面巡检数据" onClick={closeImport}>
          <section className="cover-import-dialog" onClick={(event) => event.stopPropagation()}>
            <header>
              <div><span className="eyebrow">IMPORT PREVIEW</span><h2>导入封面巡检数据</h2><p>先预检、后确认。预检不会写入频道和视频主表。</p></div>
              <button className="icon-button" type="button" aria-label="关闭导入窗口" onClick={closeImport} disabled={importBusy}>×</button>
            </header>

            <div className="cover-import-controls">
              <label><span>数据类型</span><select value={importKind} onChange={(event) => { setImportKind(event.target.value as CoverImportKind); setImportPreview(null); }} disabled={importBusy || Boolean(importPreview?.status === "completed")}><option value="channels">频道总表</option><option value="videos">视频明细</option><option value="baseline">历史检测基准</option></select></label>
              <input ref={importInputRef} type="file" accept=".xlsx" onChange={(event) => selectImportFile(event.target.files?.[0] ?? null)} hidden />
              <label className={`cover-import-dropzone${isDragging ? " is-dragging" : ""}`} onDragEnter={() => setIsDragging(true)} onDragLeave={() => setIsDragging(false)} onDragOver={(event) => event.preventDefault()} onDrop={handleImportDrop} onClick={() => importInputRef.current?.click()}>
                <Icon name="upload" />
                <div><strong>{importFile?.name || "拖入 Excel，或点击选择文件"}</strong><span>{importFile ? `${(importFile.size / 1024 / 1024).toFixed(2)} MB` : "仅支持 .xlsx，最大 150 MB"}</span></div>
              </label>
            </div>

            {importError && <div className="cover-import-message error" role="alert">{importError}</div>}
            {importPreview && (
              <div className={`cover-import-preview ${importPreview.status}`}>
                <div className="cover-import-preview-title"><div><strong>{importPreview.status === "completed" ? "导入已完成" : "预检完成"}</strong><span>工作表：{importPreview.sheet_name || "默认首个工作表"}</span></div><code>{importPreview.import_id.slice(0, 8)}</code></div>
                <div className="cover-import-stats">
                  <div><span>来源行</span><strong>{formatNumber(importPreview.stats.total_rows || 0)}</strong></div>
                  <div><span>有效行</span><strong>{formatNumber(importPreview.stats.valid_rows || 0)}</strong></div>
                  <div><span>唯一频道</span><strong>{formatNumber(importPreview.stats.unique_channels || 0)}</strong></div>
                  <div><span>唯一视频</span><strong>{formatNumber(importPreview.stats.unique_videos || 0)}</strong></div>
                  <div className="muted"><span>重复</span><strong>{formatNumber(importPreview.stats.duplicate_rows || 0)}</strong></div>
                  <div className={(importPreview.stats.conflict_rows || 0) > 0 ? "danger" : "muted"}><span>冲突</span><strong>{formatNumber(importPreview.stats.conflict_rows || 0)}</strong></div>
                  <div className={(importPreview.stats.missing_rows || 0) > 0 ? "warning" : "muted"}><span>缺字段</span><strong>{formatNumber(importPreview.stats.missing_rows || 0)}</strong></div>
                  {importPreview.status === "completed" && <div className="success"><span>已落库视频</span><strong>{formatNumber(importPreview.stats.applied_videos || 0)}</strong></div>}
                </div>
                {(importPreview.stats.conflict_rows || 0) > 0 && <p className="cover-import-caution">存在归属冲突的行不会自动覆盖，将保留在冲突记录中等待人工处理。</p>}
              </div>
            )}

            <footer>
              <button className="ghost-button" type="button" onClick={closeImport} disabled={importBusy}>{importPreview?.status === "completed" ? "关闭" : "取消"}</button>
              {!importPreview && <button className="primary-button" type="button" onClick={() => void runImportPreview()} disabled={importBusy || !importFile}>{importBusy ? "正在预检..." : "开始预检"}</button>}
              {importPreview?.status === "previewed" && <button className="primary-button" type="button" onClick={() => void confirmImport()} disabled={importBusy}>{importBusy ? "正在导入..." : "确认并导入"}</button>}
            </footer>
          </section>
        </div>
      )}
    </div>
  );
}
