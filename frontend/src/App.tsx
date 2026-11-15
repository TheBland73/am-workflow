import { Layout, Menu } from "antd";
import {
  ApiOutlined, DeploymentUnitOutlined, IdcardOutlined, PlayCircleOutlined,
} from "@ant-design/icons";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import RunsPage from "./pages/RunsPage";
import RunPage from "./pages/RunPage";
import ProvidersPage from "./pages/ProvidersPage";
import TemplatesPage from "./pages/TemplatesPage";

const { Sider, Content } = Layout;

export default function App() {
  const location = useLocation();
  const navigate = useNavigate();
  const selected = location.pathname.startsWith("/providers")
    ? "/providers"
    : location.pathname.startsWith("/templates")
      ? "/templates"
      : "/runs";

  return (
    <Layout className="am-shell">
      <Sider width={208} theme="light" style={{ borderRight: "1px solid #e5e7eb" }}>
        <div className="am-logo">
          <span className="am-logo-mark">AM</span>
          <span>AM-Workflow</span>
        </div>
        <Menu
          mode="inline"
          selectedKeys={[selected]}
          onClick={({ key }) => navigate(key)}
          items={[
            { key: "/runs", icon: <PlayCircleOutlined />, label: "运行" },
            { key: "/providers", icon: <ApiOutlined />, label: "供应商与模型" },
            { key: "/templates", icon: <IdcardOutlined />, label: "角色模板" },
          ]}
        />
        <div style={{ padding: "18px 20px", color: "#9ca3af", fontSize: 12, lineHeight: 1.7 }}>
          <DeploymentUnitOutlined /> W1 最小原型
          <br />
          固定 SDLC 模板 · 人工门 · 证据判定
        </div>
      </Sider>
      <Layout>
        <Content className="am-content">
          <Routes>
            <Route path="/" element={<Navigate to="/runs" replace />} />
            <Route path="/runs" element={<RunsPage />} />
            <Route path="/runs/:runId" element={<RunPage />} />
            <Route path="/providers" element={<ProvidersPage />} />
            <Route path="/templates" element={<TemplatesPage />} />
            <Route path="*" element={<Navigate to="/runs" replace />} />
          </Routes>
        </Content>
      </Layout>
    </Layout>
  );
}
