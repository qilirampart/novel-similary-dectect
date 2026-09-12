import { useEffect, useRef, useState } from "react";

import {
  coverRiskCaseAssetUrl,
  getCoverRiskCaseDetail,
  listCoverRiskCases,
  reviewCoverRiskCase,
  type CoverRiskCaseDetailResponse,
  type CoverRiskCaseEvent,
  type CoverRiskCaseReviewAction,
  type CoverRiskCaseSummary
} from "../api";

const PAGE_SIZE = 50;

const STATUS_LABELS: Record<string, string> = {
  open: "待复核",
  needs_review: "待复核",
  confirmed_rectified: "已确认整改",
  false_positive: "误报",
  unavailable: "已失联",
  closed: "已关闭"
};

const EVENT_LABELS: Record<string, string> = {
  risk_detected: "检测到风险",
  review_detected: "模型建议复核",
  unknown_detected: "检测异常",
  rectification_candidate: "疑似已换图整改",
  safe_redetection: "同图模型结果波动",
  confirmed_rectified: "人工确认已整改",
  false_positive: "人工标记误报",
  kept_open: "继续跟进",
  marked_unavailable: "标记失联",
  reopened: "重新打开"
};

function formatTime(value?: string | null): string {
  if (!value) return "--";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN");
}

function EvidenceCard({
  caseId,
  title,
  event,
  emptyText
}: {
  caseId: string;
  title: string;
  event?: CoverRiskCaseEvent;
  emptyText: string;
}) {
  return (
    <article className="cover-evidence-card">
      <header>
        <div><span>{title}</span><strong>{event ? EVENT_LABELS[event.event_type] || event.event_type : "暂无证据"}</strong></div>
        {event?.confidence != null && <b>{Math.round(event.confidence * 100)}%</b>}
      </header>
      <div className="cover-evidence-image">
        {event?.asset_id ? (
          <img src={coverRiskCaseAssetUrl(caseId, event.asset_id)} alt={`${title}封面证据`} />
        ) : (
          <span>{emptyText}</span>
        )}
      </div>
      <div className="cover-evidence-copy">
        <strong>{event?.summary || emptyText}</strong>
        <p>{event?.evidence || event?.reason || "暂无可见证据说明"}</p>
        {event?.content_sha256 && <code title={event.content_sha256}>图片版本 {event.content_sha256.slice(0, 12)}</code>}
      </div>
    </article>
  );
}

export function CoverRiskReviewPanel() {
  const [status, setStatus] = useState("needs_review");
  const [cases, setCases] = useState<CoverRiskCaseSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState<CoverRiskCaseDetailResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [notice, setNotice] = useState("");
  const detailRequestRef = useRef(0);
  const listRequestRef = useRef(0);

  async function loadDetail(caseId: string) {
    const requestId = ++detailRequestRef.current;
    setSelectedId(caseId);
    setDetail(null);
    setError("");
    setNotice("");
    try {
      const response = await getCoverRiskCaseDetail(caseId);
      if (requestId !== detailRequestRef.current) return;
      setDetail(response);
      setNote("");
    } catch (loadError) {
      if (requestId !== detailRequestRef.current) return;
      setError(loadError instanceof Error ? loadError.message : "风险案件详情加载失败");
    }
  }

  async function loadCases(nextOffset = offset, preferredId = selectedId) {
    const requestId = ++listRequestRef.current;
    detailRequestRef.current += 1;
    setLoading(true);
    setError("");
    try {
      const response = await listCoverRiskCases(status, PAGE_SIZE, nextOffset);
      if (requestId !== listRequestRef.current) return;
      setCases(response.items);
      setTotal(response.total);
      setOffset(nextOffset);
      const target = response.items.some((item) => item.case_id === preferredId)
        ? preferredId
        : response.items[0]?.case_id || "";
      if (target) await loadDetail(target);
      else {
        detailRequestRef.current += 1;
        setSelectedId("");
        setDetail(null);
      }
    } catch (loadError) {
      if (requestId !== listRequestRef.current) return;
      setError(loadError instanceof Error ? loadError.message : "风险案件列表加载失败");
    } finally {
      if (requestId === listRequestRef.current) setLoading(false);
    }
  }

  useEffect(() => {
    void loadCases(0, "");
  }, [status]);

  async function submitReview(action: CoverRiskCaseReviewAction) {
    if (!detail || note.trim().length < 2) {
      setError("请填写至少 2 个字的复核说明");
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const response = await reviewCoverRiskCase(detail.case.case_id, action, note.trim());
      setDetail(response);
      await loadCases(offset, action === "keep_open" ? detail.case.case_id : "");
      setNotice("复核结论已保存");
    } catch (reviewError) {
      setError(reviewError instanceof Error ? reviewError.message : "保存复核结论失败");
    } finally {
      setBusy(false);
    }
  }

  const openedEvent = detail?.events.find(
    (item) => item.detection_id === detail.case.opened_detection_id
  );
  const comparisonEvent = detail
    ? [...detail.events].reverse().find(
        (item) => item.event_type === "rectification_candidate" || item.event_type === "safe_redetection"
      ) || [...detail.events].reverse().find((item) => item.detection_id && item.detection_id !== detail.case.opened_detection_id)
    : undefined;
  const isActive = detail ? ["open", "needs_review"].includes(detail.case.current_status) : false;
  const latestSystemEvent = detail
    ? [...detail.events].reverse().find((item) => item.actor_type === "system" && item.detection_id)
    : undefined;
  const canConfirmRectified = latestSystemEvent?.event_type === "rectification_candidate";

  return (
    <section className="cover-review-workspace">
      <aside className="card-panel cover-case-list">
        <div className="section-heading">
          <div><h2>风险案件</h2><p>共 {total} 条</p></div>
          <button className="ghost-button slim" type="button" disabled={loading} onClick={() => void loadCases()}>刷新</button>
        </div>
        <label className="cover-case-filter">
          <span>案件状态</span>
          <select value={status} onChange={(event) => setStatus(event.target.value)} disabled={loading}>
            <option value="needs_review">待复核</option>
            <option value="confirmed_rectified">已确认整改</option>
            <option value="false_positive">误报</option>
            <option value="unavailable">已失联</option>
            <option value="">全部</option>
          </select>
        </label>
        <div className="cover-case-list-items">
          {cases.map((item) => (
            <button key={item.case_id} type="button" className={selectedId === item.case_id ? "active" : ""} onClick={() => void loadDetail(item.case_id)}>
              <span><strong title={item.video_title}>{item.video_title}</strong><i className={item.current_status}>{STATUS_LABELS[item.current_status] || item.current_status}</i></span>
              <small>{item.video_id}</small>
              <p>{item.opened_summary}</p>
              <time>{formatTime(item.updated_at)}</time>
            </button>
          ))}
          {!loading && cases.length === 0 && <div className="cover-case-empty">当前筛选下没有案件</div>}
        </div>
        <div className="cover-run-pagination">
          <span>{total ? `${offset + 1}-${Math.min(offset + PAGE_SIZE, total)} / ${total}` : "0 / 0"}</span>
          <div>
            <button className="outline-button slim" type="button" disabled={loading || offset === 0} onClick={() => void loadCases(Math.max(offset - PAGE_SIZE, 0), "")}>上一页</button>
            <button className="outline-button slim" type="button" disabled={loading || offset + PAGE_SIZE >= total} onClick={() => void loadCases(offset + PAGE_SIZE, "")}>下一页</button>
          </div>
        </div>
      </aside>

      <article className="card-panel cover-case-detail">
        {error && <div className="cover-import-message error" role="alert">{error}</div>}
        {notice && <div className="cover-import-message success" role="status">{notice}</div>}
        {detail ? (
          <>
            <header className="cover-case-detail-heading">
              <div><span className="eyebrow">CASE REVIEW</span><h2>{detail.case.video_title}</h2><p>{detail.case.video_id} · 建案于 {formatTime(detail.case.opened_at)}</p></div>
              <div><span className={`cover-case-status ${detail.case.current_status}`}>{STATUS_LABELS[detail.case.current_status] || detail.case.current_status}</span><a className="outline-button slim" href={detail.case.video_url} target="_blank" rel="noreferrer">打开视频</a></div>
            </header>

            <div className="cover-evidence-compare">
              <EvidenceCard caseId={detail.case.case_id} title="首次风险证据" event={openedEvent} emptyText="首次风险图片缺失" />
              <EvidenceCard caseId={detail.case.case_id} title="最新复测证据" event={comparisonEvent} emptyText="尚未形成复测证据" />
            </div>

            <section className="cover-case-timeline">
              <div className="section-heading"><div><h3>案件时间线</h3><p>系统检测与人工结论分开记录</p></div></div>
              <div>
                {detail.events.map((item) => (
                  <article key={item.case_event_id} className={item.event_type}>
                    <i />
                    <div><span><strong>{EVENT_LABELS[item.event_type] || item.event_type}</strong><time>{formatTime(item.created_at)}</time></span><p>{item.summary || item.reason}</p>{item.evidence && <small>{item.evidence}</small>}</div>
                  </article>
                ))}
              </div>
            </section>

            <section className="cover-case-actions">
              <div><h3>人工处置</h3><p>{isActive ? "结论必须基于页面中的新旧封面证据。" : "该案件已关闭，如需继续处理可重新打开。"}</p></div>
              <textarea value={note} onChange={(event) => setNote(event.target.value)} placeholder="填写判断依据、核查过程或后续跟进说明" maxLength={1000} disabled={busy} />
              <div>
                {isActive ? (
                  <>
                    <button className="primary-button" type="button" disabled={busy || !canConfirmRectified} title={canConfirmRectified ? "" : "仅换图后的安全复测可以确认整改"} onClick={() => void submitReview("confirm_rectified")}>确认已整改</button>
                    <button className="outline-button" type="button" disabled={busy} onClick={() => void submitReview("keep_open")}>继续跟进</button>
                    <button className="outline-button" type="button" disabled={busy} onClick={() => void submitReview("false_positive")}>标记误报</button>
                    <button className="ghost-button danger" type="button" disabled={busy} onClick={() => void submitReview("mark_unavailable")}>标记失联</button>
                  </>
                ) : (
                  <button className="outline-button" type="button" disabled={busy} onClick={() => void submitReview("reopen")}>重新打开案件</button>
                )}
              </div>
            </section>
          </>
        ) : (
          <div className="cover-run-detail-empty">{loading ? "正在加载风险案件..." : "从左侧选择一个案件开始复核"}</div>
        )}
      </article>
    </section>
  );
}
