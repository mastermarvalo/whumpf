import { useEffect, useState } from "react";
import type { Theme } from "./theme";
import { Z } from "./zIndex";

interface Args {
  theme: Theme;
  mobile: boolean;
  mobileBottom: number;
  /** Subscribes a dismiss callback to first-interaction events on the map. */
  onDismissBind: (dismiss: () => void) => () => void;
}

const AUTO_DISMISS_MS = 8000;

export function StartHint({ theme, mobile, mobileBottom, onDismissBind }: Args) {
  const [visible, setVisible] = useState(true);

  useEffect(() => {
    if (!visible) return;
    const dismiss = () => setVisible(false);
    const unbind = onDismissBind(dismiss);
    const timer = setTimeout(dismiss, AUTO_DISMISS_MS);
    return () => {
      unbind();
      clearTimeout(timer);
    };
  }, [visible, onDismissBind]);

  if (!visible) return null;

  return (
    <div
      role="status"
      style={{
        position: "fixed",
        bottom: mobile ? mobileBottom + 4 : 28,
        left: "50%",
        transform: "translateX(-50%)",
        zIndex: Z.MAP_OVERLAY,
        background: theme.panel,
        color: theme.text,
        padding: "6px 12px",
        borderRadius: 16,
        fontSize: 12,
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        boxShadow: "0 2px 8px rgba(0,0,0,0.2)",
        border: `1px solid ${theme.divider}`,
        userSelect: "none",
        pointerEvents: "none",
        opacity: 0.92,
        whiteSpace: "nowrap",
      }}
    >
      Zoom in or enable layers for more terrain detail
    </div>
  );
}
