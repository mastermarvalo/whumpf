export const THEMES = {
  light: {
    panel: "rgba(255,255,255,0.95)",
    text: "#1a1a1a",
    muted: "#777",
    divider: "rgba(0,0,0,0.08)",
    soonBg: "rgba(0,0,0,0.06)",
    soonText: "#aaa",
    accent: "#4a90d9",
  },
  dark: {
    panel: "rgba(18,18,28,0.96)",
    text: "#e8e8e8",
    muted: "#666",
    divider: "rgba(255,255,255,0.08)",
    soonBg: "rgba(255,255,255,0.05)",
    soonText: "#555",
    accent: "#5ba3f0",
  },
};

export type Theme = typeof THEMES.light;

export const MOBILE_NAV_H = 56; // px — height of the bottom nav bar on mobile
