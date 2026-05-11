import type { ReactNode } from "react";
import { MOBILE_NAV_H, type Theme } from "./theme";

export function MobileNav({
  theme,
  layersOpen,
  measureActive,
  onLayersToggle,
  onMeasureToggle,
}: {
  theme: Theme;
  layersOpen: boolean;
  measureActive: boolean;
  onLayersToggle: () => void;
  onMeasureToggle: () => void;
}) {
  function NavBtn({
    label,
    active,
    onClick,
    icon,
  }: {
    label: string;
    active: boolean;
    onClick: () => void;
    icon: ReactNode;
  }) {
    return (
      <button
        onClick={onClick}
        style={{
          flex: 1,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: 3,
          background: "none",
          border: "none",
          cursor: "pointer",
          color: active ? theme.accent : theme.muted,
          padding: "6px 0",
          fontSize: 10,
          fontFamily: "ui-sans-serif, system-ui, sans-serif",
          fontWeight: active ? 700 : 400,
          letterSpacing: "0.04em",
          minHeight: 44,
        }}
      >
        {icon}
        {label}
      </button>
    );
  }

  return (
    <div
      style={{
        position: "fixed",
        bottom: 0,
        left: 0,
        right: 0,
        height: MOBILE_NAV_H,
        background: theme.panel,
        borderTop: `1px solid ${theme.divider}`,
        display: "flex",
        alignItems: "stretch",
        zIndex: 1000,
        paddingBottom: "env(safe-area-inset-bottom)",
        boxShadow: "0 -2px 12px rgba(0,0,0,0.12)",
      }}
    >
      <NavBtn
        label="Layers"
        active={layersOpen}
        onClick={onLayersToggle}
        icon={
          <svg width="20" height="16" viewBox="0 0 20 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
            <line x1="1" y1="2" x2="19" y2="2"/>
            <line x1="1" y1="8" x2="19" y2="8"/>
            <line x1="1" y1="14" x2="19" y2="14"/>
          </svg>
        }
      />
      <NavBtn
        label="Measure"
        active={measureActive}
        onClick={onMeasureToggle}
        icon={
          <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
            <line x1="2" y1="16" x2="16" y2="2"/>
            <line x1="2" y1="16" x2="5" y2="16"/>
            <line x1="2" y1="16" x2="2" y2="13"/>
            <line x1="9" y1="9" x2="11" y2="7"/>
            <line x1="5.5" y1="12.5" x2="7.5" y2="10.5"/>
          </svg>
        }
      />
    </div>
  );
}
