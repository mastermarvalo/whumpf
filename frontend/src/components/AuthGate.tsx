import { useState } from "react";
import { setToken } from "../auth";

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

type Tab = "login" | "register";

export function AuthGate({ onAuth }: { onAuth: () => void }) {
  const [tab, setTab] = useState<Tab>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      let r: Response;
      if (tab === "login") {
        const body = new URLSearchParams({ username: email, password });
        r = await fetch(`${API_URL}/auth/token`, {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: body.toString(),
        });
      } else {
        r = await fetch(`${API_URL}/auth/register`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email, password }),
        });
      }
      if (!r.ok) {
        const data = await r.json().catch(() => ({}));
        setError(data.detail ?? "Something went wrong");
        return;
      }
      const { access_token } = await r.json();
      setToken(access_token);
      onAuth();
    } catch {
      setError("Could not reach the server");
    } finally {
      setLoading(false);
    }
  }

  const inputStyle: React.CSSProperties = {
    width: "100%",
    padding: "10px 12px",
    borderRadius: 6,
    border: "1.5px solid rgba(255,255,255,0.15)",
    background: "rgba(255,255,255,0.07)",
    color: "#e8e8e8",
    fontFamily: "ui-sans-serif, system-ui, sans-serif",
    fontSize: 14,
    outline: "none",
    boxSizing: "border-box",
  };

  const btnStyle: React.CSSProperties = {
    width: "100%",
    padding: "11px",
    borderRadius: 6,
    border: "none",
    background: "#4a90d9",
    color: "#fff",
    fontFamily: "ui-sans-serif, system-ui, sans-serif",
    fontSize: 14,
    fontWeight: 600,
    cursor: loading ? "not-allowed" : "pointer",
    opacity: loading ? 0.7 : 1,
    marginTop: 4,
  };

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "linear-gradient(135deg, #0d1117 0%, #161b22 100%)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
      }}
    >
      <div
        style={{
          width: 360,
          background: "rgba(22,27,34,0.95)",
          border: "1px solid rgba(255,255,255,0.1)",
          borderRadius: 12,
          padding: "32px 28px 28px",
          boxShadow: "0 24px 64px rgba(0,0,0,0.5)",
        }}
      >
        {/* Logo / title */}
        <div style={{ textAlign: "center", marginBottom: 24 }}>
          <div style={{ fontSize: 28, fontWeight: 800, color: "#e8e8e8", letterSpacing: "-0.02em" }}>
            whumpf
          </div>
          <div style={{ fontSize: 13, color: "#666", marginTop: 4 }}>
            backcountry terrain intelligence
          </div>
        </div>

        {/* Tab switcher */}
        <div
          style={{
            display: "flex",
            background: "rgba(255,255,255,0.05)",
            borderRadius: 7,
            padding: 3,
            marginBottom: 20,
          }}
        >
          {(["login", "register"] as Tab[]).map((t) => (
            <button
              key={t}
              onClick={() => { setTab(t); setError(null); }}
              style={{
                flex: 1,
                padding: "7px 0",
                border: "none",
                borderRadius: 5,
                background: tab === t ? "rgba(255,255,255,0.1)" : "transparent",
                color: tab === t ? "#e8e8e8" : "#666",
                fontFamily: "ui-sans-serif, system-ui, sans-serif",
                fontSize: 13,
                fontWeight: tab === t ? 600 : 400,
                cursor: "pointer",
                transition: "all 0.15s",
              }}
            >
              {t === "login" ? "Sign in" : "Create account"}
            </button>
          ))}
        </div>

        <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <input
            type="email"
            placeholder="Email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            autoComplete="email"
            style={inputStyle}
          />
          <input
            type="password"
            placeholder={tab === "register" ? "Password (8+ characters)" : "Password"}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            autoComplete={tab === "login" ? "current-password" : "new-password"}
            style={inputStyle}
          />

          {error && (
            <div
              style={{
                background: "rgba(208,52,44,0.15)",
                border: "1px solid rgba(208,52,44,0.3)",
                borderRadius: 5,
                padding: "8px 12px",
                color: "#f87171",
                fontSize: 13,
              }}
            >
              {error}
            </div>
          )}

          <button type="submit" disabled={loading} style={btnStyle}>
            {loading ? "…" : tab === "login" ? "Sign in" : "Create account"}
          </button>
        </form>
      </div>
    </div>
  );
}
