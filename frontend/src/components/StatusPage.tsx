import { useEffect, useState, type CSSProperties, type FormEvent } from "react";
import { apiFetch } from "../auth";

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

interface ServiceHealth {
  name: string;
  healthy: boolean;
  detail: string | null;
}

interface Banner {
  id: number;
  title: string;
  body: string;
  severity: "minor" | "major" | "critical";
  created_at: string;
  resolved_at: string | null;
}

interface StatusResponse {
  overall: "operational" | "degraded" | "outage";
  services: ServiceHealth[];
  active_banner: Banner | null;
  recent_banners: Banner[];
}

interface Me {
  email: string;
  is_admin: boolean;
}

const POLL_MS = 30_000;

const OVERALL_LABEL: Record<StatusResponse["overall"], string> = {
  operational: "All systems operational",
  degraded:    "Some systems are degraded",
  outage:      "Service outage",
};
const OVERALL_COLOR: Record<StatusResponse["overall"], string> = {
  operational: "#1a9641",
  degraded:    "#f4820a",
  outage:      "#d7191c",
};
const SEVERITY_COLOR: Record<Banner["severity"], string> = {
  minor:    "#4a90d9",
  major:    "#f4820a",
  critical: "#d7191c",
};

const PAGE_BG = "linear-gradient(135deg, #0d1117 0%, #161b22 100%)";
const PANEL_BG = "rgba(22,27,34,0.85)";
const PANEL_BORDER = "1px solid rgba(255,255,255,0.08)";
const TEXT = "#e8e8e8";
const MUTED = "#888";

export function StatusPage() {
  const [data, setData] = useState<StatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [me, setMe] = useState<Me | null>(null);

  async function refresh() {
    try {
      const r = await fetch(`${API_URL}/status`);
      if (!r.ok) throw new Error(String(r.status));
      setData(await r.json());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "fetch failed");
    }
  }

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, POLL_MS);
    return () => clearInterval(t);
  }, []);

  // Find out if the current viewer is admin so we can render the form.
  useEffect(() => {
    fetch(`${API_URL}/auth/me`, { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((u) => setMe(u as Me | null))
      .catch(() => setMe(null));
  }, []);

  return (
    <div
      style={{
        minHeight: "100vh",
        background: PAGE_BG,
        color: TEXT,
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        padding: "40px 16px",
        boxSizing: "border-box",
      }}
    >
      <div style={{ maxWidth: 720, margin: "0 auto" }}>
        <header style={{ marginBottom: 28 }}>
          <a
            href="/"
            style={{
              color: MUTED, fontSize: 12, textDecoration: "none",
              letterSpacing: "0.05em",
            }}
          >
            ← whumpf
          </a>
          <h1 style={{ margin: "8px 0 0", fontSize: 26, fontWeight: 700 }}>Status</h1>
        </header>

        {error && !data && (
          <ErrorBox message="Couldn't reach the status API." />
        )}

        {data && (
          <>
            <OverallCard data={data} />
            {data.active_banner && (
              <BannerCard banner={data.active_banner} prominent />
            )}
            <ServicesList services={data.services} />
            {data.recent_banners.filter((b) => b.resolved_at).length > 0 && (
              <RecentHistory banners={data.recent_banners.filter((b) => b.resolved_at)} />
            )}
            {me?.is_admin && (
              <AdminControls data={data} onChange={refresh} />
            )}
          </>
        )}
      </div>
    </div>
  );
}

// ── components ─────────────────────────────────────────────────────────────────

function OverallCard({ data }: { data: StatusResponse }) {
  const color = OVERALL_COLOR[data.overall];
  return (
    <div
      style={{
        background: PANEL_BG,
        border: `1px solid ${color}55`,
        borderLeft: `4px solid ${color}`,
        borderRadius: 8,
        padding: "16px 18px",
        marginBottom: 16,
        display: "flex",
        alignItems: "center",
        gap: 12,
      }}
    >
      <span
        style={{
          width: 14, height: 14, borderRadius: "50%",
          background: color, flexShrink: 0,
          boxShadow: `0 0 10px ${color}80`,
        }}
      />
      <div style={{ fontSize: 16, fontWeight: 600 }}>
        {OVERALL_LABEL[data.overall]}
      </div>
    </div>
  );
}

function ServicesList({ services }: { services: ServiceHealth[] }) {
  return (
    <div
      style={{
        background: PANEL_BG,
        border: PANEL_BORDER,
        borderRadius: 8,
        padding: "4px 18px",
        marginBottom: 16,
      }}
    >
      {services.map((s, i) => (
        <div
          key={s.name}
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            padding: "12px 0",
            borderTop: i > 0 ? "1px solid rgba(255,255,255,0.06)" : undefined,
          }}
        >
          <span style={{ textTransform: "capitalize", fontSize: 14 }}>{s.name}</span>
          <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span
              style={{
                width: 8, height: 8, borderRadius: "50%",
                background: s.healthy ? "#1a9641" : "#d7191c",
              }}
            />
            <span style={{ fontSize: 12, color: s.healthy ? "#1a9641" : "#d7191c" }}>
              {s.healthy ? "Operational" : "Down"}
            </span>
          </span>
        </div>
      ))}
    </div>
  );
}

function BannerCard({ banner, prominent }: { banner: Banner; prominent?: boolean }) {
  const color = SEVERITY_COLOR[banner.severity];
  return (
    <div
      style={{
        background: prominent ? `${color}15` : PANEL_BG,
        border: `1px solid ${color}55`,
        borderLeft: `4px solid ${color}`,
        borderRadius: 8,
        padding: "14px 16px",
        marginBottom: 12,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 12 }}>
        <div style={{ fontWeight: 600, fontSize: 14 }}>{banner.title}</div>
        <div style={{ fontSize: 11, color: MUTED, textTransform: "uppercase", letterSpacing: "0.05em" }}>
          {banner.severity}
        </div>
      </div>
      {banner.body && (
        <div style={{ fontSize: 13, color: "#bbb", marginTop: 6, whiteSpace: "pre-wrap", lineHeight: 1.5 }}>
          {banner.body}
        </div>
      )}
      <div style={{ fontSize: 11, color: MUTED, marginTop: 8 }}>
        Posted {new Date(banner.created_at).toLocaleString()}
        {banner.resolved_at && ` · Resolved ${new Date(banner.resolved_at).toLocaleString()}`}
      </div>
    </div>
  );
}

function RecentHistory({ banners }: { banners: Banner[] }) {
  return (
    <div style={{ marginTop: 24 }}>
      <h2 style={{ fontSize: 13, color: MUTED, fontWeight: 600, letterSpacing: "0.08em", textTransform: "uppercase", margin: "0 0 10px" }}>
        Recent history
      </h2>
      {banners.map((b) => <BannerCard key={b.id} banner={b} />)}
    </div>
  );
}

function ErrorBox({ message }: { message: string }) {
  return (
    <div
      style={{
        background: "rgba(208,52,44,0.12)",
        border: "1px solid rgba(208,52,44,0.4)",
        borderRadius: 6,
        padding: "10px 14px",
        color: "#f87171",
        fontSize: 13,
      }}
    >
      {message}
    </div>
  );
}

// ── admin controls ─────────────────────────────────────────────────────────────

function AdminControls({ data, onChange }: { data: StatusResponse; onChange: () => void }) {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [severity, setSeverity] = useState<Banner["severity"]>("minor");
  const [submitting, setSubmitting] = useState(false);
  const [adminError, setAdminError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setAdminError(null);
    setSubmitting(true);
    try {
      const r = await apiFetch(`${API_URL}/status`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, body, severity }),
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error(d.detail ?? `${r.status}`);
      }
      setTitle("");
      setBody("");
      setSeverity("minor");
      setOpen(false);
      onChange();
    } catch (err) {
      setAdminError(err instanceof Error ? err.message : "failed to post");
    } finally {
      setSubmitting(false);
    }
  }

  async function resolve(id: number) {
    const r = await apiFetch(`${API_URL}/status/${id}/resolve`, { method: "POST" });
    if (r.ok) onChange();
  }

  async function remove(id: number) {
    if (!window.confirm("Delete this banner permanently? Use Resolve instead if you want to keep it in the history.")) return;
    const r = await apiFetch(`${API_URL}/status/${id}`, { method: "DELETE" });
    if (r.ok) onChange();
  }

  const inputStyle: CSSProperties = {
    width: "100%",
    padding: "9px 12px",
    borderRadius: 5,
    border: "1px solid rgba(255,255,255,0.15)",
    background: "rgba(255,255,255,0.05)",
    color: TEXT,
    fontFamily: "inherit",
    fontSize: 13,
    outline: "none",
    boxSizing: "border-box",
  };

  return (
    <div
      style={{
        marginTop: 32,
        padding: "16px 18px",
        border: "1px dashed rgba(91,163,240,0.4)",
        borderRadius: 8,
        background: "rgba(91,163,240,0.06)",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: open ? 14 : 0 }}>
        <h2 style={{ fontSize: 12, color: "#5ba3f0", fontWeight: 600, letterSpacing: "0.08em", textTransform: "uppercase", margin: 0 }}>
          Admin
        </h2>
        <button
          onClick={() => setOpen((o) => !o)}
          style={{
            background: "none", border: "none", color: "#5ba3f0",
            fontSize: 13, cursor: "pointer", padding: 0,
            fontFamily: "inherit",
          }}
        >
          {open ? "Hide" : "Post new incident"}
        </button>
      </div>

      {open && (
        <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <input
            placeholder="Title (e.g. SNOTEL data unavailable)"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            required
            style={inputStyle}
          />
          <textarea
            placeholder="Optional details — visible on the public page"
            value={body}
            onChange={(e) => setBody(e.target.value)}
            rows={3}
            style={{ ...inputStyle, resize: "vertical", fontFamily: "inherit" }}
          />
          <select
            value={severity}
            onChange={(e) => setSeverity(e.target.value as Banner["severity"])}
            style={inputStyle}
          >
            <option value="minor">Minor — single feature degraded</option>
            <option value="major">Major — significant impact</option>
            <option value="critical">Critical — site down</option>
          </select>
          {adminError && <ErrorBox message={adminError} />}
          <button
            type="submit"
            disabled={submitting || !title.trim()}
            style={{
              padding: "9px 16px",
              border: "none",
              borderRadius: 5,
              background: "#5ba3f0",
              color: "#fff",
              fontSize: 13,
              fontWeight: 600,
              cursor: submitting ? "not-allowed" : "pointer",
              opacity: submitting ? 0.7 : 1,
              alignSelf: "flex-end",
            }}
          >
            {submitting ? "Posting…" : "Publish"}
          </button>
        </form>
      )}

      {/* Resolve / delete actions on existing banners */}
      {data.recent_banners.length > 0 && (
        <div style={{ marginTop: 14, paddingTop: 14, borderTop: "1px solid rgba(91,163,240,0.2)" }}>
          <div style={{ fontSize: 11, color: MUTED, marginBottom: 6 }}>Manage existing banners</div>
          {data.recent_banners.map((b) => (
            <div
              key={b.id}
              style={{
                display: "flex", alignItems: "center", justifyContent: "space-between",
                padding: "6px 0", fontSize: 12, color: "#bbb",
              }}
            >
              <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", marginRight: 12 }}>
                {b.title} <span style={{ color: MUTED }}>· {b.severity}{b.resolved_at ? " · resolved" : ""}</span>
              </span>
              <span style={{ display: "flex", gap: 10 }}>
                {!b.resolved_at && (
                  <button
                    onClick={() => resolve(b.id)}
                    style={{ background: "none", border: "none", color: "#1a9641", cursor: "pointer", padding: 0, fontSize: 12, fontFamily: "inherit" }}
                  >
                    Resolve
                  </button>
                )}
                <button
                  onClick={() => remove(b.id)}
                  style={{ background: "none", border: "none", color: "#d7191c", cursor: "pointer", padding: 0, fontSize: 12, fontFamily: "inherit" }}
                >
                  Delete
                </button>
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
