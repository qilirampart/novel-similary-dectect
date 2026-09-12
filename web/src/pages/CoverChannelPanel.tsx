import { useEffect, useRef, useState } from "react";

import {
  createCoverMonitorRun,
  listCoverMonitorChannels,
  type CoverChannelSummary
} from "../api";

const PAGE_SIZE = 50;

function formatNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN").format(Math.max(Number(value) || 0, 0));
}

function formatTime(value?: string | null): string {
  if (!value) return "尚未扫描";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function scanLabel(channel: CoverChannelSummary): string {
  if (channel.latest_scan_completeness === "complete") return "扫描完整";
  if (channel.latest_scan_completeness === "partial") return "部分完成";
  if (channel.latest_scan_completeness === "failed") return "扫描失败";
  return channel.last_scan_at ? "等待结果" : "尚未扫描";
}

export function CoverChannelPanel() {
  const [channels, setChannels] = useState<CoverChannelSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [keywordInput, setKeywordInput] = useState("");
  const [keyword, setKeyword] = useState("");
  const [activeOnly, setActiveOnly] = useState(true);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const requestRef = useRef(0);

  async function load(nextOffset = offset) {
    const requestId = ++requestRef.current;
    setLoading(true);
    setError("");
    try {
      const response = await listCoverMonitorChannels({
        keyword,
        active: activeOnly ? true : undefined,
        limit: PAGE_SIZE,
        offset: nextOffset
      });
      if (requestId !== requestRef.current) return;
      setChannels(response.items);
      setTotal(response.total);
      setOffset(response.offset);
    } catch (loadError) {
      if (requestId !== requestRef.current) return;
      setError(loadError instanceof Error ? loadError.message : "频道清单加载失败");
    } finally {
      if (requestId === requestRef.current) setLoading(false);
    }
  }

  useEffect(() => {
    void load(0);
  }, [keyword, activeOnly]);

  const pageIds = channels.map((channel) => channel.channel_pk);
  const pageSelected = pageIds.length > 0 && pageIds.every((id) => selected.has(id));

  function togglePage() {
    setSelected((previous) => {
      const next = new Set(previous);
      if (pageSelected) pageIds.forEach((id) => next.delete(id));
      else pageIds.forEach((id) => next.add(id));
      return next;
    });
  }

  function toggleChannel(channelPk: number) {
    setSelected((previous) => {
      const next = new Set(previous);
      if (next.has(channelPk)) next.delete(channelPk);
      else next.add(channelPk);
      return next;
    });
  }

  async function createSelectedRun() {
    if (selected.size === 0) return;
    setRunning(true);
    setMessage("");
    setError("");
    try {
      const run = await createCoverMonitorRun({
        intensity: "standard",
        includeShorts: true,
        forceRefresh: false,
        maxItemsPerScope: 0,
        channelPks: Array.from(selected)
      });
      setMessage(`巡检批次 ${run.run_id.slice(0, 12)} 已创建，共选择 ${formatNumber(selected.size)} 个频道。`);
    } catch (runError) {
      setError(runError instanceof Error ? runError.message : "创建巡检批次失败");
    } finally {
      setRunning(false);
    }
  }

  return (
    <section className="card-panel cover-channel-panel">
      <div className="section-heading cover-channel-heading">
        <div><h2>频道清单</h2><p>共 {formatNumber(total)} 个频道，勾选结果会跨分页保留。</p></div>
        <button className="primary-button" type="button" disabled={selected.size === 0 || running} onClick={() => void createSelectedRun()}>
          {running ? "正在创建..." : `巡检已选频道 (${formatNumber(selected.size)})`}
        </button>
      </div>

      <div className="cover-channel-toolbar">
        <form onSubmit={(event) => { event.preventDefault(); setKeyword(keywordInput.trim()); }}>
          <input value={keywordInput} onChange={(event) => setKeywordInput(event.target.value)} placeholder="搜索频道名、频道 ID 或代理商" aria-label="搜索频道" />
          <button className="outline-button slim" type="submit">搜索</button>
        </form>
        <label><input type="checkbox" checked={activeOnly} onChange={(event) => setActiveOnly(event.target.checked)} />只看启用频道</label>
        <button className="ghost-button slim" type="button" onClick={togglePage} disabled={channels.length === 0}>{pageSelected ? "取消本页全选" : "全选当前页"}</button>
        {selected.size > 0 && <button className="ghost-button slim" type="button" onClick={() => setSelected(new Set())}>清空选择</button>}
      </div>

      {(error || message) && <div className={`cover-channel-message${error ? " error" : ""}`} role={error ? "alert" : "status"}>{error || message}</div>}

      <div className="cover-channel-table">
        <div className="cover-channel-row head"><span>选择</span><span>频道</span><span>代理归属</span><span>视频 / 案件</span><span>最近扫描</span><span>状态</span></div>
        {channels.map((channel) => (
          <label className={`cover-channel-row${selected.has(channel.channel_pk) ? " selected" : ""}`} key={channel.channel_pk}>
            <span><input type="checkbox" checked={selected.has(channel.channel_pk)} onChange={() => toggleChannel(channel.channel_pk)} /></span>
            <span><strong title={channel.name}>{channel.name}</strong><small>{channel.channel_id}</small></span>
            <span>{channel.operator_name || "未分配"}</span>
            <span><strong>{formatNumber(channel.video_count)}</strong><small>{formatNumber(channel.open_case_count)} 个开放案件</small></span>
            <span><strong>{formatTime(channel.last_scan_at)}</strong><small>{channel.latest_scan_status || "无批次记录"}</small></span>
            <span><i className={channel.latest_scan_completeness || "pending"}>{scanLabel(channel)}</i></span>
          </label>
        ))}
        {!loading && channels.length === 0 && <div className="cover-channel-empty">没有符合当前条件的频道</div>}
        {loading && <div className="cover-channel-empty">正在加载频道清单...</div>}
      </div>

      <div className="cover-run-pagination">
        <span>第 {total === 0 ? 0 : offset + 1}-{Math.min(offset + PAGE_SIZE, total)} 条，共 {formatNumber(total)} 条</span>
        <div><button className="outline-button slim" type="button" disabled={offset <= 0 || loading} onClick={() => void load(Math.max(offset - PAGE_SIZE, 0))}>上一页</button><button className="outline-button slim" type="button" disabled={offset + PAGE_SIZE >= total || loading} onClick={() => void load(offset + PAGE_SIZE)}>下一页</button></div>
      </div>
    </section>
  );
}
