import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App as AntApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import App from "./App";
import ErrorBoundary from "./ErrorBoundary";
import "./styles/endfield.css";
import "./styles/console.css";
import "./styles/plan.css";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: "#161616",
          colorInfo: "#161616",
          colorLink: "#161616",
          colorText: "#161616",
          colorTextSecondary: "#4a4a46",
          colorBorder: "#d4d4d2",
          colorBgContainer: "#ffffff",
          borderRadius: 0,
          controlHeight: 32,
          fontSize: 13,
          fontFamily:
            'Saira, "Noto Sans SC", "Microsoft YaHei", system-ui, -apple-system, sans-serif',
        },
        components: {
          Button: { fontWeight: 600, primaryShadow: "none", defaultShadow: "none" },
          Table: { headerBg: "transparent", rowHoverBg: "#f7f7f5" },
          Modal: { contentBg: "#ffffff", headerBg: "#ffffff" },
          Drawer: { colorBgElevated: "#ffffff" },
        },
      }}
    >
      <AntApp>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            <ErrorBoundary>
              <App />
            </ErrorBoundary>
          </BrowserRouter>
        </QueryClientProvider>
      </AntApp>
    </ConfigProvider>
  </React.StrictMode>,
);
