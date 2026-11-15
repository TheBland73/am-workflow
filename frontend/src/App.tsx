import { NavLink, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import RunsPage from "./pages/RunsPage";
import RunPage from "./pages/RunPage";
import ProvidersPage from "./pages/ProvidersPage";
import TemplatesPage from "./pages/TemplatesPage";

export default function App() {
  const location = useLocation();
  const navigate = useNavigate();
  const match = location.pathname.match(/^\/runs\/([^/]+)/);
  const runId = match?.[1]?.toUpperCase();

  return (
    <div className="ef-shell">
      <div className="ef-tri" aria-hidden="true">
        <i />
        <i />
        <i />
      </div>

      <header className="ef-cmdbar">
        <a
          className="brand"
          href="/runs"
          onClick={(event) => {
            event.preventDefault();
            navigate("/runs");
          }}
        >
          <span className="seal" aria-hidden="true">A</span>
          AM-WORKFLOW
        </a>
        <nav aria-label="分区">
          <NavLink to="/runs">
            运行<small>Runs</small>
          </NavLink>
          <NavLink to="/providers">
            供应商<small>Providers</small>
          </NavLink>
          <NavLink to="/templates">
            角色<small>Agents</small>
          </NavLink>
        </nav>
        <div className="util">
          <span className="ef-field">{runId ? `RUN ${runId}` : "W1 · LOCAL"}</span>
          <span className="ef-field">MOCK / REAL</span>
        </div>
      </header>

      <main className="am-main">
        <Routes>
          <Route path="/" element={<Navigate to="/runs" replace />} />
          <Route path="/runs" element={<RunsPage />} />
          <Route path="/runs/:runId" element={<RunPage />} />
          <Route path="/providers" element={<ProvidersPage />} />
          <Route path="/templates" element={<TemplatesPage />} />
          <Route path="*" element={<Navigate to="/runs" replace />} />
        </Routes>
      </main>
    </div>
  );
}
