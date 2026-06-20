import { useState } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { useAuth } from "../auth";
import { Icon } from "../icons";

type LoginLocationState = {
  from?: {
    pathname?: string;
  };
};

export function LoginPage() {
  const location = useLocation();
  const { user, loading, loginAction } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  if (!loading && user) {
    const state = location.state as LoginLocationState | null;
    return <Navigate to={state?.from?.pathname || "/"} replace />;
  }

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      await loginAction(username, password);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "登录失败");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="login-shell">
      <div className="login-backdrop" />
      <section className="login-panel">
        <div className="login-panel__hero">
          <span className="eyebrow">账号登录</span>
          <h1>小说相似度比对平台</h1>
          <p>先完成身份确认，再进入你的任务、结果和复核空间。</p>
          <div className="login-highlights">
            <div className="login-highlight-card">
              <Icon name="shield" />
              <div>
                <strong>用户隔离</strong>
                <span>任务、结果、复核仅对当前账号可见</span>
              </div>
            </div>
            <div className="login-highlight-card">
              <Icon name="layers" />
              <div>
                <strong>批量任务追踪</strong>
                <span>批量检测、导出和复核保持在同一账号视图下</span>
              </div>
            </div>
          </div>
        </div>

        <form className="login-form" onSubmit={handleSubmit}>
          <div className="login-form__head">
            <span className="eyebrow">本地环境</span>
            <h2>登录后进入工作台</h2>
            <p>请输入你的账号信息，进入当前账号对应的任务与复核空间。</p>
          </div>

          <label className="field-group">
            <span>用户名</span>
            <input value={username} onChange={(event) => setUsername(event.target.value)} placeholder="请输入用户名" autoComplete="username" />
          </label>

          <label className="field-group">
            <span>密码</span>
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="请输入密码"
              autoComplete="current-password"
            />
          </label>

          {error ? <div className="login-error">{error}</div> : null}

          <button className="primary-button login-submit" type="submit" disabled={submitting || loading}>
            {submitting ? "登录中..." : "进入系统"}
          </button>
        </form>
      </section>
    </div>
  );
}
