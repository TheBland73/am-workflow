import { useEffect, useState } from "react";
import {
  BarChartOutlined,
  CloudServerOutlined,
  DeploymentUnitOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  PlayCircleOutlined,
  TeamOutlined,
} from "@ant-design/icons";
import { NavLink, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import RunsPage from "./pages/RunsPage";
import RunPage from "./pages/RunPage";
import PlansPage from "./pages/PlansPage";
import PlanPage from "./pages/PlanPage";
import ProvidersPage from "./pages/ProvidersPage";
import TemplatesPage from "./pages/TemplatesPage";
import AnalyticsPage from "./pages/AnalyticsPage";

const NAV = [
  { to: "/runs", label: "运行", en: "RUNS", icon: <PlayCircleOutlined /> },
  { to: "/plans", label: "编排", en: "PLANS", icon: <DeploymentUnitOutlined /> },
  { to: "/analytics", label: "数据统计", en: "ANALYTICS", icon: <BarChartOutlined /> },
  { to: "/providers", label: "供应商", en: "PROVIDERS", icon: <CloudServerOutlined /> },
  { to: "/templates", label: "角色", en: "AGENTS", icon: <TeamOutlined /> },
];

const COLLAPSE_KEY = "am.rail.collapsed";

export default function App() {
  const location = useLocation();
  const navigate = useNavigate();
  const match = location.pathname.match(/^\/runs\/([^/]+)/);
  const runId = match?.[1]?.toUpperCase();
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem(COLLAPSE_KEY) === "1";
    } catch {
      return false;
    }
  });

  useEffect(() => {
    try {
      localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "0");
    } catch {
      // storage is optional
    }
  }, [collapsed]);

  useEffect(() => {
    const query = window.matchMedia("(max-width: 1080px)");
    const apply = () => {
      if (query.matches) setCollapsed(true);
    };
    apply();
    query.addEventListener("change", apply);
    return () => query.removeEventListener("change", apply);
  }, []);

  return (
    <div className={`ef-shell${collapsed ? " is-collapsed" : ""}`}>
      <aside className="am-rail" aria-label="侧栏">
        <div className="am-rail-top">
          <button
            type="button"
            className="am-rail-brand"
            onClick={() => navigate("/runs")}
            title="AM-WORKFLOW"
          >
            <span className="seal" aria-hidden="true">A</span>
            <span className="am-rail-name">AM-WORKFLOW</span>
          </button>
          <button
            type="button"
            className="am-rail-toggle"
            onClick={() => setCollapsed((value) => !value)}
            aria-label={collapsed ? "展开侧栏" : "折叠侧栏"}
            title={collapsed ? "展开侧栏" : "折叠侧栏"}
          >
            {collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
          </button>
        </div>

        <nav className="am-rail-nav" aria-label="分区">
          {NAV.map((item) => (
            <NavLink key={item.to} to={item.to} title={item.label}>
              <span className="am-rail-icon" aria-hidden="true">{item.icon}</span>
              <span className="am-rail-label">
                <b>{item.label}</b>
                <small>{item.en}</small>
              </span>
            </NavLink>
          ))}
        </nav>

        <div className="am-rail-foot">
          {collapsed ? (
            <span className="am-rail-w1" title={runId ? `RUN ${runId} · MOCK / REAL` : "W1 · LOCAL"}>W1</span>
          ) : (
            <>
              <span className="am-rail-meta">{runId ? `RUN ${runId}` : "W1 · LOCAL"}</span>
              <span className="am-rail-meta">MOCK / REAL</span>
            </>
          )}
        </div>
      </aside>

      <main className="am-main">
        <Routes>
          <Route path="/" element={<Navigate to="/runs" replace />} />
          <Route path="/runs" element={<RunsPage />} />
          <Route path="/runs/:runId" element={<RunPage />} />
          <Route path="/plans" element={<PlansPage />} />
          <Route path="/plans/:planId" element={<PlanPage />} />
          <Route path="/analytics" element={<AnalyticsPage />} />
          <Route path="/providers" element={<ProvidersPage />} />
          <Route path="/templates" element={<TemplatesPage />} />
          <Route path="*" element={<Navigate to="/runs" replace />} />
        </Routes>
      </main>
    </div>
  );
}
