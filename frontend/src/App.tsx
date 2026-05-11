import { useEffect, useState } from "react";
import { AuthGate } from "./components/AuthGate";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Map as MapView } from "./components/Map";
import type { Region } from "./components/Map/types";
import { ResetPasswordView } from "./components/ResetPasswordView";
import { StatusBar } from "./components/StatusBar";
import { StatusPage } from "./components/StatusPage";
import { ToastContainer, showToast } from "./components/Toast";
import { apiFetch, logout as serverLogout } from "./auth";

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export interface UserSummary {
  id: number;
  email: string;
  email_verified: boolean;
  is_admin: boolean;
}

export interface StravaStatus {
  connected: boolean;
  athlete_name: string | null;
  athlete_icon_url: string | null;
}

function readAndStripParam(name: string): string | null {
  const url = new URL(window.location.href);
  const v = url.searchParams.get(name);
  if (v != null) {
    url.searchParams.delete(name);
    window.history.replaceState({}, "", url.toString());
  }
  return v;
}

export default function App() {
  // /status is a standalone public page — split into its own component so
  // hooks order in the main app stays consistent across renders.
  if (window.location.pathname === "/status") {
    return (
      <>
        <StatusPage />
        <ToastContainer />
      </>
    );
  }
  return <MainApp />;
}

function MainApp() {
  // null = session check in flight; UserSummary = authed; false = not authed.
  // The httpOnly cookie is invisible to JS, so we have to ask the backend.
  const [user, setUser] = useState<UserSummary | false | null>(null);
  // Regions the user can access. null until /regions completes.
  const [regions, setRegions] = useState<Region[] | null>(null);
  const [stravaStatus, setStravaStatus] = useState<StravaStatus>({
    connected: false,
    athlete_name: null,
    athlete_icon_url: null,
  });
  // Captured once on mount so a re-render doesn't clear the URL prematurely.
  const [resetToken] = useState<string | null>(() => readAndStripParam("reset"));

  async function refreshUser(): Promise<UserSummary | false> {
    const r = await fetch(`${API_URL}/auth/me`, { credentials: "include" });
    if (!r.ok) {
      setUser(false);
      return false;
    }
    const u = (await r.json()) as UserSummary;
    setUser(u);
    return u;
  }

  async function refreshStravaStatus() {
    const r = await apiFetch(`${API_URL}/strava/status`);
    if (r.ok) setStravaStatus(await r.json());
  }

  async function handleLogout() {
    await serverLogout();
    setUser(false);
  }

  async function handleResendVerification() {
    if (!user) return;
    await fetch(`${API_URL}/auth/verify-email/request`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: user.email }),
    });
    showToast("Verification email sent — check your inbox.", "info");
  }

  async function handleDeleteAccount() {
    const ok = window.confirm(
      "Delete your account permanently? This cannot be undone.\n\n" +
      "Your Strava connection will also be revoked.",
    );
    if (!ok) return;
    const r = await apiFetch(`${API_URL}/auth/me`, { method: "DELETE" });
    if (r.ok) {
      setUser(false);
      showToast("Account deleted.", "info");
    } else {
      showToast("Couldn't delete account — try again later.", "error");
    }
  }

  // One-shot verify-email handler — fires regardless of auth state. After
  // success, refresh /auth/me so the banner disappears for logged-in users.
  useEffect(() => {
    const verifyToken = readAndStripParam("verify");
    if (!verifyToken) return;
    (async () => {
      const r = await fetch(`${API_URL}/auth/verify-email/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: verifyToken }),
      });
      if (r.ok) {
        showToast("Email verified!", "success");
        // If the user is logged in, surface the new verified state immediately.
        await refreshUser().catch(() => {});
      } else {
        showToast("Verification link is invalid or has expired.", "error");
      }
    })();
  }, []);

  // Probe the session cookie once on mount. Skipped while the reset flow is
  // active so we don't flash AuthGate behind the reset form.
  useEffect(() => {
    if (resetToken) return;
    refreshUser().catch(() => setUser(false));
  }, [resetToken]);

  // Token expired mid-session → back to login.
  useEffect(() => {
    const handler = () => setUser(false);
    window.addEventListener("whumpf:unauthorized", handler);
    return () => window.removeEventListener("whumpf:unauthorized", handler);
  }, []);

  // On auth or page load: check for OAuth callback result, then load Strava status.
  useEffect(() => {
    if (!user) return;
    const stravaParam = readAndStripParam("strava");
    if (stravaParam === "connected") {
      showToast("Strava connected.", "success");
    } else if (stravaParam === "denied" || stravaParam === "error") {
      showToast("Strava connection failed.", "error");
    }
    refreshStravaStatus();
  }, [user]);

  // Fetch the user's region registry on auth. The map can't initialize
  // without bbox/center metadata, so we block its mount until this resolves.
  useEffect(() => {
    if (!user) {
      setRegions(null);
      return;
    }
    apiFetch(`${API_URL}/regions`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((data: Region[]) => {
        if (data.length === 0) {
          showToast("No regions are enabled for your account.", "error");
          setRegions([]);
          return;
        }
        setRegions(data);
      })
      .catch(() => {
        showToast("Couldn't load regions — try refreshing.", "error");
        setRegions([]);
      });
  }, [user]);

  // Reset-password flow — full-screen view above auth.
  if (resetToken) {
    return (
      <>
        <ResetPasswordView
          token={resetToken}
          onDone={() => {
            // Drop the token; AuthGate will mount on the next render because
            // `user` is still null/false at this point.
            window.location.search = "";
          }}
        />
        <ToastContainer />
      </>
    );
  }

  if (user === null) {
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

  if (!user) {
    return (
      <>
        <AuthGate onAuth={() => refreshUser()} />
        <ToastContainer />
      </>
    );
  }

  // Hold rendering of the map until we know which region to show. Same
  // splash as the auth probe — usually < 50ms after login.
  if (regions === null || regions.length === 0) {
    return (
      <>
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
        <ToastContainer />
      </>
    );
  }

  return (
    <div style={{ position: "relative", width: "100vw", height: "100vh" }}>
      <ErrorBoundary>
        <MapView
          user={user}
          region={regions[0]}
          onLogout={handleLogout}
          stravaStatus={stravaStatus}
          onStravaStatusChange={refreshStravaStatus}
          onResendVerification={handleResendVerification}
          onDeleteAccount={handleDeleteAccount}
        />
      </ErrorBoundary>
      <StatusBar />
      <ToastContainer />
    </div>
  );
}
