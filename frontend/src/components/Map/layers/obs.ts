import maplibregl from "maplibre-gl";

const OBS_COLORS: Record<string, string> = {
  caught: "#d7191c",
  avy:    "#ff9933",
  field:  "#5ba3f5",
};

export function addObsLayers(map: maplibregl.Map) {
  if (map.getSource("caic-obs")) return;
  map.addSource("caic-obs", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({
    id: "caic-obs-circles",
    type: "symbol",
    source: "caic-obs",
    layout: {
      "text-field": "▲",
      "text-size": 18,
      "text-allow-overlap": true,
      "text-ignore-placement": true,
      "text-anchor": "center",
    },
    paint: {
      "text-color": ["match", ["get", "obs_type"],
        "caught", OBS_COLORS.caught,
        "avy",    OBS_COLORS.avy,
        OBS_COLORS.field,
      ],
      "text-halo-color": "rgba(255,255,255,0.9)",
      "text-halo-width": 1.5,
    },
  });
}

export function setObsData(map: maplibregl.Map | null, geojson: object) {
  if (!map) return;
  const src = map.getSource("caic-obs") as maplibregl.GeoJSONSource | undefined;
  src?.setData(geojson as Parameters<typeof src.setData>[0]);
}

export function setObsVisibility(map: maplibregl.Map | null, visible: boolean) {
  if (!map || !map.getLayer("caic-obs-circles")) return;
  map.setLayoutProperty("caic-obs-circles", "visibility", visible ? "visible" : "none");
}

export function buildObsPopupHtml(p: Record<string, unknown>): string {
  const obsType = String(p.obs_type ?? "field");
  const color = OBS_COLORS[obsType] ?? OBS_COLORS.field;
  const typeLabel = obsType === "caught" ? "Caught in Avalanche"
    : obsType === "avy" ? "Avalanche Observed"
    : "Field Observation";
  const observer = p.is_anonymous ? "Anonymous" : String(p.observer ?? "Unknown");
  const org = p.organization ? `<span style="color:#aaa"> · ${p.organization}</span>` : "";
  const date = p.observed_at
    ? new Date(String(p.observed_at)).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })
    : "";
  const zone = p.zone ? `<div style="font-size:11px;color:#aaa;margin-top:1px">${p.zone}</div>` : "";
  const route = p.route ? `<div style="font-size:11px;color:#ccc;margin-top:4px"><b>Route:</b> ${String(p.route).slice(0, 120)}</div>` : "";
  const desc = p.description
    ? `<div style="font-size:12px;color:#ddd;margin-top:8px;line-height:1.45">${String(p.description).slice(0, 300)}${String(p.description).length > 300 ? "…" : ""}</div>`
    : "";
  const avyBadge = Number(p.avy_count) > 0
    ? `<span style="background:${OBS_COLORS.avy};color:#fff;border-radius:3px;padding:1px 6px;font-size:10px;margin-left:6px">${p.avy_count} avy obs</span>`
    : "";
  const link = p.link
    ? `<div style="margin-top:10px"><a href="${p.link}" target="_blank" rel="noopener" style="color:#5ba3f5;font-size:11px;text-decoration:none">View full report →</a></div>`
    : "";
  return `
    <div style="font-family:ui-sans-serif,system-ui,sans-serif;min-width:220px;max-width:290px">
      <div style="display:flex;align-items:center;gap:7px;margin-bottom:6px">
        <span style="width:9px;height:9px;border-radius:50%;background:${color};flex-shrink:0;border:1.5px solid #fff"></span>
        <b style="font-size:13px;color:#eee">${typeLabel}</b>${avyBadge}
      </div>
      <div style="font-size:12px;color:#ccc">${observer}${org}</div>
      <div style="font-size:11px;color:#888">${date}</div>
      ${zone}${route}${desc}${link}
    </div>`;
}
