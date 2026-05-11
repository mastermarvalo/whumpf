import type { ReactNode } from "react";
import { MOBILE_NAV_H, type Theme } from "./theme";
import { Z } from "./zIndex";

export function MobileNav({
  theme,
  layersOpen,
  toolsActive,
  onLayersToggle,
  onToolsToggle,
}: {
  theme: Theme;
  layersOpen: boolean;
  toolsActive: boolean;
  onLayersToggle: () => void;
  onToolsToggle: () => void;
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
        aria-label={label}
        aria-pressed={active}
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
        zIndex: Z.MOBILE_NAV,
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
        label="Tools"
        active={toolsActive}
        onClick={onToolsToggle}
        icon={
          <svg width="18" height="18" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
            <path d="M10.5 1.5a4 4 0 0 0-3.78 5.27L2 11.5 4.5 14l4.73-4.72A4 4 0 1 0 10.5 1.5z"/>
            <line x1="10.5" y1="1.5" x2="12.5" y2="3.5"/>
            <line x1="8.5" y1="3.5" x2="10.5" y2="5.5"/>
          </svg>
        }
      />
    </div>
  );
}
