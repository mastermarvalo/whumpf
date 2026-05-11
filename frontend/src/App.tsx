import { useEffect, useState } from "react";
import { AuthGate } from "./components/AuthGate";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Map as MapView } from "./components/Map";
import { StatusBar } from "./components/StatusBar";
import { apiFetch, logout as serverLogout } from "./auth";

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export interface StravaStatus {
  connected: boolean;
  athlete_name: string | null;
  athlete_icon_url: string | null;
}

export default function App() {
  // null = session check in flight; true/false = decided.
  // The httpOnly cookie is invisible to JS, so we have to ask the backend.
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [stravaStatus, setStravaStatus] = useState<StravaStatus>({
    connected: false,
    athlete_name: null,
    athlete_icon_url: null,
  });

  async function refreshStravaStatus() {
    const r = await apiFetch(`${API_URL}/strava/status`);
    if (r.ok) setStravaStatus(await r.json());
  }

  async function handleLogout() {
    await serverLogout();
    setAuthed(false);
  }

  // Probe the session cookie once on mount.
  useEffect(() => {
    fetch(`${API_URL}/auth/me`, { credentials: "include" })
      .then((r) => setAuthed(r.ok))
      .catch(() => setAuthed(false));
  }, []);

  // Token expired mid-session → back to login.
  useEffect(() => {
    const handler = () => setAuthed(false);
    window.addEventListener("whumpf:unauthorized", handler);
    return () => window.removeEventListener("whumpf:unauthorized", handler);
  }, []);

  // On auth or page load: check for OAuth callback result, then load Strava status.
  useEffect(() => {
    if (!authed) return;
    const params = new URLSearchParams(window.location.search);
    const stravaParam = params.get("strava");
    if (stravaParam) {
      window.history.replaceState({}, "", window.location.pathname);
    }
    refreshStravaStatus();
  }, [authed]);

  if (authed === null) {
    // Brief flash while /auth/me is in flight. Better than showing AuthGate
    // to a logged-in user only to swap to the map a moment later.
    return (
      <div
        style={{
          position: "fixed",
          inset: 0,
          background: "linear-gradient(135deg, #0d1117 0%, #161b22 100%)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: "#555",
          fontFamily: "ui-sans-serif, system-ui, sans-serif",
          fontSize: 13,
          letterSpacing: "0.04em",
        }}
      >
        whumpf
      </div>
    );
  }

  if (!authed) {
    return <AuthGate onAuth={() => setAuthed(true)} />;
  }

  return (
    <div style={{ position: "relative", width: "100vw", height: "100vh" }}>
      <ErrorBoundary>
        <MapView
          onLogout={handleLogout}
          stravaStatus={stravaStatus}
          onStravaStatusChange={refreshStravaStatus}
        />
      </ErrorBoundary>
      <StatusBar />
    </div>
  );
}
