// Z-index ladder for the Map UI. Previously every floating element claimed
// 999 or 1000, which meant mobile bottom sheets, info panels, and the
// activity card all stacked unpredictably depending on mount order.
//
// Tiers (low → high):
//   MAP_OVERLAY     — small badges anchored to the map (region lock, 1m pill)
//   FLOATING_PANEL  — bottom-anchored floating cards (info, measure, strava)
//   TOP_PANEL       — top-anchored controls always visible on desktop
//                     (LayerPanel, SearchBar)
//   FLY_OUT         — ToolboxPanel + dropdowns that should sit above panels
//   MOBILE_NAV      — bottom nav bar on mobile (must clear panels above it)
//   SHEET_BACKDROP  — modal dim for MobileSheet
//   SHEET           — MobileSheet body
//   TOAST           — transient notifications, always on top
//   ERROR_BOUNDARY  — full-screen crash screen

export const Z = {
  MAP_OVERLAY:    100,
  FLOATING_PANEL: 500,
  TOP_PANEL:      1000,
  FLY_OUT:        1100,
  MOBILE_NAV:     1200,
  SHEET_BACKDROP: 1500,
  SHEET:          1501,
  TOAST:          2000,
  ERROR_BOUNDARY: 9000,
} as const;
