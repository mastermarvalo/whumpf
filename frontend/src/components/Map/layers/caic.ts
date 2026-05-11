import maplibregl from "maplibre-gl";

export function addCaicLayers(map: maplibregl.Map) {
  if (map.getSource("caic-danger")) return;
  map.addSource("caic-danger", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({
    id: "caic-danger-fill",
    type: "fill",
    source: "caic-danger",
    paint: {
      "fill-color": ["get", "color"],
      "fill-opacity": 0.35,
    },
  });
  map.addLayer({
    id: "caic-danger-line",
    type: "line",
    source: "caic-danger",
    paint: {
      "line-color": ["get", "color"],
      "line-width": 1.5,
      "line-opacity": 0.8,
    },
  });
}

export function setCaicData(map: maplibregl.Map | null, geojson: object) {
  if (!map) return;
  const src = map.getSource("caic-danger") as maplibregl.GeoJSONSource | undefined;
  src?.setData(geojson as Parameters<typeof src.setData>[0]);
}

export function setCaicVisibility(map: maplibregl.Map | null, visible: boolean) {
  if (!map) return;
  const v = visible ? "visible" : "none";
  for (const id of ["caic-danger-fill", "caic-danger-line"]) {
    if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", v);
  }
}

// ── CAIC detail popup HTML ─────────────────────────────────────────────────────

const DANGER_COLORS: Record<string, string> = {
  low: "#00b200", moderate: "#f4e500", considerable: "#ff9933",
  high: "#d7191c", extreme: "#1a1a1a", noForecast: "#666",
};
const DANGER_LABELS: Record<string, string> = {
  low: "Low", moderate: "Moderate", considerable: "Considerable",
  high: "High", extreme: "Extreme", noForecast: "No Rating",
};
const ELEV_LABELS: Record<string, string> = { alp: "Alpine", tln: "Treeline", btl: "Below Treeline" };

function dangerBadge(level: string): string {
  const c = DANGER_COLORS[level] ?? "#666";
  const fg = level === "moderate" ? "#333" : "#fff";
  const lbl = DANGER_LABELS[level] ?? level;
  return `<span style="background:${c};color:${fg};border-radius:3px;padding:1px 5px;font-size:10px;font-weight:700">${lbl}</span>`;
}

function dangerTriangle(danger: { alp: string; tln: string; btl: string }): string {
  // Isoceles triangle W=60 H=60, divided into 3 equal-height bands.
  // At y=20: left=20, right=40. At y=40: left=10, right=50.
  const alp = DANGER_COLORS[danger.alp] ?? "#666";
  const tln = DANGER_COLORS[danger.tln] ?? "#666";
  const btl = DANGER_COLORS[danger.btl] ?? "#666";
  return `<svg width="60" height="60" viewBox="0 0 60 60" style="flex-shrink:0">
    <polygon points="10,40 50,40 60,60 0,60" fill="${btl}"/>
    <polygon points="20,20 40,20 50,40 10,40" fill="${tln}"/>
    <polygon points="30,0 20,20 40,20" fill="${alp}"/>
    <text x="30" y="55" text-anchor="middle" font-size="7" fill="rgba(0,0,0,0.5)" font-family="sans-serif">BTL</text>
    <text x="30" y="35" text-anchor="middle" font-size="7" fill="rgba(0,0,0,0.5)" font-family="sans-serif">TLN</text>
    <text x="30" y="16" text-anchor="middle" font-size="7" fill="rgba(0,0,0,0.45)" font-family="sans-serif">ALP</text>
  </svg>`;
}

function aspectRose(aspectElevations: string[], color: string): string {
  const cx = 20, cy = 20, ri = 6, ro = 17;
  const dirs = ["n","ne","e","se","s","sw","w","nw"];
  // Which base aspects are hit (any elevation)
  const hit = new Set(aspectElevations.map(ae => ae.split("_")[0]));
  const svgAngles: Record<string, number> = {
    n: -90, ne: -45, e: 0, se: 45, s: 90, sw: 135, w: 180, nw: 225
  };
  let paths = "";
  for (const dir of dirs) {
    const theta = (svgAngles[dir] * Math.PI) / 180;
    const half = (22.5 * Math.PI) / 180;
    const a1 = theta - half, a2 = theta + half;
    const x1i = cx + ri * Math.cos(a1), y1i = cy + ri * Math.sin(a1);
    const x2i = cx + ri * Math.cos(a2), y2i = cy + ri * Math.sin(a2);
    const x1o = cx + ro * Math.cos(a1), y1o = cy + ro * Math.sin(a1);
    const x2o = cx + ro * Math.cos(a2), y2o = cy + ro * Math.sin(a2);
    const f = (n: number) => n.toFixed(1);
    const fill = hit.has(dir) ? color : "rgba(255,255,255,0.1)";
    paths += `<path d="M${f(x1i)},${f(y1i)} A${ri},${ri} 0 0,1 ${f(x2i)},${f(y2i)} L${f(x2o)},${f(y2o)} A${ro},${ro} 0 0,0 ${f(x1o)},${f(y1o)} Z" fill="${fill}" stroke="rgba(255,255,255,0.15)" stroke-width="0.5"/>`;
  }
  return `<svg width="40" height="40" viewBox="0 0 40 40" style="flex-shrink:0">
    ${paths}
    <text x="${cx}" y="${cy - ro - 2}" text-anchor="middle" font-size="6" fill="rgba(255,255,255,0.5)" font-family="sans-serif">N</text>
  </svg>`;
}

interface CaicProblem {
  label: string; likelihood: string; size_min: string; size_max: string;
  aspects: string[]; elevations: string[]; aspect_elevations: string[];
}
export interface CaicZoneDetail {
  forecaster: string; valid_date: string;
  danger: { alp: string; tln: string; btl: string };
  problems: CaicProblem[];
  link: string;
}

// Problem type → accent color for the aspect rose
const PROBLEM_COLORS: Record<string, string> = {
  "Wet Loose": "#ff9933", "Wind Slab": "#4a90d9", "Storm Slab": "#9b59b6",
  "Persistent Slab": "#e05a2b", "Deep Persistent Slab": "#c0392b",
  "Cornice": "#f4e500", "Glide Avalanche": "#2ecc71",
};

export function buildCaicDetailHtml(d: CaicZoneDetail): string {
  const meta = [
    d.forecaster ? `<span>${d.forecaster}</span>` : "",
    d.valid_date ? `<span style="color:#888">${d.valid_date}</span>` : "",
  ].filter(Boolean).join(" · ");

  const roseHtml = dangerTriangle(d.danger);
  const roseLabels = (["alp","tln","btl"] as const).map((k) =>
    `<div style="font-size:11px;line-height:1.6">
      <span style="color:#aaa;font-size:10px">${ELEV_LABELS[k]}</span><br/>
      ${dangerBadge(d.danger[k])}
    </div>`
  ).join("");

  const dangerSection = `
    <div style="display:flex;gap:10px;align-items:center;margin:8px 0 10px">
      ${roseHtml}
      <div style="display:flex;flex-direction:column;gap:2px">${roseLabels}</div>
    </div>`;

  const problemsHtml = d.problems.length === 0 ? "" : `
    <div style="font-weight:700;font-size:10px;letter-spacing:.05em;color:#aaa;margin-bottom:4px">AVALANCHE PROBLEMS</div>
    ${d.problems.map((p) => {
      const color = PROBLEM_COLORS[p.label] ?? "#e05a2b";
      const elevStr = p.elevations.map(e => ELEV_LABELS[e] ?? e).join(", ");
      const sizeStr = p.size_min && p.size_max ? `Size ${p.size_min}–${p.size_max}` : "";
      return `<div style="display:flex;gap:8px;align-items:flex-start;margin-bottom:8px">
        ${aspectRose(p.aspect_elevations, color)}
        <div style="line-height:1.5">
          <div style="font-weight:700;font-size:12px;color:${color}">${p.label}</div>
          <div style="font-size:11px">${[p.likelihood, sizeStr].filter(Boolean).join(" · ")}</div>
          ${p.aspects.length ? `<div style="font-size:10px;color:#aaa">${p.aspects.join(", ")}${elevStr ? ` · ${elevStr}` : ""}</div>` : ""}
        </div>
      </div>`;
    }).join("")}`;

  const linkHtml = `<a href="${d.link}" target="_blank" rel="noopener"
    style="color:#e05a2b;font-size:11px;text-decoration:none">CAIC Backcountry Forecast →</a>`;

  return `<div style="font-family:ui-sans-serif,system-ui,sans-serif;font-size:12px;color:#eee;line-height:1.4">
    <div style="font-weight:700;font-size:13px;margin-bottom:2px">CAIC Forecast</div>
    ${meta ? `<div style="font-size:10px;color:#888;margin-bottom:6px">${meta}</div>` : ""}
    ${dangerSection}
    ${problemsHtml}
    ${linkHtml}
  </div>`;
}
