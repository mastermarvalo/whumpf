import { Component, type ErrorInfo, type ReactNode } from "react";
import { Z } from "./Map/zIndex";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Whumpf UI crashed:", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div
        role="alert"
        style={{
          position: "fixed",
          inset: 0,
          zIndex: Z.ERROR_BOUNDARY,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: 16,
          background: "#0d1117",
          color: "#e8e8e8",
          fontFamily: "ui-sans-serif, system-ui, sans-serif",
          padding: 24,
          textAlign: "center",
        }}
      >
        <div style={{ fontSize: 18, fontWeight: 700 }}>Something broke</div>
        <div style={{ fontSize: 13, color: "#999", maxWidth: 480, lineHeight: 1.5 }}>
          The map UI hit an unrecoverable error. Reloading usually fixes it.
          If this keeps happening, the browser console has the details.
        </div>
        <button
          onClick={() => window.location.reload()}
          style={{
            padding: "8px 22px",
            border: "none",
            borderRadius: 6,
            background: "#4a90d9",
            color: "#fff",
            fontSize: 13,
            fontWeight: 600,
            cursor: "pointer",
          }}
        >
          Reload
        </button>
      </div>
    );
  }
}
