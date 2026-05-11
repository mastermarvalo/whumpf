import type { ReactNode } from "react";
import type { Theme } from "./theme";

export function MobileSheet({
  open,
  onClose,
  theme,
  children,
}: {
  open: boolean;
  onClose: () => void;
  theme: Theme;
  children: ReactNode;
}) {
  return (
    <>
      <div
        onClick={onClose}
        style={{
          position: "fixed",
          inset: 0,
          zIndex: 1001,
          background: "rgba(0,0,0,0.35)",
          opacity: open ? 1 : 0,
          pointerEvents: open ? "auto" : "none",
          transition: "opacity 0.25s",
        }}
      />
      <div
        style={{
          position: "fixed",
          bottom: 0,
          left: 0,
          right: 0,
          zIndex: 1002,
          background: theme.panel,
          borderRadius: "16px 16px 0 0",
          maxHeight: "82vh",
          overflowY: "auto",
          transform: open ? "translateY(0)" : "translateY(100%)",
          transition: "transform 0.3s cubic-bezier(0.32,0.72,0,1)",
          paddingBottom: "env(safe-area-inset-bottom)",
        }}
      >
        <div style={{ display: "flex", justifyContent: "center", padding: "10px 0 2px" }}>
          <div style={{ width: 40, height: 4, borderRadius: 2, background: theme.divider }} />
        </div>
        {children}
      </div>
    </>
  );
}
