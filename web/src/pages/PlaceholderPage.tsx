type PlaceholderKind = "rules" | "library" | "logs";

const pageMeta: Record<PlaceholderKind, { title: string; subtitle: string; bullets: string[] }> = {
  rules: {
    title: "规则配置",
    subtitle: "规则管理占位页，后续承接策略与口径配置流程。",
    bullets: ["定义复核规则", "映射风险标签", "接入导出策略"]
  },
  library: {
    title: "检索库管理",
    subtitle: "检索库浏览占位页，后续承接数据与索引管理能力。",
    bullets: ["浏览数据集", "检查章节分组", "校验索引状态"]
  },
  logs: {
    title: "日志中心",
    subtitle: "日志查看占位页，后续承接运维诊断能力。",
    bullets: ["筛选最近事件", "追踪 Worker 错误", "检查请求历史"]
  }
};

export function PlaceholderPage({ kind }: { kind: PlaceholderKind }) {
  const meta = pageMeta[kind];

  return (
    <div className="page-grid">
      <section className="page-heading">
        <div>
          <span className="eyebrow">模块</span>
          <h1>{meta.title}</h1>
          <p>{meta.subtitle}</p>
        </div>
      </section>

      <section className="dark-card-panel placeholder-hero">
        <div>
          <span className="eyebrow">占位页</span>
          <h2>{meta.title}</h2>
          <p>该模块保留给控制台的下一阶段迭代使用。</p>
        </div>
      </section>

      <section className="placeholder-grid span-full">
        <article className="card-panel placeholder-card">
          <h3>后续规划</h3>
          <ul className="bullet-list">
            {meta.bullets.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </article>
        <article className="card-panel placeholder-card">
          <h3>页面结构</h3>
          <div className="placeholder-wire">
            <div className="wire-bar" />
            <div className="wire-grid">
              <span />
              <span />
              <span />
              <span />
            </div>
            <div className="wire-table">
              <span />
              <span />
              <span />
            </div>
          </div>
        </article>
        <article className="card-panel placeholder-card">
          <h3>当前状态</h3>
          <div className="status-stack">
            <div className="status-row">
              <span>界面</span>
              <strong>已预留</strong>
            </div>
            <div className="status-row">
              <span>API</span>
              <strong>待接入</strong>
            </div>
            <div className="status-row">
              <span>范围</span>
              <strong>预留中</strong>
            </div>
          </div>
        </article>
      </section>
    </div>
  );
}
