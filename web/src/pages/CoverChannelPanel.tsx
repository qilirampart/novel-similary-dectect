import { useEffect, useRef, useState } from "react";

import {
  createCoverMonitorRun,
  deactivateCoverMonitorChannels,
  getCoverMonitorFilterOptions,
  listCoverMonitorChannelIds,
  listCoverMonitorChannels,
  type CoverFilterOptionsResponse,
  type CoverChannelSummary
} from "../api";

const PAGE_SIZE = 50;
const EMPTY_FILTER_OPTIONS: CoverFilterOptionsResponse = { operators: [], channels: [] };

function formatNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN").format(Math.max(Number(value) || 0, 0));
}

function formatTime(value?: string | null): string {
  if (!value) return "尚未扫描";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function scanLabel(channel: CoverChannelSummary): string {
  if (!channel.active) return "已停用";
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
  const [operatorPk, setOperatorPk] = useState<number | undefined>();
  const [filterOptions, setFilterOptions] = useState(EMPTY_FILTER_OPTIONS);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [selectingAll, setSelectingAll] = useState(false);
  const [deleting, setDeleting] = useState(false);
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
        operatorPk,
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
  }, [keyword, activeOnly, operatorPk]);

  useEffect(() => {
    void getCoverMonitorFilterOptions()
      .then(setFilterOptions)
      .catch((loadError) => setError(loadError instanceof Error ? loadError.message : "代理筛选项加载失败"));
  }, []);

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

  async function selectAllFiltered() {
    setSelectingAll(true);
    setMessage("");
    setError("");
    try {
      const response = await listCoverMonitorChannelIds({
        keyword,
        active: activeOnly ? true : undefined,
        operatorPk
      });
      if (response.truncated) {
        throw new Error(`当前筛选结果有 ${formatNumber(response.total)} 个频道，超过单批次 5,000 个频道的上限，请先缩小筛选范围。`);
      }
      setSelected((previous) => {
        const next = new Set(previous);
        response.channel_pks.forEach((id) => next.add(id));
        return next;
      });
      setMessage(`已选择当前筛选下的全部 ${formatNumber(response.total)} 个频道。`);
    } catch (selectError) {
      setError(selectError instanceof Error ? selectError.message : "全选频道失败");
    } finally {
      setSelectingAll(false);
    }
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

  async function deactivateSelected() {
    if (selected.size === 0 || deleting) return;
    const count = selected.size;
    if (!window.confirm(`确认清除选中的 ${formatNumber(count)} 个频道吗？\n\n该操作为软删除：频道、视频、检测结果和历史任务都会保留；取消“只看启用频道”后仍可查看。`)) return;
    setDeleting(true);
    setMessage("");
    setError("");
    try {
      const result = await deactivateCoverMonitorChannels(Array.from(selected));
      setSelected(new Set());
      setMessage(`已软删除 ${formatNumber(result.deactivated_count)} 个频道，历史数据均已保留。`);
      await load(0);
    } catch (deleteError) {
      setError(deleteError instanceof Error ? deleteError.message : "软删除频道失败");
    } finally {
      setDeleting(false);
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
        <label className="cover-channel-operator-filter">
          <span>代理归属</span>
          <select
            aria-label="按代理归属筛选频道"
            value={operatorPk ?? ""}
            disabled={loading}
            onChange={(event) => setOperatorPk(event.target.value === "" ? undefined : Number(event.target.value))}
          >
            <option value="">全部代理</option>
            {filterOptions.operators.map((item) => (
              <option value={item.operator_pk} key={item.operator_pk}>{item.name}（{formatNumber(item.channel_count)}）</option>
            ))}
          </select>
        </label>
        <label><input type="checkbox" checked={activeOnly} onChange={(event) => setActiveOnly(event.target.checked)} />只看启用频道</label>
        <button className="ghost-button slim" type="button" onClick={togglePage} disabled={channels.length === 0}>{pageSelected ? "取消本页全选" : "全选当前页"}</button>
        <button className="outline-button slim" type="button" onClick={() => void selectAllFiltered()} disabled={total === 0 || loading || selectingAll}>{selectingAll ? "正在全选..." : `全选所有 (${formatNumber(total)})`}</button>
        {selected.size > 0 && <button className="ghost-button slim danger" type="button" onClick={() => void deactivateSelected()} disabled={deleting || running}>{deleting ? "正在清除..." : `清除选中 (${formatNumber(selected.size)})`}</button>}
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
            <span><i className={!channel.active ? "inactive" : channel.latest_scan_completeness || "pending"}>{scanLabel(channel)}</i></span>
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
