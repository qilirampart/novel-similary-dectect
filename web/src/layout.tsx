import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useMemo, useState } from "react";

import { Icon } from "./icons";
import type { NavItem, TopNavItem } from "./types";

const sideNavItems: NavItem[] = [
  { label: "总览看板", path: "/", icon: "dashboard" },
  { label: "单条比对", path: "/single-compare", icon: "search" },
  { label: "批量任务", path: "/batch-tasks", icon: "layers" },
  { label: "结果复核", path: "/review", icon: "review" },
  { label: "任务记录", path: "/task-records", icon: "tasks" },
  { label: "规则配置", path: "/rules", icon: "rules" },
  { label: "检索库管理", path: "/retrieval-library", icon: "database" },
  { label: "日志中心", path: "/logs", icon: "logs" },
  { label: "系统状态", path: "/system-status", icon: "status" }
];

const topNavItems: TopNavItem[] = [
  { label: "总览看板", path: "/" },
  { label: "单条比对", path: "/single-compare" },
  { label: "批量任务", path: "/batch-tasks" },
  { label: "结果复核", path: "/review" },
  { label: "系统状态", path: "/system-status" }
];

function matchesPath(target: string, current: string) {
  return target === "/" ? current === "/" : current.startsWith(target);
}

export function AppLayout() {
  const location = useLocation();
  const [collapsed, setCollapsed] = useState(false);

  const currentTopNav = useMemo(
    () => topNavItems.find((item) => matchesPath(item.path, location.pathname)),
    [location.pathname]
  );

  return (
    <div className={`app-shell${collapsed ? " is-collapsed" : ""}`}>
      <aside className="sidebar">
        <div className="brand-panel">
          <div className="brand-mark">2</div>
          {!collapsed && (
            <div>
              <div className="brand-title">小说库相似度比对</div>
              <div className="brand-subtitle">运营控制台</div>
            </div>
          )}
        </div>
        <button className="collapse-button" type="button" onClick={() => setCollapsed((value) => !value)}>
          <Icon name="arrow" />
        </button>
        <nav className="side-nav">
          <div className="nav-group">
            {sideNavItems.slice(0, 6).map((item) => (
              <NavLink
                key={item.path}
                to={item.path}
                end={item.path === "/"}
                className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}
              >
                <Icon name={item.icon} className="nav-icon" />
                {!collapsed && <span>{item.label}</span>}
              </NavLink>
            ))}
          </div>
          <div className="nav-divider" />
          <div className="nav-group">
            {sideNavItems.slice(6).map((item) => (
              <NavLink key={item.path} to={item.path} className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}>
                <Icon name={item.icon} className="nav-icon" />
                {!collapsed && <span>{item.label}</span>}
              </NavLink>
            ))}
          </div>
          <button className="nav-footer-button" type="button">
            <Icon name="arrow" className="nav-icon rotate-180" />
            {!collapsed && <span>收起导航</span>}
          </button>
        </nav>
      </aside>

      <div className="content-shell">
        <header className="topbar">
          <nav className="top-nav">
            {topNavItems.map((item) => (
              <NavLink
                key={item.path}
                to={item.path}
                end={item.path === "/"}
                className={({ isActive }) => `top-nav-item${isActive ? " active" : ""}`}
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
          <div className="topbar-meta">
            <button className="icon-button with-badge" type="button">
              <Icon name="bell" />
              <span className="badge-dot">3</span>
            </button>
            <div className="org-switcher">
              <span className="org-main">相似度比对控制台</span>
              <span className="org-sub">{currentTopNav?.label ?? "总览看板"}</span>
            </div>
            <div className="user-chip">
              <div className="user-avatar">A</div>
              <div>
                <div className="user-name">web-ui</div>
                <div className="user-role">操作员</div>
              </div>
            </div>
          </div>
        </header>
        <main className="page-shell">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
