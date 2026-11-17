import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/**
 * 页面级兜底：单个面板的数据形状异常不应该让整个应用白屏。
 * 这里只捕获渲染期错误，网络错误仍由 react-query / 组件自己处理。
 */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("[render-error]", error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return <>{this.props.children}</>;
    return (
      <div className="ef-crash">
        <div className="ef-crash-card">
          <span className="ef-crash-code">RENDER ERROR</span>
          <h1>页面渲染出错</h1>
          <p>{error.message || "未知错误"}</p>
          <pre>{error.stack}</pre>
          <div className="ef-crash-actions">
            <button type="button" onClick={() => this.setState({ error: null })}>
              重试
            </button>
            <button type="button" onClick={() => window.location.reload()}>
              重新加载
            </button>
          </div>
        </div>
      </div>
    );
  }
}
