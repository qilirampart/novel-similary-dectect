import { useEffect, useState } from "react";

import { getSystemStatus } from "../api";
import { formatModuleLabel, formatOperationalImpact, formatRuntimeStateLabel, formatSystemCardLabel, formatSystemHint, formatSystemItemLabel, formatSystemServiceTitle, formatTaskMessage, formatTaskStatusLabel } from "../displayText";
import { Icon } from "../icons";
import { StatusState } from "../StatusState";

export function SystemStatusPage() {
  const [payload, setPayload] = useState<{
    cards: Array<{ label: string; value: string; hint: string; tone: string }>;
    services: Array<{ title: string; status: string; items: Array<[string, string]> }>;
    recent_exceptions: Array<{ time: string; module: string; type: string; summary: string; impact: string }>;
    slow_tasks: Array<{ id: string; type: string; duration: string; status: string; time: string }>;
  } | null>(null);
  const [error, setError] = useState("");

  async function load() {
    try {
      const response = await getSystemStatus();
      setPayload(response.payload);
      setError("");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "加载系统状态失败");
    }
  }

  useEffect(() => {
    void load();
  }, []);

  return (
    <div className="page-grid system-grid">
      <section className="page-heading">
        <div>
          <span className="eyebrow">系统</span>
          <h1>系统状态</h1>
          <p>集中查看数据库、工作进程和导出链路的运行健康度。</p>
        </div>
        <div className="toolbar-compact">
          <button className="chip-button with-icon" type="button" onClick={() => void load()}>
            <Icon name="refresh" />
            刷新
          </button>
        </div>
      </section>

      {error && (
        <StatusState
          className="span-full"
          title="系统状态加载失败"
          description={error}
          tone="error"
          variant="inline"
          icon="warning"
        />
      )}

      <section className="stats-grid stats-grid--six">
        {payload?.cards?.length ? (
          payload.cards.map((item) => (
            <article key={item.label} className={`stat-card compact tone-${item.tone}`}>
              <div className="stat-body">
                <span className="stat-label">{formatSystemCardLabel(item.label)}</span>
                <strong className="stat-value">{formatRuntimeStateLabel(String(item.value ?? ""))}</strong>
                <span className="stat-delta">{formatSystemHint(item.hint)}</span>
              </div>
            </article>
          ))
        ) : (
          <StatusState
            className="span-full"
            title={error ? "系统卡片暂不可用" : "正在加载系统卡片"}
            description={error ? "请稍后刷新，或先检查业务库和运行目录是否可用。" : "正在同步数据库、任务和导出链路的运行状态。"}
            tone={error ? "error" : "info"}
            icon={error ? "warning" : "pulse"}
          />
        )}
      </section>

      <section className="service-grid span-full">
        {payload?.services?.length ? (
          payload.services.map((panel) => (
            <article key={panel.title} className="dark-card-panel service-panel">
              <div className="service-head">
                <div>
                  <span className="eyebrow">{formatSystemServiceTitle(panel.title)}</span>
                  <h2>{formatRuntimeStateLabel(panel.status)}</h2>
                </div>
                <span className="online-dot large" />
              </div>
              <div className="service-items">
                {panel.items.map(([label, value]) => (
                  <div key={label} className="service-item">
                    <span>{formatSystemItemLabel(label)}</span>
                    <strong>{value}</strong>
                  </div>
                ))}
              </div>
            </article>
          ))
        ) : (
          <StatusState
            className="span-full"
            title={error ? "服务明细暂不可用" : "正在加载服务明细"}
            description={error ? "当前无法读取数据库、语义后端和运行目录的状态明细。" : "正在同步检索数据库、业务库和运行目录信息。"}
            tone={error ? "error" : "info"}
            variant="dark"
            icon={error ? "warning" : "database"}
          />
        )}
      </section>

      <section className="card-panel">
        <div className="section-heading">
          <div>
            <h2>最近异常</h2>
            <p>查看任务存储中的最近失败与告警事件。</p>
          </div>
        </div>
        <div className="incident-list">
          {payload?.recent_exceptions?.length ? (
            payload.recent_exceptions.map((item) => (
              <article key={`${item.time}-${item.module}-${item.type}`} className="incident-row">
                <div>
                  <strong>{formatModuleLabel(item.module)}</strong>
                  <p>{formatTaskMessage(item.summary)}</p>
                </div>
                <div className="incident-meta">
                  <span>{item.time}</span>
                  <span>{formatRuntimeStateLabel(item.type)}</span>
                  <span>{formatOperationalImpact(item.impact)}</span>
                </div>
              </article>
            ))
          ) : (
            <StatusState title="当前没有异常记录" description="最近一次状态同步没有发现新的失败或告警事件。" icon="shield" />
          )}
        </div>
      </section>

      <section className="card-panel">
        <div className="section-heading">
          <div>
            <h2>慢任务</h2>
            <p>查看最近任务耗时和状态变化。</p>
          </div>
        </div>
        <div className="incident-list">
          {payload?.slow_tasks?.length ? (
            payload.slow_tasks.map((item) => (
              <article key={item.id} className="incident-row">
                <div>
                  <strong>{item.id}</strong>
                  <p>{item.type === "batch_compare" ? "批量比对" : item.type}</p>
                </div>
                <div className="incident-meta">
                  <span>{item.duration}</span>
                  <span>{formatTaskStatusLabel(item.status)}</span>
                  <span>{item.time}</span>
                </div>
              </article>
            ))
          ) : (
            <StatusState title="当前没有慢任务记录" description="最近同步的任务耗时没有出现需要额外关注的慢任务。" icon="clock" />
          )}
        </div>
      </section>

      <section className="dark-card-panel span-full suggestion-panel">
        <div className="section-heading inverted">
          <div>
            <h2>运行建议</h2>
            <p>基于当前运行状态的快速提醒。</p>
          </div>
        </div>
        <div className="suggestion-list">
          <article>
            <strong>关注运行中和排队中任务数量，及时识别任务积压。</strong>
            <p>如果排队任务持续上升而工作进程数量没有变化，优先检查工作进程健康度和导出路径。</p>
          </article>
          <article>
            <strong>利用改写检测的回退结果识别语义召回薄弱点。</strong>
            <p>如果结果长期只落在回退链路，通常意味着语义层不可用，或查询文本需要更宽的召回范围。</p>
          </article>
          <article>
            <strong>保持导出目录可写。</strong>
            <p>摘要导出和复核导出应持续对最近完成的任务可用。</p>
          </article>
        </div>
      </section>
    </div>
  );
}
