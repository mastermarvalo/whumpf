import { useState } from "react";

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

interface Props {
  token: string;
  onDone: () => void;
}

/**
 * Full-screen password-reset form, shown when the user lands on
 * `/?reset=<token>` from the reset email. Sits above AuthGate so an
 * unauthenticated user can complete the reset without being prompted to
 * sign in first.
 */
export function ResetPasswordView({ token, onDone }: Props) {
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [done, setDone] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (password.length < 8) {
      setError("Password must be at least 8 characters");
      return;
    }
    if (password !== confirm) {
      setError("Passwords don't match");
      return;
    }
    setLoading(true);
    try {
      const r = await fetch(`${API_URL}/auth/password-reset/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, new_password: password }),
      });
      if (!r.ok) {
        const data = await r.json().catch(() => ({}));
        setError(data.detail ?? "Reset link is invalid or has expired");
        return;
      }
      setDone(true);
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
        <div style={{ textAlign: "center", marginBottom: 24 }}>
          <div style={{ fontSize: 22, fontWeight: 700, color: "#e8e8e8" }}>
            Reset your password
          </div>
        </div>

        {done ? (
          <>
            <div style={{ color: "#e8e8e8", fontSize: 14, marginBottom: 20, textAlign: "center" }}>
              Your password has been reset. Sign in with your new password.
            </div>
            <button
              onClick={onDone}
              style={{
                width: "100%",
                padding: "11px",
                borderRadius: 6,
                border: "none",
                background: "#4a90d9",
                color: "#fff",
                fontSize: 14,
                fontWeight: 600,
                cursor: "pointer",
              }}
            >
              Continue to sign in
            </button>
          </>
        ) : (
          <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <input
              type="password"
              placeholder="New password (8+ characters)"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              autoComplete="new-password"
              autoFocus
              style={inputStyle}
            />
            <input
              type="password"
              placeholder="Confirm new password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              required
              autoComplete="new-password"
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

            <button
              type="submit"
              disabled={loading}
              style={{
                width: "100%",
                padding: "11px",
                borderRadius: 6,
                border: "none",
                background: "#4a90d9",
                color: "#fff",
                fontSize: 14,
                fontWeight: 600,
                cursor: loading ? "not-allowed" : "pointer",
                opacity: loading ? 0.7 : 1,
                marginTop: 4,
              }}
            >
              {loading ? "…" : "Set new password"}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
